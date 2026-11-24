# Repeatable dynamic traffic experiments

The dynamic traffic runner changes offered UDP demand independently of controller
decisions. It uses the existing Abilene host inventory, receivers, host image,
receiver-statistics parser, and four-client resource limit. It provides workload
validation, deterministic generation, scheduling, optional live execution, exact
intended-schedule replay, and saved evidence. It makes no router configuration,
OSPF, capacity, shaping, inference, or Batfish changes.

Run commands from the repository root. Offline commands require the project's
Python environment with Pydantic. Live commands additionally require Nick's
already-running Abilene network, Docker access, the local `advnet-host:0.1` image,
and preexisting receivers. The runner never deploys Abilene, builds/pulls images,
or creates/replaces receivers. Live execution requires `--allow-live`.

## Resolve and inspect a plan offline

```bash
.venv/bin/python -m advnet.dynamic_traffic resolve \
  --workload experiments/dynamic/seeded.json \
  --manifest topologies/abilene.json \
  --output runs/dynamic-plan.json
```

This performs no subprocess, network, Docker, or inference operations. It writes
a validated resolved schedule with explicit ordered events, including the seed.
The output file must be new. Inspect it before sending traffic.

Input is one of three strict JSON schemas: an explicit `DynamicWorkload` with
`flows`, a `WorkloadGenerator` identified by `flow_count`, or a `ResolvedWorkload`
with `events`. Mixed schemas, extra measurement fields, duplicate JSON keys,
nonfinite numbers, unknown hosts, invalid ordering, unsupported overlap, and
receiver-port conflicts are rejected. Configuration schema details and burst
semantics are in [TRAFFIC_PERFORMANCE.md](TRAFFIC_PERFORMANCE.md#dynamic-workload-definitions-offline-only).

The same seed, settings, and supported Python RNG behavior reproduce the same
generated flows. Saving a resolved schedule also supports exact intended replay
without a seed. Replay never generates new random values. Schedule hashes use
canonical JSON with sorted object keys and preserved event/flow list order;
raw input hashes separately preserve the original bytes and formatting.

## Example demand profiles

| Profile | Intended behavior |
| --- | --- |
| `experiments/dynamic/light.json` | Two low-rate arrivals/departures over 60 seconds, at 1 and 2 Mbps. |
| `experiments/dynamic/moderate.json` | Three overlapping flows over 90 seconds, with a sustained 10-to-20 Mbps change. |
| `experiments/dynamic/heavy.json` | Four staggered clients at 50–80 Mbps over 90 seconds. |
| `experiments/dynamic/burst.json` | A temporary 5-to-80 Mbps burst with background traffic over 75 seconds. |
| `experiments/dynamic/sustained.json` | Sustained 5-to-30-to-15 Mbps changes with a background arrival/departure. |
| `experiments/dynamic/seeded.json` | Six generated arrivals, fixed seed 7311, randomized endpoints and 2–20 Mbps rates. |

Light/moderate/heavy describe requested demand, not verified congestion or
measured throughput. Offered rates do not configure link capacities. These
profiles are outside the frozen fault benchmark. Long intervals allow for
Docker overhead but do not guarantee precise live packet timing.

## Nick: run an optional live experiment

After obtaining the reviewed branch, use the environment that already hosts
Abilene. Confirm the existing lab/image and the receivers required by the plan;
these commands only read state:

```bash
sudo docker ps --format '{{.Names}}'
sudo docker image inspect advnet-host:0.1 --format '{{.Id}}'
sudo docker exec clab-advnet-abilene-h-sva ss -H -4 -lnt 'sport = :5201'
sudo docker exec clab-advnet-abilene-h-nyc ss -H -4 -lnt 'sport = :5201'
```

The light profile uses those two receivers. A missing/busy receiver produces a
failure; the runner does not repair or replace it. Alternate destination ports
require separately prepared receivers. Run while cooperating existing collectors
and repair trials are idle: the runner shares `runs/.abilene.lock` with them.
It cannot exclude an unrelated client that ignores that lock.

First resolve a light profile offline, then replay exactly that schedule live:

```bash
.venv/bin/python -m advnet.dynamic_traffic resolve \
  --workload experiments/dynamic/light.json \
  --manifest topologies/abilene.json \
  --output runs/light-plan.json

sudo .venv/bin/python -m advnet.dynamic_traffic replay \
  --schedule runs/light-plan.json \
  --manifest topologies/abilene.json \
  --output runs/dynamic-light-01 --allow-live \
  --operation-timeout-seconds 30 --cleanup-timeout-seconds 30 \
  --poll-interval-seconds 0.5
```

Every output directory must be new; existing artifacts are never overwritten.
These 30-second budgets are explicit integration allowances for Docker control
operations and cleanup. They do not change repair acceptance thresholds. The
default operation budget is 5 seconds, cleanup 10 seconds, and polling 0.1 seconds.
Frequent Docker inspections add overhead; inspect recorded lateness rather than
assuming exact event execution.

To resolve and run a profile in one command instead:

```bash
sudo .venv/bin/python -m advnet.dynamic_traffic run \
  --workload experiments/dynamic/burst.json \
  --manifest topologies/abilene.json \
  --output runs/dynamic-burst-01 --allow-live \
  --operation-timeout-seconds 30 --cleanup-timeout-seconds 30
```

To repeat an experiment's exact intended schedule and saved inventory:

```bash
sudo .venv/bin/python -m advnet.dynamic_traffic replay \
  --schedule runs/dynamic-burst-01/resolved-workload.json \
  --manifest runs/dynamic-burst-01/manifest.json \
  --output runs/dynamic-burst-02 --allow-live \
  --operation-timeout-seconds 30 --cleanup-timeout-seconds 30
```

Compare `resolved_schedule_sha256` across runs. Matching hashes establish
identical intended events, not identical actual timing, network state, or
performance. Also compare manifest/code hashes, settings, runtime versions,
measurement availability, and actual event timing. The runtime image tag alone
does not establish an identical image binary; retain its Docker image ID and
iperf3 version when collecting research evidence.

## Saved artifacts

| File | Contents |
| --- | --- |
| `input-workload.json` | Exact original input bytes: definition, generator settings, or saved resolved schedule. |
| `manifest.json` | Exact input inventory bytes. |
| `resolved-workload.json` | Validated explicit intended event schedule and seed. |
| `execution.json` | Scheduler attempts, intended/actual times, errors, cleanup, raw iperf3 output, client segments, and restart records. |
| `flow-lifecycle.jsonl` | Observed creation, launch, stop, removal, rate-change events, and final client-state summaries. |
| `measurements.json` | Existing per-segment receiver metrics with units, status indicators, and measurement errors. |
| `evidence.json` | Experiment ID, execution status, seed, hashes, scheduler settings, clock origin/UTC anchor, runtime, cleanup, and errors. |

Input snapshots and initial evidence are written before clients launch. The
clock origin is published before the first event. Output files are replaced
atomically within the new run directory. Execution, measurement, and lifecycle
files are finalized after cleanup, including on ordinary errors, Ctrl+C, and
`SystemExit`. The evidence manifest hashes all other artifacts without a circular
self-hash. Code hashes describe source files on disk at run preparation; avoid
editing source during a run. They do not hash Python bytecode, Docker image
contents, host configuration, or the live router state.

An I/O failure exits unsuccessfully; it may leave an incomplete artifact directory.
If saving evidence also fails after an execution exception, the original error
is preserved with a note. `SIGKILL`, machine failure, or power loss can prevent
final artifacts/cleanup. A remaining `status="running"` means unfinalized, not
success. Inspect any owned client IDs or unconfirmed create errors retained in
available evidence. Never use broad process-name kills or container pruning.

Exit codes are 0 for resolution or completed scheduler execution, 1 for ordinary
execution/input failures, 2 for command usage errors, and 130 for Ctrl+C. A
propagated `SystemExit` retains its own code. Always inspect cleanup and
`throughput_status`: completed control execution can still have unavailable
receiver measurements. Partial or missing data is not a successful performance
measurement.

## Timeline and interpretation

All intended/actual event, client lifecycle, and restart times are seconds
relative to `timeline.started_monotonic_seconds`. The absolute monotonic origin
has meaning on the same host and boot. `evidence.json` publishes the experiment
ID, clock origin, and a UTC timestamp sampled between two monotonic readings.
A future component can save its own records separately with that ID and compute
`at_seconds = monotonic_now - started_monotonic_seconds`. The UTC sample provides
an approximate wall-clock anchor; it does not synchronize clocks across machines.

Traffic artifacts have `scope="traffic_generation"` and contain no controller
decisions or inference-cost estimates. A future controller/network observer can
align its separate events with this timeline. Controller integration and any
coordination changes to the current exclusive lab lock are separate workstreams.

Lifecycle timestamps describe observations of control operations, not packet
arrival times. Final state summaries have `at_seconds=null` and unavailable
timing rather than an invented transition time. Same-time lifecycle observations
retain stable file ordering; the authoritative intended event order is the
resolved schedule's `sequence` field.

Rate changes and burst boundaries restart owned clients. `restart_control_seconds`
records the control interval; `minimum_no_client_seconds` is a verified minimum
interval without an owned client. The exact packet gap remains
`traffic_gap_seconds=null`, with unverified/incomplete status. Continuous packet
transmission is not claimed.

Receiver Mbps, loss fraction, and jitter in ms reuse existing metrics. Missing or
invalid values/statuses remain explicit; sender/offered rates never replace
receiver measurements. There is no aggregate inferred across differently timed
segments, and no new loaded-RTT, periodic router-counter, or utilization sampling.
This runner does not alter the existing performance comparison tools or apply
repair acceptance checks to changing workloads.

## Offline and optional live tests

Run the complete suite without network operations or paid inference:

```bash
RUN_BATFISH_INTEGRATION=0 RUN_TRAFFIC_INTEGRATION=0 \
RUN_DYNAMIC_TRAFFIC_INTEGRATION=0 \
  .venv/bin/python -m unittest discover -s tests -v
```

Offline replay tests compare intended events and canonical hashes, forbid RNG
use during replay, and execute the real scheduler/transport against fake clocks
and mocked subprocesses. They cover failure evidence, cleanup, missing data,
output preservation, and CLI behavior. Passing them establishes offline logic
correctness, not live network throughput, congestion, or optimization gains.

On Nick's already-running Abilene environment only, optionally run:

```bash
sudo env RUN_DYNAMIC_TRAFFIC_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m unittest discover -s tests -p test_dynamic_live.py -v
```

The flag is absent/disabled by default. With it enabled, missing Docker, image,
receivers, or measurement orchestration fail the test rather than silently
passing. The test uses two low-rate flows for 50 seconds, with a sustained change
and a burst; it never starts routers, shapes links, modifies routes, or calls an
LLM. It checks exact intended replay, completed commands, and removal of the
owned clients. It allows missing receiver statistics and therefore does not
establish traffic performance. Evidence, version output, CLI logs, and cleanup
checks are retained under `runs/dynamic-live-<timestamp>-<id>/`; set
`ADVNET_TEST_OUTPUT_DIR` to choose another directory. Preserve these ignored
artifacts separately for the research report. This optional live test has not
been executed as part of the offline implementation.
