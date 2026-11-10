"""Public, scenario-independent repair options shared by both experiment arms."""
import hashlib
import json
from pathlib import Path

from pydantic import TypeAdapter

from .contracts import CatalogAction, Proposal


def experiment_catalog(manifest, path=None):
    if path is not None:
        catalog = TypeAdapter(dict[str, CatalogAction]).validate_json(Path(path).read_text())
    else:
        catalog = {}
        # Inventory only: never read the private fault or its ground-truth fix.
        for node, router in manifest['routers'].items():
            interface = router.get('host_interface')
            if interface is None:
                continue
            catalog[f'enable_host_{node}'] = CatalogAction(
                description=(f'Enable {node} {interface} only if it is administratively shutdown, '
                             'host connectivity is required, and observed failures support this repair. '
                             'Otherwise escalate; this does not repair OSPF or congestion.'),
                proposal=Proposal(
                    outcome='candidate', diagnosis='Candidate recovery of a required host-facing interface.',
                    evidence=['Public topology inventory identifies the required host-facing interface.'],
                    changes=[{'device':node, 'interface':interface,
                              'action':'set_interface_enabled', 'value':True}],
                    expected_effect='Restore the attached host; verification and live checks must confirm it.'))
    if set(catalog) & {'no_change', 'escalate'} or len(catalog) > 253:
        raise ValueError('Catalog reserves no_change/escalate and supports at most 253 actions')
    for action in catalog.values():
        for change in action.proposal.changes:
            if change.device not in manifest['routers'] or change.interface not in manifest['routers'][change.device]['interfaces']:
                raise ValueError('Catalog contains a device/interface outside the manifest')
    return catalog


def catalog_data(catalog):
    return {name:action.model_dump(mode='json') for name,action in catalog.items()}


def catalog_hash(catalog):
    return hashlib.sha256(json.dumps(catalog_data(catalog),sort_keys=True).encode()).hexdigest()
