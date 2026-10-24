import hashlib
import json
import shutil
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tests import scratch_dir
from corpus.processing import WorkerLock
from corpus.publication import PublicationError
from corpus.storage import Library, encoded


class DownloadPublicationTests(unittest.TestCase):
    def setUp(self):
        self.root = scratch_dir("download-publication")
        pass
        self.library = Library(self.root)
        self.job_id = uuid.uuid4().hex
        stamp = time.time()
        with self.library.connect() as db:
            db.execute("INSERT INTO jobs(id,collection,idempotency_key,input_json,status,available_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (self.job_id, "test", uuid.uuid4().hex, encoded({"mode": "download"}), "working", stamp, stamp, stamp))
        self.payload = b"download publication fixture"
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self.extension = ".txt"
        self.retrieval = {"final_url": "https://example.test/source.txt", "http_status": 200}
        directory = self.root / "files" / self.sha[:2] / self.sha
        directory.mkdir(parents=True)
        self.candidate = directory / ("pending-" + self.job_id)
        self.target = directory / "original.txt"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def reserve(self):
        return self.library.publications.reserve_download(
            self.job_id, self.sha, self.extension, self.retrieval, len(self.payload), self.candidate, self.target,
            terminal_status="downloaded", terminal_receipt=self.retrieval)

    def validated(self):
        key = self.reserve()
        self.candidate.write_bytes(self.payload)
        self.library.publications.stage_download(key)
        self.library.publications.validate_download(key)
        return key

    def artifact(self):
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM file_artifacts WHERE job_id=?", (self.job_id,)).fetchone()
            return dict(row) if row else None

    def test_publish_commits_artifact_and_state_together(self):
        key = self.validated()
        self.library.publications.publish_download(key)
        self.assertFalse(self.candidate.exists())
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual(self.artifact()["sha256"], self.sha)
        self.assertEqual(self.library.publications.download_state(key), "PUBLISHED")

    def test_recover_after_rename_before_database_commit(self):
        key = self.validated()
        self.candidate.rename(self.target)
        with WorkerLock(self.library.root):
            outcomes = self.library.recover()
        download = next(row for row in outcomes if row.get("kind") == "download")
        self.assertEqual(download["state"], "PUBLISHED")
        self.assertEqual(self.artifact()["storage_key"], self.target.relative_to(self.root).as_posix())
        self.assertEqual(self.library.job(self.job_id)["status"], "downloaded")

    def test_published_receipt_is_completed_offline_exactly_once(self):
        key = self.validated()
        self.library.publications.publish_download(key)
        with self.library.connect() as db:
            db.execute("UPDATE jobs SET status='working' WHERE id=?", (self.job_id,))
        with WorkerLock(self.library.root):
            first = self.library.recover()
        with WorkerLock(self.library.root):
            second = self.library.recover()
        self.assertEqual(self.library.job(self.job_id)["status"], "downloaded")
        self.assertTrue(any(row.get("publication_key") == key for row in first))
        self.assertFalse(any(row.get("publication_key") == key for row in second))

    def test_database_and_journal_transaction_roll_back_together(self):
        key = self.validated()
        with self.library.connect() as db:
            db.execute("CREATE TRIGGER reject_download_publish BEFORE UPDATE OF state ON download_publication_journal WHEN NEW.state='PUBLISHED' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(Exception):
            self.library.publications.publish_download(key)
        self.assertIsNone(self.artifact())
        self.assertEqual(self.library.publications.download_state(key), "VALIDATED")
        with self.library.connect() as db:
            db.execute("DROP TRIGGER reject_download_publish")
        with WorkerLock(self.library.root):
            self.library.recover()
        self.assertIsNotNone(self.artifact())
        self.assertEqual(self.library.publications.download_state(key), "PUBLISHED")

    def test_revocation_before_commit_does_not_resurrect_receipt(self):
        key = self.validated()
        original = self.library.publications._validate_download_path
        calls = 0

        def revoke_before_commit(row, path):
            nonlocal calls
            result = original(row, path)
            calls += 1
            if calls == 2:
                with self.library.connect() as db:
                    db.execute("UPDATE download_publication_journal SET state='RECONCILIATION_REQUIRED' WHERE publication_key=?", (key,))
            return result

        with patch.object(self.library.publications, "_validate_download_path", side_effect=revoke_before_commit):
            with self.assertRaises(PublicationError):
                self.library.publications.publish_download(key)
        self.assertIsNone(self.artifact())
        self.assertEqual(self.library.publications.download_state(key), "RECONCILIATION_REQUIRED")

    def test_published_corrupt_target_requires_reconciliation_on_access(self):
        key = self.validated()
        self.library.publications.publish_download(key)
        self.target.write_bytes(b"corrupt")
        with self.assertRaises(PublicationError):
            self.library.publications.check_download_available(self.job_id)
        self.assertEqual(self.library.publications.download_state(key), "RECONCILIATION_REQUIRED")
        self.assertIsNone(self.artifact())
        self.assertEqual(self.library.job(self.job_id)["status"], "reconciliation_required")

    def test_published_missing_target_revokes_receipt_in_publish_path(self):
        key = self.validated()
        self.library.publications.publish_download(key)
        self.target.unlink()
        with self.assertRaises(PublicationError):
            self.library.publications.publish_download(key)
        self.assertEqual(self.library.publications.download_state(key), "RECONCILIATION_REQUIRED")
        self.assertIsNone(self.artifact())
        self.assertEqual(self.library.job(self.job_id)["status"], "reconciliation_required")

    def test_valid_legacy_download_is_adopted_once(self):
        key = self.validated()
        self.library.publications.publish_download(key)
        with self.library.connect() as db:
            db.execute("DELETE FROM download_publication_journal WHERE job_id=?", (self.job_id,))
            db.execute("UPDATE file_artifacts SET retrieval_json=? WHERE job_id=?",
                       (encoded(self.retrieval), self.job_id))
        self.assertTrue(self.library.publications.check_download_available(self.job_id))
        row = self.library.connect().execute(
            "SELECT state,detail FROM download_publication_journal WHERE job_id=?", (self.job_id,)
        ).fetchone()
        self.assertEqual(tuple(row), ("PUBLISHED", "validated legacy adoption"))

    def test_corrupt_legacy_download_is_persistently_quarantined(self):
        key = self.validated()
        self.library.publications.publish_download(key)
        with self.library.connect() as db:
            db.execute("DELETE FROM download_publication_journal WHERE job_id=?", (self.job_id,))
        self.target.write_bytes(b"corrupt")
        first = self.library.job(self.job_id)
        second = self.library.job(self.job_id)
        self.assertEqual(first["status"], "reconciliation_required")
        self.assertEqual(second["status"], "reconciliation_required")
        self.assertIsNone(self.artifact())

    def test_existing_conflict_is_not_overwritten_or_deleted(self):
        key = self.validated()
        conflicting = b"existing different bytes"
        self.target.write_bytes(conflicting)
        with self.assertRaises(PublicationError):
            self.library.publications.publish_download(key)
        self.assertEqual(self.target.read_bytes(), conflicting)
        self.assertEqual(self.candidate.read_bytes(), self.payload)
        self.assertEqual(self.library.publications.download_state(key), "CONFLICT")
        self.assertIsNone(self.artifact())

    def test_replay_reuses_bound_retrieval_metadata(self):
        key = self.validated()
        self.library.publications.publish_download(key)
        self.assertTrue(self.library.publications.reuse_published_download(self.job_id, self.sha, self.extension))
        self.assertEqual(json.loads(self.artifact()["retrieval_json"]), self.retrieval)


if __name__ == "__main__":
    unittest.main()
