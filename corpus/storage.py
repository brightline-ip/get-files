from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
import shutil
from pathlib import Path
from .tagging import infer_tags, normalize_tags
from .acquisition import validated_url, AcquisitionError
from urllib.parse import parse_qsl, urlsplit
from .publication import PublicationError, PublicationJournal


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value: bytes):
    return hashlib.sha256(value).hexdigest()


def collection_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValueError("collection must be a lowercase name, such as legal or research")
    return value


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Library:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "corpus.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, original_key TEXT NOT NULL, normalized_key TEXT NOT NULL,
                    title TEXT NOT NULL, format TEXT NOT NULL, status TEXT NOT NULL,
                    bytes INTEGER NOT NULL, created_at REAL NOT NULL, manifest_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memberships (
                    id INTEGER PRIMARY KEY, collection TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES documents(id),
                    origin_key TEXT NOT NULL, metadata_json TEXT NOT NULL, added_at REAL NOT NULL,
                    UNIQUE(collection,document_id,origin_key)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
                    sequence INTEGER NOT NULL, locator TEXT NOT NULL, text TEXT NOT NULL, sha256 TEXT NOT NULL,
                    start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
                    UNIQUE(document_id,sequence)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunk_search USING fts5(text);
                CREATE INDEX IF NOT EXISTS memberships_collection ON memberships(collection,document_id);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, collection TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    input_json TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    document_id TEXT REFERENCES documents(id), error_kind TEXT, error_message TEXT,
                    UNIQUE(collection,idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), timestamp REAL NOT NULL,
                    method TEXT NOT NULL, url TEXT, outcome TEXT NOT NULL, http_status INTEGER, detail TEXT
                );
                CREATE TABLE IF NOT EXISTS hosts (host TEXT PRIMARY KEY, next_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS document_revisions (
                    document_id TEXT NOT NULL, manifest_hash TEXT NOT NULL, manifest_json TEXT NOT NULL,
                    created_at REAL NOT NULL, PRIMARY KEY(document_id,manifest_hash)
                );
                CREATE TABLE IF NOT EXISTS document_tags (
                    collection TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES documents(id),
                    tag TEXT NOT NULL, source TEXT NOT NULL, evidence_json TEXT NOT NULL,
                    PRIMARY KEY(collection,document_id,tag,source)
                );
                CREATE INDEX IF NOT EXISTS tags_lookup ON document_tags(collection,tag,document_id);
                CREATE TABLE IF NOT EXISTS file_artifacts (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id), sha256 TEXT NOT NULL,
                    storage_key TEXT NOT NULL, extension TEXT NOT NULL, bytes INTEGER NOT NULL,
                    retrieval_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publication_journal (
                    publication_key TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                    revision_identity TEXT NOT NULL, state TEXT NOT NULL,
                    candidate_dir TEXT NOT NULL, target_dir TEXT NOT NULL,
                    manifest_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
                    detail TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(document_id,revision_identity)
                );
                CREATE INDEX IF NOT EXISTS publication_state ON publication_journal(state,updated_at);
                CREATE TABLE IF NOT EXISTS download_publication_journal (
                    publication_key TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
                    sha256 TEXT NOT NULL, extension TEXT NOT NULL, bytes INTEGER NOT NULL,
                    retrieval_json TEXT NOT NULL, retrieval_digest TEXT NOT NULL,
                    state TEXT NOT NULL, candidate_path TEXT NOT NULL, target_path TEXT NOT NULL,
                    terminal_status TEXT, terminal_receipt_json TEXT,
                    detail TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS download_publication_state ON download_publication_journal(state,updated_at);
                CREATE TABLE IF NOT EXISTS publication_job_completions (
                    publication_key TEXT NOT NULL REFERENCES publication_journal(publication_key),
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id), collection TEXT NOT NULL,
                    origin_json TEXT, state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(download_publication_journal)")}
            if "terminal_status" not in columns:
                db.execute("ALTER TABLE download_publication_journal ADD COLUMN terminal_status TEXT")
            if "terminal_receipt_json" not in columns:
                db.execute("ALTER TABLE download_publication_journal ADD COLUMN terminal_receipt_json TEXT")
        self.publications = PublicationJournal(self)

    def connect(self):
        db = sqlite3.connect(self.database, timeout=30, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def artifact_path(self, key):
        if not isinstance(key,str) or Path(key).is_absolute() or '..' in Path(key).parts:
            raise ValueError('Invalid artifact path')
        path=(self.root/key).resolve(strict=True)
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError('Artifact is outside the library')
        return path

    def capacity(self):
        free=shutil.disk_usage(self.root).free
        with self.connect() as db:
            pending,oldest=db.execute("SELECT count(*),min(created_at) FROM jobs WHERE status IN ('queued','working','retry_wait')").fetchone()
        return {'free_bytes':free,'minimum_free_bytes':1024**3,'pending_jobs':pending,'oldest_pending_seconds':max(0,int(time.time()-oldest)) if oldest else 0,'accepting_jobs':free>=1024**3 and pending<1000}

    def continue_ocr(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r'[a-f0-9]{32}', job_id):
            raise ValueError('Invalid job ID')
        previous = self.job(job_id)
        if not previous or previous['status'] not in {'needs_ocr','needs_review'} or not previous.get('document_id'):
            raise ValueError('Continue OCR requires a terminal PDF normalization job')
        key = 'ocr-continue:' + job_id
        with self.connect() as db:
            existing = db.execute('SELECT id FROM jobs WHERE collection=? AND idempotency_key=?', (previous['collection'], key)).fetchone()
        if existing:
            return self.job(existing[0])
        document = self.document(previous['document_id'], previous['collection'])
        ocr = document.get('manifest', {}).get('metadata', {}).get('ocr', {}) if document else {}
        if not document or document['format'] != '.pdf' or not set(ocr.get('requested_pages', [])) - set(ocr.get('attempted_pages', [])):
            raise ValueError('No unattempted OCR pages; inspect blank/failed pages manually')
        return self.enqueue(previous['collection'], {'ocr_document_id': previous['document_id'],
            'idempotency_key': key, 'mode': 'corpus'}, ocr_continuation=True)

    def enqueue(self, collection, item, *, ocr_continuation=False):
        collection = collection_name(collection)
        if not isinstance(item, dict):
            raise ValueError("each item must be an object")
        allowed={'path','url','title','mode','tags','expected_format','expected_sha256','source_system','source_id','project_id','source_url','source_date','source_retrieved_at','idempotency_key','alternate_urls'}
        if ocr_continuation:
            allowed = {'ocr_document_id','idempotency_key','mode'}
            if not re.fullmatch(r'[a-f0-9]{64}', str(item.get('ocr_document_id', ''))):
                raise ValueError('Invalid saved document ID')
        if set(item)-allowed:
            raise ValueError('Unsupported intake fields; submit source metadata only')
        for field in ('path','url','title','expected_format','expected_sha256','source_system','source_id','project_id','source_url','idempotency_key'):
            if item.get(field) is not None and (not isinstance(item[field],str) or len(item[field])>8192):
                raise ValueError(field+' must be a string of at most 8192 characters')
        if item.get('title') is not None and len(item['title'])>2048:
            raise ValueError('title exceeds 2048 characters')
        normalize_tags(item.get("tags"))
        if item.get("mode", "corpus") not in ("download", "corpus", "both"):
            raise ValueError("mode must be download, corpus or both")
        if not ocr_continuation and bool(item.get("path")) == bool(item.get("url")):
            raise ValueError("supply exactly one path or url")
        if item.get("expected_sha256") and not re.fullmatch(r"[0-9a-fA-F]{64}", str(item["expected_sha256"])):
            raise ValueError("expected_sha256 must be a SHA-256 hex digest")
        if not isinstance(item.get("alternate_urls", []), list) or len(item.get("alternate_urls", [])) > 8:
            raise ValueError("alternate_urls must be a list of at most eight URLs")
        for url in [item[k] for k in ('url','source_url') if item.get(k)]+item.get('alternate_urls',[]):
            try: validated_url(url)
            except AcquisitionError: raise ValueError('Invalid source URL') from None
            if any(k.lower() in {'token','access_token','api_key','apikey','password','secret','signature','x-amz-signature','x-goog-signature','x-amz-credential'} for k,_ in parse_qsl(urlsplit(url).query)):
                raise ValueError('Credential-bearing source URLs are not accepted; use a public URL')
        # Caller-generated keys let an intended refresh become a new acquisition without replacing old versions.
        key = str(item.get("idempotency_key") or digest(encoded(item).encode()))
        if len(key) > 200:
            raise ValueError("idempotency_key is too long")
        stamp, job_id = time.time(), uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute("SELECT * FROM jobs WHERE collection=? AND idempotency_key=?", (collection, key)).fetchone()
            if old:
                if json.loads(old["input_json"]) != item:
                    raise ValueError("idempotency_key already belongs to a different request")
                job_id = old["id"]
            else:
                if shutil.disk_usage(self.root).free<1024**3:
                    raise ValueError('Storage below the 1 GiB reserve; free space before submitting more files')
                if db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','working','retry_wait')").fetchone()[0]>=1000:
                    raise ValueError('Queue is full; retry after pending work completes')
                db.execute("INSERT OR IGNORE INTO jobs(id,collection,idempotency_key,input_json,status,available_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                           (job_id, collection, key, encoded(item), "queued", stamp, stamp, stamp))
                row = db.execute("SELECT id,input_json FROM jobs WHERE collection=? AND idempotency_key=?", (collection, key)).fetchone()
                if json.loads(row["input_json"]) != item:
                    raise ValueError("idempotency_key already belongs to a different request")
                job_id = row["id"]
        return self.job(job_id)

    def recover(self):
        # Only call after acquiring the exclusive process worker lock.
        reconciliation = self.publications.reconcile()
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='queued',available_at=?,updated_at=? WHERE status='working'", (time.time(), time.time()))
        return reconciliation

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status IN ('queued','retry_wait') AND available_at<=? ORDER BY created_at LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='working',attempts=attempts+1,updated_at=? WHERE id=?", (time.time(), row["id"]))
            result = dict(row)
            result["attempts"] += 1
            result["input"] = json.loads(result.pop("input_json"))
            return result

    def finish(self, job_id, status, document_id=None, error_kind=None, message=None, delay=0):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?,document_id=?,error_kind=?,error_message=?,available_at=?,updated_at=? WHERE id=?",
                       (status, document_id, error_kind, message, time.time()+delay, time.time(), job_id))

    def attempt(self, job_id, method, url, outcome, http_status=None, detail=None):
        with self.connect() as db:
            db.execute("INSERT INTO attempts(job_id,timestamp,method,url,outcome,http_status,detail) VALUES(?,?,?,?,?,?,?)",
                       (job_id, time.time(), method, url, outcome, http_status, detail))

    def throttle(self, host, seconds=1.5):
        with self.connect() as db:
            row = db.execute("SELECT next_at FROM hosts WHERE host=?", (host,)).fetchone()
            scheduled = max(time.time(), row[0] if row else 0)
            db.execute("INSERT INTO hosts VALUES(?,?) ON CONFLICT(host) DO UPDATE SET next_at=excluded.next_at", (host, scheduled+seconds))
        time.sleep(max(0, scheduled-time.time()))

    def cool_down(self, host, seconds):
        with self.connect() as db:
            db.execute("INSERT INTO hosts VALUES(?,?) ON CONFLICT(host) DO UPDATE SET next_at=max(hosts.next_at,excluded.next_at)", (host, time.time()+seconds))

    def save_document(self, manifest, chunks, *, db=None):
        if db is not None:
            return self._save_document(db, manifest, chunks)
        with self.connect() as transaction:
            return self._save_document(transaction, manifest, chunks)

    def _save_document(self, db, manifest, chunks):
        old = db.execute("SELECT manifest_json FROM documents WHERE id=?", (manifest["document_id"],)).fetchone()
        if old:
            db.execute("INSERT OR IGNORE INTO document_revisions VALUES(?,?,?,?)", (manifest["document_id"],digest(old[0].encode()),old[0],time.time()))
            db.execute("DELETE FROM chunk_search WHERE rowid IN (SELECT id FROM chunks WHERE document_id=?)", (manifest["document_id"],))
            db.execute("DELETE FROM chunks WHERE document_id=?", (manifest["document_id"],))
            db.execute("UPDATE documents SET original_key=?,normalized_key=?,title=?,format=?,status=?,bytes=?,manifest_json=? WHERE id=?",
                       (manifest["original_key"],manifest["normalized_key"],manifest["title"],manifest["format"],manifest["status"],manifest["bytes"],encoded(manifest),manifest["document_id"]))
        result = db.execute("INSERT OR IGNORE INTO documents VALUES(?,?,?,?,?,?,?,?,?)",
            (manifest["document_id"], manifest["original_key"], manifest["normalized_key"], manifest["title"], manifest["format"], manifest["status"], manifest["bytes"], time.time(), encoded(manifest)))
        if result.rowcount or old:
            for c in chunks:
                cursor = db.execute("INSERT INTO chunks(document_id,sequence,locator,text,sha256,start_offset,end_offset) VALUES(?,?,?,?,?,?,?)",
                    (manifest["document_id"], c["sequence"], c["locator"], c["text"], c["sha256"], c["start_offset"], c["end_offset"]))
                db.execute("INSERT INTO chunk_search(rowid,text) VALUES(?,?)", (cursor.lastrowid, c["text"]))

    def associate(self, collection, doc_id, metadata):
        collection_name(collection)
        supplied = normalize_tags(metadata.get("tags"))
        origin = digest(encoded({k:metadata.get(k) for k in ("source_system","source_id","url","path")}).encode())
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO memberships(collection,document_id,origin_key,metadata_json,added_at) VALUES(?,?,?,?,?)",
                       (collection, doc_id, origin, encoded(metadata), time.time()))
            for tag in supplied:
                db.execute("INSERT OR REPLACE INTO document_tags VALUES(?,?,?,?,?)", (collection,doc_id,tag,"caller",encoded({"reason":"caller supplied"})))
        self.retag(collection, doc_id)

    def retag(self, collection, doc_id):
        doc = self.document(doc_id, collection)
        if doc is None:
            raise ValueError("Document not in collection")
        text = self.artifact_path(doc["normalized_key"]).read_text(encoding="utf-8")
        labels = infer_tags(doc["title"], text)
        with self.connect() as db:
            db.execute("DELETE FROM document_tags WHERE collection=? AND document_id=? AND source='automatic'", (collection,doc_id))
            for tag,evidence in labels.items():
                db.execute("INSERT INTO document_tags VALUES(?,?,?,?,?)", (collection,doc_id,tag,"automatic",encoded(evidence)))
        return self.document(doc_id, collection)["tags"]

    @staticmethod
    def _tags(db, collection, doc_id):
        return sorted({r[0] for r in db.execute("SELECT tag FROM document_tags WHERE collection=? AND document_id=?", (collection,doc_id))})

    def tag_inventory(self, collection):
        collection_name(collection)
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT t.tag,count(DISTINCT t.document_id) AS documents,count(DISTINCT CASE WHEN d.status='ready' THEN d.id END) AS ready FROM document_tags t JOIN documents d ON d.id=t.document_id WHERE t.collection=? GROUP BY t.tag ORDER BY t.tag", (collection,))]

    def _document_raw(self, doc_id, collection):
        """Return the selected database row without asserting publication integrity.

        Internal reconciliation code may need to inspect quarantined selections. Public
        read paths must use document(), which validates the selected package first.
        """
        collection = collection_name(collection)
        with self.connect() as db:
            row = db.execute("SELECT * FROM documents WHERE id=? AND EXISTS(SELECT 1 FROM memberships m WHERE m.document_id=documents.id AND m.collection=?)", (doc_id, collection)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["manifest"] = json.loads(result.pop("manifest_json"))
            result["origins"] = [json.loads(r[0]) for r in db.execute("SELECT metadata_json FROM memberships WHERE document_id=? AND collection=?", (doc_id, collection))]
            # Shared file hashes must not expose a title supplied in another collection.
            result['title']=next((origin['title'] for origin in result['origins'] if origin.get('title')),result['id'])
            result["tags"] = self._tags(db, collection, doc_id)
            result["tag_evidence"] = [{"tag":r[0],"source":r[1],"evidence":json.loads(r[2])} for r in db.execute("SELECT tag,source,evidence_json FROM document_tags WHERE collection=? AND document_id=? ORDER BY tag,source", (collection,doc_id))]
            return result

    def document(self, doc_id, collection):
        result = self._document_raw(doc_id, collection)
        if result is None:
            return None
        if result["status"] == "reconciliation_required":
            raise PublicationError("selected publication requires reconciliation")
        self.publications.check_selected_available(result["id"], result["manifest"])
        result["manifest"]["title"] = result["title"]
        return result

    def known_document(self, doc_id):
        with self.connect() as db:
            row = db.execute("SELECT manifest_json FROM documents WHERE id=?", (doc_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def job_receipt(self, collection, idempotency_key):
        collection = collection_name(collection)
        if not isinstance(idempotency_key, str) or not re.fullmatch(r'[a-f0-9]{64}', idempotency_key):
            raise ValueError('An exact SHA-256 operation key is required')
        with self.connect() as db:
            row = db.execute('SELECT id FROM jobs WHERE collection=? AND idempotency_key=?',
                             (collection, idempotency_key)).fetchone()
        return {'found': row is not None, 'collection': collection,
                'idempotency_key': idempotency_key, 'job_id': row['id'] if row else None}

    def job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["input"] = json.loads(result.pop("input_json"))
            result["history"] = [dict(r) for r in db.execute("SELECT * FROM attempts WHERE job_id=? ORDER BY id", (job_id,))]
            artifact = db.execute("SELECT * FROM file_artifacts WHERE job_id=?", (job_id,)).fetchone()
            if artifact:
                try:
                    self.publications.check_download_available(job_id)
                except Exception:
                    result["status"] = "reconciliation_required"
                    result["error_kind"] = "storage_integrity"
                    result["error_message"] = "Published download is unavailable"
                    return result
                result["file"] = dict(artifact)
                result["file"]["retrieval"] = json.loads(result["file"].pop("retrieval_json"))
                result["file"]["path"] = str(self.root / artifact["storage_key"])
                result["file"]["download_route"] = "/v1/jobs/"+job_id+"/original"
            return result

    def search(self, collection, query, limit=10, include_incomplete=False, tags=None):
        collection = collection_name(collection)
        inline_tags = re.findall(r"(?<!\S)tag:([a-zA-Z0-9_-]+)(?=\s|$)", query)
        query = re.sub(r"(?<!\S)tag:[a-zA-Z0-9_-]+(?=\s|$)", "", query)
        requested = normalize_tags((tags or []) + inline_tags)
        terms = re.findall(r"\w+", query, re.UNICODE)[:20]
        if not terms and not requested:
            return []
        expression = " OR ".join('"'+term+'"' for term in terms)
        with self.connect() as db:
            filters = " AND ".join("EXISTS(SELECT 1 FROM document_tags t WHERE t.document_id=d.id AND t.collection=? AND t.tag=?)" for _ in requested)
            params = [bool(include_incomplete), collection]
            where = "(? OR d.status='ready') AND EXISTS(SELECT 1 FROM memberships m WHERE m.document_id=d.id AND m.collection=?)"
            if requested:
                where += " AND " + filters
                for tag in requested:
                    params.extend([collection,tag])
            if terms:
                sql = "SELECT c.*,d.title,d.status,d.created_at,bm25(chunk_search) AS score FROM chunk_search JOIN chunks c ON c.id=chunk_search.rowid JOIN documents d ON d.id=c.document_id WHERE chunk_search MATCH ? AND " + where + " ORDER BY score,c.id LIMIT ?"
                params.insert(0,expression)
            else:
                # Subject browsing returns one record per document, including
                # scans when explicitly requested, even if they have no chunks.
                sql = "SELECT d.id AS document_id,d.title,d.status,d.created_at,0 AS score FROM documents d WHERE " + where + " ORDER BY d.title,d.id LIMIT ?"
            params.append(min(max(int(limit),1),50))
            rows = db.execute(sql, params).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                selected = db.execute("SELECT manifest_json,status FROM documents WHERE id=?", (item["document_id"],)).fetchone()
                if not selected or selected["status"] == "reconciliation_required":
                    raise PublicationError("selected publication requires reconciliation")
                self.publications.check_selected_available(item["document_id"], json.loads(selected["manifest_json"]))
                item["origins"] = [json.loads(r[0]) for r in db.execute("SELECT metadata_json FROM memberships WHERE document_id=? AND collection=?", (item["document_id"], collection))]
                item['title']=next((origin['title'] for origin in item['origins'] if origin.get('title')),item['document_id'])
                item["tags"] = self._tags(db, collection, item["document_id"])
                result.append(item)
            return result

    def summary(self, collection=None):
        if collection is not None:
            collection_name(collection)
        with self.connect() as db:
            collections = [dict(r) for r in db.execute("SELECT m.collection,d.status,count(DISTINCT d.id) AS documents FROM memberships m JOIN documents d ON d.id=m.document_id WHERE (? IS NULL OR m.collection=?) GROUP BY m.collection,d.status", (collection,collection))]
            jobs = [dict(r) for r in db.execute("SELECT collection,status,count(*) AS count FROM jobs WHERE (? IS NULL OR collection=?) GROUP BY collection,status", (collection,collection))]
            return {"collections":collections,"jobs":jobs}
