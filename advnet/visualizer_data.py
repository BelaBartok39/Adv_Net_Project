"""Read experiment artifacts through a fixed allowlist, plus telemetry recording/replay."""
import difflib
from datetime import datetime
import json
import re
from pathlib import Path

MAX_FILE=12*1024*1024
MAX_FRAMES=5000


def read_json(path):
    try:
        if path.stat().st_size>MAX_FILE:
            return None
        return json.loads(path.read_text())
    except (OSError,ValueError):
        return None


def read_lines(path,limit=MAX_FRAMES):
    output=[]
    try:
        # Tail bounded bytes so long observer sessions do not grow response memory.
        with path.open('rb') as stream:
            size=path.stat().st_size
            stream.seek(max(0,size-MAX_FILE))
            if size>MAX_FILE:
                stream.readline()
            for line in stream:
                try:
                    output.append(json.loads(line))
                except ValueError:
                    continue
                if len(output)>limit:
                    output=output[-limit:]
    except OSError:
        pass
    return output


def _time(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0


def _decision(row):
    route = row.get('route')
    calls = [dict(call) for call in row.get('calls') or []]
    metrics = row.get('metrics') or {}
    for call in calls:
        stage = call.get('stage')
        if metrics.get(f'{stage}_calls') == 1:
            call['usage'] = {key: metrics.get(f'{stage}_{key}')
                             for key in ('input_tokens', 'output_tokens', 'cost_usd')}
    return {'status': row.get('status'), 'error_type': row.get('error_type'), 'calls': calls,
            'screening': {'selected': row.get('selected_action') or route,
                          'effective_route': route, 'selected_probability': row.get('selected_probability')},
            'threshold': row.get('threshold')}


def continuous_experiment(name, rows, *, replay=False):
    """Adapt the controller's phase/event journal to the dashboard's trial view."""
    attempts, events, frames = {}, [], []
    last_hosts = last_phase = None
    observe_only = False
    mode = None
    peak = None
    phase_events = {'decision', 'verification', 'verified', 'deployment', 'runtime_validation',
                    'runtime_result', 'rollback', 'recovery', 'blocked', 'stopped'}
    for row in rows:
        if not isinstance(row, dict) or not _time(row.get('at')):
            continue
        at, event, phase = row['at'], row.get('event'), row.get('phase')
        mode = row.get('mode') or mode
        attempt_id = row.get('intervention')
        item = None
        if isinstance(attempt_id, str) and event in ('transition', 'decision_result', 'proposal',
                                                     'proposal_rejected', 'inference_discarded', 'worker_error'):
            item = attempts.setdefault(attempt_id, {'id': attempt_id, 'started_at': _time(at), 'diffs': {}})
        stage, status, message = None, 'complete', None
        if event == 'live_session_started':
            observe_only = row.get('observe_only') is True
            stage = 'continuous_start'
        elif event == 'observation':
            runtime = row.get('runtime') or {}
            health = (row.get('measurement') or {}).get('reachability') or {}
            failed = runtime.get('failed_pairs')
            if isinstance(failed, list):
                measured_at = health.get('observed_at') or runtime.get('end') or at
                ports = {link['link_id']: link['throughput_mbps']
                         for link in (row.get('measurement') or {}).get('links', [])
                         if isinstance(link.get('throughput_mbps'), (int, float))}
                if not runtime.get('error'):
                    ports.update({key: utilization * capacity for key, utilization in
                                  runtime.get('link_utilization', {}).items()
                                  if (capacity := runtime.get('link_capacities_mbps', {}).get(key)) and key not in ports})
                if ports:
                    peak = max([peak or 0, *ports.values()])
                check = {'observed_at': measured_at, 'all_reachable': runtime.get('all_reachable'),
                         'mean_rtt_ms': runtime.get('mean_rtt_ms'), 'failed_pairs': failed,
                         'ospf_healthy': runtime.get('ospf_healthy'),
                         'mismatched_ospf': runtime.get('mismatched_ospf'),
                         'performance': runtime.get('performance'),
                         'traffic': {'link_utilization': runtime.get('link_utilization', {})},
                         'port_rates': ports}
                checkpoint = {'kind': 'measurement', 'label': 'Controller observation', 'phase': last_phase,
                              'sort_time': _time(at), 'sampled_at': measured_at, 'measurement': check}
                if replay or not frames:
                    frames.append(checkpoint)
                else:
                    frames[-1] = checkpoint
                affected = {tuple(pair) for pair in failed}
                if affected and affected != last_hosts:
                    stage, status = 'reachability_failed', 'failed'
                    message = f'{len(affected)} directed host pairs failed'
                elif not affected and last_hosts:
                    stage, message = 'reachability_restored', 'All required host pairs passed'
                last_hosts = affected
        elif event == 'decision_result' and item is not None:
            item.update(decision=_decision(row), decision_at=_time(at))
            stage = 'decision_result'
            if row.get('status') == 'decision_error':
                status, message = 'failed', row.get('error_type')
        elif event == 'proposal' and item is not None:
            item.setdefault('decision', {})['proposal'] = {'outcome': 'candidate', 'changes': row.get('changes', [])}
            item['proposal_at'] = _time(at)
            stage = 'proposal'
        elif event == 'policy':
            decision = row.get('decision') or {}
            reasons = decision.get('reasons', [])
            if decision.get('stage') == 'proposal' and reasons and 'proposal_eligible' not in reasons:
                # Earlier journals did not attach an intervention to the policy assessment.
                attempt_id = attempt_id or next(reversed(attempts), None)
                item = attempts.get(attempt_id)
                stage, status, message = 'policy_rejected', 'failed', ', '.join(reasons)
                if item is not None:
                    item.update(decision_state=stage, decision_state_at=_time(at))
        elif event == 'transition' and isinstance(phase, str):
            outcome = row.get('outcome')
            last_phase = {'stage': phase, 'at': at, 'status': 'running'
                          if phase in ('decision', 'verification', 'deployment', 'runtime_validation', 'rollback')
                          else 'complete'}
            if phase in phase_events or outcome:
                stage, status = phase, last_phase['status']
            if phase == 'verified' and item is not None:
                item.update(verification={'accepted': True, 'details': ['Batfish approved the candidate.']},
                            verification_at=_time(at))
            if phase == 'runtime_result' and item is not None:
                item.update(runtime_windows=row.get('windows'), runtime_at=_time(at))
                if row.get('accepted') is False:
                    status, message = 'failed', 'Live acceptance checks failed'
                    item.update(acceptance={'accepted': False, 'details': row.get('details') or [message]},
                                acceptance_at=_time(at))
            if phase == 'rollback':
                message = row.get('reason')
            if outcome:
                stage, status, message = 'continuous_' + outcome, 'complete' if outcome == 'success' else 'failed', row.get('evidence')
                if item is not None:
                    if outcome == 'rejected' and message in ('verification_rejected', 'baseline_verification_rejected'):
                        item.update(verification={'accepted': False, 'details': [message.replace('_', ' ')]},
                                    verification_at=_time(at))
                    if outcome in ('success', 'rolled_back', 'rollback_unconfirmed'):
                        report = item.get('acceptance', {'details': []})
                        item.update(acceptance={'accepted': outcome == 'success',
                                                'details': [*report['details'], (message or outcome).replace('_', ' ')]},
                                    acceptance_at=_time(at))
                    item['outcome'] = outcome
                    item['outcome_at'] = _time(at)
        elif event in ('proposal_rejected', 'inference_discarded', 'worker_error'):
            stage, status, message = event, 'failed', row.get('reason') or row.get('error_type')
            if item is not None:
                item['decision_state'] = event
                item['decision_state_at'] = _time(at)
        if stage:
            events.append({'at': at, 'stage': stage, 'status': status, 'message': message,
                           'attempt': attempt_id, 'elapsed_seconds': row.get('elapsed_seconds')})
    if replay and frames and last_phase and _time(last_phase['at']) > frames[-1]['sort_time']:
        frames.append({**frames[-1], 'label': 'Final recorded phase',
                       'phase': last_phase, 'sort_time': _time(last_phase['at'])})
    return {'id': name, 'kind': 'continuous', 'result': None, 'scenario': {}, 'events': events if replay else events[-80:],
            'attempts': list(attempts.values()), 'frames': frames,
            'continuous': {'mode': mode, 'observe_only': observe_only, 'phase': last_phase, 'peak_mbps': peak}}


class Artifacts:
    def __init__(self,root):
        self.root=Path(root).resolve()

    def safe(self,path):
        path=path.resolve()
        if path!=self.root and self.root not in path.parents:
            raise ValueError('Path escapes experiment directory')
        return path

    def run(self,name):
        parts=name.split('/')
        if (len(parts) not in (1,3) or (len(parts)==3 and parts[1]!='trials')
                or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,150}',p) for p in parts)):
            raise ValueError('Invalid experiment ID')
        return self.safe(self.root/name)

    def runs(self):
        if not self.root.exists():
            return []
        output=[]
        candidates=[]
        for path in self.root.iterdir():
            try:
                self.safe(path)
                if path.is_dir():
                    candidates.append(path)
                    trials=self.safe(path/'trials')
                    if (path/'plan.json').is_file() and trials.is_dir():
                        candidates.extend(trials.iterdir())
            except (OSError, ValueError):
                continue
        for path in candidates:
            try:
                identifier = path.relative_to(self.root).as_posix()
                self.run(identifier)
                journal = self.safe(path/'events.jsonl')
                if not path.is_dir() or not journal.is_file():
                    continue
                result_path = self.safe(path/'result.json')
                result = read_json(result_path)
                continuous = self.safe(path/'live-baseline.json').is_file()
                updated = max(journal.stat().st_mtime, result_path.stat().st_mtime if result else 0)
            except (OSError, ValueError):
                continue
            output.append({'id':identifier,'label':result.get('scenario',path.name) if result else path.name,
                           'kind':'continuous' if continuous else 'experiment',
                           'complete':bool(result),'success':result.get('success') if result else None,
                           'mode':result.get('mode') if result else None,'updated':updated})
        telemetry=self.root/'visualizer'
        if telemetry.is_dir():
            for path in telemetry.glob('*.jsonl'):
                try:
                    self.safe(path)
                    output.append({'id':'telemetry:'+path.stem,'label':'Observer '+path.stem,'kind':'telemetry',
                                   'updated':path.stat().st_mtime})
                except ValueError:
                    continue
        return sorted(output,key=lambda r:r['updated'],reverse=True)[:60]

    def campaign(self,name):
        root=self.run(name)
        plan=read_json(self.safe(root/'plan.json'))
        if not plan or not isinstance(plan.get('jobs'),list):
            return None
        jobs=[]
        for index,job in enumerate(plan['jobs'][:10000],1):
            identifier=f'{name}/trials/{job["id"]}'
            path=self.run(identifier)
            result=read_json(self.safe(path/'result.json'))
            started=(path/'events.jsonl').is_file()
            jobs.append({'id':identifier,'index':index,'scenario':Path(job['scenario']).stem,
                         'mode':job['system_one'],'repetition':job['repetition'],
                         'status':('accepted' if result.get('success') else 'failed') if result else
                                  'running' if started else 'pending',
                         'cleanup_succeeded':result.get('cleanup_succeeded') if result else None,
                         'attempts':result.get('attempts') if result else None,
                         'seconds':result.get('fault_to_acceptance_seconds') if result else None})
        return {'id':name,'total':len(jobs),'completed':sum(j['status'] in ('accepted','failed') for j in jobs),
                'successful':sum(j['status']=='accepted' for j in jobs),
                'failed':sum(j['status']=='failed' for j in jobs),'jobs':jobs,
                'settings':{k:plan.get(k) for k in ('llm_model','sysone_model','jev_threshold','attempts','seconds')}}

    def latest_check(self):
        paths=[]
        if self.root.exists():
            for path in self.root.glob('abilene-check-*.json'):
                try:
                    self.safe(path)
                    paths.append(path)
                except ValueError:
                    continue
        if not paths:
            return None
        return read_json(max(paths,key=lambda p:p.stat().st_mtime))

    def experiment(self,name, *, replay=False):
        root=self.run(name)
        if not root.is_dir():
            raise FileNotFoundError(name)
        result=read_json(self.safe(root/'result.json'))
        result_at=(root/'result.json').stat().st_mtime if result else None
        events=read_lines(self.safe(root/'events.jsonl'))
        if self.safe(root/'live-baseline.json').is_file():
            return continuous_experiment(name, events, replay=replay)
        scenario=read_json(self.safe(root/'private-scenario.json')) or {}
        frames=[]
        for filename,label in [('healthy-measurements.json','Healthy baseline'),('impaired-measurements.json','Injected impairment'),('reset-measurements.json','Harness reset')]:
            path=self.safe(root/filename)
            value=read_json(path)
            if value:
                frames.append({'kind':'measurement','label':label,'sampled_at':value.get('observed_at'),
                               'sort_time':path.stat().st_mtime,'measurement':value})
        attempts=[]
        for attempt in sorted(root.glob('attempt-*'),key=lambda p:p.name):
            if not re.fullmatch(r'attempt-[1-5]',attempt.name):
                continue
            self.safe(attempt)
            decision=read_json(self.safe(attempt/'decision.json'))
            observation=self.safe(attempt/'observation.json')
            if not decision and not observation.is_file():
                continue
            checkpoint=read_json(self.safe(attempt/'checkpoint.json')) or {}
            configs={}
            diffs={}
            snapshot=self.safe(attempt/'snapshot/configs')
            if snapshot.is_dir():
                for cfg in snapshot.glob('*.cfg'):
                    self.safe(cfg)
                    if cfg.stat().st_size<=MAX_FILE:
                        candidate=cfg.read_text()
                        # Strip the verifier-only detection marker from the diff.
                        if candidate.startswith('! device:'):
                            candidate=candidate.split('\n',1)[-1]
                        configs[cfg.stem]=candidate
                        if cfg.stem in checkpoint:
                            diff=''.join(difflib.unified_diff(checkpoint[cfg.stem].splitlines(True),candidate.splitlines(True),fromfile='before',tofile='candidate'))
                            if diff:
                                diffs[cfg.stem]=diff
            info={'id':attempt.name,'decision':decision,'diffs':diffs,'candidate_configs':configs,
                  'decision_at':(attempt/'decision.json').stat().st_mtime if decision else None,
                  'started_at':observation.stat().st_mtime if observation.is_file() else attempt.stat().st_mtime,
                  'verification':read_json(self.safe(attempt/'verification.json')),
                  'acceptance':read_json(self.safe(attempt/'acceptance.json'))}
            for report_name in ('verification','acceptance'):
                info[report_name+'_at']=(attempt/(report_name+'.json')).stat().st_mtime if info[report_name] else None
            attempts.append(info)
            path=self.safe(attempt/'measurements.json')
            value=read_json(path)
            if value:
                frames.append({'kind':'measurement','label':attempt.name,'sampled_at':value.get('observed_at'),
                               'sort_time':path.stat().st_mtime,'measurement':value,'attempt':attempt.name})
        frames.sort(key=lambda f:f['sort_time'])
        if result and frames:
            frames.append({**frames[-1],'kind':'measurement','label':'Final outcome','sort_time':result_at,'final':True})
        for i,frame in enumerate(frames):
            frame['index']=i
        return {'id':name,'result':result,'result_at':result_at,'events':events,'attempts':attempts,'frames':frames,
                'scenario':{k:scenario[k] for k in ('name','objective','flow','flows') if k in scenario}}

    def replay(self,name):
        if name.startswith('telemetry:'):
            stem=name[len('telemetry:'):]
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',stem):
                raise ValueError('Invalid observer session ID')
            path=self.safe(self.root/'visualizer'/(stem+'.jsonl'))
            if not path.is_file():
                raise FileNotFoundError(name)
            rows=read_lines(path)
            return {'id':name,'kind':'telemetry','frames':[r for r in rows if r.get('kind')=='telemetry'],
                    'paths':[r for r in rows if r.get('kind')=='path'],
                    'observer_events':[r for r in rows if r.get('kind')=='observer_event']}
        return {'kind':'experiment',**self.experiment(name, replay=True)}
