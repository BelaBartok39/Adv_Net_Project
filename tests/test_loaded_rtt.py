"""Offline loaded-RTT tests with mocked traffic and ping; no live measurements."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
from threading import Event, get_ident
import unittest
from unittest.mock import Mock, patch

from advnet.contracts import Observation
from advnet.lab import Deadline, Lab, loaded_rtt
from advnet.trial import acceptable, dump


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / 'topologies/abilene.json').read_text())
FLOW = {'source': 'sea', 'destination': 'nyc', 'seconds': 12, 'offered_mbps': 5}
PING = ('64 bytes from 10.64.11.10: icmp_seq=1 ttl=58 time=20.0 ms\n'
        '1 packets transmitted, 1 received, 0% packet loss, time 0ms\n'
        'rtt min/avg/max/mdev = 20.000/20.000/20.000/0.000 ms\n')


def report():
    data = json.loads((ROOT / 'tests/fixtures/iperf3/udp-text-server.json').read_text())
    data['start']['test_start']['duration'] = 12
    for summary in data['end'].values():
        summary.update(seconds=12, end=12, bytes=summary['bytes'] * 4)
    return data


def sample(start, end, rtt=20, status='measured'):
    return {'start_seconds': start, 'end_seconds': end, 'rtt_ms': rtt,
            'status': status, 'error': None, 'stdout': PING, 'stderr': ''}


class LoadedRttTests(unittest.TestCase):
    def test_only_complete_probes_in_verified_interval_contribute(self):
        samples = [sample(0.1, 0.2, 100), sample(2, 2.3, 200),
                   sample(3, 3.1, 10), sample(7, 7.1, 30),
                   sample(11.9, 12.1, 300), sample(13, 13.1, 400)]
        result = loaded_rtt(report(), samples, 14)
        self.assertEqual(result['window']['start_seconds'], 2.05)
        self.assertEqual(result['window']['end_seconds'], 11.95)
        self.assertEqual(result['mean_ms'], 20)
        self.assertEqual(result['min_ms'], 10)
        self.assertEqual(result['max_ms'], 30)
        self.assertEqual(result['samples_used'], 2)
        self.assertEqual(result['status'], 'measured')
        self.assertEqual([s['included'] for s in result['samples']],
                         [False, False, True, True, False, False])
        self.assertNotIn('included', samples[0])

    def test_failed_probes_are_explicit_and_not_averaged_as_zero(self):
        result = loaded_rtt(report(), [sample(3, 3.1, 20),
                                      sample(4, 5, None, 'failed')], 14)
        self.assertEqual(result['mean_ms'], 20)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['samples_failed'], 1)
        self.assertIn('failed', result['reason'])
        result = loaded_rtt(report(), [sample(4, 5, None, 'failed')], 14)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['mean_ms'])
        self.assertEqual(result['samples_failed'], 1)

    def test_no_verified_overlap_or_no_eligible_samples_is_unknown(self):
        for elapsed, samples in ((26, [sample(16, 16.1)]),
                                 (14, []), (14, [sample(13, 13.1)])):
            with self.subTest(elapsed=elapsed, samples=samples):
                result = loaded_rtt(report(), samples, elapsed)
                self.assertEqual(result['status'], 'unavailable')
                self.assertIsNone(result['mean_ms'])
                self.assertIsNotNone(result['reason'])
                self.assertFalse(any(s['included'] for s in result['samples']))

    def test_receiver_duration_cannot_substitute_for_sender_timing(self):
        data = report()
        del data['end']['sum_sent']
        result = loaded_rtt(data, [sample(3, 3.1)], 14)
        self.assertIsNone(result['mean_ms'])
        self.assertIn('missing', result['reason'])

    def test_invalid_sender_timing_never_claims_loaded_rtt(self):
        for field, value in (('sender', False), ('seconds', None), ('seconds', True),
                             ('seconds', '12'), ('seconds', -1), ('seconds', 20),
                             ('seconds', float('nan')), ('seconds', float('inf')),
                             ('start', 1), ('start', None), ('end', 11),
                             ('bytes', 0), ('bytes', False)):
            with self.subTest(field=field, value=value):
                data = report()
                data['end']['sum_sent'][field] = value
                result = loaded_rtt(data, [sample(3, 3.1)], 14)
                self.assertIsNone(result['mean_ms'])
                self.assertIsNone(result['window'])
                self.assertEqual(result['status'], 'unavailable')
                json.dumps(result, allow_nan=False)

    def test_legacy_sender_sum_is_supported(self):
        data = report()
        data['end']['sum'] = data['end'].pop('sum_sent')
        result = loaded_rtt(data, [sample(3, 3.1, 0)], 14)
        self.assertEqual(result['mean_ms'], 0)
        self.assertEqual(result['status'], 'measured')

    def test_ping_is_bound_and_bounded_and_failures_are_retained(self):
        cases = [(subprocess.CompletedProcess([], 0, PING, ''), 'measured', 20),
                 (subprocess.CompletedProcess([], 1, '100% packet loss', ''), 'failed', None),
                 (subprocess.CompletedProcess([], 0, 'unexpected output', ''), 'failed', None),
                 (subprocess.TimeoutExpired('ping', 2), 'error', None),
                 (FileNotFoundError('docker unavailable'), 'error', None),
                 (TimeoutError('deadline expired'), 'error', None)]
        for response, status, rtt in cases:
            with self.subTest(response=response):
                lab = Lab(MANIFEST)
                stop = Mock(spec=Event)
                stop.is_set.return_value = False
                stop.wait.return_value = True
                deadline = Mock(spec=Deadline)
                deadline.remaining.return_value = 30
                kwargs = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
                with patch('advnet.lab.subprocess.run', **kwargs) as run, \
                        patch('advnet.lab.time.monotonic', side_effect=[103, 104]):
                    samples = lab._sample_loaded_rtt('sea', 'nyc', deadline, stop, 100)
                self.assertEqual(len(samples), 1)
                self.assertEqual(samples[0]['status'], status)
                self.assertEqual(samples[0]['rtt_ms'], rtt)
                if status != 'measured':
                    self.assertTrue(samples[0]['error'])
                self.assertEqual(run.call_args.args[0], [
                    'docker', 'exec', 'clab-advnet-abilene-h-sea', 'ping', '-n',
                    '-I', 'eth1', '-c', '1', '-W', '1', '-w', '1', '10.64.11.10'])
                self.assertEqual(run.call_args.kwargs['timeout'], 2)
                self.assertEqual(samples[0]['start_seconds'], 3)
                self.assertEqual(samples[0]['end_seconds'], 4)

    def test_ping_subprocess_runs_while_iperf_command_is_in_progress(self):
        lab = Lab(MANIFEST)
        traffic_running, probe_recorded, stop = Event(), Event(), Event()
        real_wait = stop.wait
        main_thread = get_ident()
        main_times, probe_times = iter([100, 114]), iter([103, 103.1])
        def clock():
            return next(main_times if get_ident() == main_thread else probe_times)
        def ping(*args, **kwargs):
            self.assertTrue(traffic_running.wait(2))
            return subprocess.CompletedProcess(args[0], 0, PING, '')
        def wait(interval):
            probe_recorded.set()
            return real_wait(interval)
        def traffic(*args, **kwargs):
            traffic_running.set()
            self.assertTrue(probe_recorded.wait(2))
            traffic_running.clear()
            return json.dumps(report())
        counters = {n: {i: 0 for i in r['transit_interfaces']}
                    for n, r in MANIFEST['routers'].items()}
        deadline = Mock(spec=Deadline)
        deadline.remaining.return_value = 30
        with patch.object(lab, 'counters', return_value=counters), \
                patch('advnet.lab.Event', return_value=stop), \
                patch.object(stop, 'wait', side_effect=wait), \
                patch('advnet.lab.command', side_effect=traffic) as iperf, \
                patch('advnet.lab.subprocess.run', side_effect=ping) as run, \
                patch('advnet.lab.time.monotonic', side_effect=clock):
            result = lab.traffic(FLOW, deadline)
        self.assertTrue(stop.is_set())
        self.assertEqual(run.call_count, 1)
        self.assertIn('iperf3', iperf.call_args.args[0])
        self.assertEqual(result['loaded_rtt']['status'], 'measured')
        self.assertEqual(result['loaded_rtt']['mean_ms'], 20)
        self.assertEqual(result['loaded_rtt']['samples_used'], 1)
        self.assertEqual(result['received_mbps'], 4.9)
        self.assertEqual(result['jitter_ms'], 0.625)
        self.assertEqual(result['duration_seconds'], 14)

    def test_sampler_is_joined_when_traffic_times_out_or_fails(self):
        for error in (subprocess.TimeoutExpired('iperf3', 27), RuntimeError('iperf3 failed')):
            with self.subTest(error=error):
                lab = Lab(MANIFEST)
                entered, exited = Event(), Event()
                stops = []
                def sampler(source, destination, deadline, stop, start):
                    stops.append(stop)
                    entered.set()
                    self.assertTrue(stop.wait(2))
                    exited.set()
                    return []
                def traffic(*args, **kwargs):
                    self.assertTrue(entered.wait(2))
                    raise error
                with patch.object(lab, 'counters', return_value={}), \
                        patch.object(lab, '_sample_loaded_rtt', side_effect=sampler), \
                        patch('advnet.lab.command', side_effect=traffic):
                    with self.assertRaises(type(error)):
                        lab.traffic(FLOW, Deadline(30))
                self.assertTrue(exited.is_set())
                self.assertTrue(stops[0].is_set())

    def test_counter_read_does_not_wait_for_final_probe_completion(self):
        lab = Lab(MANIFEST)
        entered, counters_read = Event(), Event()
        counters = {n: {i: 0 for i in r['transit_interfaces']}
                    for n, r in MANIFEST['routers'].items()}
        def read_counters(deadline):
            if entered.is_set():
                counters_read.set()
            return counters
        def sampler(source, destination, deadline, stop, start):
            entered.set()
            self.assertTrue(stop.wait(2))
            self.assertTrue(counters_read.wait(2))
            return []
        def traffic(*args, **kwargs):
            self.assertTrue(entered.wait(2))
            return json.dumps(report())
        with patch.object(lab, 'counters', side_effect=read_counters), \
                patch.object(lab, '_sample_loaded_rtt', side_effect=sampler), \
                patch('advnet.lab.command', side_effect=traffic):
            result = lab.traffic(FLOW, Deadline(30))
        self.assertEqual(result['received_mbps'], 4.9)

    def test_idle_rtt_reachability_observations_and_acceptance_are_preserved(self):
        lab = Lab(MANIFEST)
        probes = loaded_rtt(report(), [sample(3, 3.1, 20)], 14)
        traffic = {'received_mbps': 5, 'loss_fraction': 0,
                   'max_link_utilization': 0.05, 'loaded_rtt': probes}
        neighbors = {n: '\n'.join(f'{rid} 1 default 0 FULL 00:00:30' for rid in ids)
                     for n, ids in lab.expected_neighbors().items()}
        pairs = [{'source': a, 'destination': b, 'reachable': True,
                  'loss_fraction': 0, 'rtt_avg_ms': 4}
                 for a in MANIFEST['routers'] for b in MANIFEST['routers'] if a != b]
        configs = {n: (ROOT / f'labs/configs/abilene/{n}.cfg').read_text()
                   for n in MANIFEST['routers']}
        with patch.object(lab, 'neighbors', return_value=neighbors), \
                patch.object(lab, 'ping_matrix', return_value=pairs), \
                patch.object(lab, 'traffic', return_value=traffic) as run, \
                patch.object(lab, 'configs', return_value=configs):
            idle = lab.measure(Deadline(30))
            run.assert_not_called()
            measurement = lab.measure(Deadline(30), FLOW)
            observation = lab.observe('test-loaded-rtt', measurement, Deadline(30), 'Preserve traffic.')
        self.assertIsNone(idle['traffic'])
        self.assertEqual(measurement['mean_rtt_ms'], 4)
        self.assertEqual(len(measurement['pairs']), 110)
        self.assertTrue(measurement['all_reachable'])
        decoded = Observation.model_validate_json(observation.model_dump_json())
        self.assertEqual(decoded.measurements['traffic']['loaded_rtt']['mean_ms'], 20)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'measurements.json'
            dump(path, measurement)
            self.assertEqual(json.loads(path.read_text())['traffic']['loaded_rtt'], probes)
        impaired = deepcopy(measurement)
        impaired['traffic']['max_link_utilization'] = 1
        for value in (None, 10000):
            current = deepcopy(measurement)
            current['traffic']['loaded_rtt']['mean_ms'] = value
            self.assertTrue(acceptable(current, measurement, impaired, 'optimization')['accepted'])
            current['mean_rtt_ms'] = 100
            self.assertFalse(acceptable(current, measurement, impaired, 'optimization')['accepted'])


if __name__ == '__main__':
    unittest.main()
