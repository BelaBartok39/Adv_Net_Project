# Abilene fault benchmark v1

The suite has 30 recoverable faults and a frozen 11-action host-interface catalog.
Its purpose is to measure both shortcut savings and the overhead/failures of
screening before escalation. The models receive only live observations and a
neutral objective. Fault labels, routing expectations, and reference repairs are
private harness data, saved for evaluation but never passed into either model.

## Coverage

| Family | Cases | Expected first route | What it tests |
|---|---:|---|---|
| Single host interface disabled | 11 | Catalog | Same routine fault at every site; avoid Seattle-only bias |
| One backbone interface disabled | 4 | Escalate | Alternate routing may preserve pings, but an expected OSPF adjacency is missing |
| One-sided OSPF area mismatch | 4 | Escalate | Configuration diagnosis beyond the host-only catalog |
| Two or three hosts disabled | 3 | Escalate | One catalog action cannot repair the whole fault |
| Host fault plus local/remote routing fault | 3 | Escalate | Resist a tempting but incomplete routine repair |
| Router isolated by all transit interfaces | 2 | Escalate | Coordinated multi-interface recovery |
| Direct-link congestion | 3 | Escalate | Connectivity is intact; reroute a 5 Mbps UDP flow away from a 2 Mbps link |

Congestion cases use 12-second receiver measurements, 1200-byte datagrams, and
OSPF cost adjustments. They require throughput/loss preservation, an improvement
in maximum sampled utilization, full connectivity, and healthy OSPF. Existing
single-flow measurements also attempt loaded ICMP RTT sampling, separate from
idle RTT; see [timing, failure handling, and limitations](ABILENE_BASELINE.md#4-additional-scenarios-and-comparisons).
Loaded RTT may be unavailable when overlap cannot be verified and does not change
benchmark acceptance. These are not application-latency or historical Abilene
propagation-delay measurements. All fault types fit the existing bounded repair language.
ACLs, BGP, process crashes, physical hardware faults, and healthy/no-fault controls
are outside this first suite.

“Escalate” means the current catalog cannot fix the entire case in one accepted
change. It does not imply every case requires deep reasoning: backbone shutdown
is simple but outside this catalog, while compound cases require broader
diagnosis. A model can still choose incorrectly. Such errors must remain visible
in results rather than being corrected by secret scenario labels.

## Inspect and validate

The index is `scenarios/benchmark-v1/suite.json`. Profiles are `all`, `routine`,
`escalation`, and a six-case `smoke` subset.

```bash
.venv/bin/python scripts/scenario-suite.py list --profile smoke
.venv/bin/python scripts/scenario-suite.py validate \
  --output captures/scenario-suite-validation.json
```

Validation checks typed changes, inventory, traffic parameters, catalog
fingerprint, and a deliberately small offline admin/OSPF/cost model. Every
impairment must be present, every reference must repair it, and each routing
label must match exhaustive testing of the available catalog actions. This is
an offline proof under the model assumptions, not a live success measurement.

With Batfish running, additionally verify representative impaired/reference
snapshots (no Docker writes or hosted inference):

```bash
.venv/bin/python scripts/scenario-suite.py validate --profile smoke --batfish \
  --output captures/scenario-suite-batfish-smoke.json
```

An impaired snapshot can correctly pass Batfish when hosts remain reachable.
The live controller additionally requires every expected OSPF adjacency to be
FULL. Batfish does not establish congestion performance. Successful source
snapshot checks do not prove that fault injection, convergence, shaping, or
models work for every case on the live lab; run the smoke subset before scaling.

## Run a matched pair

Start the visualizer in a separate terminal as usual. Export `OPEN_ROUTER_KEY`,
`LLM_MODEL`, `SYSONE_MODEL`, and `BATFISH_HOST`. Pick a scenario:

```bash
SCENARIO=scenarios/benchmark-v1/routine-host-sea-down.json
```

LLM-only baseline:

```bash
sudo --preserve-env=OPEN_ROUTER_KEY,LLM_MODEL,SYSONE_MODEL,BATFISH_HOST \
  .venv/bin/python -m advnet.experiment run \
  --scenario "$SCENARIO" --system-one off --reset-baseline \
  --attempts 3 --seconds 900
```

Jev-assisted arm:

```bash
sudo --preserve-env=OPEN_ROUTER_KEY,LLM_MODEL,SYSONE_MODEL,BATFISH_HOST \
  .venv/bin/python -m advnet.experiment run \
  --scenario "$SCENARIO" --system-one jev --reset-baseline \
  --attempts 3 --seconds 900
```

For escalation, repeat with `SCENARIO=scenarios/benchmark-v1/ospf-area-hou-atl.json`
and then `SCENARIO=scenarios/benchmark-v1/compound-sea-host-and-backbone.json`.
After those work, try `SCENARIO=scenarios/benchmark-v1/congestion-sea-sva.json`.

`--reset-baseline` deliberately restores all routers from `labs/configs/abilene/`
and normalizes link shaping to 100 Mbps before healthy preflight. It removes
previous cost/area changes and is recorded separately from fault-to-acceptance
timing. Reset occurs while holding the experiment lock. Missing source files
fail before trial execution; a failed reset stops injection and is reported as
incomplete cleanup. Keep the same source/config/catalog hashes across comparisons.
Successful repair configurations can remain afterward; reset before every arm.

## Guaranteed escalation control

To measure screening overhead without relying on the classifier choosing to
escalate, rerun a case with:

```bash
sudo --preserve-env=OPEN_ROUTER_KEY,LLM_MODEL,SYSONE_MODEL,BATFISH_HOST \
  .venv/bin/python -m advnet.experiment run \
  --scenario "$SCENARIO" --system-one jev --force-escalation \
  --reset-baseline --attempts 3 --seconds 900
```

This actually calls Jev, retains its raw choice, and then forces the first LLM
call even if Jev selected a catalog repair. Jev errors/timeouts also fall back.
After a rejected attempt, subsequent decisions go directly to the LLM as usual.
Every verification/runtime guard still applies. These controlled trials are
explicitly flagged and excluded from routing-accuracy fields; do not mix them
into natural escalation rates or claim they measure classifier correctness.
Network/rate-limit failures can still prevent a request from completing.

## Reporting

Results and CSV include family, expected and actual first routes, classifier
choice correctness, effective routing correctness, unnecessary escalation,
incorrect catalog choices, false no-change claims, screening errors, model calls,
usage, costs, and repair success/time. Errors, timeouts, retries, Batfish
rejections, runtime rejections, and failed trials stay in the dataset. Selection
correctness is separate from runtime success; a later LLM repair does not erase
an incorrect initial Jev decision.

```bash
mkdir -p captures
.venv/bin/python scripts/summarize-trials.py runs > captures/comparison.csv
```

Repeat each case in both modes, counterbalance which mode runs first, and keep
model versions, threshold, prompt/catalog, attempt/time budgets, observer settings,
and traffic fixed. Start with five matched repetitions per case; report trial
counts, success rates, median/tail repair times, inference time, escalation rate,
and cost by family. Increase repetitions before interpreting small differences.
Timeouts are failures/censored outcomes, not missing fast successes.

Do not equate this suite's 11:19 case count with a real workload distribution.
Report stratified results, then explicit weighted mixes such as 25%, 50%, 75%,
and 90% routine cases. The expected time benefit depends on routine savings
minus escalation overhead; both routing mistakes and model/service variability
affect it. Free Nemotron billing can make Jev more expensive in dollars despite
lower latency. Use reported charges, or clearly separate any priced-model
estimate from observed charges. Healthy controls and a latency-under-load suite
can be added later without changing the frozen fault benchmark.

Regenerate definitions with `python scripts/generate-scenario-suite.py` only when
intentionally changing the benchmark. This generator reads public inventory and
baseline configs, never results. Catalog changes invalidate routing labels and
are rejected until the suite is regenerated/versioned.
