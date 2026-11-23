"""Deterministic scheduler tests with fake time and an in-memory backend."""
from copy import deepcopy
import json
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import patch

from advnet.dynamic_scheduler import FlowExit, ScheduleInterrupted, run_schedule
from advnet.dynamic_workload import resolve_workload


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / 'topologies/abilene.json').read_text())


def flow(flow_id='a', **overrides):
    return {'flow_id': flow_id, 'source': 'sea', 'destination': 'sva', 'offered_mbps': 5,
            'start_seconds': 0, 'stop_seconds': 8, **overrides}


def schedule(flows=None, duration=10):
    return resolve_workload({'duration_seconds': duration, 'flows': [flow()] if flows is None else flows},
                            MANIFEST)


class FakeClock:
    def __init__(self, origin=100):
        self.origin = self.now = origin
        self.sleeps = []
        self.on_sleep = None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        if seconds <= 0:
            raise AssertionError('Nonpositive sleep')
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep()

    @property
    def elapsed(self):
        return self.now - self.origin


class FakeBackend:
    """Owned resources outlive start(); no control call waits for flow lifetime."""
    def __init__(self, clock):
        self.clock = clock
        self.calls, self.polls = [], []
        self.owned, self.active = set(), {}
        self.peak_active = 0
        self.effects, self.delays, self.exits = {}, {}, {}
        self.mutate_flow = False

    def control(self, action, flow_id, timeout, rate=None):
        self.calls.append((action, flow_id, self.clock.elapsed, rate, timeout))
        self.clock.now += self.delays.get((action, flow_id), 0)
        error = self.effects.get((action, flow_id))
        if error:
            raise error

    def start(self, flow, offered_mbps, *, timeout_seconds):
        self.owned.add(flow.flow_id)  # Includes partially failed starts.
        self.control('start', flow.flow_id, timeout_seconds, offered_mbps)
        self.active[flow.flow_id] = offered_mbps
        self.peak_active = max(self.peak_active, len(self.active))
        if self.mutate_flow:
            flow.destination = 'not-in-the-inventory'

    def set_rate(self, flow_id, offered_mbps, *, timeout_seconds):
        self.control('set_rate', flow_id, timeout_seconds, offered_mbps)
        self.active[flow_id] = offered_mbps

    def stop(self, flow_id, *, timeout_seconds):
        self.control('stop', flow_id, timeout_seconds)
        del self.active[flow_id]
        self.owned.remove(flow_id)

    def poll(self, flow_id, *, timeout_seconds):
        self.polls.append((flow_id, self.clock.elapsed, timeout_seconds))
        self.clock.now += self.delays.get(('poll', flow_id), 0)
        error = self.effects.get(('poll', flow_id))
        if error:
            raise error
        when, exit_status = self.exits.get(flow_id, (float('inf'), None))
        return exit_status if self.clock.elapsed >= when else None

    def cleanup(self, *, timeout_seconds):
        self.control('cleanup', None, timeout_seconds)
        self.active.clear()
        self.owned.clear()


class DynamicSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.backend = FakeBackend(self.clock)

    def run_schedule(self, resolved=None, **options):
        return run_schedule(schedule() if resolved is None else resolved, MANIFEST, self.backend,
                            clock=self.clock, poll_interval_seconds=0.25, **options)

    def test_absolute_times_and_idle_tail_use_one_monotonic_origin(self):
        result = self.run_schedule(schedule([flow(start_seconds=3, stop_seconds=7)]))
        self.assertEqual(result.status, 'completed')
        self.assertEqual([call[:3] for call in self.backend.calls],
                         [('start', 'a', 3), ('stop', 'a', 7), ('cleanup', None, 10)])
        self.assertEqual(result.finished_seconds, 10)
        self.assertEqual([event.actual_seconds for event in result.events], [3, 7])
        self.assertEqual([event.lateness_seconds for event in result.events], [0, 0])
        self.assertTrue(result.cleanup_succeeded)
        self.assertFalse(self.backend.owned)

    def test_second_arrival_does_not_wait_for_first_flow_to_finish(self):
        result = self.run_schedule(schedule([flow(), flow('b', destination='nyc',
                                                          start_seconds=1, stop_seconds=4)]))
        self.assertEqual(result.status, 'completed')
        self.assertEqual(self.backend.peak_active, 2)
        self.assertEqual([call[:3] for call in self.backend.calls[:4]],
                         [('start', 'a', 0), ('start', 'b', 1), ('stop', 'b', 4), ('stop', 'a', 8)])
        self.assertTrue(self.backend.polls)

    def test_rate_changes_and_bursts_use_effective_rates(self):
        result = self.run_schedule(schedule([flow(
            rate_changes=[{'at_seconds': 2, 'offered_mbps': 7}, {'at_seconds': 4, 'offered_mbps': 10}],
            bursts=[{'start_seconds': 3, 'stop_seconds': 6, 'offered_mbps': 40}])]))
        self.assertEqual(result.status, 'completed')
        self.assertEqual([call[:4] for call in self.backend.calls[:-1]], [
            ('start', 'a', 0, 5), ('set_rate', 'a', 2, 7), ('set_rate', 'a', 3, 40),
            ('set_rate', 'a', 6, 10), ('stop', 'a', 8, None)])

    def test_same_timestamp_departures_rates_and_arrivals_preserve_sequence(self):
        resolved = schedule([
            flow('z', stop_seconds=5),
            flow('b', destination='nyc', rate_changes=[{'at_seconds': 5, 'offered_mbps': 6}]),
            flow('a', destination='den', rate_changes=[{'at_seconds': 5, 'offered_mbps': 7}]),
            flow('new-z', start_seconds=5), flow('new-a', destination='lax', start_seconds=5)])
        result = self.run_schedule(resolved)
        self.assertEqual(result.status, 'completed')
        self.assertEqual([(call[0], call[1]) for call in self.backend.calls if call[2] == 5],
                         [('stop', 'z'), ('set_rate', 'b'), ('set_rate', 'a'),
                          ('start', 'new-z'), ('start', 'new-a')])
        self.assertEqual([(e.intended.action, e.intended.flow_id) for e in result.events],
                         [(call[0], call[1]) for call in self.backend.calls[:-1]])
        self.assertEqual(self.backend.peak_active, 4)

    def test_fractional_event_times_are_not_rounded_to_poll_interval(self):
        result = self.run_schedule(schedule([flow(start_seconds=0.125, stop_seconds=0.375)], duration=0.5))
        self.assertEqual([e.actual_seconds for e in result.events], [0.125, 0.375])
        self.assertEqual(result.finished_seconds, 0.5)

    def test_decimal_targets_and_deadlines_do_not_stall_due_to_float_rounding(self):
        result = self.run_schedule(schedule([flow(start_seconds=0.1, stop_seconds=0.3)], duration=0.6))
        self.assertEqual(result.status, 'completed')
        self.assertAlmostEqual(result.events[0].actual_seconds, 0.1)
        self.assertAlmostEqual(result.events[1].actual_seconds, 0.3)
        self.assertTrue(all(e.lateness_seconds >= 0 for e in result.events))
        self.setUp()
        result = self.run_schedule(timeout_seconds=0.1)
        self.assertEqual(result.status, 'timed_out')
        self.assertAlmostEqual(result.finished_seconds, 0.1)

    def test_backend_latency_is_recorded_without_shifting_later_targets(self):
        self.backend.delays['start', 'a'] = 0.5
        resolved = schedule([flow(), flow('b', destination='nyc', start_seconds=0,
                                          rate_changes=[{'at_seconds': 2, 'offered_mbps': 8}])])
        result = self.run_schedule(resolved)
        self.assertEqual(result.events[0].finished_seconds, 0.5)
        self.assertEqual(result.events[1].intended.at_seconds, 0)
        self.assertEqual(result.events[1].actual_seconds, 0.5)
        self.assertEqual(result.events[1].lateness_seconds, 0.5)
        self.assertEqual(result.events[2].actual_seconds, 2)

    def test_failed_start_rate_change_or_stop_aborts_and_cleans_up(self):
        resolved = schedule([flow(rate_changes=[{'at_seconds': 2, 'offered_mbps': 8}])])
        for action, completed in (('start', 0), ('set_rate', 1), ('stop', 2)):
            with self.subTest(action=action):
                self.setUp()
                self.backend.effects[action, 'a'] = RuntimeError(action + ' failed')
                result = self.run_schedule(resolved)
                self.assertEqual(result.status, 'failed')
                self.assertEqual(result.error.flow_id, 'a')
                self.assertEqual(result.events[completed].status, 'failed')
                self.assertTrue(all(e.status == 'skipped' for e in result.events[completed + 1:]))
                self.assertTrue(result.cleanup_succeeded)
                self.assertFalse(self.backend.owned)

    def test_partial_start_failure_cleans_all_other_active_flows(self):
        self.backend.effects['start', 'b'] = RuntimeError('failed after allocating a resource')
        result = self.run_schedule(schedule([flow(), flow('b', destination='nyc', start_seconds=1)]))
        self.assertEqual(result.status, 'failed')
        self.assertTrue(result.cleanup_succeeded)
        self.assertFalse(self.backend.owned)
        self.assertFalse(self.backend.active)
        self.assertEqual(sum(call[0] == 'cleanup' for call in self.backend.calls), 1)

    def test_early_exit_even_with_zero_returncode_is_partial_failure(self):
        for returncode in (0, 1, -9):
            with self.subTest(returncode=returncode):
                self.setUp()
                self.backend.exits['a'] = (1, FlowExit(returncode=returncode, error='synthetic exit'))
                result = self.run_schedule()
                self.assertEqual(result.status, 'failed')
                self.assertEqual(result.flow_exits[0].exit.returncode, returncode)
                self.assertEqual(result.flow_exits[0].at_seconds, 1)
                self.assertEqual(result.events[1].status, 'skipped')
                self.assertTrue(result.cleanup_succeeded)

    def test_poll_failure_or_invalid_return_value_aborts_with_flow_identity(self):
        for mode in ('exception', 'malformed'):
            with self.subTest(mode=mode):
                self.setUp()
                if mode == 'exception':
                    self.backend.effects['poll', 'a'] = RuntimeError('poll failed')
                else:
                    self.backend.exits['a'] = (0, {'returncode': 'not an integer'})
                result = self.run_schedule()
                self.assertEqual(result.status, 'failed')
                self.assertEqual(result.error.flow_id, 'a')
                self.assertTrue(result.cleanup_succeeded)

    def test_deadline_before_first_arrival_skips_all_events(self):
        result = self.run_schedule(schedule([flow(start_seconds=3)]), timeout_seconds=1.5)
        self.assertEqual(result.status, 'timed_out')
        self.assertEqual([e.status for e in result.events], ['skipped', 'skipped'])
        self.assertEqual(self.backend.calls[0][:3], ('cleanup', None, 1.5))

    def test_deadline_while_active_still_allows_independent_cleanup_budget(self):
        result = self.run_schedule(timeout_seconds=2, cleanup_timeout_seconds=7)
        self.assertEqual(result.status, 'timed_out')
        self.assertEqual(result.finished_seconds, 2)
        self.assertEqual(result.events[0].status, 'completed')
        self.assertEqual(result.events[1].status, 'skipped')
        self.assertEqual(self.backend.calls[-1][-1], 7)
        self.assertFalse(self.backend.active)

    def test_deadline_limits_backend_operation_timeout(self):
        result = self.run_schedule(timeout_seconds=2, operation_timeout_seconds=5)
        self.assertEqual(self.backend.calls[0][-1], 2)
        self.assertTrue(all(0 < timeout <= 2 for _, _, timeout in self.backend.polls))
        self.assertEqual(result.status, 'timed_out')

    def test_exclusive_deadline_does_not_dispatch_an_event_at_expiry(self):
        result = self.run_schedule(schedule([flow(stop_seconds=2)], duration=2), timeout_seconds=2)
        self.assertEqual(result.status, 'timed_out')
        self.assertEqual(result.events[-1].status, 'skipped')
        self.assertFalse(any(call[0] == 'stop' for call in self.backend.calls))

    def test_default_deadline_allows_final_stop_at_experiment_duration(self):
        result = self.run_schedule(schedule(duration=8))
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.events[-1].actual_seconds, 8)
        self.assertEqual(result.timeout_seconds, 13)

    def test_slow_control_operation_and_backend_timeout_are_recorded(self):
        for mode in ('overrun', 'timeout'):
            with self.subTest(mode=mode):
                self.setUp()
                if mode == 'overrun':
                    self.backend.delays['start', 'a'] = 2
                else:
                    self.backend.effects['start', 'a'] = TimeoutError('synthetic timeout')
                result = self.run_schedule(operation_timeout_seconds=1)
                self.assertEqual(result.status, 'timed_out')
                self.assertEqual(result.events[0].status, 'failed')
                self.assertTrue(result.cleanup_succeeded)

    def test_slow_poll_cannot_be_reported_as_success(self):
        self.backend.delays['poll', 'a'] = 2
        result = self.run_schedule(operation_timeout_seconds=1)
        self.assertEqual(result.status, 'timed_out')
        self.assertIn('poll exceeded', result.error.message)
        self.assertTrue(result.cleanup_succeeded)

    def test_expired_flow_is_not_started_after_delayed_dispatch(self):
        self.backend.delays['start', 'a'] = 2
        result = self.run_schedule(schedule([flow(), flow('b', destination='nyc',
                                                          start_seconds=0, stop_seconds=1)]))
        self.assertEqual(result.status, 'timed_out')
        self.assertIn('lifetime elapsed', result.error.message)
        self.assertFalse(any(call[:2] == ('start', 'b') for call in self.backend.calls))

    def test_pre_cancelled_run_never_starts_traffic(self):
        result = self.run_schedule(cancelled=lambda: True)
        self.assertEqual(result.status, 'cancelled')
        self.assertTrue(all(e.status == 'skipped' for e in result.events))
        self.assertEqual([call[0] for call in self.backend.calls], ['cleanup'])

    def test_thread_event_can_cancel_between_scheduled_events(self):
        cancellation = Event()
        self.clock.on_sleep = cancellation.set
        result = self.run_schedule(cancelled=cancellation.is_set)
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(result.finished_seconds, 0.25)
        self.assertFalse(self.backend.active)

    def test_cancellation_during_flow_and_idle_tail_is_responsive(self):
        for stop, cancellation_time in ((8, 0.5), (1, 3)):
            with self.subTest(stop=stop):
                self.setUp()
                result = self.run_schedule(schedule([flow(stop_seconds=stop)]),
                                           cancelled=lambda: self.clock.elapsed >= cancellation_time)
                self.assertEqual(result.status, 'cancelled')
                self.assertEqual(result.finished_seconds, cancellation_time)
                self.assertTrue(result.cleanup_succeeded)

    def test_cancellation_after_backend_call_is_recorded_and_cleaned(self):
        self.backend.delays['start', 'a'] = 0.5
        result = self.run_schedule(cancelled=lambda: self.clock.elapsed >= 0.5)
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(result.events[0].status, 'failed')
        self.assertEqual(result.events[0].finished_seconds, 0.5)
        self.assertFalse(self.backend.active)

    def test_keyboard_interrupt_propagates_after_cleanup_with_partial_result(self):
        self.backend.effects['start', 'a'] = KeyboardInterrupt()
        with self.assertRaises(ScheduleInterrupted) as caught:
            self.run_schedule()
        result = caught.exception.result
        self.assertEqual(result.status, 'interrupted')
        self.assertEqual(result.events[0].status, 'failed')
        self.assertTrue(result.cleanup_succeeded)
        self.assertFalse(self.backend.owned)

    def test_interrupt_while_sleeping_cleans_active_flows(self):
        def interrupt():
            raise KeyboardInterrupt()
        self.clock.on_sleep = interrupt
        with self.assertRaises(ScheduleInterrupted) as caught:
            self.run_schedule()
        self.assertEqual(caught.exception.result.events[0].status, 'completed')
        self.assertTrue(caught.exception.result.cleanup_succeeded)
        self.assertFalse(self.backend.active)

    def test_system_exit_is_preserved_after_cleanup(self):
        self.backend.effects['start', 'a'] = SystemExit(3)
        with self.assertRaises(SystemExit) as caught:
            self.run_schedule()
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(caught.exception.schedule_result.status, 'interrupted')
        self.assertTrue(caught.exception.schedule_result.cleanup_succeeded)

    def test_cleanup_failure_does_not_hide_primary_error(self):
        self.backend.effects['start', 'a'] = RuntimeError('primary failure')
        self.backend.effects['cleanup', None] = RuntimeError('cleanup failure')
        result = self.run_schedule()
        self.assertEqual(result.error.message, 'primary failure')
        self.assertEqual(result.cleanup_error.message, 'cleanup failure')
        self.assertFalse(result.cleanup_succeeded)

    def test_cleanup_failure_or_overrun_prevents_success(self):
        for mode in ('failure', 'overrun'):
            with self.subTest(mode=mode):
                self.setUp()
                if mode == 'failure':
                    self.backend.effects['cleanup', None] = RuntimeError('cleanup failed')
                else:
                    self.backend.delays['cleanup', None] = 2
                result = self.run_schedule(cleanup_timeout_seconds=1)
                self.assertEqual(result.status, 'failed')
                self.assertFalse(result.cleanup_succeeded)
                self.assertIsNotNone(result.cleanup_error)

    def test_cleanup_interrupt_propagates_and_retains_evidence(self):
        self.backend.effects['cleanup', None] = KeyboardInterrupt()
        with self.assertRaises(ScheduleInterrupted) as caught:
            self.run_schedule()
        self.assertFalse(caught.exception.result.cleanup_succeeded)
        self.assertEqual(caught.exception.result.cleanup_error.error_type, 'KeyboardInterrupt')

    def test_clock_failure_or_backward_time_cannot_prevent_cleanup(self):
        for value in (99, float('nan')):
            with self.subTest(value=value):
                self.setUp()
                def break_clock():
                    self.clock.now = value
                self.clock.on_sleep = break_clock
                result = self.run_schedule()
                self.assertEqual(result.status, 'failed')
                self.assertIn('monotonic', result.error.message)
                self.assertTrue(result.cleanup_succeeded)
                self.assertIsNone(result.cleanup_started_seconds)
                self.assertIsNone(result.cleanup_finished_seconds)
                json.loads(result.model_dump_json())

    def test_lost_cleanup_timing_is_missing_data_and_prevents_success(self):
        cleanup = self.backend.cleanup
        def break_clock_after_cleanup(**options):
            cleanup(**options)
            self.clock.now = float('nan')
        self.backend.cleanup = break_clock_after_cleanup
        result = self.run_schedule()
        self.assertEqual(result.status, 'failed')
        self.assertTrue(result.cleanup_succeeded)
        self.assertIsNone(result.cleanup_finished_seconds)
        self.assertIsNone(result.finished_seconds)
        self.assertIn('monotonic', result.error.message)
        json.dumps(result.model_dump(), allow_nan=False)

    def test_preflight_rejects_bad_inputs_before_any_backend_operations(self):
        broken_schedule = schedule().model_dump()
        broken_schedule['events'][0]['at_seconds'] = 1
        unknown_host = schedule().model_dump()
        unknown_host['flows'][0]['source'] = 'unknown'
        for candidate in (broken_schedule, unknown_host):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                self.run_schedule(candidate)
        for name in ('timeout_seconds', 'operation_timeout_seconds', 'cleanup_timeout_seconds'):
            for value in (0, -1, float('inf'), float('nan'), True, '1'):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.run_schedule(**{name: value})
        for interval in (0, float('nan'), True):
            with self.assertRaises(ValueError):
                run_schedule(schedule(), MANIFEST, self.backend, clock=self.clock, poll_interval_seconds=interval)
        with self.assertRaises(ValueError):
            self.run_schedule(cancelled=True)
        self.assertFalse(self.backend.calls)
        self.assertFalse(self.backend.polls)

    def test_inputs_are_isolated_from_backend_mutation_and_results_are_json_serializable(self):
        resolved = schedule()
        saved = deepcopy(resolved.model_dump())
        self.backend.mutate_flow = True
        result = self.run_schedule(resolved)
        self.assertEqual(resolved.model_dump(), saved)
        self.assertEqual(result.status, 'completed')
        json.dumps(result.model_dump(), allow_nan=False)

    def test_default_clock_uses_monotonic_and_no_network_or_wall_clock(self):
        with patch('advnet.dynamic_scheduler.time.monotonic', side_effect=self.clock.monotonic), \
                patch('advnet.dynamic_scheduler.time.sleep', side_effect=self.clock.sleep), \
                patch('time.time', side_effect=AssertionError('Wall clock read')), \
                patch('subprocess.run', side_effect=AssertionError('Network operation')), \
                patch('subprocess.Popen', side_effect=AssertionError('Network operation')):
            result = run_schedule(schedule(), MANIFEST, self.backend, poll_interval_seconds=0.25)
        self.assertEqual(result.status, 'completed')


if __name__ == '__main__':
    unittest.main()
