"""Deterministic materialization of the bounded interface patch language."""
import re
from .contracts import Proposal


def materialize(config: str, changes: list) -> str:
    lines = config.splitlines()
    for change in changes:
        header = f'interface {change.interface}'
        starts = [i for i, line in enumerate(lines) if line.rstrip() == header]
        if len(starts) != 1:
            raise ValueError(f'Expected one existing interface block: {change.interface}')
        start = starts[0]
        end = start + 1
        while end < len(lines) and (lines[end].startswith((' ', '\t')) or not lines[end].strip()):
            end += 1
        match change.action:
            case 'set_ospf_cost':
                pattern, command = r'(?:no )?ip ospf cost(?: .*|$)', f'ip ospf cost {change.value}'
            case 'set_ospf_area':
                pattern, command = r'(?:no )?ip ospf area(?: .*|$)', f'ip ospf area {change.value}'
            case 'set_interface_enabled':
                pattern = r'(?:no )?shutdown'
                command = 'no shutdown' if change.value else 'shutdown'
            case _:
                raise ValueError('Unsupported patch')
        body = [line for line in lines[start+1:end] if not re.fullmatch(pattern, line.strip())]
        lines[start+1:end] = body + ['   ' + command]
    return '\n'.join(lines) + '\n'


def candidate_configs(configs: dict[str, str], proposal: Proposal) -> dict[str, str]:
    output = dict(configs)
    for device in {c.device for c in proposal.changes}:
        output[device] = materialize(configs[device], [c for c in proposal.changes if c.device == device])
    return output
