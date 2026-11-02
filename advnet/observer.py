"""Read-only Docker telemetry and explicitly requested ICMP traceroutes.

This module never calls Lab.replace/shape, changes routes, or runs an LLM.
"""
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .lab import Deadline, Lab, full_neighbors


def now():
    return datetime.now(timezone.utc).isoformat()


def rates(previous, current, elapsed):
    if previous is None or elapsed <= 0:
        return {}
    output={}
    for node,interfaces in current.items():
        for interface,value in interfaces.items():
            old=previous.get(node,{}).get(interface)
            output[f'{node}:{interface}']=(value-old)*8/elapsed/1e6 if old is not None and value>=old else None
    return output


def interface_addresses(text):
    """Read interface IPv4 addresses without needing running-config access."""
    from ipaddress import IPv4Address
    addresses=[]
    for value in re.findall(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])',text):
        try:
            addresses.append(str(IPv4Address(value)))
        except ValueError:
            continue
    return list(dict.fromkeys(addresses))


def parse_trace(text, manifest, source, destination):
    ip_map={}
    for node,r in manifest['routers'].items():
        ip_map[r['host_ip']]=r['host']
        ip_map[r['gateway']]=node
    # Transit addresses are supplied by live configs to Monitor.probe as needed.
    ip_map.update(manifest.get('probe_ip_map',{}))
    hops=[]
    for line in text.splitlines():
        match=re.match(r'^\s*(\d+)\s+(.+)$',line)
        if not match:
            continue
        addresses=re.findall(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])',match[2])
        hops.append({'ttl':int(match[1]),'addresses':addresses,
                     'nodes':list(dict.fromkeys(ip_map.get(ip) for ip in addresses if ip in ip_map)),
                     'raw':line.strip()})
    observed=[manifest['routers'][source]['host']]
    for hop in hops:
        for node in hop['nodes']:
            if node!=observed[-1]:
                observed.append(node)
    target_ip=manifest['routers'][destination]['host_ip']
    complete=bool(hops and target_ip in hops[-1]['addresses'])
    edges=[]
    previous_node=manifest['routers'][source]['host']
    previous_ttl=0
    for hop in hops:
        if len(hop['nodes'])==1:
            node=hop['nodes'][0]
            if hop['ttl']==previous_ttl+1 and previous_node!=node:
                edges.append([previous_node,node])
            previous_node,previous_ttl=node,hop['ttl']
        else:
            previous_node,previous_ttl=None,-100
    return {'source':source,'destination':destination,'hops':hops,'nodes':observed,'edges':edges,
            'complete':complete,'sampled_at':now(),'raw':text,
            'evidence':'ICMP traceroute: one probe per TTL; not individual packet tracking.'}


class Monitor:
    def __init__(self,manifest,status_interval=10,config_interval=30):
        self.lab=Lab(manifest)
        self.manifest=manifest
        self.previous=None
        self.counter_time=None
        self.status_time=0
        self.config_time=0
        self.nodes={n:{'status':'unknown','full_neighbors':[],'config':None} for n in self.lab.routers}
        self.hosts={r['host']:{'status':'unknown'} for r in self.lab.routers.values()}
        self.status_interval=status_interval
        self.config_interval=config_interval

    def _states(self):
        names=[self.lab.prefix+n for n in [*self.nodes,*self.hosts]]
        p=subprocess.run(['docker','inspect',*names],capture_output=True,text=True,timeout=8)
        try:
            records=json.loads(p.stdout)
        except json.JSONDecodeError:
            records=[]
        states={r['Name'].lstrip('/'):r['State'] for r in records}
        for name,node in {**self.nodes,**self.hosts}.items():
            state=states.get(self.lab.prefix+name)
            node['status']=('running' if state.get('Running') else state.get('Status','unknown')) if state else 'unknown'
            node['status_at']=now()
        if p.returncode and not states:
            raise RuntimeError('Docker unavailable or lab not deployed')

    def sample(self):
        errors=[]
        monotonic=time.monotonic()
        if monotonic-self.status_time >= self.status_interval:
            try:
                self._states()
                active=[n for n in self.nodes if self.nodes[n]['status']=='running']
                def one(node):
                    try:
                        text=self.lab.cli(node,'show ip ospf neighbor',Deadline(5))
                        return node,text,None
                    except Exception as error:
                        return node,None,type(error).__name__
                with ThreadPoolExecutor(max_workers=4) as pool:
                    for node,text,error in pool.map(one,active):
                        if error:
                            errors.append(f'{node}: neighbor sample unavailable')
                            self.nodes[node]['neighbors_error']=error
                        else:
                            self.nodes[node].update(neighbors=text,full_neighbors=sorted(full_neighbors(text)),neighbors_at=now(),neighbors_error=None)
                p=subprocess.run(['docker','stats','--no-stream','--format','{{json .}}',
                                  *[self.lab.prefix+n for n in active]],capture_output=True,text=True,timeout=6) if active else None
                if p and p.returncode==0:
                    for line in p.stdout.splitlines():
                        stats=json.loads(line)
                        name=stats['Name'].removeprefix(self.lab.prefix)
                        if name in self.nodes:
                            self.nodes[name].update(cpu=stats.get('CPUPerc'),memory=stats.get('MemUsage'),metrics_at=now())
                self.status_time=monotonic
            except Exception as error:
                errors.append('Docker/status collection unavailable ('+type(error).__name__+')')
        if monotonic-self.config_time >= self.config_interval:
            active=[n for n in self.nodes if self.nodes[n]['status']=='running']
            def config_one(node):
                try:
                    value=self.lab.cli(node,'show running-config',Deadline(5))
                    return node,value,None
                except Exception as error:
                    return node,None,{'type':type(error).__name__,'detail':str(error)[-512:]}
            with ThreadPoolExecutor(max_workers=4) as pool:
                for node,text,error in pool.map(config_one,active):
                    if error:
                        self.nodes[node]['config_error']=error['type']
                        self.nodes[node]['config_error_detail']=error['detail']
                    else:
                        self.nodes[node].update(config=text,config_at=now(),config_error=None,config_error_detail=None)
            self.config_time=monotonic
        failed_configs=[n for n,s in self.nodes.items() if s.get('config_error')]
        if failed_configs:
            errors.append(f'Running-config collection failed on {len(failed_configs)} router(s); see Configuration for details.')
        throughput={}
        if all(n['status']=='running' for n in self.nodes.values()):
            try:
                current=self.lab.counters(Deadline(8))
                sampled=time.monotonic()
                throughput=rates(self.previous,current,sampled-self.counter_time if self.counter_time else 0)
                self.previous,self.counter_time=current,sampled
            except Exception as error:
                self.previous,self.counter_time=None,None
                errors.append('Counter collection unavailable ('+type(error).__name__+')')
        else:
            self.previous,self.counter_time=None,None
        links=[]
        for l in self.manifest['links']:
            a,b=self.nodes[l['a']],self.nodes[l['b']]
            unavailable=any(n['status']!='running' for n in (a,b))
            neighbor_unknown=any(not n.get('neighbors_at') or n.get('neighbors_error') for n in (a,b))
            full=(self.lab.routers[l['b']]['router_id'] in a['full_neighbors'] and self.lab.routers[l['a']]['router_id'] in b['full_neighbors'])
            state='unknown' if unavailable or neighbor_unknown else ('up' if full else 'degraded')
            links.append({**l,'state':state,'a_mbps':throughput.get(f'{l["a"]}:{l["a_interface"]}'),
                          'b_mbps':throughput.get(f'{l["b"]}:{l["b_interface"]}'),
                          'utilization':None,'utilization_note':'Measured Mbps only. Effective shaping capacity is not inferred from the topology.'})
        return {'schema_version':1,'kind':'telemetry','sampled_at':now(),'source':'live',
                'nodes':self.nodes,'hosts':self.hosts,'links':links,'errors':errors,
                'status_interval':self.status_interval,'config_interval':self.config_interval}

    def refresh_addresses(self):
        def one(node):
            try:
                text=self.lab.cli(node,'show ip interface brief',Deadline(6))
                return node,interface_addresses(text),None
            except Exception as error:
                return node,[],type(error).__name__
        with ThreadPoolExecutor(max_workers=4) as pool:
            for name,addresses,error in pool.map(one,self.nodes):
                self.nodes[name]['addresses_error']=error
                if not error:
                    self.nodes[name].update(addresses=addresses,addresses_at=now())

    def probe(self,source,destination):
        if source not in self.nodes or destination not in self.nodes or source==destination:
            raise ValueError('Choose two different routers from the inventory')
        # Resolve hops independently of running-config collection.
        self.refresh_addresses()
        ip_map={}
        for node,state in self.nodes.items():
            for ip in state.get('addresses',[]) if not state.get('addresses_error') else []:
                ip_map[ip]=node
            config=state.get('config') or ''
            for ip in re.findall(r'\bip address ((?:\d+\.){3}\d+)/',config):
                ip_map.setdefault(ip,node)
        result=subprocess.run(['docker','exec',self.lab.prefix+self.lab.routers[source]['host'],
                               'traceroute','-I','-n','-m','16','-q','1','-w','1',
                               self.lab.routers[destination]['host_ip']],capture_output=True,text=True,timeout=22)
        text=result.stdout
        if not text.strip():
            raise RuntimeError('Traceroute did not produce an observation')
        value=parse_trace(text,{**self.manifest,'probe_ip_map':ip_map},source,destination)
        value['unmapped_addresses']=sorted({ip for h in value['hops'] for ip in h['addresses'] if not h['nodes']})
        value['address_collection_errors']={n:s['addresses_error'] for n,s in self.nodes.items() if s.get('addresses_error')}
        return value
