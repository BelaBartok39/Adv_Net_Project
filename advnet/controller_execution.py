"""Passive runtime evidence and one reserved, checkpointed controller intervention."""
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field, JsonValue, model_validator

from .contracts import Contract, Proposal
from .eos import candidate_configs
from .stability_policy import CandidateAction, InterventionOutcome
from .trial import _batch_records, acceptable, acceptable_concurrent, snapshot


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode()).hexdigest()


@dataclass
class Plan:
    proposal: Proposal
    before: dict
    after: dict
    action: CandidateAction
    demand_epoch: str | None = None


class RuntimeWindow(Contract):
    """A trusted collector attests one scoped, constant-demand measurement window."""
    model_config = ConfigDict(revalidate_instances='always', frozen=True)

    source: Literal['synthetic', 'live']
    start: AwareDatetime
    end: AwareDatetime
    scope: Annotated[str, Field(min_length=1)]
    demand_id: Annotated[str, Field(min_length=1)]
    demand_epoch: Annotated[str, Field(min_length=1)]
    config_sha256: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    capacity_sha256: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')] | None = None
    all_reachable: bool
    ospf_healthy: bool
    mean_rtt_ms: float | None = Field(ge=0)
    probe_pairs: list[dict[str, JsonValue]] = Field(default_factory=list)
    traffic_scope: Literal['confirmed_idle', 'single_flow', 'concurrent_udp']
    traffic: dict | None

    @model_validator(mode='after')
    def valid_window(self):
        if self.start >= self.end:
            raise ValueError('Runtime window must have positive duration')
        if (self.traffic is None) != (self.traffic_scope == 'confirmed_idle'):
            raise ValueError('Receiver evidence required for active traffic')
        if self.traffic is not None and ((self.traffic.get('mode') == 'concurrent_udp')
                                         != (self.traffic_scope == 'concurrent_udp')):
            raise ValueError('Traffic scope differs from measured mode')
        if self.traffic is not None and self.traffic.get('mode') != 'concurrent_udp':
            for key in ('received_mbps', 'loss_fraction', 'max_link_utilization'):
                value = self.traffic.get(key)
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError('Missing receiver or utilization evidence')
            if self.traffic['loss_fraction'] > 1:
                raise ValueError('Invalid measured loss')
        if self.traffic and self.traffic.get('mode') == 'concurrent_udp':
            _batch_records(self.model_dump())
        return self

    def delivered_baseline(self):
        if self.traffic_scope == 'confirmed_idle':
            return True
        receivers = self.traffic['flows'] if self.traffic_scope == 'concurrent_udp' else [self.traffic]
        return all(r['received_mbps'] > 0 and r['loss_fraction'] < 1 for r in receivers)

    def comparable(self, other):
        return ((self.scope, self.source, self.traffic_scope, self.demand_id, self.end-self.start)
                == (other.scope, other.source, other.traffic_scope, other.demand_id, other.end-other.start))

    def performance_check(self, healthy):
        """Reuse acceptance tolerances for ongoing, matched-demand performance.

        Connectivity and OSPF have separate signals. Missing or incomparable
        evidence must remain unknown rather than count as a performance fault.
        """
        if (not self.comparable(healthy) or self.capacity_sha256 != healthy.capacity_sha256
                or (self.source == 'live' and self.capacity_sha256 is None)
                or self.mean_rtt_ms is None or healthy.mean_rtt_ms is None
                or not healthy.all_reachable or not healthy.ospf_healthy
                or not healthy.delivered_baseline()):
            raise ValueError('Incomparable performance reference')
        current = {**self.model_dump(), 'all_reachable': True, 'ospf_healthy': True}
        reference = healthy.model_dump()
        check = acceptable_concurrent if self.traffic_scope == 'concurrent_udp' else acceptable
        return check(current, reference, reference, 'repair')

    def summary(self):
        fields = self.model_dump(mode='json', exclude={'traffic'})
        if self.traffic:
            allowed = ('received_mbps', 'loss_fraction', 'max_link_utilization', 'link_utilization',
                       'mode', 'concurrency_verified', 'overlap_window',
                       'source', 'destination', 'destination_port', 'offered_mbps', 'actual_offered_mbps',
                       'sent_packets', 'received_packets')
            fields['traffic'] = {k: self.traffic[k] for k in allowed if k in self.traffic}
            if self.traffic_scope == 'concurrent_udp':
                keys = ('source', 'destination', 'destination_port', 'offered_mbps', 'seconds',
                        'received_mbps', 'loss_fraction', 'actual_offered_mbps', 'sent_packets', 'received_packets')
                fields['traffic']['flows'] = [{k: flow[k] for k in keys if k in flow}
                                              for flow in self.traffic['flows']]
        return fields


class RuntimeRejection(ValueError):
    """An internal reason code safe to write to the evidence log."""


class RuntimeMeasurementError(ValueError):
    """A collector-owned reason and measurement evidence, without configs/prompts.

    Use only for known internal collector failures. Arbitrary transport/provider
    exceptions retain the executor's existing type-only error reporting.
    """
    def __init__(self, reason, evidence):
        super().__init__(reason)
        self.evidence = evidence


def verified(verdict):
    details = verdict.get('details')
    return (verdict.get('accepted') is True and not verdict.get('process_error')
            and isinstance(details, list) and bool(details) and all(isinstance(x, str) for x in details))


class EvidenceFailure(RuntimeError):
    """The owner lost its audit or persistence channel. Preserve the reservation."""


def outcome(status, reason):
    return InterventionOutcome(status=status, evidence=reason,
                               network_state_confirmed=status != 'rollback_unconfirmed')


class VerifiedExecutor:
    """Inject Lab-compatible transport, a Batfish verifier and a passive evidence reader.

    windows(phase, kind, deadline) returns a RuntimeWindow. 'healthy' is a
    previously verified baseline. 'before' and 'after' cover this intervention.
    Transport and evidence readers must honor the supplied deadline.
    """

    def __init__(self, lab, verifier, windows, scope, *, healthy_configs, normalize=dict,
                 congestion_threshold=None, current_epoch=None):
        self.healthy_configs = dict(healthy_configs)
        self.lab, self.verifier, self.windows, self.scope = lab, verifier, windows, scope
        self.normalize = normalize
        self.congestion_threshold = congestion_threshold
        self.current_epoch = current_epoch

    def config_id(self, configs):
        return fingerprint(self.normalize(configs))

    def readback(self, deadline):
        configs = self.lab.configs(deadline)
        if set(configs) != set(self.lab.manifest['routers']) or not configs or not all(
                isinstance(text, str) and text.strip() for text in configs.values()):
            raise ValueError('Incomplete network configuration readback')
        return configs

    def prepare(self, proposal, observation, deadline):
        proposal.check_scope(observation)
        before = self.readback(deadline)
        if before != {name: node.running_config for name, node in observation.devices.items()}:
            raise ValueError('Inference checkpoint changed')
        after = candidate_configs(before, proposal)
        patch = sorted((c.model_dump() for c in proposal.changes),
                       key=lambda c: (c['device'], c['interface'], c['action']))
        action = CandidateAction(scope=self.scope, action_key=fingerprint(patch),
                                 before_state=self.config_id(before), after_state=self.config_id(after))
        return Plan(proposal, before, after, action, observation.measurements.get('demand_epoch'))

    def execute(self, plan, kind, source, root, deadline, recovery_deadline, now, max_age, transition, changed):
        """Callbacks persist phases/readbacks. Recovery gets its own cleanup budget."""
        mutated = False
        before_id, after_id = plan.action.before_state, plan.action.after_state
        before_normal = self.normalize(plan.before)
        after_normal = self.normalize(plan.after)
        last = before_id
        touched = {change.device for change in plan.proposal.changes}

        def read_change(limit):
            nonlocal last
            actual = self.normalize(self.readback(limit))
            if any(actual[n] not in (before_normal[n], after_normal[n]) for n in actual):
                raise ValueError('Unrecognized external configuration')
            current = fingerprint(actual)
            if current != last:
                changed(last, current)
                last = current
            return current

        try:
            transition('verification')
            if set(self.healthy_configs) != set(plan.before):
                return outcome('rejected', 'incomplete_healthy_checkpoint')
            gate = self.verifier.verify(snapshot(root/'healthy-snapshot', self.healthy_configs, self.lab.manifest), deadline)
            if not verified(gate):
                return outcome('rejected', 'baseline_verification_rejected')
            transition('baseline_verified', config_sha256=self.config_id(self.healthy_configs))
            verdict = self.verifier.verify(snapshot(root/'snapshot', plan.after, self.lab.manifest), deadline)
            if not verified(verdict):
                return outcome('rejected', 'verification_rejected')
            transition('verified', candidate_sha256=plan.action.after_state)
            healthy, before = (RuntimeWindow.model_validate(self.windows(phase, kind, deadline))
                               for phase in ('healthy', 'before'))
            transition('runtime_baseline', windows=[healthy.summary(), before.summary()])
            if (not healthy.comparable(before) or before.source != source or before.scope != self.scope
                    or not healthy.all_reachable or not healthy.ospf_healthy or healthy.mean_rtt_ms is None
                    or not healthy.delivered_baseline()
                    or healthy.config_sha256 != self.config_id(self.healthy_configs)
                    or healthy.end > before.start or before.config_sha256 != plan.action.before_state
                    or (kind == 'optimization' and before.traffic is None)):
                return outcome('rejected', 'incomparable_baseline')
            if (kind == 'repair' and before.all_reachable and before.ospf_healthy
                    and before.performance_check(healthy)['accepted']):
                return outcome('rejected', 'repair_already_healthy')
            if kind == 'optimization':
                utilization = before.traffic['max_link_utilization']
                congested = (utilization > healthy.traffic['max_link_utilization'] * 1.05
                             if self.congestion_threshold is None else utilization >= self.congestion_threshold)
                if (not before.all_reachable or not before.ospf_healthy or not congested
                        or (plan.demand_epoch is not None and before.demand_epoch != plan.demand_epoch)):
                    return outcome('rejected', 'optimization_impairment_unconfirmed')
            if self.readback(deadline) != plan.before:
                return outcome('rejected', 'checkpoint_changed')
            if not 0 <= (now() - before.end).total_seconds() <= max_age:
                return outcome('rejected', 'stale_before_window')
            if self.current_epoch and self.current_epoch() != before.demand_epoch:
                return outcome('rejected', 'demand_changed_before_deployment')
            transition('deployment', devices=sorted(touched), checkpoint_sha256=plan.action.before_state,
                       candidate_sha256=plan.action.after_state)
            deadline.remaining()
            mutated = True
            self.lab.replace({n: plan.after[n] for n in touched}, root/'deploy', deadline)
            if read_change(deadline) != after_id:
                raise RuntimeRejection('incomplete_deployment_readback')
            transition('runtime_validation')
            if self.lab.wait_convergence(deadline, 120, require_healthy=True) is not True:
                raise RuntimeRejection('convergence_unconfirmed')
            since = now()
            after = RuntimeWindow.model_validate(self.windows('after', kind, deadline))
            transition('runtime_measurement', window=after.summary())
            if (not after.comparable(before) or after.demand_epoch != before.demand_epoch
                    or after.capacity_sha256 != before.capacity_sha256
                    or after.start < since or not 0 <= (now() - after.end).total_seconds() <= max_age
                    or after.config_sha256 != plan.action.after_state):
                raise RuntimeRejection('incomparable_after_window')
            stages = [w.model_dump() for w in (after, healthy, before)]
            check = acceptable_concurrent if after.traffic_scope == 'concurrent_udp' else acceptable
            acceptance = check(*stages, kind)
            accepted = acceptance['accepted']
            transition('runtime_result', accepted=accepted, details=acceptance['details'],
                       windows=[w.summary() for w in (healthy, before, after)])
            if not accepted or read_change(deadline) != after_id:
                raise RuntimeRejection('runtime_or_final_readback_rejected')
            deadline.remaining()
            return outcome('success', 'verified_readback_and_runtime')
        except EvidenceFailure:
            raise
        except Exception as error:
            if isinstance(error, RuntimeMeasurementError):
                transition('runtime_result', accepted=False, details=[str(error)],
                           measurement_error=error.evidence)
            if not mutated:
                return outcome('rejected', 'predeploy_' + type(error).__name__)
            limit = recovery_deadline()
            try:
                transition('rollback', reason=str(error) if isinstance(error, (RuntimeRejection, RuntimeMeasurementError))
                           else type(error).__name__)
                current = read_change(limit)
                if current != before_id:
                    self.lab.replace({n: plan.before[n] for n in touched}, root/'rollback', limit)
                if read_change(limit) != before_id:
                    raise ValueError('Rollback checkpoint mismatch')
                return outcome('rolled_back', 'checkpoint_readback_confirmed')
            except EvidenceFailure:
                raise
            except Exception as failure:
                return outcome('rollback_unconfirmed', 'rollback_' + type(failure).__name__)
