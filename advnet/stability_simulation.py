"""Reproducible SYNTHETIC stability-policy trace; no live network or inference."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import count
import json
from pathlib import Path

from .stability_policy import (
    CandidateAction, ConfigurationChange, HealthMeasurement, InterventionOutcome,
    LinkMeasurement, NetworkObservation, PolicyConfig, StabilityPolicy,
)


ANCHOR = datetime(2026, 1, 1, tzinfo=timezone.utc)


class SyntheticClock:
    def __init__(self):
        self.seconds = 0

    def __call__(self):
        return ANCHOR + timedelta(seconds=self.seconds)


def _state(policy):
    state = policy.state
    return {
        'signals': {key: value.model_dump(mode='json') for key, value in state.signals.items()},
        'cooldown_until': state.cooldown_until.isoformat() if state.cooldown_until else None,
        'active': state.active.model_dump(mode='json') if state.active else None,
        'intervention_count': len(state.interventions), 'configuration_change_count': len(state.changes),
        'opportunity': state.opportunity.opportunity_id if state.opportunity else None,
    }


def run_simulation():
    """Run fixed synthetic cases through the actual policy and check milestones.

    Outcomes are scripted inputs, never measured improvements or optimization
    claims. The longer switching window is an explicit simulation setting.
    """
    clock = SyntheticClock()
    sequence = count(1)
    policy = StabilityPolicy(PolicyConfig(switching_window=600.0), clock=clock,
                             id_factory=lambda: f'synthetic-request-{next(sequence)}')
    trace = []
    request_id = None

    def candidate(before, after):
        return CandidateAction(action_key=f'{before}->{after}', scope='synthetic-network-config',
                               before_state=before, after_state=after)

    def change(at, before, after):
        policy.record_configuration_change(ConfigurationChange(
            occurred_at=ANCHOR + timedelta(seconds=at), scope='synthetic-network-config',
            before_state=before, after_state=after, evidence='SYNTHETIC scripted configuration readback'))

    def finish(identifier, status):
        policy.complete_intervention(identifier, InterventionOutcome(
            status=status, network_state_confirmed=status != 'rollback_unconfirmed',
            evidence='SYNTHETIC scripted outcome; no configuration was deployed'))

    def step(at, utilization=0.95, *, reachable=True, label='sustained demand',
             proposals=(), completion=None, missing_capacity=False, reply_to=None):
        nonlocal request_id
        clock.seconds = at
        before = _state(policy)
        lifecycle = []
        if completion:
            # Synthetic readback resolves the earlier uncertain B->C attempt.
            change(100, 'B', 'C')
            change(at, 'C', 'B')
            finish(completion, 'rolled_back')
            lifecycle.append({'intervention_id': completion, 'outcome': 'rolled_back', 'synthetic': True})
        health = HealthMeasurement(observed_at=clock(), healthy=True, evidence='SYNTHETIC required OSPF peers')
        link = (LinkMeasurement(link_id='r1:Ethernet1', observed_at=clock(), throughput_mbps=95.0,
                                throughput_evidence='SYNTHETIC measured Mbps; effective capacity unknown')
                if missing_capacity else LinkMeasurement(link_id='r1:Ethernet1', observed_at=clock(),
                    utilization=utilization, utilization_evidence='SYNTHETIC reliable utilization'))
        observation = NetworkObservation(
            observation_id=f'synthetic-{at}', observed_at=clock(), links=[link], ospf=health,
            reachability=HealthMeasurement(observed_at=clock(), healthy=reachable, evidence='SYNTHETIC host matrix'))
        decision = policy.evaluate(observation)
        if decision.recommendation.startswith('permit_'):
            request_id = decision.opportunity_id
        assessments = []
        token = reply_to or request_id
        for proposed_before, proposed_after, identifier, status in proposals:
            proposed = candidate(proposed_before, proposed_after)
            kind = 'repair' if decision.recommendation == 'permit_repair_evaluation' else 'optimization'
            assessment = policy.assess_proposal(proposed, kind, opportunity_id=token)
            assessments.append({'request_id': token, 'candidate': proposed.model_dump(mode='json'),
                                'decision': assessment.model_dump(mode='json')})
            if assessment.recommendation.startswith('permit_') and identifier:
                reservation = policy.start_intervention(identifier, proposed, kind, opportunity_id=token)
                if reservation.stage != 'reservation':
                    raise RuntimeError('Synthetic reservation unexpectedly suppressed')
                if status == 'success':
                    change(at, proposed_before, proposed_after)
                finish(identifier, status)
                lifecycle.append({'intervention_id': identifier, 'outcome': status, 'synthetic': True})
        trace.append({'synthetic': True, 'at_seconds': at, 'label': label,
                      'observation': observation.model_dump(mode='json'),
                      'decision': decision.model_dump(mode='json'), 'proposal_assessments': assessments,
                      'intervention_outcomes': lifecycle, 'state_before': before, 'state_after': _state(policy)})

    step(0, 0.4, label='healthy baseline')
    step(10, label='temporary congestion')
    step(20, 0.4, label='temporary congestion cleared naturally')
    step(30, 0.4, label='healthy')
    step(40)
    step(50)
    step(60, proposals=[('A', 'B', 'opt-1', 'success')], label='first sustained event; scripted success')
    step(70, label='success cooldown')
    step(80, label='success cooldown')
    step(90, reachable=False, label='connectivity failure during cooldown')
    step(100, reachable=False, label='urgent repair; reversal blocked; alternative rollback uncertain',
         proposals=[('B', 'A', None, None), ('B', 'C', 'repair-1', 'rollback_unconfirmed')])
    step(110, reachable=False, label='unsafe state holds active slot')
    step(240, reachable=False, label='unsafe state remains held after cooldown deadline')
    step(250, 0.4, completion='repair-1', label='explicit synthetic rollback confirmation')
    step(260, 0.4, label='rollback cooldown')
    step(300)
    step(310)
    step(320, label='recent reversal blocked; alternative proposal rejected',
         proposals=[('B', 'C', None, None), ('B', 'D', 'opt-2', 'rejected')])
    step(330, label='rejection cooldown')
    step(340)
    step(350, proposals=[('B', 'D', 'opt-3', 'rejected')], label='second unsuccessful identical action')
    step(360, label='rejection cooldown')
    step(370, label='rejection cooldown expired; collecting')
    step(380, proposals=[('B', 'D', None, None), ('B', 'E', 'opt-4', 'failed')],
         label='unsuccessful action limit; alternative fails before mutation')
    step(390, label='failure cooldown')
    step(400, label='failure cooldown')
    step(410, proposals=[('B', 'F', 'opt-5', 'success')], label='cooldown expires; scripted success')
    step(420, label='success cooldown')
    step(430, label='success cooldown')
    step(440, 0.4, label='natural recovery during cooldown')
    step(450, 0.4, label='recovery samples during cooldown')
    step(500)
    step(510)
    step(520, label='intervention frequency limit')
    step(540, reachable=False, label='urgent repair cannot bypass frequency limit')
    step(545, reachable=False, label='urgent repair still frequency limited')
    step(600, 0.4, label='health recovers')
    step(610, 0.4, label='health recovery confirmed')
    step(620)
    step(630)
    step(640, proposals=[('F', 'A', None, None), ('F', 'G', None, None)],
         label='actual configuration switching blocks old state; novel state eligible')
    step(650, 0.4, label='recovery')
    step(660, 0.4, label='rearm')
    step(700)
    step(710)
    step(720, label='start delayed synthetic inference')
    step(725, label='monitoring while inference is pending')
    step(730, proposals=[('F', 'H', 'opt-6', 'success')], label='independent later event')
    step(740, missing_capacity=True, label='throughput alone is insufficient')
    step(800, missing_capacity=True, label='missing effective capacity after cooldown')

    for at in range(810, 901, 10):
        step(at, label='persistent congestion with delayed inference')
        if at == 830:
            expired_request = request_id
    step(910, label='retry backoff')
    step(920, label='delayed response to expired request while replacement is open',
         proposals=[('H', 'I', None, None)], reply_to=expired_request)

    by_time = {row['at_seconds']: row for row in trace}
    expected = {
        0: 'no_intervention', 20: 'no_intervention', 60: 'permit_optimization_evaluation',
        70: 'suppress_cooldown', 100: 'permit_repair_evaluation', 240: 'suppress_unsafe_state',
        250: 'suppress_cooldown', 410: 'permit_optimization_evaluation',
        520: 'suppress_unstable_action', 545: 'suppress_unstable_action',
        640: 'permit_optimization_evaluation', 720: 'permit_optimization_evaluation', 800: 'insufficient_telemetry',
    }
    checks = []
    for at, recommendation in expected.items():
        if by_time[at]['decision']['recommendation'] != recommendation:
            raise RuntimeError(f'Synthetic milestone {at}: expected {recommendation}, got {by_time[at]["decision"]}')
        checks.append(f'{at}s: {recommendation}')
    for at, reason in [(100, 'recent_reversal'), (320, 'recent_reversal'),
                       (380, 'repeated_unsuccessful_action'), (640, 'state_switching')]:
        if reason not in by_time[at]['proposal_assessments'][0]['decision']['reasons']:
            raise RuntimeError(f'Synthetic proposal milestone {at}: missing {reason}')
        checks.append(f'{at}s proposal: {reason}')
    issued = by_time[720]['decision']
    returned = by_time[730]['proposal_assessments'][0]['decision']
    if (by_time[725]['decision']['reasons'][0] != 'opportunity_pending'
            or issued['opportunity_id'] != returned['opportunity_id']
            or returned['observation_id'] != 'synthetic-730'
            or returned['reasons'] != ['proposal_eligible']):
        raise RuntimeError('Delayed inference did not retain its token and validate the latest sample')
    checks.append('730s: original request, latest telemetry, one reservation')
    if ('opportunity_expired' not in by_time[890]['decision']['reasons']
            or 'opportunity_id_mismatch' not in by_time[920]['proposal_assessments'][0]['decision']['reasons']
            or by_time[920]['decision']['recommendation'] != 'permit_optimization_evaluation'):
        raise RuntimeError('Expected expiration, bounded retry and refusal of the older reply')
    checks.append('890s: fixed deadline expires during monitoring')
    checks.append('920s: replacement opens, expired reply refused')
    summary = {
        'synthetic': True,
        'scope': 'Offline policy logic only; scripted outcomes, no live optimization or performance improvement.',
        'evaluations': len(trace),
        'recommendations': dict(sorted(Counter(row['decision']['recommendation'] for row in trace).items())),
        'outcomes': dict(sorted(Counter(event['outcome'] for row in trace for event in row['intervention_outcomes']).items())),
        'milestone_checks_passed': len(checks), 'milestones': checks,
        'final_active_intervention': policy.state.active,
        'configuration_changes': len(policy.state.changes),
    }
    return {'trace': trace, 'summary': summary, 'snapshot': policy.snapshot().model_dump(mode='json')}


def _json(value):
    return json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New directory for SYNTHETIC trace and state')
    args = parser.parse_args(argv)
    result = run_simulation()
    try:
        args.output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error('Output directory already exists; choose a new directory')
    (args.output / 'trace.jsonl').write_text(''.join(
        json.dumps(row, sort_keys=True, allow_nan=False) + '\n' for row in result['trace']))
    (args.output / 'summary.json').write_text(_json(result['summary']))
    (args.output / 'final-policy-state.json').write_text(_json(result['snapshot']))
    print(_json(result['summary']), end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
