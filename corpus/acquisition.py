from __future__ import annotations

import email.utils
import http.client
import ipaddress
import math
import socket
import ssl
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit, parse_qsl, urlencode


def diagnostic_url(value):
    u=urlsplit(value)
    # Redirect targets can contain temporary signed credentials. Never persist
    # query values from a remote redirect as provenance/log output.
    return urlunsplit((u.scheme,u.netloc,u.path,urlencode([(k,'REDACTED') for k,_ in parse_qsl(u.query)]),''))


class AcquisitionError(Exception):
    def __init__(self, kind, message, *, status=None, retry_after=0, transient=False):
        super().__init__(message)
        self.kind, self.status = kind, status
        self.retry_after, self.transient = retry_after, transient


def validated_url(value):
    try:
        if not isinstance(value,str) or len(value)>8192:
            raise ValueError()
        u = urlsplit(value)
        if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password:
            raise ValueError()
        if u.port not in (None,80,443):
            raise ValueError()
        if any(ord(c)<32 for c in value):
            raise ValueError()
        return urlunsplit((u.scheme,u.netloc,u.path or "/",u.query,""))
    except (TypeError, ValueError):
        raise AcquisitionError("unsafe_url", "Only credential-free HTTP(S) URLs on ports 80/443 are supported")


# NAT64 prefixes are "global" to the ipaddress module but can embed private IPv4 destinations.
NAT64_NETWORKS = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"),
                  ipaddress.ip_network("2002::/16"), ipaddress.ip_network("2001::/32"))  # plus 6to4 and Teredo
STALL_SECONDS = 30
URL_SAFE = "/%:@!$&'()*+,;=~-._"
# Saved under a neutral name: a fetched page must not be able to place a runnable file in the user's folders.
RUNNABLE_EXTENSIONS = {".exe",".com",".scr",".pif",".msi",".msp",".bat",".cmd",".ps1",".psm1",".vbs",".vbe",".js",".jse",".wsf",".wsh",".hta",".lnk",".url",".reg",".dll",".cpl",".jar",".appx",".msix",".iso",".img",".vhd",".vhdx",".scf",".inf",".mht",".mhtml",".xll",".xlam",".docm",".xlsm",".pptm",".dotm"}


def public_addresses(host, port, lookup=socket.getaddrinfo):
    records = lookup(host, port, type=socket.SOCK_STREAM)
    if not records:
        raise AcquisitionError("dns", "DNS returned no addresses", transient=True)
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        if not address.is_global or (address.version == 6 and any(address in net for net in NAT64_NETWORKS)) or (address.version == 6 and address.ipv4_mapped and not address.ipv4_mapped.is_global):
            raise AcquisitionError("unsafe_url", "Source resolves to a private or non-public address")
    return records


def close_socket(sock):
    try:sock.shutdown(socket.SHUT_RDWR)
    except OSError:pass
    sock.close()


class DeadlineConnection(http.client.HTTPConnection):
    deadline_timer = None

    def close(self):
        if self.deadline_timer:self.deadline_timer.cancel()
        super().close()


def open_public(url, timeout=45):
    """Connect to a vetted IP directly, retaining the original TLS hostname/Host header."""
    u = urlsplit(validated_url(url))
    port = u.port or (443 if u.scheme == "https" else 80)
    deadline=time.monotonic()+timeout
    records = public_addresses(u.hostname,port)
    last = None
    for family, kind, proto, _, address in records[:4]:
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('Source connection deadline exceeded')
        sock = socket.socket(family,kind,proto)
        socket_ref=[sock]
        timer=threading.Timer(remaining,lambda ref=socket_ref:close_socket(ref[0]))
        timer.daemon=True;timer.start()
        try:
            sock.settimeout(remaining)
            sock.connect(address)
            if u.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock,server_hostname=u.hostname)
                socket_ref[0]=sock
            conn = DeadlineConnection(u.hostname,port,timeout=remaining)
            conn.deadline_timer=timer
            conn.sock = sock
            # Paths copied from documents often contain spaces or non-ASCII characters.
            conn.request("GET", urlunsplit(("","",quote(u.path or "/",safe=URL_SAFE),quote(u.query,safe=URL_SAFE+"?"),"")), headers={
                "User-Agent":"SourceCorpus/1.0 (local research document library)",
                "Accept":"application/pdf,text/html,application/xml,text/plain,*/*;q=0.5",
                "Accept-Encoding":"identity",
            })
            response = conn.getresponse()
            # Headers arrived. From here the 180-second budget in download() and a stall limit apply.
            timer.cancel()
            conn.deadline_timer = None
            sock.settimeout(STALL_SECONDS)
            return conn, response
        except Exception as exc:
            timer.cancel()
            sock.close()
            last = exc
    raise last


def retry_seconds(value):
    if not value:
        return 0
    try:
        seconds=float(value)
        return min(86400,max(0,seconds)) if math.isfinite(seconds) else 0
    except ValueError:
        try:
            return min(86400,max(0,email.utils.parsedate_to_datetime(value).timestamp()-time.time()))
        except (ValueError,TypeError,OverflowError):
            return 0


def challenge_page(data):
    text = data[:200000].decode("utf-8",errors="replace").lower()
    # Match distinctive gateway/challenge phrases, not an incidental discussion of CAPTCHA.
    return any(marker in text for marker in (
        "federal register :: request access", "<title>just a moment", "<title>access denied",
        "verify you are human", "checking your browser before accessing", "enable javascript and cookies to continue",
        "your request has been flagged as potentially automated", "incapsula incident id",
    ))


class PDFLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.preferred, self.links = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("name","").lower() == "citation_pdf_url" and a.get("content"):
            self.preferred.append(a["content"])
        if tag in ("a","link") and a.get("href") and (a.get("type") == "application/pdf" or urlsplit(a["href"]).path.lower().endswith(".pdf")):
            self.links.append(a["href"])


def pdf_link(data, base_url):
    parser = PDFLinks()
    parser.feed(data.decode("utf-8",errors="replace"))
    links = list(dict.fromkeys(urljoin(base_url,v) for v in (parser.preferred or parser.links)))
    if len(links) != 1:
        raise AcquisitionError("needs_resolution", "PDF landing page does not identify exactly one PDF; supply a direct or alternate URL")
    return links[0]


class Downloader:
    def __init__(self, library, max_bytes=256*1024*1024, transport=open_public):
        self.library, self.max_bytes, self.transport = library, max_bytes, transport

    def download(self, url, destination: Path, expected_format=None):
        current = validated_url(url)
        visited, started = set(), time.monotonic()
        for _ in range(10):
            if current in visited:
                raise AcquisitionError("redirect_loop", "Source contains a redirect/link loop")
            visited.add(current)
            host = urlsplit(current).hostname
            with self.library.connect() as db:
                row = db.execute("SELECT next_at FROM hosts WHERE host=?", (host,)).fetchone()
            if row and row[0]-time.time()>2:
                raise AcquisitionError("host_cooldown", "Source host is cooling down", retry_after=row[0]-time.time(), transient=True)
            self.library.throttle(host)
            conn = response = None
            try:
                remaining=180-(time.monotonic()-started)
                if remaining<=0:raise AcquisitionError('timeout','Acquisition exceeded 180 seconds',transient=True)
                conn, response = self.transport(current,timeout=min(45,remaining)) if self.transport is open_public else self.transport(current)
                status = response.status
                if status in (301,302,303,307,308):
                    location = response.getheader("Location")
                    if not location:
                        raise AcquisitionError("redirect", "Redirect omitted Location",status=status)
                    current = validated_url(urljoin(current,location))
                    continue
                if status < 200 or status >= 300:
                    delay = retry_seconds(response.getheader("Retry-After"))
                    transient = status in (408,425,429,500,502,503,504)
                    kind = "needs_access" if status in (401,403) else "not_found" if status in (404,410) else "rate_limited" if status == 429 else "http_error"
                    if delay and transient:
                        self.library.cool_down(host,delay)
                    raise AcquisitionError(kind, f"HTTP {status} from {host}",status=status,retry_after=delay,transient=transient)
                length = response.getheader("Content-Length")
                if length and int(length)>self.max_bytes:
                    raise AcquisitionError("too_large", f"Source exceeds configured {self.max_bytes} byte limit",status=status)
                media = (response.getheader("Content-Type") or "application/octet-stream").split(";")[0].lower().strip()
                size = 0
                with destination.open("wb") as out:
                    # read1 returns available bytes; read(size) can wait forever
                    # for 64 KiB from a server that continually drips a few bytes.
                    read=getattr(response,'read1',response.read)
                    while block := read(65536):
                        size += len(block)
                        if size>self.max_bytes:
                            raise AcquisitionError("too_large", f"Source exceeds configured {self.max_bytes} byte limit",status=status)
                        if time.monotonic()-started>180:
                            raise AcquisitionError("timeout", "Acquisition exceeded 180 seconds",transient=True)
                        out.write(block)
                if not size:
                    raise AcquisitionError("empty_file", "Source response is empty",status=status)
                if length and int(length) != size:
                    raise AcquisitionError("truncated", "Content-Length does not match received bytes",status=status,transient=True)
                with destination.open("rb") as inp:
                    head = inp.read(200000)
                if challenge_page(head):
                    raise AcquisitionError("needs_access", "Received an access-challenge page instead of source content",status=status)
                wants_pdf = expected_format == ".pdf" or urlsplit(url).path.lower().endswith(".pdf")
                if wants_pdf and not head.startswith(b"%PDF-"):
                    if "html" in media or b"<html" in head.lower():
                        if size>2*1024*1024:
                            raise AcquisitionError("needs_resolution", "Oversized PDF landing page")
                        current = validated_url(pdf_link(destination.read_bytes(),current))
                        continue
                    raise AcquisitionError("format_mismatch", "Requested PDF but received another file type")
                extension = ".pdf" if head.startswith(b"%PDF-") else {"text/html":".html","application/xhtml+xml":".html","text/plain":".txt","application/xml":".xml","text/xml":".xml","application/json":".json","text/csv":".csv","application/vnd.openxmlformats-officedocument.wordprocessingml.document":".docx","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":".xlsx","application/vnd.openxmlformats-officedocument.presentationml.presentation":".pptx","application/epub+zip":".epub"}.get(media)
                extension = extension or Path(urlsplit(current).path).suffix.lower() or ".bin"
                if extension in RUNNABLE_EXTENSIONS:
                    extension = ".bin"
                return {"final_url":diagnostic_url(current),"http_status":status,"media_type":media,"extension":extension,"bytes":size,"fetched_at":time.time()}
            except AcquisitionError:
                raise
            except ssl.SSLCertVerificationError as exc:
                raise AcquisitionError("tls_error","The source's security certificate could not be verified") from exc
            except (http.client.InvalidURL,UnicodeError) as exc:
                raise AcquisitionError("unsafe_url","The URL contains characters that cannot be requested") from exc
            except (OSError,http.client.HTTPException,ValueError) as exc:
                kind = "tls_error" if isinstance(exc,ssl.SSLError) else "dns_error" if isinstance(exc,socket.gaierror) else "connection_error"
                raise AcquisitionError(kind,"Source connection failed ("+type(exc).__name__+")",transient=True) from exc
            finally:
                if response:
                    response.close()
                if conn:
                    conn.close()
        raise AcquisitionError("redirect_limit", "Source exceeded redirect/link limit")
