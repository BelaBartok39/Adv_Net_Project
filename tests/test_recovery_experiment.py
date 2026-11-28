"""Offline tests for controlled fault ownership and cleanup; no Docker calls."""
from contextlib import redirect_stderr
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
