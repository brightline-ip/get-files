from __future__ import annotations

import hmac
import ipaddress
import json
import os
import sys
import threading
import time
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .processing import Processor, WorkerLock
from .storage import encoded
from .get_files import submit


class BoundedHTTPServer(ThreadingHTTPServer):
    def __init__(self,*args,**kwargs):
        self.slots=threading.BoundedSemaphore(32)
        super().__init__(*args,**kwargs)

    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try: super().process_request(request,address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self,request,address):
        timer=threading.Timer(30,self.shutdown_request,args=(request,))
        timer.daemon=True;timer.start()
        try:
            request.settimeout(10)
            super().process_request_thread(request,address)
        finally:
            timer.cancel();self.slots.release()

    def handle_error(self,request,address):
        print(encoded({'event':'http_handler_error','error_type':sys.exc_info()[0].__name__}),file=sys.stderr,flush=True)


def serve(library, token, host="127.0.0.1", port=8766, *, stop_event=None, ready_callback=None, processor=None, spoke_config=None):
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError('The master-token API must bind to loopback; use the restricted TLS spoke for n8n')
    if len(token)<32:
        raise ValueError("A service token of at least 32 characters is required")
    stop = stop_event or threading.Event()
    # Opt-in (GET_FILES_ALLOW_DOCKER_HOST=1) for a client running in Docker Desktop, such as the Research
    # Verification Workbench container, which reaches this loopback service as host.docker.internal.
    # The bearer token is still required; Origin and Fetch Metadata checks are unchanged.
    docker_host = os.environ.get('GET_FILES_ALLOW_DOCKER_HOST') == '1'
    processor = processor or Processor(library)
    worker_error = None

    def worker():
        nonlocal worker_error
        while not stop.is_set():
            try:
                if worker_error: library.recover()
                if shutil.disk_usage(library.root).free<1024**3:
                    stop.wait(5);continue
                job = library.claim()
                if job:
                    if job['attempts']>4:
                        library.finish(job['id'],'failed',error_kind='retry_exhausted',message='Worker recovery attempt limit reached')
                    else: processor.process(job)
                else: stop.wait(0.5)
                worker_error = None
            except Exception as exc:
                worker_error = type(exc).__name__
                print(encoded({'event':'worker_error','error_type':worker_error}),file=sys.stderr,flush=True)
                stop.wait(1)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Do not log query strings, document titles, headers, or request bodies.
            pass

        def send_json(self, value, status=200):
            data = encoded(value).encode()
            self.send_response(status)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Content-Length",str(len(data)))
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.end_headers()
            self.wfile.write(data)

        def trusted_request(self):
            # Defends the loopback API against DNS rebinding and cross-site browser requests.
            port=self.server.server_port
            hosts={f"127.0.0.1:{port}",f"localhost:{port}",f"[::1]:{port}"}
            if docker_host:hosts.add(f"host.docker.internal:{port}")
            host=self.headers.get('Host','').lower()
            if len(self.headers.get_all('Host',[]))!=1 or host not in hosts:
                self.send_json({'error':'Untrusted Host'},403);return False
            origin=self.headers.get('Origin')
            if origin and origin!='http://'+host:
                self.send_json({'error':'Cross-origin requests are not permitted'},403);return False
            if self.headers.get('Sec-Fetch-Site') not in {None,'same-origin','none'}:
                self.send_json({'error':'Cross-site requests are not permitted'},403);return False
            return True

        def authorized(self):
            headers=self.headers.get_all('Authorization',[])
            return len(headers)==1 and hmac.compare_digest(headers[0].encode('utf-8'),("Bearer "+token).encode('utf-8'))

        def do_GET(self):
            if not self.trusted_request():return
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                healthy=thread.is_alive() and worker_error is None
                try:capacity=library.capacity()
                except Exception:capacity={'accepting_jobs':False};healthy=False
                status=200 if healthy else 503
                if not self.authorized():
                    # Unauthenticated callers only learn liveness; process and queue details need the token.
                    return self.send_json({"ok":healthy,"service":"Get Files","worker_alive":thread.is_alive()},status)
                return self.send_json({"ok":healthy,"service":"Get Files","version":"1.2-scoped","process_id":os.getpid(),"worker_alive":thread.is_alive(),"worker_error":worker_error,**capacity},status)
            if not self.authorized():
                return self.send_json({"error":"Unauthorized"},401)
            try:
                query = {key:values[-1] for key,values in parse_qs(parsed.query).items()}
                if parsed.path == '/v1/job-receipt':
                    fields = parse_qs(parsed.query, keep_blank_values=True)
                    if set(fields) != {'collection', 'idempotency_key'} or any(len(v) != 1 for v in fields.values()):
                        raise ValueError('Supply one collection and one exact operation key')
                    return self.send_json(library.job_receipt(fields['collection'][0], fields['idempotency_key'][0]))
                if parsed.path == "/v1/status":
                    return self.send_json(library.summary(query.get("collection")))
                if parsed.path == "/v1/search":
                    return self.send_json({"results":library.search(query.get("collection"),query.get("q",""),int(query.get("limit",10)),query.get("include_incomplete")=="true",[t for t in query.get("tags","").split(",") if t])})
                if parsed.path == "/v1/tags":
                    return self.send_json({"tags":library.tag_inventory(query.get("collection"))})
                parts = parsed.path.strip("/").split("/")
                if len(parts)==4 and parts[:2]==["v1","jobs"] and parts[3]=="original":
                    job = library.job(parts[2])
                    if not job or not job.get("file"):
                        return self.send_json({"error":"Original download not available"},404)
                    path = library.artifact_path(job["file"]["storage_key"])
                    self.send_response(200)
                    self.send_header("Content-Type","application/pdf" if job["file"]["extension"]==".pdf" else "application/octet-stream")
                    self.send_header("Content-Disposition",f'attachment; filename="{job["file"]["sha256"]}{job["file"]["extension"]}"')
                    self.send_header("X-Content-Type-Options","nosniff")
                    self.send_header("Content-Length",str(path.stat().st_size))
                    self.end_headers()
                    with path.open("rb") as stream:
                        while block := stream.read(65536):
                            self.wfile.write(block)
                    return
                if len(parts)==3 and parts[:2]==["v1","jobs"]:
                    result = library.job(parts[2])
                    return self.send_json(result or {"error":"Job not found"},200 if result else 404)
                if len(parts) in (3,4) and parts[:2]==["v1","documents"]:
                    doc = library.document(parts[2],query.get("collection"))
                    if not doc:
                        return self.send_json({"error":"Document not in collection"},404)
                    if len(parts)==3:
                        return self.send_json(doc)
                    if parts[3]=="chunks":
                        offset = max(0,int(query.get("offset",0)))
                        limit = min(100,max(1,int(query.get("limit",20))))
                        with library.connect() as db:
                            rows = [dict(r) for r in db.execute("SELECT * FROM chunks WHERE document_id=? ORDER BY sequence LIMIT ? OFFSET ?", (doc["id"],limit,offset))]
                        return self.send_json({"document_id":doc["id"],"status":doc["status"],"chunks":rows,"next_offset":offset+len(rows)})
                    if parts[3] in ("original","normalized"):
                        path = library.artifact_path(doc[parts[3]+"_key"])
                        self.send_response(200)
                        self.send_header("Content-Type","application/octet-stream")
                        self.send_header("Content-Disposition",f'attachment; filename="{path.name}"')
                        self.send_header("X-Content-Type-Options","nosniff")
                        self.send_header("Content-Length",str(path.stat().st_size))
                        self.end_headers()
                        with path.open("rb") as stream:
                            while block := stream.read(65536):
                                self.wfile.write(block)
                        return
                return self.send_json({"error":"Not found"},404)
            except (ValueError,TypeError) as exc:
                return self.send_json({"error":str(exc)},400)
            except Exception:
                return self.send_json({"error":"Internal error; inspect local service state"},500)

        def do_POST(self):
            if not self.trusted_request():return
            if stop.is_set():return self.send_json({'error':'Service is stopping'},503)
            if not self.authorized():
                return self.send_json({"error":"Unauthorized"},401)
            try:
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1:
                    return self.send_json({'error':'Use one Content-Length and no transfer encoding'},400)
                if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/json':
                    return self.send_json({'error':'Content-Type must be application/json'},415)
                size = int(self.headers.get("Content-Length","0"))
                if not 0<size<=2*1024*1024:
                    return self.send_json({"error":"JSON body must be between 1 byte and 2 MiB"},413)
                raw=self.rfile.read(size)
                if len(raw)!=size: raise ValueError('Truncated JSON request')
                body = json.loads(raw)
                if not isinstance(body,dict):
                    raise ValueError("Expected JSON object")
                if self.path=='/v1/admin/shutdown':
                    result=self.send_json({'stopping':True},202)
                    stop.set()
                    return result
                if self.path == "/v1/get-files":
                    return self.send_json(submit(library,body),202)
                if self.path == "/v1/ingest":
                    items = body.get("items")
                    if not isinstance(items,list) or not 1<=len(items)<=100:
                        raise ValueError("items must contain 1 to 100 objects")
                    results = []
                    for index,item in enumerate(items):
                        try:
                            if isinstance(item,dict) and item.get('path') and not ipaddress.ip_address(self.client_address[0]).is_loopback:
                                raise ValueError('Remote callers may submit URLs only; local file imports require a loopback client')
                            job = library.enqueue(body.get("collection"),item)
                            results.append({"index":index,"job_id":job["id"],"status":job["status"]})
                        except ValueError as exc:
                            results.append({"index":index,"error":str(exc)})
                    return self.send_json({"items":results},202)
                parts = self.path.strip("/").split("/")
                if len(parts)==4 and parts[:2]==['v1','jobs'] and parts[3]=='continue-ocr':
                    if body:
                        raise ValueError('Continue OCR takes an empty body and a prior job ID')
                    return self.send_json(library.continue_ocr(parts[2]),202)
                if len(parts)==4 and parts[:2]==["v1","jobs"] and parts[3]=="retry":
                    with library.connect() as db:
                        changed = db.execute("UPDATE jobs SET status='queued',attempts=0,available_at=?,updated_at=?,error_kind=NULL,error_message=NULL WHERE id=? AND status IN ('failed','needs_access','needs_resolution')", (time.time(),time.time(),parts[2])).rowcount
                    return self.send_json({"requeued":bool(changed)},202 if changed else 409)
                return self.send_json({"error":"Not found"},404)
            except (ValueError,TypeError) as exc:
                return self.send_json({"error":str(exc)},400)
            except Exception:
                return self.send_json({"error":"Internal error; inspect local service state"},500)

    with WorkerLock(library.root):
        library.recover()
        server = BoundedHTTPServer((host,port),Handler)
        server.daemon_threads = True
        spoke=None
        if spoke_config:
            from .spoke import create_spoke_server
            try:spoke=create_spoke_server(library,spoke_config,lambda:thread.is_alive() and worker_error is None and not stop.is_set())
            except Exception:server.server_close();raise
        thread = threading.Thread(target=worker,daemon=True)
        thread.start()
        if spoke:
            spoke.daemon_threads=True
            threading.Thread(target=spoke.serve_forever,daemon=True).start()
        if ready_callback: ready_callback(server)
        def shutdown_when_stopped():
            stop.wait();server.shutdown()
            if spoke:spoke.shutdown()
        threading.Thread(target=shutdown_when_stopped,daemon=True).start()
        print(f"Get Files listening on {host}:{server.server_port}",flush=True)
        try:
            server.serve_forever()
        finally:
            stop.set()
            server.server_close()
            if spoke:spoke.server_close()
            thread.join(timeout=330)
