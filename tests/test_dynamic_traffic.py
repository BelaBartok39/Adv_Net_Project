"""Offline CLI/replay/evidence tests using the real scheduler and mocked Docker."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from advnet.dynamic_scheduler import ScheduleInterrupted
from advnet.dynamic_traffic import execute_workload, load_schedule, main, resolve_file
from advnet.dynamic_udp import run_dynamic_udp
from test_dynamic_scheduler import FakeClock
from test_dynamic_udp import DockerHarness, MANIFEST, ROOT, flow
from test_dynamic_live import _run_cli


class DynamicTrafficTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.input = self.base/'workload.json'
        self.manifest = self.base/'manifest.json'
        self.manifest.write_text(json.dumps(MANIFEST))
        self.input.write_text(json.dumps({'duration_seconds': 6, 'seed': 7311, 'flows': [
            flow(stop_seconds=5, rate_changes=[{'at_seconds': 1, 'offered_mbps': 10}],
                 bursts=[{'start_seconds': 2, 'stop_seconds': 3, 'offered_mbps': 40}]),
            flow('b', source='den', destination='nyc', start_seconds=0.5, stop_seconds=4)]}))
        self.clock = FakeClock()
        self.docker = DockerHarness(self.clock)

    def runner(self, resolved, manifest, **options):
        return run_dynamic_udp(resolved, manifest, lock_path=self.base/'lab.lock', **options)

    def execute(self, *, source=None, output='run', **options):
        with patch('subprocess.run', side_effect=self.docker), \
                patch('subprocess.Popen', side_effect=AssertionError('Unexpected subprocess')):
            return execute_workload(source or self.input, self.manifest, self.base/output,
                                    allow_live=True, clock=self.clock, runner=self.runner,
                                    poll_interval_seconds=0.25, **options)

    def read(self, filename, output='run'):
        return json.loads((self.base/output/filename).read_text())

    def test_resolve_is_offline_and_identical_seeded_inputs_produce_identical_saved_schedules(self):
        profile = ROOT/'experiments/dynamic/seeded.json'
        with patch('subprocess.run', side_effect=AssertionError('Network operation')):
            first = resolve_file(profile, self.manifest, self.base/'first.json')
            second = resolve_file(profile, self.manifest, self.base/'second.json')
        self.assertEqual(first['seed'], 7311)
        self.assertEqual(first['schedule_sha256'], second['schedule_sha256'])
        self.assertEqual((self.base/'first.json').read_bytes(), (self.base/'second.json').read_bytes())

    def test_same_seedless_resolved_schedule_replays_without_any_random_generation(self):
        raw = json.loads((ROOT/'experiments/dynamic/seeded.json').read_text())
        raw['seed'] = None
        self.input.write_text(json.dumps(raw))
        resolved_file = self.base/'resolved.json'
        resolve_file(self.input, self.manifest, resolved_file)
        with patch('advnet.dynamic_traffic.generate_workload', side_effect=AssertionError('Replay regenerated demand')):
            self.execute(source=resolved_file, replay=True)
        self.assertEqual(self.read('resolved-workload.json'), json.loads(resolved_file.read_text()))
        self.assertIsNone(self.read('evidence.json')['seed'])

    def test_run_and_replay_preserve_identical_intended_event_order_and_schedule_hash(self):
        first = self.execute()
        self.clock.now = 200
        second = self.execute(source=self.base/'run/resolved-workload.json', output='replay', replay=True)
        self.assertEqual(first['schedule_sha256'], second['schedule_sha256'])
        self.assertEqual((first['status'], second['status']), ('completed', 'completed'))
        self.assertEqual(self.read('resolved-workload.json'), self.read('resolved-workload.json', 'replay'))
        first_events = [event['intended'] for event in self.read('execution.json')['scheduler']['events']]
        replay_events = [event['intended'] for event in self.read('execution.json', 'replay')['scheduler']['events']]
        self.assertEqual(first_events, replay_events)
        self.assertEqual(self.read('evidence.json', 'replay')['action'], 'replay')

    def test_evidence_snapshots_hashes_seed_runtime_and_shared_timeline(self):
        original = self.input.read_bytes()
        result = self.execute()
        evidence = self.read('evidence.json')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(evidence['seed'], 7311)
        self.assertEqual(evidence['scope'], 'traffic_generation')
        self.assertEqual((self.base/'run/input-workload.json').read_bytes(), original)
        self.assertEqual(evidence['input_sha256']['workload'], hashlib.sha256(original).hexdigest())
        self.assertEqual(evidence['input_sha256']['manifest'], hashlib.sha256(self.manifest.read_bytes()).hexdigest())
        self.assertEqual(evidence['resolved_schedule_sha256'],
                         hashlib.sha256((self.base/'run/resolved-workload.json').read_bytes()).hexdigest())
        self.assertIn('pydantic', evidence['runtime'])
        self.assertEqual(evidence['timeline']['started_monotonic_seconds'], 100)
        self.assertEqual(evidence['timeline']['unit'], 'seconds')
        self.assertEqual(evidence['scheduler_options']['poll_interval_seconds'], 0.25)
        self.assertEqual(evidence['scheduler_options']['operation_timeout_seconds'], 5)
        for name, digest in evidence['code_sha256'].items():
            self.assertEqual(digest, hashlib.sha256((ROOT/name).read_bytes()).hexdigest())
        for name, digest in evidence['artifact_sha256'].items():
            self.assertEqual(digest, hashlib.sha256((self.base/'run'/name).read_bytes()).hexdigest())
        self.assertNotIn('evidence.json', evidence['artifact_sha256'])
        self.assertNotIn('controller_decisions', evidence)

    def test_inputs_and_clock_origin_are_published_before_any_client_creation(self):
        def inspect_start(argv, options):
            evidence = self.read('evidence.json')
            self.assertEqual(evidence['status'], 'running')
            self.assertEqual(evidence['timeline']['started_monotonic_seconds'], 100)
            self.assertTrue((self.base/'run/resolved-workload.json').is_file())
            self.assertEqual(self.read('manifest.json'), MANIFEST)
        self.docker.effects['create'] = inspect_start
        self.execute()

    def test_lifecycle_records_share_experiment_id_preserve_unknown_timing_and_restart_gaps(self):
        self.execute()
        records = [json.loads(line) for line in (self.base/'run/flow-lifecycle.jsonl').read_text().splitlines()]
        self.assertEqual([record['sequence'] for record in records], list(range(len(records))))
        self.assertEqual({record['experiment_id'] for record in records}, {self.read('evidence.json')['experiment_id']})
        times = [record['at_seconds'] for record in records if record['at_seconds'] is not None]
        self.assertEqual(times, sorted(times))
        self.assertIn('client_created', {record['kind'] for record in records})
        self.assertIn('client_removed', {record['kind'] for record in records})
        summaries = [record for record in records if record['kind'] == 'client_final_state']
        self.assertTrue(summaries)
        self.assertTrue(all(record['at_seconds'] is None and record['timing_status'] == 'unavailable' for record in summaries))
        changes = [record for record in records if record['kind'] == 'rate_change_finished']
        self.assertTrue(all(record['traffic_gap_seconds'] is None and record['gap_status'] == 'unverified' for record in changes))

    def test_measurements_reuse_receiver_metrics_with_units_without_offered_load_substitution(self):
        self.execute()
        measurements = self.read('measurements.json')
        self.assertEqual(measurements['units'], {'received_mbps': 'Mbps', 'loss_fraction': 'fraction', 'jitter_ms': 'ms'})
        self.assertEqual(measurements['segments'][0]['metrics']['received_mbps'], 4.7)
        self.assertEqual(measurements['throughput_status'], 'available')
        self.assertNotIn('offered_mbps', measurements['segments'][0])
        self.assertEqual(self.read('execution.json')['segments'][0]['offered_mbps'], 5)

    def test_missing_receiver_data_stays_unavailable_and_completed_control_is_separate(self):
        self.docker.reports[f'{1:064x}'] = {'error': 'interrupt - the client has terminated'}
        result = self.execute()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['throughput_status'], 'partial')
        metrics = self.read('measurements.json')['segments'][0]
        self.assertIsNone(metrics['metrics']['received_mbps'])
        self.assertEqual(metrics['metrics']['throughput_status'], 'unavailable')
        self.assertIsNotNone(metrics['measurement_error'])

    def test_failed_client_and_cleanup_errors_are_saved_with_skipped_events(self):
        self.docker.effects['start'] = RuntimeError('synthetic start failure')
        self.docker.effects['rm'] = RuntimeError('synthetic cleanup failure')
        result = self.execute()
        evidence = self.read('evidence.json')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('start failure', evidence['error']['message'])
        self.assertIn('cleanup failure', evidence['cleanup_error']['message'])
        self.assertFalse(evidence['cleanup_succeeded'])
        self.assertTrue(any(event['status'] == 'skipped' for event in self.read('execution.json')['scheduler']['events']))

    def test_lock_failure_before_execution_is_recorded_without_fabricated_results(self):
        def fail(*args, **kwargs):
            raise RuntimeError('Another Abilene experiment is running')
        with self.assertRaisesRegex(RuntimeError, 'Another Abilene experiment'):
            execute_workload(self.input, self.manifest, self.base/'run', allow_live=True, runner=fail, clock=self.clock)
        evidence = self.read('evidence.json')
        self.assertEqual(evidence['status'], 'failed')
        self.assertIsNone(evidence['cleanup_succeeded'])
        self.assertIsNone(evidence['timeline']['started_monotonic_seconds'])
        self.assertFalse((self.base/'run/execution.json').exists())

    def test_interrupt_persists_partial_evidence_and_propagates_after_cleanup(self):
        def interrupt():
            raise KeyboardInterrupt()
        self.clock.on_sleep = interrupt
        with self.assertRaises(ScheduleInterrupted):
            self.execute()
        evidence = self.read('evidence.json')
        self.assertEqual(evidence['status'], 'interrupted')
        self.assertTrue(evidence['cleanup_succeeded'])
        self.assertTrue((self.base/'run/flow-lifecycle.jsonl').is_file())
        self.assertFalse(self.docker.containers)

    def test_system_exit_persists_partial_evidence_and_preserves_exit_code(self):
        def exit_run():
            raise SystemExit(3)
        self.clock.on_sleep = exit_run
        with self.assertRaises(SystemExit) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(self.read('evidence.json')['status'], 'interrupted')
        self.assertFalse(self.docker.containers)

    def test_replay_rejects_generator_inputs_and_inconsistent_schedules(self):
        for source in ((ROOT/'experiments/dynamic/seeded.json').read_bytes(), b'{"events": [], "flows": []}'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                load_schedule(source, MANIFEST, replay=True)

    def test_all_examples_validate_and_burst_and_sustained_profiles_have_rate_events(self):
        profiles = {path.stem: load_schedule(path.read_bytes(), MANIFEST)[0]
                    for path in (ROOT/'experiments/dynamic').glob('*.json')}
        self.assertEqual(set(profiles), {'light', 'moderate', 'heavy', 'burst', 'sustained', 'seeded'})
        for name in ('burst', 'sustained'):
            self.assertTrue(any(event.action == 'set_rate' for event in profiles[name].events))
        self.assertEqual(len(profiles['heavy'].flows), 4)

    def test_invalid_duplicate_nonfinite_and_mixed_json_fail_offline(self):
        values = (b'[]', b'{"duration_seconds": 1, "duration_seconds": 2}', b'{"duration_seconds": NaN}',
                  b'{"duration_seconds": 1, "flows": [], "flow_count": 1}')
        with patch('subprocess.run', side_effect=AssertionError('Network operation')):
            for data in values:
                with self.subTest(data=data), self.assertRaises(ValueError):
                    load_schedule(data, MANIFEST)

    def test_output_files_and_directories_are_never_overwritten(self):
        path = self.base/'resolved.json'
        path.write_text('preserve this')
        with self.assertRaises(FileExistsError):
            resolve_file(self.input, self.manifest, path)
        self.assertEqual(path.read_text(), 'preserve this')
        self.execute()
        saved = (self.base/'run/evidence.json').read_bytes()
        count = len(self.docker.calls)
        with self.assertRaises(FileExistsError):
            self.execute()
        self.assertEqual((self.base/'run/evidence.json').read_bytes(), saved)
        self.assertEqual(len(self.docker.calls), count)

    def test_live_opt_in_and_invalid_settings_fail_before_output_creation(self):
        with self.assertRaises(PermissionError):
            execute_workload(self.input, self.manifest, self.base/'run')
        for name, value in (('timeout_seconds', float('nan')), ('operation_timeout_seconds', 0),
                             ('cleanup_timeout_seconds', True), ('unknown_setting', 1)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                execute_workload(self.input, self.manifest, self.base/'run', allow_live=True, **{name: value})
        self.assertFalse((self.base/'run').exists())

    def test_failure_saving_evidence_does_not_hide_original_error(self):
        from advnet import dynamic_traffic
        atomic_write = dynamic_traffic._atomic_write
        def failing_write(path, data):
            if path.name == 'evidence.json' and json.loads(data)['status'] != 'running':
                raise OSError('synthetic disk full')
            return atomic_write(path, data)
        def fail(*args, **kwargs):
            raise RuntimeError('original execution error')
        with patch.object(dynamic_traffic, '_atomic_write', side_effect=failing_write), \
                self.assertRaisesRegex(RuntimeError, 'original execution error') as caught:
            execute_workload(self.input, self.manifest, self.base/'run', allow_live=True, runner=fail)
        self.assertTrue(any('disk full' in note for note in caught.exception.__notes__))

    def test_disk_failure_retains_returned_client_error_and_cleanup_result(self):
        from advnet import dynamic_traffic
        atomic_write = dynamic_traffic._atomic_write
        def failing_write(path, data):
            if path.name == 'execution.json':
                raise OSError('synthetic disk full')
            return atomic_write(path, data)
        self.docker.effects['start'] = RuntimeError('synthetic client failure')
        with patch.object(dynamic_traffic, '_atomic_write', side_effect=failing_write), \
                self.assertRaisesRegex(OSError, 'disk full') as caught:
            self.execute()
        self.assertTrue(any('client failure' in note for note in caught.exception.__notes__))
        self.assertTrue(caught.exception.traffic_result['scheduler']['cleanup_succeeded'])
        self.assertFalse(self.docker.containers)

    def test_cli_resolve_run_replay_exit_status_and_opt_in(self):
        with redirect_stdout(io.StringIO()) as stdout:
            code = main(['resolve', '--workload', str(self.input), '--manifest', str(self.manifest),
                         '--output', str(self.base/'resolved.json')])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())['status'], 'resolved')
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(['run', '--workload', str(self.input), '--output', str(self.base/'run')])
        self.assertEqual(caught.exception.code, 2)
        for action, source_flag in (('run', '--workload'), ('replay', '--schedule')):
            with patch('advnet.dynamic_traffic.SystemClock', return_value=self.clock), \
                    patch('advnet.dynamic_traffic.run_dynamic_udp', side_effect=self.runner), \
                    patch('subprocess.run', side_effect=self.docker), redirect_stdout(io.StringIO()) as stdout:
                code = main([action, source_flag, str(self.input if action == 'run' else self.base/'resolved.json'),
                             '--manifest', str(self.manifest), '--output', str(self.base/action),
                             '--allow-live', '--poll-interval-seconds', '0.25'])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())['status'], 'completed')
            self.clock.now = 200

    def test_cli_failures_and_interrupt_return_nonzero(self):
        for mode in ('failure', 'interrupt'):
            with self.subTest(mode=mode):
                self.setUp()
                if mode == 'failure':
                    self.docker.effects['start'] = RuntimeError('failed start')
                else:
                    def interrupt():
                        raise KeyboardInterrupt()
                    self.clock.on_sleep = interrupt
                with patch('advnet.dynamic_traffic.SystemClock', return_value=self.clock), \
                        patch('advnet.dynamic_traffic.run_dynamic_udp', side_effect=self.runner), \
                        patch('subprocess.run', side_effect=self.docker), \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code = main(['run', '--workload', str(self.input), '--manifest', str(self.manifest),
                                 '--output', str(self.base/'run'), '--allow-live'])
                self.assertEqual(code, 1 if mode == 'failure' else 130)

    def test_live_test_timeout_requests_graceful_cleanup_before_forcing_cli_exit(self):
        for forced in (False, True):
            with self.subTest(forced=forced):
                process = Mock()
                process.poll.return_value = None
                timeout = subprocess.TimeoutExpired(['fake-cli'], 180)
                process.wait.side_effect = ([timeout, subprocess.TimeoutExpired(['fake-cli'], 60), -9]
                                            if forced else [timeout, 130])
                with patch('subprocess.Popen', return_value=process), self.assertRaises(subprocess.TimeoutExpired):
                    _run_cli(['fake-cli'], self.base)
                process.send_signal.assert_called_once_with(signal.SIGINT)
                self.assertEqual(process.kill.call_count, 1 if forced else 0)
                self.assertTrue((self.base/'stdout.txt').exists())

    def test_cli_reports_bad_manifest_without_starting_traffic(self):
        self.manifest.write_text('{}')
        with redirect_stderr(io.StringIO()), patch('subprocess.run') as run:
            code = main(['resolve', '--workload', str(self.input), '--manifest', str(self.manifest),
                         '--output', str(self.base/'resolved.json')])
        self.assertEqual(code, 1)
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
