# Stateful stability policy (offline)

`advnet/stability_policy.py` decides when a future controller may **evaluate** a
repair or optimization. It neither selects a router change nor authorizes its
deployment. `deployment_authorized` is always `false`, including at proposal and
reservation stages. There is no Docker, router, Jev, LLM, Batfish, lab lock, or
experiment-runner operation in this module.

The policy is standalone and is also used by the
[persistent controller](PERSISTENT_CONTROLLER.md). Observations and outcomes in
its unit tests and simulation are **synthetic**; they demonstrate policy logic,
not network optimization, throughput gains, repair effectiveness, or live safety
validation.

## Contracts and evidence

Models inherit the existing strict `advnet.contracts.Contract`: unknown fields,
nonfinite numbers, numeric booleans, naive timestamps, invalid ranges and
inconsistent evidence are rejected. Models are frozen; policy inputs, returned
state and snapshots are copied to protect internal nested collections.

| Contract | Meaning |
| --- | --- |
| `NetworkObservation` | Observation correlation ID, aware timestamp, directed link samples, reachability and OSPF samples, and an optional performance health sample. Every inner sample retains its own timestamp. |
| `LinkMeasurement` | Optional measured Mbps, effective capacity in Mbps, utilization as a ratio, and provenance strings. No nominal speed or offered-demand fallback. |
| `HealthMeasurement` | `healthy=True/False/None` over the caller's explicit required hosts, adjacencies or performance reference, sample timestamp and evidence. Unknown is not healthy. |
| `CandidateAction` | Canonical action fingerprint, stable state-domain scope, before/after state fingerprints. Contains no executable router commands. |
| `ConfigurationChange` | Actual observed before/after states with timestamp and readback evidence; includes external changes and rollbacks. |
| `InterventionOutcome` | `success`, `rejected`, `failed`, `rolled_back` or `rollback_unconfirmed`, explicit state confirmation and supporting evidence. |
| `InterventionRecord`, `ActiveIntervention` | Started attempt, frequency deadline, outcomes, unsuccessful-action deadline, and the single active slot. |
| `PolicyDecision` | Recommendation, bounded machine-readable `Reason` codes, stage, affected links, observation ID, cooldown and active ID. |
| `PolicyState`, `PolicySnapshot` | Recent observations, signal streaks/latches, deadlines, active slot, audit histories, config and versioned clock anchor. |

Utilization is available either directly with `utilization_evidence`, or by
dividing measured throughput with `throughput_evidence` by an explicitly supplied
effective capacity with `capacity_evidence`. Both must describe the same directed
interface and sampling window. A measured rate without effective capacity is
valid telemetry but insufficient for congestion evaluation. Direct utilization
does not require a separately supplied capacity. Ratios above 1 are allowed to
represent over-capacity measurements. When direct and calculated values coexist,
they must agree within 5% relative or 0.02 absolute tolerance; this is an input
consistency check, not a calibrated measurement-error model.

Evidence strings are caller attestations: the policy cannot authenticate them or
independently establish effective capacity. A future collector must supply
same-window measured capacity/shaping evidence and handle counter resets and
measurement errors before using a rate. Synthetic or offered Mbps must never be
silently substituted for measured telemetry.

## Trigger behavior and initial settings

Every value below is an **initial experimental setting requiring live
calibration**, not a scientifically established threshold. All are configurable
through `PolicyConfig`; changing them does not alter existing trial acceptance
criteria. Threshold equality is inclusive. A deadline is expired at equality.

| Setting | Default |
| --- | --- |
| Congestion high / clear ratios | 0.85 / 0.65 |
| Congestion consecutive samples AND minimum duration | 3 AND 20 seconds |
| Repair failure samples AND minimum duration, per health signal | 2 AND 5 seconds |
| Performance failure samples AND minimum duration | 3 AND 20 seconds |
| Fixed lifetime of an inference request (`opportunity_seconds`) | 60 seconds |
| Minimum time between new requests of the same kind (`min_request_interval`) | 5 seconds |
| Backoff after closing an optimization / repair request (`optimization_retry_seconds`, `repair_retry_seconds`) | 30 / 30 seconds |
| Recovery samples AND minimum duration | 2 AND 5 seconds |
| Maximum telemetry age / maximum sample gap | 15 / 15 seconds |
| Success / rejection / confirmed pre-mutation failure cooldown | 60 / 15 / 30 seconds |
| Confirmed rollback / unconfirmed rollback cooldown | 45 / 120 seconds |
| Maximum interventions in a frequency window | 4 starts in 300 seconds |
| Same unsuccessful action limit / window | 2 outcomes / 300 seconds |
| Recent reversal window | 120 seconds |
| State-switching threshold / window | 3 actual changes / 300 seconds |
| Recent observation history size | 128 observations |

Each directed link has its own consecutive-high count and onset timestamp. Both
the count and duration must qualify. Separate links cannot combine their samples
to create a sustained event. Before a trigger, a sample below the high threshold
breaks the high streak. After an optimization recommendation, the trigger is
latched; samples in the deadband between clear and high cannot rearm it.
Consecutive clear samples satisfying the recovery duration rearm it. This permits
multiple independent congestion episodes in one long-lived policy instance.

Reachability and OSPF use independent failure streaks. A fresh known failure can
permit repair evaluation even if other telemetry is unavailable; optimization
requires both health signals to be fresh and healthy and all supplied link
measurements to have reliable utilization. Any measured health failure takes
priority over optimization. A pending repair can retry after its request closes and the repair backoff expires,
provided fresh evidence still meets the failure thresholds. This
prevents a discarded or suppressed candidate from permanently latching out a
legitimate repair. A successful recovery also rearms repair immediately after
the configured recovery count and duration.

An optional `performance` health signal uses its own three-sample, 20-second
failure streak and produces `performance_failure` repair opportunities. The live
collector supplies this signal from matching-demand baseline RTT, per-flow
delivery and loss tolerances, and fingerprints its reference scope so a demand
or capacity change cannot combine failure votes. Unknown performance blocks
optimization when supplied; callers that omit this optional signal retain the
original reachability/OSPF policy interface. Performance repairs respect normal
cooldowns and reversal limits; only reachability failures qualify for the urgent
cooldown exception. Recovery or a changed performance scope invalidates a pending
performance evaluation before its result can be deployed.

The supplied links define the scope: omitted inventory cannot be inferred. The
caller must supply every link it expects the policy to cover. No link samples
means insufficient utilization evidence. Missing, invalid or stale samples reset
pending evidence for affected signals; they cannot prove recovery or clear a
latch. A gap above `max_gap_seconds` restarts sustained and recovery counting.
A fresh, identical cached reading can supply context while another source
advances. For example, staggered healthy reachability and OSPF checks can
support a newly measured congested link. Only a distinct, advancing measurement
counts toward its source's failure or recovery streak. A repeated timestamp may
appear as the informational reason `non_increasing_observation`. A contradictory
reading at the same measurement timestamp produces `conflicting_measurement`
and resets the affected streak. Both versions of that timestamp remain unusable.
A strictly newer valid reading can begin a new streak. Invalid observation dictionaries produce `invalid_telemetry`;
malformed configuration, lifecycle calls and snapshots raise validation errors
without accepting an intervention.

Recommendations are `no_intervention`, `collect_observations`,
`permit_repair_evaluation`, `permit_optimization_evaluation`,
`suppress_cooldown`, `suppress_unstable_action`, `suppress_active_intervention`,
`insufficient_telemetry`, or `suppress_unsafe_state`. Inspect `reasons` for detail:
for example `congestion_sustained`, `stale_telemetry`, `frequency_limit`,
`recent_reversal`, or `rollback_unconfirmed`. A healthy repair scope can still
return insufficient optimization evidence when capacity/utilization is absent.

## Cooldowns and candidate safeguards

Cooldowns begin when outcomes are recorded; an existing longer deadline is never
shortened. Rejected proposals must be recorded through the intervention lifecycle
even if Batfish rejected them before deployment. `failed` means the caller has
confirmed no mutation or a safe unchanged state. Use `rolled_back` for failed
deployment with confirmed rollback. Any uncertain state must be
`rollback_unconfirmed`: it retains the active slot indefinitely, even after its
120-second cooldown deadline. Only explicit confirmed `rolled_back` completion
for that same intervention ID releases the hold. Elapsed time is not evidence
of rollback success.

By default, sustained **reachability** failure can bypass routine cooldowns;
OSPF-only repairs cannot. Disable this through
`urgent_repair_bypasses_cooldown=False` if desired. Urgency does not bypass an
active intervention, unconfirmed rollback, frequency limits, candidate history
checks, verification, checkpoint validation or runtime acceptance requirements.
The exception produces `urgent_repair_cooldown_exception` in the trigger decision.

The live controller confirms an external configuration change before evaluating
the next observation. When a fresh repair proposal exactly restores the state
before the latest external change, that change alone is excluded from the
reversal and switching checks. Earlier history and controller-originated
transitions still count. The repair must pass Batfish and live acceptance.

Recording any intervention outcome establishes an exclusive lower bound on each
inner measurement timestamp. For a completion at 100 s, failures measured at
90 s, 95 s or 100 s cannot justify a new repair. Distinct failures measured at
101 s and 106 s can qualify under the default urgent-repair settings.

The two consultation stages are deliberately distinct:

1. `evaluate(observation)` considers current measured conditions, active state,
   frequency and cooldown. A permit consumes the trigger and records a short-lived
   evaluation opportunity. Evaluate once per new sample.
2. `assess_proposal(candidate, kind, opportunity_id=token)` requires the matching fresh opportunity and
   checks unsuccessful-action history, actual configuration reversals, repeated
   returns to earlier states and agreement with the latest known state. It does
   not infer oscillation from changing congestion.

An unsuccessful action is identified by a stable action key **or** the same
before/after state pair within a scope, so relabelling candidate IDs does not
evade suppression. Recent inverse changes are reversals. After the configured
number of actual scoped changes, returning to a previously seen scoped state
is suppressed as `state_switching`. A novel target remains eligible. An already
applied target is suppressed; a proposed starting state conflicting with known
history requires refreshed evidence (`state_history_mismatch`). All history
windows expire; repairs can retry rather than being permanently blacklisted.

`record_configuration_change()` accepts only actual observed changes in timestamp
order per scope, never a candidate's presumed effect. Do not record a rejected
proposal as an applied route change. Include confirmed rollbacks and independent
operator changes. Scope and fingerprints must be stable and canonical: e.g., a
hash of complete normalized configuration over the affected domain, with volatile
route ages excluded. Use consistently scoped route-state fingerprints for routing
history. The policy cannot discover omitted changes or correlate inconsistent
scopes; the existing deployment layer must still compare the current checkpoint
against the candidate's starting state.

Use a stable `event_id` and the original `occurred_at` for every delivery of
the same transition. Legacy callers can omit the ID if scope, occurrence time,
and before/after fingerprints together uniquely identify the transition.
Repeated delivery, even with different evidence text, preserves the original
history entry, safety deadlines and any pending opportunity. Reusing an ID with
different transition data raises an error. Supply distinct stable IDs when
two actual transitions could otherwise have identical tuples (for example after
a clock reset). Polling an unchanged configuration produces no change event.

## Integration interface

```python
from advnet.stability_policy import (
    CandidateAction, InterventionOutcome, NetworkObservation, StabilityPolicy,
)

policy = StabilityPolicy()  # optionally PolicyConfig(...), clock=aware_utc_callable
observation = NetworkObservation.model_validate_json(observation_json)
decision = policy.evaluate(observation)
# Save decision separately as policy evidence, including its reasons and stage.
if decision.recommendation == 'permit_optimization_evaluation':
    token = decision.opportunity_id  # capture with the work item before starting inference
    # An external component obtains a candidate through its own decision process.
    candidate = CandidateAction(
        action_key=canonical_patch_hash, scope=state_domain,
        before_state=checkpoint_hash, after_state=candidate_state_hash,
    )
    assessment = policy.assess_proposal(candidate, 'optimization', opportunity_id=token)
    if assessment.recommendation == 'permit_optimization_evaluation':
        reservation = policy.start_intervention(attempt_id, candidate, 'optimization', opportunity_id=token)
        if reservation.stage == 'reservation':
            # Persist the held slot BEFORE external verify/deploy/measure/rollback.
            saved_json = policy.snapshot().model_dump_json()
            # External code retains ALL existing safety gates; policy does no IO.
            # Later record actual readback changes and a confirmed outcome:
            policy.complete_intervention(attempt_id, InterventionOutcome(
                status='rejected', network_state_confirmed=True,
                evidence='External verifier rejected the candidate before deployment',
            ))
```

Dispatch a model call once, on an observation-stage permit. Give the worker the
issued `opportunity_id` and keep monitoring. Consistent observations return
`collect_observations` with `opportunity_pending`, the same ID and the original
absolute deadline. An unchanged cached measurement can support the open request
only while its own timestamp remains fresh. Assessment and reservation both
check the worker's ID, current clock, latest evidence, cooldown and action
history. An eligible proposal decision carries the latest observation ID.
Reservation consumes the request and records its ID on the intervention.

A target link at or below the clearing threshold immediately retires its
request. Confirmed recovery still requires the normal count and duration.
Readings in the hysteresis band can support an existing request. Changes to
the monitored link set, known effective capacity, congestion on another link,
health status or a supplied health `scope_fingerprint` require a new assessment.
Use a stable fingerprint of the monitored subjects and failing peers or host
pairs if their identities matter. Aggregate booleans alone cannot reveal a
change from one failed peer to another. Report observed configuration/route
changes via `record_configuration_change()`: the policy conservatively retires
pending inference on each newly recorded transition. Essential evidence gaps and stale
samples also retire the request.

One crossing of the high threshold on a non-target link retires an optimization
request, even if the link returns to its former load in the next sample.
During inference the policy has no proposed path to check for traffic shifting
onto that link. The cancellation decision carries `bystander_threshold_crossing`,
the retired `opportunity_id` and the affected link IDs in `links`. Record it
to quantify discarded requests under bursty traffic. Only an observation-stage
permit starts new inference.

The deadline expires at equality and monitoring never extends it. Expiry is
processed on the next evaluation or matching proposal lookup. If a worker
abandons a request, call `close_opportunity(token, 'cancelled')`. For an
unusable inference result call `close_opportunity(token, 'rejected')`, which
also starts the rejection cooldown. A candidate rejected by a verifier after
reservation still uses `complete_intervention()` so its action enters the
unsuccessful-action history. Assessment of an ineligible candidate alone
leaves room to evaluate an already available alternative under the same token.
Dispatch a new model call only on the next observation-stage permit.

A retired token cannot consume a replacement. After expiry, cancellation, or
rejection, the same kind of request waits out its backoff from closure and
requires fresh sustained evidence. When the impairment changes, confirmed
recovery can rearm the old signal and a newly qualified different impairment
can open a request, subject to the per-kind minimum interval. Cooldowns,
frequency limits, and the unconfirmed rollback hold still apply. A finished
intervention resets signal counts so a retry requires fresh samples.

Calls on one `StabilityPolicy` instance serialize under an in-process lock.
Inference runs outside the lock. The caller owns durable writes, dispatch
deduplication and cancellation of outstanding model calls, and must coordinate
a single authoritative owner across processes. Persist after reservations, outcomes, actual changes and evaluations; on
restart, reconcile an active attempt with external readback rather than clearing
it by age. The caller owns atomic durable writes, decision logs, authentication,
telemetry collection, action generation, all verification/deployment operations,
acceptance measurement and rollback confirmation.

## Persistence and time

```python
saved_json = policy.snapshot().model_dump_json()
restored = StabilityPolicy.restore(saved_json, clock=aware_utc_callable)
```

All inputs use timezone-aware datetimes, normalized by comparisons against an
injectable UTC clock. The module does not persist raw monotonic seconds. In-process
backward clock movement fails safely: evaluations return `clock_regression` and
mutating lifecycle/snapshot calls raise until the clock catches up. Production
callers must supply a stable clock; forward wall-clock corrections during a
running instance are indistinguishable from elapsed time.

Restore is intentionally conservative for **both forward and backward clock
origin changes**: remaining cooldown, frequency, unsuccessful-action, reversal,
switching and repair-retry budgets at snapshot time are rebased onto the new
clock. Downtime never spends these budgets. Repeated restores can extend waiting,
so a future controller should avoid unnecessary reloads. Expired windows remain
expired. Observation and actual-change audit timestamps remain unchanged; a
`clock_generation` marks each restore so new lifecycle events can be ordered even
when the new clock predates earlier audit timestamps. Fresh observations rebuild
streaks; prior emitted latches survive, and the old opportunity is retired with a fresh retry backoff. Snapshot format 2
contains the request ID, opening evidence, deadline and retry windows. The
unreleased format 1 is rejected; do not resume an active intervention by
creating an empty policy if restoration fails.
An active intervention and unconfirmed rollback survive every restore.

Every restore starts a fresh measurement generation, including when restoring
the same checkpoint twice. Read `policy.measurement_generation` when starting
a collection and retain that value on each inner `HealthMeasurement` and
`LinkMeasurement` through caching and delivery. The initial value is `None`
for compatibility with existing producers. After restore, an old or untagged
measurement produces `old_measurement_generation`. Within the new generation
each source must also measure strictly after the restore time. These two checks
preserve ordering across restarts even when clock ranges overlap. Discarding
a cached result and collecting again is necessary if its generation is unknown.
This reader accepts older format-2 snapshots and collapses duplicate legacy
readback entries within the same audit clock generation.

Only recent observations are bounded. Intervention and configuration histories
are retained for audit and identifier uniqueness; callers must plan storage and
archiving for very long experiments. Expired histories no longer suppress
interventions, but the implementation scans retained histories on evaluation.
Snapshots are trusted application state, not signed network evidence; only the
writer/owner should be allowed to supply or edit them.

## Existing components reviewed and reused

- `contracts.py`: reuses strict validation; existing observation/proposal/feedback
  contracts are unchanged. New timing/provenance contracts remain isolated.
- `observer.py`: its measured Mbps and explicit unknown utilization are preserved.
  A future adapter must retain individual health timestamps and effective-capacity
  provenance rather than assuming the manifest capacity.
- `metrics.py`: inference accounting is unrelated to trigger timing and unchanged.
- `pipeline.py`: candidate generation stays external, with all existing retry and
  unconfirmed-rollback gates intact.
- `trial.py`: its canonical patch hashing, checkpoints, verifier gating, config
  readback, acceptance and rollback checks remain authoritative and unchanged.
- `lab.py`: collection/deployment are external. In particular, a utilization value
  derived from `Lab.capacities` alone is not sufficient evidence of live shaping.
- `experiment.py` and bounded runner: no lock, execution or acceptance change.
- PR #6 (`ef645bb752ec4db1d14b937cdc08ae22b343fe6f`, merged into main): its scheduler
  uses relative monotonic timing, and its evidence supplies clock-origin/UTC
  anchors, experiment IDs, flow lifecycle and per-segment receiver measurements.
  It does not supply periodic router utilization or health observations. A future
  adapter must correlate separate policy evidence with the experiment ID and
  convert timestamps using a verified common timeline; offered rates and flow
  events cannot replace measured network state. The PR's exclusive Abilene lock
  remains untouched. The offline regression run includes PR #6 and the corrected policy.

## Reproducible offline simulation

```bash
.venv/bin/python -m advnet.stability_simulation \
  --output /tmp/advnet-stability-simulation
```

Choose a new output directory on each run. The fake UTC clock begins at
2026-01-01; no sleeping, randomness, sockets, Docker or inference is used. The
simulation uses the real policy, default settings except an explicitly longer
600-second switching window. It saves:

| File | Contents |
| --- | --- |
| `trace.jsonl` | 62 timestamped observations, conditions, decisions/reasons, proposal assessments, scripted outcomes, before/after policy state summaries. |
| `summary.json` | Synthetic scope label, decision/outcome counts and 20 checked behavioral milestones. |
| `final-policy-state.json` | Full validated, restorable policy snapshot including audit history and settings. |

Expected outcomes: three scripted successes, two rejected proposals, one confirmed
pre-mutation failure, one unconfirmed rollback subsequently confirmed. These are
**inputs** to the policy, not observed network results. The trace demonstrates
temporary congestion clearing naturally, repeated independent sustained events,
cooldowns/expiry, urgent repair, a persistent unsafe-state hold, repeated-action
and actual-history safeguards, frequency limiting and missing capacity.
See [the requirements checklist](STABILITY_POLICY_CHECKLIST.md) for test mappings
and the branch's offline verification results.
