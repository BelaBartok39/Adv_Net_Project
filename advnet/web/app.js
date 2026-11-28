'use strict';
const $ = id => document.getElementById(id);
const NS = 'http://www.w3.org/2000/svg';
const positions = {sea:[145,110],sva:[100,320],lax:[165,510],den:[365,235],kcy:[545,290],hou:[540,525],atl:[770,440],ind:[745,250],chi:[660,100],was:[970,325],nyc:[1000,135]};
const stageNames = {restore_baseline:'Restore source baseline',baseline_capacities:'Reset link capacities',path_probe:'Path probe',checkpoint_healthy:'Checkpoint',measure_healthy:'Healthy baseline',verify_healthy:'Verify baseline',initial_convergence:'Wait for healthy OSPF',inject_fault:'Fault injection',inject_capacity_change:'Link impairment',fault_convergence:'Fault convergence',measure_impaired:'Measure impairment',observe:'Collect state',llm_decision:'LLM decision',system_one_decision:'Jev screening / LLM fallback',verify_candidate:'Batfish verification',deploy:'Apply candidate',post_deploy_convergence:'Wait for OSPF recovery',measure_candidate:'Measure outcome',rollback_candidate:'Roll back',harness_reset_not_a_repair:'Harness reset',reset_convergence:'Wait for reset OSPF',verify_reset_runtime:'Check reset',restore_capacities:'Restore capacity',validate_healthy_traffic:'Check healthy flow overlap',validate_impaired_traffic:'Check impaired flow overlap'};
Object.assign(stageNames, {
 continuous_start:'Continuous monitoring started', reachability_failed:'Host connectivity lost',
 reachability_restored:'Host connectivity restored', monitoring:'Monitoring the network',
 evaluation_pending:'Assessing the impairment', decision:'Waiting for decision', decision_result:'Decision returned',
 proposal:'Candidate proposed', proposal_assessment:'Check candidate', verification:'Batfish verification',
 baseline_verified:'Baseline verified', verified:'Batfish approved', deployment:'Apply candidate',
 runtime_validation:'Check live recovery', runtime_result:'Runtime checks', cooldown:'Monitoring during cooldown',
 rollback:'Roll back candidate', recovery:'Check saved intervention', blocked:'Interventions blocked',
 continuous_success:'Runtime accepted', continuous_rejected:'Candidate rejected',
 continuous_rolled_back:'Candidate rolled back', continuous_rollback_unconfirmed:'Rollback unconfirmed',
 continuous_failed:'Intervention failed', stopped:'Continuous run ended',
 inference_discarded:'Decision discarded', proposal_rejected:'Proposal rejected',
 policy_rejected:'Stability policy rejected the proposal', worker_error:'Decision worker failed'
});
let topology, latest, frame, experiment, observedPath, paused=false, replay=null, replayIndex=0, player=null;
let selected='sea', selectedHost=false, selectedLink=null, activeTab='device', selectedEvent=null, zoom=1, stream;
let history=[], lastHistoryTime=null, edges=new Map(), routers=new Map(), hosts=new Map();
let probeState={status:'idle',message:''};
let lastExperimentId=null;
const node = (tag, attrs={}, text) => {const el=document.createElementNS(NS,tag);for(const [k,v] of Object.entries(attrs))el.setAttribute(k,v);if(text!==undefined)el.textContent=text;return el;};
const html = (tag, cls='', text) => {const el=document.createElement(tag);if(cls)el.className=cls;if(text!==undefined)el.textContent=text;return el;};
const fmt = (value, digits=1) => typeof value==='number'&&Number.isFinite(value)?value.toFixed(digits):'—';
const stageLabel = stage => stageNames[stage] || (typeof stage==='string' ? stage.replaceAll('_',' ') : 'Observation');
function rateLabel(value){
 if(value===0||value>=1)return [fmt(value,2),'Mbps'];
 if(value>=.001)return [fmt(value*1000,1),'kbps'];
 return [fmt(value*1e6,1),'bps'];
}
const clock = value => value?new Date(value).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}):'not sampled';
const edgeKey = (a,b) => [a,b].sort().join('|');
const set = (id,text) => {const el=$(id);if(el.textContent!==String(text))el.textContent=text;};
const age = value => {if(!value)return 'not sampled';const seconds=Math.max(0,(Date.now()-Date.parse(value))/1000);return seconds<60?`${Math.round(seconds)}s ago`:seconds<3600?`${Math.round(seconds/60)}m ago`:`${Math.round(seconds/3600)}h ago`;};
function pill(id,text,kind='muted'){set(id,text);$(id).className='pill '+kind;}
function detailList(values){const dl=html('dl','details');for(const [key,value] of values){const pair=html('div');pair.append(html('dt','',key),html('dd','',value??'—'));dl.append(pair);}return dl;}
async function api(path){const response=await fetch(path,{cache:'no-store'});const data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);return data;}
function cost(name,iface){const config=frame?.nodes?.[name]?.config;if(!config)return null;const lines=config.split('\n');const start=lines.findIndex(l=>l.trimEnd()==='interface '+iface);if(start<0)return null;for(let i=start+1;i<lines.length&&/^\s/.test(lines[i]);i++){const m=lines[i].trim().match(/^ip ospf cost (\d+)$/);if(m)return Number(m[1]);}return null;}
function inspect(target, name, {host=false, link=null}={}){
 const open=()=>{
  selected=name;selectedHost=host;selectedLink=link;activeTab='device';
  renderMap();renderInspector();
  if(window.matchMedia('(max-width:780px)').matches)$('inspector-title').scrollIntoView({block:'nearest'});
 };
 target.addEventListener('click',open);
 target.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open();}});
}
function hostState(host){
 if(host.attachment==='admin_down')return 'admin down';
 if(host.status&&!['running','recorded','unknown'].includes(host.status))return host.status;
 return host.reachability||host.status||'unknown';
}
function buildMap(){
 const svg=$('topology'),group=node('g',{id:'viewport'});svg.append(group);
 for(const link of topology.links){const [x1,y1]=positions[link.a], [x2,y2]=positions[link.b];const g=node('g',{'data-link':link.id});
  const title=node('title');g.append(title);const attrs={x1,y1,x2,y2};
  const base=node('line',{...attrs,class:'backbone-base'}), flow=node('line',{...attrs,class:'backbone-flow'}), path=node('line',{...attrs,class:'backbone-path'}), hit=node('line',{...attrs,class:'backbone-hit',tabindex:0,role:'button','aria-label':`${link.a} to ${link.b}, inspect link`});
  const rate=node('text',{x:(x1+x2)/2,y:(y1+y2)/2-8,class:'backbone-label'}), label=node('text',{x:(x1+x2)/2,y:(y1+y2)/2+9,class:'backbone-cost'});
  inspect(hit,link.a,{link:link.id});
  g.append(base,flow,path,rate,label,hit);group.append(g);edges.set(edgeKey(link.a,link.b),{base,flow,path,rate,label,title,link});
 }
 for(const [name,r] of Object.entries(topology.routers)){const [x,y]=positions[name];const hx=x+(x>850?45:-45),hy=y+(y>440?45:-42);
  const wire=node('line',{x1:x,y1:y,x2:hx,y2:hy,class:'host-wire'}), path=node('line',{x1:x,y1:y,x2:hx,y2:hy,class:'backbone-path'});group.append(wire,path);edges.set(edgeKey(name,r.host),{path,wire,host:r.host});
  const host=node('g',{class:'host',tabindex:0,role:'button','aria-label':`${r.host}, ${r.host_ip}`});host.append(node('rect',{x:hx-6,y:hy-5,width:12,height:9,rx:1,class:'host-body'}),node('text',{x:hx+10,y:hy+3,class:'host-label'},r.host));
  inspect(host,name,{host:true});group.append(host);hosts.set(r.host,host);
  const router=node('g',{class:'router',transform:`translate(${x},${y})`,tabindex:0,role:'button','aria-label':`${r.site}, select router`});
  router.append(node('circle',{r:30,class:'halo'}),node('rect',{x:-18,y:-16,width:36,height:32,rx:8,class:'body'}),node('path',{d:'M -10 -4 H 10 M 5 -8 L 10 -4 L 5 0 M 10 5 H -10 M -5 1 L -10 5 L -5 9',class:'icon'}),node('text',{x:0,y:45,class:'name'},r.site),node('text',{x:0,y:59,class:'tag'},name.toUpperCase()));
  inspect(router,name);group.append(router);routers.set(name,router);
 }
}
function renderMap(){
 const list=frame?.links||[], pathEdges=new Set((observedPath?.edges||[]).map(([a,b])=>edgeKey(a,b)));
 for(const [key,items] of edges){items.path.style.opacity=pathEdges.has(key)?'0.85':'0';if(items.host){const state=hostState(frame?.hosts?.[items.host]||{});items.wire.classList.toggle('down',['admin down','unreachable'].includes(state));continue;}const l=list.find(x=>x.id===items.link.id)||{}, state=l.state||'unknown';
  const high=typeof l.utilization==='number'&&l.utilization>=.85;const color=high?'#e8b86a':state==='up'?'#588f83':state==='degraded'?'#d0a461':'#486071';
  const values=[l.a_mbps,l.b_mbps].filter(v=>typeof v==='number'), rate=values.length?Math.max(...values):null;
  items.base.setAttribute('stroke',color);items.base.style.stroke=color;items.base.style.strokeWidth=rate===null?'2':String(2+Math.min(5,Math.log2(1+rate)));
  items.flow.style.stroke=high?'#e8b86a':'#78cdb5';items.flow.style.opacity=rate!==null&&rate>.005?'0.65':'0';items.flow.style.strokeWidth=rate===null?'2':String(1+Math.min(3,Math.log2(1+rate)));
  items.rate.textContent=rate===null?(typeof l.utilization==='number'?`${fmt(l.utilization*100,0)}%`:''):rateLabel(rate).join(' ');
  const ca=cost(l.a,l.a_interface), cb=cost(l.b,l.b_interface);items.label.textContent=ca!==null&&cb!==null?`c ${ca===cb?ca:ca+' / '+cb}`:'cost not sampled';
  items.title.textContent=`${items.link.a}:${items.link.a_interface} ↔ ${items.link.b}:${items.link.b_interface}\nOSPF: ${state}\nDirectional peak: ${fmt(rate,3)} Mbps`;
 }
 for(const [name,g] of routers){const n=frame?.nodes?.[name]||{}, related=list.filter(l=>l.a===name||l.b===name);let state='unknown';if(['running','recorded'].includes(n.status)){state=related.some(l=>l.state==='degraded')?'degraded':related.length&&related.every(l=>l.state==='up')?'up':'unknown';}else if(n.status&&n.status!=='unknown')state='down';g.setAttribute('class',`router ${state}${name===selected&&!selectedLink&&!selectedHost?' selected':''}`);}
 for(const [name,g] of hosts){
  const current=hostState(frame?.hosts?.[name]||{});
  g.setAttribute('class',`host ${current==='admin down'?'unreachable':current}${selectedHost&&topology.routers[selected].host===name?' selected':''}`);
 }
}
function renderSummary(){
 const nodes=Object.values(frame?.nodes||{});const observed=nodes.filter(n=>['running','recorded'].includes(n.status)).length;
 set('router-count',observed||'—');set('router-note',frame?.source==='recorded'?'Recorded router observations':`${nodes.filter(n=>n.status==='running').length} running · ${Math.max(0,11-nodes.filter(n=>n.status==='running').length)} unknown or stopped`);
 const links=frame?.links||[];set('link-count',links.length?links.filter(l=>l.state==='up').length:'—');
 const measurement=frame?.measurement||latest?.last_check;const pairs=measurement?.pairs;
 set('pair-count',measurement?.pair_passed??(pairs?.length?pairs.filter(p=>p.reachable&&p.loss_fraction===0).length:measurement?.all_reachable===true&&frame?.source==='demo'?110:'—'));
 if($('pair-total'))set('pair-total',' / '+(measurement?.pair_total??110));
 set('pair-note',frame?.source==='demo'?'Synthetic reachability sample':measurement?.observed_at?`${measurement.source==='controller'?'Controller check · ':''}${clock(measurement.observed_at)} · ${age(measurement.observed_at)}`:'No connectivity sample');
 const rates=links.flatMap(l=>[l.a_mbps,l.b_mbps]).filter(v=>typeof v==='number');const utilizations=links.map(l=>l.utilization).filter(v=>typeof v==='number');
 if(rates.length){
  const [value,unit]=rateLabel(Math.max(...rates)),runPeak=!replay&&experiment?.continuous?.peak_mbps;
  set('peak-rate',value);set('peak-unit',' '+unit);
  set('peak-note',(frame.source==='demo'?'Synthetic counter sample':frame.source==='recorded'?'Recorded counter sample':'Latest counter sample')+(typeof runPeak==='number'?' · Run peak '+rateLabel(runPeak).join(' '):''));
 }
 else if(utilizations.length){set('peak-rate',fmt(Math.max(...utilizations)*100,0));set('peak-unit',' %');set('peak-note','Recorded utilization sample');}else{set('peak-rate','—');set('peak-unit',' Mbps');set('peak-note','Waiting for two counter samples');}
 set('rtt',`Mean RTT ${fmt(measurement?.mean_rtt_ms,2)} ms`);set('sample-time',frame?.sampled_at?`${replay?'Replay':'Sample'} ${clock(frame.sampled_at)} · ${frame.source}`:'Waiting for telemetry');
 const notice=$('notice');let message;
 if(replay){pill('connection','REPLAY','warning');notice.className='notice warning';message=`Recorded observations · ${replay.id} · checkpoint ${replayIndex+1} of ${replay.frames.length}. Gaps are not interpolated.`;}
 else if(latest?.mode==='demo'){pill('connection','DEMO / SYNTHETIC','warning');notice.className='notice warning';message='DEMONSTRATION ONLY — simulated traffic, model decisions, and verification. No Docker commands or API calls.';}
 else if(latest?.mode==='files'){pill('connection','FILES ONLY','muted');notice.className='notice';message='Experiment files update live; the map shows the last recorded measurement checkpoint, not continuous telemetry.';}
 else if(latest?.mode==='relay'){pill('connection',frame?.errors?.length?'OBSERVER STALE':'EXISTING OBSERVER',frame?.errors?.length?'warning':'good');notice.className=frame?.errors?.length?'notice warning':'notice';message=frame?.errors?.length?frame.errors.join(' · '):`Live telemetry from observer :${latest.observer_port}. No additional Docker polling. Campaign artifacts refresh every two seconds.`;}
 else if(frame?.errors?.length){pill('connection','TELEMETRY LIMITED','warning');notice.className='notice warning';message=frame.errors.join(' · ');}
 else{pill('connection','LIVE OBSERVER','good');notice.className='notice';message='Read-only telemetry. Manual path probes send ICMP traffic. Counter animation represents rates, not individual packets.';}
 if(paused&&!replay)message+=' View paused; collection and recording continue.';notice.textContent=message;
 set('recording-state',latest?.recording?`Recording ${latest.recording}`:'Not recording');
}
function callCards(id,calls){
 const box=$(id);box.replaceChildren();
 for(const call of calls){const usage=call.usage||{},cost=usage.cost??usage.cost_usd;
  const row=html('div','model-call');row.append(html('strong','',call.model||'Model unavailable'),html('span','small',`${call.error_type||'Completed'} · ${fmt(call.elapsed_ms/1000,2)} s`),html('span','small',`Tokens in ${usage.input_tokens??usage.prompt_tokens??'unknown'} / out ${usage.output_tokens??usage.completion_tokens??'unknown'} · cost ${typeof cost==='number'?'$'+cost.toFixed(6):'unknown'}`));box.append(row);
 }
}
function renderDecisionMonitor(){
 const attempt=experiment?.attempts?.at(-1),decision=attempt?.decision,screen=decision?.screening||{};
 const continuous=experiment?.continuous;
 const phase=continuous?.phase||experiment?.events?.at(-1),campaign=!replay?latest?.campaign:null,job=campaign?.jobs?.find(j=>j.id===experiment?.id);
 const recordedMode=continuous?.mode||experiment?.result?.mode;
 const mode=job?.mode||(recordedMode==='jev_llm'?'jev':recordedMode==='llm_only'?'off':null);
 const calls=decision?.calls||[],jev=calls.filter(c=>c.stage==='jev'),llm=calls.filter(c=>(c.stage||'llm')==='llm');
 const allCalls=(experiment?.attempts||[]).flatMap(a=>a.decision?.calls||[]);
 set('active-trial',experiment?.scenario?.name||experiment?.id?.split('/').at(-1)||'Waiting for a trial');
 pill('decision-mode',continuous?.observe_only?'OBSERVE ONLY':mode==='jev'?'JEV + LLM':mode==='off'?'LLM ONLY':experiment?'DECISION PIPELINE':'NO TRIAL',experiment?'good':'muted');
 set('active-phase',experiment?.result?(experiment.result.success?'Runtime accepted':'Trial ended without acceptance'):phase?stageLabel(phase.stage):'Waiting for a journal');
 set('phase-elapsed',phase&&!experiment?.result?phase.status==='running'?` · running ${fmt(Math.max(0,((replay?Date.parse(frame.sampled_at):Date.now())-Date.parse(phase.at))/1000),0)} s`:typeof phase.elapsed_seconds==='number'?` · ${phase.status} (${fmt(phase.elapsed_seconds,1)} s)`:'':'');
 set('active-calls',`Recorded requests · Jev ${allCalls.filter(c=>c.stage==='jev').length} · LLM ${allCalls.filter(c=>(c.stage||'llm')==='llm').length}${attempt?' · '+attempt.id:''}`);
 const pending=['decision','llm_decision','system_one_decision'].includes(phase?.stage)&&phase.status==='running'&&!decision;
 set('jev-route',mode==='off'||continuous?.observe_only?'Disabled for this arm':screen.bypassed?'Screening bypassed':screen.selected?screen.selected:screen.error_type?'Screening failed':pending?'Decision in progress':'Pending decision');
 set('jev-detail',screen.selected?`Choice probability ${fmt(screen.selected_probability==null?null:screen.selected_probability*100,0)}% · threshold ${fmt((decision?.threshold??campaign?.settings?.jev_threshold??experiment?.result?.decision_settings?.jev_threshold??.9)*100,0)}% · effective route: ${screen.effective_route}`:screen.bypassed?(screen.reason||'LLM-only mode').replaceAll('_',' '):screen.error_type?`${screen.error_type}; effective route ${screen.effective_route}`:pending&&mode!=='off'?'Jev and any LLM fallback are one journal stage. The exact route appears when the decision returns.':'');
 set('llm-route',llm.length?(llm.some(c=>c.error_type)?'Request failed':'Response recorded'):decision&&screen.effective_route&&screen.effective_route!=='escalate'?'Not called — Jev shortcut':pending?'Awaiting decision result':'Pending decision');
 set('llm-detail',llm.length?`${llm.length} request(s) in this attempt`:pending?'A pending decision is not counted as a completed API call.':decision&&screen.effective_route!=='escalate'?'The catalog proposal still requires Batfish and live acceptance.':'');
 callCards('jev-calls',jev);callCards('llm-calls',llm);
 const p=decision?.proposal,gate=attempt?.verification,accept=attempt?.acceptance;
 const outcome=attempt?.outcome;
 set('candidate-state',outcome?stageLabel('continuous_'+outcome):attempt?.decision_state?stageLabel(attempt.decision_state):accept?accept.accepted?'Runtime accepted':'Runtime rejected':gate?gate.accepted?'Batfish approved':'Batfish rejected':p?.outcome==='candidate'?'Proposed changes':p?.outcome==='needs_observation'?'More evidence requested':decision?.status==='decision_error'?'Decision error':pending?'Awaiting proposal':'No proposal yet');
 set('candidate-detail',p?.changes?.length?p.changes.map(c=>`${c.device.toUpperCase()} ${c.interface}: ${c.action.replaceAll('_',' ')} → ${c.value}`).join(' · '):p?.diagnosis||decision?.error_type||'');
 const traffic=experiment?.scenario?.flows||[experiment?.scenario?.flow].filter(Boolean);
 set('decision-freshness',continuous?'Controller journal refreshes every two seconds. Host reachability updates as each probe window is recorded.':traffic.length?`Traffic is sent during measurement stages: ${traffic.map(f=>`${f.source.toUpperCase()} → ${f.destination.toUpperCase()} ${f.offered_mbps} Mbps / ${f.seconds}s`).join(' · ')}. Other stages can be quiet.`:'Trial journals refresh every two seconds. Traffic animations show measured rates.');
 const panel=$('campaign-panel');panel.classList.toggle('hidden',!campaign);
 if(campaign){set('campaign-name',campaign.id);set('campaign-count',`${campaign.completed} / ${campaign.total} complete`);set('campaign-outcomes',`${campaign.successful} accepted · ${campaign.failed} failed`);$('campaign-progress').max=campaign.total||1;$('campaign-progress').value=campaign.completed;
  const body=$('campaign-jobs'),signature=JSON.stringify(campaign.jobs);if(body.dataset.signature!==signature){body.replaceChildren();for(const row of campaign.jobs){const tr=html('tr',row.id===experiment?.id?'current-job':'');for(const text of [row.index,row.scenario,row.mode==='jev'?'Jev + LLM':'LLM only',row.status,row.seconds===null?'—':fmt(row.seconds,1)+' s'])tr.append(html('td','',text));body.append(tr);}body.dataset.signature=signature;}
 }
}
function sparkline(parent,key){const values=history.map(f=>{const l=f.links?.find(x=>x.id===key);return l&&typeof l.a_mbps==='number'?Math.max(l.a_mbps,l.b_mbps||0):null;}).filter(v=>v!==null);if(values.length<2){parent.append(html('p','small','Rate history appears after multiple live counter samples.'));return;}
 const svg=node('svg',{viewBox:'0 0 320 75',class:'chart','aria-label':'Recent directional peak rate history'});svg.append(node('line',{x1:0,y1:65,x2:320,y2:65}));const max=Math.max(.01,...values);svg.append(node('polyline',{points:values.map((v,i)=>`${i*320/(values.length-1)},${65-v/max*53}`).join(' ')}));parent.append(svg,html('p','small',`Recent peak: ${fmt(max,3)} Mbps · ${values.length} samples`));}
function renderInspector(){
 const r=topology.routers[selected], n=frame?.nodes?.[selected]||{}, host=frame?.hosts?.[r.host]||{};
 const link=selectedLink?((frame?.links||[]).find(l=>l.id===selectedLink)||topology.links.find(l=>l.id===selectedLink)):null;
 set('inspector-title',selectedLink?selectedLink.toUpperCase():selectedHost?r.host:r.site);
 const status=selectedLink?(link?.state||'unknown'):selectedHost?hostState(host):(n.status||'unknown');
 pill('node-state',status.toUpperCase(),['running','up','reachable'].includes(status)?'good':['admin down','unreachable'].includes(status)?'bad':status==='degraded'?'warning':'muted');
 document.querySelectorAll('.tab').forEach(button=>{button.classList.toggle('active',button.dataset.tab===activeTab);button.setAttribute('aria-selected',button.dataset.tab===activeTab?'true':'false');});for(const tab of ['device','config','changes'])$(`${tab}-panel`).classList.toggle('hidden',tab!==activeTab);
 if(activeTab==='device'){const panel=$('device-panel');panel.replaceChildren();
  if(link){panel.append(detailList([['Endpoint A',`${link.a}:${link.a_interface}`],['Endpoint B',`${link.b}:${link.b_interface}`],['A → B',`${fmt(link.a_mbps,3)} Mbps`],['B → A',`${fmt(link.b_mbps,3)} Mbps`],['OSPF cost A',cost(link.a,link.a_interface)],['OSPF cost B',cost(link.b,link.b_interface)]]));if(typeof link.utilization==='number')panel.append(html('p','small',`Recorded peak utilization ${fmt(link.utilization*100)}%.`));panel.append(html('div','section-label','TRAFFIC HISTORY'));sparkline(panel,link.id);panel.append(html('p','small','Rate and OSPF adjacency are separate observations. A missing neighbor does not prove a physical link failure.'));}
  else if(selectedHost){
   const attachment=host.attachment==='admin_down'?'Administratively down':host.attachment==='enabled'?'Enabled':'Unknown';
   panel.append(detailList([['Host IP',r.host_ip],['Gateway',r.gateway],['Subnet',r.host_subnet],
                           ['Attached router',`${selected.toUpperCase()} ${r.host_interface}`],
                           ['Container',host.status],['Router port',attachment],
                           ['Host-pair checks',host.checked_pairs==null?'—':`${host.checked_pairs-host.failed_pairs} / ${host.checked_pairs} passed`]]));
   if(host.attachment_at)panel.append(html('p','small',`Router port sampled ${clock(host.attachment_at)} · ${age(host.attachment_at)}`));
   if(host.reachability_at)panel.append(html('p','small',`Controller check ${clock(host.reachability_at)} · ${age(host.reachability_at)}`));
  }
  else{panel.append(detailList([['Router',selected.toUpperCase()],['Management',r.mgmt_ip],['Memory',n.memory],['CPU',n.cpu],['FULL neighbors',`${n.full_neighbors?.length??'—'} / ${r.transit_interfaces.length}`],['Test host',r.host_ip]]));panel.append(html('div','section-label','OSPF NEIGHBORS'),html('pre','code-block',n.neighbors||'No neighbor observation available.'));panel.append(html('p','small',`Neighbors: ${clock(n.neighbors_at)} · ${age(n.neighbors_at)}. Resources: ${clock(n.metrics_at)}.`));panel.append(html('p','small',`Attached host ${r.host} uses ${r.host_subnet}, through ${r.host_interface}.`));}
 }
 if(activeTab==='config'){set('config-provenance',`${selected.toUpperCase()} · ${n.config_source||'running configuration'} · ${n.config_at?clock(n.config_at):frame?.source==='recorded'?'recorded checkpoint':'not sampled'}`);set('config-content',n.config||(n.config_error?'Configuration collection failed ('+n.config_error+'). '+(n.config_error_detail||'')+' Path mapping uses interface addresses independently.':'No configuration sample available. Live configs refresh every 30 seconds by default.'));}
 if(activeTab==='changes')renderChanges();
}
function renderChanges(){
 const items=experiment?.attempts||[], select=$('attempt-select'), previous=select.value;
 const signature=experiment?.id+'|'+items.map(a=>a.id).join('|');if(select.dataset.signature!==signature){select.replaceChildren();for(const a of items){const option=html('option','',a.id);option.value=a.id;select.append(option);}select.value=items.some(a=>a.id===previous)?previous:items.at(-1)?.id||'';select.dataset.signature=signature;}if($('follow-attempt').checked)select.value=items.at(-1)?.id||'';
 select.disabled=!items.length;const container=$('change-content');container.replaceChildren();
 if(selectedEvent){container.append(html('div','change-title',stageLabel(selectedEvent.stage)),html('p','small',`${selectedEvent.status||'complete'} · ${clock(selectedEvent.at)}`));if(selectedEvent.message)container.append(html('p','small',selectedEvent.message.replaceAll('_',' ')));}
 const attempt=items.find(a=>a.id===select.value)||items.at(-1);if(!attempt||!attempt.decision){container.append(html('div','empty',attempt?.decision_state?stageLabel(attempt.decision_state):attempt?'Decision in progress. Completed model calls will appear when the decision returns.':'No recorded model decision.'));return;}
 const screen=attempt.decision.screening||{};container.append(html('div','section-label','JEV / ROUTING'),html('p','small',screen.selected?`Selected ${screen.selected}${screen.selected_probability==null?'':` at ${fmt(screen.selected_probability*100,0)}%`}. Effective route ${screen.effective_route}.`:screen.bypassed?`Bypassed: ${screen.reason||'LLM-only arm'}.`:screen.error_type||'No screening result recorded.'));
 const p=attempt.decision?.proposal;container.append(html('div','change-title','Proposal'),html('p','change-copy',p?.diagnosis||(p?.changes?.length?'Typed configuration changes recorded.':'No candidate changes recorded.')));
 for(const evidence of p?.evidence||[])container.append(html('p','small','↳ '+evidence));
 for(const [name,diff] of Object.entries(attempt.diffs||{})){container.append(html('div','section-label',name.toUpperCase()+' · PROPOSED DIFF'));const pre=html('div','diff');for(const line of diff.split('\n'))pre.append(html('div','diff-line '+(line.startsWith('+++')||line.startsWith('---')||line.startsWith('@@')?'header':line.startsWith('+')?'add':line.startsWith('-')?'remove':''),line||' '));container.append(pre);}
 if(!Object.keys(attempt.diffs||{}).length&&p?.changes?.length)container.append(html('pre','code-block',JSON.stringify(p.changes,null,2)));
 const gate=attempt.verification, acceptance=attempt.acceptance;container.append(html('div','section-label','VERIFICATION / RUNTIME'));
 for(const [name,report] of [['Batfish',gate],['Runtime',acceptance]]){container.append(html('p','small',`${name}: ${report?(report.accepted?'accepted':'rejected'):'pending / not recorded'}`));for(const detail of report?.details||[])container.append(html('p','small',detail));}
 if(attempt.runtime_windows?.length){
  for(const [index,window] of attempt.runtime_windows.entries()){
   const label=['Healthy reference','Before deployment','After deployment'][index];
   const reachability=window.all_reachable?'All host pairs passed':'Host-pair checks failed';
   container.append(html('div','change-title',label),html('p','small',`${reachability} · mean RTT ${fmt(window.mean_rtt_ms,2)} ms`));
   const traffic=window.traffic, flows=traffic?.flows||(traffic?[traffic]:[]);
   for(const flow of flows)container.append(html('p','small',`${flow.source||'Source'} → ${flow.destination||'Destination'} · ${fmt(flow.received_mbps,2)} Mbps received · ${fmt(flow.loss_fraction*100,2)}% loss`));
  }
 }
 if(p?.expected_effect)container.append(html('p','small','Expected effect: '+p.expected_effect));
 const calls=attempt.decision?.calls||[];for(const call of calls)container.append(html('p','small',`${(call.stage||'llm').toUpperCase()} · ${call.model||call.error_type||'model unavailable'} · ${fmt(call.elapsed_ms,0)} ms · input tokens ${call.usage?.input_tokens??call.usage?.prompt_tokens??'unknown'} · output tokens ${call.usage?.output_tokens??call.usage?.completion_tokens??'unknown'}`));
}
function observerEvents(){
 if(!replay)return latest?.observer_events||[];
 const cutoff=Date.parse(frame?.sampled_at||'');
 return (replay.observer_events||[]).filter(e=>Date.parse(e.at)<=cutoff);
}
function renderTimeline(){
 const result=experiment?.result, activity=observerEvents();
 if(experiment?.continuous){const phase=experiment.continuous.phase?.stage;pill('trial-state',phase==='stopped'?'ENDED':phase==='blocked'?'BLOCKED':'CONTINUOUS',phase==='stopped'?'muted':'warning');set('trial-meta',`${experiment.id} · ${experiment.attempts.length} recorded decision attempt(s)`);}
 else if(!experiment){pill('trial-state',activity.length?'PROBE ACTIVITY':'NO TRIAL',activity.length?'good':'muted');set('trial-meta',activity.length?'Read-only observer activity. Path probes do not run the LLM repair loop.':'No trial journal found. Start a repair trial in your terminal, or probe a path to record observer activity.');}
 else{pill('trial-state',result?(result.success?'ACCEPTED':'FAILED / ENDED'):'IN PROGRESS',result?(result.success?'good':'warning'):'warning');set('trial-meta',`${experiment.id}${result?` · ${result.attempts??0} attempt(s) · acceptance ${result.fault_to_acceptance_seconds===null?'not achieved':fmt(result.fault_to_acceptance_seconds,1)+' s'}`:' · reading the experiment journal'}`);}
 const combined=[...(experiment?.events||[]),...activity].filter(e=>e?.stage).sort((a,b)=>Date.parse(a.at)-Date.parse(b.at));
 const compressed=[];
 for(const e of combined){
  const prior=e.id?compressed.findIndex(p=>p.id===e.id):-1;
  if(prior>=0)compressed[prior]=e;
  else if(!e.id&&compressed.at(-1)?.stage===e.stage&&compressed.at(-1)?.status==='running')compressed[compressed.length-1]=e;
  else compressed.push(e);
 }
 const signature=JSON.stringify(compressed.slice(-40));if($('timeline').dataset.signature===signature)return;$('timeline').dataset.signature=signature;
 const timeline=$('timeline'),follow=timeline.scrollLeft+timeline.clientWidth>=timeline.scrollWidth-40;timeline.replaceChildren();
 for(const event of compressed.slice(-40)){
  const button=html('button','event '+(event.status||'complete'));
  const title=event.stage==='path_probe'?`Probe ${event.source.toUpperCase()} → ${event.destination.toUpperCase()}`:stageLabel(event.stage);
  button.append(html('span','time',clock(event.at)),html('strong','',title),html('span','duration',event.status==='running'?'in progress…':`${fmt(event.elapsed_seconds,2)} s · ${event.status||'complete'}`));
  button.title=event.message||title;
  button.addEventListener('click',()=>{if(event.stage==='path_probe'){set('path-description',event.message||title);$('trace-details').open=true;}else{
   selectedEvent=event;activeTab='changes';
   if(event.attempt){$('follow-attempt').checked=false;}
   renderInspector();
   if(event.attempt&&experiment?.attempts.some(a=>a.id===event.attempt)){$('attempt-select').value=event.attempt;renderChanges();}
  }});
  timeline.append(button);
 }
 if(!compressed.length)timeline.append(html('div','empty','No observer activity or LLM experiment recorded yet.'));
 if(follow)timeline.scrollLeft=timeline.scrollWidth;
}
function renderPath(){
 const busy=probeState.status==='running'&&!replay, button=$('probe');
 button.disabled=busy||!!replay||['files','relay'].includes(latest?.mode);button.textContent=busy?'Probing…':'Probe path ↗';
 $('flow-source').disabled=busy;$('flow-destination').disabled=busy;$('load-replay').disabled=busy;
 if(busy){set('path-description',probeState.message);return;}
 if(probeState.status==='failed'&&!replay){set('path-description',probeState.message);$('path-description').classList.remove('active');return;}
 const p=observedPath;
 $('trace-details').classList.toggle('hidden',!p);
 if(!p){set('path-description',replay?'No observed path captured at this checkpoint.':'No path probe yet. Counters do not establish a packet’s route.');$('path-description').classList.remove('active');return;}
 const gaps=p.hops?.some(h=>h.nodes.length!==1);
 set('path-description',`${p.synthetic?'SYNTHETIC PATH':'ICMP PROBE'} · ${clock(p.sampled_at)} · ${p.nodes.join(' → ')}${p.complete?'':' · destination not confirmed'}${gaps?' · unmapped or missing hops; see traceroute details':''}`);
 $('path-description').classList.add('active');$('path-description').title=p.evidence||'';
 const missing=p.unmapped_addresses?.length?'\n\nUnmapped addresses: '+p.unmapped_addresses.join(', '):'';
 const errors=Object.keys(p.address_collection_errors||{}).length?'\nAddress collection unavailable on: '+Object.keys(p.address_collection_errors).join(', '):'';
 set('trace-output',(p.raw||p.evidence||'')+missing+errors);
}
function updateRecordings(){const select=$('recording'), current=select.value;const list=latest?.runs||[],signature=JSON.stringify(list.map(r=>[r.id,r.complete,r.kind]));if(select.dataset.signature===signature)return;select.replaceChildren(html('option','','Choose a recording…'));select.firstChild.value='';for(const r of list){const mode=r.mode==='jev_llm'?' · Jev + LLM':r.mode==='llm_only'?' · LLM only':'';const option=html('option','',`${r.kind==='telemetry'?'Observer':r.kind==='continuous'?'Continuous':'Trial'} · ${r.label}${mode}`);option.value=r.id;select.append(option);}if([...select.options].some(o=>o.value===current))select.value=current;select.dataset.signature=signature;}
function render(){if(!topology||!frame)return;renderMap();renderInspector();renderSummary();renderDecisionMonitor();renderTimeline();renderPath();updateRecordings();}
function currentState(state){latest=state;updateRecordings();if(paused||replay)return;frame=state.frame;experiment=state.experiment;if(experiment?.id!==lastExperimentId){selectedEvent=null;lastExperimentId=experiment?.id;$('follow-attempt').checked=true;}const p=state.path;if(!['running','failed'].includes(probeState.status)&&p&&p.source===$('flow-source').value&&p.destination===$('flow-destination').value)observedPath=p;
 if(frame.sampled_at!==lastHistoryTime){history.push(frame);history=history.slice(-90);lastHistoryTime=frame.sampled_at;}render();}
async function probe(){
 if(replay||probeState.status==='running')return;
 const source=$('flow-source').value,destination=$('flow-destination').value;
 if(source===destination){probeState={status:'failed',message:'Choose two different sites.'};renderPath();return;}
 observedPath=null;
 probeState={status:'running',message:`Probing ${topology.routers[source].site} → ${topology.routers[destination].site}. Collecting addresses and sending ICMP probes…`};
 renderMap();renderPath();
 try{
  observedPath=await api(`/api/path?source=${encodeURIComponent(source)}&destination=${encodeURIComponent(destination)}`);
  probeState={status:'complete',message:''};
 }catch(error){probeState={status:'failed',message:error.message};}
 try{const update=await api('/api/state');latest=update;experiment=update.experiment;}catch{/* Retain the probe result even if a subsequent refresh fails. */}
 renderMap();renderPath();renderTimeline();
}
function replayFrame(index){if(!replay?.frames.length)return;replayIndex=Math.max(0,Math.min(replay.frames.length-1,index));const f=replay.frames[replayIndex];frame=f.telemetry||f;
 if(replay.kind==='experiment'){const cutoff=f.sort_time*1000;experiment={...replay,result:replay.result_at*1000<=cutoff?replay.result:null,events:replay.events.filter(e=>Date.parse(e.at)<=cutoff),attempts:replay.attempts.filter(a=>(a.decision_at??a.started_at)*1000<=cutoff).map(a=>({...a,verification:a.verification_at*1000<=cutoff?a.verification:null,acceptance:a.acceptance_at*1000<=cutoff?a.acceptance:null}))};}
 else if(replay.kind==='continuous'){
  const cutoff=f.sort_time, appears=at=>typeof at==='number'&&at<=cutoff;
  const attempts=replay.attempts.filter(a=>appears(a.started_at)).map(a=>({
   ...a,decision:appears(a.decision_at)?{...a.decision,proposal:appears(a.proposal_at)?a.decision.proposal:null}:null,
   verification:appears(a.verification_at)?a.verification:null,
   acceptance:appears(a.acceptance_at)?a.acceptance:null,
   outcome:appears(a.outcome_at)?a.outcome:null,
   decision_state:appears(a.decision_state_at)?a.decision_state:null,
   runtime_windows:appears(a.runtime_at)?a.runtime_windows:null
  }));
  const events=replay.events.filter(e=>Date.parse(e.at)<=cutoff*1000);
  if(selectedEvent&&!events.includes(selectedEvent))selectedEvent=null;
  experiment={...replay,events,attempts,continuous:{...replay.continuous,phase:f.phase}};
 }
 else experiment=f.experiment||null;
 observedPath=f.path?.source===$('flow-source').value&&f.path?.destination===$('flow-destination').value?f.path:null;if(!observedPath){const paths=(replay.paths||[]).filter(p=>p.source===$('flow-source').value&&p.destination===$('flow-destination').value&&Date.parse(p.sampled_at)<=Date.parse(frame.sampled_at));observedPath=paths.at(-1)||null;}
 history=replay.frames.slice(Math.max(0,replayIndex-89),replayIndex+1).map(f=>f.telemetry||f);$('scrubber').value=replayIndex;set('replay-position',`${replayIndex+1} / ${replay.frames.length}`);render();}
function stopPlayer(){if(player)clearInterval(player);player=null;$('play').textContent='▶';}
async function loadReplay(){const id=$('recording').value;if(!id)return;stopPlayer();try{const data=await api('/api/replay?run='+encodeURIComponent(id));if(!data.frames.length)throw new Error('This recording has no complete observation checkpoints yet.');replay=data;$('scrubber').disabled=false;$('scrubber').max=data.frames.length-1;$('play').disabled=false;$('return-live').disabled=false;$('pause').disabled=true;observedPath=null;replayFrame(0);}catch(error){set('path-description',error.message);}}
function returnLive(){stopPlayer();replay=null;paused=false;history=[];observedPath=null;lastHistoryTime=null;$('pause').disabled=false;$('pause').textContent='Pause view';$('return-live').disabled=true;$('play').disabled=true;$('scrubber').disabled=true;set('replay-position','Current view');if(latest)currentState(latest);}
function changeZoom(delta){zoom=Math.min(1.8,Math.max(.6,zoom+delta));$('viewport').setAttribute('transform',`translate(${560*(1-zoom)},${325*(1-zoom)}) scale(${zoom})`);}
async function init(){try{topology=await api('/api/topology');buildMap();for(const id of ['flow-source','flow-destination'])for(const [name,r] of Object.entries(topology.routers)){const option=html('option','',r.site);option.value=name;$(id).append(option);}$('flow-source').value='sea';$('flow-destination').value='sva';
 document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>{activeTab=b.dataset.tab;renderInspector();}));$('attempt-select').addEventListener('change',()=>{$('follow-attempt').checked=false;renderChanges();});$('follow-attempt').addEventListener('change',renderChanges);$('inspect-decision').addEventListener('click',()=>{activeTab='changes';renderInspector();});$('probe').addEventListener('click',probe);
 for(const id of ['flow-source','flow-destination'])$(id).addEventListener('change',()=>{observedPath=null;probeState={status:'idle',message:''};renderMap();renderPath();if(replay)replayFrame(replayIndex);});
 $('animate').addEventListener('change',e=>$('topology').classList.toggle('no-animation',!e.target.checked));$('pause').addEventListener('click',()=>{paused=!paused;$('pause').textContent=paused?'Resume view':'Pause view';if(!paused&&latest)currentState(latest);else renderSummary();});
 $('zoom-in').addEventListener('click',()=>changeZoom(.15));$('zoom-out').addEventListener('click',()=>changeZoom(-.15));$('fit').addEventListener('click',()=>{zoom=1;changeZoom(0);});$('load-replay').addEventListener('click',loadReplay);$('return-live').addEventListener('click',returnLive);$('scrubber').addEventListener('input',e=>{stopPlayer();replayFrame(Number(e.target.value));});
 $('play').addEventListener('click',()=>{if(player){stopPlayer();return;}if(replayIndex>=replay.frames.length-1)replayFrame(0);$('play').textContent='Ⅱ';player=setInterval(()=>{if(replayIndex>=replay.frames.length-1){stopPlayer();return;}replayFrame(replayIndex+1);},1000);});
 currentState(await api('/api/state'));stream=new EventSource('/api/events');stream.onmessage=event=>{try{currentState(JSON.parse(event.data));}catch(error){console.error('Observation update failed',error);}};stream.onerror=()=>{if(!replay&&!paused){pill('connection','RECONNECTING','warning');$('notice').textContent='Connection interrupted. Displayed observations may be stale; reconnecting…';$('notice').className='notice warning';}};
 }catch(error){$('notice').textContent=error.message;$('notice').className='notice error';}}
init();
