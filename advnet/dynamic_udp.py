"""Opt-in dynamic UDP transport using only owned, resource-limited sidecars.

Receivers must already be running. Client restarts implement rate changes.
All subprocess calls are bounded; Docker is never invoked by default.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
from ipaddress import IPv4Address
import json
import math
from pathlib import Path
import re
import subprocess
from uuid import uuid4

from .dynamic_scheduler import FlowExit, ScheduleInterrupted, SystemClock, run_schedule
from .dynamic_workload import DynamicFlow, RateChange, ResolvedWorkload
from .lab import Lab, udp_metrics
from .scenarios import MAX_CONCURRENT_FLOWS, validate_flow_endpoints


LABEL = 'advnet.dynamic-run'
LOCK_PATH = Path(__file__).resolve().parents[1] / 'runs/.abilene.lock'


class _Budget:
    def __init__(self, clock, seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('Timeout must be finite and positive')
        self.clock = clock
        self.last = clock.monotonic()
        if type(self.last) not in (int, float) or not math.isfinite(self.last):
            raise RuntimeError('Operation clock must return finite monotonic seconds')
        self.end = self.last + seconds

    def remaining(self):
        current = self.clock.monotonic()
        if type(current) not in (int, float) or not math.isfinite(current) or current < self.last:
            raise RuntimeError('Operation clock must return finite, nondecreasing monotonic seconds')
        self.last = current
        remaining = self.end - current
        if remaining <= 0:
            raise TimeoutError('Dynamic UDP operation deadline exceeded')
        return remaining


@contextmanager
def _lab_lock(path):
    """Share the existing experiment lock; never truncate or unlink it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('r' if path.exists() else 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Another Abilene experiment is running') from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class DynamicUdpBackend:
    """One run's Docker clients. Use run_dynamic_udp to acquire the lab lock.

    Detached docker start returns after launch; poll inspects container state.
    No attached CLI or pipe needs to wait for the flow lifetime. Registration
    precedes create, and every mutation targets an ownership-checked full ID.
    """
    def __init__(self, lab, *, allow_live=False, clock=None):
        if allow_live is not True:
            raise PermissionError('Dynamic UDP traffic is disabled; explicitly set allow_live=True')
        self.lab = lab
        self.clock = SystemClock() if clock is None else clock
        self.token = uuid4().hex
        self.segments, self.rate_changes = [], []
        self.active, self.flows, self.first_launch = {}, {}, {}
        self.closed = False

    def _run(self, args, budget, *, check=True):
        try:
            result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=budget.remaining())
        except subprocess.TimeoutExpired as error:
            raise TimeoutError('Docker '+args[0]+' timed out') from error
        if check and result.returncode:
            raise RuntimeError('Docker '+args[0]+' failed: '+(result.stderr or result.stdout)[-1500:])
        return result

    @staticmethod
    def _missing(result):
        return bool(result.returncode and re.search(r'no such (?:container|object)\b', result.stderr, re.I))

    def _inspect(self, segment, budget, *, missing_ok=False):
        identifier = segment['container_id'] or segment['container_name']
        result = self._run(['inspect', '--type', 'container', '--format', '{{json .}}', identifier],
                           budget, check=False)
        if self._missing(result) and missing_ok:
            return None
        if result.returncode:
            raise RuntimeError('Unable to inspect dynamic client: '+result.stderr[-1500:])
        value = json.loads(result.stdout)
        identifier = value['Id']
        if (not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{64}', identifier)
                or value['Config'].get('Labels', {}).get(LABEL) != self.token
                or segment['container_id'] not in (None, identifier)):
            raise RuntimeError('Refusing to mutate a container not owned by this dynamic run')
        segment['container_id'] = identifier
        segment['create_uncertain'] = False
        if type(value['State']['Running']) is not bool or type(value['State']['ExitCode']) is not int:
            raise ValueError('Invalid Docker client state')
        return value['State']

    def _receiver(self, flow, budget):
        destination = self.lab.routers[flow.destination]
        expression = f'sport = :{flow.destination_port}'
        try:
            listeners = self.lab.execute(destination['host'], 'ss', '-H', '-4', '-lnt', expression, deadline=budget)
            connected = self.lab.execute(destination['host'], 'ss', '-H', '-4', '-nt', 'state', 'established',
                                         expression, deadline=budget)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError('Receiver availability check timed out') from error
        addresses = (destination['host_ip'], '0.0.0.0', '*')
        if not any(re.search(r'(?:^|\s)'+re.escape(address)+':'+str(flow.destination_port)+r'(?:\s|$)', listeners)
                   for address in addresses):
            raise RuntimeError(f'No preexisting data-address receiver for {flow.destination}:{flow.destination_port}')
        return not connected.strip()

    def _wait_receiver(self, flow, budget):
        while not self._receiver(flow, budget):
            self.clock.sleep(min(0.05, budget.remaining()))

    def _validate_start(self, flow, offered_mbps):
        if self.closed:
            raise RuntimeError('Dynamic backend is closed')
        flow = DynamicFlow.model_validate(flow.model_dump() if isinstance(flow, DynamicFlow) else flow)
        rate = RateChange(at_seconds=0, offered_mbps=offered_mbps).offered_mbps
        validate_flow_endpoints([flow.model_dump()], self.lab.routers)
        source, destination = self.lab.routers[flow.source], self.lab.routers[flow.destination]
        if source['host'] == destination['host']:
            raise ValueError('Traffic endpoints must be distinct physical hosts')
        for endpoint in (source, destination):
            IPv4Address(endpoint['host_ip'])
        reserved = [segment for segment in self.segments if not segment['removed']]
        if len(reserved) >= MAX_CONCURRENT_FLOWS:
            raise ValueError(f'At most {MAX_CONCURRENT_FLOWS} concurrent clients are supported')
        validate_flow_endpoints([self.flows[segment['flow_id']].model_dump() for segment in reserved] + [flow.model_dump()],
                                self.lab.routers)
        return flow, rate

    def _launch(self, flow, rate, budget):
        flow, rate = self._validate_start(flow, rate)
        if not self._receiver(flow, budget):
            raise RuntimeError('Receiver is busy; unrelated clients and servers are left untouched')
        source, destination = self.lab.routers[flow.source], self.lab.routers[flow.destination]
        index = len(self.segments)
        segment = {'flow_id': flow.flow_id, 'segment_index': index,
                   'source': flow.source, 'destination': flow.destination,
                   'destination_port': flow.destination_port, 'offered_mbps': rate,
                   'container_name': f'advnet-dynamic-{self.token}-{index}', 'container_id': None,
                   'create_uncertain': False, 'removed': False, 'status': 'preparing',
                   'created_monotonic_seconds': None, 'removed_monotonic_seconds': None,
                   'start_requested_monotonic_seconds': None, 'start_returned_monotonic_seconds': None,
                   'stop_requested_monotonic_seconds': None, 'stop_confirmed_monotonic_seconds': None,
                   'returncode': None, 'raw_iperf_json': None, 'stderr': None,
                   'metrics': {'received_mbps': None, 'loss_fraction': None, 'jitter_ms': None,
                               'throughput_status': 'unavailable', 'loss_status': 'unavailable', 'jitter_status': 'unavailable'},
                   'measurement_error': 'No receiver report collected', 'error': None}
        self.segments.append(segment)  # Register before a possibly partial create.
        self.flows[flow.flow_id] = flow
        self.first_launch.setdefault(flow.flow_id, self.clock.monotonic())
        # Positive, integer safety duration; scheduled stop/restart normally wins.
        remaining = flow.stop_seconds - flow.start_seconds - (self.clock.monotonic() - self.first_launch[flow.flow_id])
        seconds = max(1, math.ceil(remaining) + 1)
        args = ['create', '--name', segment['container_name'], '--label', LABEL+'='+self.token,
                '--network', 'container:'+self.lab.prefix+source['host'], '--cap-drop', 'ALL',
                '--memory', '128m', '--pids-limit', '32', '--pull', 'never', 'advnet-host:0.1',
                'iperf3', '-c', destination['host_ip'], '-B', source['host_ip'],
                '-p', str(flow.destination_port), '-u', '-l', '1200', '-b', f'{rate}M',
                '-t', str(seconds), '--connect-timeout', '1000', '-i', '0', '-J', '--get-server-output']
        try:
            segment['create_uncertain'] = True
            created = self._run(args, budget, check=False)
            if created.returncode:
                segment['create_uncertain'] = False
                raise RuntimeError('Unable to create dynamic client: '+(created.stderr or created.stdout)[-1500:])
            identifier = created.stdout.strip()
            if not re.fullmatch(r'[a-f0-9]{64}', identifier):
                raise ValueError('Docker create did not return a full container ID')
            segment['container_id'] = identifier
            self._inspect(segment, budget)
            segment['create_uncertain'] = False
            segment['created_monotonic_seconds'] = self.clock.monotonic()
            segment['start_requested_monotonic_seconds'] = self.clock.monotonic()
            self._run(['start', identifier], budget)
            segment['start_returned_monotonic_seconds'] = self.clock.monotonic()
            segment['status'] = 'running'
            self.active[flow.flow_id] = segment
        except BaseException as error:
            segment['status'], segment['error'] = 'failed', type(error).__name__+': '+str(error)
            raise
        return segment

    def start(self, flow, offered_mbps, *, timeout_seconds):
        flow, rate = self._validate_start(flow, offered_mbps)
        if flow.flow_id in self.flows:
            raise ValueError('Dynamic flow IDs cannot be reused')
        self._launch(flow, rate, _Budget(self.clock, timeout_seconds))

    def _collect(self, segment, budget):
        logs = self._run(['logs', segment['container_id']], budget)
        segment['raw_iperf_json'], segment['stderr'] = logs.stdout, logs.stderr
        metrics = {'received_mbps': None, 'loss_fraction': None, 'jitter_ms': None,
                   'throughput_status': 'unavailable', 'loss_status': 'unavailable', 'jitter_status': 'unavailable'}
        segment['metrics'] = metrics
        segment['measurement_error'] = None
        try:
            data = json.loads(logs.stdout)
            values, receiver = udp_metrics(data)
            if (type(receiver['bits_per_second']) not in (int, float)
                    or type(receiver['lost_percent']) not in (int, float)
                    or type(values['received_mbps']) not in (int, float) or not 0 <= values['received_mbps'] < math.inf
                    or type(values['loss_fraction']) not in (int, float) or not 0 <= values['loss_fraction'] <= 1):
                raise ValueError('Invalid receiver throughput or loss')
            segment['metrics'] = {**values, 'throughput_status': 'measured', 'loss_status': 'measured'}
        except (ValueError, TypeError, KeyError, AttributeError, RuntimeError) as error:
            segment['measurement_error'] = type(error).__name__+': '+str(error)

    def poll(self, flow_id, *, timeout_seconds):
        budget = _Budget(self.clock, timeout_seconds)
        segment = self.active[flow_id]
        state = self._inspect(segment, budget)
        if state['Running']:
            return None
        segment['returncode'], segment['status'] = state['ExitCode'], 'unexpected_exit'
        self._collect(segment, budget)
        return FlowExit(returncode=state['ExitCode'], error=state.get('Error') or segment['measurement_error'])

    def _remove(self, segment, budget):
        state = self._inspect(segment, budget, missing_ok=True)
        if state is None:
            if segment['create_uncertain']:
                raise RuntimeError('Timed-out create remains unconfirmed; cannot establish cleanup')
        else:
            removed = self._run(['rm', '-f', segment['container_id']], budget, check=False)
            if removed.returncode and not self._missing(removed):
                raise RuntimeError('Unable to remove owned client: '+removed.stderr[-1500:])
        segment['removed'] = True
        segment['removed_monotonic_seconds'] = self.clock.monotonic()
        self.active.pop(segment['flow_id'], None)

    def _finish(self, segment, budget, *, scheduled):
        state = self._inspect(segment, budget)
        was_running = state['Running']
        segment['stop_requested_monotonic_seconds'] = self.clock.monotonic()
        if was_running:
            self._run(['kill', '--signal=SIGINT', segment['container_id']], budget)
            while True:
                state = self._inspect(segment, budget)
                if not state['Running']:
                    break
                self.clock.sleep(min(0.05, budget.remaining()))
        segment['stop_confirmed_monotonic_seconds'] = self.clock.monotonic()
        segment['returncode'] = state['ExitCode']
        self._collect(segment, budget)
        self._remove(segment, budget)
        segment['status'] = 'stopped' if scheduled else 'cancelled'
        if scheduled:
            if not was_running or state.get('OOMKilled') or state['ExitCode'] not in (0, 1, 130):
                segment['status'] = 'unexpected_exit'
                raise RuntimeError(f'Dynamic client exited unexpectedly: {state["ExitCode"]}')
            # Interrupt output can lack receiver statistics; never invent them.
            try:
                reported_error = json.loads(segment['raw_iperf_json']).get('error')
            except (ValueError, AttributeError):
                reported_error = None
            if reported_error and not str(reported_error).startswith('interrupt - '):
                segment['status'] = 'failed'
                raise RuntimeError('Traffic client failed: '+str(reported_error))
            self._wait_receiver(self.flows[segment['flow_id']], budget)

    def stop(self, flow_id, *, timeout_seconds):
        self._finish(self.active[flow_id], _Budget(self.clock, timeout_seconds), scheduled=True)

    def set_rate(self, flow_id, offered_mbps, *, timeout_seconds):
        if self.closed:
            raise RuntimeError('Dynamic backend is closed')
        rate = RateChange(at_seconds=0, offered_mbps=offered_mbps).offered_mbps
        budget = _Budget(self.clock, timeout_seconds)
        previous = self.active[flow_id]
        change = {'flow_id': flow_id, 'offered_mbps': rate, 'previous_segment_index': previous['segment_index'],
                  'next_segment_index': None, 'requested_monotonic_seconds': self.clock.monotonic(),
                  'finished_monotonic_seconds': None, 'restart_control_seconds': None,
                  'minimum_no_client_seconds': None, 'traffic_gap_seconds': None,
                  'gap_status': 'unverified', 'error': None}
        self.rate_changes.append(change)
        try:
            self._finish(previous, budget, scheduled=True)
            next_segment = self._launch(self.flows[flow_id], rate, budget)
            change['next_segment_index'] = next_segment['segment_index']
            change['minimum_no_client_seconds'] = (next_segment['start_requested_monotonic_seconds']
                                                    - previous['stop_confirmed_monotonic_seconds'])
        except BaseException as error:
            change['gap_status'], change['error'] = 'incomplete', type(error).__name__+': '+str(error)
            raise
        finally:
            change['finished_monotonic_seconds'] = self.clock.monotonic()
            change['restart_control_seconds'] = change['finished_monotonic_seconds'] - change['requested_monotonic_seconds']

    def cleanup(self, *, timeout_seconds):
        self.closed = True
        budget = _Budget(self.clock, timeout_seconds)
        pending = [segment for segment in self.segments if not segment['removed']]
        errors = []
        interruption = None
        for index, segment in enumerate(pending):
            try:
                # Reserve time for all clients and a force-removal fallback.
                share = budget.remaining() / (len(pending) - index)
                graceful = _Budget(self.clock, share / 2)
                try:
                    state = self._inspect(segment, graceful, missing_ok=True)
                    if state is not None:
                        self._finish(segment, graceful, scheduled=False)
                    else:
                        self._remove(segment, graceful)
                except BaseException as error:
                    if not isinstance(error, Exception) and interruption is None:
                        interruption = error
                    segment['error'] = segment['error'] or type(error).__name__+': '+str(error)
                    self._remove(segment, _Budget(self.clock, min(share / 2, budget.remaining())))
                    segment['status'] = 'force_removed'
            except BaseException as error:
                if not isinstance(error, Exception) and interruption is None:
                    interruption = error
                errors.append(segment['container_name']+': '+type(error).__name__+': '+str(error))
        if interruption is not None:
            if errors:
                interruption.add_note('Other dynamic cleanup errors: '+'; '.join(errors))
            raise interruption
        if errors:
            raise RuntimeError('Dynamic client cleanup failed: '+'; '.join(errors))

    def evidence(self, origin):
        """Convert transport timestamps to the scheduler's experiment timeline."""
        def relative(record):
            result = {}
            for key, value in deepcopy(record).items():
                if key.endswith('_monotonic_seconds'):
                    key = key.removesuffix('_monotonic_seconds')+'_seconds'
                    value = value - origin if value is not None else None
                result[key] = value
            return result
        return {'segments': [relative(segment) for segment in self.segments],
                'rate_changes': [relative(change) for change in self.rate_changes]}


def run_dynamic_udp(schedule, manifest, *, allow_live=False, clock=None, lock_path=LOCK_PATH, **scheduler_options):
    """Opt-in library integration; no CLI, shaping, routing writes, or inference.

    The shared experiment lock excludes cooperating static/dynamic collectors.
    External clients are checked but cannot be atomically excluded; iperf busy
    errors abort without signalling any server. Result timing is control timing,
    not proof of packet overlap or throughput. Interruptions retain UDP evidence.
    """
    if allow_live is not True:
        raise PermissionError('Dynamic UDP traffic is disabled; explicitly set allow_live=True')
    value = schedule.model_dump() if isinstance(schedule, ResolvedWorkload) else schedule
    resolved = ResolvedWorkload.model_validate(value).validate_inventory(manifest)
    clock = SystemClock() if clock is None else clock
    backend = DynamicUdpBackend(Lab(manifest), allow_live=True, clock=clock)
    with _lab_lock(lock_path):
        try:
            result = run_schedule(resolved, manifest, backend, clock=clock, **scheduler_options)
        except (ScheduleInterrupted, SystemExit) as error:
            result = error.result if isinstance(error, ScheduleInterrupted) else error.schedule_result
            error.traffic_result = {'scheduler': result.model_dump(mode='json'),
                                    **backend.evidence(result.started_monotonic_seconds)}
            raise
    return {'scheduler': result.model_dump(mode='json'), **backend.evidence(result.started_monotonic_seconds)}
