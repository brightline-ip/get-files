"""Opaque, single-use operation grants bound to an authenticated TLS client."""
import hashlib
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path

from .storage import ClosingConnection, collection_name

AUDIENCE='source-corpus-spoke-v1'
ACTIONS={'ingest','get_files','search','tags','job','read_source','original'}


class AccessDenied(ValueError):
    pass


def request_digest(request):
    return hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def validate_request(request,policy):
    if not isinstance(request,dict) or set(request)!={'action','collection','parameters'}:
        raise AccessDenied('Supply action, collection and parameters')
    action=request['action'];collection=collection_name(request['collection']);params=request['parameters']
    if not isinstance(action,str) or action not in ACTIONS or action not in policy['actions'] or collection not in policy['collections']:
        raise AccessDenied('Operation or collection is not permitted')
    if not isinstance(params,dict):raise AccessDenied('parameters must be an object')
    allowed={
        'ingest':{'items'},'get_files':{'url','urls','items','mode','format','tags'},
        'search':{'q','limit','tags','include_incomplete'},'tags':set(),'job':{'job_id'},
        'read_source':{'document_id','offset','limit'},'original':{'document_id','job_id'},
    }[action]
    if set(params)-allowed:raise AccessDenied('Unsupported operation parameters')
    if action in {'ingest','get_files'}:
        items=params.get('items',[])
        if not isinstance(items,list) or len(items)>100:raise AccessDenied('Batch exceeds 100 items')
        if any(isinstance(item,dict) and 'path' in item for item in items):raise AccessDenied('Remote local-file intake is forbidden')
        if action=='ingest' and (not items or any(not isinstance(item,dict) or not isinstance(item.get('url'),str) for item in items)):
            raise AccessDenied('Intake requires URL objects')
    if action=='search':
        if not isinstance(params.get('q',''),str) or len(params.get('q',''))>2000:raise AccessDenied('Query exceeds limit')
        if type(params.get('include_incomplete',False)) is not bool:raise AccessDenied('Invalid incomplete flag')
    if 'limit' in params and (type(params['limit']) is not int or not 1<=params['limit']<=(5 if action=='read_source' else 10)):
        raise AccessDenied('Invalid result limit')
    if 'offset' in params and (type(params['offset']) is not int or not 0<=params['offset']<=100000):raise AccessDenied('Invalid offset')
    for field,length in [('job_id',32),('document_id',64)]:
        if field in params and (not isinstance(params[field],str) or not re.fullmatch('[a-f0-9]{'+str(length)+'}',params[field])):
            raise AccessDenied('Invalid resource identifier')
    if action=='job' and 'job_id' not in params:raise AccessDenied('job_id required')
    if action=='read_source' and 'document_id' not in params:raise AccessDenied('document_id required')
    if action=='original' and len(params)!=1:raise AccessDenied('Choose a document or job original')
    request_digest(request)
    return request


class GrantStore:
    def __init__(self,path,clock=time.time):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self.clock=clock
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS grants (digest TEXT PRIMARY KEY, client TEXT NOT NULL, audience TEXT NOT NULL, request TEXT NOT NULL, issued REAL NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE INDEX IF NOT EXISTS grants_client_time ON grants(client,issued)')

    def connect(self):
        return sqlite3.connect(self.path,timeout=5,factory=ClosingConnection)

    def issue(self,client,request,ttl=60):
        if type(ttl) is not int or not 1<=ttl<=60:raise AccessDenied('Invalid grant lifetime')
        now=self.clock();grant=secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM grants WHERE expires < ?',(now-60,))
            if db.execute('SELECT count(*) FROM grants WHERE client=? AND issued>?',(client,now-60)).fetchone()[0]>=60:
                raise AccessDenied('Grant rate limit reached')
            if db.execute('SELECT count(*) FROM grants').fetchone()[0]>=2000:raise AccessDenied('Grant capacity reached')
            db.execute('INSERT INTO grants VALUES(?,?,?,?,?,?,0)',(hashlib.sha256(grant.encode()).hexdigest(),client,AUDIENCE,request_digest(request),now,now+ttl))
        return {'grant':grant,'expires_in':ttl,'audience':AUDIENCE}

    def consume(self,grant,client,request):
        if not isinstance(grant,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',grant):raise AccessDenied('Invalid or expired grant')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed=db.execute('UPDATE grants SET used=1 WHERE digest=? AND client=? AND audience=? AND request=? AND used=0 AND expires>?',
                (hashlib.sha256(grant.encode()).hexdigest(),client,AUDIENCE,request_digest(request),self.clock())).rowcount
            if changed!=1:raise AccessDenied('Invalid, expired or previously used grant')
