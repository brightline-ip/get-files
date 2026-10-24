import fs from 'node:fs/promises';
import {createReadStream,constants} from 'node:fs';
import {createHash} from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {CorpusClient} from './client.mjs';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const active=new Set(['queued','working','retry_wait']);
async function hashFile(file) {
  const hash=createHash('sha256');
  for await(const chunk of createReadStream(file))hash.update(chunk);
  return hash.digest('hex');
}
const powershellExe=path.join(process.env.SystemRoot||process.env.windir||'C:\\Windows','System32','WindowsPowerShell','v1.0','powershell.exe');
export class GetFilesClient extends CorpusClient {
  constructor({dataRoot=process.env.GET_FILES_DATA||path.join(root,'data'),exportRoot=process.env.GET_FILES_EXPORT_ROOT||path.join(os.homedir(),'Documents','Get Files'),autoStart=process.env.GET_FILES_AUTOSTART!=='false',...options}={}) {
    super(options);this.dataRoot=path.resolve(dataRoot);this.exportRoot=path.resolve(exportRoot);this.autoStart=autoStart;
  }
  async ensureService() {
    try {
      const response=await fetch(this.baseUrl+'/health',{signal:AbortSignal.timeout(2000),redirect:'error'});
      const health=await response.json();
      if(!response.ok||!health.ok||health.service!=='Get Files'||!health.worker_alive)throw new Error('Port belongs to an unhealthy or different service');
      return;
    } catch(error) {
      if(!['ECONNREFUSED'].includes(error.cause?.code))throw error;
      if(!this.autoStart||this.baseUrl!=='http://127.0.0.1:8766')throw error;
      const result=spawnSync(powershellExe,['-NoProfile','-File',path.join(root,'start.ps1'),'-Background'],{windowsHide:true,stdio:'ignore',timeout:45000});
      if(result.status!==0)throw new Error('Could not start Get Files; inspect its local service logs');
    }
  }
  async request(route,body) {await this.ensureService();return super.request(route,body);}
  async getFiles({wait_seconds=2,output_dir,...body}) {
    if(!Number.isFinite(wait_seconds)||wait_seconds<0)throw new Error('wait_seconds must be a nonnegative number');
    const batch=await this.request('/v1/get-files',body);
    const ids=batch.items.filter(x=>x.job_id).map(x=>x.job_id);
    return {...batch,...await this.getJobs(ids,{wait_seconds,output_dir})};
  }
  async getJobs(ids,{wait_seconds=0,output_dir}={}) {
    if(!Array.isArray(ids)||ids.length>100)throw new Error('Supply at most 100 job IDs');
    if(!Number.isFinite(wait_seconds)||wait_seconds<0)throw new Error('wait_seconds must be a nonnegative number');
    const deadline=Date.now()+Math.min(20,Math.max(0,wait_seconds))*1000;
    let jobs;
    do {
      jobs=[];
      for(let offset=0;offset<ids.length;offset+=4)jobs.push(...await Promise.all(ids.slice(offset,offset+4).map(async id=>{
        try{return await this.job(id);}catch{return {id,status:'unavailable',input:{},error_message:'Could not retrieve job; retry status lookup using this ID'};}
      })));
      if(!jobs.some(j=>active.has(j.status))||Date.now()>=deadline)break;
      await new Promise(r=>setTimeout(r,400));
    } while(true);
    const results=[];
    for(const job of jobs) {
      const item={job_id:job.id,status:job.status,collection:job.collection,url:job.input.url,title:job.input.title,document_id:job.document_id,error_kind:job.error_kind,error_message:job.error_message};
      if(job.file)item.original={path:job.file.path,sha256:job.file.sha256,bytes:job.file.bytes,extension:job.file.extension,download_url:this.baseUrl+job.file.download_route};
      if(job.document_id) {
        try {
        const doc=await this.document(job.collection,job.document_id);
        item.tags=doc.tags;
        item.extraction_warnings=doc.manifest?.warnings||[];
        item.ocr=doc.manifest?.metadata?.ocr||null;
        item.original??={path:path.join(this.dataRoot,doc.original_key),sha256:doc.id,bytes:doc.bytes,extension:doc.format,download_url:this.baseUrl+'/v1/documents/'+doc.id+'/original?collection='+encodeURIComponent(job.collection)};
        item.normalized_path=path.join(this.dataRoot,doc.normalized_key);
        } catch {item.context_error='Could not retrieve document metadata; retry using this job ID';}
      } else item.tags=job.input.tags||[];
      if(output_dir&&item.original){try{item.saved_path=await this.exportOriginal(item.original,output_dir,item.title);}catch(error){item.export_error=error.message;}}
      results.push(item);
    }
    return {jobs:results,pending:jobs.some(j=>active.has(j.status)),...(output_dir?{output_dir}:{})};
  }
  async completePdfOcr({job_id,max_batches=8,timeout_seconds=300}) {
    if(!/^[a-f0-9]{32}$/.test(job_id)||!Number.isInteger(max_batches)||max_batches<1||max_batches>16||
       !Number.isFinite(timeout_seconds)||timeout_seconds<10||timeout_seconds>600)throw new Error('Supply a job ID, 1-16 batches, and a 10-600 second timeout');
    const deadline=Date.now()+timeout_seconds*1000;
    const chain=[];
    const seen=new Set([job_id]);
    let currentJobId=job_id;
    let previousFailed=null;
    let previousBlank=null;
    while(true) {
      const current=await this.job(currentJobId);
      if(!current.document_id)throw new Error('OCR completion requires a normalized PDF job');
      const document=await this.document(current.collection,current.document_id);
      if(document.format!=='.pdf'||!document.manifest?.metadata?.ocr)throw new Error('OCR completion requires a normalized PDF job');
      const ocr=document.manifest?.metadata?.ocr||{};
      const pendingPages=Array.isArray(ocr.pending_pages)?ocr.pending_pages:[];
      const attemptedPages=new Set(ocr.attempted_pages||[]);
      const failedPages=new Set(ocr.failed_pages||[]);
      const recognizedPages=new Set(ocr.recognized_pages||[]);
      const blankPages=new Set([...attemptedPages].filter(page=>!failedPages.has(page)&&!recognizedPages.has(page)));
      if(chain.length&&(failedPages.size>previousFailed||blankPages.size>previousBlank))return {status:'ocr_page_failures',job_id:currentJobId,
        document_id:current.document_id,batches_run:chain.length,continuation_jobs:chain,quality_status:document.status,
        pending_pages:pendingPages,failed_pages:[...failedPages],blank_pages:[...blankPages],
        unresolved_pages:ocr.unresolved_pages||[],recognition_verified:false};
      if(!pendingPages.length)return {status:'coverage_complete',job_id:currentJobId,document_id:current.document_id,
        batches_run:chain.length,continuation_jobs:chain,quality_status:document.status,pending_pages:[],
        unresolved_pages:ocr.unresolved_pages||[],recognition_verified:false};
      if(chain.length>=max_batches)return {status:'batch_cap_reached',job_id:currentJobId,document_id:current.document_id,
        batches_run:chain.length,continuation_jobs:chain,quality_status:document.status,pending_pages:pendingPages,
        unresolved_pages:ocr.unresolved_pages||[],recognition_verified:false};
      if(Date.now()>=deadline)return {status:'timeout',job_id:currentJobId,document_id:current.document_id,
        batches_run:chain.length,continuation_jobs:chain,pending_pages:pendingPages,recognition_verified:false};
      previousFailed=failedPages.size;
      previousBlank=blankPages.size;
      let next=await this.request('/v1/jobs/'+currentJobId+'/continue-ocr',{});
      if(seen.has(next.id))return {status:'no_progress',job_id:currentJobId,document_id:current.document_id,
        batches_run:chain.length,continuation_jobs:chain,pending_pages:pendingPages,recognition_verified:false};
      seen.add(next.id);
      chain.push(next.id);
      while(active.has(next.status)&&Date.now()<deadline) {
        await new Promise(resolve=>setTimeout(resolve,400));
        next=await this.job(next.id);
      }
      if(active.has(next.status))return {status:'timeout',job_id:next.id,document_id:current.document_id,
        batches_run:chain.length,continuation_jobs:chain,pending_pages:pendingPages,recognition_verified:false};
      if(!['needs_ocr','needs_review','ready'].includes(next.status)||!next.document_id)return {status:'failed',job_id:next.id,
        document_id:current.document_id,batches_run:chain.length,continuation_jobs:chain,failed_job:next,
        pending_pages:pendingPages,recognition_verified:false};
      currentJobId=next.id;
    }
  }
  async exportOriginal(original,outputDir,title='source') {
    if(!/^[a-f0-9]{64}$/.test(original.sha256)||!/^\.[a-z0-9]{1,10}$/.test(original.extension))throw new Error('Invalid original hash or extension');
    const workspace=this.exportRoot;
    await fs.mkdir(workspace,{recursive:true});
    const output=path.resolve(workspace,outputDir);
    const within=(parent,child)=>{const rel=path.relative(parent,child);return rel===''||(!rel.startsWith('..'+path.sep)&&rel!=='..'&&!path.isAbsolute(rel));};
    if(!within(workspace,output))throw new Error('Export folder must be within the export root ('+workspace+'); set GET_FILES_EXPORT_ROOT to change it');
    // Validate the nearest existing ancestor before creating directories, so
    // a junction cannot turn a export-root export into an outside write.
    let ancestor=output;
    while(true) {
      try {
        if(!within(await fs.realpath(workspace),await fs.realpath(ancestor)))throw new Error('Export folder resolves outside the export root');
        break;
      } catch(error) {
        if(error.code!=='ENOENT')throw error;
        const parent=path.dirname(ancestor);
        if(parent===ancestor)throw error;
        ancestor=parent;
      }
    }
    await fs.mkdir(output,{recursive:true});
    if(!within(await fs.realpath(workspace),await fs.realpath(output)))throw new Error('Export folder resolves outside the export root');
    const source=await fs.realpath(original.path);
    if(!within(await fs.realpath(this.dataRoot),source))throw new Error('Original resolves outside corpus storage');
    if(await hashFile(source)!==original.sha256)throw new Error('Source integrity mismatch; nothing exported');
    const label=(title||'source').replace(/[^a-zA-Z0-9 -]/g,'').trim().slice(0,85)||'source';
    const target=path.join(output,label+'-'+original.sha256.slice(0,12)+original.extension);
    try {await fs.copyFile(source,target,constants.COPYFILE_EXCL);}
    catch(error) {if(error.code!=='EEXIST')throw error;}
    if(!within(await fs.realpath(output),await fs.realpath(target))||await hashFile(target)!==original.sha256)throw new Error('Export conflict or integrity mismatch; existing file was not overwritten');
    return target;
  }
}
