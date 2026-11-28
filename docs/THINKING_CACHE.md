# Verified action memory for Jev

This experimental feature lets verified LLM proposals become future Jev catalog
choices. It stores typed actions and scoped evidence, not model weights or raw
reasoning. It is disabled by default. Live effectiveness has not been measured.

## Lifecycle

The existing controller first asks Jev to choose an action or escalate. An
escalation uses the LLM as before. The controller can record its typed patch only
after Batfish accepts it, runtime checks accept its measured effect, and final
configuration readback confirms the deployment. Rejected, rolled-back,
unconfirmed, missing-measurement and synthetic outcomes cannot seed the cache.

A new entry starts in quarantine. The **same patch under compatible contexts** must
succeed in at least two distinct controller output directories before becoming
active. Retries and resumes within one directory count once. This is a minimum
replication rule, not proof of safety or causal improvement. Two runs may produce
different valid patches, leaving both quarantined; further matching successes
would then be needed. Cached selections cannot validate or promote themselves.

The next inference worker reads eligible active actions from SQLite and merges
them into the public action catalog. Jev must still choose one confidently;
otherwise it escalates. Both Jev and LLM see the same merged catalog. All existing
scope, stability, checkpoint, Batfish, convergence, runtime and rollback checks
remain in force. A reused action that reaches the executor and is rejected,
rolled back or unconfirmed is retired. Retirement has no automatic reversal.
Policy rejection before execution does not establish a failed runtime action.

Learning is a small local database transaction after execution; no extra LLM call
or free-form catalog-description generation is used. Descriptions are generated
from fixed application text and proposals retain only validated typed patches.
The original LLM may use a learned action from its prompt; if that LLM-led proposal
passes validation it can provide another success. Pure Jev cache hits cannot.

## Matching and provenance

An entry binds to the manifest, original catalog, prompt version, model IDs,
evidence mode, endpoint/provider settings and output budget. Reuse also requires
matching normalized full pre-change configuration, interface inventory,
topology, requirements, objective, demand profile, shaping fingerprint,
intervention kind, health flags, failed host pairs, mismatched OSPF adjacencies,
performance-health/latency alarm state, affected links and metric-definition
scope. Those conditions remain exact. Matching version 2 allows limited noise
in the measured slow-pair set: the intersection divided by the union must be at
least 90%, against **every** accepted training context. Both sets may be empty;
an empty set cannot match a nonempty set. Opposite directions are distinct pairs.
The prototype and every accepted context are retained, preventing gradual
expansion through a chain of similar cases. Different typed patches never
share success counts. Timestamps, observation IDs and demand-epoch IDs
are excluded to allow recurrence across separate runs.

Slow-pair matching compares each observed pair RTT against the current aggregate
latency limit solely as a matching feature; it is not a per-pair acceptance SLA.
The 90% cutoff is an experimental heuristic, not a validated fault classifier.
Matching symptoms does not identify a root cause or guarantee a correct action.
The exact configuration guard intentionally limits transfer to new topology or
configuration states. Live validation remains necessary on every use.

Stored provenance includes protocol binding, context, typed patch, trial path,
intervention ID and accepted/failed outcomes. Raw configurations and LLM prose
are not copied into the database. Existing trial artifacts retain model timing,
usage, runtime evidence and decisions. Every enabled decision records eligible
entry IDs, catalog hash, cache mode and binding; selections retain the action ID.
The journal records promotions/retirements and sanitized cache errors. Cache
read errors fall back to the base catalog; write errors leave the executor's
network outcome intact. Such errors must be reported in experiment analysis.

## Commands

With the lab, shaping, environment and healthy baseline ready, a first training
run can use:

```bash
sudo --preserve-env=OPEN_ROUTER_KEY,LLM_MODEL,SYSONE_MODEL,BATFISH_HOST,LLM_BASE_URL \
  .venv/bin/python -m advnet.controller run \
  --duration 20m --system-one jev \
  --workload experiments/continuous/sea-sva.json \
  --output runs/thinking-cache/train-01 \
  --thinking-cache runs/thinking-cache/memory.sqlite --cache-mode collect
```

`collect` records and promotes but does not retrieve. `adaptive` both learns and
retrieves. `frozen` retrieves without modifying the database. Use unique output
directories for independent trials. This command starts monitoring and traffic;
it does **not** inject a fault automatically.

Inspect learned entries, then create a consistent evaluation snapshot:

```bash
sudo .venv/bin/python -m advnet.thinking_cache inspect runs/thinking-cache/memory.sqlite
sudo .venv/bin/python -m advnet.thinking_cache freeze \
  runs/thinking-cache/memory.sqlite runs/thinking-cache/evaluation.sqlite
```

Use the snapshot with `--thinking-cache runs/thinking-cache/evaluation.sqlite
--cache-mode frozen` in subsequent fresh runs. It requires an active entry with
the matching protocol/model binding. Its file hash is bound to controller resume
parameters and checked before retrieval. A changed snapshot produces a recorded
cache error and base-catalog fallback; exclude such a run from a valid frozen
comparison. Snapshot creation refuses to overwrite an existing destination.

Omit both cache flags for the original fixed-catalog behavior. Disabling the
cache does not undo accepted network changes; restore baseline between trials.

## Proposed first experiment

Use 20-minute runs with one fault per output directory. Allow two minutes of
healthy measurements, inject the fault, then give it a five-minute repair
window. Record deadline failures rather than waiting indefinitely. Measure at
least two minutes of stability after acceptance. Restore and verify baseline
before the next independent trial. Hold traffic, shaping, power policy, model,
provider settings, verification and acceptance rules fixed.

Start with one reproducible fault that the LLM has already repaired, such as the
Denver–Kansas City latency condition. Training runs must exclude automatic fault
removal from credited acceptance: removal itself can restore performance and
must not become evidence that a patch fixed it. Keep the original fixed-window
latency protocol unchanged when making comparisons to those historical results.
A 20-minute session is an allowance for training/stability work, not a matched
replacement for earlier eight-minute timed trials.

After at least two matching accepted LLM repairs, freeze the catalog and evaluate
fresh fault instances. Report fixed LLM-only, fixed Jev+LLM and learned Jev+LLM
arms separately. An additional LLM-only arm supplied the same learned catalog
helps distinguish the value of stored knowledge from Jev classification.
Vary fault location/severity and include healthy and out-of-catalog conditions;
label deliberate exact repeats separately from held-out variations. Do not
learn from evaluation trials. An adaptive follow-on study can then measure
learning over repeated exposure, with exposure order and catalog versions logged.

Report accepted repair before deadline, decision versus end-to-end recovery time,
post-repair RTT/delivery, LLM avoidance, incorrect selections, rejection/rollback,
cache misses/errors and cost. Include training and discarded-call costs, alongside
evaluation cost and amortized totals. Analyze convergence and verification time
separately from Jev/LLM time. A faster classifier does not imply the same speedup
in total network recovery.

Version 1 completed live collection trials, documented in the experiment
report. Version 2 has offline lifecycle, inference and controller integration
coverage plus a separate live reference-readiness check. Live version-2 training,
promotion, reuse and speed/cost benefits remain unmeasured.

## Prepared automated training pilot

Preparation: `runs/thinking-cache-training-20261127T224720Z/plan.json`.
`run-training.py` runs two independent 20-minute Jev+Terra collection trials.
The controlled experiment now accepts `--duration`, `--fault-after`,
`--thinking-cache` and `--cache-mode`; the launcher uses a two-minute baseline,
200 ms Denver-to-Kansas-City delay, five-minute hold and original 35/25 Mbps
workload on 70 Mbps backbone shaping. All setup, fault injection/removal,
configuration restoration and traffic cleanup are automatic. Setup may add
several minutes to each timed trial.

Collection is closed before injection and before removal starts. Only accepted
actions completed while the fault remains injected can seed/promote entries.
Retrieval/retirement remain independent of this collection switch. Later model
calls are still billed and retained in the trial artifacts. After each completed
trial, the launcher snapshots the cache if one exists and prints its entry
statuses. Two successful trials can still produce different quarantined patches.

The preparation pins configured Terra/Jev model IDs, raw evidence, output budget,
source inventory/hashes and AC/performance power policy. It samples power each
second and stops advancement after any deviation or unconfirmed cleanup. It
requires a fresh cache on initial launch and preserves completed prefixes with
`--resume`; incomplete trials require inspection rather than silent reruns.
Frozen evaluation is not part of this pilot. No live pilot has been executed by
the agent.

## Matching version 2 and fresh training

The first live pilot collected two identical successful patches but split them
because two marginal host-pair RTTs changed classification. Version 2 keeps the
structural guards exact and permits at most the disagreement represented by a
90% minimum directed-pair Jaccard overlap. Promotion and retrieval compare with
all accepted examples for that entry. The cutoff was selected after diagnosing
version 1, so evaluation must be prospective rather than presented as an
independent confirmation on the original data.

Preparation: `runs/thinking-cache-training-v2-20261127T234421Z/plan.json`.
This uses a new empty cache and two independent 20-minute collection trials,
with the same timing, models, traffic and safety checks as the original pilot.
Version 1 entries are neither imported nor promoted. The original database and
snapshots remain archived; version 2 also records each accepted context in
`validation_contexts` for audit. The protocol version is part of both context and
model/protocol binding. Frozen evaluation follows only after real promotion.

## Further research ideas, not implemented

A strategy catalog could store a conditional intent such as avoiding a degraded
link while preserving delivery. Jev would select the strategy, a deterministic
planner would derive typed OSPF changes for the current configuration, and the
existing verifier/executor would evaluate the result. This might transfer more
widely than memorizing numerical patches, but applicability and planner behavior
would require separate validation.

A shadow stage could record Jev's choices while the LLM still controls the trial,
and check those candidates in Batfish without deploying them. This could expose
scope and policy errors before live reuse. Batfish alone cannot establish actual
latency/delivery improvement; shadow inference adds calls and its cost must be
reported. Neither proposal is included in the current matching experiment.

### Independent validation of a preselected patch

`advnet.recovery_experiment --arm designated` takes a read-only training
snapshot (`--training-snapshot`), an exact quarantined identifier
(`--designated-action`), and a new output directory. The arm exercises the
patch on a fresh performance fault after the stability policy permits repair.
It checks context compatibility with all training examples, then runs Batfish,
runtime acceptance, readback and three further stability windows while the
delay remains active. The arm uses an event-driven validation period with a
180-second deadline. Timing comparisons between natural decision arms use
the timed experiment protocol.

After successful validation and cleanup, `evaluation.sqlite` contains a copy
of the training evidence plus the new trial and its origin in
`designated_validations`. This procedure allows a successful independent
validation to count toward promotion. When reporting the independent-success
total, identify how many successes came from LLM selection and how many from
designated validation. Use a fresh frozen trial to measure natural Jev selection.
