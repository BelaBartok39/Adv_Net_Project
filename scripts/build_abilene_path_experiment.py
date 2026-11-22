"""Build separate Abilene path-policy fixtures; never access live routers.

Run: .venv/bin/python -m scripts.build_abilene_path_experiment --output <new-dir>
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path

from advnet.batfish_gate import host_addresses, path_policies
from advnet.contracts import Proposal
from advnet.eos import candidate_configs
from advnet.trial import dump, snapshot

ROOT = Path(__file__).resolve().parents[1]
DEFINITION = ROOT / 'experiments/abilene-path-policy.json'


def experiment_inputs():
    baseline = json.loads((ROOT / 'topologies/abilene.json').read_text())
    definition = json.loads(DEFINITION.read_text())
    manifest = deepcopy(baseline)
    manifest['path_policies'] = definition['path_policies']
    manifest['requirements'].append(definition['policy_requirement'])
    path_policies(manifest, host_addresses(manifest))
    configs = {node: (ROOT / f'labs/configs/abilene/{node}.cfg').read_text()
               for node in manifest['routers']}
    proposal = Proposal.model_validate(definition['candidate'])
    return baseline, manifest, configs, proposal, definition['scenario']


def build_experiment(directory):
    baseline, manifest, configs, proposal, scenario = experiment_inputs()
    candidate = candidate_configs(configs, proposal)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    dump(directory / 'manifest.json', manifest)
    dump(directory / 'candidate-proposal.json', proposal.model_dump(mode='json'))
    dump(directory / 'scenario.json', scenario)
    snapshot(directory / 'healthy/snapshot', configs, manifest)
    snapshot(directory / 'candidate/snapshot', candidate, manifest)
    snapshot(directory / 'candidate-no-policy/snapshot', candidate, baseline)
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('runs/abilene-path-experiment'),
                        help='New output directory; existing directories are refused')
    args = parser.parse_args()
    try:
        directory = build_experiment(args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Experiment build failed: {error}\n')
    print(f'Built Abilene experiment: {directory.resolve()} (11 routers, 11 hosts)')


if __name__ == '__main__':
    main()
