import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from corpus.server import serve
from corpus.storage import Library


class JobReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='corpus-receipts-')
        self.addCleanup(self.temp.cleanup)
        self.library = Library(Path(self.temp.name) / 'data')
        self.key = 'a' * 64
        self.jobs = {}
        for collection in ('legal', 'research-b'):
            job = self.library.enqueue(collection, {'url': 'https://example.org/synthetic.txt', 'idempotency_key': self.key})
            self.library.finish(job['id'], 'failed', error_kind='synthetic', message='Test only')
            self.jobs[collection] = job['id']

    def snapshot(self):
        with self.library.connect() as db:
            return [tuple(row) for row in db.execute('SELECT * FROM jobs ORDER BY id')]

    def test_exact_lookup_is_scoped_minimal_read_only_and_survives_reopen(self):
        before = self.snapshot()
        reopened = Library(self.library.root)
        for collection, job_id in self.jobs.items():
            self.assertEqual(reopened.job_receipt(collection, self.key), {
                'found': True, 'collection': collection, 'idempotency_key': self.key, 'job_id': job_id})
        self.assertNotEqual(*self.jobs.values())
        self.assertEqual(reopened.job_receipt('other', self.key)['found'], False)
        self.assertIsNone(reopened.job_receipt('legal', 'b' * 64)['job_id'])
        self.assertEqual(self.snapshot(), before)

    def test_lookup_rejects_partial_keys_and_invalid_collections(self):
        for key in (None, '', 'a' * 63, 'A' * 64, '*', '../state'):
            with self.assertRaises(ValueError):
                self.library.job_receipt('legal', key)
        for collection in (None, '', '../legal', 'Legal'):
            with self.assertRaises(ValueError):
                self.library.job_receipt(collection, self.key)

    def test_http_lookup_requires_auth_and_one_exact_scoped_selector(self):
        token = 'synthetic-receipt-token-' * 3
        stop, ready = threading.Event(), threading.Event()
        state = {}
        class NoWork:
            def process(self, job):
                raise AssertionError('No work should be queued by receipt lookup')
        def on_ready(server):
            state['port'] = server.server_address[1]
            ready.set()
        thread = threading.Thread(target=serve, args=(self.library, token), kwargs={
            'port': 0, 'processor': NoWork(), 'stop_event': stop, 'ready_callback': on_ready}, daemon=True)
        thread.start()
        try:
            self.assertTrue(ready.wait(10))
            before = self.snapshot()
            def get(query, authorized=True):
                conn = http.client.HTTPConnection('127.0.0.1', state['port'], timeout=5)
                try:
                    conn.request('GET', '/v1/job-receipt?' + query, headers={'Authorization': 'Bearer ' + token} if authorized else {})
                    response = conn.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    conn.close()
            query = urlencode({'collection': 'legal', 'idempotency_key': self.key})
            self.assertEqual(get(query, False)[0], 401)
            status, result = get(query)
            self.assertEqual(status, 200)
            self.assertEqual(result, self.library.job_receipt('legal', self.key))
            self.assertEqual(get(urlencode({'collection': 'other', 'idempotency_key': self.key}))[1]['found'], False)
            for bad in (query + '&collection=other', query + '&extra=1', 'collection=legal', query.replace(self.key, '*')):
                self.assertEqual(get(bad)[0], 400)
            self.assertEqual(self.snapshot(), before)
        finally:
            stop.set()
            thread.join(10)
            self.assertFalse(thread.is_alive())
