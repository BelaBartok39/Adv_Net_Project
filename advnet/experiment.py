"""CLI for Abilene baseline checks, observation capture, and LLM-only trials."""
import argparse
import fcntl
import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .lab import Deadline, Lab
from .trial import DecisionProcess, ProcessVerifier, Trial, dump


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['check','collect','run'])
    parser.add_argument('--manifest',type=Path,default=Path('topologies/abilene.json'))
    parser.add_argument('--output',type=Path)
    parser.add_argument('--scenario',type=Path)
    parser.add_argument('--model',default=os.environ.get('LLM_MODEL'))
    parser.add_argument('--provider',choices=['openai','anthropic','compatible'],default='openai')
    parser.add_argument('--base-url',default=os.environ.get('LLM_BASE_URL'))
    parser.add_argument('--max-tokens',type=int,default=4096)
    parser.add_argument('--attempts',type=int,default=5)
    parser.add_argument('--seconds',type=float,default=900)
    parser.add_argument('--verifier-command',help='Executable plus arguments; snapshot directory is appended. No shell evaluation.')
    args=parser.parse_args()
    if args.action=='run' and (not args.model or not args.scenario):
        parser.error('run requires --model and --scenario')
    if args.action=='run':
        credential='ANTHROPIC_API_KEY' if args.provider=='anthropic' else 'OPENAI_API_KEY'
        if not os.environ.get(credential):
            parser.error(f'{credential} must be set before a fault can be injected')
        if args.provider=='compatible' and not args.base_url:
            parser.error('compatible provider requires --base-url')
    if not 1<=args.attempts<=5 or args.seconds<=0 or args.seconds>86400 or args.max_tokens<1:
        parser.error('Invalid attempt, time, or token budget')
    manifest=json.loads(args.manifest.read_text())
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
        if args.action in ('check','collect'):
            deadline=Deadline(args.seconds)
            lab.wait_convergence(deadline)
            measurements=lab.measure(deadline)
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
        decision=DecisionProcess(args.provider,args.model,directory,args.base_url,args.max_tokens)
        argv=shlex.split(args.verifier_command) if args.verifier_command else [sys.executable,'-m','advnet.batfish_gate']
        verifier=ProcessVerifier(argv)
        trial=Trial(lab,decision,verifier,directory,args.attempts,args.seconds)
        scenario=json.loads(args.scenario.read_text())
        result=trial.run(scenario)
        print(json.dumps({'output':str(directory),'success':result['success'],'attempts':result['attempts'],
                          'fault_to_acceptance_seconds':result['fault_to_acceptance_seconds'],
                          'cleanup_succeeded':result['cleanup_succeeded'],'error':result.get('error')},indent=2))
        return 0 if result['success'] else 1


if __name__=='__main__':
    sys.exit(main())
