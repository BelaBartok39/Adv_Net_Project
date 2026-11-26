"""Live Abilene evidence from bounded packet windows and real router readbacks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
import socket
from statistics import mean
from threading import Event
import time
from uuid import uuid4

from .contracts import DeviceState, Observation
from .controller_execution import RuntimeWindow, fingerprint
from .controller_telemetry import Frame
from .controller_traffic import RECEIVER_GRACE_SECONDS, demand_id
from .lab import command, full_neighbors
from .stability_policy import HealthMeasurement, LinkMeasurement, NetworkObservation


def _set_network_namespace(fd):
    if hasattr(os, 'setns'):
        os.setns(fd, os.CLONE_NEWNET)
    else:
        # Python 3.11 on Linux still supports the underlying libc operation.
        from ctypes import CDLL, get_errno
        if CDLL(None, use_errno=True).setns(fd, 0):
            raise OSError(get_errno(), 'Cannot enter network namespace')


def tbf_rates(rows, interfaces):
    matched = {}
    for row in rows:
        if row.get('kind') != 'tbf' or row.get('root') is not True:
            continue
        interface = interfaces.get(row.get('dev'))
        if interface is None:
            continue
        rate = row.get('options', {}).get('rate')  # iproute2 JSON reports bytes/second.
        if type(rate) is int and rate > 0:
            matched[interface] = rate * 8 / 1e6
    return matched


class LabNamespaces:
    """Pin the network namespaces of the lab containers for the session."""
    def __init__(self, descriptors):
        self.descriptors = descriptors

    @classmethod
    @contextmanager
    def open(cls, lab, deadline):
        nodes = (*lab.routers, *(router['host'] for router in lab.routers.values()))
        names = [lab.prefix + node for node in nodes]
        inspected = json.loads(command(['docker', 'inspect', '--type', 'container', *names],
                                       timeout=min(30, deadline.remaining())))
        containers = {value['Name'].removeprefix('/'): value for value in inspected}
        local = os.stat('/proc/thread-self/ns/net')
        seen = {(local.st_dev, local.st_ino)}
        descriptors = {}
        with ExitStack() as resources:
            for node, name in zip(nodes, names):
                state = containers[name]['State']
                pid = state['Pid']
                if state['Running'] is not True or type(pid) is not int or pid <= 0:
                    raise ValueError(f'Lab container is stopped: {name}')
                handle = resources.enter_context(open(f'/proc/{pid}/ns/net', 'rb'))
                info = os.fstat(handle.fileno())
                key = (info.st_dev, info.st_ino)
                if key in seen:
                    raise ValueError(f'Expected a separate lab network namespace: {name}')
                seen.add(key)
                descriptors[node] = handle.fileno()
            yield cls(descriptors)

    def socket_at(self, node):
        with open('/proc/thread-self/ns/net', 'rb') as original:
            _set_network_namespace(self.descriptors[node])
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            finally:
                _set_network_namespace(original.fileno())
        return sock

    def capacities(self, lab, deadline):
        def one(node):
            path = f'/proc/{os.getpid()}/fd/{self.descriptors[node]}'
            rows = json.loads(command(['nsenter', f'--net={path}', '--', 'tc', '-j', 'qdisc', 'show'],
                                      timeout=min(10, deadline.remaining())))
            interfaces = {'eth' + interface[8:]: f'{node}:{interface}'
                          for interface in lab.routers[node]['transit_interfaces']}
            return tbf_rates(rows, interfaces)

        with ThreadPoolExecutor(max_workers=4) as pool:
            return {key: rate for rates in pool.map(one, lab.routers) for key, rate in rates.items()}


@dataclass
class HealthSnapshot:
    opened: datetime
    closed: datetime
    pairs: list
    neighbors: dict
    failed_pairs: list
    mismatched_ospf: list
    mean_rtt_ms: float | None


@dataclass
class LiveSample:
    at: datetime
    start: datetime
    end: datetime
    configs: dict
    health: HealthSnapshot
    links: list
    evidence: dict


class LiveCollector:
    health_probe_workers = 4

    def __init__(self, lab, namespaces, traffic, config_id, scope, clock, seconds):
        self.lab, self.namespaces, self.traffic = lab, namespaces, traffic
        self.config_id, self.scope, self.clock, self.seconds = config_id, scope, clock, seconds
        self.baselines = {}
        self.observations = 0
        self.receiver_windows = 0
        self.expected_ospf = lab.expected_neighbors()
        self.expected_pairs = sorted((a, b) for a in lab.routers for b in lab.routers if a != b)
        self.interfaces = {f'{link[side]}:{link[side + "_interface"]}'
                           for link in lab.manifest['links'] for side in ('a', 'b')}

    def _ports(self, deadline):
        with ThreadPoolExecutor(max_workers=1) as pool:
            caps = pool.submit(self.namespaces.capacities, self.lab, deadline)
            opened = self.clock.monotonic()
            counters = self.lab.counters(deadline)
            closed = self.clock.monotonic()
            return caps.result(), counters, (opened + closed) / 2

    def _health(self, deadline):
        opened = self.clock.now()
        with ThreadPoolExecutor(max_workers=2) as pool:
            # Bound the probe burst into the software forwarding path. Several
            # workers still allow failed pairs to time out inside the window.
            pings = pool.submit(self.lab.ping_matrix, deadline, count=1,
                                workers=self.health_probe_workers)
            neighbors = self.lab.neighbors(deadline)
            pairs = pings.result()
        if sorted((p['source'], p['destination']) for p in pairs) != self.expected_pairs:
            raise ValueError('Incomplete host reachability scope')
        if neighbors.keys() != self.expected_ospf.keys():
            raise ValueError('Incomplete OSPF scope')
        failed = sorted((p['source'], p['destination']) for p in pairs
                        if not p['reachable'] or p['loss_fraction'] != 0)
        observed = {node: full_neighbors(text) for node, text in neighbors.items()}
        mismatch = sorted((node, sorted(self.expected_ospf[node] - peers),
                           sorted(peers - self.expected_ospf[node]))
                          for node, peers in observed.items() if peers != self.expected_ospf[node])
        rtts = [p['rtt_avg_ms'] for p in pairs if p['rtt_avg_ms'] is not None]
        return HealthSnapshot(opened, self.clock.now(), pairs, neighbors, failed, mismatch,
                              mean(rtts) if rtts else None)

    def _sample(self, deadline):
        if deadline.remaining() <= self.seconds + RECEIVER_GRACE_SECONDS:
            raise TimeoutError('Insufficient time for a live measurement window')
        start_configs = self.lab.configs(deadline)
        before, first, first_at = self._ports(deadline)
        if deadline.remaining() <= self.seconds + RECEIVER_GRACE_SECONDS:
            raise TimeoutError('Insufficient time for a live measurement window')
        start = self.clock.now()
        capture = self.traffic.begin(self.seconds)
        measured = False
        error = None
        flows = None
        try:
            health = self._health(deadline)
            self._wait_until(capture.end, deadline)
            after, last, last_at = self._ports(deadline)
            self._wait_until(capture.end + RECEIVER_GRACE_SECONDS, deadline)
            measured = True
        finally:
            try:
                flows = self.traffic.finish(capture, measured=measured)
            except ValueError as failure:
                error = str(failure)
        configs = self.lab.configs(deadline)
        at = self.clock.now()
        end = start + timedelta(seconds=self.seconds)
        port_time = last_at - first_at
        links = []
        for key in sorted(self.interfaces):
            node, interface = key.split(':', 1)
            delta = last[node][interface] - first[node][interface]
            rate = delta * 8 / (port_time * 1e6) if delta >= 0 and port_time > 0 else None
            capacity = after.get(key)
            links.append((key, rate, capacity if capacity == before.get(key) else None))
        utilization = {key: rate / capacity for key, rate, capacity in links
                       if rate is not None and capacity is not None}
        capacity_id = (fingerprint(after) if before == after and set(after) == self.interfaces else None)
        epoch, demand = self.traffic.current()
        profile = demand_id(capture.profile)
        if (epoch, demand) != (capture.epoch, profile):
            error = 'Traffic demand changed before the measurement completed'
        if health.opened < start or health.closed > end or end > at:
            error = 'Health checks were outside the receiver window'
        if self.config_id(start_configs) != self.config_id(configs):
            error = 'Router configuration changed during the measurement'
        if flows and len(utilization) != len(self.interfaces):
            error = 'Effective TBF capacity or interface counters unavailable'
        raw = {'start': start.isoformat(), 'end': end.isoformat(), 'demand_epoch': capture.epoch,
               'demand_id': profile, 'flows': flows, 'link_utilization': utilization,
               'link_capacities_mbps': after, 'counter_interval_seconds': port_time,
               'capacity_sha256': capacity_id, 'config_sha256': self.config_id(configs),
               'error': error}
        return LiveSample(at, start, end, configs, health, links, raw)

    def _wait_until(self, until, deadline):
        while (remaining := until - self.clock.monotonic()) > 0:
            time.sleep(min(0.1, deadline.remaining(), remaining))

    def _runtime(self, sample):
        health, raw = sample.health, sample.evidence
        flows = raw['flows']
        if raw['error'] or flows is None:
            raise ValueError(raw['error'] or 'Receiver evidence unavailable')
        traffic, scope = None, 'confirmed_idle'
        if flows:
            maximum = max(raw['link_utilization'].values())
            scope = 'concurrent_udp' if len(flows) > 1 else 'single_flow'
            aggregate = {'max_link_utilization': maximum, 'link_utilization': raw['link_utilization']}
            if len(flows) == 1:
                traffic = {**flows[0], **aggregate}
            else:
                traffic = {**aggregate, 'mode': scope, 'concurrency_verified': True, 'flows': flows,
                           'overlap_window': {'start_seconds': 0, 'end_seconds': self.seconds}}
        return RuntimeWindow(source='live', scope=self.scope, start=sample.start, end=sample.end,
                             demand_id=raw['demand_id'], demand_epoch=raw['demand_epoch'],
                             config_sha256=raw['config_sha256'], capacity_sha256=raw['capacity_sha256'],
                             all_reachable=not health.failed_pairs, ospf_healthy=not health.mismatched_ospf,
                             mean_rtt_ms=health.mean_rtt_ms, traffic_scope=scope, traffic=traffic)

    def __call__(self, generation, deadline):
        sample = self._sample(deadline)
        at, health, raw = sample.at, sample.health, sample.evidence
        failed, mismatch = health.failed_pairs, health.mismatched_ospf
        health_at = min(health.closed, at)
        reach = HealthMeasurement(observed_at=health_at, measurement_generation=generation,
                                  healthy=not failed, scope_fingerprint=fingerprint([self.scope, failed]),
                                  evidence='Directed host-pair ICMP probes')
        ospf = HealthMeasurement(observed_at=health_at, measurement_generation=generation,
                                  healthy=not mismatch, scope_fingerprint=fingerprint([self.scope, mismatch]),
                                  evidence='Expected FULL OSPF adjacency set per router')
        reference = self.baselines.get(raw['demand_id'])
        performance_check = None
        try:
            if reference is not None:
                performance_check = self._runtime(sample).performance_check(reference)
        except ValueError:
            pass
        performance = HealthMeasurement(
            observed_at=at, measurement_generation=generation,
            healthy=performance_check['accepted'] if performance_check else None,
            scope_fingerprint=fingerprint([self.scope, raw['demand_id'], raw['capacity_sha256']]),
            evidence='Matched-demand baseline RTT and per-flow receiver checks')
        raw['performance'] = {
            'healthy': performance.healthy,
            'details': performance_check['details'] if performance_check else ['Comparable performance evidence unavailable.'],
            'baseline_mean_rtt_ms': reference.mean_rtt_ms if reference else None,
            'latency_limit_ms': reference.mean_rtt_ms * 1.25 + 5
            if reference is not None and reference.mean_rtt_ms is not None else None,
        }
        rates = [LinkMeasurement(link_id=key, observed_at=at, measurement_generation=generation,
                                 throughput_mbps=rate,
                                 throughput_evidence='Linux interface TX byte delta' if rate is not None else None,
                                 effective_capacity_mbps=capacity,
                                 capacity_evidence='Matching live TBF rate at both edges' if capacity else None)
                 for key, rate, capacity in sample.links if raw['flows'] and not raw['error']]
        identifier = uuid4().hex
        signal = NetworkObservation(observation_id=identifier, observed_at=at, links=rates,
                                    reachability=reach, ospf=ospf, performance=performance)
        raw.update(all_reachable=reach.healthy, ospf_healthy=ospf.healthy, mean_rtt_ms=health.mean_rtt_ms,
                   failed_pairs=[list(pair) for pair in failed],
                   mismatched_ospf=[list(adjacency) for adjacency in mismatch])
        inventory = self.lab.routers
        devices = {node: DeviceState(interfaces=router['interfaces'], running_config=sample.configs[node],
                                     telemetry={'ospf_neighbors': health.neighbors[node], 'host_ip': router['host_ip'],
                                                'host_interface': router['host_interface']})
                   for node, router in inventory.items()}
        links = [f'{link["a"]}:{link["a_interface"]} -- {link["b"]}:{link["b_interface"]}'
                 for link in self.lab.manifest['links']]
        links += [f'{node}:{router["host_interface"]} -- {router["host"]}:eth1'
                  for node, router in inventory.items()]
        model = Observation(scenario_id=identifier, observed_at=at, devices=devices, topology=links,
                            objective='Restore connectivity, OSPF and baseline latency/delivery tolerances, '
                            'or relieve sustained congestion. See performance measurements for failures.',
                            requirements=self.lab.manifest['requirements'],
                            measurements={**raw, 'pairs': health.pairs})
        self.observations += 1
        if raw['flows'] and not raw['error']:
            self.receiver_windows += 1
        return Frame('live', signal, model, raw)

    def window(self, phase, kind, deadline):
        if phase == 'healthy':
            _, demand = self.traffic.current()
            return self.baselines[demand]
        return self._runtime(self._sample(deadline))

    def calibrate(self, profiles, baseline_id, deadline):
        for demand, profile in profiles.items():
            try:
                for flow, rate in profile:
                    self.traffic.start(flow, rate, timeout_seconds=deadline.remaining())
                window = self._runtime(self._sample(deadline))
                if (window.config_sha256 != baseline_id or window.demand_id != demand
                        or not window.all_reachable or not window.ospf_healthy
                        or window.mean_rtt_ms is None or not window.delivered_baseline()):
                    raise ValueError('Unable to establish a healthy live reference for demand ' + demand[:12])
                self.baselines[demand] = window
            finally:
                for flow_id in list(self.traffic.active):
                    self.traffic.stop(flow_id, timeout_seconds=5)
