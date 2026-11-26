"""Offline tests of the real policy with synthetic measurements and fake UTC."""
from datetime import datetime, timedelta, timezone
import json
import unittest

from pydantic import ValidationError

from advnet.stability_policy import (
    ActiveIntervention, CandidateAction, ConfigurationChange, HealthMeasurement,
    InterventionOutcome, LinkMeasurement, NetworkObservation, PolicyConfig,
    PolicyDecision, PolicySnapshot, PolicyState, SignalState, StabilityPolicy,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self):
        self.seconds = 0

    def __call__(self):
        return BASE + timedelta(seconds=self.seconds)


def action(before='A', after='B', *, key=None, scope='network-config'):
    return CandidateAction(action_key=key or before + '->' + after, scope=scope,
                           before_state=before, after_state=after)


def outcome(status):
    return InterventionOutcome(status=status, network_state_confirmed=status != 'rollback_unconfirmed',
                               evidence='SYNTHETIC caller-confirmed test result')


class StabilityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.policy = StabilityPolicy(clock=self.clock)
        self.request_id = 'no-request'

    def observation(self, at, utilization=0.4, *, reachable=True, ospf=True, links=None):
        self.clock.seconds = at
        now = self.clock()
        return NetworkObservation(
            observation_id=f'synthetic-{at}', observed_at=now,
            reachability=HealthMeasurement(observed_at=now, healthy=reachable, evidence='SYNTHETIC host matrix'),
            ospf=HealthMeasurement(observed_at=now, healthy=ospf, evidence='SYNTHETIC required FULL peers'),
            links=links if links is not None else [LinkMeasurement(
                link_id='r1:Ethernet1', observed_at=now, utilization=utilization,
                utilization_evidence='SYNTHETIC measured utilization')],
        )

    def evaluate(self, at, utilization=0.4, **kwargs):
        decision = self.policy.evaluate(self.observation(at, utilization, **kwargs))
        if decision.recommendation.startswith('permit_'):
            self.request_id = decision.opportunity_id
        return decision

    def ready(self, start=0):
        self.evaluate(start, 0.95)
        self.evaluate(start + 10, 0.95)
        return self.evaluate(start + 20, 0.95)

    def begin(self, identifier='i1', candidate=None):
        decision = self.policy.start_intervention(identifier, candidate or action(), 'optimization', opportunity_id=self.request_id)
        self.assertEqual(decision.recommendation, 'permit_optimization_evaluation')
        self.assertFalse(decision.deployment_authorized)
        return decision

    def change(self, at, before, after, scope='network-config'):
        self.clock.seconds = at
        self.policy.record_configuration_change(ConfigurationChange(
            occurred_at=self.clock(), before_state=before, after_state=after,
            scope=scope, evidence='SYNTHETIC configuration readback'))

    def test_healthy(self):
        for at in (0, 10, 20, 100):
            decision = self.evaluate(at)
            self.assertEqual(decision.recommendation, 'no_intervention')
            self.assertIn('healthy', decision.reasons)
            self.assertFalse(decision.deployment_authorized)

    def test_temporary_congestion_clears_naturally(self):
        self.assertEqual(self.evaluate(0, 0.95).recommendation, 'collect_observations')
        self.assertEqual(self.evaluate(10, 0.4).recommendation, 'no_intervention')
        self.assertEqual(self.evaluate(20, 0.4).recommendation, 'no_intervention')
        self.assertFalse(self.policy.state.signals['link:r1:Ethernet1'].latched)

    def test_sustained_congestion(self):
        decision = self.ready()
        self.assertEqual(decision.recommendation, 'permit_optimization_evaluation')
        self.assertEqual(decision.links, ['r1:Ethernet1'])
        self.assertIn('congestion_sustained', decision.reasons)
        self.assertIn('trigger_latched', self.evaluate(30, 0.95).reasons)

    def test_duration_and_count_both_required(self):
        for at in (0, 1, 2, 10):
            self.assertEqual(self.evaluate(at, 0.95).recommendation, 'collect_observations')
        self.assertEqual(self.evaluate(20, 0.95).recommendation, 'permit_optimization_evaluation')

    def test_configurable_count_and_duration(self):
        self.policy = StabilityPolicy(PolicyConfig(congestion_samples=4, congestion_seconds=6.0), clock=self.clock)
        for at in (0, 2, 4):
            self.assertEqual(self.evaluate(at, 0.9).recommendation, 'collect_observations')
        self.assertEqual(self.evaluate(6, 0.9).recommendation, 'permit_optimization_evaluation')

    def test_multiple_congested_links_and_independent_streaks(self):
        for at in (0, 10, 20):
            sample = self.observation(at)
            links = [LinkMeasurement(link_id=name, observed_at=self.clock(), utilization=util,
                                     utilization_evidence='SYNTHETIC')
                     for name, util in [('a', 0.95), ('b', 0.9), ('c', 0.4)]]
            decision = self.policy.evaluate({**sample.model_dump(), 'links': links})
        self.assertEqual(decision.links, ['a', 'b'])
        self.assertEqual(decision.recommendation, 'permit_optimization_evaluation')

    def test_alternating_congested_links_do_not_combine(self):
        for at in (0, 10, 20, 30):
            sample = self.observation(at)
            links = [LinkMeasurement(link_id=name, observed_at=self.clock(), utilization=util,
                                     utilization_evidence='SYNTHETIC')
                     for name, util in [('a', 0.95 if at % 20 == 0 else 0.4),
                                       ('b', 0.4 if at % 20 == 0 else 0.95)]]
            decision = self.policy.evaluate({**sample.model_dump(), 'links': links})
            self.assertNotEqual(decision.recommendation, 'permit_optimization_evaluation')

    def test_fluctuating_threshold_never_accumulates_consecutive_highs(self):
        for index, value in enumerate([0.86, 0.84] * 8):
            decision = self.evaluate(index * 5, value)
            self.assertNotEqual(decision.recommendation, 'permit_optimization_evaluation')
            self.assertNotIn('state_switching', decision.reasons)

    def test_hysteresis_prevents_rearming_in_deadband(self):
        self.ready()
        for at, value in [(30, 0.7), (40, 0.9), (50, 0.65), (60, 0.7), (70, 0.9)]:
            self.assertEqual(self.evaluate(at, value).recommendation, 'collect_observations')
        self.assertTrue(self.policy.state.signals['link:r1:Ethernet1'].latched)

    def test_recovery_and_rearming(self):
        self.ready()
        self.evaluate(30, 0.65)
        decision = self.evaluate(40, 0.65)
        self.assertIn('recovery_confirmed', decision.reasons)
        self.assertEqual(decision.recommendation, 'no_intervention')
        self.assertEqual(self.ready(50).recommendation, 'permit_optimization_evaluation')

    def test_many_independent_events_in_one_experiment(self):
        for start in (0, 50, 100, 150):
            self.assertEqual(self.ready(start).recommendation, 'permit_optimization_evaluation')
            self.evaluate(start + 30, 0.4)
            self.assertIn('recovery_confirmed', self.evaluate(start + 40, 0.4).reasons)

    def test_reachability_failures(self):
        self.assertIn('repair_pending', self.evaluate(0, reachable=False).reasons)
        decision = self.evaluate(5, reachable=False)
        self.assertEqual(decision.recommendation, 'permit_repair_evaluation')
        self.assertIn('reachability_failure', decision.reasons)

    def test_ospf_adjacency_failures(self):
        self.evaluate(0, ospf=False)
        decision = self.evaluate(5, ospf=False)
        self.assertEqual(decision.recommendation, 'permit_repair_evaluation')
        self.assertIn('ospf_failure', decision.reasons)

    def test_unresolved_repair_retries_without_permanent_latch(self):
        self.evaluate(0, reachable=False)
        self.assertEqual(self.evaluate(5, reachable=False).recommendation, 'permit_repair_evaluation')
        for at in (10, 20, 30):
            self.assertEqual(self.evaluate(at, reachable=False).recommendation, 'collect_observations')
        self.assertEqual(self.evaluate(35, reachable=False).recommendation, 'collect_observations')
        for at in range(45, 96, 10):
            decision = self.evaluate(at, reachable=False)
        self.assertEqual(decision.recommendation, 'permit_repair_evaluation')

    def test_repair_recovery_rearms_before_retry_deadline(self):
        self.evaluate(0, reachable=False)
        self.evaluate(5, reachable=False)
        self.evaluate(10)
        self.assertIn('recovery_confirmed', self.evaluate(15).reasons)
        self.evaluate(20, reachable=False)
        self.assertEqual(self.evaluate(25, reachable=False).recommendation, 'permit_repair_evaluation')

    def test_different_health_failures_do_not_combine(self):
        for at, reachable, ospf in [(0, False, True), (5, True, False), (10, False, True)]:
            self.assertEqual(self.evaluate(at, reachable=reachable, ospf=ospf).recommendation, 'collect_observations')

    def test_repair_takes_priority_over_optimization(self):
        self.evaluate(0, 0.95, reachable=False)
        self.evaluate(10, 0.95, reachable=False)
        decision = self.evaluate(20, 0.95, reachable=False)
        self.assertNotEqual(decision.recommendation, 'permit_optimization_evaluation')

    def test_known_failure_can_request_repair_with_other_telemetry_missing(self):
        for at in (0, 5):
            sample = self.observation(at, reachable=False, links=[])
            decision = self.policy.evaluate({**sample.model_dump(), 'ospf': None})
            self.request_id = decision.opportunity_id
        self.assertEqual(decision.recommendation, 'permit_repair_evaluation')
        self.assertIn('proposal_eligible', self.policy.assess_proposal(action(), 'repair', opportunity_id=self.request_id).reasons)

    def test_success_cooldown_and_expiration(self):
        self.ready()
        self.begin()
        self.policy.complete_intervention('i1', outcome('success'))
        self.assertEqual(self.policy.state.cooldown_until, BASE + timedelta(seconds=80))
        for at in (30, 40, 50, 60, 70):
            self.assertEqual(self.evaluate(at, 0.95).recommendation, 'suppress_cooldown')
        self.assertEqual(self.evaluate(80, 0.95).recommendation, 'permit_optimization_evaluation')

    def test_all_outcome_cooldowns(self):
        for status, duration in [('rejected', 15), ('failed', 30), ('rolled_back', 45), ('rollback_unconfirmed', 120)]:
            with self.subTest(status=status):
                self.setUp()
                self.ready()
                self.begin()
                self.policy.complete_intervention('i1', outcome(status))
                self.assertEqual(self.policy.state.cooldown_until, BASE + timedelta(seconds=20 + duration))
                decision = self.evaluate(21, 0.95)
                expected = 'suppress_unsafe_state' if status == 'rollback_unconfirmed' else 'suppress_cooldown'
                self.assertEqual(decision.recommendation, expected)

    def test_urgent_repair_can_bypass_routine_cooldown(self):
        self.ready()
        self.begin()
        self.policy.complete_intervention('i1', outcome('success'))
        self.evaluate(30, reachable=False)
        decision = self.evaluate(35, reachable=False)
        self.assertIn('urgent_repair_cooldown_exception', decision.reasons)
        self.assertEqual(decision.recommendation, 'permit_repair_evaluation')
        self.assertEqual(self.policy.start_intervention('repair', action('A', 'C'), 'repair', opportunity_id=self.request_id).stage, 'reservation')

    def test_urgent_exception_can_be_disabled(self):
        self.policy = StabilityPolicy(PolicyConfig(urgent_repair_bypasses_cooldown=False), clock=self.clock)
        self.ready()
        self.begin()
        self.policy.complete_intervention('i1', outcome('success'))
        self.evaluate(30, reachable=False)
        self.assertEqual(self.evaluate(35, reachable=False).recommendation, 'suppress_cooldown')

    def test_ospf_repair_observes_cooldown(self):
        self.ready()
        self.begin()
        self.policy.complete_intervention('i1', outcome('success'))
        self.evaluate(30, ospf=False)
        self.assertEqual(self.evaluate(35, ospf=False).recommendation, 'suppress_cooldown')

    def test_unconfirmed_rollback_holds_even_after_deadline_and_for_urgent_repairs(self):
        self.ready()
        self.begin()
        self.policy.complete_intervention('i1', outcome('rollback_unconfirmed'))
        self.evaluate(1000, reachable=False)
        decision = self.evaluate(1005, reachable=False)
        self.assertEqual(decision.recommendation, 'suppress_unsafe_state')
        self.assertIn('rollback_unconfirmed', decision.reasons)
        self.assertEqual(self.policy.start_intervention('i2', action(), 'repair', opportunity_id=self.request_id).recommendation, 'suppress_unsafe_state')
        with self.assertRaises(ValueError):
            self.policy.complete_intervention('i1', outcome('success'))
        self.policy.complete_intervention('i1', outcome('rolled_back'))
        self.assertIsNone(self.policy.state.active)
        self.assertEqual(len(self.policy.state.interventions[0].outcomes), 2)
        self.evaluate(1010, reachable=False)
        self.assertEqual(self.evaluate(1015, reachable=False).recommendation, 'permit_repair_evaluation')

    def test_overlapping_interventions(self):
        self.ready()
        self.begin()
        decision = self.policy.start_intervention('i2', action('A', 'C'), 'optimization', opportunity_id=self.request_id)
        self.assertEqual(decision.recommendation, 'suppress_active_intervention')
        self.assertEqual(len(self.policy.state.interventions), 1)
        self.assertEqual(self.evaluate(30, reachable=False).recommendation, 'suppress_active_intervention')

    def test_outcome_correlates_to_active_id(self):
        self.ready()
        self.begin()
        before = self.policy.state
        with self.assertRaises(ValueError):
            self.policy.complete_intervention('wrong', outcome('success'))
        self.assertEqual(self.policy.state, before)

    def test_duplicate_intervention_id_is_rejected(self):
        self.ready()
        self.begin()
        self.policy.complete_intervention('i1', outcome('rejected'))
        self.ready(40)
        with self.assertRaises(ValueError):
            self.policy.start_intervention('i1', action(), 'optimization', opportunity_id=self.request_id)
        self.assertIsNone(self.policy.state.active)

    def test_frequency_limit_expires_and_does_not_permanently_block_repairs(self):
        self.policy = StabilityPolicy(PolicyConfig(max_interventions=1, intervention_window=60.0), clock=self.clock)
        self.ready()
        self.begin()
        self.policy.complete_intervention('i1', outcome('rejected'))
        self.evaluate(40, reachable=False)
        self.assertIn('frequency_limit', self.evaluate(45, reachable=False).reasons)
        self.evaluate(70, reachable=False)
        self.assertEqual(self.evaluate(80, reachable=False).recommendation, 'permit_repair_evaluation')

    def test_repeated_unsuccessful_actions_and_expiration(self):
        for identifier, start, status in [('i1', 0, 'rejected'), ('i2', 40, 'rolled_back')]:
            self.ready(start)
            self.begin(identifier)
            self.policy.complete_intervention(identifier, outcome(status))
        self.ready(110)
        decision = self.policy.assess_proposal(action(key='different-prose-id'), 'optimization', opportunity_id=self.request_id)
        self.assertIn('repeated_unsuccessful_action', decision.reasons)
        self.assertIn('proposal_eligible', self.policy.assess_proposal(action('A', 'C'), 'optimization', opportunity_id=self.request_id).reasons)
        self.evaluate(140, 0.4)
        self.evaluate(150, 0.4)
        self.ready(370)
        self.assertIn('proposal_eligible', self.policy.assess_proposal(action(), 'optimization', opportunity_id=self.request_id).reasons)

    def test_reversal_uses_actual_configuration_history_and_expires(self):
        self.change(0, 'A', 'B')
        self.ready(10)
        decision = self.policy.assess_proposal(action('B', 'A'), 'optimization', opportunity_id=self.request_id)
        self.assertIn('recent_reversal', decision.reasons)
        self.evaluate(40, 0.4)
        self.evaluate(50, 0.4)
        self.ready(100)
        self.assertIn('proposal_eligible', self.policy.assess_proposal(action('B', 'A'), 'optimization', opportunity_id=self.request_id).reasons)

    def test_repeated_state_switching_uses_real_history(self):
        self.change(0, 'A', 'B')
        self.change(10, 'B', 'C')
        self.change(20, 'C', 'B')
        self.ready(150)
        self.assertIn('state_switching', self.policy.assess_proposal(action('B', 'A'), 'optimization', opportunity_id=self.request_id).reasons)
        self.assertIn('proposal_eligible', self.policy.assess_proposal(action('B', 'D'), 'optimization', opportunity_id=self.request_id).reasons)

    def test_unexecuted_proposals_do_not_create_configuration_history(self):
        self.ready()
        self.begin()
        self.policy.complete_intervention('i1', outcome('rejected'))
        self.assertEqual(self.policy.state.changes, [])

    def test_configuration_scope_and_current_state_are_checked(self):
        self.change(0, 'A', 'B')
        self.ready(10)
        self.assertIn('state_already_applied', self.policy.assess_proposal(action(), 'optimization', opportunity_id=self.request_id).reasons)
        self.assertIn('state_history_mismatch', self.policy.assess_proposal(action('X', 'Y'), 'optimization', opportunity_id=self.request_id).reasons)
        self.assertIn('proposal_eligible', self.policy.assess_proposal(action('X', 'Y', scope='other'), 'optimization', opportunity_id=self.request_id).reasons)

    def test_missing_capacity_does_not_infer_utilization(self):
        sample = self.observation(0, links=[LinkMeasurement(
            link_id='a', observed_at=BASE, throughput_mbps=99.0, throughput_evidence='SYNTHETIC counter delta')])
        decision = self.policy.evaluate(sample)
        self.assertIn('missing_utilization', decision.reasons)
        self.assertEqual(decision.recommendation, 'insufficient_telemetry')

    def test_verified_capacity_and_throughput_can_calculate_utilization(self):
        for at in (0, 10, 20):
            sample = self.observation(at, links=[LinkMeasurement(
                link_id='a', observed_at=BASE + timedelta(seconds=at), throughput_mbps=9.0,
                throughput_evidence='SYNTHETIC counter window', effective_capacity_mbps=10.0,
                capacity_evidence='SYNTHETIC same-window effective shaping readback')])
            decision = self.policy.evaluate(sample)
        self.assertEqual(decision.recommendation, 'permit_optimization_evaluation')

    def test_unattested_throughput_is_insufficient(self):
        sample = self.observation(0, links=[LinkMeasurement(
            link_id='a', observed_at=BASE, throughput_mbps=9.0, effective_capacity_mbps=10.0,
            capacity_evidence='SYNTHETIC effective capacity')])
        self.assertIn('missing_utilization', self.policy.evaluate(sample).reasons)

    def test_direct_reliable_utilization_needs_no_capacity(self):
        self.assertEqual(self.ready().recommendation, 'permit_optimization_evaluation')

    def test_missing_health_blocks_optimization(self):
        for at in (0, 10, 20):
            sample = self.observation(at, 0.95)
            decision = self.policy.evaluate({**sample.model_dump(), 'reachability': None})
        self.assertEqual(decision.recommendation, 'insufficient_telemetry')
        self.assertIn('missing_health', decision.reasons)

    def test_empty_links_are_explicitly_insufficient(self):
        self.assertIn('missing_utilization', self.evaluate(0, links=[]).reasons)

    def test_stale_outer_and_individual_telemetry(self):
        sample = self.observation(0, 0.95)
        self.clock.seconds = 16
        self.assertIn('stale_telemetry', self.policy.evaluate(sample).reasons)
        outer = self.observation(20, 0.95)
        self.assertIn('stale_telemetry', self.policy.evaluate({**outer.model_dump(), 'links': sample.links}).reasons)

    def test_future_telemetry_and_out_of_order_measurements(self):
        sample = self.observation(10)
        self.clock.seconds = 0
        self.assertIn('future_telemetry', self.policy.evaluate(sample).reasons)
        with self.assertRaises(ValidationError):
            NetworkObservation(observation_id='bad', observed_at=BASE, links=sample.links)

    def test_duplicate_outer_observations_do_not_add_samples(self):
        sample = self.observation(0, 0.95)
        self.policy.evaluate(sample)
        for _ in range(5):
            self.assertIn('non_increasing_observation', self.policy.evaluate(sample).reasons)
        self.assertEqual(self.policy.state.signals['link:r1:Ethernet1'].count, 1)

    def test_cached_inner_samples_do_not_accumulate(self):
        sample = self.observation(0, 0.95)
        self.policy.evaluate(sample)
        for at in (5, 10):
            outer = self.observation(at, 0.95)
            decision = self.policy.evaluate({**outer.model_dump(), 'links': sample.links})
            self.assertIn('non_increasing_observation', decision.reasons)
        self.assertEqual(self.policy.state.signals['link:r1:Ethernet1'].count, 1)

    def test_gap_breaks_sustained_condition(self):
        self.evaluate(0, 0.95)
        self.evaluate(10, 0.95)
        decision = self.evaluate(26, 0.95)
        self.assertIn('measurement_gap', decision.reasons)
        self.assertEqual(decision.recommendation, 'collect_observations')

    def test_missing_and_invalid_samples_break_pending_streak(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                self.setUp()
                self.evaluate(0, 0.95)
                self.evaluate(10, 0.95)
                if missing:
                    self.evaluate(15, links=[])
                else:
                    self.clock.seconds = 15
                    self.assertIn('invalid_telemetry', self.policy.evaluate({'unknown': True}).reasons)
                self.assertEqual(self.evaluate(20, 0.95).recommendation, 'collect_observations')
                self.evaluate(30, 0.95)
                self.assertEqual(self.evaluate(40, 0.95).recommendation, 'permit_optimization_evaluation')

    def test_missing_samples_cannot_clear_latched_trigger(self):
        self.ready()
        self.evaluate(30, links=[])
        self.assertIn('trigger_latched', self.evaluate(40, 0.95).reasons)

    def test_proposal_stage_requires_recent_opportunity_and_matching_kind(self):
        self.assertIn('no_evaluation_opportunity', self.policy.assess_proposal(action(), 'optimization', opportunity_id=self.request_id).reasons)
        self.ready()
        self.assertIn('no_evaluation_opportunity', self.policy.assess_proposal(action(), 'repair', opportunity_id=self.request_id).reasons)
        self.clock.seconds = 36
        self.assertIn('stale_telemetry', self.policy.assess_proposal(action(), 'optimization', opportunity_id=self.request_id).reasons)
        self.assertIsNone(self.policy.state.active)

    def test_proposal_freshness_uses_measurement_time(self):
        sample = self.observation(0, 0.95)
        self.policy.evaluate(sample)
        self.evaluate(5, 0.95)
        self.evaluate(10, 0.95)
        # Health samples at t=10 are still valid when wrapped in t=20.
        older = self.policy.state.recent_observations[-1]
        outer = self.observation(20, 0.95)
        decision = self.policy.evaluate({**outer.model_dump(), 'reachability': older.reachability, 'ospf': older.ospf})
        # Cached health is explicitly insufficient, even with a fresh wrapper.
        self.assertEqual(decision.recommendation, 'insufficient_telemetry')

    def test_proposal_expires_when_essential_inner_sample_expires(self):
        self.evaluate(0, 0.95)
        self.evaluate(10, 0.95)
        outer = self.observation(20, 0.95)
        health = HealthMeasurement(observed_at=BASE + timedelta(seconds=15), healthy=True, evidence='SYNTHETIC')
        decision = self.policy.evaluate({**outer.model_dump(), 'reachability': health, 'ospf': health})
        self.assertEqual(decision.recommendation, 'permit_optimization_evaluation')
        self.request_id = decision.opportunity_id
        self.clock.seconds = 31
        self.assertIn('stale_telemetry', self.policy.assess_proposal(action(), 'optimization', opportunity_id=self.request_id).reasons)

    def test_clock_regression_fails_safely(self):
        self.evaluate(10, 0.95)
        self.assertIn('clock_regression', self.evaluate(5, 0.95).reasons)
        with self.assertRaises(ValueError):
            self.policy.snapshot()
        self.assertEqual(self.evaluate(15, 0.95).recommendation, 'collect_observations')

    def test_snapshot_roundtrip_preserves_audit_and_cooldown(self):
        self.ready()
        self.begin()
        self.change(20, 'A', 'B')
        self.policy.complete_intervention('i1', outcome('success'))
        encoded = self.policy.snapshot().model_dump_json()
        restored = StabilityPolicy.restore(encoded, clock=self.clock)
        self.assertEqual(restored.state.interventions, self.policy.state.interventions)
        self.assertEqual(restored.state.changes, self.policy.state.changes)
        self.assertEqual(restored.state.recent_observations, self.policy.state.recent_observations)
        self.assertEqual(restored.state.cooldown_until, BASE + timedelta(seconds=80))
        self.assertIsNone(restored.state.opportunity)

    def test_restore_rebases_deadlines_for_forward_and_backward_clock_origins(self):
        self.ready()
        self.begin()
        self.policy.complete_intervention('i1', outcome('success'))
        snapshot = self.policy.snapshot()
        for origin in (-1000, 100000):
            with self.subTest(origin=origin):
                self.clock.seconds = origin
                restored = StabilityPolicy.restore(snapshot, clock=self.clock)
                self.assertEqual((restored.state.cooldown_until - self.clock()).total_seconds(), 60)
                self.assertEqual((restored.state.interventions[0].frequency_until - self.clock()).total_seconds(), 300)
                self.policy = restored
                self.assertEqual(self.evaluate(origin + 1).recommendation, 'suppress_cooldown')

    def test_restored_active_intervention_can_finish_after_clock_origin_changes(self):
        self.ready()
        self.begin()
        self.change(20, 'A', 'B')
        snapshot = self.policy.snapshot()
        self.clock.seconds = -1000
        restored = StabilityPolicy.restore(snapshot, clock=self.clock)
        restored.record_configuration_change(ConfigurationChange(
            occurred_at=self.clock(), scope='network-config', before_state='B', after_state='A', evidence='SYNTHETIC rollback'))
        restored.complete_intervention('i1', outcome('rolled_back'))
        self.assertIsNone(restored.state.active)
        self.assertEqual(restored.state.interventions[0].outcomes[-1].clock_generation, 1)

    def test_restored_unconfirmed_rollback_remains_held(self):
        self.ready()
        self.begin()
        self.policy.complete_intervention('i1', outcome('rollback_unconfirmed'))
        snapshot = self.policy.snapshot()
        self.clock.seconds = 10000
        self.policy = StabilityPolicy.restore(snapshot, clock=self.clock)
        self.assertIn('rollback_unconfirmed', self.evaluate(11000, reachable=False).reasons)

    def test_restore_resets_streaks_and_preserves_latches(self):
        self.evaluate(0, 0.95)
        self.evaluate(10, 0.95)
        self.policy = StabilityPolicy.restore(self.policy.snapshot(), clock=self.clock)
        self.assertEqual(self.evaluate(20, 0.95).recommendation, 'collect_observations')
        self.evaluate(30, 0.95)
        self.evaluate(40, 0.95)
        self.policy = StabilityPolicy.restore(self.policy.snapshot(), clock=self.clock)
        self.assertIn('trigger_latched', self.evaluate(50, 0.95).reasons)

    def test_state_access_and_snapshot_do_not_expose_mutable_internal_lists(self):
        self.evaluate(0)
        state = self.policy.state
        state.recent_observations.clear()
        self.assertEqual(len(self.policy.state.recent_observations), 1)
        saved = self.policy.snapshot()
        saved.state.signals.clear()
        self.assertTrue(self.policy.state.signals)

    def test_returned_decision_does_not_expose_internal_opportunity(self):
        decision = self.ready()
        decision.links.clear()
        decision.reasons.append('reachability_failure')
        self.assertEqual(self.policy.state.opportunity.links, ['r1:Ethernet1'])
        self.assertNotIn('reachability_failure', self.policy.state.opportunity.reasons)

    def test_recent_observations_are_bounded(self):
        self.policy = StabilityPolicy(PolicyConfig(observation_history_size=2), clock=self.clock)
        for at in (0, 10, 20):
            self.evaluate(at)
        self.assertEqual(len(self.policy.state.recent_observations), 2)

    def test_slow_inference_uses_latest_sample_and_one_dispatch(self):
        issued = self.ready()
        self.assertEqual(issued.recommendation, 'permit_optimization_evaluation')
        self.assertTrue(issued.opportunity_id)
        for at in (25, 35, 45, 55):
            pending = self.evaluate(at, 0.91)
            self.assertEqual(pending.recommendation, 'collect_observations')
            self.assertIn('opportunity_pending', pending.reasons)
            self.assertEqual((pending.opportunity_id, pending.expires_at),
                             (issued.opportunity_id, issued.expires_at))
        checked = self.policy.assess_proposal(action(), 'optimization', opportunity_id=issued.opportunity_id)
        self.assertEqual(checked.observation_id, 'synthetic-55')
        self.assertEqual(checked.reasons, ['proposal_eligible'])
        self.assertEqual(self.begin().stage, 'reservation')

    def test_absolute_expiry_and_delayed_reply_after_reissue(self):
        self.policy = StabilityPolicy(PolicyConfig(opportunity_seconds=20.0, optimization_retry_seconds=12.0),
                                      clock=self.clock)
        token = self.ready().opportunity_id
        self.evaluate(30, 0.95)
        self.evaluate(39, 0.95)
        self.clock.seconds = 40
        blocked = self.policy.start_intervention('late', action(), 'optimization', opportunity_id=token)
        self.assertEqual(blocked.reasons, ['opportunity_expired'])
        self.assertIsNone(self.policy.state.active)
        for at in (41, 46, 51):
            self.assertEqual(self.evaluate(at, 0.95).recommendation, 'collect_observations')
        newer = self.evaluate(52, 0.95)
        self.assertEqual(newer.recommendation, 'permit_optimization_evaluation')
        self.assertNotEqual(token, newer.opportunity_id)
        self.assertIn('opportunity_id_mismatch', self.policy.start_intervention(
            'late', action(), 'optimization', opportunity_id=token).reasons)
        self.assertEqual(self.begin().stage, 'reservation')

    def test_clear_during_inference_revokes_assessed_candidate(self):
        token = self.ready().opportunity_id
        self.assertEqual(self.policy.assess_proposal(action(), 'optimization', opportunity_id=token).reasons,
                         ['proposal_eligible'])
        self.assertIn('opportunity_changed', self.evaluate(25, 0.65).reasons)
        self.assertIn('no_evaluation_opportunity', self.policy.start_intervention(
            'stale', action(), 'optimization', opportunity_id=token).reasons)
        self.assertIn('recovery_confirmed', self.evaluate(30, 0.65).reasons)

    def test_material_change_in_link_scope_or_health_during_inference(self):
        for case in ('new_hot_link', 'changed_capacity', 'connectivity'):
            with self.subTest(case=case):
                self.setUp()
                token = self.ready().opportunity_id
                sample = self.observation(25, 0.95, reachable=case != 'connectivity')
                links = list(sample.links)
                if case == 'new_hot_link':
                    links.append(LinkMeasurement(link_id='other', observed_at=self.clock(), utilization=0.95,
                                                 utilization_evidence='synthetic'))
                if case == 'changed_capacity':
                    links[0] = links[0].model_copy(update={'effective_capacity_mbps': 50, 'capacity_evidence': 'readback'})
                changed = self.policy.evaluate({**sample.model_dump(), 'links': links})
                self.assertIn('opportunity_changed', changed.reasons)
                self.assertIsNone(self.policy.state.opportunity)
                self.assertNotEqual(self.policy.start_intervention('late', action(), 'optimization',
                                    opportunity_id=token).stage, 'reservation')

    def test_configuration_readback_invalidates_before_reservation(self):
        token = self.ready().opportunity_id
        self.change(25, 'X', 'Y', scope='external-route')
        self.assertIn('no_evaluation_opportunity', self.policy.start_intervention(
            'late', action(), 'optimization', opportunity_id=token).reasons)

    def test_repair_continues_with_same_failed_peers_and_rejects_changed_peers(self):
        for at, fingerprint in ((0, 'peer-a'), (5, 'peer-a'), (15, 'peer-a'), (20, 'peer-b')):
            sample = self.observation(at, ospf=False)
            ospf = sample.ospf.model_copy(update={'scope_fingerprint': fingerprint})
            decision = self.policy.evaluate({**sample.model_dump(), 'ospf': ospf})
            if at == 5:
                token = decision.opportunity_id
            if at == 15:
                self.assertIn('opportunity_pending', decision.reasons)
                self.assertIn('proposal_eligible', self.policy.assess_proposal(action(), 'repair', opportunity_id=token).reasons)
        self.assertIn('opportunity_changed', decision.reasons)
        self.assertIsNone(self.policy.state.opportunity)

    def test_changing_failed_peer_identity_breaks_repair_streak(self):
        for at, peer in ((0, 'a'), (5, 'b'), (10, 'b')):
            sample = self.observation(at, ospf=False)
            ospf = sample.ospf.model_copy(update={'scope_fingerprint': peer})
            decision = self.policy.evaluate({**sample.model_dump(), 'ospf': ospf})
            expected = 'permit_repair_evaluation' if at == 10 else 'collect_observations'
            self.assertEqual(decision.recommendation, expected)

    def test_cached_health_valid_until_it_stales(self):
        token = self.ready().opportunity_id
        health = self.policy.state.recent_observations[-1].ospf
        for at in (30, 36):
            sample = self.observation(at, 0.95)
            result = self.policy.evaluate({**sample.model_dump(), 'ospf': health})
            if at == 30:
                self.assertIn('opportunity_pending', result.reasons)
        self.assertIn('stale_telemetry', result.reasons)
        self.assertIn('no_evaluation_opportunity', self.policy.assess_proposal(action(), 'optimization',
                                                                              opportunity_id=token).reasons)

    def test_repair_sample_gap_retires_request(self):
        self.evaluate(0, reachable=False)
        self.evaluate(5, reachable=False)
        self.assertIn('measurement_gap', self.evaluate(25, reachable=False).reasons)
        self.assertIsNone(self.policy.state.opportunity)

    def test_cancelled_or_rejected_inference_retries_only_after_backoff_and_cooldown(self):
        for reason, next_time in [('cancelled', 36), ('rejected', 46)]:
            with self.subTest(reason=reason):
                self.setUp()
                self.policy = StabilityPolicy(PolicyConfig(optimization_retry_seconds=15, rejected_cooldown=25),
                                              clock=self.clock)
                token = self.ready().opportunity_id
                self.clock.seconds = 21
                self.policy.close_opportunity(token, reason)
                for at in range(22, next_time, 2):
                    self.assertFalse(self.evaluate(at, 0.95).recommendation.startswith('permit_'))
                newer = self.evaluate(next_time, 0.95)
                self.assertEqual(newer.recommendation, 'permit_optimization_evaluation')
                with self.assertRaises(ValueError):
                    self.policy.close_opportunity(token, reason)
                self.assertNotEqual(newer.opportunity_id, token)

    def test_concurrent_reservations_consume_token_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        token = self.ready().opportunity_id
        barrier = Barrier(2)
        def reserve(identifier):
            barrier.wait(timeout=5)
            return self.policy.start_intervention(identifier, action(), 'optimization', opportunity_id=token)
        with ThreadPoolExecutor(max_workers=2) as pool:
            decisions = list(pool.map(reserve, ['one', 'two']))
        self.assertCountEqual([d.recommendation for d in decisions],
                              ['permit_optimization_evaluation', 'suppress_active_intervention'])
        self.assertEqual(len(self.policy.state.interventions), 1)

    def test_restore_drops_inflight_token_but_allows_bounded_retry(self):
        token = self.ready().opportunity_id
        snapshot = self.policy.snapshot().model_dump_json()
        self.clock.seconds = -100
        self.policy = StabilityPolicy.restore(snapshot, clock=self.clock)
        self.assertIn('no_evaluation_opportunity', self.policy.assess_proposal(
            action(), 'optimization', opportunity_id=token).reasons)
        for at in (-99, -89, -79):
            self.assertEqual(self.evaluate(at, 0.95).recommendation, 'collect_observations')
        self.assertEqual(self.evaluate(-70, 0.95).recommendation, 'permit_optimization_evaluation')

    def test_invalid_clock_contract(self):
        for value in (1, datetime(2026, 1, 1)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                StabilityPolicy(clock=lambda: value).snapshot()

    def test_invalid_contracts(self):
        factories = [
            lambda: PolicyConfig(congestion_clear=0.9),
            lambda: PolicyConfig(congestion_samples=1),
            lambda: PolicyConfig(repair_samples=True),
            lambda: PolicyConfig(max_interventions=0),
            lambda: PolicyConfig(success_cooldown=-1.0),
            lambda: PolicyConfig(congestion_high=float('nan')),
            lambda: PolicyConfig(max_age_seconds=float('inf')),
            lambda: PolicyConfig(unknown=True),
            lambda: LinkMeasurement(link_id='a', observed_at=BASE, throughput_mbps=-1.0),
            lambda: LinkMeasurement(link_id='a', observed_at=BASE, utilization=True, utilization_evidence='test'),
            lambda: LinkMeasurement(link_id='a', observed_at=BASE, utilization=0.9),
            lambda: LinkMeasurement(link_id='a', observed_at=BASE, utilization=float('inf'), utilization_evidence='test'),
            lambda: LinkMeasurement(link_id='a', observed_at=BASE, effective_capacity_mbps=100.0),
            lambda: LinkMeasurement(link_id='a', observed_at=BASE, effective_capacity_mbps=0.0, capacity_evidence='test'),
            lambda: LinkMeasurement(link_id='a', observed_at=BASE, throughput_mbps=9.0, throughput_evidence='test',
                                    effective_capacity_mbps=10.0, capacity_evidence='test', utilization=0.1, utilization_evidence='test'),
            lambda: HealthMeasurement(observed_at=BASE, healthy='true', evidence='test'),
            lambda: HealthMeasurement(observed_at=BASE.replace(tzinfo=None), healthy=True, evidence='test'),
            lambda: CandidateAction(action_key='x', scope=' ', before_state='A', after_state='B'),
            lambda: action('A', 'A'),
            lambda: InterventionOutcome(status='failed', network_state_confirmed=False, evidence='test'),
            lambda: InterventionOutcome(status='rollback_unconfirmed', network_state_confirmed=True, evidence='test'),
            lambda: PolicyDecision(evaluated_at=BASE, recommendation='no_intervention', reasons=['healthy'], deployment_authorized=True),
            lambda: SignalState(count=1),
            lambda: PolicyState(active=ActiveIntervention(intervention_id='orphan')),
        ]
        for factory in factories:
            with self.subTest(factory=factory), self.assertRaises(ValidationError):
                factory()

    def test_duplicate_links_invalid_observation_and_unknown_kind(self):
        sample = self.observation(0)
        self.assertIn('invalid_telemetry', self.policy.evaluate({**sample.model_dump(), 'links': sample.links * 2}).reasons)
        with self.assertRaises(ValueError):
            self.policy.assess_proposal(action(), 'invalid', opportunity_id=self.request_id)

    def test_invalid_configuration_history_and_snapshots(self):
        self.change(10, 'A', 'B')
        before = self.policy.state
        with self.assertRaises(ValueError):
            self.policy.record_configuration_change(ConfigurationChange(
                occurred_at=BASE + timedelta(seconds=5), scope='network-config', before_state='B',
                after_state='C', evidence='SYNTHETIC old history'))
        self.assertEqual(self.policy.state, before)
        with self.assertRaises(ValueError):
            self.policy.record_configuration_change(ConfigurationChange(
                occurred_at=BASE + timedelta(seconds=20), scope='network-config', before_state='B',
                after_state='C', evidence='SYNTHETIC future history'))
        saved = json.loads(self.policy.snapshot().model_dump_json())
        saved['schema_version'] = 'unknown'
        with self.assertRaises(ValidationError):
            StabilityPolicy.restore(json.dumps(saved), clock=self.clock)
        saved['schema_version'] = '2'
        saved['state']['active'] = {'intervention_id': 'missing', 'status': 'running'}
        with self.assertRaises(ValidationError):
            PolicySnapshot.model_validate_json(json.dumps(saved))


if __name__ == '__main__':
    unittest.main()
