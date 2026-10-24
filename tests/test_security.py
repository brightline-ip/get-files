"""Isolated negative cases and HTTP -> worker -> archive integration.

No production database, credentials, URLs or services are used.
"""
import http.client
import json
import sqlite3
import socket
import subprocess
import sys
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tests import TEST_PARENT
from corpus.acquisition import AcquisitionError, Downloader, retry_seconds, diagnostic_url, open_public
from corpus.extraction_worker import preflight
from corpus.extraction_limits import bounded_extract
from corpus.processing import Processor, working_directory
from corpus.server import serve
from corpus.storage import Library, digest
from tests.test_corpus import Connection, Response


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp=working_directory(TEST_PARENT)
        self.root=self.temp.__enter__()
        self.library=Library(self.root/'data')
        self.processor=Processor(self.library,allowed_roots=[self.root])

    def tearDown(self):
        self.temp.__exit__(None,None,None)

    def test_sensitive_inputs_rejected_before_persistence(self):
        invalid=[{'url':'https://user:fixture@example.com'}, {'url':'https://example.com/?token=fixture'},
                 {'url':'https://example.com','credential':'fixture'}, {'url':'https://example.com','title':{}},
                 {'url':'https://example.com','alternate_urls':[{}]}, {'url':'https://example.com','title':'x'*2049}]
        for item in invalid:
            with self.subTest(item=list(item)):
                with self.assertRaises(ValueError): self.library.enqueue('research',item)
        with self.library.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)

    @unittest.skipUnless(sys.platform == 'win32','NTFS alternate data streams are only available on Windows')
    def test_local_configuration_and_artifact_traversal_rejected(self):
        credential=self.root/'.hermes'/'auth.json'
        credential.parent.mkdir();credential.write_text('{"fixture":true}')
        with self.assertRaises(AcquisitionError):self.processor.source_path(credential)
        for value in ['\\\\untrusted.example\\share\\file.txt','//untrusted.example/share/file.txt','relative.txt',str(credential)+':stream.txt']:
            with self.assertRaises(AcquisitionError):self.processor.source_path(value)
        for key in ['../auth.json',str(credential),'C:relative.txt']:
            with self.assertRaises((ValueError,FileNotFoundError)):
                self.library.artifact_path(key)

    def test_retry_bounds_and_redirect_diagnostics(self):
        for value in ['inf','nan','-inf']:
            self.assertEqual(retry_seconds(value),0)
        self.assertLessEqual(retry_seconds('999999999999'),86400)
        value=diagnostic_url('https://example.com/file?custom_secret=fixture-secret&x=123')
        self.assertNotIn('fixture-secret',value)
        self.assertNotIn('123',value)

    def test_slow_headers_cannot_keep_connection_alive(self):
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
        done=threading.Event()
        def drip():
            conn,_=listener.accept()
            try:
                conn.recv(8192)
                for byte in b'HTTP/1.1 200 OK\r\nX-Slow: endless':
                    conn.send(bytes([byte]))
                    if done.wait(.03):break
            except OSError:pass
            finally:conn.close()
        thread=threading.Thread(target=drip);thread.start()
        started=time.monotonic()
        try:
            address=(socket.AF_INET,socket.SOCK_STREAM,6,'',listener.getsockname())
            with patch('corpus.acquisition.public_addresses',return_value=[address]):
                with self.assertRaises((OSError,http.client.HTTPException)):
                    open_public('http://example.com/fixture',timeout=.2)
            self.assertLess(time.monotonic()-started,2)
        finally:done.set();listener.close();thread.join(2)

    def test_archive_expansion_rejected_and_parser_timeout_cleans_output(self):
        archive=self.root/'fixture.docx'
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:z.writestr('word/document.xml','a'*100000)
        with self.assertRaises(ValueError):preflight(archive)
        source=self.root/'fixture.txt';source.write_text('ordinary fixture')
        with self.assertRaises(AcquisitionError) as caught:bounded_extract(source,timeout=0.00001)
        self.assertEqual(caught.exception.kind,'extraction_timeout')
        self.assertFalse((self.root/'extraction-result.json').exists())

    def test_source_copy_change_does_not_commit_artifact(self):
        source=self.root/'fixture.txt';source.write_text('before')
        job=self.library.enqueue('research',{'path':str(source),'mode':'download'})
        def changed_copy(src,dst):Path(dst).write_text('after')
        with patch('corpus.processing.shutil.copyfile',side_effect=changed_copy):
            with self.assertRaises(AcquisitionError):self.processor.preserve_download(source,'.txt',job,{})
        with self.library.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM file_artifacts').fetchone()[0],0)
        self.assertFalse(list(self.library.root.rglob('pending-*')))

    def test_concurrent_idempotency(self):
        from concurrent.futures import ThreadPoolExecutor
        item={'url':'https://example.com/fixture','idempotency_key':'same'}
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids=list(pool.map(lambda _:self.library.enqueue('research',item)['id'],range(16)))
        self.assertEqual(len(set(ids)),1)

    @unittest.skipUnless(sys.platform == 'win32','PowerShell token initialization is only available on Windows')
    def test_token_initialization_never_replaces_existing_fixture(self):
        token=self.root/'test-token.txt';token.write_text('fixture-existing-token')
        script=Path('scripts/runtime.ps1').resolve()
        command=". '"+str(script)+"'; Initialize-CorpusToken '"+str(token)+"'"
        result=subprocess.run(['powershell.exe','-NoProfile','-Command',command],capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0)
        self.assertEqual(token.read_text(),'fixture-existing-token')
        token.unlink()
        calls=[subprocess.Popen(['powershell.exe','-NoProfile','-Command',command],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(4)]
        for call in calls:
            call.communicate(timeout=15);self.assertEqual(call.returncode,0)
        self.assertEqual(len(token.read_text()),64)
        listing=subprocess.run(['icacls.exe',str(token)],capture_output=True,text=True,timeout=15).stdout
        for broad in ('Everyone','Authenticated Users','BUILTIN\\Users'):self.assertNotIn(broad,listing)
        self.assertNotIn('(I)',listing)

    def test_parser_exception_does_not_disclose_source_text(self):
        source=self.root/'source.txt';source.write_text('fixture source')
        self.library.enqueue('research',{'path':str(source)})
        with patch.object(self.processor,'normalize_file',side_effect=RuntimeError('fixture-private-value')):
            result=self.processor.process(self.library.claim())
        self.assertEqual(result['status'],'failed')
        self.assertNotIn('fixture-private-value',json.dumps(result))

    def test_queue_capacity_allows_existing_idempotent_request(self):
        first=self.library.enqueue('research',{'url':'https://example.com/one'})
        with self.library.connect() as db:
            db.executemany("INSERT INTO jobs(id,collection,idempotency_key,input_json,status,available_at,created_at,updated_at) VALUES(?,?,?,'{}','queued',0,0,0)",[(str(i),'research',str(i)) for i in range(999)])
        self.assertEqual(first['id'],self.library.enqueue('research',{'url':'https://example.com/one'})['id'])
        with self.assertRaisesRegex(ValueError,'full'):self.library.enqueue('research',{'url':'https://example.com/two'})

    def test_low_disk_blocks_new_work_without_changing_existing_jobs(self):
        from types import SimpleNamespace
        existing=self.library.enqueue('research',{'url':'https://example.com/existing'})
        with patch('corpus.storage.shutil.disk_usage',return_value=SimpleNamespace(free=100)):
            self.assertFalse(self.library.capacity()['accepting_jobs'])
            with self.assertRaisesRegex(ValueError,'Storage'):self.library.enqueue('research',{'url':'https://example.com/new'})
            self.assertEqual(self.library.job(existing['id'])['status'],'queued')

    def test_rebinding_and_cross_site_requests_refused_and_health_is_minimal(self):
        stop=threading.Event();ready=threading.Event();state={}
        token='rebind-fixture-only-'+'x'*32
        def on_ready(server):state['port']=server.server_port;ready.set()
        def request(route,headers=None,auth=True):
            conn=http.client.HTTPConnection('127.0.0.1',state['port'],timeout=5)
            hdr=dict(headers or {})
            if auth:hdr['Authorization']='Bearer '+token
            try:
                conn.putrequest('GET',route,skip_host='Host' in hdr)
                for key,value in hdr.items():conn.putheader(key,value)
                conn.endheaders();response=conn.getresponse()
                return response.status,json.loads(response.read())
            finally:conn.close()
        thread=threading.Thread(target=serve,args=(self.library,token),kwargs={'port':0,'processor':self.processor,'stop_event':stop,'ready_callback':on_ready})
        thread.start()
        try:
            self.assertTrue(ready.wait(5))
            for _ in range(60):
                if request('/health',auth=False)[0]==200:break
                time.sleep(.05)
            status,anonymous=request('/health',auth=False)
            self.assertEqual(status,200)
            self.assertEqual(set(anonymous),{'ok','service','worker_alive'})
            status,detail=request('/health')
            self.assertEqual(status,200)
            for key in ('process_id','pending_jobs','free_bytes'):self.assertIn(key,detail)
            self.assertEqual(request('/health',{'Host':'attacker.example:%d'%state['port']},auth=False)[0],403)
            self.assertEqual(request('/v1/status',{'Host':'rebind.example'})[0],403)
            self.assertEqual(request('/v1/status',{'Origin':'http://evil.example'})[0],403)
            self.assertEqual(request('/v1/status',{'Sec-Fetch-Site':'cross-site'})[0],403)
            self.assertEqual(request('/v1/status',{'Host':'localhost:%d'%state['port']})[0],200)
            conn=http.client.HTTPConnection('127.0.0.1',state['port'],timeout=5)
            try:
                conn.request('POST','/v1/get-files','{}',{'Content-Type':'application/json','Authorization':'Bearer '+token,'Host':'rebind.example'})
                response=conn.getresponse();self.assertEqual(response.status,403);response.read()
            finally:conn.close()
        finally:
            stop.set();thread.join(10)
            self.assertFalse(thread.is_alive())

    def test_http_and_mcp_assembled_system_with_worker_recovery(self):
        body=b'Semiconductor patent research. Public fixture only.'
        self.processor.downloader=Downloader(self.library,transport=lambda url:(Connection(),Response(body,headers={'Content-Type':'text/plain'})))
        stop=threading.Event();ready=threading.Event();state={}
        token='audit-fixture-only-'+'x'*32
        token_file=self.root/'fixture-token.txt';token_file.write_text(token)
        original_claim=self.library.claim;failed=threading.Event()
        def claim():
            if not failed.is_set():failed.set();raise sqlite3.OperationalError('fixture database unavailable')
            return original_claim()
        def on_ready(server):state['port']=server.server_port;ready.set()
        def request(route,method='GET',payload=None,auth=token,headers=None):
            conn=http.client.HTTPConnection('127.0.0.1',state['port'],timeout=5)
            data=json.dumps(payload) if payload is not None else None
            hdr={'Authorization':'Bearer '+auth,'Content-Type':'application/json',**(headers or {})}
            try:
                conn.request(method,route,body=data,headers=hdr);response=conn.getresponse()
                return response.status,response.read()
            finally:conn.close()
        with patch.object(self.library,'claim',side_effect=claim),patch.object(self.library,'throttle'):
            thread=threading.Thread(target=serve,args=(self.library,token),kwargs={'port':0,'processor':self.processor,'stop_event':stop,'ready_callback':on_ready})
            thread.start()
            try:
                self.assertTrue(ready.wait(5))
                self.assertEqual(request('/health')[0],503)
                self.assertEqual(request('/v1/status',auth='wrong')[0],401)
                self.assertEqual(request('/v1/status',auth='caf\u00e9')[0],401)
                self.assertEqual(request('/v1/get-files','POST',{},headers={'Content-Type':'text/plain'})[0],415)
                self.assertEqual(request('/v1/get-files','POST',{},headers={'Transfer-Encoding':'chunked'})[0],400)
                conn=http.client.HTTPConnection('127.0.0.1',state['port'],timeout=5)
                conn.putrequest('GET','/v1/status');conn.putheader('Authorization','Bearer '+token);conn.putheader('Authorization','Bearer '+token);conn.endheaders()
                response=conn.getresponse();self.assertEqual(response.status,401);response.read();conn.close()
                for _ in range(40):
                    if request('/health')[0]==200:break
                    time.sleep(.05)
                self.assertEqual(request('/health')[0],200)
                batch={'url':'https://example.com/fixture.txt','collection':'research','mode':'both','tags':['semiconductor-research']}
                status,raw=request('/v1/get-files','POST',batch);self.assertEqual(status,202)
                job_id=json.loads(raw)['items'][0]['job_id']
                self.assertEqual(json.loads(request('/v1/get-files','POST',batch)[1])['items'][0]['job_id'],job_id)
                for _ in range(150):
                    job=json.loads(request('/v1/jobs/'+job_id)[1])
                    if job['status'] not in {'queued','working','retry_wait'}:break
                    time.sleep(.05)
                self.assertEqual(job['status'],'ready',job.get('error_kind'))
                status,original=request('/v1/jobs/'+job_id+'/original')
                self.assertEqual(status,200);self.assertEqual(digest(original),digest(body))
                self.assertEqual(request('/v1/documents/'+job['document_id']+'?collection=other')[0],404)
                result=subprocess.run(['node','integrations/mcp/audit-isolated.mjs',str(state['port']),str(token_file),str(self.library.root)],capture_output=True,text=True,timeout=40)
                self.assertEqual(result.returncode,0,result.stderr[:1000])
                self.assertIn('isolated MCP passed',result.stdout)
            finally:
                stop.set();thread.join(10)
                self.assertFalse(thread.is_alive())


if __name__=='__main__':unittest.main()
