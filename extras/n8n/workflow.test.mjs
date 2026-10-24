import {test} from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs/promises';
import {workflow} from './n8n-workflow.mjs';
const evaluate=(expression,json,hook={})=>vm.runInNewContext(expression.slice(3,-2).trim(),{$json:json,$:()=>({item:{json:hook}})},{timeout:100});
test('workflow graph, credentials, timeouts and controlled responses',async()=>{
  const w=workflow();
  assert.deepEqual(JSON.parse(await fs.readFile(new URL('./workflow.json',import.meta.url),'utf8')),w);
  assert.equal(w.nodes.length,12);assert.equal(new Set(w.nodes.map(n=>n.name)).size,12);
  assert.equal(w.settings.saveDataErrorExecution,'none');assert.equal(w.settings.saveDataSuccessExecution,'none');
  for(const hook of w.nodes.filter(n=>n.type.endsWith('.webhook'))) {
    assert.equal(hook.parameters.authentication,'headerAuth');assert.equal(hook.parameters.responseMode,'responseNode');
    const grant=w.nodes.find(n=>n.name===w.connections[hook.name].main[0][0].node);
    const forward=w.nodes.find(n=>n.name===w.connections[grant.name].main[0][0].node);
    const respond=w.nodes.find(n=>n.name===w.connections[forward.name].main[0][0].node);
    assert.equal(forward.onError,'continueRegularOutput');assert.equal(forward.parameters.options.timeout,20000);
    assert.equal(forward.parameters.options.response.response.neverError,true);
    for(const code of [200,202,400,401,404,429,500,503]) {
      assert.equal(evaluate(respond.parameters.options.responseCode,{statusCode:code,body:{fixture:true}}),code);
      assert.equal(evaluate(respond.parameters.responseBody,{statusCode:code,body:{fixture:true}}).fixture,true);
    }
    for(const failed of [{error:'fixture secret'}, {}, {statusCode:200,body:'invalid'}, {statusCode:0,body:{}}]) {
      assert.equal(evaluate(respond.parameters.options.responseCode,failed),502);
      assert.equal(evaluate(respond.parameters.responseBody,failed).error,'Corpus service unavailable or invalid response');
    }
    for(const request of [grant,forward]) {
      assert.equal(request.parameters.authentication,'none');assert.ok(request.credentials.httpSslAuth);
      assert.equal(request.parameters.provideSslCertificates,true);
      assert.equal(new URL(request.parameters.url).origin,'https://host.docker.internal:8767');
    }
    assert.equal(grant.parameters.jsonBody,forward.parameters.jsonBody);
    const input={query:{collection:'research',q:'x',limit:'3'},params:{jobId:'a'.repeat(32)},body:{collection:'research',items:[{url:'https://example.com'}]}};
    const operation=evaluate(grant.parameters.jsonBody,{},input);
    assert.equal(operation.collection,'research');assert.ok(['ingest','search','job'].includes(operation.action));
    assert.equal(evaluate(forward.parameters.headerParameters.parameters[0].value,{body:{grant:'fixture-grant'}}),'Bearer fixture-grant');
  }
  assert.ok(!w.pinData);assert.ok(w.nodes.every(n=>!n.disabled&&!n.type.includes('executeCommand')&&!n.type.endsWith('.code')));
  assert.ok(!JSON.stringify(w).includes('sourceCorpusHeaderAuthV1'));
});
