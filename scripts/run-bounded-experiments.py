#!/usr/bin/env python3
"""Prepare and execute sequential, matched Abilene trials; retain every outcome."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from advnet.catalog import catalog_hash, experiment_catalog
from advnet.scenarios import load_scenario


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def packages():
    result = {'python': sys.version}
    for name in ('pydantic', 'openai', 'typesafe-sdk', 'pybatfish'):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


def make_plan(profile, repetitions, include_concurrent, llm_model, sysone_model,
              base_url, attempts=3, seconds=900):
    if not llm_model or not sysone_model:
        raise ValueError('Both LLM and Jev model IDs are required to prepare a plan')
    if not 1 <= repetitions <= 100 or not 1 <= attempts <= 5 or not 0 < seconds <= 86400:
        raise ValueError('Invalid repetitions, attempts, or time budget')
    index_path = ROOT / 'scenarios/benchmark-v1/suite.json'
    index = json.loads(index_path.read_text())
    entries = {entry['name']: entry for entry in index['cases']}
    cases = [Path('scenarios/benchmark-v1') / entries[name]['path']
             for name in index['profiles'][profile]]
    if include_concurrent:
        cases.append(Path('scenarios/concurrent-sea-sva-congestion.json'))
    manifest = json.loads((ROOT / 'topologies/abilene.json').read_text())
    catalog = experiment_catalog(manifest)
    if catalog_hash(catalog) != index['catalog_sha256']:
        raise ValueError('Benchmark catalog fingerprint is stale')
    for case in cases:
        load_scenario(ROOT / case, manifest, catalog)
    jobs = []
    for repetition in range(1, repetitions + 1):
        for case_index, case in enumerate(cases):
            # Reverse first arm across cases and across repetitions.
            modes = ('off', 'jev') if (case_index + repetition - 1) % 2 == 0 else ('jev', 'off')
            for mode in modes:
                jobs.append({'id': f'{len(jobs)+1:04d}-r{repetition}-{case.stem}-{mode}',
                             'scenario': str(case), 'system_one': mode,
                             'repetition': repetition})
    files = {*ROOT.glob('advnet/*.py'), *ROOT.glob('labs/configs/abilene/*.cfg'),
             ROOT / 'topologies/abilene.json', index_path,
             Path(__file__).resolve(), ROOT / 'scripts/summarize-trials.py',
             ROOT / 'pyproject.toml', ROOT / 'uv.lock', *(ROOT / case for case in cases)}
    return {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
            'purpose': 'bounded pilot' if repetitions == 1 else 'repeated bounded comparison',
            'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'profile': profile, 'repetitions': repetitions, 'include_concurrent': include_concurrent,
            'llm_model': llm_model, 'sysone_model': sysone_model, 'base_url': base_url,
            'attempts': attempts, 'seconds': seconds, 'jev_threshold': 0.9,
            'catalog_sha256': catalog_hash(catalog), 'packages': packages(),
            'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted(files)},
            'job_count': len(jobs), 'jobs': jobs}


def command(plan, job, output):
    return [sys.executable, '-m', 'advnet.experiment', 'run',
            '--scenario', job['scenario'], '--system-one', job['system_one'],
            '--provider', 'openrouter', '--base-url', plan['base_url'],
            '--model', plan['llm_model'], '--sysone-model', plan['sysone_model'],
            '--jev-threshold', str(plan['jev_threshold']), '--reset-baseline',
            '--attempts', str(plan['attempts']), '--seconds', str(plan['seconds']),
            '--output', str(output)]


def saved_outcome(directory):
    path = directory / 'result.json'
    if not path.exists():
        if directory.exists():
            raise ValueError(f'Incomplete trial requires inspection before continuing: {directory}')
        return None
    value = json.loads(path.read_text())
    if any(type(value.get(key)) is not bool for key in ('success', 'cleanup_succeeded')):
        raise ValueError(f'Malformed trial outcome: {path}')
    return value


def stop_reason(result):
    if result['cleanup_succeeded'] is not True:
        return 'Cleanup did not succeed; inspect lab state before continuing.'
    if result.get('baseline_reset_completed') is not True or result.get('attempts', 0) == 0:
        return 'Baseline reset or healthy preflight failed; inspect the setup.'
    if any(call.get('error_type') in {'AuthenticationError', 'TypeSafeAuthenticationError'}
           for call in result.get('model_calls', [])):
        return 'Provider authentication failed; retained this outcome and stopped further calls.'
    return None


def summarize(directory, plan):
    records = []
    for job in plan['jobs']:
        path = directory / 'trials' / job['id'] / 'result.json'
        if not path.exists():
            continue
        result = json.loads(path.read_text())
        records.append({**job, 'trial_id': result.get('trial_id'),
                        'success': result.get('success'),
                        'cleanup_succeeded': result.get('cleanup_succeeded'),
                        'fault_to_acceptance_seconds': result.get('fault_to_acceptance_seconds'),
                        'error_type': result.get('error_type')})
    summary = {'planned': len(plan['jobs']), 'recorded': len(records),
               'successful': sum(r['success'] is True for r in records),
               'failed': sum(r['success'] is False for r in records),
               'by_mode': {mode: dict(Counter('success' if r['success'] else 'failure'
                                             for r in records if r['system_one'] == mode))
                           for mode in ('off', 'jev')},
               'jobs': records}
    save(directory / 'summary.json', summary)
    with (directory / 'comparison.csv').open('w') as stream:
        subprocess.run([sys.executable, str(ROOT / 'scripts/summarize-trials.py'),
                        str(directory / 'trials')], cwd=ROOT, stdout=stream, check=True)
    return summary


def check_sources(plan):
    for name, digest in plan['source_sha256'].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f'Source changed since preparation: {name}. Prepare a separate campaign.')


def execute(directory):
    directory = directory.resolve()
    plan = json.loads((directory / 'plan.json').read_text())
    check_sources(plan)
    if packages() != plan['packages']:
        raise ValueError('Python or dependency versions changed since preparation')
    if not os.environ.get('OPEN_ROUTER_KEY'):
        raise ValueError('Export OPEN_ROUTER_KEY in the shell used to execute the campaign')
    if not os.environ.get('BATFISH_HOST', 'localhost').strip():
        raise ValueError('BATFISH_HOST must be a hostname')
    (directory / 'logs').mkdir(exist_ok=True)
    (directory / 'trials').mkdir(exist_ok=True)
    with (ROOT / 'runs/.bounded-campaign.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for index, job in enumerate(plan['jobs'], 1):
            check_sources(plan)
            output = directory / 'trials' / job['id']
            result = saved_outcome(output)
            if result is None:
                print(f'[{index}/{len(plan["jobs"])}] {job["id"]}', flush=True)
                interrupted = False
                with (directory / 'logs' / (job['id'] + '.stdout.txt')).open('x') as stdout, \
                     (directory / 'logs' / (job['id'] + '.stderr.txt')).open('x') as stderr:
                    process = subprocess.Popen(command(plan, job, output), cwd=ROOT,
                                               stdout=stdout, stderr=stderr, start_new_session=True)
                    try:
                        returncode = process.wait()
                    except KeyboardInterrupt:
                        # Signal the controller and allow its finally block to restore the lab.
                        process.send_signal(signal.SIGINT)
                        print('Stopping; waiting for the active trial to finish cleanup...', flush=True)
                        returncode = process.wait()
                        interrupted = True
                result = saved_outcome(output)
                summarize(directory, plan)
                if result is None:
                    raise ValueError(f'Trial exited {returncode} without a result; see logs/{job["id"]}.*')
                print(f'  success={result["success"]}, cleanup={result["cleanup_succeeded"]}', flush=True)
                if interrupted:
                    raise ValueError('Campaign interrupted; recorded the trial outcome and stopped')
            reason = stop_reason(result)
            if reason:
                summarize(directory, plan)
                raise ValueError(reason)
        summary = summarize(directory, plan)
        print(json.dumps({'output': str(directory), **{k: v for k, v in summary.items() if k != 'jobs'}}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prepare = sub.add_parser('prepare', help='Freeze an experiment plan; no model calls or lab writes')
    prepare.add_argument('--profile', choices=['smoke', 'all', 'routine', 'escalation'], default='smoke')
    prepare.add_argument('--repetitions', type=int, default=1)
    prepare.add_argument('--include-concurrent', action='store_true')
    prepare.add_argument('--llm-model', default=os.environ.get('LLM_MODEL'))
    prepare.add_argument('--sysone-model', default=os.environ.get('SYSONE_MODEL'))
    prepare.add_argument('--base-url', default=os.environ.get('LLM_BASE_URL', 'https://openrouter.ai/api/v1'))
    prepare.add_argument('--output', required=True, type=Path)
    run = sub.add_parser('run', help='Run or resume the frozen plan without rerunning recorded outcomes')
    run.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            plan = make_plan(args.profile, args.repetitions, args.include_concurrent,
                             args.llm_model, args.sysone_model, args.base_url)
            args.output.mkdir(parents=True, exist_ok=False)
            save(args.output / 'plan.json', plan)
            print(json.dumps({'output': str(args.output.resolve()), 'jobs': plan['job_count'],
                              'purpose': plan['purpose'], 'models': [plan['llm_model'], plan['sysone_model']]}, indent=2))
        else:
            execute(args.directory)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
