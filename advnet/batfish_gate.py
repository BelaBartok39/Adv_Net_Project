"""Conservative Batfish gate for the initial all-hosts-reachable routing policy.

Run as: python -m advnet.batfish_gate /absolute/path/to/snapshot
No approval is possible when initialization or any expected flow is missing.
"""
import json
import os
import sys
from pathlib import Path
from uuid import uuid4


def check(directory):
    from pybatfish.client.session import Session
    from pybatfish.datamodel.flow import HeaderConstraints
    manifest=json.loads((directory/'intent.json').read_text())
    bf=Session(host=os.environ.get('BATFISH_HOST','localhost'))
    bf.set_network('advnet-abilene')
    bf.init_snapshot(str(directory),name='candidate-'+uuid4().hex,overwrite=False)
    parsed=bf.q.fileParseStatus().answer().frame()
    if parsed.empty or any(str(v).upper()!='PASSED' for v in parsed['Status']):
        return {'accepted':False,'details':['Snapshot has missing or non-PASSED parsing results. Inspect Batfish initialization.']}
    issues=bf.q.initIssues().answer().frame()
    if not issues.empty:
        return {'accepted':False,'details':['Batfish initialization issues require review: '+str(row) for row in issues.to_dict('records')][:20]}
    actual=set(bf.q.nodeProperties().answer().frame()['Node'])
    hosts={r['host']:r['host_ip'] for r in manifest['routers'].values()}
    if not set(manifest['routers']).union(hosts).issubset(actual):
        return {'accepted':False,'details':['Snapshot did not model every expected router and test host.']}
    failures=[]
    checked=0
    for destination,ip in hosts.items():
        frame=bf.q.traceroute(startLocation='/^h-/',headers=HeaderConstraints(dstIps=ip,ipProtocols=['ICMP']),
                             maxTraces=10000,ignoreFilters=False).answer().frame()
        sources=set()
        for row in frame.itertuples():
            flow=row.Flow
            source=flow.ingressNode
            if source not in hosts or str(flow.srcIp)!=hosts[source] or str(flow.dstIp)!=ip:
                failures.append('Unexpected flow or source address in traceroute result.')
                continue
            sources.add(source)
            traces=row.Traces
            if not traces or row.TraceCount!=len(traces):
                failures.append(f'{source} -> {destination}: missing or truncated traces.')
                continue
            dispositions={str(t.disposition).upper().split('.')[-1] for t in traces}
            if dispositions!={'ACCEPTED'}:
                failures.append(f'{source} -> {destination}: '+', '.join(sorted(dispositions)))
            if source!=destination:
                checked+=1
        if sources!=set(hosts):
            failures.append(f'Missing expected source flows to {destination}.')
    expected=len(hosts)*(len(hosts)-1)
    if checked!=expected:
        failures.append(f'Expected {expected} directed host checks; got {checked}.')
    return {'accepted':not failures,'details':failures or [f'All {expected} directed host flows are ACCEPTED on every modeled path.'],
            'scope':'ICMP all-host reachability; no security-zone policy has been defined yet.'}


def main():
    try:
        value=check(Path(sys.argv[1]))
    except Exception as error:
        print(f'Batfish gate error: {type(error).__name__}: {error}',file=sys.stderr)
        value={'accepted':False,'details':['Batfish was unavailable or returned an invalid result. No deployment approved.']}
    print(json.dumps(value))


if __name__=='__main__':
    main()
