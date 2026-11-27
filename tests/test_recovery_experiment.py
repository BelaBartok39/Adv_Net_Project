"""Offline tests for controlled fault ownership and cleanup; no Docker calls."""
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from advnet.dynamic_traffic import load_schedule
from advnet.recovery_experiment import LinkDelay, fault_session, main, timed_results, workload


class TrafficControl:
    def __init__(self):
        self.rows = [{'kind': 'tbf', 'root': True, 'handle': '8001:',
                      'options': {'rate': 8750000, 'burst': 32767, 'lat': 50000}}]
        self.calls = []
        self.queue_available = True

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        args = argv[argv.index('tc') + 1:]
        if args[0] == '-j':
            return json.dumps(self.rows)
        if args[:2] == ['qdisc', 'add']:
            self.rows.append({'kind': 'netem', 'handle': LinkDelay.handle,
                              'parent': '8001:1', 'options': {'limit': 10000,
                              'delay': {'delay': .2, 'jitter': 0, 'correlation': 0}}})
        elif args[:2] == ['qdisc', 'del']:
            self.rows = [r for r in self.rows if r.get('handle') != LinkDelay.handle]
            self.queue_available = False
        elif args[:2] == ['qdisc', 'change']:
            self.queue_available = True
        else:
            raise AssertionError('Unexpected command')
        return ''


class RecoveryExperimentTests(unittest.TestCase):
    def delay(self, tc):
        return LinkDelay(None, SimpleNamespace(descriptors={'den': 17}), execute=tc)

    def test_delay_is_a_child_and_preserves_the_existing_root(self):
        tc = TrafficControl()
        original = deepcopy(tc.rows)
        delay = self.delay(tc)
        delay.apply()
        self.assertEqual(tc.rows[0], original[0])
        delay.remove()
        delay.remove()
        self.assertEqual(tc.rows, original)
        self.assertTrue(tc.queue_available)
        self.assertTrue(any('change' in call for call in tc.calls))
        self.assertFalse(any('replace' in call for call in tc.calls))
        self.assertEqual(sum('del' in call for call in tc.calls), 1)

    def test_existing_qdisc_is_not_overwritten(self):
        tc = TrafficControl()
        tc.rows.append({'kind': 'netem', 'parent': '8001:1', 'handle': '9999:'})
        with self.assertRaisesRegex(ValueError, 'existing child'):
            self.delay(tc).apply()
        self.assertFalse(any('add' in call or 'del' in call for call in tc.calls))

    def test_changed_ownership_cannot_delete_someone_elses_qdisc(self):
        tc = TrafficControl()
        delay = self.delay(tc)
        delay.apply()
        tc.rows[1]['parent'] = '9999:1'
        with self.assertRaisesRegex(ValueError, 'ownership changed'):
            delay.remove()
        self.assertFalse(any('del' in call for call in tc.calls))

    def test_cleanup_detects_changed_bandwidth(self):
        tc = TrafficControl()
        delay = self.delay(tc)
        delay.apply()
        tc.rows[0] = {**tc.rows[0], 'options': {'rate': 12500000}}
        with self.assertRaisesRegex(ValueError, 'Root shaping changed'):
            delay.remove()

    def test_cancellation_removes_delay_before_session_returns(self):
        applied, removed = Event(), Event()

        class FakeDelay:
            def __init__(self, *args):
                pass

            def apply(self):
                applied.set()
                return []

            def remove(self):
                removed.set()

        with TemporaryDirectory() as folder, patch('advnet.recovery_experiment.LinkDelay', FakeDelay):
            owner = SimpleNamespace(stop=Event())
            collector = SimpleNamespace(lab=None)
            with self.assertRaisesRegex(RuntimeError, 'synthetic cancellation'):
                with fault_session(Path(folder), 200, 0, 180, owner, collector, None):
                    self.assertTrue(applied.wait(1))
                    raise RuntimeError('synthetic cancellation')
            self.assertTrue(removed.is_set())
            events = [json.loads(l)['event'] for l in (Path(folder) / 'fault-events.jsonl').read_text().splitlines()]
            self.assertEqual(events[-1], 'fault_removed')

    def test_workload_has_constant_demand_and_no_hidden_bursts(self):
        manifest = json.loads(Path('topologies/abilene.json').read_text())
        schedule, _ = load_schedule(json.dumps(workload(360)).encode(), manifest)
        self.assertEqual(schedule.duration_seconds, 360)
        self.assertEqual([f.offered_mbps for f in schedule.flows], [35, 25])

    def test_five_minute_fault_extends_workload_and_controller_without_changing_rates(self):
        manifest = json.loads(Path('topologies/abilene.json').read_text())
        capacities = {f'{link[side]}:{link[side + "_interface"]}': 70
                      for link in manifest['links'] for side in ('a', 'b')}
        namespaces = SimpleNamespace(descriptors={'den': 17}, capacities=lambda *args: capacities)
        lab = SimpleNamespace(configs=lambda *args: {'sea': 'fixture\n'}, wait_convergence=lambda *args, **kw: True)
        calls = []
        def timed_arm(argv, *, session_hook):
            calls.append(argv)
            with session_hook('owner', 'collector', namespaces):
                pass
            return 0
        with TemporaryDirectory() as folder, redirect_stdout(StringIO()):
            root = Path(folder) / 'trial'
            with (patch('advnet.recovery_experiment.Lab', return_value=lab),
                  patch('advnet.recovery_experiment.LabNamespaces.open', side_effect=lambda *args: nullcontext(namespaces)),
                  patch('advnet.recovery_experiment._lab_lock', side_effect=lambda *args: nullcontext()),
                  patch('advnet.recovery_experiment.setup'),
                  patch('advnet.recovery_experiment.controller.main', side_effect=timed_arm),
                  patch('advnet.recovery_experiment.fault_session', return_value=nullcontext()) as fault,
                  patch('advnet.recovery_experiment.timed_results', return_value={}),
                  patch('advnet.recovery_experiment.LinkDelay.rows', return_value=[])):
                self.assertEqual(main(['--arm', 'observe', '--output', str(root), '--fault-hold', '5m',
                                       '--openrouter-provider', 'wafer', '--max-output-tokens', '8192',
                                       '--llm-evidence', 'path-summary']), 0)
                self.assertEqual(fault.call_args.args[2:4], (60, 300))
            self.assertEqual(calls[0][calls[0].index('--duration') + 1], '480s')
            plan = json.loads((root / 'plan.json').read_text())
            self.assertEqual((plan['duration_seconds'], plan['fault_hold_seconds']), (480, 300))
            self.assertEqual(plan['llm_request_settings'], {'openrouter_provider': 'wafer', 'max_output_tokens': 8192,
                                                          'llm_evidence': 'path-summary'})
            self.assertEqual(calls[0][calls[0].index('--openrouter-provider') + 1], 'wafer')
            self.assertEqual(calls[0][calls[0].index('--max-output-tokens') + 1], '8192')
            self.assertEqual(calls[0][calls[0].index('--llm-evidence') + 1], 'path-summary')
            traffic = json.loads((root / 'workload.json').read_text())
            self.assertEqual([f['offered_mbps'] for f in traffic['flows']], [35, 25])
            self.assertTrue(all(f['stop_seconds'] == 480 for f in traffic['flows']))
            for provider, tokens, evidence in [('wafer', '4096', 'path-summary'),
                                               ('other-provider', '8192', 'path-summary'),
                                               ('wafer', '8192', 'raw')]:
                with redirect_stderr(StringIO()), patch('advnet.recovery_experiment.Lab.configs') as readback:
                    with self.assertRaises(SystemExit):
                        main(['--arm', 'observe', '--output', str(Path(folder)/'never-created'),
                              '--fault-hold', '5m', '--match', str(root/'plan.json'),
                              '--openrouter-provider', provider, '--max-output-tokens', tokens,
                              '--llm-evidence', evidence])
                    readback.assert_not_called()
                    self.assertFalse((Path(folder)/'never-created').exists())

    def test_invalid_llm_options_fail_before_lab_or_output_changes(self):
        for options in (['--max-output-tokens', '0'], ['--max-output-tokens', '65537'],
                        ['--openrouter-provider', 'invalid provider']):
            with TemporaryDirectory() as folder, redirect_stderr(StringIO()):
                output = Path(folder)/'never-created'
                with patch('advnet.recovery_experiment.Lab') as lab, self.assertRaises(SystemExit):
                    main(['--arm', 'observe', '--output', str(output), *options])
                lab.assert_not_called()
                self.assertFalse(output.exists())

    def test_fault_hold_cannot_extend_run_beyond_twenty_four_hours(self):
        with TemporaryDirectory() as folder, redirect_stderr(StringIO()):
            root = Path(folder) / 'never-created'
            with self.assertRaises(SystemExit):
                main(['--arm', 'observe', '--output', str(root), '--fault-hold', '24h'])
            self.assertFalse(root.exists())

    def test_missing_credentials_fail_before_lab_or_output_changes(self):
        with TemporaryDirectory() as folder, patch.dict('os.environ', {}, clear=True), redirect_stderr(StringIO()):
            output = Path(folder) / 'never-created'
            with self.assertRaises(SystemExit):
                main(['--arm', 'jev', '--output', str(output)])
            self.assertFalse(output.exists())

    def test_invalid_matching_plan_is_rejected_before_any_lab_access(self):
        with TemporaryDirectory() as folder, redirect_stderr(StringIO()):
            reference = Path(folder) / 'plan.json'
            reference.write_text('{}')
            output = Path(folder) / 'never-created'
            with patch('advnet.recovery_experiment.Lab.configs') as readback:
                with self.assertRaises(SystemExit):
                    main(['--arm', 'observe', '--output', str(output), '--match', str(reference)])
                readback.assert_not_called()
                self.assertFalse(output.exists())

    def test_fault_removal_cannot_be_credited_as_autonomous_recovery(self):
        for completed_at, expected in [('2026-01-01T00:03:00+00:00', 120),
                                       ('2026-01-01T00:04:00.500000+00:00', None),
                                       ('2026-01-01T00:05:00+00:00', None)]:
            with self.subTest(completed_at=completed_at), TemporaryDirectory() as folder:
                root = Path(folder)
                child = root / 'trials' / 'controller'
                child.mkdir(parents=True)
                fault = [{'event': 'fault_applied', 'at': '2026-01-01T00:01:00+00:00'},
                         {'event': 'fault_remove_started', 'at': '2026-01-01T00:04:00+00:00'},
                         {'event': 'fault_removed', 'at': '2026-01-01T00:04:01+00:00'}]
                (root / 'fault-events.jsonl').write_text('\n'.join(json.dumps(e) for e in fault))
                (child / 'events.jsonl').write_text('')
                state = {'policy': {'state': {'interventions': [
                    {'kind': 'repair', 'outcomes': [{'occurred_at': completed_at,
                                                   'outcome': {'status': 'success'}}]}]}}}
                (child / 'controller.json').write_text(json.dumps(state))
                self.assertEqual(timed_results(root)['fault_to_accepted_repair_seconds'], expected)


if __name__ == '__main__':
    unittest.main()
