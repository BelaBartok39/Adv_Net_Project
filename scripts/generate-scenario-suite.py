#!/usr/bin/env python3
"""Generate benchmark-v1 deterministically from public Abilene inventory."""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from advnet.catalog import experiment_catalog, catalog_hash
from advnet.contracts import Proposal
from advnet.scenario_model import interface_state


def generate():
    manifest=json.loads((ROOT/'topologies/abilene.json').read_text())
    configs={n:(ROOT/f'labs/configs/abilene/{n}.cfg').read_text() for n in manifest['routers']}
    catalog=experiment_catalog(manifest)
    digest=catalog_hash(catalog)
    links={link['id']:link for link in manifest['links']}
    directory=ROOT/'scenarios/benchmark-v1'; directory.mkdir(exist_ok=True)
    cases=[]

    def change(node,interface,action,value):
        return {'device':node,'interface':interface,'action':action,'value':value}

    def host(node):
        return change(node,manifest['routers'][node]['host_interface'],'set_interface_enabled',False)

    def backbone(link,action='set_interface_enabled',value=False):
        edge=links[link]
        return change(edge['a'],edge['a_interface'],action,value)

    def proposal(changes):
        return Proposal(outcome='candidate',diagnosis='Private harness definition; never model input.',
                        evidence=['Private experiment control.'],changes=changes,
                        expected_effect='Create or reverse the controlled impairment.').model_dump(mode='json')

    def inverse(changes):
        result=[]
        for item in changes:
            state=interface_state(configs[item['device']],item['interface'])
            value={'set_interface_enabled':state['up'],'set_ospf_area':state['area'],'set_ospf_cost':state['cost']}[item['action']]
            result.append(change(item['device'],item['interface'],item['action'],value))
        return result

    def add(name,family,changes,expected='escalate',actions=None,flow=None,rates=None,repair=None):
        performance=flow is not None
        case={'name':name,'objective':'optimization' if performance else 'repair',
              'objective_text':('Reduce maximum transit-link utilization under unchanged traffic load; preserve delivered throughput, loss, connectivity, and expected OSPF adjacencies.'
                                if performance else 'Restore all required host connectivity and expected OSPF adjacencies while preserving healthy performance.'),
              'evaluation':{'family':family,'expected_route':expected,'expected_actions':actions or [],
                            'catalog_sha256':digest,
                            'explanation':('One host-interface action covers the entire fault.' if expected=='catalog'
                                           else 'No single action in the frozen host-interface catalog repairs the complete impairment.')},
              'reference_repair':proposal(repair if repair is not None else inverse(changes))}
        if changes: case['fault']=proposal(changes)
        if flow: case['flow']=flow
        if rates: case['capacities_mbps']=rates
        (directory/(name+'.json')).write_text(json.dumps(case,indent=2)+'\n')
        cases.append({'name':name,'path':name+'.json','family':family,'expected_route':expected})

    for node in manifest['routers']:
        add(f'routine-host-{node}-down','single_host',[host(node)],'catalog',[f'enable_host_{node}'])
    for link in ('sea-den','lax-hou','kcy-ind','was-nyc'):
        add(f'backbone-{link}-down','backbone_shutdown',[backbone(link)])
    for link in ('sea-sva','den-kcy','hou-atl','chi-nyc'):
        add(f'ospf-area-{link}','ospf_area',[backbone(link,'set_ospf_area','0.0.0.1')])
    for nodes in (('sea','nyc'),('lax','was'),('hou','ind','chi')):
        add('multiple-hosts-'+'-'.join(nodes),'multiple_hosts',[host(node) for node in nodes])
    add('compound-sea-host-and-backbone','compound',[host('sea'),backbone('sea-den')])
    add('compound-nyc-host-and-ospf','compound',[host('nyc'),backbone('chi-nyc','set_ospf_area','0.0.0.1')])
    add('compound-lax-host-and-remote-ospf','compound',[host('lax'),backbone('hou-atl','set_ospf_area','0.0.0.1')])
    for node in ('sea','hou'):
        changes=[change(node,interface,'set_interface_enabled',False) for interface in manifest['routers'][node]['transit_interfaces']]
        add(f'isolate-router-{node}','router_isolation',changes)
    for link in ('sea-sva','hou-atl','chi-nyc'):
        edge=links[link]
        repair=[change(edge[side],edge[side+'_interface'],'set_ospf_cost',1000) for side in ('a','b')]
        add(f'congestion-{link}','congestion',[],flow={'source':edge['a'],'destination':edge['b'],
            'seconds':12,'offered_mbps':5},rates={link:2},repair=repair)
    index={'version':1,'catalog_sha256':digest,'cases':cases,
           'profiles':{'all':[c['name'] for c in cases],
                       'routine':[c['name'] for c in cases if c['expected_route']=='catalog'],
                       'escalation':[c['name'] for c in cases if c['expected_route']=='escalate'],
                       'smoke':['routine-host-sea-down','backbone-sea-den-down','ospf-area-hou-atl',
                                'multiple-hosts-sea-nyc','compound-sea-host-and-backbone','congestion-sea-sva']}}
    (directory/'suite.json').write_text(json.dumps(index,indent=2)+'\n')
    print(json.dumps({'cases':len(cases),'families':dict(Counter(c['family'] for c in cases))},indent=2))


if __name__=='__main__': generate()
