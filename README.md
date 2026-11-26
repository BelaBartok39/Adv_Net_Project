# Autonomous network repair and traffic optimization

An 11-router Arista cEOS Abilene lab with 14 backbone links and 11 test hosts.
The LLM-only loop observes, proposes, verifies with Batfish, deploys, and checks
live results with rollback on failure. The Seattle interface repair has passed
end to end in one attempt (93.31 seconds). Optional Jev screening is available
through `--system-one jev`; the default is `--system-one off` (LLM-only).

Run commands from the repository root on the Linux Docker host.

## First-time setup

Requires Docker, Containerlab, uv, and the imported `ceos:4.36.3M` image.

```bash
sudo systemctl start docker
uv sync --extra openai --extra batfish
sudo docker build -t advnet-host:0.1 labs/hosts
sudo docker run -d --name advnet-batfish \
  -p 127.0.0.1:9996:9996 \
  -v advnet-batfish-data:/data batfish/allinone:latest
```

Create the Batfish container once; use `docker start` afterward.

## Start the lab

```bash
sudo systemctl start docker
sudo docker start advnet-batfish
sudo containerlab deploy -t labs/abilene.clab.yml --reconfigure
sudo .venv/bin/python -m advnet.experiment check --seconds 300
```

`--reconfigure` recreates the baseline, including host IPs, routes, and 1500-byte
MTUs, and resets configuration artifacts. Use Containerlab rather than only
`docker start` for routers after a host restart. Allow EOS and Batfish to boot.
The check should report 110 pairs with healthy OSPF and full reachability.

## Environment variables (OpenRouter)

In the terminal used for experiments:

```bash
read -rsp "OpenRouter API key: " OPEN_ROUTER_KEY
printf '\n'
export OPEN_ROUTER_KEY
export LLM_MODEL="nvidia/nemotron-3-ultra-550b-a55b:free"
export LLM_BASE_URL="https://openrouter.ai/api/v1"
export BATFISH_HOST="localhost"
```

Both the LLM and Jev use the same `OPEN_ROUTER_KEY`. `LLM_MODEL` selects the
LLM; `SYSONE_MODEL` independently selects the System One model. `BATFISH_HOST` is a hostname only, without a scheme or port; Batfish uses
port 9996. Variables last for this terminal session. `.env.example` lists them,
but `.env` files are **not loaded automatically**. Keep API keys out of Git.

The pinned model above completed the repair example. Free availability and rate
limits can vary. Use `openrouter/free` for initial testing if needed; it can
select different models on each request.

## End-to-end repair

This disables Seattle's host-facing interface and asks the LLM to repair it.
Deployment requires Batfish approval, followed by live acceptance checks.

```bash
sudo --preserve-env=OPEN_ROUTER_KEY,LLM_MODEL,LLM_BASE_URL,BATFISH_HOST \
  .venv/bin/python -m advnet.experiment run \
  --scenario scenarios/sea-host-interface-down.json \
  --provider openrouter \
  --base-url "$LLM_BASE_URL" \
  --model "$LLM_MODEL" \
  --system-one off \
  --attempts 1
```

Look for `success: true` and `cleanup_succeeded: true`. Results, observations,
model/token usage, verifier reports, and timings are saved in `runs/<trial>/`.
Use up to `--attempts 5` for iteration. OpenRouter is the default provider; `--provider compatible` remains an alias.

## Enable Jev for comparison

Install the optional SDK and select a System One model. Use the same OpenRouter key:

```bash
uv sync --extra openai --extra batfish --extra jev
export SYSONE_MODEL="typesafe/jev-1.14"
```

Repeat the repair command with Jev enabled:

```bash
sudo --preserve-env=OPEN_ROUTER_KEY,LLM_MODEL,LLM_BASE_URL,BATFISH_HOST,SYSONE_MODEL \
  .venv/bin/python -m advnet.experiment run \
  --scenario scenarios/sea-host-interface-down.json \
  --provider openrouter --base-url "$LLM_BASE_URL" --model "$LLM_MODEL" \
  --system-one jev --sysone-model "$SYSONE_MODEL" --jev-threshold 0.9 --attempts 1
```

Switch back with `--system-one off`. Both modes receive the same public action
catalog; remeasure the baseline for a matched comparison with this version.
The first catalog covers host-interface recovery. Complex routing/congestion,
uncertainty, errors, and rejected repairs go to the LLM. Jev candidates still
require Batfish and live checks. The 0.9 threshold is an initial, uncalibrated
setting. Pin a resolved Jev version and the LLM model for reported comparisons.

Export comparison data, including separate Jev/LLM calls, tokens, and times:

```bash
mkdir -p captures
.venv/bin/python scripts/summarize-trials.py runs > captures/comparison.csv
```

Missing usage or prices remain unknown. [System One details](docs/SYSTEM_ONE.md)
describe the catalog, fallback rules, and limitations.

## Fault benchmark

The [30-case fault suite](docs/SCENARIO_SUITE.md) covers routine host failures,
backbone shutdowns, OSPF mismatches, multiple/compound faults, router isolation,
and congestion. It includes private routing labels, reference repairs, and
separate controlled escalation probes.

```bash
.venv/bin/python scripts/scenario-suite.py list --profile smoke
.venv/bin/python scripts/scenario-suite.py validate
```

Use `--reset-baseline` before each benchmark arm, the same scenario/models and
budgets, and toggle `--system-one off|jev`. `--force-escalation` is an explicitly
labelled overhead control that calls Jev before forcing LLM fallback; it is
excluded from routing-accuracy scores. See the linked guide for paired commands.

## Visualizer and traffic

The standalone [stability policy](docs/STABILITY_POLICY.md) provides stateful
repair/optimization timing, cooldowns and configuration-history safeguards for a
future controller. Its tests and reproducible simulation run entirely offline;
it performs no deployment, inference or experiment orchestration.

The [traffic and performance guide](docs/TRAFFIC_PERFORMANCE.md) covers
measurement-only profiles, concurrent UDP trials, automatic stage comparisons,
and optional Linux-only live validation. Existing benchmark scenarios and
single-flow acceptance thresholds remain unchanged.

In a separate terminal, start the dashboard and open **http://localhost:8765**:

```bash
sudo .venv/bin/python -m advnet.visualizer
```

Watch telemetry, probe paths, and follow the trial timeline. To send a 30-second,
5 Mbps UDP flow from Seattle to New York:

```bash
sudo docker exec clab-advnet-abilene-h-sea iperf3 \
  -c 10.64.11.10 -B 10.64.1.10 -u -b 5M -l 1200 -t 30
```

If normal-user commands cannot write files created by sudo:

```bash
sudo chown -R "$(id -u):$(id -g)" runs
```

## Shut down and restart

Stop the visualizer with Ctrl+C. Remove lab containers and stop Batfish:

```bash
sudo containerlab destroy -t labs/abilene.clab.yml
sudo docker stop advnet-batfish
```

Saved results and the Batfish data volume remain. To also stop Docker for the
whole machine:

```bash
sudo systemctl stop docker.service docker.socket
```

Use **Start the lab** next session. To restart only Batfish:

```bash
sudo docker restart advnet-batfish
```

## Tests and details

```bash
.venv/bin/python -m unittest discover -s tests
RUN_BATFISH_INTEGRATION=1 ADVNET_TEST_OUTPUT_DIR=captures/batfish-integration \
  .venv/bin/python -m unittest discover -s tests
```

The second command includes real local Batfish verification. Current policy
coverage is all-host routing reachability; security-zone and congestion-repair
experiments remain separate work.

- [Abilene setup, scenarios, and measured results](docs/ABILENE_BASELINE.md)
- [Visualizer and replay](docs/VISUALIZER.md)
- [Decision pipeline and partner interfaces](docs/DECISION_PIPELINE.md)
- [Earlier Diamond lab](labs/DIAMOND.md)
