import copy
import http.client
import json
import ssl
import threading
import unittest
from pathlib import Path

from tests import TEST_PARENT
from corpus.processing import working_directory
from corpus.spoke import create_spoke_server, execute
from corpus.access import AccessDenied
from corpus.storage import Library
from corpus.tls_material import create_material


class SpokeTLSTests(unittest.TestCase):
    def test_job_evidence_is_scoped_and_bound_to_current_normalized_revision(self):
        class FixtureLibrary:
            def job(self, job_id):
                return {'id':job_id,'collection':'legal','status':'needs_ocr','document_id':'b'*64}
            def document(self, document_id, collection):
                self.lookup=(document_id,collection)
                return {'status':'needs_ocr','manifest':{'warnings':['Synthetic page 2 missing'],'metadata':{'ocr':{'pending_pages':[2]}},'normalized_sha256':'c'*64}}
        library=FixtureLibrary()
        request={'action':'job','collection':'legal','parameters':{'job_id':'a'*32}}
        result,code=execute(library,request)
        self.assertEqual(code,200)
        self.assertEqual(library.lookup,('b'*64,'legal'))
        self.assertEqual(result['extraction_warnings'],['Synthetic page 2 missing'])
        self.assertEqual(result['ocr']['pending_pages'],[2])
        self.assertEqual(result['normalized_sha256'],'c'*64)
        self.assertFalse(result['coverage_verified'])
        with self.assertRaises(AccessDenied):execute(library,{**request,'collection':'research-b'})
        library.document=lambda *_:None
        result,_=execute(library,request)
        self.assertIn('context_error',result)
        self.assertNotIn('extraction_warnings',result)
        self.assertFalse(result['coverage_verified'])

    def test_tls_grants_and_operation_scoping(self):
        with working_directory(TEST_PARENT) as root:
            config=create_material(root/'identities',collections=['research']);config.update(host='127.0.0.1',port=0)
            library=Library(root/'data');server=create_spoke_server(library,config,lambda:True)
            thread=threading.Thread(target=server.serve_forever);thread.start()
            def context(client=True):
                ctx=ssl.create_default_context(cafile=config['ca_file'])
                if client:ctx.load_cert_chain(root/'identities'/'n8n-client.pem',root/'identities'/'n8n-client-key.pem')
                return ctx
            def call(route,body,grant=None,ctx=None):
                connection=http.client.HTTPSConnection('127.0.0.1',server.server_port,context=ctx or context(),timeout=5)
                headers={'Content-Type':'application/json'}
                if grant:headers['Authorization']='Bearer '+grant
                try:
                    connection.request('POST',route,json.dumps(body),headers);response=connection.getresponse()
                    return response.status,json.loads(response.read())
                finally:connection.close()
            request={'action':'get_files','collection':'research','parameters':{'url':'https://example.com/fixture.txt','mode':'download'}}
            try:
                with self.assertRaises((OSError,ssl.SSLError,http.client.HTTPException)):call('/v1/grants',request,ctx=context(False))
                with self.assertRaises((OSError,ssl.SSLError,http.client.HTTPException)):call('/v1/grants',request,ctx=ssl.create_default_context())
                self.assertEqual(call('/v1/execute',request,grant='fixture-master-token-that-is-not-a-grant')[0],403)
                status,issued=call('/v1/grants',request);self.assertEqual(status,200)
                self.assertEqual(issued['expires_in'],60)
                changed=copy.deepcopy(request);changed['parameters']['url']='https://example.com/other'
                self.assertEqual(call('/v1/execute',changed,issued['grant'])[0],403)
                status,result=call('/v1/execute',request,issued['grant']);self.assertEqual(status,202)
                job_id=result['items'][0]['job_id']
                self.assertEqual(call('/v1/execute',request,issued['grant'])[0],403)
                grant=call('/v1/grants',request)[1]['grant']
                self.assertEqual(call('/v1/execute',request,grant)[1]['items'][0]['job_id'],job_id)
                self.assertEqual(call('/v1/grants',{**request,'collection':'private'})[0],403)
                local={'action':'ingest','collection':'research','parameters':{'items':[{'path':str(root/'private.txt'),'url':'https://example.com'}]}}
                self.assertEqual(call('/v1/grants',local)[0],403)
                lookup={'action':'job','collection':'research','parameters':{'job_id':job_id}}
                grant=call('/v1/grants',lookup)[1]['grant']
                self.assertEqual(call('/v1/execute',lookup,grant)[1]['id'],job_id)
                with library.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
            finally:server.shutdown();server.server_close();thread.join(5)


if __name__=='__main__':unittest.main()
