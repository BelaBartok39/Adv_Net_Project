"""Stateful intervention timing and candidate safeguards; no execution authority.

One instance serializes its transitions. Callers persist snapshots after lifecycle
events. All timestamps are aware UTC-compatible datetimes, never raw monotonic
values. No network, inference, verification, or deployment modules are imported.
"""
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import wraps
import math
from threading import RLock
from uuid import uuid4
from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field, ValidationError, model_validator

from .contracts import Contract


Identifier = Annotated[str, Field(min_length=1, pattern=r"\S")]
Seconds = Annotated[float, Field(ge=0, le=31536000)]
Kind = Literal['repair', 'optimization']
Recommendation = Literal[
    'no_intervention', 'collect_observations', 'permit_repair_evaluation',
    'permit_optimization_evaluation', 'suppress_cooldown', 'suppress_unstable_action',
    'suppress_active_intervention', 'insufficient_telemetry', 'suppress_unsafe_state',
]
Reason = Literal[
    'healthy', 'congestion_pending', 'congestion_sustained', 'trigger_latched',
    'recovery_confirmed', 'reachability_failure', 'ospf_failure', 'repair_pending',
    'missing_health', 'missing_utilization', 'invalid_telemetry', 'stale_telemetry',
    'future_telemetry', 'non_increasing_observation', 'measurement_gap',
    'cooldown_active', 'urgent_repair_cooldown_exception', 'frequency_limit',
    'repeated_unsuccessful_action', 'recent_reversal', 'state_switching',
    'active_intervention', 'rollback_unconfirmed', 'proposal_eligible',
    'no_evaluation_opportunity', 'opportunity_expired', 'state_already_applied',
    'clock_regression', 'state_history_mismatch', 'opportunity_pending',
    'opportunity_changed', 'opportunity_cancelled', 'opportunity_rejected',
    'opportunity_retry_wait', 'opportunity_id_mismatch',
]


class PolicyContract(Contract):
    model_config = ConfigDict(frozen=True)


class LinkMeasurement(PolicyContract):
    """One directed interface rate; evidence must refer to this sample's window.

    Evidence strings attest provenance, not independently verified truth. Nominal
    topology speeds and offered traffic rates are not effective capacities.
    """
    link_id: Identifier
    observed_at: AwareDatetime
    throughput_mbps: float | None = Field(default=None, ge=0)
    throughput_evidence: Identifier | None = None
    effective_capacity_mbps: float | None = Field(default=None, gt=0)
    capacity_evidence: Identifier | None = None
    utilization: float | None = Field(default=None, ge=0)
    utilization_evidence: Identifier | None = None

    @model_validator(mode='after')
    def consistent_evidence(self):
        for value, evidence in (
            (self.effective_capacity_mbps, self.capacity_evidence),
            (self.utilization, self.utilization_evidence),
        ):
            if (value is None) != (evidence is None):
                raise ValueError('Capacity/utilization and its evidence must be supplied together')
        if self.throughput_evidence is not None and self.throughput_mbps is None:
            raise ValueError('Throughput evidence requires a measured rate')
        if self.utilization is not None and self.calculated_utilization is not None:
            if not math.isclose(self.utilization, self.calculated_utilization,
                                rel_tol=0.05, abs_tol=0.02):
                raise ValueError('Measured and calculated utilization disagree')
        return self

    @property
    def calculated_utilization(self):
        if (self.throughput_mbps is not None and self.throughput_evidence is not None
                and self.effective_capacity_mbps is not None):
            value = self.throughput_mbps / self.effective_capacity_mbps
            return value if math.isfinite(value) else None
        return None

    @property
    def reliable_utilization(self):
        return self.utilization if self.utilization is not None else self.calculated_utilization


class HealthMeasurement(PolicyContract):
    """Aggregate over the caller's explicit required hosts or OSPF adjacencies."""
    observed_at: AwareDatetime
    healthy: bool | None
    scope_fingerprint: Identifier | None = None
    evidence: Identifier


class NetworkObservation(PolicyContract):
    observation_id: Identifier
    observed_at: AwareDatetime
    links: list[LinkMeasurement] = Field(default_factory=list)
    reachability: HealthMeasurement | None = None
    ospf: HealthMeasurement | None = None

    @model_validator(mode='after')
    def sample_order(self):
        if len({link.link_id for link in self.links}) != len(self.links):
            raise ValueError('Duplicate directed link identifiers')
        samples = [*self.links, self.reachability, self.ospf]
        if any(sample.observed_at > self.observed_at for sample in samples if sample is not None):
            raise ValueError('Measurement is newer than the enclosing observation')
        return self


class PolicyConfig(PolicyContract):
    """Initial experimental settings; live calibration is required."""
    congestion_high: float = Field(default=0.85, gt=0)
    congestion_clear: float = Field(default=0.65, ge=0)
    congestion_samples: int = Field(default=3, ge=2)
    congestion_seconds: Seconds = 20.0
    repair_samples: int = Field(default=2, ge=1)
    repair_seconds: Seconds = 5.0
    min_request_interval: float = Field(default=5.0, gt=0, le=86400)
    opportunity_seconds: float = Field(default=60.0, gt=0, le=86400)
    optimization_retry_seconds: float = Field(default=30.0, gt=0, le=86400)
    repair_retry_seconds: float = Field(default=30.0, gt=0, le=31536000)
    recovery_samples: int = Field(default=2, ge=1)
    recovery_seconds: Seconds = 5.0
    max_age_seconds: float = Field(default=15.0, gt=0, le=86400)
    max_gap_seconds: float = Field(default=15.0, gt=0, le=86400)
    success_cooldown: Seconds = 60.0
    rejected_cooldown: Seconds = 15.0
    failed_cooldown: Seconds = 30.0
    rollback_cooldown: Seconds = 45.0
    unconfirmed_rollback_cooldown: Seconds = 120.0
    urgent_repair_bypasses_cooldown: bool = True
    max_interventions: int = Field(default=4, ge=1)
    intervention_window: float = Field(default=300.0, gt=0, le=31536000)
    max_unsuccessful_actions: int = Field(default=2, ge=1)
    unsuccessful_window: float = Field(default=300.0, gt=0, le=31536000)
    reversal_window: float = Field(default=120.0, gt=0, le=31536000)
    max_state_switches: int = Field(default=3, ge=2)
    switching_window: float = Field(default=300.0, gt=0, le=31536000)
    observation_history_size: int = Field(default=128, ge=1, le=10000)

    @model_validator(mode='after')
    def thresholds(self):
        if self.congestion_clear >= self.congestion_high:
            raise ValueError('Clearing threshold must be below congestion threshold')
        return self


class PolicyDecision(PolicyContract):
    evaluated_at: AwareDatetime
    stage: Literal['observation', 'proposal', 'reservation'] = 'observation'
    recommendation: Recommendation
    reasons: list[Reason] = Field(min_length=1)
    observation_id: Identifier | None = None
    links: list[str] = Field(default_factory=list)
    cooldown_until: AwareDatetime | None = None
    active_intervention_id: Identifier | None = None
    opportunity_id: Identifier | None = None
    expires_at: AwareDatetime | None = None
    deployment_authorized: Literal[False] = False


class CandidateAction(PolicyContract):
    """Canonical fingerprints supplied by the caller, independent of prose/IDs.

    scope identifies the same configuration or route domain across all actions;
    states fingerprint the complete normalized state within that domain.
    """
    action_key: Identifier
    scope: Identifier
    before_state: Identifier
    after_state: Identifier

    @model_validator(mode='after')
    def changes_state(self):
        if self.before_state == self.after_state:
            raise ValueError('Candidate must change state')
        return self


class ConfigurationChange(PolicyContract):
    """An actual observed change, including external changes and rollbacks."""
    occurred_at: AwareDatetime
    scope: Identifier
    before_state: Identifier
    after_state: Identifier
    evidence: Identifier

    @model_validator(mode='after')
    def changes_state(self):
        if self.before_state == self.after_state:
            raise ValueError('History must describe an actual state change')
        return self


class InterventionOutcome(PolicyContract):
    status: Literal['success', 'rejected', 'failed', 'rolled_back', 'rollback_unconfirmed']
    network_state_confirmed: bool
    evidence: Identifier

    @model_validator(mode='after')
    def safe_outcome(self):
        if self.network_state_confirmed != (self.status != 'rollback_unconfirmed'):
            raise ValueError('Uncertain network state must be recorded as rollback_unconfirmed')
        return self


class OutcomeRecord(PolicyContract):
    occurred_at: AwareDatetime
    clock_generation: int = Field(default=0, ge=0)
    outcome: InterventionOutcome


class InterventionRecord(PolicyContract):
    opportunity_id: Identifier
    intervention_id: Identifier
    kind: Kind
    candidate: CandidateAction
    started_at: AwareDatetime
    clock_generation: int = Field(default=0, ge=0)
    frequency_until: AwareDatetime
    unsuccessful_until: AwareDatetime | None = None
    outcomes: list[OutcomeRecord] = Field(default_factory=list)


class ChangeRecord(PolicyContract):
    change: ConfigurationChange
    clock_generation: int = Field(default=0, ge=0)
    reversal_until: AwareDatetime
    switching_until: AwareDatetime


class ActiveIntervention(PolicyContract):
    intervention_id: Identifier
    status: Literal['running', 'rollback_unconfirmed'] = 'running'


class SignalState(PolicyContract):
    first_at: AwareDatetime | None = None
    last_at: AwareDatetime | None = None
    count: int = Field(default=0, ge=0)
    latched: bool = False
    retry_after: AwareDatetime | None = None
    recovery_first_at: AwareDatetime | None = None
    recovery_count: int = Field(default=0, ge=0)

    @model_validator(mode='after')
    def consistent_counts(self):
        if (self.count > 0) != (self.first_at is not None):
            raise ValueError('Signal count requires a first timestamp')
        if (self.recovery_count > 0) != (self.recovery_first_at is not None):
            raise ValueError('Recovery count requires a first timestamp')
        if (self.count or self.recovery_count) and self.last_at is None:
            raise ValueError('Signal count requires a last timestamp')
        if self.last_at is not None and any(at > self.last_at for at in (self.first_at, self.recovery_first_at) if at is not None):
            raise ValueError('Signal timestamps are out of order')
        return self


class EvaluationOpportunity(PolicyContract):
    opportunity_id: Identifier
    kind: Kind
    expires_at: AwareDatetime
    opening: NetworkObservation
    links: list[str]
    reasons: list[Reason]

    @property
    def signal_keys(self):
        if self.kind == 'optimization':
            return ['link:' + name for name in self.links]
        return ['health:' + name for name in ('reachability', 'ospf') if name + '_failure' in self.reasons]


class PolicyState(PolicyContract):
    clock_generation: int = Field(default=0, ge=0)
    recent_observations: list[NetworkObservation] = Field(default_factory=list)
    signals: dict[str, SignalState] = Field(default_factory=dict)
    last_observation_at: AwareDatetime | None = None
    last_clock_at: AwareDatetime | None = None
    cooldown_until: AwareDatetime | None = None
    active: ActiveIntervention | None = None
    interventions: list[InterventionRecord] = Field(default_factory=list)
    changes: list[ChangeRecord] = Field(default_factory=list)
    opportunity: EvaluationOpportunity | None = None
    retry_until: dict[Kind, AwareDatetime] = Field(default_factory=dict)

    @model_validator(mode='after')
    def coherent_history(self):
        ids = [event.intervention_id for event in self.interventions]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate intervention identifiers')
        unfinished = []
        for event in self.interventions:
            times = [(outcome.clock_generation, outcome.occurred_at) for outcome in event.outcomes]
            if times != sorted(times) or any(at < (event.clock_generation, event.started_at) for at in times):
                raise ValueError('Outcome history is out of order')
            statuses = [record.outcome.status for record in event.outcomes]
            if statuses not in ([], ['rollback_unconfirmed'], ['rollback_unconfirmed', 'rolled_back']):
                if len(statuses) != 1 or statuses[0] == 'rollback_unconfirmed':
                    raise ValueError('Invalid outcome sequence')
            if not statuses or statuses[-1] == 'rollback_unconfirmed':
                unfinished.append(event.intervention_id)
        if unfinished != ([self.active.intervention_id] if self.active else []):
            raise ValueError('Active intervention and unfinished history disagree')
        if self.active:
            event = next(event for event in self.interventions if event.intervention_id == self.active.intervention_id)
            expected = 'rollback_unconfirmed' if event.outcomes else 'running'
            if self.active.status != expected:
                raise ValueError('Active intervention status disagrees with outcome')
        times_by_scope = {}
        for record in self.changes:
            change = record.change
            at = (record.clock_generation, change.occurred_at)
            if at < times_by_scope.get(change.scope, at):
                raise ValueError('Configuration history is out of order')
            times_by_scope[change.scope] = at
        if self.opportunity and self.active:
            raise ValueError('An intervention and an inference request cannot both own the slot')
        return self


class PolicySnapshot(PolicyContract):
    schema_version: Literal['2'] = '2'
    clock_format: Literal['aware_datetime'] = 'aware_datetime'
    saved_at: AwareDatetime
    config: PolicyConfig
    state: PolicyState

    @model_validator(mode='after')
    def consistent_clock(self):
        if self.state.last_clock_at != self.saved_at:
            raise ValueError('Snapshot clock anchor disagrees with state')
        return self


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


class StabilityPolicy:
    """Pure policy with caller-owned persistence and serialized lifecycle calls."""

    def __init__(self, config: PolicyConfig | None = None, *, clock: Callable[[], datetime] | None = None,
                 id_factory: Callable[[], str] | None = None):
        self._lock = RLock()
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self.config = PolicyConfig.model_validate(config or PolicyConfig()).model_copy(deep=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._state = PolicyState()

    @property
    @_serialized
    def state(self):
        return self._state.model_copy(deep=True)

    def _now(self):
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Clock must return a timezone-aware datetime')
        return value.astimezone(timezone.utc)

    def _check_clock(self, now):
        return self._state.last_clock_at is None or now >= self._state.last_clock_at

    def _update(self, **fields):
        # Revalidate transitions as well as persisted input; model_copy(update=)
        # deliberately bypasses Pydantic validation and is unsuitable here.
        self._state = PolicyState.model_validate({**self._state.model_dump(), **fields})

    def _decision(self, now, recommendation, reasons, *, stage='observation', links=None, observation_id=None, opportunity=None):
        return PolicyDecision(evaluated_at=now, stage=stage, recommendation=recommendation,
                              reasons=list(dict.fromkeys(reasons)), links=links or [],
                              opportunity_id=opportunity.opportunity_id if opportunity else None,
                              expires_at=opportunity.expires_at if opportunity else None,
                              observation_id=observation_id, cooldown_until=self._state.cooldown_until,
                              active_intervention_id=self._state.active.intervention_id if self._state.active else None)

    def _freshness(self, at, now):
        age = (now - at).total_seconds()
        return 'future_telemetry' if age < 0 else ('stale_telemetry' if age > self.config.max_age_seconds else None)

    def _reset_pending(self):
        self._update(signals={key: SignalState(latched=value.latched, retry_after=value.retry_after)
                              for key, value in self._state.signals.items()}, opportunity=None)

    def _signal(self, key, at, *, degraded, clear, samples, seconds):
        """Advance only once per measurement timestamp; gaps break continuity."""
        old = self._state.signals.get(key, SignalState())
        if old.last_at is not None and at <= old.last_at:
            return False, False, 'non_increasing_observation'
        gap = old.last_at is not None and (at - old.last_at).total_seconds() > self.config.max_gap_seconds
        if gap:
            old = SignalState(latched=old.latched, retry_after=old.retry_after)
        first, count = (old.first_at or at, old.count + 1) if degraded else (None, 0)
        recovery_first, recovery_count = ((old.recovery_first_at or at, old.recovery_count + 1)
                                          if clear else (None, 0))
        recovered = (recovery_count >= self.config.recovery_samples
                     and (at - recovery_first).total_seconds() >= self.config.recovery_seconds)
        latched = old.latched and not recovered and (old.retry_after is None or at < old.retry_after)
        ready = bool(degraded and not latched and count >= samples
                     and (at - first).total_seconds() >= seconds)
        signal = SignalState(first_at=first, last_at=at, count=count, latched=latched,
                             retry_after=old.retry_after if latched else None,
                             recovery_first_at=recovery_first, recovery_count=recovery_count)
        self._update(signals={**self._state.signals, key: signal})
        return ready, bool(recovered and old.latched), 'measurement_gap' if gap else None

    def _block(self, now, *, urgent=False):
        active = self._state.active
        if active:
            if active.status == 'rollback_unconfirmed':
                return 'suppress_unsafe_state', ['rollback_unconfirmed']
            return 'suppress_active_intervention', ['active_intervention']
        if sum(event.frequency_until > now for event in self._state.interventions) >= self.config.max_interventions:
            return 'suppress_unstable_action', ['frequency_limit']
        if self._state.cooldown_until and now < self._state.cooldown_until:
            if not (urgent and self.config.urgent_repair_bypasses_cooldown):
                return 'suppress_cooldown', ['cooldown_active']
        return None

    def _retire(self, now, *, backoff=True):
        """Retire the token. Recovery or a bounded retry can rearm its signals."""
        op = self._state.opportunity
        if op is None:
            return
        delay = self.config.repair_retry_seconds if op.kind == 'repair' else self.config.optimization_retry_seconds
        deadline = now + timedelta(seconds=delay)
        signals = dict(self._state.signals)
        for key in op.signal_keys:
            signal = signals.get(key)
            if signal and signal.latched:
                signals[key] = signal.model_copy(update={'retry_after': deadline})
        retry = dict(self._state.retry_until)
        if backoff:
            retry[op.kind] = max(deadline, retry.get(op.kind, deadline))
        self._update(signals=signals, opportunity=None, retry_until=retry)

    def _unsupported(self, op, latest, now, previous=None):
        """The originating impairment must still match the latest measurement scope."""
        if now >= op.expires_at:
            return 'opportunity_expired'
        def discontinuity(sample, prior):
            if prior is None or sample is None:
                return None
            gap = (sample.observed_at - prior.observed_at).total_seconds()
            if gap < 0 or (gap == 0 and sample != prior):
                return 'non_increasing_observation'
            if gap > min(self.config.max_gap_seconds, self.config.max_age_seconds):
                return 'measurement_gap'
            return None

        for name in ('reachability', 'ospf'):
            sample = getattr(latest, name)
            opening = getattr(op.opening, name)
            # Unknown unrelated health is allowed for a known repair. A changed
            # health signature requires a new diagnosis, including for repairs.
            expected = opening.healthy if opening else None
            if (sample.healthy if sample else None) != expected:
                return 'opportunity_changed'
            if sample and opening and sample.scope_fingerprint != opening.scope_fingerprint:
                return 'opportunity_changed'
            if op.kind == 'optimization' or name + '_failure' in op.reasons:
                if not sample or self._freshness(sample.observed_at, now):
                    return 'stale_telemetry'
                issue = discontinuity(sample, getattr(previous, name, None))
                if issue:
                    return issue
        if op.kind == 'repair':
            return None
        original = {link.link_id: link for link in op.opening.links}
        if set(original) != {link.link_id for link in latest.links}:
            return 'opportunity_changed'
        prior_links = {link.link_id: link for link in previous.links} if previous else {}
        for link in latest.links:
            issue = discontinuity(link, prior_links.get(link.link_id))
            if issue:
                return issue
            opening_link = original[link.link_id]
            utilization = link.reliable_utilization
            if self._freshness(link.observed_at, now):
                return 'stale_telemetry'
            if utilization is None:
                return 'missing_utilization'
            if link.effective_capacity_mbps != opening_link.effective_capacity_mbps:
                return 'opportunity_changed'
            if link.link_id in op.links:
                if utilization <= self.config.congestion_clear:
                    return 'opportunity_changed'
            elif (utilization >= self.config.congestion_high) != (opening_link.reliable_utilization >= self.config.congestion_high):
                return 'opportunity_changed'
        return None

    @_serialized
    def close_opportunity(self, opportunity_id: str, reason: Literal['cancelled', 'rejected']):
        """Close an inference request that produced no usable candidate."""
        now = self._now()
        op = self._state.opportunity
        if not self._check_clock(now) or not op or op.opportunity_id != opportunity_id:
            raise ValueError('Clock regression or no matching open opportunity')
        if reason not in ('cancelled', 'rejected'):
            raise ValueError('Expected cancelled or rejected')
        self._retire(now)
        deadline = self._state.cooldown_until
        if reason == 'rejected':
            until = now + timedelta(seconds=self.config.rejected_cooldown)
            deadline = max(until, deadline or until)
        self._update(last_clock_at=now, cooldown_until=deadline)
        return self._decision(now, 'collect_observations', ['opportunity_' + reason], opportunity=op)

    @_serialized
    def evaluate(self, observation: NetworkObservation | dict) -> PolicyDecision:
        now = self._now()
        if not self._check_clock(now):
            self._retire(self._state.last_clock_at)
            self._reset_pending()
            return self._decision(now, 'insufficient_telemetry', ['clock_regression'])
        self._update(last_clock_at=now)
        try:
            observation = NetworkObservation.model_validate(observation).model_copy(deep=True)
        except ValidationError:
            self._retire(now)
            self._reset_pending()
            return self._decision(now, 'insufficient_telemetry', ['invalid_telemetry'])
        error = self._freshness(observation.observed_at, now)
        if error:
            self._retire(now)
            self._reset_pending()
            return self._decision(now, 'insufficient_telemetry', [error], observation_id=observation.observation_id)
        if self._state.last_observation_at and observation.observed_at <= self._state.last_observation_at:
            if (observation.observed_at == self._state.last_observation_at
                    and observation != self._state.recent_observations[-1]):
                self._retire(now)
                self._reset_pending()
            return self._decision(now, 'insufficient_telemetry', ['non_increasing_observation'],
                                  observation_id=observation.observation_id)
        previous = self._state.recent_observations[-1] if self._state.recent_observations else None
        self._update(last_observation_at=observation.observed_at,
                     recent_observations=[*self._state.recent_observations, observation][-self.config.observation_history_size:])
        reasons, ready_repairs, ready_links, unknown = [], [], [], []
        health_values = {}
        for name in ('reachability', 'ospf'):
            sample = getattr(observation, name)
            key = 'health:' + name
            error = self._freshness(sample.observed_at, now) if sample else 'missing_health'
            if error or sample.healthy is None:
                unknown.append(error or 'missing_health')
                continue
            prior = getattr(previous, name, None)
            if prior and sample.scope_fingerprint != prior.scope_fingerprint:
                # A failure on another set of peers/pairs starts its own streak.
                signals = dict(self._state.signals)
                signals.pop(key, None)
                self._update(signals=signals)
            health_values[name] = sample.healthy
            ready, recovered, error = self._signal(key, sample.observed_at, degraded=not sample.healthy,
                                                  clear=sample.healthy, samples=self.config.repair_samples,
                                                  seconds=self.config.repair_seconds)
            if error:
                (unknown if error == 'non_increasing_observation' else reasons).append(error)
            if ready:
                ready_repairs.append(name)
            if recovered:
                reasons.append('recovery_confirmed')
        measured_links = set()
        for link in observation.links:
            key = 'link:' + link.link_id
            error = self._freshness(link.observed_at, now)
            utilization = link.reliable_utilization
            if error or utilization is None:
                unknown.append(error or 'missing_utilization')
                continue
            measured_links.add(link.link_id)
            ready, recovered, error = self._signal(key, link.observed_at,
                                                  degraded=utilization >= self.config.congestion_high,
                                                  clear=utilization <= self.config.congestion_clear,
                                                  samples=self.config.congestion_samples,
                                                  seconds=self.config.congestion_seconds)
            if error:
                (unknown if error == 'non_increasing_observation' else reasons).append(error)
            if ready:
                ready_links.append(link.link_id)
            if recovered:
                reasons.append('recovery_confirmed')
        # Missing/stale samples reset pending evidence, but cannot clear a latch.
        valid = {'health:' + name for name in health_values} | {'link:' + name for name in measured_links}
        self._update(signals={key: value if key in valid else SignalState(latched=value.latched, retry_after=value.retry_after)
                              for key, value in self._state.signals.items()})
        op = self._state.opportunity
        if op:
            invalid = self._unsupported(op, observation, now, previous)
            if not invalid:
                return self._decision(now, 'collect_observations', ['opportunity_pending', 'trigger_latched'],
                                      observation_id=observation.observation_id, links=op.links, opportunity=op)
            self._retire(now, backoff=invalid != 'opportunity_changed')
            reasons.append(invalid)
        urgent = 'reachability' in ready_repairs
        block = self._block(now, urgent=urgent)
        if block:
            return self._decision(now, block[0], [*block[1], *reasons], links=ready_links,
                                  observation_id=observation.observation_id)
        if ready_repairs:
            keys = ['health:' + name for name in ready_repairs]
            recommendation = 'permit_repair_evaluation'
            reasons += [name + '_failure' for name in ready_repairs]
        elif any(value is False for value in health_values.values()):
            return self._decision(now, 'collect_observations', [*reasons, 'repair_pending'],
                                  observation_id=observation.observation_id)
        elif unknown or len(health_values) != 2 or not observation.links:
            return self._decision(now, 'insufficient_telemetry',
                                  [*reasons, *unknown, *(['missing_utilization'] if not observation.links else [])],
                                  observation_id=observation.observation_id)
        elif ready_links:
            keys = ['link:' + name for name in ready_links]
            recommendation = 'permit_optimization_evaluation'
            reasons.append('congestion_sustained')
        else:
            latched = any(value.latched for value in self._state.signals.values())
            pending = any(value.count for key, value in self._state.signals.items() if key.startswith('link:'))
            return self._decision(now, 'collect_observations' if latched or pending else 'no_intervention',
                                  [*reasons, 'trigger_latched' if latched else ('congestion_pending' if pending else 'healthy')],
                                  observation_id=observation.observation_id)
        if urgent and self._state.cooldown_until and now < self._state.cooldown_until:
            reasons.append('urgent_repair_cooldown_exception')
        kind = 'repair' if ready_repairs else 'optimization'
        retry = self._state.retry_until.get(kind)
        if retry and now < retry:
            return self._decision(now, 'collect_observations', [*reasons, 'opportunity_retry_wait'],
                                  observation_id=observation.observation_id)
        signals = dict(self._state.signals)
        for key in keys:
            signals[key] = signals[key].model_copy(update={'latched': True, 'retry_after': None})
        op = EvaluationOpportunity(opportunity_id=self._id_factory(), kind=kind, opening=observation,
                                   links=ready_links if kind == 'optimization' else [],
                                   reasons=reasons, expires_at=now + timedelta(seconds=self.config.opportunity_seconds))
        decision = self._decision(now, recommendation, reasons, links=op.links, opportunity=op,
                                  observation_id=observation.observation_id)
        next_request = now + timedelta(seconds=self.config.min_request_interval)
        self._update(signals=signals, opportunity=op, retry_until={**self._state.retry_until, kind: next_request})
        return decision

    @_serialized
    def assess_proposal(self, candidate: CandidateAction, kind: Kind, *, opportunity_id: str) -> PolicyDecision:
        """Assess history and a fresh evaluation opportunity; never authorize deployment."""
        candidate = CandidateAction.model_validate(candidate)
        if kind not in ('repair', 'optimization'):
            raise ValueError('Unknown intervention kind')
        now = self._now()
        if not self._check_clock(now):
            self._retire(self._state.last_clock_at)
            return self._decision(now, 'insufficient_telemetry', ['clock_regression'], stage='proposal')
        self._update(last_clock_at=now)
        opportunity = self._state.opportunity
        urgent = bool(opportunity and 'reachability_failure' in opportunity.reasons)
        block = self._block(now, urgent=urgent)
        if block:
            return self._decision(now, *block, stage='proposal')
        if not opportunity or opportunity.kind != kind:
            return self._decision(now, 'collect_observations', ['no_evaluation_opportunity'], stage='proposal')
        if opportunity.opportunity_id != opportunity_id:
            return self._decision(now, 'collect_observations', ['opportunity_id_mismatch'], stage='proposal')
        latest = self._state.recent_observations[-1]
        invalid = self._unsupported(opportunity, latest, now)
        if invalid:
            self._retire(now)
            return self._decision(now, 'insufficient_telemetry', [invalid], stage='proposal', opportunity=opportunity)
        failures = [event for event in self._state.interventions
                    if event.unsuccessful_until and event.unsuccessful_until > now
                    and event.candidate.scope == candidate.scope
                    and (event.candidate.action_key == candidate.action_key
                         or (event.candidate.before_state, event.candidate.after_state)
                         == (candidate.before_state, candidate.after_state))]
        if len(failures) >= self.config.max_unsuccessful_actions:
            return self._decision(now, 'suppress_unstable_action', ['repeated_unsuccessful_action'], stage='proposal')
        records = [record for record in self._state.changes if record.change.scope == candidate.scope]
        if records and records[-1].change.after_state == candidate.after_state:
            return self._decision(now, 'suppress_unstable_action', ['state_already_applied'], stage='proposal')
        if records and records[-1].change.after_state != candidate.before_state:
            return self._decision(now, 'insufficient_telemetry', ['state_history_mismatch'], stage='proposal')
        if any(record.reversal_until > now and record.change.before_state == candidate.after_state
               and record.change.after_state == candidate.before_state for record in records):
            return self._decision(now, 'suppress_unstable_action', ['recent_reversal'], stage='proposal')
        recent = [record.change for record in records if record.switching_until > now]
        # Returning to a previously observed state after enough actual changes
        # is switching evidence. Changing congestion alone never enters here.
        states = {state for change in recent for state in (change.before_state, change.after_state)}
        if len(recent) >= self.config.max_state_switches and candidate.after_state in states:
            return self._decision(now, 'suppress_unstable_action', ['state_switching'], stage='proposal')
        return self._decision(now, 'permit_' + kind + '_evaluation', ['proposal_eligible'], stage='proposal',
                              links=opportunity.links, observation_id=latest.observation_id, opportunity=opportunity)

    @_serialized
    def start_intervention(self, intervention_id: str, candidate: CandidateAction, kind: Kind, *, opportunity_id: str) -> PolicyDecision:
        """Reserve one policy slot before external verification/deployment begins."""
        decision = self.assess_proposal(candidate, kind, opportunity_id=opportunity_id)
        if not decision.recommendation.startswith('permit_'):
            return decision
        event = InterventionRecord(opportunity_id=opportunity_id, intervention_id=intervention_id, candidate=candidate, kind=kind,
                                   started_at=decision.evaluated_at,
                                   clock_generation=self._state.clock_generation,
                                   frequency_until=decision.evaluated_at + timedelta(seconds=self.config.intervention_window))
        if any(old.intervention_id == event.intervention_id for old in self._state.interventions):
            raise ValueError('Intervention identifier already used')
        self._update(active=ActiveIntervention(intervention_id=intervention_id),
                     interventions=[*self._state.interventions, event], opportunity=None)
        return PolicyDecision.model_validate({**decision.model_dump(), 'stage': 'reservation',
                                              'active_intervention_id': intervention_id})

    @_serialized
    def record_configuration_change(self, change: ConfigurationChange):
        """Record readback evidence, never an assumed effect of a proposal."""
        change = ConfigurationChange.model_validate(change).model_copy(deep=True)
        now = self._now()
        if not self._check_clock(now) or change.occurred_at > now:
            raise ValueError('Clock regression or future configuration history')
        age = (now - change.occurred_at).total_seconds()
        record = ChangeRecord(change=change, clock_generation=self._state.clock_generation,
                              reversal_until=now + timedelta(seconds=max(0, self.config.reversal_window - age)),
                              switching_until=now + timedelta(seconds=max(0, self.config.switching_window - age)))
        self._update(last_clock_at=now, changes=[*self._state.changes, record])
        self._retire(now)

    @_serialized
    def complete_intervention(self, intervention_id: str, outcome: InterventionOutcome):
        """Record caller-confirmed outcome; uncertainty holds the active slot."""
        outcome = InterventionOutcome.model_validate(outcome).model_copy(deep=True)
        now = self._now()
        if not self._check_clock(now):
            raise ValueError('Clock regression')
        active = self._state.active
        if not active or active.intervention_id != intervention_id:
            raise ValueError('Outcome does not match the active intervention')
        if active.status == 'rollback_unconfirmed' and outcome.status != 'rolled_back':
            raise ValueError('Unconfirmed rollback requires explicit confirmed rollback')
        events = list(self._state.interventions)
        index = next(index for index, event in enumerate(events) if event.intervention_id == intervention_id)
        event = events[index]
        duration = getattr(self.config, {
            'success': 'success_cooldown', 'rejected': 'rejected_cooldown', 'failed': 'failed_cooldown',
            'rolled_back': 'rollback_cooldown', 'rollback_unconfirmed': 'unconfirmed_rollback_cooldown',
        }[outcome.status])
        events[index] = InterventionRecord.model_validate({
            **event.model_dump(),
            'outcomes': [*event.outcomes, OutcomeRecord(occurred_at=now, clock_generation=self._state.clock_generation, outcome=outcome)],
            'unsuccessful_until': now + timedelta(seconds=self.config.unsuccessful_window)
            if outcome.status != 'success' else None,
        })
        deadline = now + timedelta(seconds=duration)
        self._update(last_clock_at=now, interventions=events,
                     cooldown_until=max(deadline, self._state.cooldown_until or deadline),
                     active=ActiveIntervention(intervention_id=intervention_id, status='rollback_unconfirmed')
                     if outcome.status == 'rollback_unconfirmed' else None,
                     # A finished attempt needs fresh sustained evidence before retry.
                     # This also allows unresolved repairs to retry after success.
                     signals={key: SignalState() for key in self._state.signals}, opportunity=None)

    @_serialized
    def snapshot(self) -> PolicySnapshot:
        now = self._now()
        if not self._check_clock(now):
            raise ValueError('Cannot snapshot after clock regression')
        self._update(last_clock_at=now)
        return PolicySnapshot(saved_at=now, config=self.config, state=self.state).model_copy(deep=True)

    @classmethod
    def restore(cls, snapshot: PolicySnapshot | str, *, clock: Callable[[], datetime] | None = None):
        """Restore conservatively: downtime never spends safety-window budgets.

        Remaining cooldown/history windows are rebased onto the new clock, even
        if its origin moved forward or backward. Audit timestamps stay intact.
        Fresh measurements must rebuild streaks; emitted trigger latches survive.
        Active and unconfirmed interventions remain held without an expiry.
        """
        saved = (PolicySnapshot.model_validate_json(snapshot) if isinstance(snapshot, str)
                 else PolicySnapshot.model_validate(snapshot)).model_copy(deep=True)
        policy = cls(saved.config, clock=clock)
        now = policy._now()

        def rebase(deadline):
            return None if deadline is None else now + max(deadline - saved.saved_at, timedelta(0))

        events = [InterventionRecord.model_validate({**event.model_dump(),
                  'frequency_until': rebase(event.frequency_until),
                  'unsuccessful_until': rebase(event.unsuccessful_until)}) for event in saved.state.interventions]
        changes = [ChangeRecord.model_validate({**record.model_dump(),
                   'reversal_until': rebase(record.reversal_until),
                   'switching_until': rebase(record.switching_until)}) for record in saved.state.changes]
        policy._state = PolicyState.model_validate({**saved.state.model_dump(),
            'clock_generation': saved.state.clock_generation + 1,
            'last_clock_at': now, 'last_observation_at': None,
            'cooldown_until': rebase(saved.state.cooldown_until), 'interventions': events, 'changes': changes,
            'retry_until': {kind: rebase(at) for kind, at in saved.state.retry_until.items()},
            'signals': {key: SignalState(latched=value.latched, retry_after=rebase(value.retry_after))
                                           for key, value in saved.state.signals.items()}})
        policy._retire(now)
        return policy
