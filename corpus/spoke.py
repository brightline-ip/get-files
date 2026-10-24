"""Restricted mTLS interface. It never loads or accepts the local master token."""
import hashlib
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .access import AccessDenied, GrantStore, ACTIONS, validate_request
from .get_files import submit
from .storage import collection_name, encoded
from .acquisition import close_socket


def client_policies(config):
    policies=config.get('clients')
    if not isinstance(policies,dict) or not 1<=len(policies)<=20:raise ValueError('Configure 1-20 TLS client policies')
    for fingerprint,policy in policies.items():
        if not isinstance(fingerprint,str) or len(fingerprint)!=64 or any(c not in '0123456789abcdef' for c in fingerprint):raise ValueError('Invalid TLS client fingerprint')
        if not isinstance(policy,dict) or set(policy)!={'collections','actions'}:raise ValueError('Invalid client policy')
        if not isinstance(policy['collections'],list) or not 1<=len(policy['collections'])<=20:raise ValueError('Explicit collections are required')
        for collection in policy['collections']:collection_name(collection)
        if not isinstance(policy['actions'],list) or not policy['actions'] or not set(policy['actions'])<=ACTIONS:raise ValueError('Invalid client actions')
    return policies


def execute(library,request):
    action=request['action'];collection=request['collection'];params=request['parameters']
    if action=='get_files':return submit(library,{**params,'collection':collection}),202
    if action=='ingest':
        items=[]
        for index,item in enumerate(params['items']):
            try:
                job=library.enqueue(collection,item)
                items.append({'index':index,'job_id':job['id'],'status':job['status']})
            except ValueError:items.append({'index':index,'error':'Invalid public source metadata'})
        return {'items':items},202
    if action=='search':return {'results':library.search(collection,params.get('q',''),params.get('limit',5),params.get('include_incomplete',False),params.get('tags',[]))},200
    if action=='tags':return {'tags':library.tag_inventory(collection)},200
    if action=='job' or (action=='original' and 'job_id' in params):
        job=library.job(params['job_id'])
        if not job or job['collection']!=collection:raise AccessDenied('Resource is not in the permitted collection')
        if action=='job':
            job['coverage_verified']=False
            if job.get('document_id'):
                # Current normalized revision, scoped to this job's collection.
                # A terminal job alone does not prove complete source coverage.
                doc=library.document(job['document_id'],collection)
                if doc:
                    manifest=doc['manifest']
                    job.update(extraction_warnings=manifest.get('warnings'),ocr=manifest.get('metadata',{}).get('ocr'),
                               normalization_status=doc['status'],normalized_sha256=manifest.get('normalized_sha256'))
                else:
                    job['context_error']='Normalized document metadata is unavailable in this collection'
            return job,200
        if not job.get('file'):raise AccessDenied('Original is unavailable')
        return library.artifact_path(job['file']['storage_key']),200
    doc=library.document(params['document_id'],collection)
    if not doc:raise AccessDenied('Resource is not in the permitted collection')
    if action=='original':return library.artifact_path(doc['original_key']),200
    offset=params.get('offset',0);limit=params.get('limit',2)
    with library.connect() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM chunks WHERE document_id=? ORDER BY sequence LIMIT ? OFFSET ?',(doc['id'],limit,offset))]
    return {'document_id':doc['id'],'title':doc['title'],'status':doc['status'],'tags':doc['tags'],'origins':doc['origins'],'extraction_warnings':doc['manifest'].get('warnings',[]),'ocr':doc['manifest'].get('metadata',{}).get('ocr'),'normalized_sha256':doc['manifest'].get('normalized_sha256'),'coverage_verified':False,'chunks':rows,'next_offset':offset+len(rows)},200


def create_spoke_server(library,config,healthy):
    from .server import BoundedHTTPServer
    policies=client_policies(config)
    grants=GrantStore(library.root/'access.sqlite3')
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.verify_mode=ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=config['ca_file'])
    context.load_cert_chain(config['cert_file'],config['key_file'])

    class TLSBoundedServer(BoundedHTTPServer):
        # Handshake in the bounded worker thread, not the single accept loop.
        def finish_request(self,sock,address):
            sock.settimeout(5)
            wrapped=context.wrap_socket(sock,server_side=True,do_handshake_on_connect=False)
            timer=threading.Timer(30,close_socket,args=(wrapped,));timer.daemon=True;timer.start()
            try:
                wrapped.do_handshake();super().finish_request(wrapped,address)
            finally:timer.cancel();wrapped.close()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass

        def send_json(self,value,status=200):
            raw=encoded(value).encode()
            self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(raw)

        def do_GET(self):
            if self.path=='/health':return self.send_json({'ok':healthy(),'service':'Get Files restricted spoke'},200 if healthy() else 503)
            self.send_json({'error':'Not found'},404)

        def do_POST(self):
            try:
                certificate=self.connection.getpeercert(binary_form=True)
                fingerprint=hashlib.sha256(certificate).hexdigest()
                policy=policies.get(fingerprint)
                if not policy:raise AccessDenied('Client is not authorized')
                if self.path not in {'/v1/grants','/v1/execute'}:return self.send_json({'error':'Not found'},404)
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1:raise AccessDenied('Invalid body framing')
                size=int(self.headers['Content-Length'])
                if not 0<size<=2*1024*1024:raise AccessDenied('Request exceeds size limit')
                if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/json':raise AccessDenied('Use application/json')
                raw=self.rfile.read(size)
                if len(raw)!=size:raise AccessDenied('Incomplete request')
                request=validate_request(json.loads(raw),policy)
                if self.path=='/v1/grants':return self.send_json(grants.issue(fingerprint,request))
                headers=self.headers.get_all('Authorization',[])
                if len(headers)!=1 or not headers[0].startswith('Bearer '):raise AccessDenied('One operation grant is required')
                grants.consume(headers[0][7:],fingerprint,request)
                result,status=execute(library,request)
                if isinstance(result,Path):
                    self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Disposition','attachment; filename="original.bin"');self.send_header('Content-Length',str(result.stat().st_size));self.send_header('Cache-Control','no-store');self.end_headers()
                    with result.open('rb') as stream:
                        while block:=stream.read(65536):self.wfile.write(block)
                    return
                self.send_json(result,status)
            except AccessDenied:
                self.send_json({'error':'Request or operation grant denied'},403)
            except (ValueError,TypeError,KeyError):
                self.send_json({'error':'Invalid request'},400)
            except Exception:
                self.send_json({'error':'Service unavailable; request a new grant before retrying the same idempotent operation'},503)

    return TLSBoundedServer((config.get('host','127.0.0.1'),int(config.get('port',8767))),Handler)
