"""Offline UDP jitter regression tests; fixtures are synthetic, not live captures."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from advnet.contracts import Observation
from advnet.lab import Deadline, Lab
from advnet.trial import acceptable, dump


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/iperf3'
MANIFEST = json.loads((ROOT / 'topologies/abilene.json').read_text())
FLOW = {'source': 'sea', 'destination': 'nyc', 'seconds': 3, 'offered_mbps': 5}


class TrafficMetricsTests(unittest.TestCase):
    def report(self, name='udp-text-server.json'):
        return json.loads((FIXTURES / name).read_text())

    def measure_traffic(self, report):
        lab = Lab(MANIFEST)
        before = {n: {i: 100 for i in r['transit_interfaces']}
                  for n, r in MANIFEST['routers'].items()}
        after = {n: {i: 1250100 for i in r['transit_interfaces']}
                 for n, r in MANIFEST['routers'].items()}
        deadline = Mock(spec=Deadline)
        deadline.remaining.return_value = 30
        with patch.object(lab, 'counters', side_effect=[before, after]), \
                patch.object(lab, '_sample_loaded_rtt', return_value=[]), \
                patch('advnet.lab.command', return_value=json.dumps(report)) as run, \
                patch('advnet.lab.time.monotonic', side_effect=[100, 102]):
            result = lab.traffic(FLOW, deadline)
        return result, run.call_args

    def test_jitter_uses_same_receiver_summary_as_throughput_and_loss(self):
        cases = [('udp-text-server.json', 0.625, 4.9, 0.002),
                 ('udp-json-server-received.json', 1.75, 4.7, 0.01),
                 ('udp-json-server-sum.json', 2.5, 4.6, 0.02)]
        for name, jitter, throughput, loss in cases:
            with self.subTest(fixture=name):
                result, call = self.measure_traffic(self.report(name))
                self.assertEqual(result['jitter_ms'], jitter)
                self.assertEqual(result['jitter_status'], 'measured')
                self.assertEqual(result['received_mbps'], throughput)
                self.assertEqual(result['loss_fraction'], loss)
                self.assertEqual(result['duration_seconds'], 2)
                self.assertEqual(result['udp_datagram_bytes'], 1200)
                self.assertEqual(result['offered_mbps'], 5)
                self.assertEqual(result['max_link_utilization'], 0.05)
                expected_interfaces = {f'{n}:{i}' for n, r in MANIFEST['routers'].items()
                                       for i in r['transit_interfaces']}
                self.assertEqual(set(result['link_utilization']), expected_interfaces)
                self.assertEqual(set(result['link_utilization'].values()), {0.05})
                self.assertFalse(result['capacities_enforced'])
                self.assertEqual(result['link_capacities_mbps'],
                                 {link['id']: link['capacity_mbps'] for link in MANIFEST['links']})
                self.assertEqual(call.args[0], [
                    'docker', 'exec', 'clab-advnet-abilene-h-sea', 'iperf3',
                    '-c', '10.64.11.10', '-B', '10.64.1.10', '-u', '-l', '1200',
                    '-b', '5M', '-t', '3', '-J', '--get-server-output'])
                self.assertEqual(call.kwargs['timeout'], 18)

    def test_zero_jitter_is_a_valid_measurement(self):
        for jitter in (0, 0.0):
            with self.subTest(jitter=jitter):
                report = self.report()
                report['end']['sum_received']['jitter_ms'] = jitter
                result, _ = self.measure_traffic(report)
                self.assertEqual(result['jitter_ms'], 0)
                self.assertEqual(result['jitter_status'], 'measured')

    def test_missing_or_null_jitter_is_unknown_and_keeps_existing_metrics(self):
        for missing in (True, False):
            with self.subTest(field_absent=missing):
                report = self.report()
                if missing:
                    del report['end']['sum_received']['jitter_ms']
                else:
                    report['end']['sum_received']['jitter_ms'] = None
                result, _ = self.measure_traffic(report)
                self.assertIsNone(result['jitter_ms'])
                self.assertEqual(result['jitter_status'], 'missing')
                self.assertEqual(result['received_mbps'], 4.9)
                self.assertEqual(result['loss_fraction'], 0.002)
                self.assertEqual(result['max_link_utilization'], 0.05)

    def test_malformed_jitter_is_explicitly_invalid_and_json_safe(self):
        for jitter in (-0.1, True, False, '0.625', '', [], {},
                       float('nan'), float('inf'), -float('inf')):
            with self.subTest(jitter=jitter):
                report = self.report()
                report['end']['sum_received']['jitter_ms'] = jitter
                result, _ = self.measure_traffic(report)
                self.assertIsNone(result['jitter_ms'])
                self.assertEqual(result['jitter_status'], 'invalid')
                self.assertEqual(result['received_mbps'], 4.9)
                self.assertEqual(result['loss_fraction'], 0.002)
                self.assertEqual(result['max_link_utilization'], 0.05)
                json.dumps(result, allow_nan=False)

    def test_sender_and_interval_jitter_do_not_fill_missing_receiver_jitter(self):
        report = self.report()
        del report['end']['sum_received']['jitter_ms']
        report['end']['sum_sent']['jitter_ms'] = 99
        report['intervals'] = [{'streams': [{'jitter_ms': 88, 'sender': True}]}]
        result, _ = self.measure_traffic(report)
        self.assertIsNone(result['jitter_ms'])
        self.assertEqual(result['jitter_status'], 'missing')

    def test_other_receiver_summary_does_not_fill_unknown_selected_jitter(self):
        for jitter, status in ((None, 'missing'), ('bad', 'invalid')):
            with self.subTest(jitter=jitter):
                report = self.report('udp-json-server-received.json')
                report['server_output_json']['end']['sum_received']['jitter_ms'] = jitter
                result, _ = self.measure_traffic(report)
                self.assertIsNone(result['jitter_ms'])
                self.assertEqual(result['jitter_status'], status)
                self.assertEqual(result['received_mbps'], 4.7)

    def test_sender_only_results_still_reject_even_with_jitter(self):
        report = self.report()
        del report['end']['sum_received']
        with self.assertRaisesRegex(ValueError, 'Receiver-side UDP results are missing'):
            self.measure_traffic(report)

    def test_jitter_survives_measurement_observation_and_saved_artifact(self):
        for jitter, status in ((0.625, 'measured'), (None, 'missing'), ('bad', 'invalid')):
            with self.subTest(jitter=jitter):
                report = self.report()
                report['end']['sum_received']['jitter_ms'] = jitter
                traffic, _ = self.measure_traffic(report)
                lab = Lab(MANIFEST)
                neighbors = {n: '\n'.join(f'{rid} 1 default 0 FULL 00:00:30'
                                          for rid in ids)
                             for n, ids in lab.expected_neighbors().items()}
                pairs = [{'source': a, 'destination': b, 'reachable': True,
                          'loss_fraction': 0, 'rtt_avg_ms': 4}
                         for a in MANIFEST['routers'] for b in MANIFEST['routers'] if a != b]
                configs = {n: (ROOT / f'labs/configs/abilene/{n}.cfg').read_text()
                           for n in MANIFEST['routers']}
                with patch.object(lab, 'neighbors', return_value=neighbors), \
                        patch.object(lab, 'ping_matrix', return_value=pairs), \
                        patch.object(lab, 'traffic', return_value=traffic), \
                        patch.object(lab, 'configs', return_value=configs):
                    measurement = lab.measure(Deadline(30), FLOW)
                    observation = lab.observe('test-jitter', measurement, Deadline(30),
                                              'Preserve delivered traffic.')
                self.assertTrue(measurement['all_reachable'])
                self.assertTrue(measurement['ospf_healthy'])
                self.assertEqual(len(measurement['pairs']), 110)
                self.assertEqual(measurement['mean_rtt_ms'], 4)
                decoded = Observation.model_validate_json(observation.model_dump_json())
                self.assertEqual(decoded.measurements['traffic'], traffic)
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / 'measurements.json'
                    dump(path, measurement)
                    self.assertEqual(json.loads(path.read_text())['traffic'], traffic)

    def test_jitter_does_not_change_runtime_acceptance(self):
        base = {'all_reachable': True, 'ospf_healthy': True, 'mean_rtt_ms': 4,
                'traffic': {'received_mbps': 5, 'loss_fraction': 0,
                            'max_link_utilization': 0.05}}
        impaired = deepcopy(base)
        impaired['traffic']['max_link_utilization'] = 0.2
        for jitter, status in ((0, 'measured'), (10000, 'measured'),
                               (None, 'missing'), (None, 'invalid')):
            with self.subTest(jitter=jitter, status=status):
                current = deepcopy(base)
                current['traffic'].update(jitter_ms=jitter, jitter_status=status)
                self.assertTrue(acceptable(current, base, impaired, 'optimization')['accepted'])
                current['traffic']['received_mbps'] = 1
                self.assertFalse(acceptable(current, base, impaired, 'optimization')['accepted'])


if __name__ == '__main__':
    unittest.main()
