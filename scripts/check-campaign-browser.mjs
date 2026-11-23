// Synthetic files exercise real SSE discovery and rendering; no Docker/model calls.
import {spawn} from 'node:child_process';
import {mkdtemp,mkdir,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const root=await mkdtemp(join(tmpdir(),'advnet-campaign-browser-'));
const campaign=join(root,'coverage'),trial=join(campaign,'trials','0002-jev');
const json=async(path,value)=>writeFile(path,JSON.stringify(value));
const journal=async events=>writeFile(join(trial,'events.jsonl'),events.map(e=>JSON.stringify({at:new Date().toISOString(),elapsed_seconds:1,...e})).join('\n')+'\n');
await mkdir(join(campaign,'trials','0001-off'),{recursive:true});
await mkdir(join(trial,'attempt-1'),{recursive:true});
await json(join(campaign,'plan.json'),{jev_threshold:.9,jobs:[
 {id:'0001-off',scenario:'one.json',system_one:'off',repetition:1},
 {id:'0002-jev',scenario:'two.json',system_one:'jev',repetition:1}]});
await writeFile(join(campaign,'trials','0001-off','events.jsonl'),'{}\n');
await json(join(campaign,'trials','0001-off','result.json'),{success:true,attempts:1,cleanup_succeeded:true});
await json(join(trial,'private-scenario.json'),{name:'two-host-repair',objective:'repair'});
await json(join(trial,'healthy-measurements.json'),{observed_at:new Date().toISOString(),pairs:[],ospf_neighbors:{},all_reachable:true,ospf_healthy:true});
await json(join(trial,'attempt-1','observation.json'),{});
await journal([{stage:'system_one_decision',status:'running'}]);
const port=18767;
const server=spawn(process.env.ADVNET_PYTHON||'python3',['-m','advnet.visualizer','--files-only','--runs',root,'--port',String(port)],{stdio:['ignore','pipe','pipe']});
let chrome,seq=0,session,buffer='';const pending=new Map(),errors=[];
const send=(method,params={},sessionId)=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});chrome.stdio[3].write(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})})+'\0');});
async function evaluate(expression){const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true},session);if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;}
async function waitFor(expression){for(let i=0;i<120;i++){if(await evaluate(expression))return;await sleep(100);}throw new Error('Timed out: '+expression);}
try{
 let ready=false;for(let i=0;i<80;i++){try{if((await fetch(`http://127.0.0.1:${port}/api/topology`)).ok){ready=true;break;}}catch{}await sleep(100);}if(!ready)throw new Error('Campaign server did not start');
 chrome=spawn('chromium',['--headless','--no-sandbox','--disable-dev-shm-usage','--no-first-run','--no-default-browser-check','--user-data-dir='+join(root,'chrome'),'--remote-debugging-pipe','about:blank'],{stdio:['ignore','ignore','pipe','pipe','pipe']});
 chrome.stdio[4].on('data',chunk=>{buffer+=chunk.toString();let i;while((i=buffer.indexOf('\0'))>=0){const raw=buffer.slice(0,i);buffer=buffer.slice(i+1);if(!raw)continue;const m=JSON.parse(raw);if(m.id){const p=pending.get(m.id);pending.delete(m.id);if(m.error)p?.reject(new Error(JSON.stringify(m.error)));else p?.resolve(m.result);}if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.text);}});
 const target=await send('Target.createTarget',{url:`http://127.0.0.1:${port}`});session=(await send('Target.attachToTarget',{targetId:target.targetId,flatten:true})).sessionId;
 await send('Runtime.enable',{},session);await send('Emulation.setDeviceMetricsOverride',{width:1500,height:1200,deviceScaleFactor:1,mobile:false},session);
 await waitFor("document.getElementById('campaign-count')?.textContent==='1 / 2 complete'");
 await waitFor("document.getElementById('jev-route').textContent==='Decision in progress'");
 await evaluate("document.getElementById('inspect-decision').click()");
 await waitFor("document.getElementById('change-content').textContent.includes('Decision in progress')");
 await json(join(trial,'attempt-1','decision.json'),{status:'decision_complete',screening:{selected:'enable_host_sea',selected_probability:.59,effective_route:'escalate'},proposal:{outcome:'candidate',diagnosis:'Synthetic repair evidence',changes:[{device:'sea',interface:'Ethernet3',action:'set_interface_enabled',value:true}]},calls:[
  {stage:'jev',model:'typesafe/test-fixture',elapsed_ms:450,usage:{input_tokens:15000,output_tokens:145,cost:.0007}},
  {stage:'llm',model:'openai/test-fixture',elapsed_ms:4000,usage:{prompt_tokens:12345,completion_tokens:678,cost:.002}}]});
 await journal([{stage:'system_one_decision',status:'complete'},{stage:'verify_candidate',status:'running'}]);
 await waitFor("document.getElementById('jev-detail').textContent.includes('59%') && document.getElementById('jev-detail').textContent.includes('escalate')");
 await waitFor("document.getElementById('llm-calls').textContent.includes('12345') && document.getElementById('llm-calls').textContent.includes('678')");
 await writeFile('/tmp/advnet-campaign-browser.png',Buffer.from((await send('Page.captureScreenshot',{format:'png'},session)).data,'base64'));
 await mkdir(join(trial,'attempt-2'));
 await json(join(trial,'attempt-2','observation.json'),{});
 await journal([{stage:'system_one_decision',status:'complete'},{stage:'system_one_decision',status:'running'}]);
 await waitFor("document.getElementById('attempt-select').value==='attempt-2' && document.getElementById('change-content').textContent.includes('Decision in progress')");
 await json(join(trial,'attempt-2','decision.json'),{status:'decision_complete',screening:{bypassed:true,reason:'prior_rejection',effective_route:'escalate'},calls:[{stage:'llm',model:'openai/test-fixture',elapsed_ms:3000,usage:{prompt_tokens:14000,completion_tokens:456}}],proposal:{outcome:'candidate',diagnosis:'Second fixture',changes:[]}});
 await json(join(trial,'result.json'),{success:true,attempts:2,cleanup_succeeded:true,fault_to_acceptance_seconds:50,mode:'jev_llm'});
 await waitFor("document.getElementById('campaign-count').textContent==='2 / 2 complete' && document.getElementById('active-calls').textContent.includes('LLM 2')");
 await waitFor("document.getElementById('jev-route').textContent==='Screening bypassed'");
 await evaluate("document.getElementById('recording').value='coverage/trials/0002-jev';document.getElementById('load-replay').click()");
 await waitFor("document.getElementById('connection').textContent==='REPLAY'");
 if(await evaluate("document.getElementById('active-calls').textContent.includes('LLM 2')"))throw new Error('Replay revealed future calls');
 await evaluate("document.getElementById('return-live').click()");
 await waitFor("document.getElementById('campaign-count').textContent==='2 / 2 complete'");
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true},session);await sleep(200);
 const layout=await evaluate('({width:innerWidth,scroll:document.documentElement.scrollWidth})');if(layout.scroll>layout.width+1)throw new Error('Mobile overflow: '+JSON.stringify(layout));
 if(errors.length)throw new Error('Browser errors: '+errors.join(', '));
 console.log(JSON.stringify({result:'PASS',checks:['nested campaign discovery','pending decision shown','Jev probability and fallback shown','both token usage formats','automatic attempt following','live progress and model counts','replay cutoff','mobile layout'],evidence:root,screenshot:'/tmp/advnet-campaign-browser.png'}));
}finally{chrome?.kill('SIGTERM');server.kill('SIGTERM');}
