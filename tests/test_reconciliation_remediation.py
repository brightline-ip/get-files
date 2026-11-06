import os
import gc
import shutil
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tests import scratch_dir
from corpus.acquisition import AcquisitionError
from corpus.processing import Processor, WorkerLock
from corpus.publication import PublicationError
from corpus.storage import Library, encoded


class ReconciliationRemediationTests(unittest.TestCase):
    def setUp(self):
        self.root = scratch_dir("reconciliation-remediation")
        self.intake = self.root / "intake"
        self.intake.mkdir(parents=True)
        self.source = self.intake / "source.txt"
        self.source.write_text("bound candidate fixture", encoding="utf-8")
        self.library = Library(self.root / "data")
        self.processor = Processor(self.library, allowed_roots=[self.intake])

    def tearDown(self):
        gc.collect()  # unreferenced SQLite connections keep the database locked on Windows
        shutil.rmtree(self.root, ignore_errors=True)

    def test_reserved_stage_survives_exception_and_reconciles_offline(self):
        original = self.library.publications.validated
        with patch.object(self.library.publications, "validated", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.processor.normalize_file(self.source, ".txt", {}, {})
        row = self.library.connect().execute("SELECT state,candidate_dir FROM publication_journal").fetchone()
        self.assertEqual(row[0], "STAGED")
        self.assertTrue(Path(row[1]).exists())
        self.library.publications.validated = original
        with WorkerLock(self.library.root):
            self.library.recover()
        self.assertIsNotNone(self.library.known_document(next(self.library.connect().execute("SELECT id FROM documents"))[0]))

    def test_validated_stage_survives_failed_atomic_rename_and_reconciles(self):
        real_replace = os.replace

        def fail_directory_rename(source, target):
            if Path(source).is_dir():
                raise PermissionError("injected rename failure")
            return real_replace(source, target)

        with patch("corpus.publication.os.replace", side_effect=fail_directory_rename):
            with self.assertRaises(PermissionError):
                self.processor.normalize_file(self.source, ".txt", {}, {})
        row = self.library.connect().execute(
            "SELECT state,candidate_dir FROM publication_journal"
        ).fetchone()
        candidate = Path(row[1])
        marker = candidate.with_name(candidate.name + ".publication-owned")
        self.assertEqual(row[0], "VALIDATED")
        self.assertTrue(candidate.exists())
        self.assertTrue(marker.exists())
        with WorkerLock(self.library.root):
            self.library.recover()
        document_id = next(self.library.connect().execute("SELECT id FROM documents"))[0]
        self.assertIsNotNone(self.library.known_document(document_id))
        self.assertFalse(marker.exists())

    def test_cached_manifest_without_available_selected_revision_fails_closed(self):
        manifest = self.processor.normalize_file(self.source, ".txt", {}, {})
        with self.library.connect() as db:
            db.execute("UPDATE publication_journal SET state='RECONCILIATION_REQUIRED'")
        with self.assertRaises(AcquisitionError) as caught:
            self.processor.normalize_file(self.source, ".txt", {}, {})
        self.assertEqual(caught.exception.kind, "storage_integrity")
        self.assertEqual(manifest["document_id"], self.library.known_document(manifest["document_id"])["document_id"])

    def test_valid_legacy_selected_package_is_adopted_once(self):
        manifest = self.processor.normalize_file(self.source, ".txt", {}, {})
        with self.library.connect() as db:
            db.execute("DELETE FROM publication_journal")
        reused = self.processor.normalize_file(self.source, ".txt", {}, {})
        self.assertEqual(reused["document_id"], manifest["document_id"])
        row = self.library.connect().execute(
            "SELECT state,detail FROM publication_journal"
        ).fetchone()
        self.assertEqual(tuple(row), ("PUBLISHED", "validated legacy adoption"))

    def test_corrupt_legacy_selected_package_is_persistently_quarantined(self):
        manifest = self.processor.normalize_file(self.source, ".txt", {}, {})
        with self.library.connect() as db:
            db.execute("DELETE FROM publication_journal")
        self.library.artifact_path(manifest["normalized_key"]).write_text("corrupt", encoding="utf-8")
        with self.assertRaises(AcquisitionError):
            self.processor.normalize_file(self.source, ".txt", {}, {})
        status = self.library.connect().execute(
            "SELECT status FROM documents WHERE id=?", (manifest["document_id"],)
        ).fetchone()[0]
        self.assertEqual(status, "reconciliation_required")

    def test_document_and_search_quarantine_tampered_selection_repeatedly(self):
        manifest = self.processor.normalize_file(self.source, ".txt", {}, {})
        self.library.associate("test", manifest["document_id"], {"title": "fixture"})
        self.library.artifact_path(manifest["normalized_key"]).write_text("corrupt", encoding="utf-8")
        with self.assertRaises(PublicationError):
            self.library.document(manifest["document_id"], "test")
        with self.assertRaises(PublicationError):
            self.library.document(manifest["document_id"], "test")
        # A quarantined document is left out of results: it must not fail the whole collection's search.
        self.assertEqual(self.library.search("test", "bound", include_incomplete=True), [])
        self.assertEqual(self.library._document_raw(manifest["document_id"], "test")["status"], "reconciliation_required")

    def test_revocation_before_document_commit_does_not_resurrect_selection(self):
        import corpus.publication as publication

        real_validate = publication.validate_package
        revoked = False

        def revoke_before_commit(*args, **kwargs):
            nonlocal revoked
            result = real_validate(*args, **kwargs)
            path = Path(args[1]).resolve()
            target = (Path(args[0]) / Path(args[2]["normalized_key"]).parent).resolve()
            if path == target and not revoked:
                revoked = True
                with self.library.connect() as db:
                    db.execute("UPDATE publication_journal SET state='RECONCILIATION_REQUIRED'")
            return result

        with patch("corpus.publication.validate_package", side_effect=revoke_before_commit):
            with self.assertRaises(PublicationError):
                self.processor.normalize_file(self.source, ".txt", {}, {})
        self.assertIsNone(self.library.known_document(next(self.library.connect().execute("SELECT document_id FROM publication_journal"))[0]))
        self.assertEqual(self.library.connect().execute("SELECT state FROM publication_journal").fetchone()[0], "RECONCILIATION_REQUIRED")

    def test_missing_published_download_revokes_receipt_repeatedly(self):
        stamp = time.time()
        job = self.library.enqueue("test", {"path": str(self.source), "mode": "download"})
        claimed = self.library.claim()
        self.processor.preserve_download(self.source, ".txt", claimed, {})
        self.library.finish(job["id"], "downloaded")
        receipt = self.library.job(job["id"])
        Path(receipt["file"]["path"]).unlink()
        first = self.library.job(job["id"])
        second = self.library.job(job["id"])
        self.assertEqual(first["status"], "reconciliation_required")
        self.assertEqual(second["status"], "reconciliation_required")
        self.assertNotIn("file", first)

    def test_published_download_idempotent_enqueue_releases_write_lock_before_validation(self):
        item = {"path": str(self.source), "mode": "download", "idempotency_key": "published-download"}
        submitted = self.library.enqueue("test", item)
        receipt = self.processor.process(self.library.claim())
        self.assertEqual(receipt["status"], "downloaded")

        started = time.monotonic()
        repeated = self.library.enqueue("test", item)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(repeated["id"], submitted["id"])
        self.assertEqual(repeated["status"], "downloaded")

        Path(repeated["file"]["path"]).write_bytes(b"tampered")
        started = time.monotonic()
        quarantined = self.library.enqueue("test", item)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(quarantined["status"], "reconciliation_required")
        self.assertNotIn("file", quarantined)

    def test_recover_finishes_bound_membership_and_job_without_source(self):
        job = self.library.enqueue("test", {"path": str(self.source), "mode": "corpus"})
        claimed = self.library.claim()
        origin = {"path": str(self.source), "mode": "corpus", "imported_at": time.time()}
        manifest = self.processor.normalize_file(
            self.source, ".txt", {"path": str(self.source), "mode": "corpus"}, {},
            completion={"job_id": job["id"], "collection": "test", "origin": origin})
        self.source.unlink()  # Recovery must use the published package, not reacquire input.
        with WorkerLock(self.library.root):
            self.library.recover()
        receipt = self.library.job(job["id"])
        self.assertEqual(receipt["status"], manifest["status"])
        self.assertEqual(receipt["document_id"], manifest["document_id"])
        self.assertIsNotNone(self.library.document(manifest["document_id"], "test"))
        with WorkerLock(self.library.root):
            self.library.recover()
        count = self.library.connect().execute(
            "SELECT count(*) FROM memberships WHERE collection='test' AND document_id=?",
            (manifest["document_id"],)).fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
