# Four-router cEOS lab

```text
                     R2
                    /  \
Host A ---------- R1    R4 ---------- Host B
                    \  /
                     R3
```

All transit interfaces run OSPF process 1 in area 0. Host-facing interfaces
are passive OSPF interfaces. The path through R2 has cost 20 between R1 and R4;
the path through R3 has cost 200. Each host-facing interface has the same default
cost, so R2 is preferred in both directions.

| Connection | First endpoint | Second endpoint |
| --- | --- | --- |
| R1 Ethernet1 — R2 Ethernet1 | 10.0.12.1/30 | 10.0.12.2/30 |
| R1 Ethernet2 — R3 Ethernet1 | 10.0.13.1/30 | 10.0.13.2/30 |
| R2 Ethernet2 — R4 Ethernet1 | 10.0.24.1/30 | 10.0.24.2/30 |
| R3 Ethernet2 — R4 Ethernet2 | 10.0.34.1/30 | 10.0.34.2/30 |
| R1 Ethernet3 — Host A eth1 | 10.10.1.1/24 | 10.10.1.10/24 |
| R4 Ethernet3 — Host B eth1 | 10.10.2.1/24 | 10.10.2.10/24 |

Management uses the existing `advnet-ceos-mgmt` Docker network. Router management
addresses are 172.30.100.11–14 in a separate MGMT VRF. Host management addresses
are 172.30.100.101–102. Hosts have explicit routes to the opposite data subnet
and no default route. Test pings bind to eth1. Test traffic must traverse the
routers rather than Docker's management bridge.

From the project root:

```bash
sudo containerlab deploy -t labs/diamond.clab.yml
sudo bash scripts/check-diamond.sh
```

The initial deployment pulls `alpine:3.22.1` for the two hosts. Routers use the
locally imported `ceos:4.36.3M`. The checker waits for connectivity, shows OSPF
neighbors and host routes, pings in both directions, and prints a traceroute.
Each router should have two FULL neighbors. The preferred path should traverse
R1, R2, and R4; traceroute reply addresses can vary by interface.

Baseline source configurations live in `labs/configs/diamond/r1.cfg` through
`r4.cfg`. Generated saved configurations live in
`labs/clab-advnet-diamond/<router>/flash/startup-config`.
Existing generated configs take precedence on subsequent deployments. To reset
this lab to the baseline after experimenting (discarding its runtime changes):

```bash
sudo containerlab destroy -t labs/diamond.clab.yml --cleanup
sudo containerlab deploy -t labs/diamond.clab.yml
```

The separate `ceos-smoke` lab can remain running. To stop it and free resources:

```bash
sudo containerlab destroy -t labs/ceos-smoke.clab.yml
```

Live forwarding and EOS configuration acceptance must be checked after deployment.

## Offline Batfish verification (Step 1)

From the repository root, build a snapshot from the four baseline router configs:

```bash
python3 scripts/build_diamond_snapshot.py --output runs/diamond-snapshot
```

The builder uses only Python's standard library. No Docker topology needs to be
running. It writes `configs/r1.cfg` through `r4.cfg` with an EOS identification
comment, `hosts/host-a.json`, `hosts/host-b.json`, and `intent.json`. Baseline
files are copied without modifying them. The output directory must be new; use
a different `--output` path for another snapshot rather than overwriting a candidate.

Host models follow `advnet.trial.snapshot`: only `eth1` is modeled, with
10.10.1.10/24 via 10.10.1.1 (r1 Ethernet3) and 10.10.2.10/24 via 10.10.2.1
(r4 Ethernet3). No host management interface or management gateway is included.

Install the existing Batfish extra into a local virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[batfish]'
```

With a reachable Batfish service on its standard port 9996, run:

```bash
BATFISH_HOST=localhost .venv/bin/python -m advnet.batfish_gate runs/diamond-snapshot
```

Set `BATFISH_HOST` to the service's hostname if it runs elsewhere. The gate does
not start Batfish or a Containerlab topology. Abilene does not need to be running.
If no service exists, deployment is a separate prerequisite; see the Batfish
service example in [Abilene instructions](../docs/ABILENE_BASELINE.md).

The gate uses `intent.json`'s `lab` as the Batfish network name. Expected routers
come from `routers`; hosts come from Diamond's separate `hosts` mapping (each
entry has `host_ip`) or Abilene's existing per-router `host`/`host_ip` entries.
Nick's experiment controller and Abilene manifest format are unchanged.

Success requires every expected router and host to be modeled and all modeled
paths to be ACCEPTED for both `host-a -> host-b` and `host-b -> host-a`: exactly
two directed cross-host checks. Source selectors use exact host names from the
manifest and exclude the destination. Self-flows cannot satisfy these checks.
Traces must start/end at those hosts and traverse modeled routers. Abilene still
requires all 110 directed cross-host checks.

Results are JSON with `accepted` (boolean) and nonempty `details` (strings).
Read `accepted`; a completed process alone is not approval. Missing or failed
queries, parsing/initialization issues, missing nodes/flows, truncated traces,
and any unexpected disposition reject the snapshot. Actual SDK/query errors are
included in `details`; unsupported EOS syntax is not silently approved. This
step checks modeled ICMP reachability only; it makes no deployment, rollback,
LLM call, congestion, or live-network correctness claim.

Run focused offline tests (no Batfish client or server required), or the full
suite after installing the project dependencies above:

```bash
python3 -m unittest discover -s tests -p 'test_batfish_gate.py' -v
.venv/bin/python -m unittest discover -s tests -v
```

The fixtures cover healthy and invalid Diamond responses, failures and missing
data, and the original Abilene manifest. They do not establish live Batfish
compatibility. On November 5, 2026, the healthy Diamond snapshot also passed real
Batfish parsing, initialization, and both directed ICMP checks against the local
server. The local capture is `runs/diamond-live-validation.json`.

Host and selector formats follow the official [snapshot format documentation](https://batfish.readthedocs.io/en/latest/formats.html)
and [specifier grammar](https://github.com/batfish/batfish/blob/master/questions/Parameters.md).

## Reject a broken snapshot (Step 3)

`labs/diamond.clab.yml` connects `r1:eth3` to `host-a:eth1`. The baseline
`r1.cfg` confirms `interface Ethernet3`, description `to-host-a`, and gateway
address `10.10.1.1/24`. Build a separate snapshot with that interface disabled:

```bash
python3 scripts/build_diamond_snapshot.py --host-a-down --output runs/diamond-host-a-down
.venv/bin/python -m advnet.batfish_gate runs/diamond-host-a-down
.venv/bin/python -m advnet.batfish_gate runs/diamond-snapshot
```

The flag replaces only `no shutdown` with the EOS command `shutdown` in the
generated R1 Ethernet3 block. Without `--output`, it defaults to
`runs/diamond-host-a-down`; healthy builds still default to `runs/diamond-snapshot`.
Both modes refuse existing output directories. The builder rejects missing,
duplicate, already disabled, or ambiguous interface blocks before creating the
snapshot. Baseline configs, existing snapshots, and the live lab are untouched.

A successful negative test requires valid parsing and modeling followed by a
connectivity rejection. A server/query error, failed parse, missing node, or
initialization issue does not demonstrate detection of broken connectivity.
Inspect JSON `accepted` and `details`, not just the process exit status.

Real Batfish verification on November 5, 2026 parsed all four broken-snapshot
router files as ARISTA: PASSED and both host files as HOST: PASSED. All six
nodes were modeled, with zero initialization issues. The gate returned
`accepted: false` with these actual ICMP results:

| Directed flow | Disposition | Modeled path before failure |
| --- | --- | --- |
| host-a -> host-b | NEIGHBOR_UNREACHABLE | host-a (cannot reach gateway 10.10.1.1) |
| host-b -> host-a | NO_ROUTE | host-b -> r4 |

The healthy snapshot was then reverified: `accepted: true`, with both paths
ACCEPTED through r1, r2, and r4. The local evidence file
`runs/diamond-host-a-down-verification.json` includes the actual gate outputs,
Batfish snapshot IDs, parse/model results, complete trace details, input hashes,
and confirmation that protected baseline/healthy files were unchanged. Runtime
evidence under `runs/` remains ignored by Git.

The full offline suite passes 72 tests, including Abilene compatibility and
five added tests for broken-snapshot generation, overwrite protection, invalid
interface handling, the CLI flag, and connectivity-specific gate rejection.

## Controller integration with simulated routers

`trial.snapshot()` now accepts Diamond's separate host mapping as well as the
original Abilene host-per-router format. The EOS identification comments, host
JSON schema, directory structure, and Abilene behavior are preserved.

`tests/test_trial_gate.py` connects the actual `Trial` and `ProcessVerifier` to
the actual Batfish gate subprocess. Only the router lab and decision source are
fixtures: no Docker, Containerlab, router command, LLM, or API key is used. A
subprocess guard permits only `python -m advnet.batfish_gate <snapshot>` and
executes that command for real against `localhost:9996`.

The fixture starts with baseline Diamond configurations. The harness disables
R1 Ethernet1 **in memory**, breaking an expected backbone adjacency while the
alternate R3 path preserves host connectivity. This satisfies the repair
precondition. A predetermined, typed Proposal then disables R1 Ethernet3,
isolating host-a in the candidate snapshot. Real Batfish must approve the initial
healthy snapshot and reject that candidate for connectivity failure.

Two bounded decision attempts allow the test to capture the controller's
verification rejection feedback on the next attempt. The fixed decision repeats
the same bad proposal; the controller's duplicate protection prevents a second
candidate verification. Neither attempt enters `deploy`. Fault injection and
final healthy-state restoration are recorded as separate in-memory writes.
The existing stage/result labels still say `llm_decision`/`llm_only`, but this
fixture makes no model calls; the evidence explicitly identifies the fixed
decision and synthetic measurements.

Offline controller, snapshot compatibility, and verifier-error regressions:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_trial_gate.py' -v
```

The live test is opt-in. Run the complete suite including real Batfish:

```bash
RUN_BATFISH_INTEGRATION=1 .venv/bin/python -m unittest discover -s tests -v
git diff --check
```

Without the flag, the live test is explicitly skipped. With the flag, an
unavailable server fails the test; it is never treated as a successful rejection.
Each live run creates a new `runs/diamond-trial-gate-<timestamp>-<id>/` directory
with `evidence.json` and the actual Trial artifacts, including baseline/candidate
verification results, snapshots, rejection feedback, and stage events.

On November 5, 2026, all 79 tests passed with live integration enabled (78 offline
tests and one live test). Batfish approved the baseline and rejected the candidate
with `host-a -> host-b: NEIGHBOR_UNREACHABLE` and `host-b -> host-a: NO_ROUTE`.
All four candidate routers parsed as ARISTA: PASSED, both hosts as HOST: PASSED,
all six nodes were modeled, and initialization issues were empty. The controller
recorded verification rejection feedback, skipped deployment, and restored its
in-memory state. The local report is
`runs/diamond-trial-gate-20261105T090804Z-d615954f/evidence.json`.

`integration_passed: true` means this safety test passed. The enclosed Trial
`success: false` is expected: the proposed repair was rejected, not deployed.
Missing/malformed verifier output, nonzero process exits, timeout cleanup, and
both manifest formats also have offline regression coverage.

This proves the real controller-to-verifier decision path with simulated router
writes. It does not validate real EOS replacement, live measurements, convergence,
or rollback. Full live Diamond controller support still requires a complete
Diamond runtime manifest and adapting `advnet/lab.py` to the two-host layout,
followed by separately authorized live-network tests. No baseline router files,
Abilene files, or `advnet/lab.py` were changed for this integration test.
