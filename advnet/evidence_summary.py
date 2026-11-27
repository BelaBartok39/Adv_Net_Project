"""Bounded, advisory summaries of existing single-area OSPF evidence.

No scenario labels, fault schedules, reference actions or extra lab queries are
inputs. Estimated router paths are not measured forwarding paths or Batfish.
"""
import heapq
import math
import re


def path_summary(observation):
    result = {'version': 1, 'status': 'unavailable',
              'scope': 'Derived from current configuration, topology and pair measurements only',
              'path_semantics': 'Single-area OSPF router-path estimates; not measured forwarding. '
                                'ICMP RTT includes both directions. Equal-cost estimates do not identify flow hashing.',
              'raw_evidence_retained': True}
    devices = observation['devices']
    measurements = observation['measurements']
    if not 1 < len(devices) <= 32:
        return {**result, 'reason': 'unsupported_inventory_size'}
    if measurements.get('ospf_healthy') is not True or measurements.get('all_reachable') is not True:
        return {**result, 'reason': 'healthy_adjacency_and_reachability_evidence_required'}
    graph = {name: [] for name in devices}
    edges, areas, used = [], set(), set()
    for link in observation['topology']:
        match = re.fullmatch(r'([\w-]+):(Ethernet\d+) -- ([\w-]+):(Ethernet\d+)', link)
        if not match:
            # Host attachments use eth1; they are not backbone graph edges.
            if re.fullmatch(r'[\w-]+:Ethernet\d+ -- [\w-]+:eth1', link):
                continue
            return {**result, 'reason': 'unsupported_topology'}
        a, ai, b, bi = match.groups()
        if a not in devices or b not in devices or a == b or len(edges) >= 128:
            return {**result, 'reason': 'unsupported_topology'}
        for source, interface, target, peer_interface in ((a, ai, b, bi), (b, bi, a, ai)):
            if (source, interface) in used:
                return {**result, 'reason': 'ambiguous_interface_attachment'}
            used.add((source, interface))
            config = devices[source]['running_config']
            blocks = re.findall(r'^interface ' + re.escape(interface) + r'\n((?:[ \t].*\n)*)', config, re.M)
            if len(blocks) != 1:
                return {**result, 'reason': 'ambiguous_interface_configuration'}
            commands = [line.strip() for line in blocks[0].splitlines()]
            costs = [line.split()[-1] for line in commands if line.startswith('ip ospf cost ')]
            area = [line.split()[-1] for line in commands if line.startswith('ip ospf area ')]
            if (len(costs) != 1 or not costs[0].isdigit() or not 1 <= int(costs[0]) <= 65535
                    or len(area) != 1 or 'shutdown' in commands):
                return {**result, 'reason': 'explicit_active_ospf_settings_required'}
            areas.add(area[0])
            edge = {'source': source, 'interface': interface, 'destination': target,
                    'peer_interface': peer_interface, 'cost': int(costs[0])}
            edges.append(edge)
            graph[source].append((target, int(costs[0]), source + ':' + interface))
    if len(areas) != 1 or not edges:
        return {**result, 'reason': 'single_area_topology_required'}

    def paths(source, target):
        # Reverse shortest distances bound enumeration of equal-cost paths.
        distances = {target: 0}
        reverse = {node: [] for node in graph}
        for node, links in graph.items():
            for neighbor, weight, key in links:
                reverse[neighbor].append((node, weight))
        queue = [(0, target)]
        while queue:
            distance, node = heapq.heappop(queue)
            if distances[node] != distance:
                continue
            for previous, weight in reverse[node]:
                candidate = distance + weight
                if candidate < distances.get(previous, math.inf):
                    distances[previous] = candidate
                    heapq.heappush(queue, (candidate, previous))
        found = []

        def visit(node, nodes, links):
            if len(found) >= 5:
                return
            if node == target:
                found.append({'routers': nodes, 'directed_interfaces': links})
                return
            for neighbor, weight, key in sorted(graph[node]):
                if (neighbor in distances and node in distances
                        and distances[node] == weight + distances[neighbor]):
                    visit(neighbor, nodes + [neighbor], links + [key])

        visit(source, [source], [])
        return {'router_cost': distances.get(source), 'paths': found[:4], 'truncated': len(found) > 4}

    raw_pairs = measurements.get('pairs')
    if not isinstance(raw_pairs, list) or len(raw_pairs) > 1024:
        return {**result, 'reason': 'unsupported_pair_measurements'}
    pairs = [p for p in raw_pairs
             if isinstance(p, dict) and p.get('source') in graph and p.get('destination') in graph
             and p['source'] != p['destination'] and type(p.get('rtt_avg_ms')) in (int, float)
             and math.isfinite(p['rtt_avg_ms']) and p['rtt_avg_ms'] >= 0]
    pairs.sort(key=lambda p: (-p['rtt_avg_ms'], p['source'], p['destination']))
    largest = [{'source': p['source'], 'destination': p['destination'], 'rtt_avg_ms': p['rtt_avg_ms'],
                'forward_estimates': paths(p['source'], p['destination']),
                'return_estimates': paths(p['destination'], p['source'])} for p in pairs[:12]]
    return {**result, 'status': 'available', 'ospf_area': next(iter(areas)),
            'directed_backbone_costs': sorted(edges, key=lambda e: (e['source'], e['interface'])),
            'aggregate_mean_rtt_ms': measurements.get('mean_rtt_ms'),
            'aggregate_latency_limit_ms': (measurements.get('performance') or {}).get('latency_limit_ms'),
            'measured_pair_count': len(pairs), 'displayed_pair_count': len(largest),
            'pair_selection': 'Largest measured RTTs, maximum 12; individual pairs are not acceptance failures',
            'largest_measured_pair_rtts': largest}
