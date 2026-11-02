"""Containerlab cEOS observation, measurement, and bounded configuration transport."""
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from .contracts import DeviceState, Observation


class Deadline:
    def __init__(self, seconds):
        if not 0 < seconds <= 86400:
            raise ValueError('Deadline must be between zero and 86400 seconds')
        self.end = time.monotonic() + seconds

    def remaining(self):
        value = self.end - time.monotonic()
        if value <= 0:
            raise TimeoutError('Experiment deadline exceeded')
        return value


def command(args, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        detail=result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f'Command failed ({result.returncode}): {args[0]}: {detail[-1500:]}')
    return result.stdout


def full_neighbors(text):
    return {line.split()[0] for line in (x.strip() for x in text.splitlines())
            if re.match(r'^\d+\.\d+\.\d+\.\d+\s', line) and re.search(r'\bFULL\b', line)}


class Lab:
    def __init__(self, manifest):
        self.manifest = manifest
        self.routers = manifest['routers']
        self.prefix = 'clab-' + manifest['lab'] + '-'
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', self.prefix):
            raise ValueError('Invalid lab name')
        self.capacities = {l['id']: l['capacity_mbps'] for l in manifest['links']}
        self.shaped = False

    def execute(self, node, *args, deadline=None):
        if node not in self.routers and node not in {r['host'] for r in self.routers.values()}:
            raise ValueError('Unknown lab node')
        timeout = min(30, deadline.remaining()) if deadline else 30
        return command(['docker', 'exec', self.prefix + node, *args], timeout=timeout)

    def cli(self, node, cmd, deadline=None):
        # Cli's -c is a single stored option, not a repeatable command sequence.
        # Two -c flags silently discarded enable, leaving privileged reads/writes
        # at level 1. Start at level 15 explicitly, preserving normal EOS AAA.
        output = self.execute(node, 'Cli', '-p', '15', '-M', '--abort-on-error', '-c', cmd, deadline=deadline)
        if re.search(r'(?m)^\s*%\s*(?:Invalid|Error|Incomplete|Ambiguous)', output):
            raise RuntimeError(f'EOS rejected command on {node}: {output[-1000:]}')
        return output

    def configs(self, deadline=None):
        def one(node):
            text = self.cli(node, 'show running-config', deadline)
            # Strip command metadata before the first actual EOS config line.
            lines = text.splitlines()
            return node, '\n'.join(line for line in lines if not line.startswith(('! Command:', '! Time:'))) + '\n'
        with ThreadPoolExecutor(max_workers=4) as pool:
            return dict(pool.map(one, self.routers))

    def neighbors(self, deadline=None):
        with ThreadPoolExecutor(max_workers=4) as pool:
            return dict(pool.map(lambda n: (n, self.cli(n, 'show ip ospf neighbor', deadline)), self.routers))

    def expected_neighbors(self):
        expected = {n:set() for n in self.routers}
        for l in self.manifest['links']:
            expected[l['a']].add(self.routers[l['b']]['router_id'])
            expected[l['b']].add(self.routers[l['a']]['router_id'])
        return expected

    def wait_convergence(self, deadline, seconds=60):
        end = min(time.monotonic()+seconds, deadline.end)
        previous, stable = None, 0
        while time.monotonic() < end:
            # Stable RIBs allow failures to be diagnosed too; "stable" is not "healthy".
            with ThreadPoolExecutor(max_workers=4) as pool:
                current = dict(pool.map(lambda n: (n, self.cli(n, 'show ip route', deadline)), self.routers))
            # Route ages vary, so compare prefixes and next-hops, not raw route text.
            normalized = {n:re.sub(r'\b\d{1,2}:\d{2}:\d{2}\b', '<age>', text) for n,text in current.items()}
            stable = stable+1 if normalized == previous else 0
            if stable >= 2:
                return
            previous = normalized
            time.sleep(min(2, deadline.remaining()))
        raise TimeoutError('Routing did not stabilize inside the convergence window')

    def ping_matrix(self, deadline):
        pairs = [(a,b) for a in self.routers for b in self.routers if a != b]
        def one(pair):
            a,b = pair
            result = subprocess.run(['docker','exec',self.prefix+self.routers[a]['host'],
                                     'ping','-n','-I','eth1','-c','3','-i','0.2','-W','1',self.routers[b]['host_ip']],
                                    capture_output=True,text=True,timeout=min(8,deadline.remaining()))
            loss = re.search(r'([\d.]+)% packet loss',result.stdout)
            rtt = re.search(r'=\s*([\d.]+)/([\d.]+)/([\d.]+)',result.stdout)
            if loss is None:
                raise RuntimeError('Ping did not produce a parseable measurement: '+a+' -> '+b)
            return {'source':a,'destination':b,'reachable':result.returncode==0,
                    'loss_fraction':float(loss[1])/100,'rtt_avg_ms':float(rtt[2]) if rtt else None}
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(one,pairs))

    def counters(self, deadline):
        def one(node):
            paths = [f'/sys/class/net/eth{i[8:]}/statistics/tx_bytes' for i in self.routers[node]['transit_interfaces']]
            text = self.execute(node, 'cat', *paths, deadline=deadline)
            values = [int(x) for x in text.split()]
            if len(values)!=len(paths):
                raise ValueError('Incomplete counter response')
            return node, dict(zip(self.routers[node]['transit_interfaces'],values))
        with ThreadPoolExecutor(max_workers=4) as pool:
            return dict(pool.map(one,self.routers))

    def traffic(self, flow, deadline):
        a,b = flow['source'],flow['destination']
        seconds = flow.get('seconds',5)
        if seconds < 1 or seconds > 60 or not 0 < flow['offered_mbps'] <= 1000:
            raise ValueError('Invalid traffic profile')
        # Counter samples surround the same constant-load measurement each time.
        before = self.counters(deadline)
        start = time.monotonic()
        output = command(['docker','exec',self.prefix+self.routers[a]['host'],'iperf3','-c',self.routers[b]['host_ip'],
                          '-B',self.routers[a]['host_ip'],'-u','-b',f'{flow["offered_mbps"]}M','-t',str(seconds),
                          '-J','--get-server-output'],timeout=min(seconds+15,deadline.remaining()))
        elapsed = time.monotonic()-start
        after = self.counters(deadline)
        data = json.loads(output)
        if data.get('error'):
            raise RuntimeError('Traffic generator failed: '+str(data['error']))
        server = data.get('server_output_json',{}).get('end',{})
        received = server.get('sum_received') or server.get('sum')
        if not received or received.get('sender') is True or 'lost_percent' not in received:
            raise ValueError('Receiver-side UDP results are missing')
        utilizations = {}
        for link in self.manifest['links']:
            for side in ('a','b'):
                n,interface = link[side],link[side+'_interface']
                delta = after[n][interface]-before[n][interface]
                if delta<0:
                    raise ValueError('Interface counter reset during measurement')
                utilizations[f'{n}:{interface}'] = delta*8 / elapsed / (self.capacities[link['id']]*1e6)
        return {'source':a,'destination':b,'offered_mbps':flow['offered_mbps'],
                'duration_seconds':elapsed,'received_mbps':received['bits_per_second']/1e6,
                'loss_fraction':received['lost_percent']/100,
                'link_utilization':utilizations,'max_link_utilization':max(utilizations.values()),
                'capacities_enforced':self.shaped,'link_capacities_mbps':dict(self.capacities)}

    def measure(self, deadline, flow=None):
        neighbors = self.neighbors(deadline)
        expected = self.expected_neighbors()
        matrix = self.ping_matrix(deadline)
        rtts = [p['rtt_avg_ms'] for p in matrix if p['rtt_avg_ms'] is not None]
        return {'observed_at':datetime.now(timezone.utc).isoformat(), 'pairs':matrix,
                'all_reachable':all(p['reachable'] and p['loss_fraction']==0 for p in matrix),
                'mean_rtt_ms':mean(rtts) if rtts else None,
                'ospf_healthy':all(full_neighbors(neighbors[n])==expected[n] for n in self.routers),
                'ospf_neighbors':neighbors,
                'traffic':self.traffic(flow,deadline) if flow else None}

    def observe(self, scenario_id, measurements, deadline, objective):
        configs = self.configs(deadline)
        return Observation(scenario_id=scenario_id,observed_at=datetime.now(timezone.utc),objective=objective,
            requirements=self.manifest['requirements'],
            topology=([f'{l["a"]}:{l["a_interface"]} -- {l["b"]}:{l["b_interface"]}' for l in self.manifest['links']]
                      +[f'{n}:{r["host_interface"]} -- {r["host"]}:eth1' for n,r in self.routers.items()]),
            devices={n:DeviceState(interfaces=r['interfaces'],running_config=configs[n],
                                  telemetry={'ospf_neighbors':measurements['ospf_neighbors'][n],
                                             'host_ip':r['host_ip'],'host_interface':r['host_interface']}) for n,r in self.routers.items()},
            measurements=measurements)

    def replace(self, configs, directory, deadline=None):
        directory = Path(directory)
        directory.mkdir(parents=True,exist_ok=True)
        for node,text in configs.items():
            path = directory / (node+'.cfg')
            path.write_text(text)
            timeout = min(30,deadline.remaining()) if deadline else 30
            command(['docker','cp',str(path),self.prefix+node+':/mnt/flash/advnet-replace.cfg'],timeout)
            self.cli(node,'configure replace flash:advnet-replace.cfg',deadline)

    def shape(self, rates, deadline=None):
        # This sidecar supplies tc without installing packages inside proprietary cEOS images.
        per_node = {n:[] for n in self.routers}
        for link in self.manifest['links']:
            rate = rates.get(link['id'],100)
            if not isinstance(rate,(int,float)) or not 0<rate<=1000:
                raise ValueError('Invalid experiment capacity')
            self.capacities[link['id']] = rate
            for side in ('a','b'):
                iface = 'eth'+link[side+'_interface'][8:]
                per_node[link[side]].append(f'tc qdisc replace dev {iface} root tbf rate {rate}mbit burst 32kb latency 50ms')
        for node,commands in per_node.items():
            command(['docker','run','--rm','--network','container:'+self.prefix+node,'--cap-add','NET_ADMIN',
                     'advnet-host:0.1','sh','-ec','\n'.join(commands)],
                    timeout=min(30,deadline.remaining()) if deadline else 30)
        self.shaped = True
