# Abilene LLM-only baseline

The next experiment is the proposal's closed loop without a System One model.
Establish repeatable network and repair measurements first; introduce Jev or Kev
only after this baseline works. Existing screening code is inactive in these trials.

## Topology and provenance

`data/abilene-source.json` records the 11 sites and 14 edges from the Internet
Topology Zoo's Abilene 2005_02 GraphML:
https://raw.githubusercontent.com/sk2/topologyzoo/master/sources/Abilene.graphml

The source graph's listed edges are represented as bidirectional Ethernet links.
Router names are sea, sva, lax, den, kcy, hou, atl, ind, chi, was, and nyc.
One Linux host is attached at each site. This is the source's topology, with
experimental addressing, uniform OSPF costs, Ethernet interfaces, and scaled
capacities. It is not a recreation of historical propagation delays or capacity.

```mermaid
flowchart LR
  sea[Seattle] --- sva[Sunnyvale]
  sea --- den[Denver]
  sva --- lax[Los Angeles]
  sva --- den
  lax --- hou[Houston]
  den --- kcy[Kansas City]
  kcy --- hou
  kcy --- ind[Indianapolis]
  hou --- atl[Atlanta]
  atl --- was[Washington DC]
  atl --- ind
  ind --- chi[Chicago]
  chi --- nyc[New York]
  was --- nyc
```

`topologies/abilene.json` is the machine-readable interface/address inventory.
`labs/configs/abilene/*.cfg` are reproducible EOS source configurations.
`labs/abilene.clab.yml` is JSON (valid YAML) and can be used directly by Containerlab.
Router management IPs are 172.30.100.41–51 and host management IPs are
172.30.100.141–151, on the existing advnet-ceos-mgmt network. Router MGMT VRFs
isolate management routing. Hosts have no default route; only the experiment
aggregate 10.64.0.0/16 is routed through their eth1 gateway. Transit links use
10.65.0.0/24 split into /30s. Each host has its own 10.64.N.0/24 subnet.

Regenerate source artifacts deterministically:

```bash
python3 scripts/generate-abilene.py
```

Do not regenerate while manually editing the generated source configs; keep new
scenario changes in the scenario files. Existing runtime configs take precedence
on redeployment. An intentional reset requires destroying this lab with --cleanup
before redeploying, which discards runtime changes.

## 1. Deploy and validate the healthy network

Run commands in your desktop terminal from the project root. Docker requires
either active docker-group membership or sudo. Keep model calls in your normal
user session so API environment variables remain available.

Optionally stop the earlier labs to free resources:

```bash
sudo containerlab destroy -t labs/diamond.clab.yml
sudo containerlab destroy -t labs/ceos-smoke.clab.yml
```

Build the traffic-host image and deploy the new topology:

```bash
sudo docker build -t advnet-host:0.1 labs/hosts
sudo containerlab deploy -t labs/abilene.clab.yml
uv sync --extra openai --extra batfish
sudo .venv/bin/python -m advnet.experiment check --seconds 300
```

The host image includes iproute2, ping, traceroute, and iperf3; it does not change
host system libraries. Each host runs an iperf3 server bound to its data address.
The check waits for routing-table stability, checks the exact expected FULL OSPF
neighbor sets, and pings all 110 directed pairs (three packets each) over eth1.
It returns failure if any pair loses packets or any intended adjacency is missing.
It writes full measurements under runs/ and prints a short summary.

Source generation, contract tests, and fake-network tests can run without Docker.
They do not establish live connectivity. On November 2, 2026, a live Abilene
capture verified all 110 directed host pairs with zero ping loss and all expected
OSPF neighbors FULL. Mean RTT was 5.69 ms. This validates that capture, not a
repair trial or congestion performance.

## 2. Validate Batfish and capture observations

The included `advnet.batfish_gate` is a conservative initial adapter that your
partner can review or replace. It models all routers and hosts, rejects parse
failures/initialization issues/missing nodes or flows, and requires all modeled
paths for all 110 directed ICMP flows to be ACCEPTED. There is no approval bypass.
It rejects rather than deploys if the service or Python dependency is unavailable.

If your partner is providing a service, set BATFISH_HOST as appropriate. Otherwise
the documented all-in-one Batfish image can run locally:

```bash
sudo docker run -d --name advnet-batfish \
  -p 127.0.0.1:9996:9996 -v advnet-batfish-data:/data batfish/allinone
```

Allow initialization before a trial. Retain the actual Docker image digest and
`uv.lock` with experiment records once installation succeeds; unpinned image
names are for initial setup, not claims of reproducible benchmark versions.
Readiness and exact pybatfish/EOS feature support still need live validation.
Snapshots are uniquely named and retained in Batfish for troubleshooting.

To capture an observation without calling a model or modifying routers:

```bash
sudo .venv/bin/python -m advnet.experiment collect --output runs/abilene-observation.json
```

All observations come from current configs, OSPF neighbors, and real host probes;
this path does not use the earlier synthetic examples or diamond action catalog.

### Generate an unverified proposal with OpenRouter

The latest live capture is saved in `captures/abilene-live/observation.json`.
This ignored runtime directory is writable by the normal user even when old
files in `runs/` were created with sudo. The empty action catalog prevents the
standalone CLI from loading diamond-specific actions.

In the terminal containing your OpenRouter key in `OPENAI_API_KEY`, run without
sudo:

```bash
.venv/bin/python -m advnet captures/abilene-live/observation.json \
  --catalog captures/abilene-live/empty-catalog.json \
  --mode live --without-jev --llm-provider compatible \
  --base-url https://openrouter.ai/api/v1 --llm-model openrouter/free \
  --timeout 120 --max-tokens 4096 \
  --log captures/abilene-live/model-calls.jsonl
```

This submits a saved observation; it does not deploy, inject faults, or call
Batfish. A healthy network may correctly produce `no_change`. `decision_complete`
means the output passed local validation, not that a repair succeeded. No
congestion inference is justified by idle pings alone; this capture has no loaded
traffic measurements. The free router can choose a different model each time;
retain the resolved model in each log and pin a model for comparison trials.

Refresh the observation before later model calls (Docker access required):

```bash
.venv/bin/python -m advnet.experiment collect \
  --output captures/abilene-live/observation.json --seconds 180
```

## 3. Run one LLM-only repair trial

Export OPENAI_API_KEY in your shell and set LLM_MODEL to an exact available model
ID. Do not put keys in commands, Git, or chat. Use your normal secret manager or
an interactive shell prompt. The CLI does not automatically load .env files.

```bash
sudo --preserve-env=OPENAI_API_KEY,LLM_MODEL,BATFISH_HOST \
  .venv/bin/python -m advnet.experiment run \
  --scenario scenarios/sea-host-interface-down.json --model "$LLM_MODEL"
```

The runner performs:

1. Checkpoint current healthy configs, wait for routing stability, measure the
   healthy network, and require its snapshot to pass Batfish BEFORE injection.
2. Start a monotonic trial deadline and inject a known fault. The scenario and
   reference injection remain private harness data, never model context.
3. Confirm the intended impairment exists and collect fresh observations.
4. Call the LLM with an EMPTY action catalog and NO Jev/Kev call. Validate the
   typed patch against observed device/interface names. Reject repeated patches.
5. Materialize the exact candidate configurations and submit them to the verifier.
   Rejections return detailed feedback; unapproved candidates never deploy.
6. Check configuration readback has not changed during verification; deploy the
   verified candidate configs using EOS `configure replace` over Docker transport.
7. Wait for routing stability and repeat runtime measurements. Keep only an
   accepted candidate; otherwise restore and check the pre-attempt configuration.
8. Stop at success, five attempts, or the 900-second fault-to-decision budget.
   On failure, restore the healthy checkpoint and check connectivity again. This
   final reset is explicitly logged as harness cleanup, not a model repair.

The five-attempt/time budget is enforced by this runner, including subprocess
timeouts for the model and verifier. Healthy preflight and emergency rollback/
cleanup have separate bounds. Interrupted/error trials are logged as failures.
Do not hard-kill the process during rollback. Local flock prevents concurrent
instances of this runner on the same working directory; it does not lock out
manual configuration or another host.

Current acceptance thresholds are explicit starting choices: all pairs reachable
with zero sampled ping loss; all intended OSPF adjacencies FULL; idle ping latency
within healthy mean times 1.25 plus 5 ms. For traffic trials, delivered throughput
must be at least 90% of healthy baseline, and UDP loss must not exceed healthy
baseline by more than two percentage points. These tolerances need development
calibration and should then be frozen for held-out trials.

No raw CLI generation is accepted: the LLM can enable/disable interfaces or set
OSPF interface cost/area. More complicated repairs require extending the typed
patch language. Per-device replacement and multi-device deployment are not
network-wide atomic; rollback can limit persistent damage but cannot prevent
all transient disruption. Successful trial changes stay in running-config;
they are not automatically persisted to startup-config.

## 4. Additional scenarios and comparisons

- `sea-sva-area-mismatch.json`: misconfigure one transit interface's OSPF area.
  Alternate paths may preserve host connectivity, so exact expected adjacencies
  are also part of the runtime repair criterion.
- `sea-sva-congestion.json`: enforce baseline 100 Mbps transit capacities, then
  reduce sea–sva to 2 Mbps in each direction and offer a fixed 5 Mbps UDP flow
  from Seattle to Sunnyvale for eight seconds per measurement. A tc sidecar
  shapes the cEOS network namespaces. This injects a capacity impairment, not a
  routing misconfiguration. The LLM may change OSPF costs to use alternate paths.

For congestion, maximum sampled directional link utilization must improve by at
least 5% compared with the impaired network, with the throughput/loss/connectivity
guardrails above. Interface tx-byte deltas surround the same fixed-load iperf3
receiver measurement. Capacity values are meaningful only when shaping succeeds.
Idle ping measurements are taken before traffic, so they must NOT be described as
latency under congestion. Future experiments should add sustained-load latency
sampling and longer/repeated measurement windows. Sidecar and counter sampling
overhead should also be characterized before interpreting small differences.

Use `--attempts 1` for a single-shot verified baseline and `--attempts 5` for the
iterative verified loop, holding all other settings constant. This is not an
exact reproduction of either cited paper. An unverified-agent ablation and
Jev/Kev are deferred until the verified LLM-only loop is measured.

Start each trial from the intended healthy baseline. To discard an accepted repair
and reset the experiment topology, intentionally run:

```bash
sudo containerlab destroy -t labs/abilene.clab.yml --cleanup
sudo containerlab deploy -t labs/abilene.clab.yml
```

An external partner verifier can replace the bundled adapter:

```bash
sudo --preserve-env=OPENAI_API_KEY,LLM_MODEL \
  .venv/bin/python -m advnet.experiment run \
  --scenario scenarios/sea-host-interface-down.json --model "$LLM_MODEL" \
  --verifier-command '/absolute/path/to/python /absolute/path/to/partner_verifier.py'
```

The snapshot directory is appended as one argument. The verifier must return only
JSON on stdout: `{"accepted": false, "details": ["Specific policy violation"]}`.
Diagnostics go to stderr. It may inspect intent.json, configs/, and hosts/.
External verification is an integration point, not a way to assert unchecked
approval. Its policy coverage and version must be recorded with results.

## Data and current limits

Each run stores healthy/impaired measurements and configs, candidate snapshots,
SHA-256 candidate digests, model inputs, model usage, verification results,
post-deployment measurements, rollback/reset artifacts, stage timings, and a
final result.json. The private scenario file is not sent to the LLM. Unknown
API dollar cost remains null rather than zero; apply dated provider prices to
recorded usage when analyzing results. Logs can contain device configs; runs/ is
ignored by Git.

`fault_to_acceptance_seconds` is time from injection start to completed runtime
acceptance, not an exact timestamp of the first restored packet. Failed repairs
have null acceptance time and remain in the dataset. trial_elapsed_seconds also
includes cleanup. Summarize without excluding failures:

```bash
python3 scripts/summarize-trials.py runs > /tmp/abilene-results.csv
```

The initial Batfish gate checks ICMP reachability. It does not yet implement the
proposal's security-zone ACL/isolation or required-path policies, and the runtime
checker does not claim application-level security validation. The test suite
checks code paths using fakes, not live API or network compatibility. The full
proposal is not experimentally complete until Docker deployment, Batfish parsing,
configuration replacement/rollback, live OpenAI calls, and repeated scenarios
have all been exercised and those additional policy/traffic cases are added.

## Development checks

```bash
uv run python -m unittest discover -s tests -v
```

References:

- Topology source: https://github.com/sk2/topologyzoo/blob/master/sources/Abilene.graphml
- EOS configuration replacement: https://www.arista.com/en/um-eos/eos-session-management-commands
- Batfish hosts/snapshots: https://batfish.readthedocs.io/en/latest/formats.html
- Batfish traceroute: https://batfish.readthedocs.io/en/latest/notebooks/forwarding.html
- OpenAI API: https://developers.openai.com/api/docs/quickstart
