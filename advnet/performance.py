"""Descriptive comparisons of recorded measurements; no acceptance decisions."""
import json
import math
from pathlib import Path
import re


STAGES = ('healthy', 'impaired', 'post')
PAIRS = {'impaired_vs_healthy': ('healthy', 'impaired'),
         'post_vs_impaired': ('impaired', 'post'),
         'post_vs_healthy': ('healthy', 'post')}
UNITS = {'received_mbps': 'Mbps', 'loss_fraction': 'fraction',
         'loaded_rtt_mean_ms': 'ms', 'jitter_ms': 'ms',
         'max_link_utilization': 'fraction', 'idle_rtt_mean_ms': 'ms'}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _number(value):
    if type(value) not in (int, float):
        return None
    try:
        return value if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _metric(value, unit, source_status=None, upper=None):
    result = {'value': None, 'unit': unit, 'status': 'missing'}
    if source_status in ('missing', 'unavailable', 'invalid'):
        result['status'] = source_status
    elif source_status not in (None, 'measured', 'partial'):
        result['status'] = 'invalid'
    elif value is not None:
        number = _number(value)
        if number is None or (upper is not None and number > upper):
            result['status'] = 'invalid'
        else:
            result.update(value=number, status='partial' if source_status == 'partial' else 'available')
    return result


def _traffic_metrics(traffic):
    traffic = _mapping(traffic)
    loaded = _mapping(traffic.get('loaded_rtt'))
    return {
        'received_mbps': _metric(traffic.get('received_mbps'), 'Mbps', traffic.get('throughput_status')),
        'loss_fraction': _metric(traffic.get('loss_fraction'), 'fraction', traffic.get('loss_status'), 1),
        'jitter_ms': _metric(traffic.get('jitter_ms'), 'ms', traffic.get('jitter_status')),
        'loaded_rtt_mean_ms': _metric(loaded.get('mean_ms'), 'ms', loaded.get('status')),
    }


def _flow_key(flow):
    source, destination = flow.get('source'), flow.get('destination')
    port = flow.get('destination_port', 5201)
    if (not isinstance(source, str) or not source or not isinstance(destination, str)
            or not destination or type(port) is not int or not 1 <= port <= 65535):
        return None
    return source, destination, port


def _normalize(measurement):
    data = _mapping(measurement)
    traffic = _mapping(data.get('traffic'))
    batch = traffic.get('mode') == 'concurrent_udp'
    mode = 'concurrent_udp' if batch else 'single_udp' if traffic else None
    summary = _traffic_metrics(traffic.get('aggregate') if batch else traffic)
    summary['max_link_utilization'] = _metric(traffic.get('max_link_utilization'), 'fraction')
    summary['idle_rtt_mean_ms'] = _metric(data.get('mean_rtt_ms'), 'ms')
    flows, warnings = {}, []
    rows = traffic.get('flows') if batch else [traffic] if traffic else []
    if not isinstance(rows, list):
        rows = []
        warnings.append('Concurrent flow records are missing or malformed.')
    for row in rows:
        row = _mapping(row)
        key = _flow_key(row)
        if key is None:
            warnings.append('Flow identity is unavailable; workload matching cannot be established.')
            continue
        if key in flows:
            raise ValueError('Duplicate flow identity prevents an unambiguous comparison: '+str(key))
        config = dict(zip(('source', 'destination', 'destination_port'), key))
        config.update(offered_mbps=_number(row.get('offered_mbps')), seconds=_number(row.get('seconds')))
        flows[key] = {'configuration': config, 'metrics': _traffic_metrics(row)}
    if batch and traffic.get('concurrency_verified') is not True:
        warnings.append('Concurrent sender overlap was not verified.')
    if traffic and traffic.get('capacities_enforced') is not True:
        warnings.append('Utilization may use reference capacities rather than enforced shaping.')
    links = {key: _metric(value, 'fraction')
             for key, value in _mapping(traffic.get('link_utilization')).items()}
    capacities = {key: _number(value) for key, value in _mapping(traffic.get('link_capacities_mbps')).items()}
    stage = {'present': isinstance(measurement, dict), 'traffic_mode': mode,
             'metrics': summary, 'flow_configurations': [row['configuration'] for row in flows.values()],
             'link_capacities_mbps': capacities,
             'warnings': sorted(set(warnings)),
             # Text preserves even malformed NaN/Infinity inputs without putting
             # nonfinite numbers in the report's normalized numeric fields.
             'raw_measurement_json': json.dumps(measurement, sort_keys=True, allow_nan=True)}
    return stage, flows, links


def _workload(before, after):
    warnings = []
    if before['traffic_mode'] and after['traffic_mode'] and before['traffic_mode'] != after['traffic_mode']:
        return 'Single-flow receiver rates and batch-window aggregates have different scopes.', warnings
    left = {_flow_key(row): row for row in before['flow_configurations']}
    right = {_flow_key(row): row for row in after['flow_configurations']}
    if left and right and set(left) != set(right):
        return 'Traffic endpoint/port sets differ.', warnings
    for key in left.keys() & right.keys():
        for field in ('offered_mbps', 'seconds'):
            a, b = left[key][field], right[key][field]
            if a is not None and b is not None and a != b:
                return 'Configured offered load or duration differs.', warnings
            if a is None or b is None:
                warnings.append('Offered load or requested duration metadata is incomplete.')
    if not left or not right:
        warnings.append('Flow metadata is incomplete; workload matching is unverified.')
    if (before['link_capacities_mbps'] and after['link_capacities_mbps']
            and before['link_capacities_mbps'] != after['link_capacities_mbps']):
        warnings.append('Capacity denominators differ; utilization deltas can reflect capacity settings.')
    return None, sorted(set(warnings))


def _finite_result(value):
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _change(before, after, mismatch=None):
    a, b = before['value'], after['value']
    result = {'before': a, 'after': b, 'unit': before['unit'],
              'before_status': before['status'], 'after_status': after['status'],
              'absolute_change': None, 'relative_change_percent': None,
              'percentage_point_change': None, 'status': 'unknown', 'reason': None,
              'relative_change_reason': None}
    if a is None or b is None:
        result['reason'] = 'Before or after measurement is missing, invalid, or unavailable.'
    elif mismatch:
        result.update(status='not_comparable', reason=mismatch)
    else:
        delta = _finite_result(b-a)
        result.update(absolute_change=delta, status='compared' if delta is not None else 'unknown')
        if delta is None:
            result['reason'] = 'Arithmetic overflow.'
        else:
            if before['unit'] == 'fraction':
                result['percentage_point_change'] = _finite_result(delta*100)
            if a == 0:
                result['relative_change_reason'] = 'Relative change is undefined for a zero baseline.'
            else:
                result['relative_change_percent'] = _finite_result(delta/a*100)
                if result['relative_change_percent'] is None:
                    result['relative_change_reason'] = 'Relative-change arithmetic overflow.'
    return result


def _comparisons(values, stages, names):
    result = {}
    for name, (before, after) in PAIRS.items():
        mismatch, warnings = _workload(stages[before], stages[after])
        result[name] = {'before_stage': before, 'after_stage': after, 'warnings': warnings,
                        'metrics': {metric: _change(values[before][metric], values[after][metric],
                                                   None if metric == 'idle_rtt_mean_ms' else mismatch)
                                    for metric in names}}
    return result


def compare_performance(healthy=None, impaired=None, post=None):
    """Compare supplied stage snapshots without altering inputs or acceptance."""
    normalized = {name: _normalize(value) for name, value in zip(STAGES, (healthy, impaired, post))}
    stages = {name: value[0] for name, value in normalized.items()}
    report = {'schema_version': 1,
              'scope': 'Descriptive recorded-data comparison; no experiment, improvement verdict, or acceptance decision.',
              'definitions': {'absolute_change': 'after - before, in the metric unit',
                              'relative_change_percent': '100 * (after - before) / before; undefined at zero',
                              'percentage_point_change': '100 * (after - before) for fraction metrics only'},
              'stages': stages,
              'comparisons': _comparisons({name: stage['metrics'] for name, stage in stages.items()}, stages, UNITS),
              'flows': [], 'links': []}
    flow_keys = set().union(*(set(value[1]) for value in normalized.values()))
    for key in sorted(flow_keys):
        rows = {name: value[1].get(key, {'configuration': None, 'metrics': _traffic_metrics({})})
                for name, value in normalized.items()}
        report['flows'].append({'identity': dict(zip(('source', 'destination', 'destination_port'), key)),
                                'stages': rows,
                                'comparisons': _comparisons({name: row['metrics'] for name, row in rows.items()},
                                                             stages, _traffic_metrics({}))})
    link_keys = set().union(*(set(value[2]) for value in normalized.values()))
    for key in sorted(link_keys):
        rows = {name: value[2].get(key, _metric(None, 'fraction')) for name, value in normalized.items()}
        report['links'].append({'interface': key, 'stages': rows,
                                'comparisons': _comparisons({name: {'utilization': row} for name, row in rows.items()},
                                                             stages, ('utilization',))})
    return report


def compare_trial_artifacts(directory):
    """Keep all attempts; select a final post stage only from recorded success."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError('Trial directory does not exist: '+str(directory))
    def read(path):
        if not path.exists():
            return None
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError('Expected a JSON object: '+str(path))
        return value
    healthy = read(directory / 'healthy-measurements.json')
    impaired = read(directory / 'impaired-measurements.json')
    result = read(directory / 'result.json') or {}
    attempts, selected, post = [], None, None
    paths = [path for path in directory.iterdir() if path.is_dir() and re.fullmatch(r'attempt-[1-9][0-9]*', path.name)]
    for path in sorted(paths, key=lambda p: int(p.name.split('-')[1])):
        index = int(path.name.split('-')[1])
        measurement = read(path / 'measurements.json')
        acceptance = read(path / 'acceptance.json')
        attempts.append({'attempt': index, 'recorded_acceptance': acceptance,
                         'comparison': compare_performance(healthy, impaired, measurement)})
        if (result.get('success') is True and type(result.get('attempts')) is int
                and result['attempts'] == index and acceptance and acceptance.get('accepted') is True
                and measurement is not None):
            selected, post = index, measurement
    return {'schema_version': 1, 'trial_id': result.get('trial_id'),
            'recorded_trial_success': result.get('success'), 'accepted_post_attempt': selected,
            'comparison': compare_performance(healthy, impaired, post), 'attempts': attempts}
