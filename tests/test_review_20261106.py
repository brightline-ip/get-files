"""Regressions for the 2026-11-06 pre-release review."""
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import TEST_PARENT
from corpus.acquisition import AcquisitionError, Downloader, open_public, public_addresses
from corpus.processing import Processor, working_directory
from corpus.publication import PublicationBusyError, PublicationError, is_transient, publication_key, replace_with_retry, revision_identity
from corpus.storage import Library
from corpus.vendor import pdf_ocr


class TransientErrorTests(unittest.TestCase):
    def test_what_counts_as_temporary(self):
        self.assertTrue(is_transient(PermissionError(32, 'in use')))
        self.assertTrue(is_transient(OSError(5, 'denied')))
        self.assertFalse(is_transient(FileNotFoundError()))
        self.assertFalse(is_transient(ValueError('hash mismatch')))

    def test_rename_retries_through_a_passing_lock(self):
        with working_directory(TEST_PARENT) as root:
            source, target = root/'a', root/'b'
            source.write_text('x')
            real, calls = __import__('os').replace, []
            def flaky(a, b):
                calls.append(1)
                if len(calls) < 3:
                    raise PermissionError(32, 'in use')
                real(a, b)
            with patch('corpus.publication.os.replace', flaky):
                replace_with_retry(source, target, wait=0)
            self.assertTrue(target.exists())
            self.assertEqual(len(calls), 3)


class LibraryRecoveryTests(unittest.TestCase):
    def build(self, root):
        intake = root/'intake'
        intake.mkdir()
        library = Library(root/'data')
        processor = Processor(library, allowed_roots=[intake])
        note = intake/'note.md'
        note.write_text('# Note\nThe quokka memorandum.\n', encoding='utf-8')
        job = library.enqueue('research', {'path': str(note), 'title': 'Note'})
        return library, processor, note, job

    def run_job(self, library, processor):
        claimed = library.claim()
        return processor.process(claimed)

    def test_a_locked_file_does_not_quarantine_or_break_search(self):
        with working_directory(TEST_PARENT) as root:
            library, processor, _, _ = self.build(root)
            done = self.run_job(library, processor)
            self.assertEqual(done['status'], 'ready')
            with patch('corpus.publication.validate_package', side_effect=PermissionError(32, 'in use')):
                self.assertEqual(library.search('research', 'quokka'), [])
                with self.assertRaises(PublicationBusyError):
                    library.document(done['document_id'], 'research')
            self.assertEqual(len(library.search('research', 'quokka')), 1)
            self.assertEqual(library.document(done['document_id'], 'research')['status'], 'ready')

    def test_real_corruption_is_quarantined_without_breaking_other_results(self):
        with working_directory(TEST_PARENT) as root:
            library, processor, note, _ = self.build(root)
            first = self.run_job(library, processor)
            second_file = note.with_name('other.md')
            second_file.write_text('# Other\nAnother quokka record.\n', encoding='utf-8')
            library.enqueue('research', {'path': str(second_file), 'title': 'Other'})
            second = self.run_job(library, processor)
            real = library.artifact_path(library.document(first['document_id'], 'research')['manifest']['normalized_key'])
            real.write_text('tampered', encoding='utf-8')
            results = library.search('research', 'quokka')
            self.assertEqual([item['document_id'] for item in results], [second['document_id']])
            with self.assertRaises(PublicationError):
                library.document(first['document_id'], 'research')

    def test_temporary_error_during_publish_retries_and_then_completes(self):
        with working_directory(TEST_PARENT) as root:
            library, processor, _, job = self.build(root)
            with patch('corpus.publication.replace_with_retry', side_effect=PermissionError(32, 'in use')):
                result = self.run_job(library, processor)
            self.assertEqual(result['status'], 'retry_wait')
            self.assertEqual(result['error_kind'], 'temporary_file_error')
            with library.connect() as db:
                db.execute("UPDATE jobs SET available_at=0 WHERE id=?", (job['id'],))
            result = self.run_job(library, processor)
            self.assertEqual(result['status'], 'ready')
            self.assertEqual(len(library.search('research', 'quokka')), 1)
            # Exactly one candidate was kept, and it was used: no hidden copies remain.
            self.assertEqual([p.name for p in (root/'data'/'staging').glob('work-*')], [])

    def test_recover_removes_orphan_working_folders(self):
        with working_directory(TEST_PARENT) as root:
            library = Library(root/'data')
            orphan = root/'data'/'downloads'/'work-abc'
            orphan.mkdir(parents=True)
            (orphan/'original.pdf').write_bytes(b'%PDF-')
            owned = root/'data'/'staging'/'work-def'
            owned.mkdir(parents=True)
            owned.with_name('work-def.publication-owned').write_text('key', encoding='ascii')
            library.recover()
            self.assertFalse(orphan.exists())
            self.assertTrue(owned.exists())


class DownloadTests(unittest.TestCase):
    def serve(self, body_delay, body=b'%PDF-1.7 synthetic', headers=b'Content-Type: application/pdf\r\n'):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        seen = []

        def run():
            conn, _ = listener.accept()
            try:
                seen.append(conn.recv(8192))
                conn.sendall(b'HTTP/1.1 200 OK\r\n' + headers + b'Content-Length: %d\r\nConnection: close\r\n\r\n' % len(body))
                time.sleep(body_delay)
                conn.sendall(body)
            except OSError:
                pass
            finally:
                conn.close()
        thread = threading.Thread(target=run)
        thread.start()
        return listener, thread, seen

    def test_body_may_outlast_the_connection_deadline(self):
        listener, thread, _ = self.serve(.6)
        try:
            address = (socket.AF_INET, socket.SOCK_STREAM, 6, '', listener.getsockname())
            with patch('corpus.acquisition.public_addresses', return_value=[address]):
                conn, response = open_public('http://example.com/fixture.pdf', timeout=.2)
                try:
                    self.assertEqual(response.read(), b'%PDF-1.7 synthetic')
                finally:
                    response.close()
                    conn.close()
        finally:
            listener.close()
            thread.join(3)

    def test_spaces_in_a_path_are_requested_safely(self):
        listener, thread, seen = self.serve(0)
        try:
            address = (socket.AF_INET, socket.SOCK_STREAM, 6, '', listener.getsockname())
            with patch('corpus.acquisition.public_addresses', return_value=[address]):
                conn, response = open_public('http://example.com/a file.pdf?q=1', timeout=2)
                response.read()
                response.close()
                conn.close()
            self.assertIn(b'GET /a%20file.pdf?q=1 HTTP/1.1', seen[0])
        finally:
            listener.close()
            thread.join(3)

    def test_runnable_files_are_saved_under_a_neutral_name(self):
        with working_directory(TEST_PARENT) as root:
            listener, thread, _ = self.serve(0, body=b'MZ not really a program', headers=b'Content-Type: application/octet-stream\r\n')
            try:
                address = (socket.AF_INET, socket.SOCK_STREAM, 6, '', listener.getsockname())
                with patch('corpus.acquisition.public_addresses', return_value=[address]):
                    result = Downloader(Library(root/'data')).download('http://example.com/setup.exe', root/'file')
                self.assertEqual(result['extension'], '.bin')
            finally:
                listener.close()
                thread.join(3)

    def test_6to4_and_teredo_are_refused(self):
        for literal in ('2002:7f00:1::', '2001:0:7f00:1::1'):
            lookup = lambda *args, **kwargs: [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', (literal, 80, 0, 0))]
            with self.assertRaises(AcquisitionError):
                public_addresses('example.com', 80, lookup=lookup)


class OcrDeadlineTests(unittest.TestCase):
    def test_a_late_batch_stops_before_the_worker_deadline(self):
        with patch.object(pdf_ocr, '_ocr_started', 1000.0):
            self.assertEqual(pdf_ocr.helper_timeout(now=lambda: 1000.0), 60)
            self.assertEqual(pdf_ocr.helper_timeout(now=lambda: 1074.0), 31.0)
            self.assertEqual(pdf_ocr.helper_timeout(now=lambda: 1200.0), 5)


if __name__ == '__main__':
    unittest.main()
