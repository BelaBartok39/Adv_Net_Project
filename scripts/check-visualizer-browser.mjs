import {spawn} from 'node:child_process';
import {writeFile} from 'node:fs/promises';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const port=18766;
const server=spawn('python3',['-m','advnet.visualizer','--demo','--port',String(port),'--no-record'],{stdio:['ignore','pipe','pipe']});
let chrome,seq=0,session;const pending=new Map();let buffer='';const errors=[];
function send(method,params={},sessionId){return new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});chrome.stdio[3].write(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})})+'\0');});}
async function evaluate(expression){const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true},session);if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;}
async function waitFor(expression){for(let i=0;i<100;i++){if(await evaluate(expression))return;await sleep(100);}throw new Error('Timed out: '+expression);}
try{
 let ready=false;for(let i=0;i<60;i++){try{const r=await fetch(`http://127.0.0.1:${port}/api/topology`);if(r.ok){ready=true;break;}}catch{}await sleep(100);}if(!ready)throw new Error('Demo server did not start');
 chrome=spawn('chromium',['--headless','--no-sandbox','--disable-dev-shm-usage','--no-first-run','--no-default-browser-check','--user-data-dir=/tmp/advnet-probe-browser-profile','--remote-debugging-pipe','about:blank'],{stdio:['ignore','ignore','pipe','pipe','pipe']});
 chrome.stdio[4].on('data',chunk=>{buffer+=chunk.toString();let i;while((i=buffer.indexOf('\0'))>=0){const raw=buffer.slice(0,i);buffer=buffer.slice(i+1);if(!raw)continue;const m=JSON.parse(raw);if(m.id){const p=pending.get(m.id);pending.delete(m.id);if(m.error)p?.reject(new Error(JSON.stringify(m.error)));else p?.resolve(m.result);}if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.text);}});
 const target=await send('Target.createTarget',{url:`http://127.0.0.1:${port}`});
 session=(await send('Target.attachToTarget',{targetId:target.targetId,flatten:true})).sessionId;
 await send('Runtime.enable',{},session);await send('Page.enable',{},session);
 await send('Emulation.setDeviceMetricsOverride',{width:1500,height:1200,deviceScaleFactor:1,mobile:false},session);
 await waitFor("document.querySelectorAll('#flow-source option').length===11 && document.getElementById('connection').textContent.includes('DEMO')");
 await evaluate("document.getElementById('probe').click()");
 await waitFor("document.getElementById('path-description').textContent.includes('SYNTHETIC PATH') && document.getElementById('timeline').textContent.includes('Probe SEA')");
 const first=await evaluate("({description:document.getElementById('path-description').textContent,highlighted:[...document.querySelectorAll('.backbone-path')].filter(n=>n.style.opacity==='0.85').length,timeline:document.getElementById('timeline').textContent.includes('Probe SEA')})");
 if(first.highlighted!==3)throw new Error('Incorrect path highlighting: '+JSON.stringify(first));
 await evaluate("window.originalFetch=window.fetch;window.fetch=(url,...args)=>String(url).includes('/api/path')?Promise.resolve(new Response(JSON.stringify({error:'Injected browser test failure'}),{status:503,headers:{'Content-Type':'application/json'}})):window.originalFetch(url,...args);document.getElementById('probe').click()");
 await waitFor("document.getElementById('path-description').textContent.includes('Injected browser test failure')");
 await sleep(2500);
 if(!await evaluate("document.getElementById('path-description').textContent.includes('Injected browser test failure')"))throw new Error('SSE erased probe error');
 await evaluate("window.fetch=window.originalFetch;document.getElementById('probe').click()");
 await waitFor("document.getElementById('path-description').textContent.includes('SYNTHETIC PATH')");
 await evaluate("document.getElementById('recording').value='demo';document.getElementById('load-replay').click()");
 await waitFor("document.getElementById('connection').textContent==='REPLAY'");
 await evaluate("document.getElementById('scrubber').value='20';document.getElementById('scrubber').dispatchEvent(new Event('input'));document.querySelector('[data-tab=changes]').click()");
 await waitFor("document.getElementById('change-content').textContent.includes('ip ospf cost 100')");
 await evaluate("document.getElementById('return-live').click()");
 await waitFor("document.getElementById('connection').textContent.includes('DEMO')");
 const screenshot=await send('Page.captureScreenshot',{format:'png'},session);
 await writeFile('/tmp/advnet-probe-fixed.png',Buffer.from(screenshot.data,'base64'));
 await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true},session);
 await sleep(300);
 const layout=await evaluate('({width:window.innerWidth,scroll:document.documentElement.scrollWidth})');
 console.log(JSON.stringify({result:'PASS',checks:['probe renders path','three observed edges highlighted','probe timeline event','errors survive SSE refresh','retry works','replay and diff work'],first,mobile:layout,browser_errors:errors,screenshot:'/tmp/advnet-probe-fixed.png'}));
 if(errors.length)throw new Error('Browser errors: '+errors.join(', '));
}finally{chrome?.kill('SIGTERM');server.kill('SIGTERM');}
