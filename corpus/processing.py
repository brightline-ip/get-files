from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .acquisition import AcquisitionError, Downloader, challenge_page, validated_url
from .storage import Library, digest, encoded
from .publication import (JOURNAL_SCHEMA_VERSION, PublicationBusyError, PublicationError, atomic_manifest,
                          publication_key, revision_identity)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
from .vendor.context_forge_extractors import SUPPORTED_EXTENSIONS, extract, extract_pdf_page, Extraction, Section
EXTRACTOR_SOURCE = PROJECT_ROOT / 'corpus' / 'vendor' / 'context_forge_extractors.py'

NORMALIZER_VERSION = "whitespace-only-v2"
PDF_NORMALIZER_VERSION = "whitespace-only-v2-layout-windows-ocr-v3"


def extract_source(path, allow_ocr=True, ocr_checkpoint=None):
    if path.suffix.lower() != ".pdf":
        return extract(path)
    from pypdf import PdfReader
    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(""):
        raise AcquisitionError("needs_access", "PDF requires a password")
    sections, warnings, layout_pages, layout_risk_pages = [], [], [], []
    if len(reader.pages)>2000:
        raise ValueError('PDF page limit exceeded')
    for number,page in enumerate(reader.pages,1):
        text, mode = extract_pdf_page(page)
        if mode == "layout":
            layout_pages.append(number)
        elif mode == "layout_rejected":
            layout_risk_pages.append(number)
        if len(text)>2_000_000 or sum(len(s.text) for s in sections)+len(text)>8_000_000:
            raise ValueError('PDF text limit exceeded')
        sections.append(Section(f"page:{number}",text,f"Page {number}"))
        if not text.strip():
            warnings.append(f"Page {number} has no native text; blank or image content needs review/OCR")
    from .vendor.pdf_ocr import apply_pdf_ocr
    metadata={"pages":len(sections),"pdf_text_orientations":[0,90,180,270]}
    if layout_pages:
        metadata["pdf_layout_pages"]=layout_pages
        warnings.append("Layout-aware native extraction was used on pages with repeated table leaders; verify table reading order against the original.")
    if layout_risk_pages:
        metadata["pdf_layout_risk_pages"]=layout_risk_pages
        warnings.append("Layout-sensitive native pages could not safely use layout extraction without changing content; plain reading order requires review.")
    result=Extraction(sections,metadata,warnings)
    return apply_pdf_ocr(path,result,checkpoint=ocr_checkpoint) if allow_ocr else result


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):
            h.update(block)
    return h.hexdigest()


@contextmanager
def working_directory(parent):
    # Scratch directories are created beneath the caller-supplied parent so the
    # caller controls their location and access rules.
    parent = Path(parent).resolve()
    parent.mkdir(parents=True,exist_ok=True)
    directory = parent / ("work-"+uuid.uuid4().hex)
    ownership = directory.with_name(directory.name + ".publication-owned")
    directory.mkdir()
    try:
        yield directory
    finally:
        if directory.exists():
            if not ownership.exists():
                resolved = directory.resolve()
                if resolved.parent != parent:
                    raise RuntimeError("Refusing cleanup outside scratch directory")
                shutil.rmtree(resolved)
        else:
            ownership.unlink(missing_ok=True)


def clean_whitespace(text):
    text = text.replace("\r\n","\n").replace("\r","\n")
    text = re.sub(r"[ \t]+"," ",text)
    text = re.sub(r" *\n *","\n",text)
    return re.sub(r"\n{3,}","\n\n",text).strip()


def sections_to_chunks(sections, size=4000, overlap=250):
    chunks = []
    for section in sections:
        text = section["text"]
        for start in range(0,len(text),size-overlap):
            fragment = text[start:start+size]
            if not fragment.strip():
                continue
            chunks.append({"sequence":len(chunks),"locator":section["locator"],"text":fragment,
                "sha256":digest(fragment.encode()),"start_offset":start,"end_offset":start+len(fragment)})
            if start+size>=len(text):
                break
    return chunks


class WorkerLock:
    def __init__(self, root):
        self.path = Path(root) / "worker.lock"
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0,2)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("Another Get Files worker is already running")
        return self

    def __exit__(self,*args):
        self.handle.close()


class Processor:
    def __init__(self, library: Library, allowed_roots=None, downloader=None):
        self.library = library
        self.allowed_roots = [Path(p).resolve() for p in (allowed_roots if allowed_roots is not None else [PROJECT_ROOT/'intake'])]
        self.downloader = downloader or Downloader(library)

    def source_path(self, value):
        # Reject network paths and NTFS alternate streams before touching the path.
        if not isinstance(value,(str,Path)):
            raise AcquisitionError('unsafe_path','An absolute local file path is required')
        value=str(value)
        if value.startswith(('\\\\','//')) or not Path(value).is_absolute() or (os.name=='nt' and ':' in value[2:]):
            raise AcquisitionError('unsafe_path','An absolute local file path without alternate streams is required')
        source = Path(value).resolve(strict=True)
        if not source.is_file() or not any(source.is_relative_to(root) for root in self.allowed_roots):
            raise AcquisitionError("unsafe_path", "Source must be a file inside a configured import root")
        if source.name.lower() in {'service-token.txt','auth.json','credentials.json'} or any(part.lower() in {'.git','.codex','.hermes','.venv','node_modules'} for part in source.parts):
            raise AcquisitionError('unsafe_path','Credential and configuration directories cannot be imported')
        if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise AcquisitionError("unsupported_format", "Local file format is not supported")
        if source.stat().st_size > self.downloader.max_bytes:
            raise AcquisitionError("too_large", "Local source exceeds configured byte limit")
        return source

    def normalize_file(self, source, extension, item, retrieval, continue_ocr=False, completion=None):
        normalizer_version = PDF_NORMALIZER_VERSION if extension == '.pdf' else NORMALIZER_VERSION
        document_id = file_hash(source)
        if item.get("expected_sha256") and document_id != item["expected_sha256"].lower():
            raise AcquisitionError("hash_mismatch", "Source bytes do not match the expected SHA-256")
        known = self.library.known_document(document_id)
        if known and known.get("normalizer") == normalizer_version and not continue_ocr:
            try:
                self.library.publications.check_selected_available(document_id, known)
            except PublicationError as exc:
                raise AcquisitionError("storage_integrity", "Selected corpus publication is unavailable") from exc
            if completion:
                key = publication_key(document_id, revision_identity(known))
                self.library.publications.bind_completion(key, completion["job_id"], completion["collection"], completion.get("origin"))
            return known
        if extension not in SUPPORTED_EXTENSIONS:
            raise AcquisitionError("unsupported_format", f"No extractor for {extension}; source requires another adapter")
        with source.open("rb") as stream:
            head = stream.read(200000)
        if challenge_page(head):
            raise AcquisitionError("needs_access", "Saved source contains an access-challenge response")
        if extension == ".pdf" and not head.startswith(b"%PDF-"):
            raise AcquisitionError("format_mismatch", "PDF signature is absent")
        staging = self.library.root / "staging"
        staging.mkdir(exist_ok=True)
        with working_directory(staging) as temp:
            original = temp / ("original"+extension)
            shutil.copyfile(source,original)
            if file_hash(original) != document_id:
                raise AcquisitionError("source_changed", "Source changed while copying; submit it again")
            from .extraction_limits import bounded_extract
            checkpoint = None
            if extension == '.pdf':
                from .vendor.pdf_ocr import CHECKPOINT_VERSION
                checkpoint_root = self.library.root / 'ocr-progress' / CHECKPOINT_VERSION
                checkpoint_root.mkdir(parents=True, exist_ok=True)
                checkpoint = checkpoint_root / (document_id + '.sqlite3')
            extraction = bounded_extract(original, allow_ocr=extension == '.pdf', ocr_checkpoint=checkpoint)
            raw = [asdict(section) for section in extraction.sections]
            cleaned = [{**section,"text":clean_whitespace(section["text"])} for section in raw]
            for before,after in zip(raw,cleaned):
                if re.sub(r"\s","",before["text"]) != re.sub(r"\s","",after["text"]):
                    raise AcquisitionError("normalization_integrity", "Normalization changed non-whitespace content")
            empty_pages = [s["locator"] for s in cleaned if s["locator"].startswith("page:") and not s["text"].strip()]
            text_chars = sum(len(s["text"]) for s in cleaned)
            if not text_chars:
                status = "needs_ocr" if extension == ".pdf" else "needs_review"
            else:
                status = "needs_ocr" if empty_pages else "needs_review" if (extraction.metadata.get('ocr',{}).get('recognized_pages') or extraction.metadata.get('pdf_layout_pages') or extraction.metadata.get('pdf_layout_risk_pages')) else "ready"
            chunks = sections_to_chunks(cleaned)
            normalized = "\n\n".join(f"## {s['heading'] or s['locator']}\n<!-- source_locator: {s['locator']} -->\n\n{s['text']}" for s in cleaned)+"\n"
            (temp/"raw-sections.json").write_text(encoded(raw),encoding="utf-8")
            (temp/"sections.json").write_text(encoded(cleaned),encoding="utf-8")
            (temp/"normalized.md").write_text(normalized,encoding="utf-8",newline="\n")
            (temp/"chunks.jsonl").write_text("".join(encoded(c)+"\n" for c in chunks),encoding="utf-8",newline="\n")
            directory = self.library.root / "documents" / document_id[:2] / document_id / normalizer_version
            progress_hash = extraction.metadata.get('ocr', {}).get('checkpoint_sha256')
            if progress_hash:
                # Keep Windows artifact paths short; validate the full digest on reuse.
                directory = directory.parent / ('ocr2-' + progress_hash[:32])
            relative = directory.relative_to(self.library.root).as_posix()
            manifest = {"document_id":document_id,"title":item.get("title") or extraction.metadata.get("title") or document_id,
                "original_key":relative+"/original"+extension,"normalized_key":relative+"/normalized.md",
                "format":extension,"bytes":original.stat().st_size,"status":status,"normalizer":normalizer_version,
                "extractor":"pypdf selective layout/all orientations + bounded Windows OCR fallback" if extension==".pdf" else "Context Forge / "+extension,
                "extractor_code_sha256":file_hash(Path(__file__)) if extension==".pdf" else file_hash(EXTRACTOR_SOURCE),
                "metadata":extraction.metadata,"warnings":extraction.warnings,"empty_pages":empty_pages,
                "source_identity_status":"not_independently_verified","raw_characters":sum(len(s["text"]) for s in raw),
                "text_characters":text_chars,"chunks":len(chunks),"sections":len(cleaned),"non_whitespace_preserved":True,
                "normalized_sha256":file_hash(temp/"normalized.md"),"created_at":time.time(),
                "artifact_hashes":{name:file_hash(temp/name) for name in ("raw-sections.json","sections.json","chunks.jsonl")}}
            manifest["publication"] = {"journal_schema_version": JOURNAL_SCHEMA_VERSION,
                                       "revision_identity": revision_identity(manifest)}
            key = self.library.publications.reserve(manifest, temp, directory)
            if completion:
                self.library.publications.bind_completion(key, completion["job_id"], completion["collection"], completion.get("origin"))
            state = self.library.publications.state(key)
            if state in {"PUBLISHED", "STAGED", "VALIDATED"}:
                # An earlier attempt got this far; its retained candidate is used and this copy is discarded.
                return self.library.publications.resume(key)
            if state != "RESERVED":
                raise AcquisitionError("storage_integrity", "This document's earlier publication needs review; it was not replaced")
            temp.with_name(temp.name + ".publication-owned").write_text(key, encoding="ascii")
            atomic_manifest(temp/"manifest.json", manifest)
            self.library.publications.staged(key)
            self.library.publications.validated(key)
            return self.library.publications.publish(key)

    def preserve_download(self, source, extension, job, retrieval):
        sha = file_hash(source)
        expected = job["input"].get("expected_sha256")
        if expected and sha != expected.lower():
            raise AcquisitionError("hash_mismatch", "Downloaded original does not match expected SHA-256")
        extension = extension if re.fullmatch(r"\.[a-z0-9]{1,10}",extension) else ".bin"
        directory = self.library.root/"files"/sha[:2]/sha
        directory.mkdir(parents=True,exist_ok=True)
        target = directory/("original"+extension)
        try:
            if self.library.publications.reuse_published_download(job["id"], sha, extension):
                return
        except PublicationError as exc:
            raise AcquisitionError("storage_integrity", "Published download failed recovery validation") from exc
        temp = directory/("pending-"+job["id"])
        terminal = "downloaded" if job["input"].get("mode") == "download" else None
        key = self.library.publications.reserve_download(job["id"], sha, extension, retrieval,
                                                         source.stat().st_size, temp, target,
                                                         terminal_status=terminal,
                                                         terminal_receipt=retrieval if terminal else None)
        if self.library.publications.download_state(key) == "PUBLISHED":
            self.library.publications.publish_download(key)
            return
        if target.exists():
            try:
                self.library.publications.validate_existing_download(key)
            except PublicationError as exc:
                raise AcquisitionError("storage_integrity", "Saved download conflicts with its publication journal") from exc
        else:
            shutil.copyfile(source,temp)
            if file_hash(temp) != sha:
                self.library.publications.retain_download_conflict(key, "source changed while copying")
                raise AcquisitionError('source_changed','Source changed while copying')
            try:
                self.library.publications.stage_download(key)
                self.library.publications.validate_download(key)
            except PublicationError as exc:
                self.library.publications.retain_download_conflict(key, str(exc))
                raise AcquisitionError("storage_integrity", "Staged download failed publication validation") from exc
        try:
            self.library.publications.publish_download(key)
        except PublicationError as exc:
            raise AcquisitionError("storage_integrity", "Download publication failed integrity validation") from exc

    def process(self, job):
        item, job_id = job["input"], job["id"]
        scratch = self.library.root/"downloads"
        scratch.mkdir(exist_ok=True)
        try:
            with working_directory(scratch) as temporary:
                if item.get('ocr_document_id'):
                    saved = self.library.document(item['ocr_document_id'], job['collection'])
                    if not saved or saved['format'] != '.pdf':
                        raise AcquisitionError('storage_integrity', 'Saved PDF not found in this collection')
                    source = self.library.artifact_path(saved['original_key'])
                    if file_hash(source) != item['ocr_document_id']:
                        raise AcquisitionError('storage_integrity', 'Saved original failed its integrity check')
                    manifest = self.normalize_file(source, '.pdf', {'title': saved['title']}, {}, continue_ocr=True,
                                                   completion={"job_id": job_id, "collection": job["collection"]})
                    self.library.attempt(job_id, 'OCR_CONTINUE', None, 'accepted', detail='Bounded continuation from hash-verified archived original')
                    # Existing collection memberships and tags are preserved; no new source identity.
                    self.library.publications.complete_bound(manifest, job_id)
                    return self.library.job(job_id)
                elif item.get("path"):
                    source = self.source_path(item["path"])
                    if item.get("mode") in ("download","both"):
                        self.preserve_download(source,source.suffix.lower(),job,{})
                    if item.get("mode") == "download":
                        self.library.finish(job_id,"downloaded")
                        return self.library.job(job_id)
                    origin = {**item,"imported_at":time.time()}
                    manifest = self.normalize_file(source,source.suffix.lower(),item,{},
                                                   completion={"job_id": job_id, "collection": job["collection"], "origin": origin})
                    self.library.attempt(job_id,"LOCAL_IMPORT",None,"accepted",detail="Original hash and normalized package validated")
                else:
                    urls = list(dict.fromkeys([item["url"]]+item.get("alternate_urls",[])))
                    errors = []
                    for url in urls:
                        try:
                            retrieval = self.downloader.download(url,Path(temporary)/"download",item.get("expected_format"))
                            if item.get("mode") in ("download","both"):
                                self.preserve_download(Path(temporary)/"download",retrieval["extension"],job,retrieval)
                            if item.get("mode") == "download":
                                self.library.attempt(job_id,"HTTP_GET",url,"accepted",retrieval["http_status"],retrieval["final_url"])
                                self.library.finish(job_id,"downloaded")
                                return self.library.job(job_id)
                            origin = {**item,**retrieval,"attempted_url":url}
                            manifest = self.normalize_file(Path(temporary)/"download",retrieval["extension"],item,retrieval,
                                                           completion={"job_id": job_id, "collection": job["collection"], "origin": origin})
                            self.library.attempt(job_id,"HTTP_GET",url,"accepted",retrieval["http_status"],retrieval["final_url"])
                            break
                        except AcquisitionError as exc:
                            self.library.attempt(job_id,"HTTP_GET",url,exc.kind,exc.status,str(exc))
                            errors.append(exc)
                    else:
                        transient = [e for e in errors if e.transient]
                        raise max(transient,key=lambda e:e.retry_after) if transient else errors[-1]
                self.library.publications.complete_bound(manifest, job_id)
        except AcquisitionError as exc:
            delay = max(exc.retry_after, min(300, 10*2**(job["attempts"]-1)))
            status = "retry_wait" if exc.transient and job["attempts"]<4 else exc.kind if exc.kind in ("needs_access","needs_resolution") else "failed"
            self.library.finish(job_id,status,error_kind=exc.kind,message=str(exc),delay=delay)
            if item.get("path"):
                self.library.attempt(job_id,"LOCAL_IMPORT",None,exc.kind,exc.status,str(exc))
        except (OSError,sqlite3.OperationalError,PublicationBusyError) as exc:
            # Usually a file held for a moment by antivirus or a sync client.
            message="A file could not be accessed ("+type(exc).__name__+"); source content omitted from diagnostics"
            self.library.attempt(job_id,"STORE",None,"temporary_file_error",detail=type(exc).__name__)
            self.library.finish(job_id,"retry_wait" if job["attempts"]<4 else "failed",error_kind="temporary_file_error",message=message,delay=min(300,10*2**(job["attempts"]-1)))
        except Exception as exc:
            self.library.attempt(job_id,"NORMALIZE",None,"extraction_failed",detail=type(exc).__name__)
            self.library.finish(job_id,"failed",error_kind="extraction_failed",message="Processing failed ("+type(exc).__name__+"); source content omitted from diagnostics")
        return self.library.job(job_id)

    def drain(self):
        with WorkerLock(self.library.root):
            self.library.recover()
            while job := self.library.claim():
                result = self.process(job)
                print(encoded({"job_id":job["id"],"title":job["input"].get("title"),"status":result["status"],"error_kind":result["error_kind"]}),flush=True)
