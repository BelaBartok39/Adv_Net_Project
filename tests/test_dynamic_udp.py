"""Offline iperf transport tests: every Docker operation is subprocess-mocked."""
from copy import deepcopy
import fcntl
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from advnet.dynamic_scheduler import ScheduleInterrupted
from advnet.dynamic_udp import DynamicUdpBackend, LABEL, run_dynamic_udp
from advnet.dynamic_workload import DynamicFlow, resolve_workload
from advnet.lab import Lab
from test_dynamic_scheduler import FakeClock


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / 'topologies/abilene.json').read_text())
REPORT = json.loads((ROOT / 'tests/fixtures/iperf3/udp-json-server-received.json').read_text())


def flow(flow_id='a', **overrides):
    return {'flow_id': flow_id, 'source': 'sea', 'destination': 'sva', 'offered_mbps': 5,
            'start_seconds': 0, 'stop_seconds': 8, **overrides}


def schedule(flows=None, duration=10):
    return resolve_workload({'duration_seconds': duration, 'flows': [flow()] if flows is None else flows}, MANIFEST)


class DockerHarness:
    """Small stateful Docker substitute, retaining unrelated resources."""
    def __init__(self, clock):
        self.clock = clock
        self.calls, self.creates, self.signals, self.removes = [], [], [], []
        self.containers = {}
        self.receivers = {(node, 5201) for node in MANIFEST['routers']}
        self.busy, self.release_at = set(), {}
        self.effects, self.delays = {}, {}
        self.stop_delay, self.receiver_delay = 0, 0
        self.stall, self.foreign, self.oom = set(), set(), set()
        self.reports = {}
        self.peak_active = 0
        self.unrelated = {'server-iperf3', 'someone-elses-client'}

    def endpoint(self, container):
        return container['destination'], container['port']

    def update(self):
        for container in self.containers.values():
            stop_at = container.get('stop_at')
            if stop_at is not None and self.clock.elapsed >= stop_at:
                container['running'] = False
                container['stop_at'] = None
                self.release_at[self.endpoint(container)] = self.clock.elapsed + self.receiver_delay

    def locate(self, identifier):
        return self.containers.get(identifier) or next(
            (container for container in self.containers.values() if container['name'] == identifier), None)

    def __call__(self, argv, **options):
        if argv[0] != 'docker' or options.get('shell') or options['timeout'] <= 0:
            raise AssertionError('Unexpected command: '+repr(argv))
        action = argv[1]
        self.calls.append((deepcopy(argv), options['timeout'], self.clock.elapsed))
        self.clock.now += self.delays.get(action, 0)
        self.update()
        effect = self.effects.get(action)
        if callable(effect):
            effect = effect(argv, options)
        if isinstance(effect, BaseException):
            raise effect
        if isinstance(effect, subprocess.CompletedProcess):
            return effect

        def result(code=0, stdout='', stderr=''):
            return subprocess.CompletedProcess(argv, code, stdout, stderr)

        if action == 'exec':
            if argv[3] != 'ss':
                raise AssertionError('Host operation other than read-only ss: '+repr(argv))
            node = next(node for node, router in MANIFEST['routers'].items()
                        if argv[2] == 'clab-advnet-abilene-'+router['host'])
            port = int(argv[-1].rsplit(':', 1)[1])
            endpoint = node, port
            if '-lnt' in argv:
                return result(stdout=(f'LISTEN 0 4096 {MANIFEST["routers"][node]["host_ip"]}:{port} 0.0.0.0:*\n'
                                      if endpoint in self.receivers else ''))
            occupied = (endpoint in self.busy or self.release_at.get(endpoint, 0) > self.clock.elapsed
                        or any(container['running'] and self.endpoint(container) == endpoint
                               for container in self.containers.values()))
            return result(stdout='0 0 established-control-socket\n' if occupied else '')
        if action == 'create':
            self.creates.append(deepcopy(argv))
            identifier = f'{len(self.creates):064x}'
            destination_ip = argv[argv.index('-c') + 1]
            destination = next(node for node, router in MANIFEST['routers'].items()
                               if router['host_ip'] == destination_ip)
            self.containers[identifier] = {
                'id': identifier, 'name': argv[argv.index('--name') + 1],
                'label': argv[argv.index('--label') + 1].split('=', 1)[1],
                'running': False, 'started': False, 'returncode': 0,
                'destination': destination, 'port': int(argv[argv.index('-p') + 1])}
            return result(stdout=identifier+'\n')
        container = self.locate(argv[-1])
        if container is None:
            return result(1, stderr='Error: No such container: '+argv[-1])
        identifier = container['id']
        if action == 'inspect':
            return result(stdout=json.dumps({
                'Id': identifier, 'Config': {'Labels': {LABEL: 'foreign' if identifier in self.foreign else container['label']}},
                'State': {'Running': container['running'], 'ExitCode': container['returncode'],
                          'OOMKilled': identifier in self.oom, 'Error': ''}}))
        if action == 'start':
            self.assert_full_id(argv[-1])
            container['running'], container['started'] = True, True
            self.peak_active = max(self.peak_active, sum(c['running'] for c in self.containers.values()))
            return result(stdout=identifier+'\n')
        if action == 'kill':
            self.assert_full_id(argv[-1])
            if argv[2] != '--signal=SIGINT':
                raise AssertionError('Unexpected signal')
            self.signals.append(identifier)
            if identifier not in self.stall:
                container['stop_at'] = self.clock.elapsed + self.stop_delay
                self.update()
            return result(stdout=identifier+'\n')
        if action == 'logs':
            value = self.reports.get(identifier, REPORT)
            return result(stdout=json.dumps(value) if isinstance(value, dict) else value, stderr='synthetic stderr')
        if action == 'rm':
            self.assert_full_id(argv[-1])
            if argv[2] != '-f':
                raise AssertionError('Unexpected removal')
            self.removes.append(identifier)
            del self.containers[identifier]
            return result(stdout=identifier+'\n')
        raise AssertionError('Unexpected Docker operation: '+repr(argv))

    @staticmethod
    def assert_full_id(value):
        if len(value) != 64 or any(char not in '0123456789abcdef' for char in value):
            raise AssertionError('Mutation used a name rather than an immutable ID')


class DynamicUdpTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(origin=100)
        self.docker = DockerHarness(self.clock)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.lock = Path(self.temporary.name) / '.abilene.lock'

    def execute(self, resolved=None, **options):
        with patch('subprocess.run', side_effect=self.docker), \
                patch('subprocess.Popen', side_effect=AssertionError('Attached client is not needed')):
            return run_dynamic_udp(schedule() if resolved is None else resolved, MANIFEST,
                                   allow_live=True, clock=self.clock, lock_path=self.lock,
                                   poll_interval_seconds=0.25, **options)

    def test_live_execution_is_disabled_before_any_file_or_network_operation(self):
        with patch('subprocess.run') as run:
            for enabled in (False, None, 1, 'yes'):
                with self.subTest(enabled=enabled), self.assertRaises(PermissionError):
                    run_dynamic_udp(schedule(), MANIFEST, allow_live=enabled, lock_path=self.lock)
                with self.assertRaises(PermissionError):
                    DynamicUdpBackend(Lab(MANIFEST), allow_live=enabled)
            run.assert_not_called()
        self.assertFalse(self.lock.exists())

    def test_single_flow_reuses_inventory_image_receiver_metrics_and_data_binding(self):
        result = self.execute()
        self.assertEqual(result['scheduler']['status'], 'completed')
        self.assertTrue(result['scheduler']['cleanup_succeeded'])
        self.assertEqual(result['scheduler']['started_monotonic_seconds'], 100)
        argv = self.docker.creates[0]
        self.assertEqual(argv[argv.index('--network')+1], 'container:clab-advnet-abilene-h-sea')
        for flag, value in (('-c', '10.64.2.10'), ('-B', '10.64.1.10'), ('-p', '5201'),
                            ('-l', '1200'), ('-b', '5.0M'), ('--memory', '128m'),
                            ('--pids-limit', '32'), ('--cap-drop', 'ALL'), ('--pull', 'never')):
            self.assertEqual(argv[argv.index(flag)+1], value)
        self.assertIn('advnet-host:0.1', argv)
        self.assertIn('-u', argv)
        self.assertIn('--get-server-output', argv)
        self.assertGreater(int(argv[argv.index('-t')+1]), 8)
        segment = result['segments'][0]
        self.assertEqual(segment['start_requested_seconds'], 0)
        self.assertEqual(segment['stop_confirmed_seconds'], 8)
        self.assertEqual(segment['metrics']['received_mbps'], 4.7)
        self.assertEqual(segment['metrics']['jitter_ms'], 1.75)
        self.assertEqual(segment['metrics']['loss_fraction'], 0.01)
        self.assertEqual(json.loads(segment['raw_iperf_json']), REPORT)
        self.assertTrue(segment['removed'])
        self.assertFalse(self.docker.containers)
        self.assertEqual(self.docker.unrelated, {'server-iperf3', 'someone-elses-client'})
        json.dumps(result, allow_nan=False)

    def test_four_simultaneous_flows_are_resource_limited_and_cleanup_only_their_clients(self):
        for port in (5202, 5203, 5204):
            self.docker.receivers.add(('sva', port))
        result = self.execute(schedule([flow(str(i), destination_port=5201+i) for i in range(4)]))
        self.assertEqual(result['scheduler']['status'], 'completed')
        self.assertEqual(self.docker.peak_active, 4)
        self.assertEqual(len(set(self.docker.signals)), 4)
        self.assertEqual(len(set(self.docker.removes)), 4)
        self.assertFalse(self.docker.containers)

    def test_dynamic_arrivals_departures_and_same_timestamp_port_reuse(self):
        result = self.execute(schedule([flow('first', stop_seconds=3),
                                        flow('second', start_seconds=3, stop_seconds=7),
                                        flow('overlap', destination='nyc', start_seconds=1, stop_seconds=5)]))
        self.assertEqual(result['scheduler']['status'], 'completed')
        self.assertEqual(self.docker.peak_active, 2)
        self.assertEqual([s['start_requested_seconds'] for s in result['segments']], [0, 1, 3])
        self.assertEqual([s['stop_confirmed_seconds'] for s in result['segments']], [3, 5, 7])

    def test_sustained_changes_and_bursts_restart_clients_and_preserve_effective_rates(self):
        result = self.execute(schedule([flow(
            rate_changes=[{'at_seconds': 2, 'offered_mbps': 10}, {'at_seconds': 4, 'offered_mbps': 15}],
            bursts=[{'start_seconds': 3, 'stop_seconds': 6, 'offered_mbps': 40}])]))
        self.assertEqual(result['scheduler']['status'], 'completed')
        self.assertEqual([s['offered_mbps'] for s in result['segments']], [5, 10, 40, 15])
        self.assertEqual(len(result['rate_changes']), 3)
        self.assertEqual(len(set(self.docker.signals)), 4)
        self.assertTrue(all(change['traffic_gap_seconds'] is None for change in result['rate_changes']))
        self.assertTrue(all(change['gap_status'] == 'unverified' for change in result['rate_changes']))

    def test_restart_records_control_delay_and_a_verified_minimum_without_claiming_packet_gap(self):
        self.docker.delays['create'] = 0.25
        self.docker.stop_delay = 0.25
        result = self.execute(schedule([flow(rate_changes=[{'at_seconds': 2, 'offered_mbps': 10}])]))
        self.assertEqual(result['scheduler']['status'], 'completed')
        change = result['rate_changes'][0]
        self.assertGreaterEqual(change['restart_control_seconds'], 0.5 - 1e-10)
        self.assertAlmostEqual(change['minimum_no_client_seconds'], 0.25)
        self.assertIsNone(change['traffic_gap_seconds'])

    def test_existing_receiver_release_is_waited_for_without_replacing_it(self):
        self.docker.receiver_delay = 0.125
        result = self.execute(schedule([flow(rate_changes=[{'at_seconds': 2, 'offered_mbps': 10}])]))
        self.assertEqual(result['scheduler']['status'], 'completed')
        self.assertGreaterEqual(result['rate_changes'][0]['minimum_no_client_seconds'], 0.125)
        self.assertTrue(all(call[0][3] == 'ss' for call in self.docker.calls if call[0][1] == 'exec'))

    def test_missing_or_busy_receiver_fails_before_client_creation(self):
        for mode in ('missing', 'busy'):
            with self.subTest(mode=mode):
                self.setUp()
                if mode == 'missing':
                    self.docker.receivers.remove(('sva', 5201))
                else:
                    self.docker.busy.add(('sva', 5201))
                result = self.execute()
                self.assertEqual(result['scheduler']['status'], 'failed')
                self.assertTrue(result['scheduler']['cleanup_succeeded'])
                self.assertFalse(self.docker.creates)
                self.assertFalse(self.docker.signals)

    def test_alternate_port_requires_preexisting_receiver(self):
        result = self.execute(schedule([flow(destination_port=5202)]))
        self.assertEqual(result['scheduler']['status'], 'failed')
        self.assertFalse(self.docker.creates)

    def test_bad_resolved_schedule_rejects_before_docker(self):
        for mode in ('unknown', 'conflict', 'ordering'):
            bad = schedule().model_dump()
            if mode == 'unknown':
                bad['flows'][0]['source'] = 'missing'
            elif mode == 'conflict':
                bad['flows'].append({**bad['flows'][0], 'flow_id': 'b'})
            else:
                bad['events'][0]['sequence'] = 2
            with self.subTest(mode=mode), patch('subprocess.run') as run, self.assertRaises(ValueError):
                run_dynamic_udp(bad, MANIFEST, allow_live=True, lock_path=self.lock)
            run.assert_not_called()

    def test_low_level_backend_validates_conflicts_rates_and_resource_limit_before_docker(self):
        backend = DynamicUdpBackend(Lab(MANIFEST), allow_live=True, clock=self.clock)
        with patch('subprocess.run', side_effect=self.docker):
            backend.start(DynamicFlow(**flow()), 5, timeout_seconds=5)
            count = len(self.docker.calls)
            for candidate, rate in ((flow('b'), 5), (flow('b', destination='unknown'), 5),
                                    (flow('b', destination='nyc'), 0)):
                with self.subTest(candidate=candidate, rate=rate), self.assertRaises(ValueError):
                    backend.start(DynamicFlow(**candidate), rate, timeout_seconds=5)
            self.assertEqual(len(self.docker.calls), count)
            for index, destination in enumerate(('nyc', 'den', 'lax')):
                backend.start(DynamicFlow(**flow(str(index), destination=destination)), 5, timeout_seconds=5)
            count = len(self.docker.calls)
            with self.assertRaisesRegex(ValueError, 'At most 4'):
                backend.start(DynamicFlow(**flow('fifth', destination='hou')), 5, timeout_seconds=5)
            self.assertEqual(len(self.docker.calls), count)
            backend.cleanup(timeout_seconds=10)

    def test_create_failure_cleans_up_already_active_sibling(self):
        def fail_second(argv, options):
            if len(self.docker.creates) == 1:
                return subprocess.CompletedProcess(argv, 1, '', 'synthetic create failure')
        self.docker.effects['create'] = fail_second
        result = self.execute(schedule([flow(), flow('b', destination='nyc', start_seconds=1)]))
        self.assertEqual(result['scheduler']['status'], 'failed')
        self.assertTrue(result['scheduler']['cleanup_succeeded'])
        self.assertFalse(self.docker.containers)
        self.assertIn('synthetic create failure', result['scheduler']['error']['message'])

    def test_create_timeout_can_recover_confirmed_owned_container_by_name(self):
        original = self.docker
        def timeout_after_create(argv, **options):
            value = original(argv, **options)
            if argv[1] == 'create':
                raise subprocess.TimeoutExpired(argv, options['timeout'])
            return value
        with patch('subprocess.run', side_effect=timeout_after_create):
            result = run_dynamic_udp(schedule(), MANIFEST, allow_live=True, clock=self.clock, lock_path=self.lock)
        self.assertEqual(result['scheduler']['status'], 'timed_out')
        self.assertTrue(result['scheduler']['cleanup_succeeded'])
        self.assertFalse(self.docker.containers)

    def test_create_timeout_with_missing_container_is_unconfirmed_not_successful_cleanup(self):
        self.docker.effects['create'] = subprocess.TimeoutExpired('docker create', 5)
        result = self.execute()
        self.assertEqual(result['scheduler']['status'], 'timed_out')
        self.assertFalse(result['scheduler']['cleanup_succeeded'])
        self.assertIn('unconfirmed', result['scheduler']['cleanup_error']['message'])

    def test_start_failure_and_partial_start_timeout_are_cleaned(self):
        for mode in ('failure', 'timeout'):
            with self.subTest(mode=mode):
                self.setUp()
                if mode == 'failure':
                    self.docker.effects['start'] = subprocess.CompletedProcess([], 1, '', 'start failed')
                    result = self.execute()
                else:
                    original = self.docker
                    def timeout_after_start(argv, **options):
                        value = original(argv, **options)
                        if argv[1] == 'start':
                            raise subprocess.TimeoutExpired(argv, options['timeout'])
                        return value
                    with patch('subprocess.run', side_effect=timeout_after_start):
                        result = run_dynamic_udp(schedule(), MANIFEST, allow_live=True,
                                                 clock=self.clock, lock_path=self.lock)
                self.assertIn(result['scheduler']['status'], ('failed', 'timed_out'))
                self.assertTrue(result['scheduler']['cleanup_succeeded'])
                self.assertFalse(self.docker.containers)

    def test_early_client_exit_is_detected_and_preserves_partial_output(self):
        def exit_client():
            for container in self.docker.containers.values():
                container['running'], container['returncode'] = False, 1
        self.clock.on_sleep = exit_client
        result = self.execute()
        self.assertEqual(result['scheduler']['status'], 'failed')
        self.assertEqual(len(result['scheduler']['flow_exits']), 1)
        self.assertIsNotNone(result['segments'][0]['raw_iperf_json'])
        self.assertFalse(self.docker.containers)

    def test_stuck_client_times_out_then_cleanup_force_removes_only_owned_container(self):
        self.docker.stall.add(f'{1:064x}')
        result = self.execute(operation_timeout_seconds=0.5, cleanup_timeout_seconds=2)
        self.assertEqual(result['scheduler']['status'], 'timed_out')
        self.assertTrue(result['scheduler']['cleanup_succeeded'])
        self.assertEqual(result['segments'][0]['status'], 'force_removed')
        self.assertFalse(self.docker.containers)

    def test_cleanup_attempts_other_clients_after_one_removal_fails(self):
        def fail_one(argv, options):
            if argv[-1] == f'{1:064x}':
                return subprocess.CompletedProcess(argv, 1, '', 'refused removal')
        self.docker.effects['rm'] = fail_one
        result = self.execute(schedule([flow(), flow('b', destination='nyc')]),
                              cancelled=lambda: self.clock.elapsed >= 1)
        self.assertEqual(result['scheduler']['status'], 'cancelled')
        self.assertFalse(result['scheduler']['cleanup_succeeded'])
        self.assertIn(f'{1:064x}', self.docker.containers)
        self.assertNotIn(f'{2:064x}', self.docker.containers)
        self.assertIn('refused removal', result['scheduler']['cleanup_error']['message'])

    def test_ownership_mismatch_never_signals_or_removes_the_foreign_container(self):
        self.clock.on_sleep = lambda: self.docker.foreign.add(f'{1:064x}')
        result = self.execute()
        self.assertEqual(result['scheduler']['status'], 'failed')
        self.assertFalse(result['scheduler']['cleanup_succeeded'])
        self.assertFalse(self.docker.signals)
        self.assertFalse(self.docker.removes)

    def test_failed_rate_restart_is_partial_and_does_not_claim_a_new_rate(self):
        def fail_restart(argv, options):
            if len(self.docker.creates) == 1:
                return subprocess.CompletedProcess(argv, 1, '', 'restart failed')
        self.docker.effects['create'] = fail_restart
        result = self.execute(schedule([flow(rate_changes=[{'at_seconds': 2, 'offered_mbps': 10}])]))
        self.assertEqual(result['scheduler']['status'], 'failed')
        self.assertEqual(result['rate_changes'][0]['gap_status'], 'incomplete')
        self.assertIsNone(result['rate_changes'][0]['next_segment_index'])
        self.assertIsNone(result['rate_changes'][0]['traffic_gap_seconds'])
        self.assertFalse(self.docker.containers)

    def test_missing_or_invalid_receiver_statistics_never_use_sender_measurements(self):
        for value in ('not JSON', {'end': {'sum_sent': {'bits_per_second': 999999999}}},
                      {'error': 'interrupt - the client has terminated'}):
            with self.subTest(value=value):
                self.setUp()
                self.docker.reports[f'{1:064x}'] = value
                result = self.execute()
                self.assertEqual(result['scheduler']['status'], 'completed')
                self.assertIsNone(result['segments'][0]['metrics']['received_mbps'])
                self.assertEqual(result['segments'][0]['metrics']['throughput_status'], 'unavailable')
                self.assertIsNotNone(result['segments'][0]['measurement_error'])

    def test_invalid_receiver_numbers_remain_unavailable_and_jitter_keeps_existing_status(self):
        for field, value in (('bits_per_second', True), ('lost_percent', True),
                             ('bits_per_second', float('nan')), ('lost_percent', 101)):
            with self.subTest(field=field, value=value):
                self.setUp()
                report = deepcopy(REPORT)
                report['server_output_json']['end']['sum_received'][field] = value
                self.docker.reports[f'{1:064x}'] = report
                result = self.execute()
                metrics = result['segments'][0]['metrics']
                self.assertIsNone(metrics['received_mbps'])
                self.assertEqual(metrics['throughput_status'], 'unavailable')
                json.dumps(result, allow_nan=False)
        self.setUp()
        report = deepcopy(REPORT)
        del report['server_output_json']['end']['sum_received']['jitter_ms']
        self.docker.reports[f'{1:064x}'] = report
        metrics = self.execute()['segments'][0]['metrics']
        self.assertIsNone(metrics['jitter_ms'])
        self.assertEqual(metrics['jitter_status'], 'missing')
        self.assertEqual(metrics['received_mbps'], 4.7)

    def test_missing_logs_fail_execution_but_cleanup_can_still_remove_client(self):
        self.docker.effects['logs'] = subprocess.CompletedProcess([], 1, '', 'logs unavailable')
        result = self.execute()
        self.assertEqual(result['scheduler']['status'], 'failed')
        self.assertTrue(result['scheduler']['cleanup_succeeded'])
        self.assertFalse(self.docker.containers)
        self.assertIsNone(result['segments'][0]['raw_iperf_json'])

    def test_container_replaced_under_same_name_is_never_removed_by_cleanup(self):
        foreign_id = 'f' * 64
        def replace():
            original = self.docker.containers.pop(f'{1:064x}', None)
            if original is not None:
                original['id'] = foreign_id
                original['label'] = 'foreign'
                self.docker.containers[foreign_id] = original
        self.clock.on_sleep = replace
        result = self.execute()
        self.assertEqual(result['scheduler']['status'], 'failed')
        self.assertTrue(result['scheduler']['cleanup_succeeded'])
        self.assertIn(foreign_id, self.docker.containers)
        self.assertFalse(self.docker.signals)
        self.assertFalse(self.docker.removes)

    def test_malformed_create_response_can_be_cleaned_by_verified_name(self):
        original = self.docker
        def malformed(argv, **options):
            result = original(argv, **options)
            if argv[1] == 'create':
                result.stdout = 'not a container id'
            return result
        with patch('subprocess.run', side_effect=malformed):
            result = run_dynamic_udp(schedule(), MANIFEST, allow_live=True, clock=self.clock, lock_path=self.lock)
        self.assertEqual(result['scheduler']['status'], 'failed')
        self.assertTrue(result['scheduler']['cleanup_succeeded'])
        self.assertFalse(self.docker.containers)

    def test_oom_and_unexpected_signal_exit_are_failures(self):
        self.docker.oom.add(f'{1:064x}')
        result = self.execute()
        self.assertEqual(result['scheduler']['status'], 'failed')
        self.assertEqual(result['segments'][0]['status'], 'unexpected_exit')
        self.assertFalse(self.docker.containers)

    def test_explicit_iperf_error_fails_even_if_container_was_running_at_stop(self):
        self.docker.reports[f'{1:064x}'] = {'error': 'the server is busy running a test'}
        result = self.execute()
        self.assertEqual(result['scheduler']['status'], 'failed')
        self.assertIn('server is busy', result['scheduler']['error']['message'])
        self.assertFalse(self.docker.containers)

    def test_receiver_check_timeout_is_classified_without_starting_a_client(self):
        self.docker.effects['exec'] = subprocess.TimeoutExpired('docker exec', 5)
        result = self.execute()
        self.assertEqual(result['scheduler']['status'], 'timed_out')
        self.assertFalse(self.docker.creates)

    def test_invalid_clock_cannot_supply_a_nonfinite_subprocess_timeout(self):
        backend = DynamicUdpBackend(Lab(MANIFEST), allow_live=True, clock=self.clock)
        self.clock.now = float('nan')
        with patch('subprocess.run') as run, self.assertRaisesRegex(RuntimeError, 'finite monotonic'):
            backend.start(DynamicFlow(**flow()), 5, timeout_seconds=5)
        run.assert_not_called()

    def test_cancellation_and_deadline_clean_up_multiple_active_clients(self):
        for mode in ('cancel', 'deadline'):
            with self.subTest(mode=mode):
                self.setUp()
                options = ({'cancelled': lambda: self.clock.elapsed >= 1} if mode == 'cancel'
                           else {'timeout_seconds': 1})
                result = self.execute(schedule([flow(), flow('b', destination='nyc')]), **options)
                self.assertEqual(result['scheduler']['status'], 'cancelled' if mode == 'cancel' else 'timed_out')
                self.assertTrue(result['scheduler']['cleanup_succeeded'])
                self.assertFalse(self.docker.containers)

    def test_interruption_propagates_with_transport_evidence_after_cleanup(self):
        def interrupt():
            raise KeyboardInterrupt()
        self.clock.on_sleep = interrupt
        with self.assertRaises(ScheduleInterrupted) as caught:
            self.execute()
        result = caught.exception.traffic_result
        self.assertEqual(result['scheduler']['status'], 'interrupted')
        self.assertTrue(result['scheduler']['cleanup_succeeded'])
        self.assertEqual(len(result['segments']), 1)
        self.assertFalse(self.docker.containers)

    def test_interruption_during_cleanup_still_attempts_other_owned_clients(self):
        first_signal = True
        def interrupt_once(argv, options):
            nonlocal first_signal
            if first_signal:
                first_signal = False
                return KeyboardInterrupt()
        self.docker.effects['kill'] = interrupt_once
        with self.assertRaises(ScheduleInterrupted) as caught:
            self.execute(schedule([flow(), flow('b', destination='nyc')]),
                         cancelled=lambda: self.clock.elapsed >= 1)
        self.assertFalse(self.docker.containers)
        self.assertFalse(caught.exception.traffic_result['scheduler']['cleanup_succeeded'])
        self.assertEqual(len(self.docker.removes), 2)

    def test_system_exit_retains_transport_evidence_and_releases_lock(self):
        def exit_run():
            raise SystemExit(3)
        self.clock.on_sleep = exit_run
        with self.assertRaises(SystemExit) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, 3)
        self.assertTrue(caught.exception.traffic_result['scheduler']['cleanup_succeeded'])
        self.assertFalse(self.docker.containers)
        with self.lock.open('r') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_busy_global_lock_rejects_before_docker_and_lock_remains_reusable(self):
        self.lock.write_text('preserve this existing lock content')
        with self.lock.open('r') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'Another Abilene experiment'):
                self.execute()
        self.assertFalse(self.docker.calls)
        self.assertEqual(self.lock.read_text(), 'preserve this existing lock content')
        self.assertEqual(self.execute()['scheduler']['status'], 'completed')
        self.assertEqual(self.lock.read_text(), 'preserve this existing lock content')

    def test_backend_can_retry_cleanup_but_cannot_start_after_cleanup(self):
        backend = DynamicUdpBackend(Lab(MANIFEST), allow_live=True, clock=self.clock)
        with patch('subprocess.run', side_effect=self.docker):
            backend.start(DynamicFlow(**flow()), 5, timeout_seconds=5)
            backend.cleanup(timeout_seconds=5)
            backend.cleanup(timeout_seconds=5)
            count = len(self.docker.calls)
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                backend.start(DynamicFlow(**flow('b')), 5, timeout_seconds=5)
            self.assertEqual(len(self.docker.calls), count)


if __name__ == '__main__':
    unittest.main()
