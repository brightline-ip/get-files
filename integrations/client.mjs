import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
export class CorpusClient {
  constructor({baseUrl=process.env.GET_FILES_URL||'http://127.0.0.1:8766',tokenFile=process.env.GET_FILES_TOKEN_FILE||path.join(process.env.GET_FILES_DATA||path.join(root,'data'),'service-token.txt')}={}) {
    const url=new URL(baseUrl);
    if(!['http:','https:'].includes(url.protocol)||url.username||url.password||url.search||url.hash||url.pathname!=='/')throw new Error('Corpus URL must be an HTTP(S) origin without credentials or a path');
    if(url.protocol==='http:'&&!['127.0.0.1','localhost','[::1]','host.docker.internal'].includes(url.hostname))throw new Error('Nonlocal corpus connections require HTTPS');
    this.baseUrl=url.origin;this.tokenFile=tokenFile;
  }
  async request(route,body) {
    if(!route.startsWith('/v1/'))throw new Error('Invalid corpus API route');
    const token=(await fs.readFile(this.tokenFile,'utf8')).trim();
    const response=await fetch(this.baseUrl+route,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),redirect:'error',signal:AbortSignal.timeout(20000)});
    if(!response.ok){let reason='';try{const failure=await response.json();if(typeof failure?.error==='string')reason=': '+failure.error.slice(0,300);}catch{}throw new Error(`Get Files returned HTTP ${response.status}${reason}`);}
    if(!response.headers.get('content-type')?.includes('application/json')){await response.body?.cancel();throw new Error('Get Files returned a non-JSON response');}
    const chunks=[];let bytes=0;
    for await(const block of response.body){bytes+=block.length;if(bytes>8*1024*1024)throw new Error('Get Files JSON response exceeds limit');chunks.push(block);}
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  }
  search(collection,q='',limit=5,{tags=[],includeIncomplete=false}={}) {return this.request('/v1/search?'+new URLSearchParams({collection,q,limit:String(limit),tags:tags.join(','),include_incomplete:String(includeIncomplete)}));}
  tags(collection) {return this.request('/v1/tags?'+new URLSearchParams({collection}));}
  ingest(collection,items) {return this.request('/v1/ingest',{collection,items});}
  job(id) {return this.request('/v1/jobs/'+encodeURIComponent(id));}
  jobReceipt(collection,idempotencyKey) {
    if(typeof collection!=='string'||!collection||typeof idempotencyKey!=='string'||!/^[a-f0-9]{64}$/.test(idempotencyKey))throw Error('Collection and exact operation key required');
    return this.request('/v1/job-receipt?'+new URLSearchParams({collection,idempotency_key:idempotencyKey}));
  }
  document(collection,id) {return this.request('/v1/documents/'+encodeURIComponent(id)+'?'+new URLSearchParams({collection}));}
}
