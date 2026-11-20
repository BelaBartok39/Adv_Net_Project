"""Small offline model for the benchmark's OSPF/admin faults; not Batfish or a live measurement."""
import heapq
import re


def interface_state(config,interface):
    match=re.search(r'^interface '+re.escape(interface)+r'\n((?:[ \t].*\n)*)',config,re.M)
    if not match:
        raise ValueError('Missing interface '+interface)
    lines=[line.strip() for line in match[1].splitlines()]
    area=next((line.split()[-1] for line in lines if line.startswith('ip ospf area ')),None)
    cost=int(next((line.split()[-1] for line in lines if line.startswith('ip ospf cost ')),'1'))
    return {'up':'shutdown' not in lines,'area':area,'cost':cost}


def predict(manifest,configs):
    states={(n,i):interface_state(configs[n],i) for n,r in manifest['routers'].items() for i in r['interfaces']}
    graph={n:[] for n in manifest['routers']}
    missing=[]
    for link in manifest['links']:
        a,b=states[link['a'],link['a_interface']],states[link['b'],link['b_interface']]
        if not (a['up'] and b['up'] and a['area'] is not None and a['area']==b['area']):
            missing.append(link['id'])
            continue
        graph[link['a']].append((link['b'],a['cost'],link['id']))
        graph[link['b']].append((link['a'],b['cost'],link['id']))
    hosts={n:states[n,r['host_interface']]['up'] for n,r in manifest['routers'].items()}
    unreachable=[]
    for source in graph:
        reached={source}; pending=[source]
        while pending:
            for target,_,_ in graph[pending.pop()]:
                if target not in reached:
                    reached.add(target); pending.append(target)
        unreachable.extend((source,target) for target in graph if target!=source and
                           (not hosts[source] or not hosts[target] or target not in reached))
    return {'graph':graph,'missing_adjacencies':missing,'unreachable_pairs':unreachable,
            'healthy':not missing and not unreachable}


def shortest_paths(graph,source,destination):
    distances={n:float('inf') for n in graph}; distances[source]=0
    previous={n:[] for n in graph}; queue=[(0,source)]
    while queue:
        cost,node=heapq.heappop(queue)
        if cost!=distances[node]: continue
        for target,weight,link in graph[node]:
            value=cost+weight
            if value<distances[target]:
                distances[target]=value; previous[target]=[(node,link)]
                heapq.heappush(queue,(value,target))
            elif value==distances[target]: previous[target].append((node,link))
    def paths(node):
        if node==source: return [[]]
        return [path+[link] for prior,link in previous[node] for path in paths(prior)]
    return paths(destination)
