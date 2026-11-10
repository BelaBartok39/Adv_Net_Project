#!/usr/bin/env python3
"""List/validate private benchmark cases without model calls or live network writes."""
import argparse
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from advnet.catalog import experiment_catalog, catalog_hash
from advnet.contracts import Proposal
from advnet.eos import candidate_configs
from advnet.lab import Deadline
from advnet.scenario_model import predict, shortest_paths
from advnet.scenarios import load_scenario
from advnet.trial import ProcessVerifier, snapshot


def model_accepts(manifest,configs,scenario):
    model=predict(manifest,configs)
    if not model['healthy']:
        return False
    if scenario.get('flow'):
        flow=scenario['flow']
        paths=shortest_paths(model['graph'],flow['source'],flow['destination'])
        constrained={link for link,rate in scenario.get('capacities_mbps',{}).items() if rate<flow['offered_mbps']}
        return bool(paths) and all(not constrained.intersection(path) for path in paths)
    return True


def validate_case(manifest,configs,catalog,scenario):
    broken=candidate_configs(configs,Proposal.model_validate(scenario['fault'])) if scenario.get('fault') else configs
    if model_accepts(manifest,broken,scenario):
        raise ValueError('Fault did not create an impairment in the offline model')
    reference=candidate_configs(broken,Proposal.model_validate(scenario['reference_repair']))
    if not model_accepts(manifest,reference,scenario):
        raise ValueError('Reference repair did not restore the offline model')
    sufficient=[name for name,action in catalog.items() if model_accepts(manifest,candidate_configs(broken,action.proposal),scenario)]
    expected=scenario['evaluation']
    if set(sufficient)!=set(expected['expected_actions']):
        raise ValueError('Catalog coverage does not match private routing labels')
    model=predict(manifest,broken)
    return broken,reference,{'name':scenario['name'],'family':expected['family'],
            'expected_route':expected['expected_route'],'sufficient_catalog_actions':sufficient,
            'predicted_unreachable_pairs':len(model['unreachable_pairs']),
            'predicted_missing_adjacencies':model['missing_adjacencies'],'offline_validation_passed':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['list','validate'],default='validate',nargs='?')
    parser.add_argument('--suite',type=Path,default=ROOT/'scenarios/benchmark-v1/suite.json')
    parser.add_argument('--profile',default='all')
    parser.add_argument('--batfish',action='store_true',help='Also verify snapshots using local Batfish; no live router or model calls')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    index=json.loads(args.suite.read_text())
    if args.profile not in index['profiles']: parser.error('Unknown profile')
    names=index['profiles'][args.profile]
    entries={entry['name']:entry for entry in index['cases']}
    if args.action=='list':
        for name in names:
            item=entries[name]
            print(f"{item['family']:20} {item['expected_route']:8} {args.suite.parent/item['path']}")
        return 0
    manifest=json.loads((ROOT/'topologies/abilene.json').read_text())
    configs={node:(ROOT/f'labs/configs/abilene/{node}.cfg').read_text() for node in manifest['routers']}
    catalog=experiment_catalog(manifest)
    if index['catalog_sha256']!=catalog_hash(catalog): parser.error('Suite catalog fingerprint is stale')
    verifier=ProcessVerifier([sys.executable,'-m','advnet.batfish_gate'])
    report={'profile':args.profile,'source':'offline source-config model; not measured repair outcomes',
            'model_calls':0,'live_router_writes':0,'cases':[]}
    with tempfile.TemporaryDirectory(prefix='advnet-suite-') as temporary:
        root=Path(temporary)
        if args.batfish:
            healthy=verifier.verify(snapshot(root/'healthy',configs,manifest),Deadline(120))
            if not healthy['accepted']: raise ValueError('Healthy snapshot rejected: '+str(healthy['details']))
        for name in names:
            scenario=load_scenario(args.suite.parent/entries[name]['path'],manifest,catalog)
            broken,reference,row=validate_case(manifest,configs,catalog,scenario)
            if args.batfish:
                print('Batfish: '+name,file=sys.stderr,flush=True)
                fixed=verifier.verify(snapshot(root/name/'reference',reference,manifest),Deadline(120))
                impaired=verifier.verify(snapshot(root/name/'impaired',broken,manifest),Deadline(120))
                if not fixed['accepted']: raise ValueError(name+': reference rejected: '+str(fixed['details']))
                # Batfish checks reachability, while runtime also checks OSPF.
                expected_reachable=row['predicted_unreachable_pairs']==0
                if impaired['accepted']!=expected_reachable:
                    raise ValueError(name+': impaired Batfish result disagrees with expected host reachability')
                row.update(batfish_reference_accepted=fixed['accepted'],batfish_impaired_accepted=impaired['accepted'])
            report['cases'].append(row)
    report['case_count']=len(report['cases'])
    report['families']=dict(Counter(row['family'] for row in report['cases']))
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({key:value for key,value in report.items() if key!='cases'},indent=2))
    return 0


if __name__=='__main__': sys.exit(main())
