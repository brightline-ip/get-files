// Hub webhook authentication and the restricted TLS identity are distinct
// credentials. Neither is the corpus master token.
export function workflow(webhookCredentialId='__N8N_CORPUS_WEBHOOK__',tlsCredentialId='__N8N_CORPUS_TLS__') {
  const nodes=[],connections={};
  const routes=[
    {name:'Intake',method:'POST',path:'source-corpus-intake',action:'ingest',collection:'h.body.collection',parameters:'({items:h.body.items})'},
    {name:'Search',method:'GET',path:'source-corpus-search',action:'search',collection:'h.query.collection',parameters:"({q:h.query.q || '',limit:Number(h.query.limit || 5),tags:(h.query.tags || '').split(',').filter(Boolean),include_incomplete:h.query.include_incomplete === 'true'})"},
    {name:'Job',method:'GET',path:'source-corpus-jobs/:jobId',action:'job',collection:'h.query.collection',parameters:'({job_id:h.params.jobId})'},
  ];
  for(const [index,r] of routes.entries()) {
    const hook=r.name+' request',grant=r.name+' operation grant',forward=r.name+' local service',respond=r.name+' response';
    const body="={{ (() => { const h=$('"+hook+"').item.json; return {action:'"+r.action+"',collection:"+r.collection+",parameters:"+r.parameters+"}; })() }}";
    nodes.push({id:'corpus-'+index+'-hook',name:hook,type:'n8n-nodes-base.webhook',typeVersion:2,position:[-640,index*240],webhookId:'source-corpus-'+r.name.toLowerCase()+'-v1',credentials:{httpHeaderAuth:{id:webhookCredentialId,name:'Source Corpus - n8n webhook only'}},parameters:{httpMethod:r.method,path:r.path,authentication:'headerAuth',responseMode:'responseNode',options:{}}});
    for(const [name,suffix,x] of [[grant,'grants',-400],[forward,'execute',-140]]) {
      nodes.push({id:'corpus-'+index+'-'+suffix,name,type:'n8n-nodes-base.httpRequest',typeVersion:4.2,position:[x,index*240],credentials:{httpSslAuth:{id:tlsCredentialId,name:'Source Corpus - restricted TLS identity'}},onError:'continueRegularOutput',parameters:{method:'POST',url:'https://host.docker.internal:8767/v1/'+suffix,provideSslCertificates:true,authentication:'none',sendBody:true,specifyBody:'json',jsonBody:body,...(suffix==='execute'?{sendHeaders:true,headerParameters:{parameters:[{name:'Authorization',value:"={{ 'Bearer ' + ($json.body?.grant || 'invalid') }}"}]}}:{}),options:{timeout:20000,response:{response:{fullResponse:true,neverError:true,responseFormat:'json'}}}}});
    }
    const valid="$json.statusCode >= 200 && $json.statusCode <= 599 && $json.body && typeof $json.body === 'object'";
    nodes.push({id:'corpus-'+index+'-respond',name:respond,type:'n8n-nodes-base.respondToWebhook',typeVersion:1.4,position:[140,index*240],parameters:{respondWith:'json',responseBody:"={{ ("+valid+") ? $json.body : {error:'Corpus service unavailable or invalid response'} }}",options:{responseCode:"={{ ("+valid+") ? $json.statusCode : 502 }}"}}});
    for(const [from,to] of [[hook,grant],[grant,forward],[forward,respond]])connections[from]={main:[[{node:to,type:'main',index:0}]]};
  }
  return {name:'Source Corpus - Intake and Search',nodes,connections,settings:{executionOrder:'v1',executionTimeout:60,saveDataSuccessExecution:'none',saveDataErrorExecution:'none',saveManualExecutions:false}};
}
