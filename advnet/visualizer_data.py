"""Read experiment artifacts through a fixed allowlist, plus telemetry recording/replay."""
import difflib
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


class Artifacts:
    def __init__(self,root):
        self.root=Path(root).resolve()

    def safe(self,path):
        path=path.resolve()
        if path!=self.root and self.root not in path.parents:
            raise ValueError('Path escapes experiment directory')
        return path

    def run(self,name):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,150}',name):
            raise ValueError('Invalid experiment ID')
        return self.safe(self.root/name)

    def runs(self):
        if not self.root.exists():
            return []
        output=[]
        for path in self.root.iterdir():
            try:
                self.safe(path)
            except ValueError:
                continue
            if path.is_dir() and (path/'events.jsonl').is_file():
                result=read_json(self.safe(path/'result.json'))
                output.append({'id':path.name,'label':result.get('scenario',path.name) if result else path.name,
                               'kind':'experiment','complete':bool(result),'success':result.get('success') if result else None,
                               'updated':path.stat().st_mtime})
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

    def experiment(self,name):
        root=self.run(name)
        if not root.is_dir():
            raise FileNotFoundError(name)
        result=read_json(self.safe(root/'result.json'))
        result_at=(root/'result.json').stat().st_mtime if result else None
        events=read_lines(self.safe(root/'events.jsonl'))
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
            if not decision:
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
                  'decision_at':(attempt/'decision.json').stat().st_mtime,
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
        return {'id':name,'result':result,'result_at':result_at,'events':events,'attempts':attempts,'frames':frames}

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
        return {'kind':'experiment',**self.experiment(name)}
