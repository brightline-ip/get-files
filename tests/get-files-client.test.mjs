import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {GetFilesClient} from '../integrations/get-files-client.mjs';

const root=fileURLToPath(new URL('../',import.meta.url));
test('export preserves originals, reuses identical files, rejects overwrite and outside folders',async()=>{
  const base=await fs.mkdtemp(path.join(os.tmpdir(),'get-files-test-'));
  const folder=path.join(base,'export');
  await fs.mkdir(folder);
  const sourceFolder=path.join(base,'data');
  await fs.mkdir(sourceFolder);
  const client=new GetFilesClient({dataRoot:sourceFolder,exportRoot:base,autoStart:false});
  try {
    const bytes=Buffer.from('Preserve these exact source bytes');
    const source=path.join(sourceFolder,'original.txt');
    await fs.writeFile(source,bytes);
    const original={path:source,sha256:createHash('sha256').update(bytes).digest('hex'),extension:'.txt'};
    await assert.rejects(client.exportOriginal({...original,extension:'/../../bad'},folder,'Source'),/Invalid/);
    await assert.rejects(client.exportOriginal({...original,sha256:'0'.repeat(64)},folder,'Source'),/integrity/);
    assert.deepEqual(await fs.readdir(folder),[]);
    const output=await client.exportOriginal(original,folder,'Source');
    assert.equal(output,await client.exportOriginal(original,folder,'Source'));
    await fs.writeFile(output,'existing user content');
    await assert.rejects(client.exportOriginal(original,folder,'Source'),/conflict|integrity/);
    assert.equal(await fs.readFile(output,'utf8'),'existing user content');
    await assert.rejects(client.exportOriginal(original,path.parse(root).root,'Source'),/within/);
    await assert.rejects(client.getJobs(['unused'],{wait_seconds:NaN}),/nonnegative/);
  } finally {
    await fs.rm(base,{recursive:true,force:true});
  }
});

test('invalid waits do not enqueue and individual failures preserve successful jobs',async()=>{
  const client=new GetFilesClient({autoStart:false});
  client.request=async()=>{throw new Error('must not enqueue');};
  await assert.rejects(client.getFiles({url:'https://example.com',wait_seconds:NaN}),/nonnegative/);
  client.job=async id=>{if(id==='missing')throw new Error('fixture failure');return {id,status:'ready',collection:'research',input:{},document_id:'a'.repeat(64),file:{path:'unused',sha256:'a'.repeat(64),bytes:1,extension:'.txt',download_route:'/v1/fixture'}};};
  client.document=async()=>{throw new Error('fixture failure');};
  client.exportOriginal=async()=>{throw new Error('fixture export failure');};
  const result=await client.getJobs(['missing','saved'],{output_dir:root});
  assert.equal(result.jobs[0].status,'unavailable');
  assert.equal(result.jobs[1].status,'ready');
  assert.ok(result.jobs[1].original);
  assert.ok(result.jobs[1].context_error);
  assert.ok(result.jobs[1].export_error);
});

test('complete PDF OCR follows continuation jobs and stops at the configured cap',async()=>{
  const client=new GetFilesClient({autoStart:false});
  const jobs={
    a:{id:'a'.repeat(32),status:'needs_ocr',collection:'research',document_id:'1'.repeat(64),input:{}},
    b:{id:'b'.repeat(32),status:'needs_ocr',collection:'research',document_id:'1'.repeat(64),input:{}},
    c:{id:'c'.repeat(32),status:'needs_review',collection:'research',document_id:'1'.repeat(64),input:{}},
  };
  const documents=[
    {format:'.pdf',status:'needs_ocr',manifest:{metadata:{ocr:{attempted_pages:[1],recognized_pages:[1],pending_pages:[2,3],unresolved_pages:[2,3]}}}},
    {format:'.pdf',status:'needs_ocr',manifest:{metadata:{ocr:{attempted_pages:[1,2],recognized_pages:[1,2],pending_pages:[3],unresolved_pages:[3]}}}},
    {format:'.pdf',status:'needs_review',manifest:{metadata:{ocr:{attempted_pages:[1,2,3],recognized_pages:[1,2,3],pending_pages:[],unresolved_pages:[]}}}},
  ];
  let current=0;
  client.job=async id=>jobs[id[0]];
  client.document=async()=>documents[current];
  client.request=async()=>{current++;return current===1?jobs.b:jobs.c;};
  const complete=await client.completePdfOcr({job_id:jobs.a.id,max_batches:4,timeout_seconds:10});
  assert.equal(complete.status,'coverage_complete');
  assert.equal(complete.batches_run,2);
  assert.equal(complete.quality_status,'needs_review');
  current=0;
  const capped=await client.completePdfOcr({job_id:jobs.a.id,max_batches:1,timeout_seconds:10});
  assert.equal(capped.status,'batch_cap_reached');
  assert.deepEqual(capped.pending_pages,[3]);
});

test('complete PDF OCR traverses an existing successor before creating the next one',async()=>{
  const client=new GetFilesClient({autoStart:false});
  const a={id:'a'.repeat(32),status:'needs_ocr',collection:'research',document_id:'1'.repeat(64),input:{}};
  const b={id:'b'.repeat(32),status:'needs_ocr',collection:'research',document_id:'1'.repeat(64),input:{}};
  const c={id:'c'.repeat(32),status:'needs_review',collection:'research',document_id:'1'.repeat(64),input:{}};
  const jobs={a,b,c};
  let complete=false;
  const calls=[];
  client.job=async id=>jobs[id[0]];
  client.document=async()=>complete
    ?{format:'.pdf',status:'needs_review',manifest:{metadata:{ocr:{attempted_pages:[1,2,3],recognized_pages:[1,2,3],pending_pages:[],unresolved_pages:[]}}}}
    :{format:'.pdf',status:'needs_ocr',manifest:{metadata:{ocr:{attempted_pages:[1,2],recognized_pages:[1,2],pending_pages:[3],unresolved_pages:[3]}}}};
  client.request=async route=>{
    calls.push(route);
    if(route.includes(a.id))return b;
    complete=true;
    return c;
  };
  const result=await client.completePdfOcr({job_id:a.id,max_batches:4,timeout_seconds:10});
  assert.equal(result.status,'coverage_complete');
  assert.equal(result.batches_run,2);
  assert.equal(calls[1],'/v1/jobs/'+b.id+'/continue-ocr');
});

test('client launches the absolute Windows PowerShell path and exports no internal pipeline helper',async()=>{
  const source=await fs.readFile(path.join(root,'integrations','get-files-client.mjs'),'utf8');
  assert.doesNotMatch(source,/spawnSync\('powershell\.exe'/);
  assert.match(source,/System32.*WindowsPowerShell.*v1\.0.*powershell\.exe/);
  const base=await fs.readFile(path.join(root,'integrations','client.mjs'),'utf8');
  assert.doesNotMatch(base,/publicClientSources|prospect_key|client-research/);
});
