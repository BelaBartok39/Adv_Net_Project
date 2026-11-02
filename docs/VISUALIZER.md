# Abilene Observatory

A local browser dashboard for the existing Abilene testbed and experiment runner.
It reads network state and experiment artifacts; it cannot deploy configurations,
inject faults, start model calls, or launch experiments. No new Python or Node
dependencies are required beyond the project's existing Python environment.
The web assets are local files; there are no CDN or external font dependencies.

## Live dashboard

From the project root, in your regular desktop terminal:

```bash
sudo .venv/bin/python -m advnet.visualizer
```

Open **http://localhost:8765**. Keep that terminal running; Ctrl+C stops the server.
If your user already has Docker access, sudo is unnecessary. No API keys are needed
for the dashboard. The server listens only on 127.0.0.1. Use `--port 8766` if the
default port is occupied.

The Abilene containers should already be deployed. If Docker is unavailable or
containers are missing, the UI shows limited/unknown telemetry rather than
inventing a healthy state. Last connectivity-check results remain explicitly
labeled by their original timestamp; they do not prove current connectivity.

## What you can see

- The 11 routers, 14 backbone links, and 11 attached test hosts.
- OSPF adjacency state on each backbone link. A missing neighbor is degraded;
  it is not automatically described as a physical link failure.
- Measured directional Mbps from interface transmit-byte counters. Link width
  and animation reflect traffic rates, not individually tracked packets.
- Current configuration costs after the next configuration refresh.
- Selected router running configuration, OSPF neighbors, Docker RAM and CPU.
- Experiment stage starts/completions, model diagnoses, candidate diffs, verifier
  outcomes, runtime acceptance, and rollback/reset events from the runner's files.
- Playback of recorded observer sessions and experiment checkpoints.

Select a router or link to inspect it. Use the Configuration and LLM changes tabs
for configuration details and candidate diffs. Candidate changes stay labeled as
proposals until verification and runtime outcomes have been recorded.

The EOS transport starts `Cli` with explicit privilege level 15 (`-p 15`) and
paging disabled. Repeating `-c enable -c ...` does not preserve the enable command
with this CLI and causes privileged reads to fail. After changing Python
collector code, restart the dashboard process; refreshing the browser only
reloads browser assets. Configuration errors now retain their diagnostic message
in the Configuration panel rather than reporting only the exception class.

### Path probing

Select source and destination sites, then click **Probe path**. The observer runs
one ICMP traceroute from the source test host to the destination test host. This
sends real probe traffic but does not change configuration.

The map highlights only known adjacent observed hops. Missing TTL responses or
unrecognized reply addresses produce gaps rather than invented connecting hops.
Hop ownership is refreshed from live `show ip interface brief`, so path display
does not depend on successful running-configuration collection. Expand
**Traceroute details** for raw output and unmapped addresses. Probe progress and
errors remain visible across automatic refreshes. The timestamp is when the probe was
taken; it is not a continuously refreshed route assertion. Run another probe
after a routing change. One probe per TTL cannot enumerate every ECMP path or
prove the exact path of a separate iperf flow. This version does not do packet
capture or cross-interface packet correlation.

Manual probes generate started/completed/failed observer events in the execution
timeline, even when no repair trial is running. These are labeled as probe
activity; a path probe does not start the LLM, Batfish, or a repair experiment.

## Collection and experiment overhead

Defaults are two seconds for counter collection and UI updates, ten seconds for
container/resource/OSPF status, and thirty seconds for running configurations.
Actual collection duration may extend the interval. Each observation displays
its own timestamp; configuration and neighbor samples may be older than counter
samples. Manual traceroutes are the only network probes initiated by this app.
The observer never launches the 110-pair ping sweep or iperf3 automatically.

For lower-frequency observation:

```bash
sudo .venv/bin/python -m advnet.visualizer \
  --interval 5 --status-interval 20 --config-interval 60
```

Counter reads, CLI calls, and Docker stats have overhead. Compare matched trials
with the same observer settings. For timing-sensitive benchmark collection, use
files-only mode and avoid live telemetry/probes rather than assuming the observer
has zero effect on the emulation.

The live observer shows Mbps rather than estimating utilization from the source
topology's capacity label. It does not independently discover effective tc rates.
Recorded trial utilization is displayed only when present in the experiment's
measurements, where shaping and the actual denominator are recorded.

## Replay and files-only operation

Live samples and manually requested paths are appended under
`runs/visualizer/<session>.jsonl`. These files are not experiment result files,
and the existing trial summarizer does not ingest them. `--no-record` disables
observer recording without stopping collection. **Pause view** freezes the browser
view only; collection and recording continue.

Choose an observer session or experiment in **Replay observations**, click Load,
and scrub or play checkpoints. Return to current leaves replay. Playback advances
one checkpoint per second; it does not claim to reproduce original wall-clock
spacing. Gaps in data are not interpolated. Experiment replay uses saved measurement
checkpoints; observer replay has higher-frequency samples. The loader reads a
bounded tail (up to 12 MiB / 5,000 records) of long observer sessions.

Model candidates and outcomes are revealed according to artifact availability
times during experiment replay. A final outcome checkpoint includes the completed
trial result. Router configurations from trial snapshots are recorded state;
files-only baseline configurations come from source files and are labeled as such.

To watch journals/replay without accessing Docker or generating any traffic:

```bash
.venv/bin/python -m advnet.visualizer --files-only
```

Run the normal experiment command in a separate terminal. The runner now records
stage-start events as well as completion/error events, so long model and verifier
calls appear in progress. Historical journals without stage starts still work.
The dashboard reads them without starting or modifying a trial.

## Synthetic demonstration

```bash
.venv/bin/python -m advnet.visualizer --demo
```

This needs neither Docker nor API credentials. It cycles through a simulated
congestion repair, including a candidate OSPF cost change, a synthetic verifier
result, and synthetic traffic. Click Probe path, or load the synthetic recording
to replay the reroute through Denver. Demo data stays in memory and is not saved
as measured experiment results. The banner always identifies the synthetic mode.

## Verification and implementation

```bash
python3 -m unittest discover -s tests -v
node --check advnet/web/app.js
node scripts/check-visualizer-browser.mjs
```

Backend tests cover HTTP responses and SSE using a socketless transport, replay
and diff reconstruction, partial journals, source labeling, probe bounds, missing
hops, and path traversal/cross-origin rejection. The optional browser regression
script requires Node and Chromium; it starts its own temporary demo server on
port 18766, checks path highlighting, timeline events, persistent errors, and
replay, then stops its owned processes. It makes no Docker or model calls.
Headless Chromium checks have passed, and the live Seattle-to-New-York trace has
been verified through the dashboard API. Additional live telemetry and repair
experiments still require Docker access.

- `advnet/visualizer.py`: local HTTP server, SSE snapshots, demo, replay orchestration.
- `advnet/observer.py`: Docker state, read-only EOS sampling, counters, ICMP traceroute.
- `advnet/visualizer_data.py`: allowlisted artifact loading and candidate diffs.
- `advnet/web/`: browser UI, SVG topology, inspector, timeline, and replay controls.

Protocol reference: https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events
