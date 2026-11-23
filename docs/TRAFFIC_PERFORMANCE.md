# Traffic collection and performance evidence

The existing UDP generator now records receiver jitter and single-flow loaded
ICMP RTT, supports bounded concurrent UDP batches, and saves descriptive stage
comparisons. These additions reuse the existing host image, data-IP bindings,
1200-byte datagrams, receiver reporting, and link counters. They do not change
the original single-flow acceptance thresholds or the frozen 30-case benchmark.

Run commands from the repository root. Abilene commands below require an
already-running lab on a machine able to host all 11 cEOS routers. They are not
instructions to start Abilene on a smaller computer.

## Collect traffic without inference or configuration changes

With host receivers running, collect one existing-style flow:

```bash
sudo .venv/bin/python -m advnet.experiment traffic \
  --traffic-profile experiments/single-traffic.json \
  --output runs/traffic-single
```

Collect two concurrent flows:

```bash
sudo .venv/bin/python -m advnet.experiment traffic \
  --traffic-profile experiments/concurrent-traffic.json \
  --output runs/traffic-concurrent
```

Outputs must be new directories. Each contains the normalized profile, manifest,
`measurements.json`, and `evidence.json` with input/code hashes and collection
status. Raw iperf output and ping samples are retained in measurements. No API
key is required. This action performs no shaping, routing writes, model calls,
Batfish queries, or repair acceptance checks. It does not run the 110-pair
reachability/OSPF checks, and its idle RTT is explicitly unknown.

To include those checks and live configurations in a decision observation, use:

```bash
sudo .venv/bin/python -m advnet.experiment collect \
  --traffic-profile experiments/concurrent-traffic.json \
  --output runs/traffic-observation.json
```

Profiles contain exactly one `flow` object or one `flows` array. Endpoints use
router/attached-host inventory names such as `sea` and `sva`. A batch supports
2–4 clients, each with source, destination, offered Mbps, duration, and optional
destination port. Four is a resource guard, not the network's flow capacity.
The default receiver port is 5201; shared destination/port pairs are rejected.
Alternate ports require separate preexisting receivers. The collector never
replaces receiver daemons. It removes only its owned temporary client sidecars.

## Run a concurrent optimization trial

On Nick's running Abilene lab, with the existing model credentials configured:

```bash
sudo --preserve-env=OPEN_ROUTER_KEY,LLM_MODEL,LLM_BASE_URL,BATFISH_HOST \
  .venv/bin/python -m advnet.experiment run \
  --scenario scenarios/concurrent-sea-sva-congestion.json \
  --system-one off --reset-baseline --attempts 3 --seconds 900
```

This command deliberately resets baseline configs/shaping, reduces sea–sva
capacity, sends both flows at each stage, and may deploy verified changes. It
uses the normal inference provider. Do not use it for a measurement-only test.
`--system-one jev` remains available with the existing System One settings.
The example is outside `scenarios/benchmark-v1`; it does not alter suite counts,
catalog labels, or the existing congestion scenarios.

Batch trials require verified sender overlap in healthy and impaired
measurements. Candidate acceptance checks matching endpoint/port identities and
unchanged offered loads/durations, then applies the original `acceptable()`
thresholds to every flow: at least 90% of healthy receiver throughput and loss
within two percentage points of healthy. Shared maximum utilization must improve
by at least 5% versus impaired for optimization. Existing reachability, OSPF,
idle RTT, Batfish, deployment, and rollback checks still apply. Aggregate goodput
cannot hide degradation of one flow. Jitter and loaded RTT remain reporting
metrics and introduce no new numeric acceptance thresholds.

## Inspect and reproduce reports

Each trial automatically writes `performance-comparison.json` and records its
status in `result.json.performance_report`. Reporting errors are recorded
separately from repair acceptance; they cannot turn a failed repair into a
success or invalidate an otherwise accepted repair. Reports preserve every
candidate attempt and select a final post stage only from the trial's recorded
successful accepted attempt. Reset measurements are not optimization results.

Healthy, impaired, and candidate measurement files remain the raw evidence.
Automatic reports hash those inputs and the saved manifest/scenario. They use
the in-memory final outcome before `result.json` is written, avoiding a circular
result/report hash. Regenerate a separate report from completed artifacts with:

```bash
.venv/bin/python scripts/compare-performance.py runs/EXISTING-TRIAL \
  --output runs/EXISTING-TRIAL/performance-comparison-reviewed.json
```

See [measurement units, timing bounds, and comparison formulas](ABILENE_BASELINE.md#4-additional-scenarios-and-comparisons).
Throughput is receiver Mbps, jitter and RTT are ms, and loss/utilization are
fractions. Aggregate batch throughput uses total received bytes divided by the
whole counter window, not summed per-flow rates from differing durations. Loss
uses packet-weighted totals. Missing/invalid data remains unknown; percentage
change at a zero baseline is undefined. Capacity denominators and incomplete
workload metadata must be considered when interpreting utilization changes.

## Validation levels

Offline regression and all 30 benchmark definitions, with no network writes or
inference:

```bash
RUN_BATFISH_INTEGRATION=0 RUN_TRAFFIC_INTEGRATION=0 \
  .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/scenario-suite.py validate \
  --output captures/traffic-benchmark-validation.json
```

The existing `RUN_BATFISH_INTEGRATION=1` tests exercise Batfish modeling. They do
not measure traffic performance. New workflow tests use explicitly synthetic
observations and decisions, exercising the real controller without router writes.

The optional live test below uses two temporary Linux hosts on a private Docker
bridge and two owned clients (each limited to 128 MiB); it never starts cEOS or
Abilene, shapes traffic, changes routes, or invokes models. Docker access and the
repository host image are required. Build that image once if missing:

```bash
sudo docker build -t advnet-host:0.1 labs/hosts
sudo env RUN_TRAFFIC_INTEGRATION=1 PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m unittest discover -s tests -p test_traffic_live.py -v
```

With the flag absent the test is skipped. With it set, missing Docker/image or
measurement failures fail the test rather than being counted as success. Evidence
is saved under `runs/traffic-linux-live-<timestamp>-<id>/`; set
`ADVNET_TEST_OUTPUT_DIR` to choose another location. This retains actual CLI
outputs, raw measurements, versions, checks, and cleanup results. Preserve these
ignored artifacts separately for the research report. Both live-test hosts and
their private network are removed, leaving preexisting containers alone.

## Dynamic workload definitions (offline only)

`advnet.dynamic_workload` defines changing demand independently of the controller.
These models provide configuration validation and schedule resolution. The
offline scheduler is described below; a dynamic traffic CLI is still future
work. Existing static traffic collectors, metrics, comparison reports, bounded
campaigns, deployment
thresholds, and frozen benchmark are unchanged.

The definitions reuse `Contract`, `MAX_CONCURRENT_FLOWS`, and
`validate_flow_endpoints`. Later execution can reuse the `Lab` host inventory,
`udp_metrics`, `loaded_rtt`, `Lab.counters`, and `Lab._link_utilization`, plus the
owned-client pattern in `run_concurrent_traffic`. `Lab.traffic` and
`run_concurrent_traffic` finish a bounded measurement before returning; dynamic
arrivals and rate changes use the separate scheduler described below.
The existing `compare_performance`, `compare_trial_artifacts`, `Trial`, and
bounded experiment runner retain their current roles.

A `DynamicWorkload` has `duration_seconds`, optional integer `seed`, schema
version `"1"`, and a nonempty `flows` list. Each flow has a unique `flow_id`,
`source`, `destination`, `destination_port` (default 5201), `offered_mbps`,
`start_seconds`, `stop_seconds`, and optional `rate_changes` and `bursts` lists.
Endpoints use the existing manifest router keys (`sea`, `sva`, etc.) to identify
their attached hosts. All times, including rate changes and bursts, are absolute
seconds relative to experiment start; fractional seconds are preserved.

For example, this configuration describes an arrival, a sustained rate change,
a temporary burst, and a departure:

```python
import json
from pathlib import Path
from advnet.dynamic_workload import ResolvedWorkload, resolve_workload

manifest = json.loads(Path('topologies/abilene.json').read_text())
definition = {
    'duration_seconds': 30,
    'flows': [{
        'flow_id': 'sea-sva', 'source': 'sea', 'destination': 'sva',
        'offered_mbps': 5, 'start_seconds': 2, 'stop_seconds': 25,
        'rate_changes': [{'at_seconds': 8, 'offered_mbps': 10}],
        'bursts': [{'start_seconds': 12, 'stop_seconds': 16, 'offered_mbps': 40}],
    }],
}
resolved = resolve_workload(definition, manifest)
Path('/tmp/resolved-workload.json').write_text(resolved.model_dump_json(indent=2))
loaded = ResolvedWorkload.model_validate_json(
    Path('/tmp/resolved-workload.json').read_text()).validate_inventory(manifest)
assert loaded.events == resolved.events
```

This produces intended events at 2 (start at 5 Mbps), 8 (10 Mbps), 12 (40 Mbps),
16 (restore 10 Mbps), and 25 seconds (stop). `offered_mbps` always means requested
sender load; it is never a claim about measured receiver throughput. Neither
the definition nor the resolved schedule accepts measurement or execution-result
fields. The resolved artifact retains flow definitions, the seed, and explicit
events with contiguous `sequence` numbers; inconsistent events are rejected
when parsed. Schema parsing checks structure and timeline semantics; use
`resolve_workload` for definitions and `validate_inventory(manifest)` after
loading resolved artifacts to check hosts against the intended inventory.

Lifetimes are half-open `[start_seconds, stop_seconds)`. At most four flows may
be active, reusing `MAX_CONCURRENT_FLOWS`; the total number of sequential flows
can exceed four. Overlapping flows cannot share a physical destination host/port.
At equal timestamps, departures precede rate changes, which precede arrivals;
input flow order breaks ties within each category. A port can be reused exactly
at a scheduled departure. These are intended lifetimes; future execution must
confirm cleanup before reuse. Alternate ports still require separate receivers,
whose existence cannot be checked offline.

All offered rates must be finite and in `(0, 1000]` Mbps, matching the existing
traffic profiles. Experiment duration is positive and at most 86400 seconds,
matching the existing deadline ceiling. Flow start must precede stop and lie
within that duration. Rate changes must be strictly ordered inside the flow
lifetime. Bursts must be ordered, non-overlapping, and inside the flow lifetime;
touching bursts are allowed. A burst's absolute offered rate must exceed the
underlying sustained rate throughout its window. Sustained changes during a
burst update the rate restored at its end. At coincident boundaries only the
effective rate is emitted, and no-op updates are omitted; the original changes
remain in the saved definition. A burst can begin at flow start or end at flow
stop without creating redundant events.

`generate_workload(specification, manifest)` accepts a `WorkloadGenerator` or
dictionary with `duration_seconds`, optional `seed`, `hosts`, `flow_count`,
`arrival_interval_seconds`, `flow_duration_seconds`, `min_offered_mbps`,
`max_offered_mbps`, and optional `destination_port`. It creates arrivals at
`i * arrival_interval_seconds`, starting at zero, with the specified lifetime.
For each arrival it samples a distinct source/destination pair from available
receiver endpoints, then a uniform offered rate between the inclusive bounds.
Candidates are sorted and randomness uses a local `random.Random(seed)`;
global RNG state and manifest insertion order do not affect the result. Generated
flows can subsequently include explicit rate changes and bursts. Invalid bounds,
unknown hosts, insufficient free receivers, more than four active clients, or
an arrival whose stop exceeds the duration raise errors without moving arrivals
or reducing demand. The generator does not retry random draws to hide conflicts.

Save the explicit resolved schedule for exact replay, including when no seed is
supplied. Seeded regeneration assumes the same inputs and Python RNG behavior;
a saved schedule requires no random draws. This phase's JSON round trips validate
intended schedules only. The scheduler tests below exercise control-call timing
and cleanup contracts with a fake backend. Process cleanup, receiver availability,
and live network performance require later transport integration.

Run the model tests without Docker, Batfish, clocks, or API calls:

```bash
.venv/bin/python -m unittest discover -s tests -p test_dynamic_workload.py -v
```

## Dynamic event scheduler (offline backend)

`advnet.dynamic_scheduler.run_schedule` accepts a resolved schedule, the target
manifest, and an injected `TrafficBackend`. It reparses the schedule (including
model instances) and validates inventory before touching the backend. It uses
`time.monotonic()` through an injectable `Clock` with `monotonic()` and
`sleep(seconds)` methods. Unit tests advance fake time without real sleeping.
There is no Docker or iperf3 backend in this phase, and the existing traffic CLI
does not execute dynamic schedules yet.

Backend methods have the following contract:

| Method | Required behavior |
| --- | --- |
| `start(flow, offered_mbps, *, timeout_seconds)` | Register owned resources before allocation and return after initiating traffic, without waiting for the flow's lifetime. |
| `set_rate(flow_id, offered_mbps, *, timeout_seconds)` | Apply the effective requested rate while preserving the flow identity. |
| `stop(flow_id, *, timeout_seconds)` | Stop the flow and release its receiver endpoint before returning, allowing ordered port reuse. |
| `poll(flow_id, *, timeout_seconds)` | Inspect current state without waiting for completion; return `None` while running or a `FlowExit` on exit. |
| `cleanup(*, timeout_seconds)` | Attempt cleanup of every resource owned by this run, including partial starts; raise if cleanup fails. |

Backend control calls must be short and respect their timeouts. The scheduler
does not wait for a client's full traffic duration or use blocking completion
collection. It polls active clients while waiting for the next event, so one
flow can remain active when another arrives. Control calls execute synchronously
in canonical schedule order; their latency can delay later calls. The scheduler
records this delay and detects overruns after calls return. It cannot forcibly
interrupt a backend that ignores its timeout. A future transport must bound its
subprocess operations rather than relying on the scheduler as a hard watchdog.

The backend should be dedicated to one run and should retain ownership records
until cleanup is confirmed. A failed start may already have allocated resources;
cleanup cannot depend only on successful starts. Receiver/process ownership and
rate-change restart gaps remain requirements for the later iperf3 integration.

Given `resolved` and `manifest` from the example above, and a backend implementing
that contract:

```python
from threading import Event
from advnet.dynamic_scheduler import ScheduleInterrupted, run_schedule

cancellation = Event()  # Another component may call cancellation.set().
try:
    result = run_schedule(resolved, manifest, backend,
                          cancelled=cancellation.is_set,
                          operation_timeout_seconds=5,
                          cleanup_timeout_seconds=10,
                          poll_interval_seconds=0.1)
except ScheduleInterrupted as interrupted:
    result = interrupted.result  # Partial evidence is available after cleanup.
    raise
```

`ScheduleResult` is separate from workload configuration and contains intended
events, actual attempt times, completion times, lateness, per-event status and
errors, unexpected client exits, and cleanup results. All timestamps are seconds
relative to the same experiment start. These times describe backend control
calls; they do not establish when packets actually started or stopped. Missing
timing after clock failure remains `None`. Lateness is nonnegative, with tiny
floating-point differences at timestamp boundaries clamped to zero. Absolute
monotonic deadlines avoid repeatedly sleeping for unrepresentable residuals at
decimal relative times such as 0.1 seconds.

Departures, rate updates, and arrivals retain their resolved event order,
including when a call is late. Later intended timestamps are never shifted to
hide delays. A start or rate update whose flow lifetime has already elapsed
aborts the run instead of generating expired demand. The scheduler also waits
through an idle tail until the declared experiment duration, unless cancelled,
interrupted, or timed out.

The optional `timeout_seconds` is an exclusive budget from experiment start;
no events are dispatched at or after its expiry. Its default is experiment
duration plus `operation_timeout_seconds` (5 seconds by default), allowing a
final stop at the experiment-duration boundary. Each operation receives the
smaller of its configured timeout and the remaining run budget. Cleanup always
runs in `finally` with a separate 10-second default budget, even when the run
budget is exhausted or cancellation is set. These scheduler settings do not
change existing deployment acceptance thresholds.

Ordinary errors and any early client exit (even return code zero) stop further
dispatch and return partial evidence. Unattempted events become `skipped`;
failed attempts retain their error and timestamps. Cleanup errors remain
separate from the primary error and prevent a successful run result. Status is
`completed`, `failed`, `timed_out`, `cancelled`, or `interrupted`.
`KeyboardInterrupt` propagates as `ScheduleInterrupted`, carrying `.result`,
after cleanup. `SystemExit` is re-raised with `.schedule_result` attached.
Cancellation and deadline checks occur between control calls and at most each
poll interval while idle; backend call latency contributes to response time.

Run both dynamic model and scheduler tests entirely offline:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_dynamic*.py' -v
```

## Limits and future work

Linux-only live measurements verify collection and orchestration; they do not
establish Abilene convergence, congestion repair, model decision quality, or
optimization gains. Abilene experiments require Nick's machine and repeated
matched workloads. Loaded RTT is sampled only in single-flow mode; conservative
timing can leave short-flow latency unknown. Concurrent jitter stays per-flow;
there is no meaningful aggregate jitter inferred across paths. Utilization uses
reference capacities unless shaping is explicitly enabled. Probe/container
overhead, statistical uncertainty, longer workloads, and larger configurable
concurrency bounds need further evaluation. No latency or performance improvement
is claimed by passing the offline suite or a single unshaped live test.
