// Invoked only by tests/test_security.py with a disposable loopback service.
import assert from 'node:assert/strict';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
const [port,tokenFile,dataRoot]=process.argv.slice(2);
assert.ok(/^\d+$/.test(port)&&Number(port)!==8766);
const tmp=path.resolve(process.env.TEMP||process.env.TMPDIR||'/tmp').toLowerCase();
assert.ok(path.resolve(tokenFile).toLowerCase().startsWith(tmp)&&path.resolve(dataRoot).toLowerCase().startsWith(tmp));
const client=new Client({name:'isolated-audit',version:'1.0.0'});
const transport=new StdioClientTransport({command:process.execPath,args:[fileURLToPath(new URL('./server.mjs',import.meta.url))],stderr:'pipe',env:{SYSTEMROOT:process.env.SYSTEMROOT,PATH:process.env.PATH,GET_FILES_URL:'http://127.0.0.1:'+port,GET_FILES_TOKEN_FILE:path.resolve(tokenFile),GET_FILES_DATA:path.resolve(dataRoot),GET_FILES_AUTOSTART:'false'}});
try {
  await client.connect(transport);
  const toolNames=(await client.listTools()).tools.map(tool=>tool.name);
  assert.equal(toolNames.length,7);
  assert.ok(toolNames.includes('continue_pdf_ocr'));
  assert.ok(toolNames.includes('complete_pdf_ocr'));
  const result=await client.callTool({name:'get_files',arguments:{url:'https://example.com/mcp-fixture.txt',collection:'research',mode:'both',wait_seconds:10}});
  assert.ok(!result.isError);
  assert.equal(result.structuredContent.jobs[0].status,'ready');
  const document_id=result.structuredContent.jobs[0].document_id;
  const noOcr=await client.callTool({name:'continue_pdf_ocr',arguments:{job_id:result.structuredContent.jobs[0].job_id}});
  assert.equal(noOcr.isError,true); // Ready text cannot trigger OCR or a redownload.
  const search=await client.callTool({name:'search_corpus',arguments:{collection:'research',query:'semiconductor',limit:1}});
  assert.ok(!search.isError);assert.equal(search.structuredContent.results.length,1);
  const read=await client.callTool({name:'read_source',arguments:{collection:'research',document_id,limit:1}});
  assert.ok(!read.isError);assert.equal(read.structuredContent.chunks.length,1);
  const wrong=await client.callTool({name:'read_source',arguments:{collection:'other',document_id,limit:1}});
  assert.equal(wrong.isError,true);
  const hostile=await client.callTool({name:'get_files',arguments:{url:'file:///fixture',collection:'research'}});
  assert.ok(hostile.isError||hostile.structuredContent.items[0].error);
  console.log('isolated MCP passed');
} finally {await client.close();}
