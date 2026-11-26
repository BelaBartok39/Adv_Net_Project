"""Resolve, replay, and record dynamic traffic; live execution requires opt-in."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

from .dynamic_scheduler import SystemClock
from .dynamic_udp import run_dynamic_udp
from .dynamic_workload import ResolvedWorkload, generate_workload, resolve_workload
from .performance import UNITS


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = ('dynamic_traffic.py', 'dynamic_udp.py', 'dynamic_scheduler.py',
                'dynamic_workload.py', 'lab.py', 'scenarios.py', 'contracts.py', 'performance.py')


def _json(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key: '+key)
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError('Nonfinite JSON number: '+value)
    return json.loads(data, object_pairs_hook=unique, parse_constant=nonfinite)


def load_schedule(data, manifest, *, replay=False):
    """Load explicit demand, generator settings, or an already resolved schedule.

    Replay always validates resolved events directly and never invokes the RNG.
    Strict models reject mixtures of the three input schemas.
    """
    value = _read_json(data)
    if not isinstance(value, dict):
        raise ValueError('Workload input must be a JSON object')
    if replay or 'events' in value:
        return ResolvedWorkload.model_validate(value).validate_inventory(manifest), 'resolved'
    if 'flow_count' in value:
        return resolve_workload(generate_workload(value, manifest), manifest), 'generator'
    return resolve_workload(value, manifest), 'workload'


def _atomic_write(path, data):
    """Replace only files inside this run's exclusively created directory."""
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.'+path.name, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def resolve_file(workload_path, manifest_path, output):
    """Offline resolution. Refuse to overwrite an existing saved schedule."""
    manifest = _read_json(Path(manifest_path).read_bytes())
    resolved, kind = load_schedule(Path(workload_path).read_bytes(), manifest)
    data = _json(resolved.model_dump(mode='json'))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as stream:
        stream.write(data)
    return {'output': str(output), 'status': 'resolved', 'input_kind': kind,
            'seed': resolved.seed, 'event_count': len(resolved.events), 'schedule_sha256': _sha(data)}


def measurement_report(execution):
    """Keep the transport's receiver metrics and missing-data indicators intact."""
    rows = [{'flow_id': segment['flow_id'], 'segment_index': segment['segment_index'],
             'metrics': deepcopy(segment['metrics']), 'measurement_error': segment['measurement_error']}
            for segment in execution.get('segments', [])]
    known = sum(row['metrics'].get('throughput_status') == 'measured'
                and row['metrics'].get('received_mbps') is not None for row in rows)
    return {'schema_version': '1', 'scope': 'receiver_per_segment',
            'units': {key: UNITS[key] for key in ('received_mbps', 'loss_fraction', 'jitter_ms')},
            'throughput_status': 'available' if rows and known == len(rows) else 'partial' if known else 'unavailable',
            'segments': rows}


def lifecycle_records(execution, experiment_id):
    """Record observed client/control events, never inferred packet timing."""
    records = []
    for segment in execution.get('segments', []):
        identity = {'flow_id': segment['flow_id'], 'segment_index': segment['segment_index'],
                    'offered_mbps': segment['offered_mbps'], 'container_id': segment['container_id']}
        for field, kind in (('created_seconds', 'client_created'),
                            ('start_requested_seconds', 'client_start_requested'),
                            ('start_returned_seconds', 'client_start_returned'),
                            ('stop_requested_seconds', 'client_stop_requested'),
                            ('stop_confirmed_seconds', 'client_stop_confirmed'),
                            ('removed_seconds', 'client_removed')):
            if segment.get(field) is not None:
                records.append({**identity, 'kind': kind, 'at_seconds': segment[field], 'timing_status': 'recorded'})
        records.append({**identity, 'kind': 'client_final_state', 'at_seconds': None,
                        'timing_status': 'unavailable', 'status': segment['status'],
                        'removed': segment['removed'], 'returncode': segment['returncode'], 'error': segment['error']})
    for change in execution.get('rate_changes', []):
        for field, kind in (('requested_seconds', 'rate_change_requested'), ('finished_seconds', 'rate_change_finished')):
            records.append({'flow_id': change['flow_id'], 'kind': kind, 'at_seconds': change.get(field),
                            'timing_status': 'recorded' if change.get(field) is not None else 'unavailable',
                            'offered_mbps': change['offered_mbps'], 'gap_status': change['gap_status'],
                            'restart_control_seconds': change['restart_control_seconds'],
                            'minimum_no_client_seconds': change['minimum_no_client_seconds'],
                            'traffic_gap_seconds': change['traffic_gap_seconds'], 'error': change['error']})
    records.sort(key=lambda record: (record['at_seconds'] is None,
                                     record['at_seconds'] if record['at_seconds'] is not None else 0))
    return [{**record, 'sequence': index, 'experiment_id': experiment_id, 'scope': 'traffic_generation'}
            for index, record in enumerate(records)]


def execute_workload(workload_path, manifest_path, output, *, replay=False, allow_live=False,
                     clock=None, runner=None, **scheduler_options):
    """Save inputs before traffic and persist complete/partial evidence afterward.

    The injected runner is for offline tests. Normal execution uses the existing
    opt-in UDP wrapper and its shared lab lock. Interruptions propagate after
    their available evidence is saved; an evidence-write error cannot hide the
    original execution error. Each output directory must be new.
    """
    if allow_live is not True:
        raise PermissionError('Live traffic requires explicit --allow-live')
    workload_path, manifest_path, output = Path(workload_path), Path(manifest_path), Path(output)
    input_data, manifest_data = workload_path.read_bytes(), manifest_path.read_bytes()
    manifest = _read_json(manifest_data)
    resolved, kind = load_schedule(input_data, manifest, replay=replay)
    schedule_data = _json(resolved.model_dump(mode='json'))
    settings = {'timeout_seconds': None, 'operation_timeout_seconds': 5,
                'cleanup_timeout_seconds': 10, 'poll_interval_seconds': 0.1}
    if set(scheduler_options) - set(settings):
        raise ValueError('Unsupported scheduler settings')
    settings.update(scheduler_options)
    for name, value in settings.items():
        if name == 'timeout_seconds' and value is None:
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(name+' must be finite and positive')
    clock = SystemClock() if clock is None else clock
    runner = run_dynamic_udp if runner is None else runner
    experiment_id = 'dynamic-'+uuid4().hex
    before = clock.monotonic()
    utc_anchor = datetime.now(timezone.utc).isoformat()
    after = clock.monotonic()
    evidence = {'schema_version': '1', 'experiment_id': experiment_id, 'scope': 'traffic_generation',
                'status': 'running', 'action': 'replay' if replay else 'run', 'input_kind': kind,
                'seed': resolved.seed, 'started_at': utc_anchor, 'finished_at': None,
                'input_sha256': {'workload': _sha(input_data), 'manifest': _sha(manifest_data)},
                'resolved_schedule_sha256': _sha(schedule_data),
                'code_sha256': {'advnet/'+name: _sha(Path(__file__).with_name(name).read_bytes()) for name in SOURCE_FILES},
                'runtime': {'python': sys.version, 'pydantic': version('pydantic'), 'client_image': 'advnet-host:0.1'},
                'scheduler_options': settings,
                'timeline': {'unit': 'seconds', 'reference': 'experiment_start',
                             'started_monotonic_seconds': None, 'clock_scope': 'same host and boot',
                             'utc_anchor': utc_anchor, 'anchor_monotonic_before_seconds': before,
                             'anchor_monotonic_after_seconds': after},
                'cleanup_succeeded': None, 'throughput_status': 'unavailable', 'error': None, 'cleanup_error': None,
                'artifact_sha256': {}}
    output.mkdir(parents=True, exist_ok=False)
    execution, failure = None, None
    def started(origin):
        evidence['timeline']['started_monotonic_seconds'] = origin
        _atomic_write(output/'evidence.json', _json(evidence))
    try:
        _atomic_write(output/'evidence.json', _json(evidence))
        _atomic_write(output/'input-workload.json', input_data)
        _atomic_write(output/'manifest.json', manifest_data)
        _atomic_write(output/'resolved-workload.json', schedule_data)
        execution = runner(resolved, manifest, allow_live=True, clock=clock, on_start=started, **settings)
    except BaseException as error:
        failure = error
        execution = getattr(error, 'traffic_result', None)
        evidence['error'] = {'error_type': type(error).__name__, 'message': str(error)}
    try:
        if execution is not None:
            scheduler = execution['scheduler']
            evidence['status'] = scheduler['status']
            evidence['cleanup_succeeded'] = scheduler['cleanup_succeeded']
            evidence['error'] = evidence['error'] or scheduler['error']
            evidence['cleanup_error'] = scheduler['cleanup_error']
            evidence['timeline']['started_monotonic_seconds'] = scheduler['started_monotonic_seconds']
            metrics = measurement_report(execution)
            evidence['throughput_status'] = metrics['throughput_status']
            records = lifecycle_records(execution, experiment_id)
            _atomic_write(output/'execution.json', _json(execution))
            _atomic_write(output/'measurements.json', _json(metrics))
            _atomic_write(output/'flow-lifecycle.jsonl', b''.join(
                (json.dumps(record, sort_keys=True, allow_nan=False)+'\n').encode() for record in records))
        else:
            evidence['status'] = 'interrupted' if failure is not None and not isinstance(failure, Exception) else 'failed'
        evidence['finished_at'] = datetime.now(timezone.utc).isoformat()
        for path in sorted(output.iterdir()):
            if path.is_file() and path.name != 'evidence.json':
                evidence['artifact_sha256'][path.name] = _sha(path.read_bytes())
        _atomic_write(output/'evidence.json', _json(evidence))
    except Exception as save_error:
        if failure is not None:
            failure.add_note('Saving dynamic traffic evidence failed: '+str(save_error))
            raise failure
        if execution is not None:
            save_error.traffic_result = execution
            primary = execution['scheduler']['error'] or execution['scheduler']['cleanup_error']
            if primary is not None:
                save_error.add_note('Execution also failed: '+primary['error_type']+': '+primary['message'])
        raise
    if failure is not None:
        raise failure
    return {'output': str(output), 'experiment_id': experiment_id, 'status': evidence['status'],
            'cleanup_succeeded': evidence['cleanup_succeeded'], 'throughput_status': evidence['throughput_status'],
            'schedule_sha256': evidence['resolved_schedule_sha256']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    for action in ('resolve', 'run', 'replay'):
        command = commands.add_parser(action)
        command.add_argument('--schedule' if action == 'replay' else '--workload', required=True, type=Path)
        command.add_argument('--manifest', type=Path, default=ROOT/'topologies/abilene.json')
        command.add_argument('--output', required=True, type=Path)
        if action != 'resolve':
            command.add_argument('--allow-live', action='store_true', help='Generate traffic on the already-running lab')
            command.add_argument('--timeout-seconds', type=float)
            command.add_argument('--operation-timeout-seconds', type=float, default=5)
            command.add_argument('--cleanup-timeout-seconds', type=float, default=10)
            command.add_argument('--poll-interval-seconds', type=float, default=0.1)
    args = parser.parse_args(argv)
    if args.action != 'resolve' and not args.allow_live:
        parser.error('run/replay require --allow-live; use resolve for offline validation')
    try:
        if args.action == 'resolve':
            result = resolve_file(args.workload, args.manifest, args.output)
        else:
            source = args.schedule if args.action == 'replay' else args.workload
            result = execute_workload(source, args.manifest, args.output, replay=args.action == 'replay',
                                      allow_live=args.allow_live, timeout_seconds=args.timeout_seconds,
                                      operation_timeout_seconds=args.operation_timeout_seconds,
                                      cleanup_timeout_seconds=args.cleanup_timeout_seconds,
                                      poll_interval_seconds=args.poll_interval_seconds)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if result['status'] in ('resolved', 'completed') else 1
    except KeyboardInterrupt:
        print('Dynamic traffic interrupted; any saved evidence is under '+str(args.output), file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
        print(type(error).__name__+': '+str(error), file=sys.stderr)
        for note in getattr(error, '__notes__', []):
            print(note, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
