# Persistent autonomous controller

The controller keeps sampling after a successful repair or optimization. A policy permit starts one inference request. The controller reserves an intervention only after checking a typed candidate against the current opportunity. Baseline and candidate verification, configuration readback and measured runtime acceptance gate each write and success record.

The offline entry point runs on a laptop. Choose a new output directory.

```bash
.venv/bin/python -m advnet.controller_demo --output /tmp/comp7311-controller-demo
```

## Architecture

```mermaid
flowchart TD
    T[Timestamped telemetry] --> P[StabilityPolicy.evaluate]
    P -->|New opportunity| W[One decision worker]
    W -->|While inference runs| T
    W -->|Candidate and matching opportunity| A[Validate scope and compare checkpoint]
    A --> R[Assess, reserve and persist]
    R --> V[Verify healthy and candidate snapshots]
    V --> D[Recheck checkpoint and deploy]
    D --> M[Read back, converge and compare passive measurements]
    M -->|Accepted| T
    M -->|Failed| B[Restore checkpoint and read back]
    B -->|Confirmed| T
    B -->|Unconfirmed| H[Hold active slot and continue sampling]
```

`PersistentController.run()` owns the state machine. The owner samples on a monotonic schedule and evaluates the actual stability policy. A separately spawned process calls the decision provider. Only the owner handles reservations, verification, writes and recovery. One intervention executes serially. Its verification, convergence and measurement can delay the next sampling tick. Inference allows sampling to continue.

The normal path runs through monitoring, evaluation pending, decision, proposal assessment, verification, deployment, runtime validation and cooldown. The controller also records rollback, recovery, blocked and stopped states. Clock time for scheduling and timeouts is monotonic. Evidence records and policy decisions use aware UTC timestamps.

## Integration interfaces

| Dependency | Contract |
| --- | --- |
| Telemetry | `telemetry(measurement_generation, deadline) -> Frame` returns its source, a `NetworkObservation`, and optional `Observation` for the decision engine. Missing aligned model input closes the opportunity. Collection errors invalidate pending evidence. |
| Policy | Supply a `StabilityPolicy` or use the default. Inject the same UTC clock into a custom policy and controller. The policy owns thresholds, hysteresis, retries, cooldowns, intervention frequency and reversal checks. |
| Decisions | `EngineProvider(llm, screener, catalog, threshold)` calls the existing `DecisionEngine.decide()`. Existing `LLM` and `Jev` adapters are compatible. Providers must be picklable for the production spawn worker. Initialize the controller under a Python main guard. |
| Executor | `VerifiedExecutor(lab, verifier, windows, scope, healthy_configs=...)` accepts the existing `Lab` transport and `ProcessVerifier`, plus a passive window reader and a trusted full healthy checkpoint. Its methods are `prepare`, `execute` and `readback`. |
| Time and cancellation | Inject a clock with `now()`, `monotonic()` and `wait(stop, seconds)`, and an Event-compatible cancellation signal. I/O providers must propagate the supplied deadline to blocking calls. |
| Persistence and recording | `Journal(directory)` implements exclusive ownership, atomic snapshot writes and JSONL events. A replacement journal supplies `root`, `owner()`, `load()` and `save(data)`. Pass `recorder(event)` to replace JSONL recording. |

A coordinator can construct the library directly once its collectors and verifier are configured.

```python
controller = PersistentController(
    Settings(mode="jev_llm", source="live", interval=5, duration=600),
    telemetry=collect_frame,
    decision=EngineProvider(llm, jev, catalog, threshold=0.9),
    executor=VerifiedExecutor(lab, batfish_verifier, read_runtime_window,
                              network_scope, healthy_configs=verified_baseline),
    journal=Journal(output_directory),
    cancel=stop_event,
)
controller.run()
```

The example requires site-supplied `collect_frame` and `read_runtime_window`. The branch supplies the monitor-format conversion, execution adapter, offline providers and demonstration. A turnkey Abilene passive evidence producer and live launcher are outstanding integration work.

### Telemetry

`monitor_frame()` accepts the actual layout of `Monitor.sample()`. Each link produces two directed router/interface identities. Counter Mbps retains the snapshot timestamp. Calculating utilization requires attested effective capacity covering the entire counter interval. Pass the verified interval start and capacity validity range. An absent rate, unknown interval or nominal topology speed provides insufficient evidence. With a manifest, omitted monitored links are represented as unknown.

Supply independent `HealthMeasurement` values for the required host pairs and OSPF peers. A fingerprint must identify their scope and, for an aggregate failure, the affected members. Preserve the original measurement timestamp and generation when caching either check. Container status, offered UDP rates and an empty list of checks cannot establish health. Read the current policy generation at collection start. Reinitialize counter baselines on restart and collect fresh health checks. A caller that cannot establish which generation produced a reading must discard it.

With an Abilene manifest and a complete set of error-free, recent running configurations, the adapter can build a decision observation aligned to the telemetry envelope. The default configuration age limit is 30 seconds. Individual configurations retain their read times. Full readback checks them again before the controller uses a returned proposal. A caller can instead supply an aligned typed model observation directly.

### Inference and retries

LLM-only bypasses Jev. Jev+LLM uses the existing threshold, catalog routing, no-change handling and escalation on uncertainty or error. The worker receives the trigger kind, scoped evidence, opportunity ID and earlier feedback for this episode. Each permit starts at most one worker. Continuing observations retain or retire its token. Polling and proposal assessment use the latest policy state. Cancellation, opportunity invalidation, expiry or shutdown discards a late result. The POSIX worker has a hard timer and the owner kills and joins abandoned workers.

An episode groups attempts by impairment kind, affected signal keys and health scope. A rejection preserves feedback within that episode. An identical rejected patch cannot reenter verification in the same episode. Success, recovery or a new impairment starts a new decision history. The default limit is three model attempts per episode. Exhaustion is recorded and suppresses further calls for the episode. All policy cooldown and frequency rules still apply.

| Controller setting | Default |
| --- | --- |
| Mode and evidence source | Required. `llm_only` or `jev_llm`, and `synthetic` or `live` |
| Sampling interval / run duration | 5 s / 600 s |
| Inference timeout | 45 s, capped by opportunity expiry and remaining run time |
| Operation / rollback budget | 120 s / 180 s |
| Maximum attempts per episode | 3, configurable from 1 through 5 |

### Execution and passive measurement

1. Validate `Proposal`, check device/interface scope and materialize the typed patch using `candidate_configs`. Hash the complete configuration mapping before and after the change. The normalized patch hash identifies repeated actions.
2. Call `assess_proposal()` and `start_intervention()`. Persist the reservation and checkpoint before external verification. Verify both the supplied healthy configuration and the candidate using the complete manifest and existing snapshot format, including path policies.
3. Read the healthy and impaired runtime windows, then re-read the network checkpoint. Drift or missing evidence rejects the proposal. Apply configuration only to affected routers.
4. Require full configuration readback and healthy OSPF convergence. Obtain a passive window starting after convergence. Use the existing `acceptable()` or `acceptable_concurrent()` guardrails, then confirm the final candidate configuration again.
5. On failure after a possible write, use a separate cleanup deadline. A partial deployment can be restored if every router matches either its own checkpoint or its intended candidate configuration. Record each confirmed transition, including partial readback and rollback. An unknown third state or a failed restoration retains the active slot.

`windows(phase, kind, deadline)` returns a validated `RuntimeWindow` for `healthy`, `before` or `after`. All three need matching source, network scope, measurement duration, traffic scope and demand identity. The healthy baseline must match the verified baseline configuration. The before/after windows must match their configuration hashes, satisfy freshness checks and share a demand epoch. The producer must establish workload composition and rates over each whole window and change the epoch on every demand transition, including a change followed by a return to the original rate. A constant experiment ID cannot supply that evidence.

An active-traffic baseline must show positive receiver delivery for every reference flow. Single-flow acceptance uses measured receiver Mbps, loss and maximum link utilization. Concurrent acceptance checks measured sender overlap, matched per-flow profiles and receivers. Traffic-free repair requires explicit confirmed idle evidence. Optimization always requires measured traffic. The controller never calls the traffic generator, iperf, baseline reset or fault injector. Incomparable evidence before deployment causes rejection. Incomparable evidence after deployment causes rollback.

### Persistence and evidence

Reuse the same private output directory when restarting a run. The controller stores a versioned policy snapshot, phase, mutation marker, episode history and binding to network scope, mode and evidence source. Each snapshot uses a temporary file, fsync and atomic replacement. Missing state in an occupied directory, corrupt state or an incompatible binding stops startup. A file lock excludes another owner of the same journal. Live runs also acquire the existing exclusive Abilene experiment lock. Running the existing standalone traffic command simultaneously requires future coordination of lab ownership.

On restart, `StabilityPolicy.restore()` retires an in-flight opportunity and preserves cooldowns and the active slot. An unfinished attempt requires current full readback matching its saved pre-change fingerprint. A confirmed unchanged pre-deployment attempt is recorded as failed. A checkpoint confirmed after a possible write resolves the rollback hold. A running candidate or unknown configuration remains blocked. Monitoring continues. Reconcile the network externally and restart the same journal to retry readback. A persistence failure propagates and leaves a held reservation available for reconciliation.

Each JSONL event has `version`, `at`, `source`, `mode`, `phase` and `event`. Observation rows include the timestamped measurements and full policy decision. Join decision, reservation, transition and outcome records by `opportunity` or `intervention` (inside policy decisions these are `opportunity_id` and `active_intervention_id`). Decision results contain routing, attempt context through the dispatch transition, call stages/models, token/cost accounting where available, and elapsed time. A killed worker may have incurred external usage that is unavailable locally. Proposal rows contain typed changes. Readbacks contain before/after hashes. Runtime results contain scoped measurement summaries, and outcomes include reasons and elapsed intervention time.

The run directory contains `controller.json`, `events.jsonl` and one directory per unique intervention attempt. Checkpoints and verifier snapshots contain configuration text and require private storage. Decision artifacts discard raw provider responses and free-form model prose. The durable policy snapshot is authoritative if a crash interrupts appending a timeline event.

## Offline results and live validation

The reproducible demo ran 100 sampling ticks. A single spike caused no inference. The first sustained impairment succeeded at 14.25 synthetic seconds. Sampling continued through cooldown. Independent later episodes produced verifier rejection at 34 s, confirmed rollback at 54.25 s and unconfirmed rollback at 74.25 s. The final 25 observations remained blocked. The four recorded configuration transitions reflect simulated readbacks. These data demonstrate controller behavior only.

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_persistent_controller*.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_stability*.py' -v
RUN_BATFISH_INTEGRATION=0 RUN_TRAFFIC_INTEGRATION=0 RUN_DYNAMIC_TRAFFIC_INTEGRATION=0 .venv/bin/python -m unittest discover -s tests -p 'test_dynamic*.py' -v
RUN_BATFISH_INTEGRATION=0 RUN_TRAFFIC_INTEGRATION=0 RUN_DYNAMIC_TRAFFIC_INTEGRATION=0 .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -W error::SyntaxWarning -m compileall -q -f advnet tests scripts
.venv/bin/python scripts/scenario-suite.py validate
git diff --check
```

The complete offline suite ran 461 tests with 454 passes and seven skips. The controller subset passed all 41 tests, the stability subset passed all 89, and the dynamic subset passed 127 with one live skip. Five full-suite skips require live integrations and two require optional SDKs. Compilation and all 30 offline scenario validations passed.

The controller tests exercise the real policy and execution state machine, process isolation, cancellation, duplicate suppression, configuration drift, partial writes, failed storage, shutdown and restart. Batfish verdicts, router operations, SDK responses and traffic measurements in those tests are synthetic.

Nick's live validation should establish collector latency and timestamp alignment, effective shaping capacity over each sample window, workload epoch provenance and comparable receiver windows. Check full EOS readback normalization against materialized candidates, the real Batfish/path-policy result, convergence time, checkpoint restoration after a partial write and restart with an uncertain configuration. Strict readback equality can conservatively block when EOS reformats a configuration. Blocking I/O collectors and transports must honor their deadlines. Validate exclusive lab ownership before combining the controller and traffic scheduler in a future phase.

The branch starts at `19ca772006b654ffa1ad5ea65829716b430daaba`, the main merge of PR #7. Its ancestry already includes PR #6 and stability-policy commit `9483fd6`. It can be reviewed directly against main.
