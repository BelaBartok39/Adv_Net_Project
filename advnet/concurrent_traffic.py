"""Bounded UDP batches using owned client sidecars and one counter window."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import subprocess
from threading import Event, Lock
from time import monotonic
from uuid import uuid4

from .lab import loaded_rtt, udp_metrics
from .scenarios import ConcurrentFlow, MAX_CONCURRENT_FLOWS, validate_flow_endpoints


def validate_flows(lab, flows):
    if not isinstance(flows, list) or not 2 <= len(flows) <= MAX_CONCURRENT_FLOWS:
        raise ValueError('Concurrent traffic requires an array of 2–4 flows')
    normalized = [ConcurrentFlow.model_validate(flow).model_dump() for flow in flows]
    validate_flow_endpoints(normalized,lab.routers)
    return normalized


def _count(value):
    return value if type(value) is int and value >= 0 else None


def summarize_batch(lab, records, before, after, elapsed):
    """Use receiver byte/packet totals and the same window as interface counters."""
    if not 0 < elapsed < math.inf:
        raise ValueError('Invalid concurrent measurement duration')
    windows = [record['sender_window'] for record in records]
    overlap = None
    if all(window is not None for window in windows):
        start = max(window['start_seconds'] for window in windows)
        end = min(window['end_seconds'] for window in windows)
        if start < end:
            overlap = {'start_seconds': start, 'end_seconds': end, 'duration_seconds': end-start}
    bytes_known = all(record['received_bytes'] is not None for record in records)
    counts_known = all(record['packets'] is not None and record['lost_packets'] is not None
                       for record in records)
    total_bytes = sum(record['received_bytes'] for record in records) if bytes_known else None
    packets = sum(record['packets'] for record in records) if counts_known else None
    lost = sum(record['lost_packets'] for record in records) if counts_known else None
    utilization = lab._link_utilization(before, after, elapsed)
    return {'mode': 'concurrent_udp', 'flow_count': len(records), 'flows': records,
            'duration_seconds': elapsed, 'concurrency_verified': overlap is not None,
            'overlap_window': overlap,
            'overlap_reason': None if overlap else 'No common verified sender interval; simultaneous traffic is not established.',
            'aggregate': {
                'received_bytes': total_bytes,
                'received_mbps': total_bytes*8/elapsed/1e6 if total_bytes is not None else None,
                'loss_fraction': lost/packets if packets else None,
                'packets': packets, 'lost_packets': lost,
                'throughput_status': 'measured' if bytes_known else 'unavailable',
                'loss_status': 'measured' if packets else 'unavailable',
                'window': 'entire batch, including shorter-flow completion and client setup/reporting',
            },
            'link_utilization': utilization, 'max_link_utilization': max(utilization.values()),
            'counter_window': {'start_seconds': 0, 'end_seconds': elapsed,
                               'before': before, 'after': after},
            'capacities_enforced': lab.shaped, 'link_capacities_mbps': dict(lab.capacities)}


def run_concurrent_traffic(lab, flows, deadline):
    flows = validate_flows(lab, flows)  # Reject conflicts before any Docker operation.
    token = uuid4().hex
    names = [f'advnet-udp-{token}-{index}' for index in range(len(flows))]
    attempted, processes = [], {}
    gate, cancelled, lock = Event(), Event(), Lock()
    pool, failure = None, None

    def run_client(index, flow):
        gate.wait()
        with lock:
            if cancelled.is_set():
                raise RuntimeError('Concurrent traffic cancelled before client start')
            started = monotonic()
            process = subprocess.Popen(['docker', 'start', '-a', names[index]],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes[index] = process
        try:
            stdout, stderr = process.communicate(timeout=min(flow['seconds']+15, deadline.remaining()))
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.communicate()
            raise
        finished = monotonic()
        if process.returncode:
            raise RuntimeError(f'Concurrent flow {index} failed: {(stderr or stdout)[-1000:]}')
        data = json.loads(stdout)
        metrics, receiver = udp_metrics(data)
        if (type(receiver['bits_per_second']) not in (int, float)
                or type(receiver['lost_percent']) not in (int, float)
                or type(metrics['received_mbps']) not in (int, float)
                or not 0 <= metrics['received_mbps'] < math.inf
                or type(metrics['loss_fraction']) not in (int, float)
                or not 0 <= metrics['loss_fraction'] <= 1):
            raise ValueError(f'Concurrent flow {index} has invalid receiver statistics')
        timing = loaded_rtt(data, [], finished-started)
        window = timing['window']
        offset = started - window_start
        if window is not None:
            window = {**window, 'start_seconds': window['start_seconds']+offset,
                      'end_seconds': window['end_seconds']+offset}
        packets, lost = _count(receiver.get('packets')), _count(receiver.get('lost_packets'))
        if packets is not None and lost is not None and lost > packets:
            packets, lost = None, None
        return {**flow, **metrics, 'flow_index': index, 'udp_datagram_bytes': 1200,
                'client_container': names[index],
                'start_seconds': offset, 'end_seconds': finished-window_start,
                'duration_seconds': finished-started, 'sender_window': window,
                'timing_reason': timing['reason'] if timing['window'] is None else None,
                'received_bytes': _count(receiver.get('bytes')),
                'packets': packets, 'lost_packets': lost,
                'raw_iperf_json': stdout, 'stderr': stderr}

    try:
        # Create synchronously, then release all starts together. Separate PID
        # namespaces make force-removal kill exactly our clients, even if a local
        # Docker CLI timed out. No anonymous iperf processes are left in hosts.
        for index, flow in enumerate(flows):
            attempted.append(names[index])
            source, destination = lab.routers[flow['source']], lab.routers[flow['destination']]
            argv = ['docker', 'create', '--name', names[index], '--label', 'advnet.udp-run='+token,
                    '--network', 'container:'+lab.prefix+source['host'], '--cap-drop', 'ALL',
                    '--memory', '128m', '--pids-limit', '32', 'advnet-host:0.1',
                    'iperf3', '-c', destination['host_ip'], '-B', source['host_ip'],
                    '-p', str(flow['destination_port']), '-u', '-l', '1200',
                    '-b', f'{flow["offered_mbps"]}M', '-t', str(flow['seconds']),
                    '-J', '--get-server-output']
            result = subprocess.run(argv, capture_output=True, text=True,
                                    timeout=min(30, deadline.remaining()))
            if result.returncode:
                raise RuntimeError('Unable to prepare concurrent client: '+(result.stderr or result.stdout)[-1000:])
        pool = ThreadPoolExecutor(max_workers=len(flows), thread_name_prefix='advnet-udp')
        futures = [pool.submit(run_client, index, flow) for index, flow in enumerate(flows)]
        before = lab.counters(deadline)
        window_start = monotonic()
        gate.set()
        records = [future.result() for future in as_completed(futures, timeout=deadline.remaining())]
        window_end = monotonic()
        after = lab.counters(deadline)
        records.sort(key=lambda record: record['flow_index'])
        return summarize_batch(lab, records, before, after, window_end-window_start)
    except BaseException as error:
        failure = error
        raise
    finally:
        cancelled.set()
        gate.set()
        # Registration and cancellation share a lock so no local child can
        # appear after this snapshot. Workers drain pipes and reap killed CLIs.
        with lock:
            for process in processes.values():
                if process.poll() is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
        cleanup_errors = []
        for name in reversed(attempted):
            try:
                inspected = subprocess.run(
                    ['docker', 'inspect', '--format', '{{ index .Config.Labels "advnet.udp-run" }}', name],
                    capture_output=True, text=True, timeout=10)
                if inspected.returncode:
                    if 'No such' not in inspected.stderr:
                        cleanup_errors.append(name+': '+inspected.stderr)
                    continue
                if inspected.stdout.strip() != token:
                    cleanup_errors.append(name+': refusing to remove a container not owned by this batch')
                    continue
                result = subprocess.run(['docker', 'rm', '-f', name], capture_output=True,
                                        text=True, timeout=10)
                if result.returncode and 'No such container' not in result.stderr:
                    cleanup_errors.append(name+': '+result.stderr)
            except (OSError, subprocess.TimeoutExpired) as error:
                cleanup_errors.append(name+': '+str(error))
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        if cleanup_errors:
            message = 'Concurrent client cleanup failed: '+'; '.join(cleanup_errors)
            if failure is not None:
                failure.add_note(message)
            else:
                raise RuntimeError(message)
