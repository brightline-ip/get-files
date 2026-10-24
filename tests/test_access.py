import copy
import hashlib
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tests import TEST_PARENT
from corpus.access import AccessDenied, GrantStore, validate_request
from corpus.processing import working_directory, Processor
from corpus.storage import Library
from corpus.spoke import execute
from corpus.token_rotation import rotate_local_token
from corpus.processing import WorkerLock


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.scratch=working_directory(TEST_PARENT);self.root=self.scratch.__enter__()
        self.now=1000;self.store=GrantStore(self.root/'access.sqlite3',clock=lambda:self.now)
        self.policy={'actions':['search','ingest','job','read_source','original'],'collections':['research']}
        self.request={'action':'search','collection':'research','parameters':{'q':'patent'}}

    def tearDown(self):self.scratch.__exit__(None,None,None)

    def test_expiration_replay_recipient_purpose_and_collection_binding(self):
        for mutation in ['client','action','collection','parameters','expiry','audience']:
            with self.subTest(mutation=mutation):
                self.now=1000;grant=self.store.issue('client-one',self.request)['grant']
                changed=copy.deepcopy(self.request);client='client-one'
                if mutation=='client':client='client-two'
                elif mutation=='expiry':self.now=1060
                elif mutation=='audience':
                    with self.store.connect() as db:db.execute("UPDATE grants SET audience='other-service'")
                elif mutation=='parameters':changed['parameters']['q']='different'
                else:changed[mutation]='other'
                with self.assertRaises(AccessDenied):self.store.consume(grant,client,changed)
        self.now=2000;grant=self.store.issue('client-one',self.request)['grant']
        self.store.consume(grant,'client-one',self.request)
        restarted=GrantStore(self.store.path,clock=lambda:self.now)
        with self.assertRaises(AccessDenied):restarted.consume(grant,'client-one',self.request)
        with self.store.connect() as db:
            self.assertNotIn(grant,str(db.execute('SELECT * FROM grants').fetchall()))

    def test_concurrent_replay_only_one_succeeds(self):
        grant=self.store.issue('client-one',self.request)['grant']
        def consume(_):
            try:self.store.consume(grant,'client-one',self.request);return True
            except AccessDenied:return False
        with ThreadPoolExecutor(max_workers=8) as pool:self.assertEqual(sum(pool.map(consume,range(16))),1)

    def test_offline_token_rotation_preserves_value_when_worker_is_running(self):
        data=self.root/'data';data.mkdir();token=data/'service-token.txt';token.write_text('fixture-old-token')
        with WorkerLock(data):
            with self.assertRaises(RuntimeError):rotate_local_token(data)
        self.assertEqual(token.read_text(),'fixture-old-token')
        rotate_local_token(data)
        self.assertEqual(len(token.read_text()),64);self.assertNotEqual(token.read_text(),'fixture-old-token')
        self.assertFalse(list(data.glob('token-pending-*')))

    def test_grant_rate_limit_and_cleanup(self):
        for _ in range(60):self.store.issue('client-one',self.request)
        with self.assertRaises(AccessDenied):self.store.issue('client-one',self.request)
        self.now+=121;self.store.issue('client-one',self.request)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM grants').fetchone()[0],1)

    def test_policy_rejects_local_intake_unknown_fields_and_collections(self):
        for request in [
            {**self.request,'collection':'private'},
            {**self.request,'action':'admin'},
            {**self.request,'parameters':{'limit':True}},
            {**self.request,'parameters':{'q':'x','path':'anything'}},
            {'action':'ingest','collection':'research','parameters':{'items':[{'url':'https://example.com','path':'C:/private.txt'}]}},
        ]:
            with self.assertRaises(AccessDenied):validate_request(request,self.policy)
        self.assertEqual(validate_request(self.request,self.policy),self.request)

    def test_job_and_original_cannot_cross_collections(self):
        library=Library(self.root/'data')
        job=library.enqueue('private',{'url':'https://example.com'})
        for action in ['job','original']:
            with self.assertRaises(AccessDenied):execute(library,{'action':action,'collection':'research','parameters':{'job_id':job['id']}})
        self.assertEqual(execute(library,{'action':'job','collection':'private','parameters':{'job_id':job['id']}})[0]['id'],job['id'])

    def test_default_import_root_does_not_include_entire_workspace(self):
        library=Library(self.root/'data');processor=Processor(library)
        source=self.root/'private.txt';source.write_text('fixture')
        with self.assertRaises(Exception):processor.source_path(source)
        self.assertEqual(Processor(library,allowed_roots=[self.root]).source_path(source),source)

    def test_shared_bytes_do_not_leak_another_collections_title(self):
        library=Library(self.root/'data');processor=Processor(library,allowed_roots=[self.root])
        source=self.root/'public.txt';source.write_text('Public patent reference shared in two collections.')
        library.enqueue('private',{'path':str(source),'title':'Private matter fixture label'})
        first=processor.process(library.claim());self.assertEqual(first['status'],'ready')
        library.enqueue('research',{'path':str(source)})
        second=processor.process(library.claim());self.assertEqual(first['document_id'],second['document_id'])
        doc=library.document(second['document_id'],'research')
        import json
        self.assertNotIn('Private matter fixture label',json.dumps(doc))
        self.assertNotIn('Private matter fixture label',json.dumps(library.search('research','patent')))


if __name__=='__main__':unittest.main()
