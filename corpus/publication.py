from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path


JOURNAL_SCHEMA_VERSION = 1


class PublicationError(RuntimeError):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_bytes(value: bytes):
    return hashlib.sha256(value).hexdigest()


def manifest_digest(manifest):
    return digest_bytes(encoded(manifest).encode("utf-8"))


def revision_identity(manifest):
    checkpoint = manifest.get("metadata", {}).get("ocr", {}).get("checkpoint_sha256") or "base"
    return f"{manifest['normalizer']}:{checkpoint}"


def publication_key(document_id, revision):
    return digest_bytes(f"{document_id}\0{revision}".encode("utf-8"))


def download_publication_key(job_id, sha256, extension, retrieval):
    retrieval_hash = digest_bytes(encoded(retrieval).encode("utf-8"))
    return digest_bytes(f"download\0{job_id}\0{sha256}\0{extension}\0{retrieval_hash}".encode("utf-8"))


def atomic_manifest(path: Path, manifest) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(encoded(manifest), encoding="utf-8")
    os.replace(temporary, path)


def _file_hash(path: Path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def validate_package(root: Path, directory: Path, manifest, expected_document_id=None, expected_revision=None,
                     selection_dir: Path | None = None, *, allow_legacy=False):
    root, directory = root.resolve(), directory.resolve()
    if not directory.is_relative_to(root) or not directory.is_dir():
        raise PublicationError("candidate directory is outside the corpus")
    document_id = manifest.get("document_id")
    if expected_document_id and document_id != expected_document_id:
        raise PublicationError("document source hash identity mismatch")
    revision = revision_identity(manifest)
    if expected_revision and revision != expected_revision:
        raise PublicationError("OCR/normalizer revision identity mismatch")
    selected = (selection_dir or directory).resolve()
    if not selected.is_relative_to(root):
        raise PublicationError("selected directory is outside the corpus")
    relative = selected.relative_to(root).as_posix()
    original_key = manifest.get("original_key")
    normalized_key = manifest.get("normalized_key")
    if not isinstance(original_key, str) or not isinstance(normalized_key, str):
        raise PublicationError("manifest artifact keys are missing")
    if Path(original_key).parent.as_posix() != relative or normalized_key != relative + "/normalized.md":
        raise PublicationError("manifest artifact keys do not select this candidate")
    original = directory / Path(original_key).name
    if _file_hash(original) != document_id:
        raise PublicationError("original source hash mismatch")
    if original.stat().st_size != manifest.get("bytes"):
        raise PublicationError("original byte count mismatch")
    normalized = directory / "normalized.md"
    if _file_hash(normalized) != manifest.get("normalized_sha256"):
        raise PublicationError("normalized hash mismatch")
    for name in ("raw-sections.json", "sections.json", "chunks.jsonl"):
        if _file_hash(directory / name) != manifest.get("artifact_hashes", {}).get(name):
            raise PublicationError(f"{name} hash mismatch")
    raw = json.loads((directory / "raw-sections.json").read_text(encoding="utf-8"))
    sections = json.loads((directory / "sections.json").read_text(encoding="utf-8"))
    chunks = [json.loads(line) for line in (directory / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    if (not isinstance(raw, list) or not isinstance(sections, list)
            or len(raw) != len(sections) or len(sections) != manifest.get("sections")):
        raise PublicationError("section structure mismatch")
    expected_normalized = "\n\n".join(
        f"## {section.get('heading') or section.get('locator')}\n"
        f"<!-- source_locator: {section.get('locator')} -->\n\n{section.get('text')}"
        for section in sections
    ) + "\n"
    if normalized.read_text(encoding="utf-8") != expected_normalized:
        raise PublicationError("normalized content does not match canonical sections")
    by_locator = {}
    for section in sections:
        locator, text = section.get("locator"), section.get("text")
        if not isinstance(locator, str) or not isinstance(text, str) or locator in by_locator:
            raise PublicationError("section locators/text are invalid or ambiguous")
        by_locator[locator] = text
    if len(chunks) != manifest.get("chunks") or [row.get("sequence") for row in chunks] != list(range(len(chunks))):
        raise PublicationError("chunk sequence/count mismatch")
    for row in chunks:
        text = row.get("text")
        if not isinstance(text, str) or digest_bytes(text.encode("utf-8")) != row.get("sha256"):
            raise PublicationError("chunk content hash mismatch")
        if not isinstance(row.get("start_offset"), int) or row.get("end_offset") != row["start_offset"] + len(text):
            raise PublicationError("chunk span mismatch")
        section_text = by_locator.get(row.get("locator"))
        if section_text is None or section_text[row["start_offset"]:row["end_offset"]] != text:
            raise PublicationError("chunk is not an exact section span")
    disk_manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest_digest(disk_manifest) != manifest_digest(manifest):
        raise PublicationError("manifest differs from journal")
    publication = manifest.get("publication", {})
    if not allow_legacy and (publication.get("journal_schema_version") != JOURNAL_SCHEMA_VERSION
            or publication.get("revision_identity") != revision):
        raise PublicationError("manifest publication identity mismatch")
    return chunks


class PublicationJournal:
    """Deterministic process-crash recovery; this is not a power-loss durability claim."""

    def __init__(self, library):
        self.library = library
        self.root = library.root.resolve()

    def reserve(self, manifest, candidate: Path, target: Path):
        revision = revision_identity(manifest)
        key = publication_key(manifest["document_id"], revision)
        candidate, target = candidate.resolve(), target.resolve()
        if not candidate.is_relative_to(self.root) or not target.is_relative_to(self.root):
            raise PublicationError("publication path escapes corpus root")
        stamp = time.time()
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM publication_journal WHERE publication_key=?", (key,)).fetchone()
            identity = (manifest["document_id"], revision, str(target))
            if row and (row["document_id"], row["revision_identity"], row["target_dir"]) != identity:
                db.execute("UPDATE publication_journal SET state='CONFLICT',detail=?,updated_at=? WHERE publication_key=?",
                           ("reservation identity mismatch", stamp, key))
                raise PublicationError("publication identity conflict")
            if row and row["state"] == "FAILED":
                db.execute("UPDATE publication_journal SET state='RESERVED',candidate_dir=?,manifest_json=?,manifest_sha256=?,detail=NULL,updated_at=? WHERE publication_key=?",
                           (str(candidate), encoded(manifest), manifest_digest(manifest), stamp, key))
                return key
            db.execute("INSERT OR IGNORE INTO publication_journal(publication_key,document_id,revision_identity,state,candidate_dir,target_dir,manifest_json,manifest_sha256,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (key, manifest["document_id"], revision, "RESERVED", str(candidate), str(target), encoded(manifest), manifest_digest(manifest), stamp, stamp))
        return key

    def _row(self, key):
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM publication_journal WHERE publication_key=?", (key,)).fetchone()
            if not row:
                raise PublicationError("publication journal entry is missing")
            return dict(row)

    def state(self, key):
        return self._row(key)["state"]

    def reserve_download(self, job_id, sha256, extension, retrieval, byte_count, candidate: Path, target: Path,
                         terminal_status=None, terminal_receipt=None):
        retrieval_json = encoded(retrieval)
        retrieval_hash = digest_bytes(retrieval_json.encode("utf-8"))
        key = download_publication_key(job_id, sha256, extension, retrieval)
        candidate, target = candidate.resolve(), target.resolve()
        if (not candidate.is_relative_to(self.root) or not target.is_relative_to(self.root)
                or candidate.parent != target.parent):
            raise PublicationError("download staging must share the target filesystem directory")
        receipt_json = encoded(terminal_receipt) if terminal_receipt is not None else None
        identity = (job_id, sha256, extension, byte_count, retrieval_hash, str(target), terminal_status, receipt_json)
        stamp = time.time()
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM download_publication_journal WHERE job_id=?", (job_id,)).fetchone()
            if row and (row["job_id"], row["sha256"], row["extension"], row["bytes"],
                        row["retrieval_digest"], row["target_path"], row["terminal_status"],
                        row["terminal_receipt_json"]) != identity:
                db.execute("UPDATE download_publication_journal SET state='CONFLICT',detail=?,updated_at=? WHERE job_id=?",
                           ("download identity mismatch", stamp, job_id))
                raise PublicationError("job is bound to a different download publication")
            if row and row["state"] == "FAILED":
                db.execute("UPDATE download_publication_journal SET publication_key=?,state='RESERVED',candidate_path=?,retrieval_json=?,terminal_status=?,terminal_receipt_json=?,detail=NULL,updated_at=? WHERE job_id=?",
                            (key, str(candidate), retrieval_json, terminal_status, receipt_json, stamp, job_id))
                return key
            db.execute("INSERT OR IGNORE INTO download_publication_journal(publication_key,job_id,sha256,extension,bytes,retrieval_json,retrieval_digest,state,candidate_path,target_path,terminal_status,terminal_receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (key, job_id, sha256, extension, byte_count, retrieval_json, retrieval_hash,
                         "RESERVED", str(candidate), str(target), terminal_status, receipt_json, stamp, stamp))
        return key

    def _download_row(self, key):
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM download_publication_journal WHERE publication_key=?", (key,)).fetchone()
            if not row:
                raise PublicationError("download publication journal entry is missing")
            return dict(row)

    def download_state(self, key):
        return self._download_row(key)["state"]

    def reuse_published_download(self, job_id, sha256, extension):
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM download_publication_journal WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return False
        row = dict(row)
        if row["state"] != "PUBLISHED":
            return False
        if row["sha256"] != sha256 or row["extension"] != extension:
            self._download_transition(row["publication_key"], "PUBLISHED", "CONFLICT", "replayed job produced different download identity")
            raise PublicationError("replayed job download identity conflicts with published artifact")
        try:
            self.publish_download(row["publication_key"])
        except Exception as exc:
            current = self.download_state(row["publication_key"])
            if current in {"PUBLISHED", "RECONCILIATION_REQUIRED"}:
                self._mark_download_unavailable(self._download_row(row["publication_key"]), str(exc))
            raise
        return True

    def _download_transition(self, key, expected, state, detail=None):
        with self.library.connect() as db:
            result = db.execute("UPDATE download_publication_journal SET state=?,detail=?,updated_at=? WHERE publication_key=? AND state=?",
                                (state, detail, time.time(), key, expected))
            if result.rowcount != 1:
                actual = db.execute("SELECT state FROM download_publication_journal WHERE publication_key=?", (key,)).fetchone()
                raise PublicationError(f"download transition {expected}->{state} refused from {actual[0] if actual else 'missing'}")

    def _mark_download_unavailable(self, row, detail):
        with self.library.connect() as db:
            current = db.execute(
                "SELECT state FROM download_publication_journal WHERE publication_key=?",
                (row["publication_key"],),
            ).fetchone()
            if not current or current[0] not in {"PUBLISHED", "RECONCILIATION_REQUIRED"}:
                raise PublicationError("download availability state changed during reconciliation")
            db.execute("DELETE FROM file_artifacts WHERE job_id=?", (row["job_id"],))
            db.execute(
                "UPDATE download_publication_journal SET state='RECONCILIATION_REQUIRED',detail=?,updated_at=? "
                "WHERE publication_key=? AND state IN ('PUBLISHED','RECONCILIATION_REQUIRED')",
                (detail[:1000], time.time(), row["publication_key"]),
            )
            db.execute(
                "UPDATE jobs SET status='reconciliation_required',error_kind='storage_integrity',"
                "error_message='Published download is unavailable',updated_at=? WHERE id=?",
                (time.time(), row["job_id"]),
            )

    def retain_download_conflict(self, key, detail):
        row = self._download_row(key)
        candidate = Path(row["candidate_path"])
        retained = self.root / "retained-downloads" / key
        if candidate.exists() and candidate != retained:
            retained.parent.mkdir(parents=True, exist_ok=True)
            if not retained.exists():
                os.replace(candidate, retained)
        with self.library.connect() as db:
            db.execute("UPDATE download_publication_journal SET state='CONFLICT',candidate_path=?,detail=?,updated_at=? WHERE publication_key=?",
                       (str(retained if retained.exists() else candidate), detail[:1000], time.time(), key))

    def _validate_download_path(self, row, path: Path):
        path = path.resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise PublicationError("download artifact is missing or outside the corpus")
        if path.stat().st_size != row["bytes"] or _file_hash(path) != row["sha256"]:
            raise PublicationError("download artifact hash or size mismatch")
        if digest_bytes(row["retrieval_json"].encode("utf-8")) != row["retrieval_digest"]:
            raise PublicationError("download retrieval metadata binding mismatch")

    def stage_download(self, key):
        row = self._download_row(key)
        self._validate_download_path(row, Path(row["candidate_path"]))
        self._download_transition(key, "RESERVED", "STAGED")

    def validate_download(self, key):
        row = self._download_row(key)
        self._validate_download_path(row, Path(row["candidate_path"]))
        self._download_transition(key, "STAGED", "VALIDATED")

    def validate_existing_download(self, key):
        row = self._download_row(key)
        try:
            self._validate_download_path(row, Path(row["target_path"]))
        except Exception as exc:
            self._download_transition(key, row["state"], "CONFLICT", str(exc)[:1000])
            raise PublicationError("existing download target conflicts with reservation") from exc
        self._download_transition(key, "RESERVED", "VALIDATED")

    def publish_download(self, key):
        row = self._download_row(key)
        if row["state"] not in {"VALIDATED", "PUBLISHED"}:
            raise PublicationError("download candidate has not been validated")
        candidate, target = Path(row["candidate_path"]), Path(row["target_path"])
        if candidate.exists() and target.exists():
            try:
                self._validate_download_path(row, target)
                self._validate_download_path(row, candidate)
            except Exception as exc:
                self._download_transition(key, row["state"], "CONFLICT", str(exc)[:1000])
                raise PublicationError("download target conflicts with retained candidate") from exc
            # Exact duplicate candidate is retained; publication never deletes recovery evidence.
        elif candidate.exists():
            self._validate_download_path(row, candidate)
            os.replace(candidate, target)
        elif not target.exists():
            if row["state"] == "PUBLISHED":
                self._mark_download_unavailable(row, "candidate and target are missing")
            else:
                self._download_transition(key, row["state"], "RECONCILIATION_REQUIRED", "candidate and target are missing")
            raise PublicationError("download publication artifact is missing")
        self._validate_download_path(row, target)
        storage_key = target.resolve().relative_to(self.root).as_posix()
        with self.library.connect() as db:
            authority = db.execute(
                "UPDATE download_publication_journal SET state='PUBLISHED',detail=NULL,updated_at=? "
                "WHERE publication_key=? AND state=?",
                (time.time(), key, row["state"]),
            )
            if authority.rowcount != 1:
                raise PublicationError("download publication authority changed before commit")
            db.execute("INSERT OR REPLACE INTO file_artifacts VALUES(?,?,?,?,?,?)",
                       (row["job_id"], row["sha256"], storage_key, row["extension"], row["bytes"], row["retrieval_json"]))
            if row.get("terminal_status"):
                # File selection, journal publication, and terminal receipt become
                # visible in the same SQLite transaction. Replays are idempotent.
                db.execute(
                    "UPDATE jobs SET status=?,document_id=NULL,error_kind=NULL,error_message=NULL,updated_at=? "
                    "WHERE id=? AND status IN ('queued','working','retry_wait','interrupted')",
                    (row["terminal_status"], time.time(), row["job_id"]),
                )
        return storage_key

    def transition(self, key, expected, state, detail=None):
        with self.library.connect() as db:
            result = db.execute("UPDATE publication_journal SET state=?,detail=?,updated_at=? WHERE publication_key=? AND state=?",
                                (state, detail, time.time(), key, expected))
            if result.rowcount != 1:
                actual = db.execute("SELECT state FROM publication_journal WHERE publication_key=?", (key,)).fetchone()
                raise PublicationError(f"publication transition {expected}->{state} refused from {actual[0] if actual else 'missing'}")

    def staged(self, key):
        self.transition(key, "RESERVED", "STAGED")

    def validated(self, key):
        row = self._row(key)
        manifest = json.loads(row["manifest_json"])
        validate_package(self.root, Path(row["candidate_dir"]), manifest, row["document_id"], row["revision_identity"],
                         Path(row["target_dir"]))
        self.transition(key, "STAGED", "VALIDATED")

    def _retain_conflict(self, row, detail):
        candidate = Path(row["candidate_dir"])
        retained = self.root / "retained-publications" / row["publication_key"]
        if candidate.exists() and candidate != retained:
            retained.parent.mkdir(parents=True, exist_ok=True)
            if not retained.exists():
                os.replace(candidate, retained)
        with self.library.connect() as db:
            db.execute("UPDATE publication_journal SET state='CONFLICT',candidate_dir=?,detail=?,updated_at=? WHERE publication_key=?",
                       (str(retained if retained.exists() else candidate), detail[:1000], time.time(), row["publication_key"]))

    def publish(self, key):
        row = self._row(key)
        if row["state"] not in {"VALIDATED", "PUBLISHED"}:
            raise PublicationError("candidate has not been validated")
        manifest = json.loads(row["manifest_json"])
        if manifest_digest(manifest) != row["manifest_sha256"]:
            self.transition(key, row["state"], "RECONCILIATION_REQUIRED", "journal manifest digest mismatch")
            raise PublicationError("journal manifest digest mismatch")
        candidate, target = Path(row["candidate_dir"]), Path(row["target_dir"])
        candidate_marker = candidate.with_name(candidate.name + ".publication-owned")
        if candidate.exists() and target.exists():
            try:
                target_manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
                validate_package(self.root, target, target_manifest, row["document_id"], row["revision_identity"])
                if manifest_digest(target_manifest) != row["manifest_sha256"]:
                    raise PublicationError("target selects different validated content")
                manifest = target_manifest
            except Exception as exc:
                self._retain_conflict(row, str(exc))
                raise PublicationError("existing target conflicts with retained candidate") from exc
        elif candidate.exists():
            validate_package(self.root, candidate, manifest, row["document_id"], row["revision_identity"], target)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(candidate, target)
        elif not target.exists():
            self.transition(key, row["state"], "RECONCILIATION_REQUIRED", "candidate and target are missing")
            raise PublicationError("publication artifacts are missing")
        chunks = validate_package(self.root, target, manifest, row["document_id"], row["revision_identity"])
        candidate_marker.unlink(missing_ok=True)
        with self.library.connect() as db:
            authority = db.execute(
                "UPDATE publication_journal SET state='PUBLISHED',detail=NULL,updated_at=? "
                "WHERE publication_key=? AND state=? AND manifest_sha256=?",
                (time.time(), key, row["state"], row["manifest_sha256"]),
            )
            if authority.rowcount != 1:
                raise PublicationError("publication authority changed before commit")
            self.library.save_document(manifest, chunks, db=db)
        return manifest

    def reconcile(self):
        outcomes = []
        with self.library.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM publication_journal WHERE state IN ('RESERVED','STAGED','VALIDATED') ORDER BY created_at,publication_key")]
        for row in rows:
            key = row["publication_key"]
            try:
                if row["state"] == "RESERVED":
                    candidate = Path(row["candidate_dir"])
                    if not (candidate / "manifest.json").is_file():
                        state = "RECONCILIATION_REQUIRED" if candidate.exists() else "FAILED"
                        self.transition(key, "RESERVED", state, "process stopped before manifest staging completed")
                        outcomes.append({"publication_key": key, "state": state})
                        continue
                    self.transition(key, "RESERVED", "STAGED")
                    row["state"] = "STAGED"
                if row["state"] == "STAGED":
                    self.validated(key)
                if row["state"] in {"STAGED", "VALIDATED"}:
                    self.publish(key)
                elif row["state"] == "PUBLISHED":
                    manifest = json.loads(row["manifest_json"])
                    validate_package(self.root, Path(row["target_dir"]), manifest, row["document_id"], row["revision_identity"])
                    if self.library.known_document(row["document_id"]) is None:
                        self.publish(key)
                outcomes.append({"publication_key": key, "state": self._row(key)["state"]})
            except Exception as exc:
                current = self._row(key)
                if row["state"] == "PUBLISHED":
                    self._mark_selected_unavailable(row)
                if current["state"] not in {"FAILED", "CONFLICT", "RECONCILIATION_REQUIRED"}:
                    self.transition(key, current["state"], "RECONCILIATION_REQUIRED", str(exc)[:1000])
                outcomes.append({"publication_key": key, "state": self._row(key)["state"], "error": str(exc)})
        outcomes.extend(self._reconcile_downloads())
        outcomes.extend(self._reconcile_completions())
        return outcomes

    def bind_completion(self, key, job_id, collection, origin):
        stamp = time.time()
        origin_json = encoded(origin) if origin is not None else None
        with self.library.connect() as db:
            old = db.execute("SELECT * FROM publication_job_completions WHERE job_id=?", (job_id,)).fetchone()
            if old and (old["publication_key"], old["collection"], old["origin_json"]) != (key, collection, origin_json):
                raise PublicationError("job completion binding conflict")
            db.execute("INSERT OR IGNORE INTO publication_job_completions VALUES(?,?,?,?,?,?,?)",
                       (key, job_id, collection, origin_json, "PENDING", stamp, stamp))

    def complete_bound(self, manifest, job_id=None):
        key = publication_key(manifest["document_id"], revision_identity(manifest))
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM publication_job_completions WHERE publication_key=? AND (? IS NULL OR job_id=?) AND state='PENDING' ORDER BY created_at LIMIT 1",
                             (key, job_id, job_id)).fetchone()
        if not row or row["state"] == "DONE":
            return False
        if self.state(key) != "PUBLISHED":
            raise PublicationError("bound job cannot finish before publication")
        origin = json.loads(row["origin_json"]) if row["origin_json"] else None
        if origin is not None:
            self.library.associate(row["collection"], manifest["document_id"], origin)
        self.library.finish(row["job_id"], manifest["status"], manifest["document_id"])
        with self.library.connect() as db:
            db.execute("UPDATE publication_job_completions SET state='DONE',updated_at=? WHERE job_id=?",
                       (time.time(), row["job_id"]))
        return True

    def _reconcile_completions(self):
        with self.library.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT c.*,p.document_id,p.manifest_json,p.state AS publication_state FROM publication_job_completions c JOIN publication_journal p USING(publication_key) WHERE c.state='PENDING' ORDER BY c.created_at")]
        outcomes = []
        for row in rows:
            if row["publication_state"] != "PUBLISHED":
                continue
            try:
                manifest = json.loads(row["manifest_json"])
                self.check_selected_available(row["document_id"], manifest)
                self.complete_bound(manifest, row["job_id"])
                outcomes.append({"publication_key": row["publication_key"], "kind": "job_completion", "state": "DONE"})
            except Exception as exc:
                outcomes.append({"publication_key": row["publication_key"], "kind": "job_completion", "state": "PENDING", "error": str(exc)})
        return outcomes

    def check_selected_available(self, document_id, manifest):
        revision = revision_identity(manifest)
        key = publication_key(document_id, revision)
        try:
            row = self._row(key)
        except PublicationError:
            try:
                row = self._adopt_legacy(document_id, manifest)
            except Exception as exc:
                with self.library.connect() as db:
                    selected = db.execute("SELECT manifest_json FROM documents WHERE id=?", (document_id,)).fetchone()
                    if selected and manifest_digest(json.loads(selected[0])) == manifest_digest(manifest):
                        db.execute("UPDATE documents SET status='reconciliation_required' WHERE id=?", (document_id,))
                raise PublicationError("legacy selected publication failed integrity validation") from exc
        if row["state"] != "PUBLISHED" or row["manifest_sha256"] != manifest_digest(manifest):
            raise PublicationError("selected publication state is not authoritative")
        try:
            allow_legacy = not bool(manifest.get("publication")) and row.get("detail") == "validated legacy adoption"
            validate_package(self.root, Path(row["target_dir"]), manifest, document_id, revision,
                             allow_legacy=allow_legacy)
        except Exception as exc:
            self._mark_selected_unavailable(row)
            self.transition(key, "PUBLISHED", "RECONCILIATION_REQUIRED", str(exc)[:1000])
            raise PublicationError("selected publication failed integrity validation") from exc
        return True

    def _adopt_legacy(self, document_id, manifest):
        """Adopt only a fully validated selected pre-journal package."""
        revision = revision_identity(manifest)
        key = publication_key(document_id, revision)
        target = (self.root / Path(manifest["normalized_key"]).parent).resolve()
        validate_package(self.root, target, manifest, document_id, revision, allow_legacy=True)
        stamp = time.time()
        with self.library.connect() as db:
            selected = db.execute("SELECT manifest_json,normalized_key,status FROM documents WHERE id=?", (document_id,)).fetchone()
            if not selected or selected[2] not in {"ready", "needs_review", "needs_ocr"}:
                raise PublicationError("legacy selected publication is unavailable")
            selected_manifest = json.loads(selected[0])
            if manifest_digest(selected_manifest) != manifest_digest(manifest) or selected[1] != manifest["normalized_key"]:
                raise PublicationError("legacy selected publication binding mismatch")
            db.execute(
                "INSERT OR IGNORE INTO publication_journal(publication_key,document_id,revision_identity,state,candidate_dir,target_dir,manifest_json,manifest_sha256,detail,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (key, document_id, revision, "PUBLISHED", str(target), str(target), encoded(manifest),
                 manifest_digest(manifest), "validated legacy adoption", stamp, stamp),
            )
        return self._row(key)

    def scrub(self, limit=25, after=""):
        with self.library.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM publication_journal WHERE state='PUBLISHED' AND publication_key>? ORDER BY publication_key LIMIT ?",
                                                    (after, min(max(int(limit), 1), 100)))]
        outcomes = []
        for row in rows:
            try:
                self.check_selected_available(row["document_id"], json.loads(row["manifest_json"]))
                outcomes.append({"publication_key": row["publication_key"], "state": "PUBLISHED"})
            except PublicationError as exc:
                outcomes.append({"publication_key": row["publication_key"], "state": "RECONCILIATION_REQUIRED", "error": str(exc)})
        return outcomes

    def _reconcile_downloads(self):
        outcomes = []
        with self.library.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT d.* FROM download_publication_journal d LEFT JOIN jobs j ON j.id=d.job_id "
                "WHERE d.state IN ('RESERVED','STAGED','VALIDATED') OR "
                "(d.state='PUBLISHED' AND d.terminal_status IS NOT NULL AND j.status IN ('queued','working','retry_wait','interrupted')) "
                "ORDER BY d.created_at,d.publication_key")]
        for row in rows:
            key = row["publication_key"]
            try:
                candidate, target = Path(row["candidate_path"]), Path(row["target_path"])
                if row["state"] == "RESERVED":
                    if target.exists() and not candidate.exists():
                        self.validate_existing_download(key)
                        row["state"] = "VALIDATED"
                    elif candidate.exists():
                        self.stage_download(key)
                        row["state"] = "STAGED"
                    else:
                        self._download_transition(key, "RESERVED", "FAILED", "process stopped before download staging began")
                        outcomes.append({"publication_key": key, "kind": "download", "state": "FAILED"})
                        continue
                if row["state"] == "STAGED":
                    self.validate_download(key)
                    row["state"] = "VALIDATED"
                if row["state"] in {"VALIDATED", "PUBLISHED"}:
                    self.publish_download(key)
                outcomes.append({"publication_key": key, "kind": "download",
                                 "state": self.download_state(key)})
            except Exception as exc:
                current = self._download_row(key)
                if current["state"] == "PUBLISHED":
                    self._mark_download_unavailable(current, str(exc))
                elif current["state"] not in {"FAILED", "CONFLICT", "RECONCILIATION_REQUIRED"}:
                    self._download_transition(key, current["state"], "RECONCILIATION_REQUIRED", str(exc)[:1000])
                outcomes.append({"publication_key": key, "kind": "download",
                                 "state": self.download_state(key), "error": str(exc)})
        return outcomes

    def check_download_available(self, job_id):
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM download_publication_journal WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            try:
                row = self._adopt_legacy_download(job_id)
            except Exception as exc:
                with self.library.connect() as db:
                    db.execute("DELETE FROM file_artifacts WHERE job_id=?", (job_id,))
                    db.execute(
                        "UPDATE jobs SET status='reconciliation_required',error_kind='storage_integrity',"
                        "error_message='Published download is unavailable',updated_at=? WHERE id=?",
                        (time.time(), job_id),
                    )
                raise PublicationError("legacy download failed integrity validation") from exc
        if row["state"] != "PUBLISHED":
            raise PublicationError("download publication is unavailable")
        row = dict(row)
        try:
            self._validate_download_path(row, Path(row["target_path"]))
        except Exception as exc:
            with self.library.connect() as db:
                db.execute("UPDATE download_publication_journal SET state='RECONCILIATION_REQUIRED',detail=?,updated_at=? WHERE job_id=?",
                           (str(exc)[:1000], time.time(), job_id))
                db.execute("DELETE FROM file_artifacts WHERE job_id=?", (job_id,))
                db.execute("UPDATE jobs SET status='reconciliation_required',error_kind='storage_integrity',error_message='Published download is unavailable',updated_at=? WHERE id=?",
                           (time.time(), job_id))
            raise PublicationError("download publication failed integrity validation") from exc
        return True

    def _adopt_legacy_download(self, job_id):
        """Validate and adopt an intact pre-journal download receipt once."""
        with self.library.connect() as db:
            artifact = db.execute(
                "SELECT a.*,j.status AS job_status FROM file_artifacts a JOIN jobs j ON j.id=a.job_id "
                "WHERE a.job_id=?", (job_id,),
            ).fetchone()
        if not artifact or artifact["job_status"] not in {"downloaded", "ready", "needs_review", "needs_ocr"}:
            raise PublicationError("legacy download receipt is unavailable")
        artifact = dict(artifact)
        retrieval = json.loads(artifact["retrieval_json"])
        canonical_retrieval = encoded(retrieval)
        target = (self.root / artifact["storage_key"]).resolve()
        row = {
            "job_id": job_id,
            "sha256": artifact["sha256"],
            "extension": artifact["extension"],
            "bytes": artifact["bytes"],
            "retrieval_json": canonical_retrieval,
            "retrieval_digest": digest_bytes(canonical_retrieval.encode("utf-8")),
        }
        self._validate_download_path(row, target)
        key = download_publication_key(job_id, artifact["sha256"], artifact["extension"], retrieval)
        candidate = target.with_name(".legacy-" + job_id)
        stamp = time.time()
        with self.library.connect() as db:
            current = db.execute("SELECT * FROM file_artifacts WHERE job_id=?", (job_id,)).fetchone()
            if not current or dict(current) != {key: artifact[key] for key in current.keys()}:
                raise PublicationError("legacy download receipt changed during adoption")
            db.execute(
                "INSERT OR IGNORE INTO download_publication_journal("
                "publication_key,job_id,sha256,extension,bytes,retrieval_json,retrieval_digest,state,"
                "candidate_path,target_path,terminal_status,terminal_receipt_json,detail,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, job_id, artifact["sha256"], artifact["extension"], artifact["bytes"],
                 canonical_retrieval, row["retrieval_digest"], "PUBLISHED", str(candidate), str(target),
                 None, None, "validated legacy adoption", stamp, stamp),
            )
        return self._download_row(key)

    def _mark_selected_unavailable(self, row):
        with self.library.connect() as db:
            selected = db.execute("SELECT manifest_json FROM documents WHERE id=?", (row["document_id"],)).fetchone()
            if not selected:
                return
            manifest = json.loads(selected[0])
            target = Path(row["target_dir"]).resolve()
            if (revision_identity(manifest) == row["revision_identity"]
                    and (not target.is_relative_to(self.root)
                         or Path(manifest["normalized_key"]).parent.as_posix()
                         == target.relative_to(self.root).as_posix())):
                db.execute("UPDATE documents SET status='reconciliation_required' WHERE id=?",
                           (row["document_id"],))
