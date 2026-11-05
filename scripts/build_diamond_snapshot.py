#!/usr/bin/env python3
"""Offline Diamond Batfish snapshot without touching baseline configs."""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def disable_interface(text, interface):
    """Replace one interface's explicit enable command, rejecting ambiguous input."""
    blocks = list(re.finditer(rf'(?m)^interface {re.escape(interface)}\n(?:[ \t]+.*\n)*', text))
    if len(blocks) != 1:
        raise ValueError(f'Expected exactly one interface {interface} block')
    block = blocks[0]
    changed, count = re.subn(r'(?m)^([ \t]+)no shutdown$', r'\1shutdown', block.group())
    if count != 1 or any(line.strip() == 'shutdown' for line in block.group().splitlines()):
        raise ValueError(f'Expected exactly one no shutdown command on {interface}')
    return text[:block.start()] + changed + text[block.end():]


def build_snapshot(directory, *, host_a_down=False):
    directory = Path(directory)
    configs = {node: (ROOT / 'labs/configs/diamond' / f'{node}.cfg').read_text()
               for node in ('r1', 'r2', 'r3', 'r4')}
    hosts = {
        'host-a': {'host_ip': '10.10.1.10', 'gateway': '10.10.1.1',
                   'router': 'r1', 'router_interface': 'Ethernet3'},
        'host-b': {'host_ip': '10.10.2.10', 'gateway': '10.10.2.1',
                   'router': 'r4', 'router_interface': 'Ethernet3'},
    }
    if host_a_down:
        # diamond.clab.yml links r1:eth3 to host-a:eth1; r1.cfg confirms
        # Ethernet3 is to-host-a, with gateway address 10.10.1.1/24.
        host = hosts['host-a']
        router = host['router']
        configs[router] = disable_interface(configs[router], host['router_interface'])
    manifest = {
        'lab': 'advnet-diamond', 'routers': {node: {} for node in configs},
        'hosts': hosts,
        'requirements': ['Every test host must reach the other over the data network.',
                         'The MGMT VRF must not be used for test traffic.'],
    }
    # Refuse reuse so a build cannot overwrite a candidate or retain stale files.
    directory.mkdir(parents=True, exist_ok=False)
    (directory / 'configs').mkdir()
    (directory / 'hosts').mkdir()
    for node, text in configs.items():
        (directory / 'configs' / f'{node}.cfg').write_text(
            f'! device: {node} (cEOSLab, EOS-4.36.3M)\n' + text)
    for name, host in hosts.items():
        # Same host JSON format as advnet.trial.snapshot. Only the data NIC is
        # modeled: its gateway is r1/r4 Ethernet3, never the Docker mgmt bridge.
        model = {'hostname': name, 'hostInterfaces': {'eth1': {
            'name': 'eth1', 'prefix': host['host_ip'] + '/24',
            'gateway': host['gateway']}}}
        (directory / 'hosts' / f'{name}.json').write_text(json.dumps(model, indent=2) + '\n')
    (directory / 'intent.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        help='New snapshot directory (must not already exist)')
    parser.add_argument('--host-a-down', action='store_true',
                        help='Disable R1 Ethernet3 only in the generated snapshot')
    args = parser.parse_args()
    output = args.output or Path('runs/diamond-host-a-down' if args.host_a_down else 'runs/diamond-snapshot')
    try:
        directory = build_snapshot(output, host_a_down=args.host_a_down)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Snapshot build failed: {error}\n')
    print(f'Built Diamond snapshot: {directory.resolve()} (4 routers, 2 hosts)')


if __name__ == '__main__':
    main()
