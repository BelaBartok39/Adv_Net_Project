"""Offline state-machine, worker, telemetry and verified-executor regressions."""
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from advnet.controller_demo import DemoDecision, ScheduledReply, demo_controller
from advnet.controller_execution import RuntimeMeasurementError, fingerprint
from advnet.controller_telemetry import CapacityEvidence, monitor_frame
from advnet.eos import normalize_readback
from advnet.persistent_controller import EngineProvider, InferenceJob, Journal
from advnet.providers import MockJev, MockLLM
from advnet.stability_policy import HealthMeasurement


class BrokenDecision:
    def __call__(self, *args):
        raise RuntimeError('secret-provider-payload')


class ExplodingJev:
    def classify(self, *args):
        raise RuntimeError('secret-provider-payload')


class SlowReply(ScheduledReply):
    def poll(self):
        return None


class SleepingProvider:
    def __call__(self, *args):
        time.sleep(15)
        return DemoDecision()(*args)


class WaitingProvider:
    def __init__(self, release):
        self.release = release

    def __call__(self, *args):
        until = time.monotonic() + 5
        while not self.release.exists():
            if time.monotonic() >= until:
                raise TimeoutError
            time.sleep(.01)
        return DemoDecision()(*args)


class PersistentControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.c, self.net = demo_controller(self.directory)
        self.c.settings.duration = 12
        self.net.load = lambda t: 0.95
        self.net.behavior = lambda t: 'success'

    def events(self, event=None):
        rows = [json.loads(line) for line in (self.directory/'events.jsonl').read_text().splitlines()]
        return [r for r in rows if event is None or r['event'] == event]

    def outcomes(self):
        return [r.outcomes[-1].outcome.status for r in self.c.policy.state.interventions]

    def test_healthy_and_transient_spike_do_not_dispatch(self):
        self.net.load = lambda t: 0.96 if t == 3 else 0.4
        self.c.run()
        self.assertFalse(self.events('decision_result'))
        self.assertEqual(self.net.writes, [])
        self.assertEqual(len(self.events('observation')), 12)

    def test_pending_request_continues_monitoring_single_dispatch_latest_assessment(self):
        self.c.run()
        self.assertEqual(len(self.events('decision_result')), 1)
        permits = [e['decision'] for e in self.events('observation')
                   if e['decision']['recommendation'].startswith('permit_')]
        self.assertEqual(len(permits), 1)
        pending = [e['decision'] for e in self.events('observation') if 'opportunity_pending' in e['decision']['reasons']]
        self.assertEqual(len(pending), 2)
        self.assertTrue(all(p['opportunity_id'] == permits[0]['opportunity_id'] for p in pending))
        reservation = next(e['decision'] for e in self.events('policy') if e['decision']['stage'] == 'reservation')
        self.assertEqual(reservation['observation_id'], pending[-1]['observation_id'])
        self.assertEqual(self.outcomes(), ['success'])
        self.assertEqual(self.net.writes, ['deploy'])
        self.assertTrue(any(e['decision']['recommendation'] == 'suppress_cooldown' for e in self.events('observation')))
        self.assertEqual(len(self.c.policy.state.changes), 1)

    def test_full_demonstration_and_restart_with_unconfirmed_readback(self):
        self.c, self.net = demo_controller(self.directory)
        self.c.run()
        self.assertEqual(self.outcomes(), ['success', 'rejected', 'rolled_back', 'rollback_unconfirmed'])
        self.assertEqual(self.net.writes, ['deploy', 'deploy', 'rollback', 'deploy', 'rollback'])
        self.assertIn('rollback_unconfirmed', self.events('observation')[-1]['decision']['reasons'])
        attempts = [e['attempt'] for e in self.events('transition') if e['phase'] == 'decision']
        self.assertEqual(attempts, [1]*4)
        active = self.c.policy.state.active.intervention_id
        self.c.settings.duration = 2
        self.c.run()
        self.assertEqual(self.c.policy.state.active.intervention_id, active)
        self.assertEqual(len(self.c.policy.state.interventions), 4)
        before = json.loads((self.directory/active/'checkpoint.json').read_text())
        self.net.config = before
        self.c.run()
        self.assertIsNone(self.c.policy.state.active)
        self.assertEqual(self.outcomes()[-1], 'rolled_back')
        self.assertEqual(self.c.policy.state.changes[-1].change.after_state, fingerprint(before))

    def test_scope_change_invalidates_late_worker(self):
        self.net.load = lambda t: 0.96 if t <= 2 else 0.3
        self.c.run()
        self.assertEqual(self.events('inference_discarded')[0]['reason'], 'opportunity_invalidated')
        self.assertFalse(self.events('decision_result'))
        self.assertFalse(self.net.writes)

    def test_expired_policy_opportunity_prevents_late_use(self):
        self.c.policy.config = self.c.policy.config.model_copy(update={'opportunity_seconds': 2})
        self.c.settings.duration = 6
        self.c.run()
        self.assertTrue(any('opportunity_expired' in e['decision']['reasons'] for e in self.events('observation')))
        self.assertFalse(self.net.writes)

    def test_inference_timeout_prevents_deployment(self):
        self.c.settings.inference_timeout = 0.4
        self.c.settings.duration = 6
        self.c.worker = SlowReply
        self.c.run()
        self.assertEqual(self.events('inference_discarded')[0]['reason'], 'inference_timeout')
        self.assertFalse(self.net.writes)
        self.assertIsNone(self.c.pending)

    def test_reply_ready_before_subtick_timeout_waits_for_fresh_observation(self):
        clock = self.c.clock

        class PromptReply(ScheduledReply):
            def __init__(self, *args):
                super().__init__(*args)
                self.ready_at = clock.monotonic() + 0.2

            def poll(self):
                if clock.monotonic() < self.ready_at:
                    return None
                self.ticks = 1
                return super().poll()

        self.c.settings.inference_timeout = 0.4
        self.c.worker = PromptReply
        self.c.run()
        self.assertFalse(self.events('inference_discarded'))
        self.assertEqual(self.outcomes(), ['success'])
        observations = self.events('observation')
        result = self.events('decision_result')[0]
        self.assertTrue(any(row['at'] == result['at'] for row in observations))

    def test_observe_only_preserves_observations_without_dispatch(self):
        self.c.settings.observe_only = True
        self.c.run()
        self.assertEqual(len(self.events('observation')), 12)
        self.assertEqual(self.events('inference_skipped')[0]['reason'], 'observe_only')
        self.assertFalse(self.events('decision_result'))
        self.assertFalse(self.net.writes)

    def test_decision_exception_and_episode_retry_bound(self):
        self.c.settings.duration = 30
        self.c.settings.max_attempts = 2
        self.c.decision = BrokenDecision()
        self.c.run()
        self.assertEqual([e['status'] for e in self.events('decision_result')], ['decision_error'] * 2)
        self.assertTrue(any(e['reason'] == 'episode_attempt_limit' for e in self.events('inference_skipped')))
        self.assertNotIn('secret-provider-payload', (self.directory/'events.jsonl').read_text())
        self.assertFalse(self.net.writes)

    def test_completion_failure_diagnostics_reach_journal_without_deployment(self):
        def truncated(*args):
            return {'status': 'decision_error', 'proposal': None, 'calls': [
                {'stage': 'llm', 'model': 'example/model', 'elapsed_ms': 500,
                 'usage': {'prompt_tokens': 100, 'completion_tokens': 4096, 'cost': .06},
                 'metadata': {'provider': 'Wafer', 'finish_reason': 'length'},
                 'error_type': 'ProviderResponseError'}],
                'error_stage': 'provider_response', 'error_type': 'ProviderResponseError',
                'provider_error_code': 'output_token_limit'}
        self.c.decision = truncated
        self.c.run()
        event = self.events('decision_result')[0]
        self.assertEqual(event['provider_error_code'], 'output_token_limit')
        self.assertEqual(event['error_stage'], 'provider_response')
        self.assertEqual(event['calls'][0]['metadata']['provider'], 'Wafer')
        self.assertEqual(event['metrics']['llm_cost_usd'], .06)
        self.assertFalse(self.net.writes)

    def test_previous_rejection_scopes_feedback_to_same_episode(self):
        self.c.settings.duration = 23
        self.net.behavior = lambda t: 'reject' if t < 7 else 'success'
        self.c.run()
        first, second = self.events('decision_result')[:2]
        self.assertEqual((first['metrics']['jev_calls'], second['metrics']['jev_calls']), (1, 0))
        self.assertEqual(self.outcomes()[:2], ['rejected', 'success'])

    def test_retry_receives_runtime_rejection_and_complete_windows(self):
        self.c.settings.duration = 23
        self.net.behavior = lambda t: 'rollback' if t < 10 else 'success'
        seen = []
        def decide(mode, observation, feedback, attempt):
            seen.append([f.model_dump(mode='json') for f in feedback])
            return DemoDecision()(mode, observation, feedback, attempt)
        self.c.decision = decide
        self.c.run()
        feedback = seen[1][0]
        self.assertTrue(feedback['rollback_succeeded'])
        self.assertTrue(any('utilization' in detail for detail in feedback['details']))
        measured = feedback['measurements']['runtime']
        self.assertEqual(set(('healthy', 'before', 'after')) - measured.keys(), set())
        self.assertEqual(measured['after']['traffic']['max_link_utilization'], .95)
        self.assertFalse(measured['acceptance']['accepted'])
        self.assertEqual(measured['acceptance_limits']['network_probe_rtt']['maximum'], 6.25)
        self.assertTrue(feedback['measurements']['rollback']['succeeded'])

    def observation_collector(self, *, recover=False, slow=False):
        test = self
        class Collector:
            calls = []
            def __call__(self, generation, deadline):
                return test.net.sample(generation, deadline)
            def observe_requests(self, requests, generation, deadline):
                self.calls.append(requests)
                test.c.clock.seconds += 1
                if slow:
                    test.c.clock.seconds += test.c.settings.observation_timeout
                deadline.remaining()
                if recover:
                    test.net.load = lambda _: .4
                frame = test.net.sample(generation, deadline)
                frame.model_input.measurements['requested_observations'] = {
                    request: {'observed_at': frame.signal.observed_at.isoformat(), 'routes': {'r1': 'fixture route'}}
                    for request in requests}
                return frame
        return Collector()

    def requesting_decision(self, requests, *, always=False, seen=None):
        def decide(mode, observation, feedback, attempt):
            if seen is not None:
                seen.append({'observation': observation.model_dump(mode='json'),
                             'feedback': [f.model_dump(mode='json') for f in feedback]})
            result = DemoDecision()(mode, observation, feedback, attempt)
            if attempt == 1 or always:
                result['proposal'].update(outcome='needs_observation', changes=[], observation_requests=requests)
            return result
        return decide

    def test_supported_request_collects_fresh_data_before_retry_without_early_write(self):
        self.c.settings.duration = 23
        collector = self.observation_collector()
        self.c.telemetry = collector
        seen = []
        self.c.decision = self.requesting_decision(['routing_tables'], seen=seen)
        self.c.run()
        self.assertEqual(collector.calls, [['routing_tables']])
        self.assertEqual(self.outcomes(), ['success'])
        self.assertFalse(self.events('proposal_rejected'))
        self.assertIn('requested_observations', seen[1]['observation']['measurements'])
        self.assertEqual(seen[1]['feedback'][-1]['measurements']['observation_request']['status'], 'completed')
        complete = self.events('observation_request_completed')[0]['at']
        deployed = next(e['at'] for e in self.events('transition') if e['phase'] == 'deployment')
        self.assertLess(complete, deployed)
        from advnet.visualizer_data import continuous_experiment
        replay = continuous_experiment('requested-observations', self.events(), replay=True)
        self.assertTrue(any(e['stage'] == 'observation_request_queued' for e in replay['events']))
        self.assertEqual(replay['attempts'][0]['decision']['proposal']['outcome'], 'needs_observation')

    def test_observation_round_limit_prevents_request_loop(self):
        self.c.settings.duration = 30
        collector = self.observation_collector()
        self.c.telemetry = collector
        self.c.decision = self.requesting_decision(['routing_tables'], always=True)
        self.c.run()
        self.assertEqual(len(collector.calls), 1)
        self.assertTrue(any(e['reason'] == 'observation_round_limit' for e in self.events('observation_request_rejected')))
        self.assertFalse(self.net.writes)

    def test_untrusted_observation_text_never_reaches_collector(self):
        self.c.settings.duration = 23
        collector = self.observation_collector()
        self.c.telemetry = collector
        self.c.decision = self.requesting_decision(['show secret-provider-payload; reboot'])
        self.c.run()
        self.assertEqual(collector.calls, [])
        self.assertTrue(self.events('observation_request_rejected'))
        self.assertNotIn('secret-provider-payload', (self.directory / 'events.jsonl').read_text())
        self.assertNotIn('secret-provider-payload', next(self.directory.glob('*/decision.json')).read_text())

    def test_recovery_during_requested_collection_discards_pending_inference(self):
        self.c.settings.duration = 23
        self.c.telemetry = self.observation_collector(recover=True)
        self.c.decision = self.requesting_decision(['refresh_telemetry'])
        self.c.run()
        self.assertEqual(len(self.events('decision_result')), 1)
        self.assertFalse(self.events('observation_request_completed')[0]['inference_allowed'])
        self.assertFalse(self.net.writes)

    def test_observation_timeout_is_bounded_and_does_not_deploy(self):
        self.c.settings.duration = 12
        self.c.telemetry = self.observation_collector(slow=True)
        self.c.decision = self.requesting_decision(['routing_tables'])
        self.c.run()
        self.assertEqual(self.events('observation_request_failed')[0]['error_type'], 'TimeoutError')
        self.assertFalse(self.net.writes)

    def test_interrupted_collection_is_not_repeated_or_reported_as_completed_on_resume(self):
        from advnet.controller_execution import EvidenceFailure
        self.c.settings.duration = 23
        collector = self.observation_collector()
        collector.observe_requests = lambda *args: (_ for _ in ()).throw(EvidenceFailure('synthetic interruption'))
        self.c.telemetry = collector
        self.c.decision = self.requesting_decision(['routing_tables'])
        with self.assertRaises(EvidenceFailure):
            self.c.run()
        saved = self.c.journal.load()['episode']
        self.assertEqual(saved['feedback'][-1]['measurements']['observation_request']['status'], 'started')
        self.c.telemetry = self.observation_collector()
        self.c.settings.duration = 1
        self.c.run()
        saved = self.c.journal.load()['episode']
        self.assertEqual(saved['feedback'][-1]['measurements']['observation_request']['status'], 'interrupted')
        self.assertFalse(self.c.telemetry.calls)
        self.assertFalse(self.net.writes)

    def test_llm_only_never_calls_jev(self):
        self.c.settings.mode = 'llm_only'
        self.c.run()
        usage = self.events('decision_result')[0]['metrics']
        self.assertEqual((usage['jev_calls'], usage['llm_calls']), (0, 1))

    def test_jev_confidence_no_change_and_failure_fallback(self):
        frame = self.net.sample(None, self.limit())
        fixture = DemoDecision()('llm_only', frame.model_input, [], 1)['proposal']
        for screener, expected in [(MockJev('no_change', 0.99), 0), (MockJev('no_change', 0.6), 1),
                                   (ExplodingJev(), 1)]:
            with self.subTest(screener=screener):
                r = EngineProvider(MockLLM(fixture), screener)('jev_llm', frame.model_input, [], 1)
                self.assertEqual(sum(c['stage'] == 'llm' for c in r['calls']), expected)
                self.assertEqual(sum(c['stage'] == 'jev' for c in r['calls']), 1)

    def limit(self):
        self.c.end = self.c.clock.monotonic() + 50
        return self.c.budget()

    def test_batfish_rejection_does_not_deploy_or_end_monitoring(self):
        self.net.behavior = lambda t: 'reject'
        self.c.run()
        self.assertEqual(self.outcomes(), ['rejected', 'rejected'])
        self.assertFalse(self.net.writes)
        self.assertEqual(len(self.events('observation')), 12)
        self.assertFalse(self.c.policy.state.changes)

    def test_unexpected_checkpoint_change_during_verification_preserved(self):
        verify = self.net.verify
        def drift(path, limit):
            if path.name == 'snapshot':
                self.net.config['r2'] += '! external change\n'
            return verify(path, limit)
        self.net.verify = drift
        self.c.run()
        self.assertEqual(self.outcomes(), ['rejected', 'rejected'])
        self.assertEqual(self.net.writes, [])
        self.assertIn('external change', self.net.config['r2'])

    def test_unexpected_checkpoint_change_during_inference(self):
        def sample(generation, deadline):
            if self.c.clock.seconds == 3:
                self.net.config['r1'] += '! external\n'
            return self.net.sample(generation, deadline)
        self.c.telemetry = sample
        self.c.settings.duration = 6
        self.c.run()
        self.assertFalse(self.net.writes)
        self.assertFalse(self.c.policy.state.interventions)
        self.assertEqual(len(self.events('proposal_rejected')), 1)

    def test_fresh_reservation_persisted_before_verification(self):
        verify = self.net.verify
        def checked(path, limit):
            saved = self.c.journal.load()
            self.assertEqual(saved['policy']['state']['active']['intervention_id'], path.parent.name)
            self.assertTrue((path.parent/'checkpoint.json').is_file())
            return verify(path, limit)
        self.net.verify = checked
        self.c.run()
        self.assertEqual(self.outcomes(), ['success'])

    def test_demand_epoch_change_causes_rollback(self):
        original = self.net.window
        def window(phase, kind, limit):
            w = original(phase, kind, limit)
            return w.model_copy(update={'demand_epoch': 'changed'}) if phase == 'after' else w
        self.c.executor.windows = window
        self.c.run()
        self.assertEqual(self.outcomes(), ['rolled_back'])
        self.assertEqual(len(self.c.policy.state.changes), 2)
        self.assertEqual(self.net.config, self.net.initial)

    def test_runtime_measurement_failure_keeps_reason_and_evidence_before_rollback(self):
        for stage in ('before', 'after'):
            with self.subTest(stage=stage), TemporaryDirectory() as directory:
                self.c, self.net = demo_controller(Path(directory))
                self.directory = Path(directory)
                self.c.settings.duration = 9
                self.net.load = lambda t: .95
                self.net.behavior = lambda t: 'success'
                original = self.net.window
                evidence = {'error': 'Health checks were outside the receiver window',
                            'health_window': {'duration_seconds': 6.5}, 'flows': []}

                def windows(phase, kind, limit):
                    if phase == stage:
                        raise RuntimeMeasurementError(evidence['error'], evidence)
                    return original(phase, kind, limit)

                self.c.executor.windows = windows
                self.c.run()
                result = next(e for e in self.events('transition') if e['phase'] == 'runtime_result')
                self.assertFalse(result['accepted'])
                self.assertEqual(result['details'], [evidence['error']])
                self.assertEqual(result['measurement_error'], evidence)
                self.assertEqual(self.net.config, self.net.initial)
                if stage == 'after':
                    self.assertEqual(self.outcomes(), ['rolled_back'])
                    rollback = next(e for e in self.events('transition') if e['phase'] == 'rollback')
                    self.assertEqual(rollback['reason'], evidence['error'])
                    self.assertEqual(self.net.writes, ['deploy', 'rollback'])
                else:
                    self.assertEqual(self.outcomes(), ['rejected'])
                    self.assertEqual(self.net.writes, [])

    def test_absent_baseline_or_missing_optimization_evidence_rejects(self):
        self.c.executor.windows = lambda *args: None
        self.c.run()
        self.assertEqual(self.outcomes(), ['rejected', 'rejected'])
        self.assertFalse(self.net.writes)

    def test_cancellation_after_deploy_uses_independent_recovery_budget(self):
        replace = self.net.replace
        def cancel(configs, directory, deadline):
            replace(configs, directory, deadline)
            if directory.name == 'deploy':
                self.c.stop.set()
        self.net.replace = cancel
        self.c.run()
        self.assertEqual(self.net.writes, ['deploy', 'rollback'])
        self.assertEqual(self.outcomes(), ['rolled_back'])
        self.assertTrue(self.events('transition')[-1]['cancelled'])
        self.assertEqual(self.net.config, self.net.initial)

    def test_shutdown_discards_pending_worker(self):
        self.c.settings.duration = 4
        self.c.run()
        self.assertEqual(self.events('inference_discarded')[-1]['reason'], 'shutdown')
        self.assertFalse(self.c.policy.state.opportunity)
        self.assertIsNone(self.c.pending)
        self.assertFalse(self.net.writes)

    def test_interrupted_after_reservation_recovery_requires_matching_readback(self):
        with patch.object(self.c.executor, 'execute', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.c.run()
        active = self.c.policy.state.active
        self.assertIsNotNone(active)
        self.net.config['r2'] += '! outside change\n'
        self.c.settings.duration = 2
        self.c.run()
        self.assertEqual(self.c.policy.state.active, active)
        self.net.config = self.net.initial.copy()
        self.c.run()
        self.assertIsNone(self.c.policy.state.active)
        self.assertEqual(self.outcomes(), ['failed'])

    def test_reversal_suppression_uses_actual_controller_readback(self):
        self.net.load = lambda t: .95 if t < 6 or t >= 15 else .4
        self.c.settings.duration = 21
        original = self.c.decision
        def reverting(mode, obs, feedback, attempt):
            r = original(mode, obs, feedback, attempt)
            if self.c.clock.seconds > 10:
                r['proposal']['changes'][0]['value'] = 10
            return r
        self.c.decision = reverting
        self.c.run()
        self.assertEqual(self.outcomes(), ['success'])
        self.assertTrue(any('recent_reversal' in e['decision']['reasons'] for e in self.events('policy')))

    def test_repair_can_restore_state_after_external_fault_following_optimization_rollback(self):
        self.net.load = lambda t: .95 if t < 6 else .4
        self.net.behavior = lambda t: 'rollback' if t < 10 else 'success'
        self.c.settings.duration = 28
        injected = False

        def sample(generation, limit):
            nonlocal injected
            if self.c.clock.seconds >= 13 and not injected:
                self.net.config['r1'] += '   shutdown\n'
                injected = True
            frame = self.net.sample(generation, limit)
            if '   shutdown\n' in self.net.config['r1']:
                reach = frame.signal.reachability.model_copy(update={'healthy': False})
                frame.signal = frame.signal.model_copy(update={'reachability': reach})
            return frame

        def decide(mode, observation, feedback, attempt):
            result = DemoDecision()(mode, observation, feedback, attempt)
            if observation.measurements['stability_policy']['kind'] == 'repair':
                result['proposal']['changes'] = [{'device': 'r1', 'interface': 'Ethernet1',
                                                  'action': 'set_interface_enabled', 'value': True}]
            return result

        def windows(phase, kind, limit):
            value = self.net.window(phase, kind, limit)
            if kind == 'repair':
                return value.model_copy(update={'all_reachable': phase != 'before', 'traffic': None,
                                                'traffic_scope': 'confirmed_idle',
                                                'config_sha256': self.c.executor.config_id(
                                                    self.net.initial if phase == 'healthy' else self.net.config)})
            return value

        self.c.executor.normalize = normalize_readback
        self.c.telemetry, self.c.decision, self.c.executor.windows = sample, decide, windows
        self.c.run()
        self.assertEqual(self.outcomes(), ['rolled_back', 'success'])
        self.assertEqual(self.c.policy.state.interventions[-1].kind, 'repair')
        self.assertEqual(normalize_readback(self.net.config), normalize_readback(self.net.initial))
        self.assertEqual([entry.change.origin for entry in self.c.policy.state.changes],
                         ['controller', 'controller', 'external', 'controller'])

    def test_telemetry_exception_during_inference_retires_token(self):
        def sample(generation, deadline):
            if self.c.clock.seconds == 3:
                raise TimeoutError
            return self.net.sample(generation, deadline)
        self.c.telemetry = sample
        self.c.settings.duration = 6
        self.c.run()
        self.assertFalse(self.net.writes)
        self.assertTrue(self.events('inference_discarded'))

    def test_telemetry_absent_capacity_or_health_and_wrong_generation(self):
        at = self.c.clock.now()
        raw = dict(sampled_at=at.isoformat(), source='synthetic', links=[
            dict(a='r1', b='r2', a_interface='Ethernet1', b_interface='Ethernet2', a_mbps=75, b_mbps=None,
                 utilization=.99, capacity=100)], nodes={'r1': {'status': 'running'}})
        cap = CapacityEvidence(mbps=100, evidence='test', valid_from=at, valid_until=at+timedelta(seconds=20))
        frame = monitor_frame(raw, observation_id='test', generation='new', capacities={'r1:Ethernet1': cap},
                              rate_window_start=at-timedelta(seconds=1))
        self.assertEqual(frame.signal.links[0].throughput_mbps, 75)
        self.assertIsNone(frame.signal.links[0].reliable_utilization)
        self.assertIsNone(frame.signal.ospf)
        self.assertIsNone(frame.signal.reachability)
        self.assertEqual(frame.signal.links[1].link_id, 'r2:Ethernet2')
        self.assertEqual(self.c.policy.evaluate(frame.signal).recommendation, 'insufficient_telemetry')

    def test_stale_scoped_health_prevents_inference(self):
        def sample(generation, deadline):
            frame = self.net.sample(generation, deadline)
            old = HealthMeasurement(scope_fingerprint='hosts', healthy=True, evidence='cached',
                                    observed_at=self.c.clock.now()-timedelta(seconds=20))
            frame.signal = frame.signal.model_copy(update={'reachability': old})
            return frame
        self.c.telemetry = sample
        self.c.run()
        self.assertFalse(self.events('decision_result'))
        self.assertIn('stale_telemetry', self.events('observation')[-1]['decision']['reasons'])

    def test_journal_exclusive_owner_and_corrupt_state_fail_closed(self):
        with Journal(self.directory).owner(), self.assertRaises(BlockingIOError):
            self.c.run()
        self.c.journal.path.write_text('{}')
        with self.assertRaises((KeyError, ValueError)):
            self.c.run()
        self.assertFalse(self.net.writes)

    def test_monitor_ticks_while_real_worker_waits_for_later_sample(self):
        release = self.directory/'release'
        self.c.worker = InferenceJob
        self.c.decision = WaitingProvider(release)
        old_wait = self.c.clock.wait
        def wait(stop, seconds):
            if self.c.clock.seconds == 6:
                release.touch()
                self.c.pending.job.process.join(5)
            old_wait(stop, seconds)
        self.c.clock.wait = wait
        self.c.run()
        pending = [e for e in self.events('observation') if 'opportunity_pending' in e['decision']['reasons']]
        self.assertGreaterEqual(len(pending), 4)
        self.assertEqual(len(self.events('decision_result')), 1)
        self.assertEqual(self.outcomes(), ['success'])

    def test_journal_failure_during_deployment_phase_holds_reservation(self):
        from advnet.controller_execution import EvidenceFailure
        save = self.c.journal.save
        def fail(data):
            if data['phase'] == 'deployment':
                raise OSError('synthetic storage failure')
            save(data)
        self.c.journal.save = fail
        with self.assertRaises(EvidenceFailure):
            self.c.run()
        self.assertIsNotNone(self.c.policy.state.active)
        self.assertFalse(self.net.writes)
        self.assertIsNotNone(self.c.journal.load()['policy']['state']['active'])

    def test_new_health_repair_with_no_traffic_uses_repair_gate(self):
        def sample(generation, limit):
            f = self.net.sample(generation, limit)
            f.signal = f.signal.model_copy(update={'reachability': f.signal.reachability.model_copy(
                update={'healthy': False})})
            return f
        def windows(phase, kind, limit):
            self.assertEqual(kind, 'repair')
            return self.net.window(phase, kind, limit).model_copy(
                update={'all_reachable': phase != 'before', 'traffic': None, 'traffic_scope': 'confirmed_idle'})
        self.c.telemetry = sample
        self.c.executor.windows = windows
        self.c.settings.duration = 9
        self.c.run()
        self.assertEqual(self.outcomes(), ['success'])
        self.assertEqual(self.c.policy.state.interventions[0].kind, 'repair')

    def test_accepted_optimization_followed_by_latency_regression_requests_verified_repair(self):
        self.c.settings.duration = 42
        self.net.load = lambda t: .95 if t < 6 else .52
        original = self.net.window

        def windows(phase, kind, limit):
            value = original(phase, kind, limit)
            # Optimization initially passes. Subsequent monitoring detects RTT
            # regression; a new candidate must actually restore that tolerance.
            if phase == 'before' and kind == 'repair':
                value = value.model_copy(update={'mean_rtt_ms': 30})
            return value

        def sample(generation, limit):
            frame = self.net.sample(generation, limit)
            degraded = len(self.net.writes) == 1 and self.c.clock.seconds >= 10
            frame.signal = frame.signal.model_copy(update={'performance': HealthMeasurement(
                observed_at=frame.signal.observed_at, measurement_generation=generation,
                healthy=not degraded, scope_fingerprint='same-demand', evidence='SYNTHETIC RTT tolerance')})
            return frame

        self.c.telemetry, self.c.executor.windows = sample, windows
        self.c.run()
        self.assertEqual(self.outcomes(), ['success', 'success'])
        self.assertEqual([r.kind for r in self.c.policy.state.interventions], ['optimization', 'repair'])
        self.assertEqual(self.net.writes, ['deploy', 'deploy'])
        self.assertTrue(any('performance_failure' in e['decision']['reasons'] for e in self.events('observation')))
        self.assertEqual(len([e for e in self.events('transition') if e['phase'] == 'verified']), 2)

    def test_latency_repair_that_does_not_restore_tolerance_rolls_back(self):
        self.c.settings.duration = 27
        self.net.load = lambda t: .52
        original = self.net.window

        def windows(phase, kind, limit):
            value = original(phase, kind, limit)
            return value.model_copy(update={'mean_rtt_ms': 30 if phase != 'healthy' else 1})

        def sample(generation, limit):
            frame = self.net.sample(generation, limit)
            frame.signal = frame.signal.model_copy(update={'performance': HealthMeasurement(
                observed_at=frame.signal.observed_at, measurement_generation=generation,
                healthy=False, scope_fingerprint='same-demand', evidence='SYNTHETIC RTT tolerance')})
            return frame

        self.c.telemetry, self.c.executor.windows = sample, windows
        self.c.run()
        self.assertEqual(self.outcomes(), ['rolled_back'])
        self.assertEqual(self.net.writes, ['deploy', 'rollback'])
        self.assertEqual(self.net.config, self.net.initial)

    def test_performance_recovers_during_inference_before_any_write(self):
        self.c.settings.duration = 27
        self.net.load = lambda t: .52

        def sample(generation, limit):
            frame = self.net.sample(generation, limit)
            frame.signal = frame.signal.model_copy(update={'performance': HealthMeasurement(
                observed_at=frame.signal.observed_at, measurement_generation=generation,
                healthy=False, scope_fingerprint='same-demand', evidence='SYNTHETIC earlier slow window')})
            return frame

        # Fresh executor windows are healthy even though earlier periodic
        # telemetry triggered evaluation. A model proposal must not cause a write.
        self.c.telemetry = sample
        self.c.run()
        self.assertEqual(self.outcomes(), ['rejected'])
        self.assertEqual(self.c.policy.state.interventions[-1].outcomes[-1].outcome.evidence,
                         'repair_already_healthy')
        self.assertEqual(self.net.writes, [])

    def test_stale_after_window_rolls_back(self):
        original = self.net.window
        def window(phase, kind, limit):
            w = original(phase, kind, limit)
            if phase == 'after':
                return w.model_copy(update={'start': w.start-timedelta(seconds=10), 'end': w.end-timedelta(seconds=10)})
            return w
        self.c.executor.windows = window
        self.c.run()
        self.assertEqual(self.outcomes(), ['rolled_back'])
        self.assertEqual(self.net.config, self.net.initial)

    def test_convergence_failure_never_accepts(self):
        self.net.wait_convergence = lambda *args, **kwargs: False
        self.c.run()
        self.assertEqual(self.outcomes(), ['rolled_back'])
        self.assertEqual(self.net.config, self.net.initial)

    def test_incomplete_rollback_and_third_party_drift_do_not_overwrite_unknown_state(self):
        replace = self.net.replace
        def drift(configs, path, limit):
            replace(configs, path, limit)
            self.net.config['r2'] += '! external change'
        self.net.replace = drift
        self.c.run()
        self.assertEqual(self.outcomes(), ['rollback_unconfirmed'])
        self.assertEqual(self.net.writes, ['deploy'])
        self.assertFalse(self.c.policy.state.changes)

    def test_missing_monitor_link_and_stale_configs_do_not_create_evidence(self):
        at = self.c.clock.now()
        manifest = dict(routers={n: dict(interfaces=['Ethernet1'], host_interface='Ethernet2', host='h-'+n)
                                 for n in ('r1', 'r2')}, requirements=['Keep hosts reachable'],
                        links=[dict(a='r1', a_interface='Ethernet1', b='r2', b_interface='Ethernet1')])
        raw = dict(source='synthetic', sampled_at=at.isoformat(), links=[],
                   nodes={n: dict(config=text, config_at=at.isoformat()) for n, text in self.net.config.items()})
        def adapt():
            return monitor_frame(raw, manifest=manifest, generation=None, observation_id='aligned')
        fresh = adapt()
        self.assertEqual(fresh.model_input.observed_at, fresh.signal.observed_at)
        self.assertEqual(set(fresh.model_input.devices), set(self.net.config))
        self.assertEqual(len(fresh.signal.links), 2)
        self.assertTrue(all(link.reliable_utilization is None for link in fresh.signal.links))
        raw['nodes']['r1']['config_at'] = (at-timedelta(minutes=2)).isoformat()
        self.assertIsNone(adapt().model_input)

    def test_repeated_rejected_patch_cannot_reenter_verification_in_same_episode(self):
        self.net.behavior = lambda t: 'reject'
        provider = self.c.decision
        def fixed(mode, obs, feedback, attempt):
            r = provider(mode, obs, feedback, attempt)
            r['proposal']['changes'][0]['value'] = 44
            return r
        self.c.decision = fixed
        self.c.run()
        self.assertEqual(self.outcomes(), ['rejected'])
        self.assertEqual(self.events('proposal_rejected')[-1]['reason'], 'repeated_episode_patch')

    def test_failed_baseline_gate_prevents_candidate_verification_and_writes(self):
        paths = []
        def verify(path, limit):
            paths.append(path.name)
            return {'accepted': 'true', 'details': ['Invalid verifier type']}
        self.net.verify = verify
        self.c.settings.duration = 6
        self.c.run()
        self.assertEqual(paths, ['healthy-snapshot'])
        self.assertEqual(self.outcomes(), ['rejected'])
        self.assertEqual(self.net.writes, [])

    def test_zero_delivery_baseline_cannot_certify_optimization(self):
        original = self.net.window
        def no_delivery(phase, kind, limit):
            window = original(phase, kind, limit)
            if phase == 'healthy':
                return window.model_copy(update={'traffic': {**window.traffic, 'received_mbps': 0}})
            return window
        self.c.executor.windows = no_delivery
        self.c.settings.duration = 6
        self.c.run()
        self.assertEqual(self.outcomes(), ['rejected'])
        self.assertEqual(self.net.writes, [])

    def test_invalid_runtime_numbers_never_count_as_acceptance(self):
        original = self.net.window
        def corrupt(phase, kind, limit):
            w = original(phase, kind, limit)
            if phase == 'after':
                return w.model_copy(update={'traffic': {**w.traffic, 'loss_fraction': float('nan')}})
            return w
        self.c.executor.windows = corrupt
        self.c.run()
        self.assertEqual(self.outcomes(), ['rolled_back'])

    def test_no_competing_traffic_or_bounded_trial_entry_points(self):
        from advnet.lab import Lab
        from advnet.trial import Trial
        with patch.object(Lab, 'traffic', side_effect=AssertionError) as traffic, \
             patch.object(Lab, 'concurrent_traffic', side_effect=AssertionError) as concurrent, \
             patch.object(Trial, 'run', side_effect=AssertionError) as trial:
            self.c.run()
        for entry in (traffic, concurrent, trial):
            entry.assert_not_called()
        self.assertEqual(self.outcomes(), ['success'])

    def test_two_independent_successes_use_fresh_episodes(self):
        self.c.settings.duration = 25
        self.net.load = lambda t: .95 if t < 6 or 15 <= t <= 21 else .4
        self.c.run()
        self.assertEqual(self.outcomes(), ['success', 'success'])
        starts = [r for r in self.events('transition') if r['phase'] == 'decision']
        self.assertEqual([r['attempt'] for r in starts], [1, 1])
        self.assertNotEqual(starts[0]['intervention'], starts[1]['intervention'])

    def test_partial_write_fingerprints_only_observed_state(self):
        provider = self.c.decision
        def two_devices(mode, obs, feedback, attempt):
            r = provider(mode, obs, feedback, attempt)
            r['proposal']['changes'].append({**r['proposal']['changes'][0], 'device': 'r2'})
            return r
        replace = self.net.replace
        def partial(configs, path, limit):
            if path.name == 'deploy':
                replace({'r1': configs['r1']}, path, limit)
                raise TimeoutError
            replace(configs, path, limit)
        self.c.decision, self.net.replace = two_devices, partial
        self.c.run()
        changes = self.c.policy.state.changes
        self.assertEqual(self.outcomes(), ['rolled_back'])
        self.assertEqual(len(changes), 2)
        self.assertNotEqual(changes[0].change.after_state, self.c.policy.state.interventions[0].candidate.after_state)
        self.assertEqual(changes[0].change.after_state, changes[1].change.before_state)
        self.assertEqual(self.net.config, self.net.initial)

    def test_synthetic_demo_reproduces_identical_evidence(self):
        a, _ = demo_controller(self.directory/'a')
        b, _ = demo_controller(self.directory/'b')
        a.run()
        b.run()
        for name in ('events.jsonl', 'controller.json'):
            self.assertEqual((a.journal.root/name).read_bytes(), (b.journal.root/name).read_bytes())

    def test_real_spawn_worker_returns_sanitized_pipeline_result(self):
        frame = self.net.sample(None, self.limit())
        job = InferenceJob(DemoDecision(), ('jev_llm', frame.model_input, [], 1), self.directory, 10)
        try:
            stop = time.monotonic()+10
            while (result := job.poll()) is None and time.monotonic() < stop:
                time.sleep(.02)
            self.assertEqual(result['metrics']['jev_calls'], 1)
            self.assertEqual(result['proposal']['changes'][0]['action'], 'set_ospf_cost')
            self.assertNotIn('"raw":', json.dumps(result))
        finally:
            job.close()

    def test_real_worker_hard_timeout_and_cleanup(self):
        frame = self.net.sample(None, self.limit())
        job = InferenceJob(SleepingProvider(), ('llm_only', frame.model_input, [], 1), self.directory, .4)
        try:
            job.process.join(5)
            self.assertFalse(job.process.is_alive())
            with self.assertRaises((RuntimeError, FileNotFoundError)):
                job.poll()
        finally:
            job.close()
