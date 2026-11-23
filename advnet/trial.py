"""Bounded LLM-only observe/verify/deploy/measure/rollback experiments."""
import hashlib
import json
import math
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .contracts import Feedback, Proposal
from .catalog import catalog_data, catalog_hash
from .eos import candidate_configs
from .lab import Deadline
from .metrics import inference_metrics
from .scenarios import routing_evaluation, ConcurrentFlow, MAX_CONCURRENT_FLOWS


def dump(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False)+'\n')


def snapshot(directory, configs, manifest):
    # Diamond lists its two hosts separately; Abilene keeps one on each router.
    hosts = (manifest['hosts'] if 'hosts' in manifest else
             {router['host']: router for router in manifest['routers'].values()})
    directory = Path(directory)
    (directory/'configs').mkdir(parents=True,exist_ok=True)
    (directory/'hosts').mkdir(exist_ok=True)
    for node,text in configs.items():
        (directory/'configs'/f'{node}.cfg').write_text(f'! device: {node} (cEOSLab, EOS-4.36.3M)\n'+text)
    for name, host in hosts.items():
        dump(directory/'hosts'/f'{name}.json',{
            'hostname':name, 'hostInterfaces':{'eth1':{
                'name':'eth1','prefix':host['host_ip']+'/24','gateway':host['gateway']}}})
    dump(directory/'intent.json',manifest)
    return directory


class ProcessVerifier:
    def __init__(self, argv):
        if not argv:
            raise ValueError('An explicit verification command is required')
        self.argv = argv

    def verify(self, directory, deadline):
        # A separate process imposes an overall timeout on SDKs and model building.
        p = subprocess.run([*self.argv,str(directory.resolve())],capture_output=True,text=True,
                           timeout=deadline.remaining())
        (directory.parent/'verifier-stderr.txt').write_text(p.stderr)
        if p.returncode:
            return {'accepted':False,'details':['Verifier failed; see verifier-stderr.txt.'], 'process_error':True}
        value = json.loads(p.stdout)
        if type(value.get('accepted')) is not bool or not isinstance(value.get('details'),list) or not value['details']:
            raise ValueError('Verifier must return accepted:boolean and a nonempty details array')
        if any(not isinstance(d,str) for d in value['details']):
            raise ValueError('Verifier details must be strings')
        return value


class DecisionProcess:
    def __init__(self, provider, model, directory, base_url=None, max_tokens=4096, *,
                 system_one='off',sysone_model=None,threshold=0.9,jev_timeout=10,catalog=None,force_escalation=False):
        if system_one not in ('off','jev'):
            raise ValueError('Unknown System One mode')
        self.provider,self.model,self.directory = provider,model,Path(directory)
        self.base_url,self.max_tokens=base_url,max_tokens
        self.system_one_enabled=system_one=='jev'
        if force_escalation and not self.system_one_enabled:
            raise ValueError('Forced escalation requires Jev')
        self.force_escalation=force_escalation
        if self.system_one_enabled and not sysone_model:
            raise ValueError('Jev mode requires a separate System One model ID')
        self.sysone_model,self.threshold,self.jev_timeout=sysone_model,threshold,jev_timeout
        self.catalog=catalog or {}
        self.settings={'system_one':system_one,'sysone_model':sysone_model if self.system_one_enabled else None,
                       'forced_escalation':force_escalation,
                       'jev_threshold':threshold,'jev_timeout_seconds':jev_timeout,
                       'llm_provider':provider,'llm_model':model,'max_tokens':max_tokens,
                       'catalog_sha256':catalog_hash(self.catalog),'catalog_size':len(self.catalog)}

    def decide(self, observation, feedback, attempt, deadline):
        root = self.directory/f'attempt-{attempt}'
        root.mkdir(exist_ok=True)
        (root/'observation.json').write_text(observation.model_dump_json(indent=2))
        dump(root/'feedback.json',[f.model_dump(mode='json') for f in feedback])
        catalog = self.directory/'catalog.json'
        dump(catalog,catalog_data(self.catalog))
        argv = [sys.executable,'-m','advnet',str(root/'observation.json'),'--mode','live',
                '--catalog',str(catalog),'--llm-provider',self.provider,'--llm-model',self.model,
                '--attempt',str(attempt),'--feedback',str(root/'feedback.json'),
                '--timeout',str(min(60,deadline.remaining())), '--max-tokens',str(self.max_tokens),
                '--log',str(root/'model-calls.jsonl')]
        if self.system_one_enabled:
            argv += ['--sysone-model',self.sysone_model,'--threshold',str(self.threshold),'--jev-timeout',str(self.jev_timeout)]
            if self.force_escalation:
                argv += ['--force-escalation']
        else:
            argv += ['--without-jev']
        if self.base_url:
            argv += ['--base-url',self.base_url]
        result = subprocess.run(argv,capture_output=True,text=True,timeout=deadline.remaining())
        (root/'decision-stderr.txt').write_text(result.stderr)
        value = json.loads(result.stdout)
        if result.returncode and value.get('status')!='decision_error':
            raise RuntimeError('Decision process failed')
        return value


def acceptable(current, healthy, impaired, objective):
    details=[]
    if not current['all_reachable']:
        details.append('Required host connectivity or zero-loss ping checks failed.')
    if not current['ospf_healthy']:
        details.append('Required OSPF adjacencies are not all FULL.')
    if current['mean_rtt_ms'] is None or current['mean_rtt_ms'] > healthy['mean_rtt_ms']*1.25+5:
        details.append('Idle ping latency exceeds healthy-baseline tolerance (25% plus 5 ms).')
    if healthy['traffic']:
        t,h=current['traffic'],healthy['traffic']
        if not t or t['received_mbps'] < h['received_mbps']*0.9:
            details.append('Delivered throughput is below 90% of the healthy baseline.')
        if not t or t['loss_fraction'] > h['loss_fraction']+0.02:
            details.append('UDP loss exceeds the healthy baseline by more than two percentage points.')
        if objective=='optimization':
            if not t or t['max_link_utilization'] >= impaired['traffic']['max_link_utilization']*0.95:
                details.append('Maximum link utilization did not improve by at least 5%.')
    return {'accepted':not details,'details':details or ['All scoped runtime acceptance checks passed.']}


def _batch_records(measurement):
    """Validate new batch measurements before using existing numeric guardrails."""
    if not isinstance(measurement,dict):
        raise ValueError('Concurrent traffic measurement is missing')
    traffic=measurement.get('traffic')
    if not isinstance(traffic,dict) or traffic.get('mode')!='concurrent_udp':
        raise ValueError('Concurrent traffic measurement is missing')
    if any(type(measurement.get(key)) is not bool for key in ('all_reachable','ospf_healthy')):
        raise ValueError('Concurrent reachability/OSPF measurements are missing')
    def valid(value):
        if type(value) not in (int,float):
            return False
        try:
            return value>=0 and math.isfinite(value)
        except OverflowError:
            return False
    window=traffic.get('overlap_window')
    if (traffic.get('concurrency_verified') is not True or not isinstance(window,dict)
            or not valid(window.get('start_seconds')) or not valid(window.get('end_seconds'))
            or window['end_seconds']<=window['start_seconds']):
        raise ValueError('Concurrent sender overlap was not verified')
    if not valid(traffic.get('max_link_utilization')) or not valid(measurement.get('mean_rtt_ms')):
        raise ValueError('Concurrent utilization or idle RTT is unavailable')
    flows=traffic.get('flows')
    if not isinstance(flows,list) or not 2<=len(flows)<=MAX_CONCURRENT_FLOWS:
        raise ValueError('Concurrent flow records are incomplete')
    records={}
    for flow in flows:
        profile=ConcurrentFlow.model_validate({key:flow[key] for key in
            ('source','destination','destination_port','offered_mbps','seconds')})
        key=(profile.source,profile.destination,profile.destination_port)
        if key in records:
            raise ValueError('Duplicate concurrent flow identity')
        if (not valid(flow.get('received_mbps')) or not valid(flow.get('loss_fraction'))
                or flow['loss_fraction']>1):
            raise ValueError('Concurrent receiver throughput/loss is unavailable')
        records[key]=flow
    return records


def acceptable_concurrent(current,healthy,impaired,objective):
    """Apply unchanged single-flow thresholds to each matched batch flow."""
    try:
        records=[_batch_records(stage) for stage in (current,healthy,impaired)]
    except (ValueError,KeyError,TypeError) as error:
        return {'accepted':False,'details':['Concurrent runtime measurement rejected: '+str(error)]}
    if any(set(record)!=set(records[1]) for record in records):
        return {'accepted':False,'details':['Concurrent flow identities differ across trial stages.']}
    failures=[]
    for key in sorted(records[1]):
        rows=[record[key] for record in records]
        if any((row['offered_mbps'],row['seconds'])!=(rows[1]['offered_mbps'],rows[1]['seconds']) for row in rows):
            failures.append(f'{key}: configured offered load/duration changed.')
            continue
        scoped=[{**stage,'traffic':{**row,'max_link_utilization':stage['traffic']['max_link_utilization']}}
                for stage,row in zip((current,healthy,impaired),rows)]
        check=acceptable(*scoped,objective)
        if not check['accepted']:
            failures.extend(f'{key}: {detail}' for detail in check['details'])
    return {'accepted':not failures,'details':failures or [
        'All flows passed the existing receiver-preservation and shared-utilization thresholds.']}


class Trial:
    def __init__(self, lab, decision, verifier, directory, max_attempts=5, seconds=900, *, baseline_configs=None):
        if not 1<=max_attempts<=5:
            raise ValueError('max_attempts must be 1..5')
        self.lab,self.decision,self.verifier,self.directory=lab,decision,verifier,Path(directory)
        self.max_attempts,self.seconds=max_attempts,seconds
        self.baseline_configs=baseline_configs
        self.directory.mkdir(parents=True,exist_ok=False)
        self.events=[]

    def stage(self,name,call):
        start=time.monotonic()
        # Begin records make long-running model/verification stages visible live.
        with (self.directory/'events.jsonl').open('a') as stream:
            stream.write(json.dumps({'stage':name,'at':datetime.now(timezone.utc).isoformat(),
                                     'status':'running','elapsed_seconds':0})+'\n')
        try:
            value=call()
        except BaseException as error:
            self.event(name,start,error_type=type(error).__name__)
            raise
        self.event(name,start)
        return value

    def event(self,name,start,**fields):
        event={'stage':name,'at':datetime.now(timezone.utc).isoformat(),
               'elapsed_seconds':time.monotonic()-start,
               'status':'failed' if 'error_type' in fields else 'complete',**fields}
        self.events.append(event)
        with (self.directory/'events.jsonl').open('a') as stream:
            stream.write(json.dumps(event)+'\n')

    def run(self,scenario):
        if scenario.get('flow') is not None and scenario.get('flows') is not None:
            raise ValueError('Specify either flow or flows, not both')
        identifier='trial-'+uuid4().hex
        system_one_enabled=getattr(self.decision,'system_one_enabled',False)
        result={'trial_id':identifier,'mode':'jev_llm' if system_one_enabled else 'llm_only',
                'system_one_enabled':system_one_enabled,'decision_settings':getattr(self.decision,'settings',{}),
                'evaluation':scenario.get('evaluation',{}),
                'baseline_reset_requested':self.baseline_configs is not None,'baseline_reset_completed':False,
                'decisions':[],'success':False,
                'scenario':scenario['name'],'attempts':0,'fault_to_acceptance_seconds':None,
                'max_attempts':self.max_attempts,'time_cap_seconds':self.seconds,
                'cost_usd':None,'model_calls':[],'cleanup_succeeded':False}
        dump(self.directory/'manifest.json',self.lab.manifest)
        dump(self.directory/'private-scenario.json',scenario)
        setup=Deadline(600)
        healthy_configs=None
        started=None
        capacities_changed=False
        feedback=[]
        seen=set()
        def measure(limit):
            return (self.lab.measure(limit,flows=scenario['flows']) if scenario.get('flows')
                    else self.lab.measure(limit,scenario.get('flow')))
        try:
            if self.baseline_configs is not None:
                dump(self.directory/'baseline-source-configs.json',self.baseline_configs)
                result['baseline_source_sha256']=hashlib.sha256(json.dumps(self.baseline_configs,sort_keys=True).encode()).hexdigest()
                self.stage('restore_baseline',lambda:self.lab.replace(self.baseline_configs,self.directory/'baseline-reset',setup))
                self.stage('baseline_capacities',lambda:self.lab.shape({},setup))
                result['baseline_reset_completed']=True
            healthy_configs=self.stage('checkpoint_healthy',lambda:self.lab.configs(setup))
            dump(self.directory/'healthy-configs.json',healthy_configs)
            if scenario.get('flow') or scenario.get('flows'):
                capacities_changed=True
                self.stage('shape_healthy',lambda:self.lab.shape({},setup))
            self.stage('initial_convergence',lambda:self.lab.wait_convergence(setup))
            healthy=self.stage('measure_healthy',lambda:measure(setup))
            dump(self.directory/'healthy-measurements.json',healthy)
            if not healthy['all_reachable'] or not healthy['ospf_healthy']:
                raise ValueError('Healthy preflight failed; no fault injected')
            if scenario.get('flows'):
                self.stage('validate_healthy_traffic',lambda:_batch_records(healthy))
            gate_dir=snapshot(self.directory/'healthy-snapshot',healthy_configs,self.lab.manifest)
            gate=self.stage('verify_healthy',lambda:self.verifier.verify(gate_dir,setup))
            dump(self.directory/'healthy-verification.json',gate)
            if not gate['accepted']:
                raise ValueError('Healthy snapshot did not pass verifier; no fault injected')
            deadline=Deadline(self.seconds)
            started=time.monotonic()
            if scenario.get('fault'):
                fault=Proposal.model_validate(scenario['fault'])
                # Apply only typed changes to known devices/interfaces.
                observation=self.lab.observe(identifier,healthy,deadline,scenario['objective_text'])
                fault.check_scope(observation)
                injected=candidate_configs(healthy_configs,fault)
                self.stage('inject_fault',lambda:self.lab.replace({n:injected[n] for n in {c.device for c in fault.changes}},self.directory/'injection',deadline))
            if scenario.get('capacities_mbps'):
                capacities_changed=True
                self.stage('inject_capacity_change',lambda:self.lab.shape(scenario['capacities_mbps'],deadline))
            self.stage('fault_convergence',lambda:self.lab.wait_convergence(deadline))
            impaired=self.stage('measure_impaired',lambda:measure(deadline))
            dump(self.directory/'impaired-measurements.json',impaired)
            if scenario.get('flows'):
                self.stage('validate_impaired_traffic',lambda:_batch_records(impaired))
            if scenario['objective']=='repair' and impaired['all_reachable'] and impaired['ospf_healthy']:
                raise ValueError('Scenario failed to produce the expected impairment')
            if scenario['objective']=='optimization' and (not impaired['traffic'] or impaired['traffic']['max_link_utilization']<=healthy['traffic']['max_link_utilization']*1.05):
                raise ValueError('Scenario failed to produce measurable congestion')
            # Fault injection checkpoint is private harness data, never model input.
            impaired_configs=self.lab.configs(deadline)
            dump(self.directory/'impaired-configs.json',impaired_configs)
            for attempt in range(1,self.max_attempts+1):
                deadline.remaining()
                result['attempts']=attempt
                root=self.directory/f'attempt-{attempt}'
                root.mkdir(exist_ok=True)
                observation=self.stage('observe',lambda:self.lab.observe(identifier,impaired if attempt==1 else last_measurement,deadline,scenario['objective_text']))
                response=self.stage('system_one_decision' if system_one_enabled else 'llm_decision',
                                    lambda:self.decision.decide(observation,feedback,attempt,deadline))
                dump(root/'decision.json',response)
                result['model_calls'].extend(response.get('calls',[]))
                result['decisions'].append({'attempt':attempt,'status':response.get('status'),
                    'screening':response.get('screening',{}),'selected_action':response.get('selected_action')})
                decision_id=response.get('decision_id',identifier+f'-{attempt}')
                if response.get('status')=='decision_error' or not response.get('proposal'):
                    feedback.append(Feedback(attempt=attempt,candidate_id=decision_id,stage='decision',accepted=False,details=['Decision did not return a valid candidate.']))
                    last_measurement=measure(deadline)
                    continue
                proposal=Proposal.model_validate(response['proposal'])
                proposal.check_scope(observation)
                if proposal.outcome!='candidate':
                    last_measurement=measure(deadline)
                    feedback.append(Feedback(attempt=attempt,candidate_id=decision_id,stage='decision',accepted=False,details=['The injected impairment still requires repair; requested observations are refreshed.']))
                    continue
                key=json.dumps(sorted([c.model_dump() for c in proposal.changes],key=lambda c:(c['device'],c['interface'],c['action'])),sort_keys=True)
                fingerprint=hashlib.sha256(key.encode()).hexdigest()
                if fingerprint in seen:
                    feedback.append(Feedback(attempt=attempt,candidate_id=decision_id,stage='decision',accepted=False,details=['This exact patch was already rejected; propose a different repair.']))
                    last_measurement=measure(deadline)
                    continue
                seen.add(fingerprint)
                checkpoint=self.lab.configs(deadline)
                candidate=candidate_configs(checkpoint,proposal)
                dump(root/'checkpoint.json',checkpoint)
                dump(root/'candidate-sha256.json',{n:hashlib.sha256(text.encode()).hexdigest() for n,text in candidate.items()})
                candidate_dir=snapshot(root/'snapshot',candidate,self.lab.manifest)
                verification=self.stage('verify_candidate',lambda:self.verifier.verify(candidate_dir,deadline))
                dump(root/'verification.json',verification)
                if not verification['accepted']:
                    feedback.append(Feedback(attempt=attempt,candidate_id=decision_id,stage='verification',accepted=False,details=verification['details']))
                    last_measurement=measure(deadline)
                    continue
                changed={c.device for c in proposal.changes}
                if self.lab.configs(deadline)!=checkpoint:
                    raise RuntimeError('Network configuration changed during candidate verification')
                self.stage('deploy',lambda:self.lab.replace({n:candidate[n] for n in changed},root/'deploy',deadline))
                self.stage('post_deploy_convergence',lambda:self.lab.wait_convergence(deadline))
                current=self.stage('measure_candidate',lambda:measure(deadline))
                dump(root/'measurements.json',current)
                acceptance=(acceptable_concurrent(current,healthy,impaired,scenario['objective']) if scenario.get('flows')
                            else acceptable(current,healthy,impaired,scenario['objective']))
                deadline.remaining()
                dump(root/'acceptance.json',acceptance)
                if acceptance['accepted']:
                    result['success']=True
                    result['fault_to_acceptance_seconds']=time.monotonic()-started
                    break
                # Recovery is attempted even after the trial's deadline expires.
                self.stage('rollback_candidate',lambda:self.lab.replace(checkpoint,root/'rollback',Deadline(180)))
                rollback=self.lab.configs(Deadline(60))
                if rollback!=checkpoint:
                    raise RuntimeError('Rollback configuration readback did not match checkpoint')
                feedback.append(Feedback(attempt=attempt,candidate_id=decision_id,stage='runtime',accepted=False,
                                         details=acceptance['details'],measurements=current,rollback_succeeded=True))
                self.lab.wait_convergence(deadline)
                last_measurement=measure(deadline)
        except BaseException as error:
            result['error_type']=type(error).__name__
            # Keep a useful failure reason; credentials are never passed as command arguments.
            result['error']=str(error)[:1000] if isinstance(error,(ValueError,TimeoutError)) else 'See stage logs and device state.'
        finally:
            try:
                if self.baseline_configs is not None and not result['baseline_reset_completed']:
                    raise RuntimeError('Baseline restoration did not complete; stop before another trial')
                if healthy_configs and not result['success'] and started is not None:
                    self.stage('harness_reset_not_a_repair',lambda:self.lab.replace(healthy_configs,self.directory/'reset',Deadline(180)))
                    if self.lab.configs(Deadline(60))!=healthy_configs:
                        raise RuntimeError('Final reset readback mismatch')
                if capacities_changed:
                    self.stage('restore_capacities',lambda:self.lab.shape({},Deadline(180)))
                if healthy_configs and not result['success'] and started is not None:
                    recovery_deadline=Deadline(180)
                    self.stage('reset_convergence',lambda:self.lab.wait_convergence(recovery_deadline))
                    recovery=self.stage('verify_reset_runtime',lambda:self.lab.measure(recovery_deadline))
                    dump(self.directory/'reset-measurements.json',recovery)
                    if not recovery['all_reachable'] or not recovery['ospf_healthy']:
                        raise RuntimeError('Harness reset did not restore healthy runtime behavior')
                result['cleanup_succeeded']=True
            except BaseException as error:
                result['cleanup_error_type']=type(error).__name__
                result['success']=False
                result['fault_to_acceptance_seconds']=None
            result['events']=self.events
            result['inference_metrics']=inference_metrics(result['model_calls'])
            result['routing_evaluation']=routing_evaluation(result)
            result['cost_usd']=result['inference_metrics']['cost_usd'] if not result.get('error_type') else None
            if started is not None:
                result['trial_elapsed_seconds']=time.monotonic()-started
            try:
                from .performance import compare_trial_artifacts
                report=compare_trial_artifacts(self.directory,result=result)
                report['code_sha256']={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                       for name in ('trial.py','lab.py','concurrent_traffic.py','scenarios.py','performance.py')}
                sources=[self.directory/name for name in ('manifest.json','private-scenario.json',
                         'healthy-measurements.json','impaired-measurements.json')]
                sources.extend(path for path in self.directory.glob('attempt-*/*.json')
                               if path.name in ('measurements.json','acceptance.json'))
                report['source_files']={str(path.resolve()):hashlib.sha256(path.read_bytes()).hexdigest()
                                        for path in sorted(sources) if path.is_file()}
                dump(self.directory/'performance-comparison.json',report)
                result['performance_report']={'status':'written','path':'performance-comparison.json'}
            except Exception as error:
                # A reporting failure must never change repair acceptance.
                result['performance_report']={'status':'failed','error_type':type(error).__name__,
                                               'error':str(error)[:1000]}
            dump(self.directory/'result.json',result)
        return result
