# Optional Abilene transit-exclusion experiment

Abilene is the final 11-router experimental topology; Diamond remains the smaller
development fixture. This experiment demonstrates that the same path gate works
on Abilene and blocks an otherwise connected candidate in the actual Trial loop.
It uses existing gate/controller behavior; no live router operations or LLM calls
are needed to reproduce the verification test.

## Policy and routing experiment

`experiments/abilene-path-policy.json` is the optional experiment definition. The
builder reads `topologies/abilene.json`, creates a separate full manifest, and adds
two directional ICMP policies between `h-sea` and `h-nyc`. Both forbid transit
router `lax`, with empty required-router lists. The manifest's prose requirements
also describe the policy, so the normal observation includes the same constraint.

This represents an administrative transit exclusion for one protected host pair.
It does not assert that LAX is actually untrusted, unavailable, or historically
restricted. It does not pin an exact path or require a particular waypoint.
The other 108 directed flows retain reachability-only requirements, and all 110
directed ICMP connectivity checks remain mandatory. Other routes can be optimized
as long as every modeled path for the protected pair avoids LAX. This optional
restriction deliberately rules out some otherwise valid optimization choices;
it is not a default requirement for ordinary Abilene experiments.

All baseline backbone interface costs are 10. The expected forward paths are:

| Snapshot | Seattle-to-New-York route | Connectivity | Gate |
| --- | --- | --- | --- |
| Healthy, with policies | sea → den → kcy → ind → chi → nyc | All 110 flows ACCEPTED | ACCEPT |
| Candidate, with policies | sea → sva → lax → hou → atl → was → nyc | All 110 flows ACCEPTED | REJECT: forbidden LAX |
| Same candidate, no policies | Same route through LAX | All 110 flows ACCEPTED | ACCEPT |

The deterministic candidate uses the existing `Proposal` contract and
`advnet.eos.candidate_configs()` to change only `den/Ethernet3` (toward `kcy`)
from cost 10 to 100. Interfaces and OSPF areas remain unchanged. The previous
50-cost backbone route becomes 140; the southern route has cost 60. The cost
is directional, so the reverse protected flow can keep its ordinary northern
route. Both directions are checked independently. Actual paths are verified
against Batfish rather than inferred solely from these cost calculations.

## Build and inspect snapshots

From the repository root, with the existing project environment:

```bash
.venv/bin/python -m scripts.build_abilene_path_experiment \
  --output runs/abilene-path-experiment
```

The output root must be new. It contains:

- `manifest.json`: the separate full Abilene experimental manifest.
- `candidate-proposal.json`: the typed, predetermined OSPF candidate.
- `scenario.json`: the optimization scenario used by the simulated Trial test.
- `healthy/snapshot`: baseline configs with the optional policies.
- `candidate/snapshot`: candidate configs with the same policies.
- `candidate-no-policy/snapshot`: identical candidate configs with the original
  Abilene manifest, demonstrating compatibility without policies.

Each snapshot contains all 11 router configs, 11 host models, and `intent.json`.
Source configs and the original manifest are never edited. Repeated builds into
different new directories produce the same input bytes.

With an already running Batfish service, inspect each gate result:

```bash
for case in healthy candidate candidate-no-policy; do
  .venv/bin/python -m advnet.batfish_gate \
    "runs/abilene-path-experiment/$case/snapshot" |
    .venv/bin/python -m json.tool
done
```

Read `accepted`, `details`, and `path_checks`. A proper negative result identifies
`h-sea -> h-nyc`, the full path through `lax`, and `path_policies[0]` with
`forbidden_routers: ["lax"]`. A service, parser, initialization, or missing-flow
error is a failed experiment, not proof of a path-specific rejection.

## Actual controller test with simulated router operations

Run the focused tests, including real Batfish:

```bash
RUN_BATFISH_INTEGRATION=1 .venv/bin/python -m unittest discover \
  -s tests -p test_abilene_path_policy.py -v
```

The test first runs all three snapshot cases through real `ProcessVerifier` gate
subprocesses. It checks all 22 parse results, all 22 modeled nodes, zero init
issues, complete trace counts, and every path for all 110 directed ICMP flows.
The candidate must reject only for the declared forbidden-transit violation.
The no-policy control must accept the exact same router configs.

The test then runs the actual `Trial` controller with:

- A standalone in-memory lab with no Docker/CLI transport. Config reads/writes,
  OSPF/runtime measurements, capacity shaping, and convergence are simulated.
- A synthetic reduction of `den-kcy` capacity from 100 to 2 Mbps under a nominal
  5 Mbps Seattle-to-New-York flow. Explicitly synthetic measurements satisfy
  Trial's optimization precondition; they are not measured performance results.
- A fixed typed cost proposal, with no model calls or API credentials.
- The actual `ProcessVerifier` and Batfish gate subprocesses. Only the gate
  command is allowed by the subprocess guard.

Batfish must accept Trial's healthy snapshot, then reject the generated candidate.
The controller must skip deployment and runtime candidate evaluation, preserve
the rejection as verification feedback, and expose it to the second decision
attempt. Repeating the same proposal exercises the existing duplicate protection;
no second candidate verification is performed. The fixture permits only a
simulated final baseline reset and records capacity restoration.

Trial's `success: false` is expected because the unsafe optimization was not
deployed. The aggregate `integration_passed: true` means the safety test passed.
Existing Trial labels such as `llm_decision` are retained even though the decision
source here is a fixture. No deployment-controller code is changed.

## Evidence and verification boundaries

Each opted-in run creates `runs/abilene-path-policy-<UTC timestamp>-<id>/` (or a
directory under `ADVNET_TEST_OUTPUT_DIR`). It retains the generated manifests,
proposals and snapshots, plus:

- Aggregate `evidence.json` describing actual versus simulated components.
- `healthy/evidence.json`, `candidate/evidence.json`, and
  `candidate-no-policy/evidence.json` containing actual gate JSON, Batfish
  snapshot IDs, parse/model results, all forwarding paths and input hashes.
- `trial-healthy/evidence.json` and `trial-candidate/evidence.json` inspecting the
  exact snapshots uploaded by the real controller's verifier subprocesses.
- `trial/` with normal Trial verification, feedback, observation, snapshot,
  event, candidate-digest and result artifacts.
- SHA-256 checks confirming the original Abilene manifest, topology and router
  configs, Diamond configs, gate, candidate generator, and controller did not change.

Runtime evidence is ignored by Git. With the integration flag unset, the real
test is explicitly skipped; with the flag set, an unavailable server fails it.

These results establish modeled ICMP forwarding and the controller's rejection
boundary. They do not establish live OSPF convergence, actual traffic congestion,
throughput improvement, live EOS replacement or rollback, or LLM decision quality.
The existing gate's scope still applies: protocol-specific concrete traceroutes,
all returned forwarding alternatives, and rejection of incomplete results.
This experiment does not add exhaustive transport-port, security-zone ACL, or
stateful application verification.

Run the complete regression suite, including Diamond and ordinary Abilene:

```bash
RUN_BATFISH_INTEGRATION=1 .venv/bin/python -m unittest discover -s tests -v
git diff --check
```

## Recorded validation

On November 22, 2026 UTC, the complete suite passed all 100 tests with real
integration enabled (97 offline tests and three real-server integration tests).
This included the existing Diamond path/controller tests and normal Abilene
reachability compatibility checks.

Real Batfish accepted the healthy Abilene experiment and rejected the cost-change
candidate solely for `h-sea -> h-nyc` traversing forbidden `lax`. The same
candidate without policies accepted. In every case all 110 directed ICMP flows
were ACCEPTED, all 22 files parsed PASSED, all expected nodes were modeled, and
initialization issues were empty. Trial's independently uploaded healthy and
candidate snapshots produced the same results. Trial skipped deployment, passed
the path diagnostic to the next attempt, and completed simulated cleanup.
Protected-file hashes were unchanged. `git diff --check` passed.

The local report is
`runs/abilene-path-policy-20261122T062516Z-2c564658/evidence.json`.
Its `healthy/evidence.json`, `candidate/evidence.json`, and
`trial-candidate/evidence.json` retain the separate verification evidence.
These are actual Batfish/controller results with simulated network operations
and synthetic runtime measurements, as described above.
