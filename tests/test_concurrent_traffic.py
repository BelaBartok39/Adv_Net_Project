"""Offline batch tests: mocked Docker clients, real orchestration and aggregation."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from threading import Barrier, Event, Lock, current_thread, get_ident
import unittest
from unittest.mock import Mock, patch

from advnet.contracts import Observation
from advnet.lab import Deadline, Lab


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / 'topologies/abilene.json').read_text())
FLOWS = [{'source': 'sea', 'destination': 'sva', 'seconds': 12, 'offered_mbps': 5},
         {'source': 'den', 'destination': 'kcy', 'seconds': 8, 'offered_mbps': 3}]


def udp_report(flow, index):
    seconds = flow['seconds']
    rate = flow['offered_mbps'] * 1_000_000
    return {'end': {
        'sum_sent': {'start': 0, 'end': seconds, 'seconds': seconds,
                     'bytes': int(rate*seconds/8), 'bits_per_second': rate, 'sender': True},
        'sum_received': {'start': 0, 'end': seconds, 'seconds': seconds,
                         'bytes': int(rate*seconds/8), 'bits_per_second': rate,
                         'packets': 1000 if index == 0 else 200,
                         'lost_packets': 10 if index == 0 else 0,
                         'lost_percent': 1 if index == 0 else 0,
                         'jitter_ms': 0.5+index, 'sender': False}}}


class BatchHarness:
    """Mock subprocesses but synchronize actual worker threads before any returns."""
    def __init__(self, flows=FLOWS, mode='ok', mutate=None):
        self.flows, self.mode, self.mutate = deepcopy(flows), mode, mutate
        self.barrier = Barrier(len(flows))
        self.clients, self.creates, self.removes = [], [], []
        self.lock = Lock()
        self.active, self.peak_active = 0, 0
        self.main_thread = get_ident()
        self.main_times = iter([100, 114])
        self.worker_times = {i: iter([101+i/10, 101+i/10+f['seconds']+0.3])
                             for i, f in enumerate(flows)}

    def clock(self):
        if get_ident() == self.main_thread:
            return next(self.main_times)
        index = int(current_thread().name.rsplit('_', 1)[1])
        return next(self.worker_times[index])

    def run(self, argv, **kwargs):
        if argv[1] == 'create':
            self.creates.append(argv)
            if self.mode == 'create_timeout' and len(self.creates) == 2:
                raise subprocess.TimeoutExpired(argv, 30)
            if self.mode == 'create_failure' and len(self.creates) == 2:
                return subprocess.CompletedProcess(argv, 1, '', 'cannot create client')
            return subprocess.CompletedProcess(argv, 0, 'synthetic-container-id\n', '')
        if argv[1] == 'inspect':
            create = next(item for item in self.creates if item[item.index('--name')+1] == argv[-1])
            if self.mode == 'create_failure' and create is self.creates[-1]:
                return subprocess.CompletedProcess(argv, 1, '', 'No such container')
            token = create[create.index('--label')+1].split('=', 1)[1]
            return subprocess.CompletedProcess(argv, 0,
                                               'another-run' if self.mode == 'unowned' else token, '')
        if argv[1:3] == ['rm', '-f']:
            self.removes.append(argv[-1])
            if self.mode == 'cleanup_failure':
                return subprocess.CompletedProcess(argv, 1, '', 'daemon unavailable')
            return subprocess.CompletedProcess(argv, 0, argv[-1], '')
        raise AssertionError('Unexpected Docker command: '+repr(argv))

    def popen(self, argv, **kwargs):
        harness = self
        index = int(argv[-1].rsplit('-', 1)[1])
        class Process:
            returncode = None
            reaped = False
            killed = False
            def __init__(self):
                self.release = Event()
                with harness.lock:
                    harness.clients.append(self)
                    harness.active += 1
                    harness.peak_active = max(harness.peak_active, harness.active)
            def poll(self):
                return self.returncode
            def kill(self):
                self.killed = True
                self.returncode = -9
                self.release.set()
            def communicate(self, timeout=None):
                if self.killed:
                    self.reaped = True
                    return '', 'client killed'
                harness.barrier.wait(2)
                if harness.mode in ('timeout', 'exit_failure', 'invalid_json', 'sender_only', 'iperf_error'):
                    if index == 0:
                        if harness.mode == 'timeout':
                            raise subprocess.TimeoutExpired(argv, timeout)
                        self.returncode = 1 if harness.mode == 'exit_failure' else 0
                        self.reaped = True
                        return ('not json' if harness.mode == 'invalid_json' else
                                json.dumps({'error': 'server is busy running a test'}) if harness.mode == 'iperf_error' else
                                json.dumps({'end': {'sum_sent': {'sender': True}}}), 'server is busy')
                    if not self.release.wait(2):
                        raise AssertionError('Sibling client was not cancelled')
                    self.reaped = True
                    return '', 'client killed'
                data = udp_report(harness.flows[index], index)
                if harness.mutate:
                    harness.mutate(data, index)
                self.returncode = 0
                self.reaped = True
                with harness.lock:
                    harness.active -= 1
                return json.dumps(data), ''
        return Process()

    def execute(self, lab=None):
        lab = lab or Lab(MANIFEST)
        before = {n: {i: 100 for i in r['transit_interfaces']}
                  for n, r in MANIFEST['routers'].items()}
        after = {n: {i: 1750100 for i in r['transit_interfaces']}
                 for n, r in MANIFEST['routers'].items()}
        deadline = Mock(spec=Deadline)
        deadline.remaining.return_value = 90
        with patch('advnet.concurrent_traffic.subprocess.run', side_effect=self.run), \
                patch('advnet.concurrent_traffic.subprocess.Popen', side_effect=self.popen), \
                patch('advnet.concurrent_traffic.monotonic', side_effect=self.clock), \
                patch.object(lab, 'counters', side_effect=(
                    ValueError('Counter collection failed') if self.mode == 'counter_failure'
                    else [before, after])) as counters:
            result = lab.concurrent_traffic(self.flows, deadline)
        self.counter_calls = counters.call_count
        return result


class ConcurrentTrafficTests(unittest.TestCase):
    def test_clients_overlap_and_share_one_counter_window(self):
        harness = BatchHarness()
        result = harness.execute()
        self.assertEqual(harness.peak_active, 2)
        self.assertEqual(harness.counter_calls, 2)
        self.assertEqual(result['duration_seconds'], 14)
        self.assertTrue(result['concurrency_verified'])
        self.assertGreater(result['overlap_window']['duration_seconds'], 0)
        self.assertEqual([f['flow_index'] for f in result['flows']], [0, 1])
        self.assertEqual([f['seconds'] for f in result['flows']], [12, 8])
        self.assertEqual([f['received_mbps'] for f in result['flows']], [5, 3])
        self.assertEqual([f['loss_fraction'] for f in result['flows']], [0.01, 0])
        self.assertEqual([f['jitter_ms'] for f in result['flows']], [0.5, 1.5])
        # Common-window goodput is byte totals / 14 seconds, not 5 + 3 Mbps.
        self.assertEqual(result['aggregate']['received_bytes'], 10500000)
        self.assertEqual(result['aggregate']['received_mbps'], 6)
        self.assertAlmostEqual(result['aggregate']['loss_fraction'], 10/1200)
        self.assertEqual(result['max_link_utilization'], 0.01)
        self.assertEqual(set(result['link_utilization'].values()), {0.01})
        self.assertEqual(result['counter_window']['end_seconds'], 14)
        self.assertFalse(result['capacities_enforced'])
        self.assertEqual(set(harness.removes), {a[a.index('--name')+1] for a in harness.creates})
        self.assertTrue(all(process.reaped for process in harness.clients))
        json.dumps(result, allow_nan=False)

    def test_owned_clients_share_source_network_and_bind_data_ip(self):
        harness = BatchHarness()
        harness.flows[1].update(destination='sva', destination_port=5202)
        harness.execute()
        for argv, flow in zip(harness.creates, harness.flows):
            self.assertEqual(argv[argv.index('--network')+1],
                             'container:clab-advnet-abilene-'+MANIFEST['routers'][flow['source']]['host'])
            self.assertEqual(argv[argv.index('-B')+1], MANIFEST['routers'][flow['source']]['host_ip'])
            self.assertEqual(argv[argv.index('-p')+1], str(flow.get('destination_port', 5201)))
            self.assertIn('advnet-host:0.1', argv)
            self.assertEqual(argv[argv.index('--cap-drop')+1], 'ALL')
            self.assertEqual(argv[argv.index('--memory')+1], '128m')
            self.assertEqual(argv[argv.index('-l')+1], '1200')
            self.assertEqual(argv[argv.index('-t')+1], str(flow['seconds']))

    def test_maximum_four_flows_is_bounded_and_all_run_together(self):
        flows = FLOWS + [dict(source='hou', destination='atl', seconds=12, offered_mbps=2),
                         dict(source='ind', destination='chi', seconds=12, offered_mbps=1)]
        harness = BatchHarness(flows)
        result = harness.execute()
        self.assertEqual(result['flow_count'], 4)
        self.assertEqual(harness.peak_active, 4)
        self.assertEqual(len(harness.removes), 4)

    def test_bad_flows_and_destination_conflicts_reject_before_docker(self):
        bad = [None, {}, [], FLOWS[:1], FLOWS*3]
        for key, value in (('source', 'unknown'), ('destination', 'sea'), ('seconds', 0),
                           ('seconds', True), ('seconds', 61), ('offered_mbps', float('nan')),
                           ('offered_mbps', 0), ('destination_port', True),
                           ('destination_port', 0), ('destination_port', 65536)):
            item = deepcopy(FLOWS)
            item[0][key] = value
            bad.append(item)
        conflict = deepcopy(FLOWS)
        conflict[1]['destination'] = 'sva'
        bad.append(conflict)
        for flows in bad:
            with self.subTest(flows=flows), \
                    patch('advnet.concurrent_traffic.subprocess.run') as run, \
                    patch('advnet.concurrent_traffic.subprocess.Popen') as popen:
                with self.assertRaises(ValueError):
                    Lab(MANIFEST).concurrent_traffic(flows, Deadline(30))
                run.assert_not_called()
                popen.assert_not_called()

    def test_client_failure_cancels_peers_and_removes_only_owned_containers(self):
        for mode, exception in (('timeout', subprocess.TimeoutExpired),
                                ('exit_failure', RuntimeError), ('invalid_json', ValueError),
                                ('sender_only', ValueError), ('iperf_error', RuntimeError)):
            with self.subTest(mode=mode):
                harness = BatchHarness(mode=mode)
                with self.assertRaises(exception):
                    harness.execute()
                self.assertEqual(len(harness.clients), 2)
                self.assertTrue(all(p.reaped for p in harness.clients))
                self.assertTrue(any(p.killed for p in harness.clients))
                self.assertEqual(set(harness.removes), {a[a.index('--name')+1] for a in harness.creates})
                self.assertTrue(all(name.startswith('advnet-udp-') for name in harness.removes))

    def test_prepare_failure_cleans_all_attempted_clients_without_starting_any(self):
        harness = BatchHarness(mode='create_failure')
        with self.assertRaisesRegex(RuntimeError, 'Unable to prepare'):
            harness.execute()
        self.assertFalse(harness.clients)
        self.assertEqual(len(harness.removes), 1)
        self.assertEqual(harness.removes[0], harness.creates[0][harness.creates[0].index('--name')+1])

    def test_cleanup_failure_is_not_reported_as_success(self):
        harness = BatchHarness(mode='cleanup_failure')
        with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
            harness.execute()
        self.assertEqual(len(harness.removes), 2)

    def test_create_timeout_cleans_owned_clients_even_without_a_reply(self):
        harness = BatchHarness(mode='create_timeout')
        with self.assertRaises(subprocess.TimeoutExpired):
            harness.execute()
        self.assertFalse(harness.clients)
        self.assertEqual(len(harness.removes), 2)

    def test_counter_failure_cancels_waiting_clients_before_any_start(self):
        harness = BatchHarness(mode='counter_failure')
        with self.assertRaisesRegex(ValueError, 'Counter collection failed'):
            harness.execute()
        self.assertFalse(harness.clients)
        self.assertEqual(len(harness.removes), 2)

    def test_cleanup_refuses_to_remove_containers_with_another_owner(self):
        harness = BatchHarness(mode='unowned')
        with self.assertRaisesRegex(RuntimeError, 'not owned by this batch'):
            harness.execute()
        self.assertFalse(harness.removes)

    def test_missing_totals_and_jitter_remain_unknown(self):
        def missing(data, index):
            receiver = data['end']['sum_received']
            for field in ('bytes', 'packets', 'lost_packets', 'jitter_ms'):
                receiver.pop(field)
        result = BatchHarness(mutate=missing).execute()
        self.assertIsNone(result['aggregate']['received_mbps'])
        self.assertIsNone(result['aggregate']['loss_fraction'])
        self.assertEqual(result['aggregate']['throughput_status'], 'unavailable')
        self.assertEqual(result['aggregate']['loss_status'], 'unavailable')
        for flow in result['flows']:
            self.assertIsNone(flow['jitter_ms'])
            self.assertEqual(flow['jitter_status'], 'missing')
            self.assertGreater(flow['received_mbps'], 0)

    def test_invalid_receiver_statistics_reject_and_cleanup(self):
        for field, value in (('bits_per_second', True), ('bits_per_second', float('nan')),
                             ('lost_percent', True), ('lost_percent', 101)):
            def invalid(data, index):
                data['end']['sum_received'][field] = value
            with self.subTest(field=field, value=value):
                harness = BatchHarness(mutate=invalid)
                with self.assertRaisesRegex(ValueError, 'invalid receiver'):
                    harness.execute()
                self.assertEqual(len(harness.removes), 2)

    def test_invalid_jitter_keeps_raw_output_without_nonfinite_result_values(self):
        def invalid(data, index):
            data['end']['sum_received']['jitter_ms'] = float('nan')
        result = BatchHarness(mutate=invalid).execute()
        for flow in result['flows']:
            self.assertIsNone(flow['jitter_ms'])
            self.assertEqual(flow['jitter_status'], 'invalid')
            self.assertIn('NaN', flow['raw_iperf_json'])
        json.dumps(result, allow_nan=False)

    def test_unverified_sender_overlap_is_explicit(self):
        def missing_timing(data, index):
            data['end'].pop('sum_sent')
        result = BatchHarness(mutate=missing_timing).execute()
        self.assertFalse(result['concurrency_verified'])
        self.assertIsNone(result['overlap_window'])
        self.assertTrue(result['overlap_reason'])
        self.assertGreater(result['aggregate']['received_mbps'], 0)

    def test_counter_reset_rejects_and_cleanup_still_runs(self):
        lab = Lab(MANIFEST)
        harness = BatchHarness()
        with patch.object(lab, '_link_utilization', side_effect=ValueError('Interface counter reset')):
            with self.assertRaisesRegex(ValueError, 'counter reset'):
                harness.execute(lab)
        self.assertEqual(len(harness.removes), 2)

    def test_measure_keeps_idle_checks_and_forwards_batch_results_to_observation(self):
        lab = Lab(MANIFEST)
        traffic = BatchHarness().execute()
        neighbors = {n: '\n'.join(f'{rid} 1 default 0 FULL 00:00:30' for rid in ids)
                     for n, ids in lab.expected_neighbors().items()}
        pairs = [{'source': a, 'destination': b, 'reachable': True,
                  'loss_fraction': 0, 'rtt_avg_ms': 4}
                 for a in MANIFEST['routers'] for b in MANIFEST['routers'] if a != b]
        configs = {n: (ROOT / f'labs/configs/abilene/{n}.cfg').read_text()
                   for n in MANIFEST['routers']}
        with patch.object(lab, 'neighbors', return_value=neighbors), \
                patch.object(lab, 'ping_matrix', return_value=pairs), \
                patch.object(lab, 'concurrent_traffic', return_value=traffic) as batch, \
                patch.object(lab, 'traffic') as single, \
                patch.object(lab, 'configs', return_value=configs):
            measurement = lab.measure(Deadline(30), flows=FLOWS)
            batch.assert_called_once()
            single.assert_not_called()
            observation = lab.observe('test-concurrent', measurement, Deadline(30), 'Preserve traffic.')
        self.assertEqual(measurement['mean_rtt_ms'], 4)
        self.assertEqual(len(measurement['pairs']), 110)
        self.assertTrue(measurement['all_reachable'])
        decoded = Observation.model_validate_json(observation.model_dump_json())
        self.assertEqual(decoded.measurements['traffic'], traffic)
        with self.assertRaisesRegex(ValueError, 'either flow or flows'):
            lab.measure(Deadline(30), FLOWS[0], flows=FLOWS)


if __name__ == '__main__':
    unittest.main()
