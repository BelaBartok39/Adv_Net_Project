"""Conservative Batfish gate for the initial all-hosts-reachable routing policy.

Run as: python -m advnet.batfish_gate /absolute/path/to/snapshot
No approval is possible when initialization or any expected flow is missing.
"""
import json
import os
import sys
import shutil
import tempfile
from ipaddress import IPv4Address
from numbers import Integral
from pathlib import Path
from uuid import uuid4


def model_config(text):
    """Comment only CLI echo and the exact EOS default L1 guard block.

    These guards reject unsupported physical speed/FEC configuration; they do not
    set routing, interface addresses, shutdown state, or ACL policy. Unknown L1
    commands remain untouched and must still fail the strict parser gate.
    """
    lines = text.splitlines(keepends=True)
    exclusions = []
    for index, line in enumerate(lines):
        if line.strip() == '> show running-config':
            lines[index] = '! EOS CLI echo: ' + line
            exclusions.append('CLI command echo')
        elif line.rstrip('\r\n') == 'system l1':
            end = index + 1
            while end < len(lines) and lines[end].startswith((' ', '\t')):
                end += 1
            if [v.strip() for v in lines[index + 1:end]] == [
                'unsupported speed action error', 'unsupported error-correction action error'
            ]:
                for position in range(index, end):
                    lines[position] = '! EOS physical guard (not modeled): ' + lines[position]
                exclusions.append('Default unsupported-speed/FEC physical configuration guards')
    return ''.join(lines), exclusions


def host_addresses(manifest):
    """Accept separate Diamond hosts and the original Abilene host-per-router form."""
    if 'hosts' in manifest:
        hosts = {name: host['host_ip'] for name, host in manifest['hosts'].items()}
    else:
        entries = list(manifest['routers'].values())
        hosts = {router['host']: router['host_ip'] for router in entries}
        if len(hosts) != len(entries):
            raise ValueError('Duplicate host names in router manifest')
    if not manifest['routers'] or len(hosts) < 2 or set(hosts) & set(manifest['routers']):
        raise ValueError('Manifest requires routers and at least two distinct test hosts')
    if any(not isinstance(name, str) or not name for name in hosts):
        raise ValueError('Invalid host name in manifest')
    hosts = {name: str(IPv4Address(ip)) for name, ip in hosts.items()}
    if len(set(hosts.values())) != len(hosts):
        raise ValueError('Test hosts must have distinct data addresses')
    return hosts


def check(directory):
    try:
        # Preserve the submitted snapshot byte-for-byte. Only the temporary
        # Batfish model input contains explicitly documented exclusions.
        with tempfile.TemporaryDirectory(prefix='advnet-batfish-') as temporary:
            model = Path(temporary) / 'snapshot'
            shutil.copytree(Path(directory), model)
            exclusions = {}
            for path in (model / 'configs').glob('*.cfg'):
                text, omitted = model_config(path.read_text())
                if omitted:
                    path.write_text(text)
                    exclusions[path.name] = omitted
            value = _check(model)
            if exclusions:
                value['model_exclusions'] = exclusions
            return value
    except Exception as error:
        # Include the actual SDK/query/parser error in the machine-readable result.
        # An unsupported query, malformed response or unavailable server rejects.
        return {'accepted': False, 'details': [f'Batfish gate error: {type(error).__name__}: {error}']}


def _check(directory):
    from pybatfish.client.session import Session
    from pybatfish.datamodel.flow import HeaderConstraints
    manifest=json.loads((directory/'intent.json').read_text())
    hosts=host_addresses(manifest)
    network=manifest['lab']
    if not isinstance(network,str) or not network.strip():
        raise ValueError('intent.json must specify a nonempty lab network name')
    bf=Session(host=os.environ.get('BATFISH_HOST','localhost'))
    bf.set_network(network)
    bf.init_snapshot(str(directory),name='candidate-'+uuid4().hex,overwrite=False)
    parsed=bf.q.fileParseStatus().answer().frame()
    if parsed.empty or any(str(v).upper()!='PASSED' for v in parsed['Status']):
        return {'accepted':False,'details':['Snapshot has missing or non-PASSED parsing results: '+str(parsed.to_dict('records'))]}
    issues=bf.q.initIssues().answer().frame()
    if not issues.empty:
        return {'accepted':False,'details':['Batfish initialization issues require review: '+str(row) for row in issues.to_dict('records')][:20]}
    actual=set(bf.q.nodeProperties().answer().frame()['Node'])
    missing=set(manifest['routers']).union(hosts)-actual
    if missing:
        return {'accepted':False,'details':['Snapshot did not model expected nodes: '+', '.join(sorted(missing))]}
    failures=[]
    checked=set()
    for destination,ip in hosts.items():
        expected_sources=set(hosts)-{destination}
        # Exact quoted node names, not an Abilene-specific prefix/regex. Exclude
        # the destination so self-flows cannot satisfy the cross-host policy.
        selector=','.join(json.dumps(name) for name in sorted(expected_sources))
        frame=bf.q.traceroute(startLocation=selector,headers=HeaderConstraints(dstIps=ip,ipProtocols=['ICMP']),
                             maxTraces=10000,ignoreFilters=False).answer().frame()
        sources=set()
        for row in frame.itertuples():
            flow=row.Flow
            source=flow.ingressNode
            if (source not in expected_sources or str(flow.srcIp)!=hosts[source]
                    or str(flow.dstIp)!=ip or str(flow.ipProtocol).upper()!='ICMP'):
                failures.append('Unexpected flow or source address in traceroute result.')
                continue
            if source in sources:
                failures.append(f'{source} -> {destination}: duplicate source flow.')
                continue
            sources.add(source)
            traces=row.Traces
            if (not traces or not isinstance(row.TraceCount,Integral)
                    or isinstance(row.TraceCount,bool) or row.TraceCount!=len(traces)):
                failures.append(f'{source} -> {destination}: missing or truncated traces.')
                continue
            dispositions={str(t.disposition).upper() for t in traces}
            if dispositions!={'ACCEPTED'}:
                failures.append(f'{source} -> {destination}: '+', '.join(sorted(dispositions)))
                continue
            # ACCEPTED must mean delivery to the intended host through modeled
            # routers, rather than local acceptance by a router with a bad IP.
            if any(len(t.hops)<3 or t.hops[0].node!=source or t.hops[-1].node!=destination
                   or any(hop.node not in manifest['routers'] for hop in t.hops[1:-1])
                   for t in traces):
                failures.append(f'{source} -> {destination}: unexpected or missing host/router hops.')
                continue
            checked.add((source,destination))
        if sources!=expected_sources:
            failures.append(f'Missing expected source flows to {destination}.')
    expected=len(hosts)*(len(hosts)-1)
    if len(checked)!=expected:
        failures.append(f'Expected {expected} directed host checks; got {len(checked)}.')
    return {'accepted':not failures,'details':failures or [f'All {expected} directed host flows are ACCEPTED on every modeled path.'],
            'scope':'ICMP all-host reachability; no security-zone policy has been defined yet.'}


def main():
    try:
        value=check(Path(sys.argv[1]))
    except Exception as error:
        print(f'Batfish gate error: {type(error).__name__}: {error}',file=sys.stderr)
        value={'accepted':False,'details':[f'Batfish gate error: {type(error).__name__}: {error}']}
    print(json.dumps(value))


if __name__=='__main__':
    main()
