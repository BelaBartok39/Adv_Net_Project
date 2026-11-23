"""Abilene checks and controlled trials with optional Jev screening."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .lab import Deadline, Lab
from .catalog import experiment_catalog
from .scenarios import load_scenario, load_traffic_profile
from .trial import DecisionProcess, ProcessVerifier, Trial, dump


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['check','collect','traffic','run'])
    parser.add_argument('--manifest',type=Path,default=Path('topologies/abilene.json'))
    parser.add_argument('--output',type=Path)
    parser.add_argument('--scenario',type=Path)
    parser.add_argument('--traffic-profile',type=Path,help='Single flow or concurrent flows JSON for traffic/collect; no model calls')
    parser.add_argument('--model',default=os.environ.get('LLM_MODEL'))
    parser.add_argument('--provider',choices=['openrouter','compatible'],default='openrouter')
    parser.add_argument('--base-url',default=os.environ.get('LLM_BASE_URL','https://openrouter.ai/api/v1'))
    parser.add_argument('--max-tokens',type=int,default=4096)
    parser.add_argument('--attempts',type=int,default=5)
    parser.add_argument('--seconds',type=float,default=900)
    parser.add_argument('--system-one',choices=['off','jev'],default='off',
                        help='off: LLM-only baseline; jev: screen routine decisions before the LLM')
    parser.add_argument('--sysone-model','--jev-model',dest='sysone_model',default=os.environ.get('SYSONE_MODEL'),
                        help='System One model ID, independent of --model / LLM_MODEL')
    parser.add_argument('--jev-threshold',type=float,default=0.9)
    parser.add_argument('--jev-timeout',type=float,default=10)
    parser.add_argument('--force-escalation',action='store_true',help='Controlled overhead probe: call Jev then force LLM fallback; exclude from routing accuracy')
    parser.add_argument('--reset-baseline',action='store_true',help='Restore all router source configs and 100 Mbps link shaping before preflight')
    parser.add_argument('--baseline-configs',type=Path,default=Path('labs/configs/abilene'))
    parser.add_argument('--catalog',type=Path,help='Optional shared action catalog; default: host-interface recovery from topology inventory')
    parser.add_argument('--verifier-command',help='Executable plus arguments; snapshot directory is appended. No shell evaluation.')
    args=parser.parse_args()
    if args.traffic_profile and args.action not in ('traffic','collect'):
        parser.error('--traffic-profile is supported by traffic and collect; run uses its scenario')
    if args.action=='traffic' and not args.traffic_profile:
        parser.error('traffic requires --traffic-profile')
    if args.force_escalation and args.system_one!='jev':
        parser.error('--force-escalation requires --system-one jev')
    if args.action=='run' and (not args.model or not args.scenario):
        parser.error('run requires --model and --scenario')
    if args.action=='run':
        if not os.environ.get('OPEN_ROUTER_KEY'):
            parser.error('OPEN_ROUTER_KEY must be set before a fault can be injected')
        if args.provider=='compatible' and not args.base_url:
            parser.error('compatible provider requires --base-url')
        if args.system_one=='jev':
            if not args.sysone_model:
                parser.error('Set SYSONE_MODEL or --sysone-model when --system-one jev is enabled')
            if importlib.util.find_spec('typesafe_sdk') is None:
                parser.error('Install the Jev dependency first: uv sync --extra openai --extra batfish --extra jev')
    if not 1<=args.attempts<=5 or args.seconds<=0 or args.seconds>86400 or args.max_tokens<1:
        parser.error('Invalid attempt, time, or token budget')
    if not math.isfinite(args.jev_threshold) or not 0<=args.jev_threshold<=1 or not math.isfinite(args.jev_timeout) or args.jev_timeout<=0:
        parser.error('Jev threshold must be 0..1 and Jev timeout must be finite and positive')
    manifest=json.loads(args.manifest.read_text())
    traffic_profile=load_traffic_profile(args.traffic_profile,manifest) if args.traffic_profile else None
    catalog=experiment_catalog(manifest,args.catalog) if args.action=='run' else {}
    scenario=load_scenario(args.scenario,manifest,catalog) if args.action=='run' else None
    baseline_configs=None
    if args.action=='run' and args.reset_baseline:
        baseline_configs={node:(args.baseline_configs/(node+'.cfg')).read_text() for node in manifest['routers']}
    lab=Lab(manifest)
    Path('runs').mkdir(exist_ok=True)
    # Prevent two local experiment processes from changing/measuring this lab at once.
    # flock does not require writing the file. Reuse locks created by an earlier
    # sudo invocation without truncating them or requiring root for observations.
    lock_path=Path('runs/.abilene.lock')
    with lock_path.open('r' if lock_path.exists() else 'w') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('Another Abilene experiment is running')
        if args.action=='traffic':
            directory=args.output or Path('runs')/('traffic-'+uuid4().hex)
            directory.mkdir(parents=True,exist_ok=False)
            dump(directory/'manifest.json',manifest)
            dump(directory/'traffic-profile.json',traffic_profile)
            evidence={'scope':'Traffic-only collection; no shaping, routing writes, Batfish, inference, or repair acceptance.',
                      'started_at':datetime.now(timezone.utc).isoformat(),
                      'input_sha256':{'manifest':hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
                                      'traffic_profile':hashlib.sha256(args.traffic_profile.read_bytes()).hexdigest()},
                      'code_sha256':{name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                     for name in ('experiment.py','lab.py','concurrent_traffic.py','scenarios.py')}}
            try:
                deadline=Deadline(args.seconds)
                traffic=(lab.concurrent_traffic(traffic_profile['flows'],deadline) if 'flows' in traffic_profile
                         else lab.traffic(traffic_profile['flow'],deadline))
                dump(directory/'measurements.json',{'observed_at':datetime.now(timezone.utc).isoformat(),
                     'scope':'traffic_only','mean_rtt_ms':None,'traffic':traffic})
                evidence['status']='collected'
            except BaseException as error:
                evidence.update(status='failed',error_type=type(error).__name__,error=str(error)[:1500])
                raise
            finally:
                evidence['finished_at']=datetime.now(timezone.utc).isoformat()
                dump(directory/'evidence.json',evidence)
            print(json.dumps({'output':str(directory),'status':'collected',
                              'flow_count':len(traffic_profile.get('flows',[])) or 1},indent=2))
            return 0
        if args.action in ('check','collect'):
            deadline=Deadline(args.seconds)
            lab.wait_convergence(deadline,120 if args.action=='check' else 60,
                                 require_healthy=args.action=='check')
            measurements=lab.measure(deadline,**traffic_profile) if traffic_profile else lab.measure(deadline)
            path=args.output or Path('runs')/('abilene-'+args.action+'-'+uuid4().hex+'.json')
            path.parent.mkdir(parents=True,exist_ok=True)
            if args.action=='collect':
                observation=lab.observe('observation-'+uuid4().hex,measurements,deadline,'Restore and maintain the declared network requirements.')
                path.write_text(observation.model_dump_json(indent=2))
            else:
                dump(path,measurements)
            print(json.dumps({'output':str(path),'pairs_checked':len(measurements['pairs']),
                              'all_reachable':measurements['all_reachable'],
                              'ospf_healthy':measurements['ospf_healthy'],'mean_rtt_ms':measurements['mean_rtt_ms']},indent=2))
            return 0 if measurements['all_reachable'] and measurements['ospf_healthy'] else 1
        directory=args.output or Path('runs')/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8])
        decision=DecisionProcess(args.provider,args.model,directory,args.base_url,args.max_tokens,
                                 system_one=args.system_one,sysone_model=args.sysone_model,
                                 threshold=args.jev_threshold,jev_timeout=args.jev_timeout,catalog=catalog,
                                 force_escalation=args.force_escalation)
        argv=shlex.split(args.verifier_command) if args.verifier_command else [sys.executable,'-m','advnet.batfish_gate']
        verifier=ProcessVerifier(argv)
        trial=Trial(lab,decision,verifier,directory,args.attempts,args.seconds,baseline_configs=baseline_configs)
        result=trial.run(scenario)
        print(json.dumps({'output':str(directory),'mode':result['mode'],'success':result['success'],'attempts':result['attempts'],
                          'fault_to_acceptance_seconds':result['fault_to_acceptance_seconds'],
                          'cleanup_succeeded':result['cleanup_succeeded'],'error':result.get('error'),
                          'performance_report':result.get('performance_report')},indent=2))
        return 0 if result['success'] else 1


if __name__=='__main__':
    sys.exit(main())
