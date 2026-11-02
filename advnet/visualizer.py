"""Local, read-only Abilene observatory. No configuration or experiment-control endpoints."""
import argparse
import copy
import json
import math
import mimetypes
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .observer import Monitor, full_neighbors, now
from .visualizer_data import Artifacts

WEB=Path(__file__).with_name('web')


def recorded_frame(manifest,measurement,configs=None,label='Recorded measurement',config_source='recorded configuration'):
    neighbors=measurement.get('ospf_neighbors',{})
    nodes={n:{'status':'recorded','full_neighbors':sorted(full_neighbors(neighbors.get(n,''))),
              'neighbors':neighbors.get(n,''),'neighbors_at':measurement.get('observed_at'),
              'config':(configs or {}).get(n),'config_source':config_source} for n in manifest['routers']}
    traffic=measurement.get('traffic') or {}
    links=[]
    for link in manifest['links']:
        a,b=link['a'],link['b']
        known=a in neighbors and b in neighbors
        full=(manifest['routers'][b]['router_id'] in nodes[a]['full_neighbors'] and manifest['routers'][a]['router_id'] in nodes[b]['full_neighbors'])
        util=traffic.get('link_utilization',{})
        values=[util.get(f'{link[side]}:{link[side+"_interface"]}') for side in ('a','b')]
        values=[v for v in values if isinstance(v,(int,float))]
        links.append({**link,'state':('up' if full else 'degraded') if known else 'unknown',
                      'a_mbps':None,'b_mbps':None,'utilization':max(values) if values else None,
                      'utilization_note':'Recorded experiment sample; see its capacity/shaping record.'})
    hosts={r['host']:{'status':'recorded'} for r in manifest['routers'].values()}
    for n,r in manifest['routers'].items():
        pairs=[p for p in measurement.get('pairs',[]) if p['source']==n]
        if pairs and all(not p['reachable'] for p in pairs):
            hosts[r['host']]['status']='unreachable'
    return {'kind':'telemetry','source':'recorded','sampled_at':measurement.get('observed_at'),
            'nodes':nodes,'hosts':hosts,'links':links,'errors':[],'measurement':measurement,'label':label}


class Demo:
    def __init__(self,manifest,configs):
        self.manifest,self.configs=manifest,configs
        self.start=time.monotonic()

    def frame(self,phase=None):
        phase=(time.monotonic()-self.start)%54 if phase is None else phase
        impaired=10<=phase<32
        healed=phase>=32
        nodes={n:{'status':'running','cpu':'1.8%','memory':'850 MiB / 30.6 GiB',
                  'neighbors':'Synthetic OSPF neighbor sample. This is not live telemetry.',
                  'neighbors_at':now(),'config':text,'config_at':now(),'config_source':'synthetic baseline',
                  'full_neighbors':[]} for n,text in self.configs.items()}
        for link in self.manifest['links']:
            nodes[link['a']]['full_neighbors'].append(self.manifest['routers'][link['b']]['router_id'])
            nodes[link['b']]['full_neighbors'].append(self.manifest['routers'][link['a']]['router_id'])
        if healed:
            nodes['sea']['config']=nodes['sea']['config'].replace('ip ospf cost 10\n','ip ospf cost 100\n',1)
        links=[]
        for index,l in enumerate(self.manifest['links']):
            rate=0.02+0.01*math.sin(phase/3+index)
            utilization=rate/100
            if l['id']=='sea-sva':
                rate=1.92 if impaired else (0.03 if healed else 5)
                utilization=rate/(2 if phase>=10 else 100)
            elif healed and l['id'] in {'sea-den','sva-den'}:
                rate,utilization=5,0.05
            links.append({**l,'state':'up','a_mbps':rate,'b_mbps':rate*0.03,'utilization':utilization})
        return {'kind':'telemetry','source':'demo','sampled_at':now(),'nodes':nodes,
                'hosts':{r['host']:{'status':'running'} for r in self.manifest['routers'].values()},
                'links':links,'errors':[],'phase':phase,
                'measurement':{'mean_rtt_ms':18.4 if impaired else 5.6,'all_reachable':True,'ospf_healthy':True}}

    def experiment(self,phase):
        events=[]
        for t,stage,status in [(0,'measure_healthy','complete'),(10,'inject_capacity_change','complete'),
                               (13,'observe','complete'),(16,'llm_decision','running'),(23,'llm_decision','complete'),
                               (24,'verify_candidate','running'),(27,'verify_candidate','complete'),
                               (29,'deploy','complete'),(32,'post_deploy_convergence','complete'),
                               (36,'measure_candidate','running'),(44,'measure_candidate','complete')]:
            if t<=phase:
                events.append({'stage':stage,'status':status,'at':(datetime.now(timezone.utc)-timedelta(seconds=phase-t)).isoformat(),
                               'elapsed_seconds':2.3 if status=='complete' else 0})
        proposal={'outcome':'candidate','diagnosis':'Synthetic example: the direct Seattle–Sunnyvale link is congested.',
                  'evidence':['Synthetic utilization: 96% on a shaped 2 Mbps link.'],
                  'expected_effect':'Use Seattle → Denver → Sunnyvale while preserving connectivity.',
                  'changes':[{'device':'sea','interface':'Ethernet1','action':'set_ospf_cost','value':100}]}
        attempts=[]
        if phase>=23:
            attempts=[{'id':'attempt-1','decision':{'proposal':proposal,'calls':[{'model':'DEMO / no API call','elapsed_ms':0,'usage':{}}]},
                       'diffs':{'sea':'--- before\n+++ candidate\n@@ Ethernet1 @@\n-   ip ospf cost 10\n+   ip ospf cost 100\n'},
                       'verification':{'accepted':True,'details':['Synthetic verification result.']} if phase>=27 else None,
                       'acceptance':{'accepted':True,'details':['Synthetic runtime acceptance.']} if phase>=44 else None}]
        return {'id':'demo','events':events,'attempts':attempts,'frames':[],
                'result':{'success':True,'attempts':1,'fault_to_acceptance_seconds':34} if phase>=44 else None}

    def probe(self,source,destination,phase=None):
        if source==destination or source not in self.manifest['routers'] or destination not in self.manifest['routers']:
            raise ValueError('Choose different sites')
        phase=self.frame()['phase'] if phase is None else phase
        edges=[(l['a'],l['b']) for l in self.manifest['links'] if phase<32 or l['id']!='sea-sva']
        queue=[[source]]
        seen={source}
        while queue:
            path=queue.pop(0)
            if path[-1]==destination:
                break
            for a,b in edges:
                n=b if a==path[-1] else a if b==path[-1] else None
                if n and n not in seen:
                    seen.add(n);queue.append(path+[n])
        nodes=[self.manifest['routers'][source]['host'],*path,self.manifest['routers'][destination]['host']]
        return {'kind':'path','source':source,'destination':destination,'sampled_at':now(),'nodes':nodes,
                'edges':list(zip(nodes,nodes[1:])),'hops':[],'complete':True,'synthetic':True,
                'raw':'Synthetic demonstration path. No packets were sent.',
                'evidence':'Synthetic demonstration; not a measured traceroute.'}


class App:
    def __init__(self,manifest,root,configs,mode='live',interval=2,status_interval=10,config_interval=30,record=True):
        self.manifest,self.mode,self.interval=manifest,mode,interval
        self.artifacts=Artifacts(root)
        self.configs=configs
        self.monitor=Monitor(manifest,status_interval,config_interval) if mode=='live' else None
        self.demo=Demo(manifest,configs) if mode=='demo' else None
        self.lock=threading.Lock()
        self.record_lock=threading.Lock()
        self.probe_lock=threading.Lock()
        self.stop=threading.Event()
        self.latest={'kind':'telemetry','source':mode,'sampled_at':None,'nodes':{},'hosts':{},'links':[],'errors':[]}
        self.record_path=None
        self.last_path=None
        self.observer_events=[]
        self.probe_status=None
        if mode=='live' and record:
            folder=Path(root)/'visualizer'
            folder.mkdir(parents=True,exist_ok=True)
            name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+str(time.time_ns())[-6:]
            self.record_path=folder/(name+'.jsonl')

    def record(self,value):
        if self.record_path:
            with self.record_lock:
                with self.record_path.open('a') as stream:
                    stream.write(json.dumps(value,allow_nan=False)+'\n')

    def loop(self):
        while not self.stop.is_set():
            started=time.monotonic()
            try:
                frame=self.monitor.sample()
            except Exception as error:
                frame={'kind':'telemetry','source':'live','sampled_at':now(),'nodes':{},'hosts':{},'links':[],
                       'errors':['Telemetry unavailable: '+type(error).__name__]}
            with self.lock:
                self.latest=copy.deepcopy(frame)
            try:
                self.record(frame)
            except OSError:
                with self.lock:
                    self.latest['errors'].append('Telemetry recording could not be written.')
            self.stop.wait(max(0.2,self.interval-(time.monotonic()-started)))

    def state(self):
        runs=self.artifacts.runs()
        check=self.artifacts.latest_check()
        experiment=None
        if self.demo:
            frame=self.demo.frame()
            experiment=self.demo.experiment(frame['phase'])
            runs=[{'id':'demo','label':'Synthetic congestion repair','kind':'experiment','complete':True},*runs]
        elif self.mode=='files':
            frame=recorded_frame(self.manifest,check,self.configs,config_source='baseline source file (not live)') if check else copy.deepcopy(self.latest)
            frame['label']='Saved baseline check; not live'
        else:
            with self.lock:
                frame=copy.deepcopy(self.latest)
        if experiment is None:
            latest=next((r for r in runs if r['kind']=='experiment'),None)
            if latest:
                experiment=self.artifacts.experiment(latest['id'])
        return {'mode':self.mode,'frame':frame,'runs':runs,'last_check':check,'experiment':experiment,
                'path':self.last_path,'recording':self.record_path.name if self.record_path else None,
                'observer_events':copy.deepcopy(self.observer_events),'probe_status':copy.deepcopy(self.probe_status),
                'served_at':now()}

    def probe_event(self,identifier,source,destination,status,started,message):
        event={'kind':'observer_event','id':identifier,'stage':'path_probe','at':now(),
               'source':source,'destination':destination,'status':status,
               'elapsed_seconds':time.monotonic()-started,'message':message}
        with self.lock:
            self.observer_events.append(event)
            self.observer_events=self.observer_events[-100:]
            self.probe_status=event
        try:
            self.record(event)
        except OSError:
            pass

    def probe(self,source,destination):
        if self.mode=='files':
            raise ValueError('Path probing is disabled in files-only mode')
        if source not in self.manifest['routers'] or destination not in self.manifest['routers'] or source==destination:
            raise ValueError('Choose two different sites from the inventory')
        if not self.probe_lock.acquire(blocking=False):
            raise BlockingIOError('A path probe is already running')
        started=time.monotonic()
        identifier='probe-'+str(time.time_ns())
        try:
            self.probe_event(identifier,source,destination,'running',started,'Collecting interface addresses and sending ICMP traceroute probes.')
            value=self.demo.probe(source,destination) if self.demo else self.monitor.probe(source,destination)
            value['kind']='path'
            self.last_path=value
            try:
                self.record(value)
            except OSError:
                pass
            self.probe_event(identifier,source,destination,'complete',started,
                             'Destination reached.' if value.get('complete') else 'Trace ended without confirming the destination.')
            return value
        except Exception as error:
            self.probe_event(identifier,source,destination,'failed',started,
                             'Probe failed: '+type(error).__name__+'. Check Docker access and traceroute availability.')
            raise
        finally:
            self.probe_lock.release()

    def replay(self,name):
        if name=='demo' and self.demo:
            frames=[]
            base=datetime.now(timezone.utc)-timedelta(seconds=54)
            for phase in range(0,54,2):
                f=self.demo.frame(phase)
                f['sampled_at']=(base+timedelta(seconds=phase)).isoformat()
                f['experiment']=self.demo.experiment(phase)
                path=self.demo.probe('sea','sva',phase)
                path['sampled_at']=f['sampled_at']
                f['path']=path
                frames.append(f)
            return {'id':'demo','kind':'telemetry','frames':frames,'paths':[]}
        value=self.artifacts.replay(name)
        if value['kind']=='experiment':
            root=self.artifacts.run(name)
            from .visualizer_data import read_json
            healthy=read_json(self.artifacts.safe(root/'healthy-configs.json'))
            impaired=read_json(self.artifacts.safe(root/'impaired-configs.json'))
            for frame in value['frames']:
                configs=impaired if frame['label']=='Injected impairment' else healthy
                if frame.get('attempt'):
                    a=next((a for a in value['attempts'] if a['id']==frame['attempt']),None)
                    configs=a['candidate_configs'] if a else None
                frame['telemetry']=recorded_frame(self.manifest,frame['measurement'],configs,frame['label'])
        return value


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'

    def log_message(self,*args):
        pass

    def send(self,status,body,kind='application/json'):
        payload=json.dumps(body,allow_nan=False).encode() if kind=='application/json' else body
        self.send_response(status)
        self.send_header('Content-Type',kind+'; charset=utf-8')
        self.send_header('Content-Length',str(len(payload)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        allowed={f'localhost:{self.server.server_port}',f'127.0.0.1:{self.server.server_port}'}
        if self.headers.get('Host') not in allowed:
            self.send(403,{'error':'Use the local dashboard address.'});return
        origin=self.headers.get('Origin')
        if origin and origin not in {'http://'+host for host in allowed}:
            self.send(403,{'error':'Cross-origin access is disabled.'});return
        if self.headers.get('Sec-Fetch-Site') in {'cross-site','same-site'}:
            self.send(403,{'error':'Open the dashboard directly on localhost.'});return
        parsed=urlsplit(self.path)
        query=parse_qs(parsed.query)
        app=self.server.app
        try:
            if parsed.path in {'/','/app.js','/style.css'}:
                file=WEB/({'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[parsed.path])
                self.send(200,file.read_bytes(),mimetypes.guess_type(file.name)[0] or 'text/plain')
            elif parsed.path=='/api/topology':
                self.send(200,app.manifest)
            elif parsed.path=='/api/state':
                self.send(200,app.state())
            elif parsed.path=='/api/replay':
                self.send(200,app.replay(query.get('run',[''])[0]))
            elif parsed.path=='/api/path':
                self.send(200,app.probe(query.get('source',[''])[0],query.get('destination',[''])[0]))
            elif parsed.path=='/api/events':
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.send_header('Cache-Control','no-cache')
                self.send_header('X-Accel-Buffering','no')
                self.end_headers()
                while not app.stop.is_set():
                    self.wfile.write(('data: '+json.dumps(app.state(),allow_nan=False)+'\n\n').encode())
                    self.wfile.flush()
                    if app.stop.wait(2):
                        break
            else:
                self.send(404,{'error':'Not found'})
        except (BrokenPipeError,ConnectionResetError):
            pass
        except BlockingIOError:
            self.send(429,{'error':'Another path probe is running. Try again shortly.'})
        except (ValueError,FileNotFoundError):
            self.send(400,{'error':'Invalid selection, missing recording, or probing unavailable in this mode.'})
        except Exception as error:
            self.send(503,{'error':'Observation unavailable ('+type(error).__name__+'). Check Docker access and lab status.'})

    def do_POST(self):
        self.send(405,{'error':'This dashboard is read-only.'})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,default=Path('topologies/abilene.json'))
    parser.add_argument('--runs',type=Path,default=Path('runs'))
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--interval',type=float,default=2)
    parser.add_argument('--status-interval',type=float,default=10)
    parser.add_argument('--config-interval',type=float,default=30)
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--demo',action='store_true')
    group.add_argument('--files-only',action='store_true')
    parser.add_argument('--no-record',action='store_true')
    args=parser.parse_args()
    if any(not math.isfinite(v) or v<1 for v in (args.interval,args.status_interval,args.config_interval)) or not 1<=args.port<=65535:
        parser.error('Intervals must be at least 1 second, and the port must be valid')
    manifest=json.loads(args.manifest.read_text())
    configs={n:(args.manifest.resolve().parent.parent/'labs/configs/abilene'/f'{n}.cfg').read_text() for n in manifest['routers']}
    mode='demo' if args.demo else 'files' if args.files_only else 'live'
    app=App(manifest,args.runs,configs,mode,args.interval,args.status_interval,args.config_interval,not args.no_record)
    try:
        server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    except OSError as error:
        parser.exit(1,f'Cannot open the local dashboard listener: {error}. Run in your desktop terminal, or choose another --port.\n')
    server.daemon_threads=True
    server.app=app
    worker=None
    if mode=='live':
        worker=threading.Thread(target=app.loop,daemon=True)
        worker.start()
    print(f'Abilene Observatory: http://localhost:{args.port} ({mode}, read-only)',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        server.server_close()
        if worker:
            worker.join(timeout=2)


if __name__=='__main__':
    main()
