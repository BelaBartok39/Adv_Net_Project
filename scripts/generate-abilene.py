#!/usr/bin/env python3
"""Deterministically render the normalized Topology Zoo graph into a cEOS lab."""
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def generate():
    source = json.loads((ROOT / 'data/abilene-source.json').read_text())
    by_id = {n['id']: n for n in source['nodes']}
    ports = defaultdict(list)
    links, inventory = [], {}
    for number, (a, b) in enumerate(source['edges']):
        a, b = by_id[a]['router'], by_id[b]['router']
        pa, pb = len(ports[a]) + 1, len(ports[b]) + 1
        ia, ib = f'Ethernet{pa}', f'Ethernet{pb}'
        subnet = number * 4
        ports[a].append((ia, f'10.65.0.{subnet+1}/30', b))
        ports[b].append((ib, f'10.65.0.{subnet+2}/30', a))
        links.append({'a': a, 'a_interface': ia, 'b': b, 'b_interface': ib,
                      'id': f'{a}-{b}', 'cost': 10, 'capacity_mbps': 100})
    nodes, wires = {}, []
    for n in source['nodes']:
        r, index = n['router'], len(inventory) + 1
        hi = f'Ethernet{len(ports[r])+1}'
        host = f'h-{r}'
        inventory[r] = {'site': n['name'], 'router_id': f'10.255.0.{index}',
                        'interfaces': [x[0] for x in ports[r]] + [hi],
                        'transit_interfaces': [x[0] for x in ports[r]],
                        'host_interface': hi, 'host': host, 'host_ip': f'10.64.{index}.10',
                        'gateway': f'10.64.{index}.1', 'host_subnet': f'10.64.{index}.0/24',
                        'mgmt_ip': f'172.30.100.{40+index}'}
        nodes[r] = {'kind': 'arista_ceos', 'image': 'ceos:4.36.3M',
                    'mgmt-ipv4': inventory[r]['mgmt_ip'], 'startup-config': f'configs/abilene/{r}.cfg'}
        nodes[host] = {'kind': 'linux', 'image': 'advnet-host:0.1',
                       'mgmt-ipv4': f'172.30.100.{140+index}', 'cmd': 'sleep infinity',
                       'exec': ['ip link set eth1 up', f'ip address replace 10.64.{index}.10/24 dev eth1',
                                'ip route del default', f'ip route replace 10.64.0.0/16 via 10.64.{index}.1 dev eth1',
                                f'iperf3 -s -B 10.64.{index}.10 -D']}
        cfg = [f'hostname {r}', '!', 'vrf instance MGMT', '!', 'interface Management0',
               '   vrf MGMT', f'   ip address {inventory[r]["mgmt_ip"]}/24', '   no shutdown',
               '!', 'ip routing', 'ip route vrf MGMT 0.0.0.0/0 172.30.100.1', '!']
        for iface, address, peer in ports[r]:
            cfg += [f'interface {iface}', f'   description to-{peer}', '   no switchport',
                    f'   ip address {address}', '   ip ospf area 0.0.0.0',
                    '   ip ospf network point-to-point', '   ip ospf cost 10', '   no shutdown', '!']
        cfg += [f'interface {hi}', f'   description to-{host}', '   no switchport',
                f'   ip address 10.64.{index}.1/24', '   ip ospf area 0.0.0.0', '   no shutdown', '!',
                'router ospf 1', f'   router-id 10.255.0.{index}', '   passive-interface default']
        cfg += [f'   no passive-interface {iface}' for iface, _, _ in ports[r]]
        cfg += ['!', 'end', '']
        (ROOT/f'labs/configs/abilene/{r}.cfg').write_text('\n'.join(cfg))
        wires.append({'endpoints': [f'{r}:eth{len(ports[r])+1}', f'{host}:eth1']})
    for link in links:
        wires.append({'endpoints':[f'{link["a"]}:eth{link["a_interface"][8:]}', f'{link["b"]}:eth{link["b_interface"][8:]}']})
    lab = {'name':'advnet-abilene', 'mgmt':{'network':'advnet-ceos-mgmt', 'ipv4-subnet':'172.30.100.0/24', 'ipv6-subnet':'fd00:ad:100::/64'}, 'topology':{'nodes':nodes, 'links':wires}}
    manifest = {'lab':'advnet-abilene', 'source':source['source_url'], 'routers': inventory, 'links':links,
                'capacity_note':'100 Mbps is an experimental reference, enforced only when traffic shaping is enabled; not a claim about veth capacity.',
                'requirements':['Every test host must reach every other test host over the data network.', 'The MGMT VRF must not be used for experiment traffic.']}
    for path, obj in [('labs/abilene.clab.yml',lab), ('topologies/abilene.json',manifest)]:
        (ROOT/path).write_text(json.dumps(obj, indent=2)+'\n')
    print(f'Rendered {len(inventory)} routers, {len(inventory)} hosts, {len(links)} backbone links.')

if __name__ == '__main__':
    generate()
