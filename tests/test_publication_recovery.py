import hashlib
import json
import gc
import shutil
import unittest
import uuid
from pathlib import Path

from tests import scratch_dir
from corpus.processing import WorkerLock
from corpus.publication import PublicationError, atomic_manifest, encoded, revision_identity
from corpus.storage import Library


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class PublicationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = scratch_dir("corpus-publication")
        pass
        self.library = Library(self.root)
        self.source = b"immutable original"
        self.document_id = sha(self.source)

    def tearDown(self):
        gc.collect()  # unreferenced SQLite connections keep the database locked on Windows
        shutil.rmtree(self.root, ignore_errors=True)

    def package(self, revision="normalizer-v1", checkpoint=None):
        suffix = revision if checkpoint is None else "ocr2-" + checkpoint[:12]
        target = self.root / "documents" / self.document_id[:2] / self.document_id / suffix
        candidate = self.root / "staging" / ("candidate-" + uuid.uuid4().hex)
        candidate.mkdir(parents=True)
        original_name = "original.txt"
        (candidate / original_name).write_bytes(self.source)
        normalized = b"## document\n<!-- source_locator: document -->\n\nnormalized text\n"
        (candidate / "normalized.md").write_bytes(normalized)
        raw = [{"locator": "document", "heading": "", "text": "normalized text"}]
        sections = list(raw)
        chunk = {"sequence": 0, "locator": "document", "text": "normalized text",
                 "sha256": sha(b"normalized text"), "start_offset": 0, "end_offset": 15}
        (candidate / "raw-sections.json").write_text(encoded(raw), encoding="utf-8")
        (candidate / "sections.json").write_text(encoded(sections), encoding="utf-8")
        (candidate / "chunks.jsonl").write_text(encoded(chunk) + "\n", encoding="utf-8")
        relative = target.relative_to(self.root).as_posix()
        metadata = {"ocr": {"checkpoint_sha256": checkpoint}} if checkpoint else {}
        manifest = {"document_id": self.document_id, "title": "Fixture", "original_key": relative + "/" + original_name,
                    "normalized_key": relative + "/normalized.md", "format": ".txt", "bytes": len(self.source),
                    "status": "ready", "normalizer": revision, "metadata": metadata,
                    "normalized_sha256": sha(normalized), "sections": 1, "chunks": 1,
                    "artifact_hashes": {name: sha((candidate / name).read_bytes())
                                        for name in ("raw-sections.json", "sections.json", "chunks.jsonl")}}
        manifest["publication"] = {"journal_schema_version": 1,
                                   "revision_identity": revision_identity(manifest)}
        return candidate, target, manifest, [chunk]

    def reserve_validated(self, **kwargs):
        candidate, target, manifest, chunks = self.package(**kwargs)
        key = self.library.publications.reserve(manifest, candidate, target)
        atomic_manifest(candidate / "manifest.json", manifest)
        self.library.publications.staged(key)
        self.library.publications.validated(key)
        return key, candidate, target, manifest, chunks

    def test_normal_publish_is_idempotent(self):
        key, candidate, target, manifest, _ = self.reserve_validated()
        self.assertEqual(self.library.publications.publish(key), manifest)
        self.assertEqual(self.library.publications.publish(key), manifest)
        self.assertFalse(candidate.exists())
        self.assertTrue(target.exists())
        self.assertEqual(self.library.known_document(self.document_id)["normalizer"], "normalizer-v1")

    def test_recover_finishes_validated_candidate_before_rename(self):
        key, _, target, _, _ = self.reserve_validated()
        with WorkerLock(self.library.root):
            outcomes = self.library.recover()
        self.assertEqual(outcomes[0]["state"], "PUBLISHED")
        self.assertTrue(target.exists())
        self.assertEqual(self.library.known_document(self.document_id)["document_id"], self.document_id)

    def test_recover_repairs_rename_before_database_commit(self):
        _, candidate, target, _, _ = self.reserve_validated()
        target.parent.mkdir(parents=True)
        candidate.rename(target)
        with WorkerLock(self.library.root):
            self.library.recover()
        self.assertEqual(self.library.known_document(self.document_id)["document_id"], self.document_id)

    def test_recover_after_database_commit_is_idempotent(self):
        key, candidate, target, manifest, chunks = self.reserve_validated()
        target.parent.mkdir(parents=True)
        candidate.rename(target)
        self.library.save_document(manifest, chunks)
        with WorkerLock(self.library.root):
            self.library.recover()
        state = self.library.connect().execute("SELECT state FROM publication_journal WHERE publication_key=?", (key,)).fetchone()[0]
        self.assertEqual(state, "PUBLISHED")
        self.assertEqual(self.library.connect().execute("SELECT count(*) FROM chunks WHERE document_id=?", (self.document_id,)).fetchone()[0], 1)

    def test_conflict_retains_candidate_and_keeps_selected_revision(self):
        first, _, _, first_manifest, _ = self.reserve_validated()
        self.library.publications.publish(first)
        key, candidate, target, _, _ = self.reserve_validated(revision="normalizer-v2")
        target.mkdir(parents=True)
        (target / "manifest.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(PublicationError):
            self.library.publications.publish(key)
        row = self.library.connect().execute("SELECT state,candidate_dir FROM publication_journal WHERE publication_key=?", (key,)).fetchone()
        self.assertEqual(row[0], "CONFLICT")
        self.assertTrue(Path(row[1]).exists())
        self.assertFalse(candidate.exists())
        self.assertEqual(self.library.known_document(self.document_id)["normalizer"], first_manifest["normalizer"])

    def test_published_tamper_is_marked_without_reselecting(self):
        key, _, target, _, _ = self.reserve_validated()
        self.library.publications.publish(key)
        (target / "normalized.md").write_text("tampered", encoding="utf-8")
        with WorkerLock(self.library.root):
            outcomes = self.library.recover()
        self.assertEqual(outcomes, [])
        outcomes = self.library.publications.scrub(limit=1)
        self.assertEqual(outcomes[0]["state"], "RECONCILIATION_REQUIRED")
        self.assertIsNotNone(self.library.known_document(self.document_id))
        status = self.library.connect().execute(
            "SELECT status FROM documents WHERE id=?", (self.document_id,)).fetchone()[0]
        self.assertEqual(status, "reconciliation_required")

    def test_database_selection_and_published_state_roll_back_together(self):
        key, _, _, _, _ = self.reserve_validated()
        original = self.library._save_document
        def fail_after_selection(db, manifest, chunks):
            original(db, manifest, chunks)
            raise RuntimeError("injected before journal commit")
        self.library._save_document = fail_after_selection
        with self.assertRaises(RuntimeError):
            self.library.publications.publish(key)
        self.library._save_document = original
        self.assertIsNone(self.library.known_document(self.document_id))
        self.assertEqual(self.library.publications.state(key), "VALIDATED")
        with WorkerLock(self.library.root):
            self.library.recover()
        self.assertIsNotNone(self.library.known_document(self.document_id))
        self.assertEqual(self.library.publications.state(key), "PUBLISHED")


if __name__ == "__main__":
    unittest.main()
