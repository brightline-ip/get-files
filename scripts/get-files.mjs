import fs from 'node:fs/promises';
import {parseArgs} from 'node:util';
import {GetFilesClient} from '../integrations/get-files-client.mjs';
const {values,positionals}=parseArgs({allowPositionals:true,options:{batch:{type:'string'},mode:{type:'string',default:'both'},pdf:{type:'boolean'},collection:{type:'string',default:'research'},tag:{type:'string',multiple:true},output:{type:'string'},wait:{type:'string',default:'20'},jobs:{type:'boolean'},'continue-ocr':{type:'string'},'complete-ocr':{type:'string'},'max-ocr-batches':{type:'string',default:'8'},'ocr-timeout':{type:'string',default:'300'}}});
const client=new GetFilesClient();
try {
  let result;
  const options={wait_seconds:Number(values.wait),output_dir:values.output};
  if(values['continue-ocr']&&values['complete-ocr'])throw new Error('Use either --continue-ocr or --complete-ocr');
  if(values['complete-ocr']) {
    const id=values['complete-ocr'];
    if(positionals.length||values.batch||values.jobs)throw new Error('Use --complete-ocr with one prior job ID, without URLs, --batch or --jobs');
    result=await client.completePdfOcr({job_id:id,max_batches:Number(values['max-ocr-batches']),timeout_seconds:Number(values['ocr-timeout'])});
  }
  else if(values['continue-ocr']) {
    const id=values['continue-ocr'];
    if(!/^[a-f0-9]{32}$/.test(id)||positionals.length||values.batch||values.jobs)throw new Error('Use --continue-ocr with one prior job ID, without URLs, --batch or --jobs');
    const job=await client.request('/v1/jobs/'+id+'/continue-ocr',{});
    result=await client.getJobs([job.id],options);
  }
  else if(values.jobs)result=await client.getJobs(positionals,options);
  else {
    if(values.batch&&positionals.length)throw new Error('Use URLs or --batch, not both');
    let items=positionals;
    if(values.batch) {
      const text=(await fs.readFile(values.batch,'utf8')).replace(/^\uFEFF/,'');
      items=text.trim().startsWith('[')?JSON.parse(text):text.split(/\r?\n/).map(x=>x.trim()).filter(x=>x&&!x.startsWith('#'));
    }
    result=await client.getFiles({items,mode:values.mode,format:values.pdf?'pdf':'auto',collection:values.collection,tags:values.tag,...options});
  }
  console.log(JSON.stringify(result,null,2));
  if(result.status&&result.status!=='coverage_complete')process.exitCode=result.status==='failed'?1:3;
  else if(result.status==='coverage_complete'&&(result.quality_status!=='ready'||result.unresolved_pages?.length))process.exitCode=3;
  else if(result.items?.some(x=>x.error)||result.jobs?.some(x=>x.export_error||x.context_error||['failed','needs_access','needs_resolution','unavailable'].includes(x.status)))process.exitCode=1;
  else if(result.pending)process.exitCode=2;
  else if(result.jobs?.some(x=>['needs_ocr','needs_review'].includes(x.status)))process.exitCode=3;
} catch(error) {console.error(error.message);process.exitCode=1;}
