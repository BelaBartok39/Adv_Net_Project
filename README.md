# Autonomous network repair and traffic optimization

The next milestone is an 11-router Arista cEOS Abilene topology, with 14 backbone
links and one Linux host per site, and a measured LLM-only closed loop before
introducing Jev or Kev. See [Abilene setup and experiment instructions](docs/ABILENE_BASELINE.md).

```bash
sudo docker build -t advnet-host:0.1 labs/hosts
sudo containerlab deploy -t labs/abilene.clab.yml
uv sync --extra openai --extra batfish
sudo .venv/bin/python -m advnet.experiment check --seconds 300
```

Abilene and live API/Batfish integration still need testing on the development
machine. The earlier four-router diamond has already passed live OSPF and host
connectivity checks; see [diamond instructions](labs/DIAMOND.md).

The decision component screens structured observations with Jev, uses a bounded
action catalog or escalates to an LLM, and emits candidates for a separate
verification/deployment controller. See [pipeline instructions and partner
contracts](docs/DECISION_PIPELINE.md).

Try a synthetic demonstration without API credentials:

```bash
uv sync
uv run python -m advnet examples/congestion.json --mock-route avoid_r2_path
uv run python -m advnet examples/congestion.json
```

The first command path selects a catalog action; the second exercises escalation.
These are scripted mocks and synthetic measurements, not live model results.

```bash
uv run python -m unittest discover -s tests -v
```

`advnet.experiment` adds live observation, bounded trials, a conservative Batfish
adapter or external verifier hook, candidate deployment, measurement, rollback,
and data capture. Offline tests use fakes; they are not live network results.
The initial policy scope is all-host routing reachability; the proposal's ACL
security zones and additional traffic evaluations remain future work.

Watch the lab in a browser with the [read-only observatory](docs/VISUALIZER.md):

```bash
sudo .venv/bin/python -m advnet.visualizer
```

Open http://localhost:8765. Use `--files-only` to watch saved experiment data
without Docker access, or `--demo` for an explicitly synthetic demonstration.
