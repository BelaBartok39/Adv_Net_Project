"""Deterministic reporting tests; synthetic values are not experimental results."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from advnet.performance import compare_performance, compare_trial_artifacts
from advnet.trial import Trial
from test_abilene import FakeDecision, FakeLab, FakeVerifier, proposal


ROOT = Path(__file__).resolve().parents[1]


def measurement(rate=5, loss=0.01, rtt=10, jitter=1, utilization=0.3):
    return {'mean_rtt_ms': 2, 'all_reachable': True, 'ospf_healthy': True,
            'traffic': {'source': 'sea', 'destination': 'sva', 'offered_mbps': 5,
                        'seconds': 12, 'duration_seconds': 12.1,
                        'received_mbps': rate, 'loss_fraction': loss,
                        'loaded_rtt': {'mean_ms': rtt, 'status': 'measured',
                                       'samples_used': 40, 'samples_failed': 0},
                        'jitter_ms': jitter, 'jitter_status': 'measured',
                        'max_link_utilization': utilization,
                        'link_utilization': {'sea:Ethernet1': utilization},
                        'capacities_enforced': True, 'link_capacities_mbps': {'sea-sva': 100}}}


def metric(report, pair, name):
    return report['comparisons'][pair]['metrics'][name]


class PerformanceTests(unittest.TestCase):
    def stages(self):
        return (measurement(), measurement(2, 0.1, 40, 5, 1),
                measurement(4.8, 0.02, 12, 1.2, 0.4))

    def test_three_stages_have_signed_changes_and_explicit_units(self):
        report = compare_performance(*self.stages())
        self.assertAlmostEqual(metric(report, 'impaired_vs_healthy', 'received_mbps')['absolute_change'], -3)
        self.assertAlmostEqual(metric(report, 'post_vs_impaired', 'received_mbps')['absolute_change'], 2.8)
        self.assertAlmostEqual(metric(report, 'post_vs_impaired', 'received_mbps')['relative_change_percent'], 140)
        self.assertAlmostEqual(metric(report, 'post_vs_healthy', 'received_mbps')['relative_change_percent'], -4)
        for name, delta, percent, unit in (
                ('loaded_rtt_mean_ms', -28, -70, 'ms'), ('jitter_ms', -3.8, -76, 'ms'),
                ('loss_fraction', -0.08, -80, 'fraction'), ('max_link_utilization', -0.6, -60, 'fraction')):
            change = metric(report, 'post_vs_impaired', name)
            self.assertEqual(change['status'], 'compared')
            self.assertAlmostEqual(change['absolute_change'], delta)
            self.assertAlmostEqual(change['relative_change_percent'], percent)
            self.assertEqual(change['unit'], unit)
        self.assertAlmostEqual(metric(report, 'post_vs_impaired', 'loss_fraction')['percentage_point_change'], -8)
        self.assertAlmostEqual(metric(report, 'post_vs_impaired', 'max_link_utilization')['percentage_point_change'], -60)
        self.assertIsNone(metric(report, 'post_vs_impaired', 'received_mbps')['percentage_point_change'])
        self.assertNotIn('accepted', report)
        self.assertNotIn('improved', report)

    def test_raw_values_and_input_objects_are_preserved(self):
        inputs = self.stages()
        before = deepcopy(inputs)
        report = compare_performance(*inputs)
        self.assertEqual(inputs, before)
        for name, original in zip(('healthy', 'impaired', 'post'), inputs):
            self.assertEqual(json.loads(report['stages'][name]['raw_measurement_json']), original)
        report['stages']['healthy']['metrics']['received_mbps']['value'] = 99
        self.assertEqual(inputs, before)

    def test_zero_baseline_keeps_absolute_change_but_relative_change_is_undefined(self):
        before, after = measurement(0, 0, 0, 0, 0), measurement(5, 0.02, 10, 1, 0.5)
        report = compare_performance(before, before, after)
        for name in ('received_mbps', 'loss_fraction', 'loaded_rtt_mean_ms', 'jitter_ms', 'max_link_utilization'):
            change = metric(report, 'post_vs_healthy', name)
            self.assertEqual(change['status'], 'compared')
            self.assertIsNotNone(change['absolute_change'])
            self.assertIsNone(change['relative_change_percent'])
            self.assertIn('zero baseline', change['relative_change_reason'])
        same = compare_performance(before, before, before)
        change = metric(same, 'post_vs_healthy', 'received_mbps')
        self.assertEqual(change['absolute_change'], 0)
        self.assertIsNone(change['relative_change_percent'])

    def test_missing_stage_or_metric_never_becomes_zero_or_an_improvement(self):
        report = compare_performance(measurement(), measurement(), None)
        for change in report['comparisons']['post_vs_impaired']['metrics'].values():
            self.assertEqual(change['status'], 'unknown')
            self.assertIsNone(change['after'])
            self.assertIsNone(change['absolute_change'])
            self.assertIsNone(change['relative_change_percent'])
        post = measurement()
        post['traffic'].pop('jitter_ms')
        post['traffic'].pop('loaded_rtt')
        report = compare_performance(measurement(), measurement(), post)
        self.assertIsNone(metric(report, 'post_vs_impaired', 'jitter_ms')['after'])
        self.assertIsNone(metric(report, 'post_vs_impaired', 'loaded_rtt_mean_ms')['after'])

    def test_invalid_numeric_fields_are_unknown_but_raw_data_survives(self):
        for value in (True, False, '1.5', [], {}, -1, float('nan'), float('inf'), 10**1000):
            with self.subTest(value=value):
                post = measurement()
                post['traffic']['jitter_ms'] = value
                report = compare_performance(measurement(), measurement(), post)
                change = metric(report, 'post_vs_impaired', 'jitter_ms')
                self.assertEqual(change['status'], 'unknown')
                self.assertEqual(change['after_status'], 'invalid')
                self.assertIsNone(change['absolute_change'])
                self.assertIn('jitter_ms', report['stages']['post']['raw_measurement_json'])
                json.dumps(report, allow_nan=False)
        post = measurement(loss=1.1)
        self.assertIsNone(metric(compare_performance(measurement(), measurement(), post),
                                 'post_vs_impaired', 'loss_fraction')['after'])

    def test_status_flags_override_numeric_values_and_partial_quality_is_retained(self):
        for status in ('missing', 'unavailable', 'invalid'):
            with self.subTest(status=status):
                post = measurement()
                post['traffic']['jitter_status'] = status
                post['traffic']['loaded_rtt']['status'] = status
                report = compare_performance(measurement(), measurement(), post)
                for name in ('jitter_ms', 'loaded_rtt_mean_ms'):
                    self.assertIsNone(metric(report, 'post_vs_impaired', name)['after'])
        post = measurement(rtt=20)
        post['traffic']['loaded_rtt'].update(status='partial', samples_failed=3)
        report = compare_performance(measurement(), measurement(), post)
        self.assertEqual(metric(report, 'post_vs_impaired', 'loaded_rtt_mean_ms')['after_status'], 'partial')
        self.assertEqual(metric(report, 'post_vs_impaired', 'loaded_rtt_mean_ms')['absolute_change'], 10)

    def test_links_use_union_and_absent_interfaces_are_not_treated_as_zero(self):
        healthy, impaired, post = self.stages()
        post['traffic']['link_utilization']['sea:Ethernet2'] = 0.2
        healthy['traffic']['link_utilization']['den:Ethernet1'] = 0.1
        report = compare_performance(healthy, impaired, post)
        links = {row['interface']: row for row in report['links']}
        self.assertEqual(set(links), {'sea:Ethernet1', 'sea:Ethernet2', 'den:Ethernet1'})
        new = links['sea:Ethernet2']['comparisons']['post_vs_healthy']['metrics']['utilization']
        self.assertIsNone(new['before'])
        self.assertEqual(new['status'], 'unknown')
        main = links['sea:Ethernet1']['comparisons']['post_vs_impaired']['metrics']['utilization']
        self.assertAlmostEqual(main['absolute_change'], -0.6)
        self.assertAlmostEqual(main['percentage_point_change'], -60)

    def test_changed_workloads_are_not_comparable(self):
        for key, value in (('destination', 'nyc'), ('destination_port', 5202),
                           ('offered_mbps', 10), ('seconds', 30)):
            with self.subTest(key=key):
                post = measurement()
                post['traffic'][key] = value
                report = compare_performance(measurement(), measurement(), post)
                change = metric(report, 'post_vs_impaired', 'received_mbps')
                self.assertEqual(change['status'], 'not_comparable')
                self.assertIsNone(change['absolute_change'])
                self.assertEqual(metric(report, 'post_vs_impaired', 'idle_rtt_mean_ms')['status'], 'compared')

    def test_legacy_incomplete_metadata_and_capacity_changes_are_flagged(self):
        healthy, impaired, post = self.stages()
        for item in (healthy, impaired, post):
            item['traffic'].pop('seconds')
        post['traffic']['link_capacities_mbps']['sea-sva'] = 2
        report = compare_performance(healthy, impaired, post)
        self.assertEqual(metric(report, 'post_vs_impaired', 'received_mbps')['status'], 'compared')
        warnings = report['comparisons']['post_vs_impaired']['warnings']
        self.assertTrue(any('duration' in warning for warning in warnings))
        self.assertTrue(any('Capacity denominators' in warning for warning in warnings))

    def batch(self):
        item = measurement()
        first = deepcopy(item['traffic'])
        second = deepcopy(first)
        second.update(source='hou', destination='atl', offered_mbps=3, received_mbps=3)
        for flow in (first, second):
            flow.pop('loaded_rtt')
        item['traffic'].update(mode='concurrent_udp', flows=[first, second], concurrency_verified=True,
                               aggregate={'received_mbps': 7.5, 'loss_fraction': 0.02,
                                          'throughput_status': 'measured', 'loss_status': 'measured'})
        return item

    def test_concurrent_flows_match_by_endpoints_and_port_not_list_order(self):
        healthy = self.batch()
        post = deepcopy(healthy)
        post['traffic']['flows'].reverse()
        for flow in post['traffic']['flows']:
            flow['jitter_ms'] += 1
        report = compare_performance(healthy, healthy, post)
        self.assertEqual(len(report['flows']), 2)
        self.assertEqual(metric(report, 'post_vs_impaired', 'received_mbps')['absolute_change'], 0)
        self.assertIsNone(metric(report, 'post_vs_impaired', 'jitter_ms')['after'])
        for flow in report['flows']:
            change = flow['comparisons']['post_vs_impaired']['metrics']['jitter_ms']
            self.assertEqual(change['absolute_change'], 1)
            self.assertIsNone(flow['stages']['post']['metrics']['loaded_rtt_mean_ms']['value'])

    def test_single_flow_and_batch_aggregates_are_not_mixed(self):
        report = compare_performance(measurement(), measurement(), self.batch())
        change = metric(report, 'post_vs_impaired', 'received_mbps')
        self.assertEqual(change['status'], 'not_comparable')
        self.assertIn('different scopes', change['reason'])

    def test_duplicate_flow_identity_rejects_instead_of_overwriting(self):
        item = self.batch()
        item['traffic']['flows'].append(deepcopy(item['traffic']['flows'][0]))
        with self.assertRaisesRegex(ValueError, 'Duplicate flow identity'):
            compare_performance(item, item, item)

    def test_relative_overflow_is_unknown_and_strict_json_remains_valid(self):
        before, after = measurement(jitter=1e-308), measurement(jitter=1e308)
        report = compare_performance(before, before, after)
        change = metric(report, 'post_vs_impaired', 'jitter_ms')
        self.assertEqual(change['absolute_change'], 1e308)
        self.assertIsNone(change['relative_change_percent'])
        self.assertIn('overflow', change['relative_change_reason'])
        json.dumps(report, allow_nan=False)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_trial_report_keeps_rejections_and_selects_only_recorded_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write(root/'healthy-measurements.json', measurement())
            self.write(root/'impaired-measurements.json', measurement(2))
            self.write(root/'attempt-1/verification.json', {'accepted': False})
            self.write(root/'attempt-2/measurements.json', measurement(3))
            self.write(root/'attempt-2/acceptance.json', {'accepted': False, 'details': ['rejected']})
            self.write(root/'attempt-10/measurements.json', measurement(4.9))
            self.write(root/'attempt-10/acceptance.json', {'accepted': True, 'details': ['accepted']})
            self.write(root/'result.json', {'trial_id': 'synthetic', 'success': True, 'attempts': 10})
            before = {p: p.read_bytes() for p in root.rglob('*.json')}
            report = compare_trial_artifacts(root)
            self.assertEqual([item['attempt'] for item in report['attempts']], [1, 2, 10])
            self.assertEqual(report['accepted_post_attempt'], 10)
            self.assertFalse(report['attempts'][1]['recorded_acceptance']['accepted'])
            self.assertIsNone(metric(report['attempts'][0]['comparison'], 'post_vs_impaired', 'received_mbps')['after'])
            self.assertEqual(metric(report['comparison'], 'post_vs_impaired', 'received_mbps')['after'], 4.9)
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob('*.json')})

    def test_failed_trial_does_not_use_candidate_or_reset_as_successful_post_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write(root/'healthy-measurements.json', measurement())
            self.write(root/'impaired-measurements.json', measurement(2))
            self.write(root/'attempt-1/measurements.json', measurement(3))
            self.write(root/'attempt-1/acceptance.json', {'accepted': True})
            self.write(root/'reset-measurements.json', measurement())
            self.write(root/'result.json', {'success': False, 'attempts': 1})
            report = compare_trial_artifacts(root)
            self.assertIsNone(report['accepted_post_attempt'])
            self.assertIsNone(metric(report['comparison'], 'post_vs_impaired', 'received_mbps')['after'])
            self.assertEqual(metric(report['attempts'][0]['comparison'], 'post_vs_impaired', 'received_mbps')['after'], 3)

    def test_existing_simulated_trial_artifacts_work_without_controller_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)/'trial'
            scenario = json.loads((ROOT/'scenarios/sea-host-interface-down.json').read_text())
            result = Trial(FakeLab(), FakeDecision(proposal()), FakeVerifier(), root, max_attempts=1).run(scenario)
            self.assertTrue(result['success'])
            report = compare_trial_artifacts(root)
            self.assertEqual(report['accepted_post_attempt'], 1)
            self.assertEqual(metric(report['comparison'], 'post_vs_healthy', 'idle_rtt_mean_ms')['absolute_change'], 0)
            self.assertEqual(metric(report['comparison'], 'post_vs_healthy', 'received_mbps')['status'], 'unknown')

    def test_cli_reads_explicit_stages_records_hashes_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for stage, value in zip(('healthy', 'impaired', 'post'), self.stages()):
                self.write(root/(stage+'.json'), value)
            argv = [sys.executable, str(ROOT/'scripts/compare-performance.py')]
            for stage in ('healthy', 'impaired', 'post'):
                argv += ['--'+stage, str(root/(stage+'.json'))]
            output = root/'comparison.json'
            process = subprocess.run([*argv, '--output', str(output)], capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(len(report['source_files']), 3)
            for stage in ('healthy', 'impaired', 'post'):
                path = root/(stage+'.json')
                self.assertEqual(report['source_files'][str(path)], hashlib.sha256(path.read_bytes()).hexdigest())
            old = output.read_bytes()
            process = subprocess.run([*argv, '--output', str(output)], capture_output=True, text=True)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(output.read_bytes(), old)
            raw = (root/'healthy.json').read_bytes()
            process = subprocess.run([*argv, '--output', str(root/'healthy.json')], capture_output=True, text=True)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual((root/'healthy.json').read_bytes(), raw)

    def test_cli_handles_incomplete_trial_and_rejects_ambiguous_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write(root/'healthy-measurements.json', measurement())
            argv = [sys.executable, str(ROOT/'scripts/compare-performance.py'), str(root)]
            process = subprocess.run(argv, capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads(process.stdout)
            self.assertIsNone(report['accepted_post_attempt'])
            self.assertEqual(report['attempts'], [])
            process = subprocess.run([*argv, '--healthy', str(root/'healthy-measurements.json')],
                                     capture_output=True, text=True)
            self.assertEqual(process.returncode, 2)
            process = subprocess.run([*argv, '--output', str(root/'result.json')],
                                     capture_output=True, text=True)
            self.assertEqual(process.returncode, 2)
            self.assertFalse((root/'result.json').exists())


if __name__ == '__main__':
    unittest.main()
