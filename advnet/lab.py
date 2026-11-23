"""Containerlab cEOS observation, measurement, and bounded configuration transport."""
import json
import math
import re
import subprocess
from threading import Event
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


def udp_metrics(data):
    """Extract the existing receiver statistics for either traffic mode."""
    if data.get('error'):
        raise RuntimeError('Traffic generator failed: '+str(data['error']))
    server = data.get('server_output_json',{}).get('end',{})
    received = server.get('sum_received') or server.get('sum') or data.get('end',{}).get('sum_received')
    if not received or received.get('sender') is not False or 'lost_percent' not in received:
        raise ValueError('Receiver-side UDP results are missing')
    # Keep unknown jitter explicit and never substitute sender estimates.
    jitter = received.get('jitter_ms')
    jitter_status = 'missing' if jitter is None else 'measured'
    if jitter is not None and (type(jitter) not in (int, float) or not 0 <= jitter < math.inf):
        jitter, jitter_status = None, 'invalid'
    return {'received_mbps':received['bits_per_second']/1e6,
            'loss_fraction':received['lost_percent']/100,
            'jitter_ms':jitter,'jitter_status':jitter_status}, received


def loaded_rtt(data, samples, elapsed):
    """Use only complete probes inside conservative iperf sender-time bounds.

    If launch=L, completion=E and sender duration=D, its unknown start S obeys
    L <= S <= E-D. Thus [E-D, L+D] lies inside every possible sender interval.
    Shrink both ends by 50 ms to avoid timer/JSON rounding boundaries. A process
    being alive alone is not evidence of traffic, nor is receiver/report time.
    """
    result = {'mean_ms': None, 'min_ms': None, 'max_ms': None,
              'status': 'unavailable', 'reason': None, 'window': None,
              'samples_used': 0, 'samples_failed': 0,
              'samples': [{**sample, 'included': False} for sample in samples]}
    end = data.get('end', {})
    sender = end.get('sum_sent') or end.get('sum')
    if not isinstance(sender, dict) or sender.get('sender') is not True:
        result['reason'] = 'Sender timing is missing; overlap cannot be verified.'
        return result
    duration, start, finish, size = (sender.get(key) for key in ('seconds', 'start', 'end', 'bytes'))
    if (any(type(value) not in (int, float) or not 0 <= value < math.inf
            for value in (duration, start, finish, size))
            or start != 0 or duration <= 0 or size <= 0
            or abs(finish - duration) > 0.001 or duration > elapsed):
        result['reason'] = 'Sender timing is invalid; overlap cannot be verified.'
        return result
    lower, upper = elapsed - duration + 0.05, duration - 0.05
    if lower >= upper:
        result['reason'] = 'Setup/report overhead leaves no verified probing interval.'
        return result
    result['window'] = {'start_seconds': lower, 'end_seconds': upper,
                        'sender_duration_seconds': duration,
                        'method': 'conservative_sender_duration_bounds'}
    values = []
    for sample in result['samples']:
        if lower <= sample['start_seconds'] <= sample['end_seconds'] <= upper:
            if sample['status'] == 'measured':
                sample['included'] = True
                values.append(sample['rtt_ms'])
            else:
                result['samples_failed'] += 1
    if not values:
        result['reason'] = 'No successful complete ping probes in the verified interval.'
        return result
    result.update(mean_ms=mean(values), min_ms=min(values), max_ms=max(values),
                  samples_used=len(values),
                  status='partial' if result['samples_failed'] else 'measured')
    if result['samples_failed']:
        result['reason'] = 'Some ping probes in the verified interval failed; see samples.'
    return result


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
            return node, '\n'.join(line for line in lines
                                   if not line.startswith(('! Command:', '! Time:'))
                                   and line.strip() != '> show running-config') + '\n'
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

    def _sample_loaded_rtt(self, source, destination, deadline, stop, start):
        """One bounded ICMP probe at a time; keep raw results and local timing."""
        samples = []
        argv = ['docker', 'exec', self.prefix + self.routers[source]['host'],
                'ping', '-n', '-I', 'eth1', '-c', '1', '-W', '1', '-w', '1',
                self.routers[destination]['host_ip']]
        while not stop.is_set():
            sample = {'start_seconds': time.monotonic() - start, 'rtt_ms': None,
                      'status': 'error', 'error': None, 'returncode': None,
                      'stdout': '', 'stderr': ''}
            try:
                process = subprocess.run(argv, capture_output=True, text=True,
                                         timeout=min(2, deadline.remaining()))
                sample.update(stdout=process.stdout, stderr=process.stderr,
                              returncode=process.returncode)
                rtt = re.search(r'(?:rtt|round-trip) [^=]*=\s*([\d.]+)/([\d.]+)/([\d.]+)',
                                process.stdout)
                if process.returncode == 0 and rtt:
                    value = float(rtt[2])
                    if not 0 <= value < math.inf:
                        raise ValueError('Ping returned an invalid RTT.')
                    sample.update(rtt_ms=value, status='measured')
                else:
                    sample.update(status='failed', error='Ping failed or returned no valid RTT summary.')
            except (OSError, subprocess.TimeoutExpired, TimeoutError, ValueError) as error:
                sample['error'] = type(error).__name__ + ': ' + str(error)
            sample['end_seconds'] = time.monotonic() - start
            samples.append(sample)
            if stop.wait(0.2):
                break
        return samples

    def _link_utilization(self, before, after, elapsed):
        utilizations = {}
        for link in self.manifest['links']:
            for side in ('a','b'):
                n,interface = link[side],link[side+'_interface']
                delta = after[n][interface]-before[n][interface]
                if delta<0:
                    raise ValueError('Interface counter reset during measurement')
                utilizations[f'{n}:{interface}'] = delta*8 / elapsed / (self.capacities[link['id']]*1e6)
        return utilizations

    def concurrent_traffic(self, flows, deadline):
        """Measure 2–4 UDP clients together; leaves existing single-flow behavior intact."""
        from .concurrent_traffic import run_concurrent_traffic
        return run_concurrent_traffic(self, flows, deadline)

    def traffic(self, flow, deadline):
        a,b = flow['source'],flow['destination']
        seconds = flow.get('seconds',5)
        if seconds < 1 or seconds > 60 or not 0 < flow['offered_mbps'] <= 1000:
            raise ValueError('Invalid traffic profile')
        # Counter samples surround the same constant-load measurement each time.
        before = self.counters(deadline)
        stop = Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            start = time.monotonic()
            try:
                probes = pool.submit(self._sample_loaded_rtt, a, b, deadline, stop, start)
                output = command(['docker','exec',self.prefix+self.routers[a]['host'],'iperf3','-c',self.routers[b]['host_ip'],
                                  '-B',self.routers[a]['host_ip'],'-u','-l','1200','-b',f'{flow["offered_mbps"]}M','-t',str(seconds),
                                  '-J','--get-server-output'],timeout=min(seconds+15,deadline.remaining()))
            finally:
                elapsed = time.monotonic()-start
                stop.set()
            # Read counters before waiting for a final in-flight probe to exit.
            after = self.counters(deadline)
            samples = probes.result()
        data = json.loads(output)
        metrics, _ = udp_metrics(data)
        utilizations = self._link_utilization(before, after, elapsed)
        return {'source':a,'destination':b,'offered_mbps':flow['offered_mbps'],
                'udp_datagram_bytes':1200,'seconds':seconds,'raw_iperf_json':output,
                'duration_seconds':elapsed,**metrics,
                'loaded_rtt':loaded_rtt(data, samples, elapsed),
                'link_utilization':utilizations,'max_link_utilization':max(utilizations.values()),
                'capacities_enforced':self.shaped,'link_capacities_mbps':dict(self.capacities)}

    def measure(self, deadline, flow=None, *, flows=None):
        if flow is not None and flows is not None:
            raise ValueError('Specify either flow or flows, not both')
        neighbors = self.neighbors(deadline)
        expected = self.expected_neighbors()
        matrix = self.ping_matrix(deadline)
        rtts = [p['rtt_avg_ms'] for p in matrix if p['rtt_avg_ms'] is not None]
        return {'observed_at':datetime.now(timezone.utc).isoformat(), 'pairs':matrix,
                'all_reachable':all(p['reachable'] and p['loss_fraction']==0 for p in matrix),
                'mean_rtt_ms':mean(rtts) if rtts else None,
                'ospf_healthy':all(full_neighbors(neighbors[n])==expected[n] for n in self.routers),
                'ospf_neighbors':neighbors,
                'traffic':(self.concurrent_traffic(flows,deadline) if flows is not None
                           else self.traffic(flow,deadline) if flow else None)}

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
