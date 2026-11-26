# Stability policy requirements and offline verification

Branch: `feature/stability-policy`, based on fetched `origin/main`
`7176b8a05bf1f592aaf41cc1394a51f91ecb798e`. PR #6 and all existing feature branch
tips are preserved; the dynamic runner is not merged into this branch.

Implementation details and integration responsibilities are in
[STABILITY_POLICY.md](STABILITY_POLICY.md). Test names below refer to actual
`StabilityPolicyTests` methods in `tests/test_stability_policy.py`, unless marked
as simulation tests. They call the real policy with fake clocks and synthetic
measurements; they do not mock its decision logic.

## Required scenarios

| Done | Requested scenario | Implementation | Corresponding test(s) |
| --- | --- | --- | --- |
| [x] | 1. Healthy conditions | `evaluate()` fresh health/utilization checks | `test_healthy` |
| [x] | 2. Temporary congestion clears naturally | Per-link consecutive high streak | `test_temporary_congestion_clears_naturally` |
| [x] | 3. Sustained congestion triggers optimization | Count AND duration, high threshold | `test_sustained_congestion`, `test_duration_and_count_both_required` |
| [x] | 4. Multiple congested links | Independent `SignalState` per directed link | `test_multiple_congested_links_and_independent_streaks`, `test_alternating_congested_links_do_not_combine` |
| [x] | 5. Fluctuation around threshold | Consecutive highs and separate clearing threshold | `test_fluctuating_threshold_never_accumulates_consecutive_highs`, `test_hysteresis_prevents_rearming_in_deadband` |
| [x] | 6. Recovery and trigger rearming | Recovery count/duration and latches | `test_recovery_and_rearming`, `test_repair_recovery_rearms_before_retry_deadline` |
| [x] | 7. Reachability failures | Separate repair signal and priority | `test_reachability_failures`, `test_repair_takes_priority_over_optimization` |
| [x] | 8. OSPF adjacency failures | Caller-scoped OSPF health repair signal | `test_ospf_adjacency_failures`, `test_different_health_failures_do_not_combine` |
| [x] | 9. Success followed by cooldown | Outcome-specific deadline | `test_success_cooldown_and_expiration` |
| [x] | 10. Cooldown expiration | Inclusive expiry and fresh evidence | `test_success_cooldown_and_expiration` |
| [x] | 11. Rejected interventions | Recorded rejection with separate cooldown | `test_all_outcome_cooldowns`, `test_repeated_unsuccessful_actions_and_expiration` |
| [x] | 12. Failed intervention, confirmed rollback | `rolled_back` outcome and safe release | `test_all_outcome_cooldowns`, `test_restored_active_intervention_can_finish_after_clock_origin_changes` |
| [x] | 13. Unconfirmed rollback blocks further intervention | Indefinite active hold until explicit confirmation | `test_unconfirmed_rollback_holds_even_after_deadline_and_for_urgent_repairs`, `test_restored_unconfirmed_rollback_remains_held` |
| [x] | 14. Repeated unsuccessful changes | Scoped action/state-pair history with bounded window | `test_repeated_unsuccessful_actions_and_expiration` |
| [x] | 15. Repeated reversals | Actual-change inverse and state-return safeguards | `test_reversal_uses_actual_configuration_history_and_expires`, `test_repeated_state_switching_uses_real_history` |
| [x] | 16. Missing capacity | No nominal-capacity or offered-rate inference | `test_missing_capacity_does_not_infer_utilization`, `test_verified_capacity_and_throughput_can_calculate_utilization` |
| [x] | 17. Missing, invalid, stale telemetry | Strict contracts, freshness, gaps, unknown evidence | `test_invalid_contracts`, `test_stale_outer_and_individual_telemetry`, `test_missing_and_invalid_samples_break_pending_streak`, `test_future_telemetry_and_out_of_order_measurements` |
| [x] | 18. Overlapping requests | Rechecked reservation and single active ID | `test_overlapping_interventions`, `test_outcome_correlates_to_active_id` |
| [x] | 19. Serialization/restoration | Versioned snapshot, rebased budgets, retained active state | `test_snapshot_roundtrip_preserves_audit_and_cooldown`, `test_restore_rebases_deadlines_for_forward_and_backward_clock_origins`, `test_restore_resets_streaks_and_preserves_latches` |
| [x] | 20. Independent events in continuous experiment | Recovery and repeated evaluations on one instance | `test_many_independent_events_in_one_experiment` |

## Other functional and architecture requirements

| Done | Capability | Implementation and evidence |
| --- | --- | --- |
| [x] | Validated timestamped observations, throughput, effective capacity, utilization, health, histories and active state | New contracts inherit existing strict `Contract`; `test_invalid_contracts`, `test_invalid_configuration_history_and_snapshots`. |
| [x] | Structured recommendations and machine-readable reasons, separate from deployment authorization | `PolicyDecision`, `Reason`, `Recommendation`; `test_invalid_contracts` rejects authorization, simulation checks every output remains unauthorized. |
| [x] | Separate configurable repair/optimization/recovery thresholds | `PolicyConfig`; `test_configurable_count_and_duration`, `test_known_failure_can_request_repair_with_other_telemetry_missing`. |
| [x] | No single-measurement optimization trigger | Minimum configurable count is two, default three; count AND duration tests and invalid config tests. |
| [x] | Hysteresis and fresh telemetry continuity | `SignalState`, `_signal()`; threshold, gap, cached-sample and missing-sample tests. |
| [x] | Success, rejection, failure, rollback, unconfirmed rollback cooldowns | `complete_intervention()`; `test_all_outcome_cooldowns`, `test_success_cooldown_and_expiration`. |
| [x] | Urgent repairs cannot bypass safety | `_block()`, `assess_proposal()`; urgent exception tests, unsafe-state and frequency tests; simulation at 100s and 545s. |
| [x] | Frequency limits expire | Start-based `frequency_until`; `test_frequency_limit_expires_and_does_not_permanently_block_repairs`. |
| [x] | Use real configuration/action history, never congestion as route-oscillation evidence | `record_configuration_change()`, scoped proposal checks; `test_unexecuted_proposals_do_not_create_configuration_history`, threshold fluctuation and state-switching tests. |
| [x] | Consult policy both before generating and after proposing a change | `evaluate()`, `assess_proposal()`, `start_intervention()`; `test_proposal_stage_requires_recent_opportunity_and_matching_kind`, `test_proposal_expires_when_essential_inner_sample_expires`. |
| [x] | No permanent blacklist of legitimate repairs | Bounded history windows and repair recommendation retry interval; `test_unresolved_repair_retries_without_permanent_latch`, expiration tests. |
| [x] | Persist observations, sustained signals, triggers, deadlines, active ID, outcomes and changes | `PolicyState`, `snapshot()`, `restore()`; snapshot/active/clock-origin tests. |
| [x] | Injected time and safe clock-origin restoration | UTC clock callable, remaining-budget rebasing, audit clock generations; backward/forward restoration and clock-regression tests. |
| [x] | Offline module independent of future experiment architecture | Imports limited to standard library, Pydantic and existing `Contract`; all 78 new tests execute without a lab. No controller/runner is installed or wired in. |
| [x] | Preserve existing verification and rollback safeguards | Existing `pipeline.py`, `trial.py`, `lab.py`, `experiment.py`, shared lock, bounded runner and PR #6 files unchanged. Full offline regression suite passes. |
| [x] | Synthetic repeatable simulation records time, conditions, decisions/reasons, outcomes, cooldowns, safeguards and transitions | `advnet/stability_simulation.py`; four `StabilitySimulationTests` cover deterministic equality, milestone evidence, restorable artifacts and output preservation. |
| [x] | Documentation and defaults explicitly uncalibrated | `STABILITY_POLICY.md`, this checklist and README entry. No scientifically validated thresholds or live improvement claims. |

## Monitoring during inference

| Requirement | Implementation | Offline test |
| --- | --- | --- |
| 1–3. Sustained trigger, ongoing monitoring and slow response | `evaluate` emits one permit. Consistent observations preserve its ID and deadline. Assessment uses the latest sample. | `test_slow_inference_uses_latest_sample_and_one_dispatch` |
| 4. Fixed expiration and identity | Check the deadline at assessment and reservation. Reject a late worker carrying a superseded ID. | `test_absolute_expiry_and_delayed_reply_after_reissue` |
| 5. Congestion clears during inference | Clearing a target retires the opportunity even between assessment and reservation. | `test_clear_during_inference_revokes_assessed_candidate` |
| 6. Material change | Recheck link set, effective capacity, health status, scoped failure identity and change readbacks. | `test_material_change_in_link_scope_or_health_during_inference`, `test_repair_continues_with_same_failed_peers_and_rejects_changed_peers`, `test_configuration_readback_invalidates_before_reservation` |
| 7. Fresh evidence | Unchanged cached evidence ages on its original timestamp. An interruption in required repair telemetry also retires the request. | `test_cached_health_valid_until_it_stales`, `test_repair_sample_gap_retires_request` |
| 8. Duplicate reservations | Token validation and consumption occur under the same instance lock. | `test_concurrent_reservations_consume_token_once` |
| 9. Bounded retry and recovery | Fixed lifetime, minimum dispatch interval, per-kind backoff, explicit closure and conservative restore. | `test_cancelled_or_rejected_inference_retries_only_after_backoff_and_cooldown`, `test_absolute_expiry_and_delayed_reply_after_reissue`, `test_restore_drops_inflight_token_but_allows_bounded_retry` |
| 10–11. Existing safety | An unconfirmed rollback retains the active slot. Fresh telemetry, hysteresis, frequency limits, cooldowns and action-history checks still govern reservations. | Scenarios 5–15 and 18–20 above, plus simulation milestones at 100s, 240s, 545s and 640s. |

## Exact offline verification commands

Executed with the existing project `.venv`; no dependency installation was needed.

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_stability*.py' -v
.venv/bin/python -m advnet.stability_simulation --output /tmp/advnet-stability-inference-verified
RUN_BATFISH_INTEGRATION=0 RUN_TRAFFIC_INTEGRATION=0 RUN_DYNAMIC_TRAFFIC_INTEGRATION=0 .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m py_compile advnet/stability_policy.py advnet/stability_simulation.py tests/test_stability_policy.py tests/test_stability_simulation.py
.venv/bin/python -m compileall -q advnet tests scripts
.venv/bin/python scripts/scenario-suite.py validate
git diff --check
git diff --cached --check
```

The full regression log was retained at `/tmp/advnet-stability-inference-regression.log`.
New tests: **78 discovered, 78 passed, 0 failures/errors, 0 skips**.
Complete suite: **281 discovered, 275 passed, 0 failures/errors, 6 skips**.
The existing main-based suite contributes 203 tests: 197 passed and six skipped.
PR #6's dynamic tests are on its unchanged separate branch, not this main-based
branch. `RUN_DYNAMIC_TRAFFIC_INTEGRATION=0` was still set explicitly as requested.

The six skips are **four disabled live tests** (three Batfish, one traffic) and
**two tests requiring absent optional SDKs** (OpenAI-compatible and TypeSafe).
They are not counted as passes or successful live verification. No additional
live integration flags were found in this branch's tests.

Syntax/compilation checks and the repository's 30-case offline scenario-model
validator pass. No configured Ruff/Flake8/mypy/CI lint checks were found, and
those linters are absent from the existing environment. Browser checks and the
Docker-based Diamond check are unrelated live/UI workflows, not offline static
checks for this policy, and were not invoked. Whitespace checks pass; complete
branch review is limited to the new policy, simulation, their tests and docs,
plus the README link.

## Simulation inspection

The synthetic simulation records **62 evaluations** and passes **20 milestone
checks**. Inspected decision and state output confirms:

- 10s temporary congestion clears at 20s without an intervention.
- 60s sustained congestion permits evaluation; scripted success begins cooldown.
- 100s sustained reachability failure bypasses routine cooldown; a recent reversal
  is blocked while an alternative candidate can be assessed.
- 240s unconfirmed rollback still blocks interventions beyond its 220s deadline.
- 250s explicit rollback confirmation releases the active slot with cooldown.
- 320s a recent reversal is blocked; proposals rejected at 320s and 350s make the
  identical action ineligible at 380s.
- 410s a confirmed pre-mutation failure's cooldown expires; another action is eligible.
- 520s and 545s the frequency limit blocks optimization and urgent repair alike.
- 640s actual configuration-state history blocks an old target; a novel state is eligible.
- 720s a later independent sustained event can trigger again.
- 725s monitoring continues on the original request. At 730s the delayed response uses the latest observation and reserves once.
- 890s another request reaches its fixed deadline despite fresh observations. At 920s a new opportunity opens and the older reply is refused.
- 800s measured throughput with unknown effective capacity is explicitly insufficient.

Observation recommendations: 4 no-intervention, 24 collect, 9 optimization permits,
1 repair permit, 9 cooldown suppressions, 12 frequency suppressions, 2 unsafe-state
holds and 1 insufficient-evidence decision. Scripted outcomes: 3 successes,
2 rejections, 1 failure, 1 unconfirmed rollback and its subsequent confirmed
rollback. Final state: no active intervention, seven intervention records and
five actual-change records, all synthetic. Byte-for-byte equivalent simulation
results are also verified by the automated reproducibility test.

## Known limits and future integration

All verification here is offline. Live capacity provenance, telemetry timing,
threshold calibration, routing behavior and real outcomes still need validation
on the Abilene environment. No live improvement or live safety claim is made.

A future controller must provide measured, correctly scoped telemetry and canonical
action/state fingerprints, synchronize calls across processes, persist state
durably, reconcile unfinished attempts, and retain every existing Batfish,
checkpoint/readback, runtime acceptance and rollback safeguard. Evidence strings
are trusted caller attestations. In-process forward clock jumps are treated as
elapsed time; use a stable clock. Restore preserves remaining safety windows
rather than consuming downtime. Histories are retained and scanned, so long runs
need a storage/archiving plan. The policy does not solve PR #6's lock coordination
or implement any persistent controller or experiment runner.
