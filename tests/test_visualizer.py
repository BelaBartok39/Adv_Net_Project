import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from advnet.observer import Monitor, parse_trace, rates
from advnet.visualizer import App, Handler, recorded_frame
from advnet.visualizer_data import Artifacts, read_lines

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=json.loads((ROOT/'topologies/abilene.json').read_text())
CONFIGS={n:(ROOT/f'labs/configs/abilene/{n}.cfg').read_text() for n in MANIFEST['routers']}


class Request:
    """Socketless HTTP request transport; sandbox sockets are intentionally unavailable."""
    def __init__(self,data):
        self.input=io.BytesIO(data)
        self.output=io.BytesIO()
    def makefile(self,*args):
        return self.input
    def sendall(self,data):
        self.output.write(data)


def get(app,path='/',method='GET',host='localhost:8765',headers=''):
    request=Request(f'{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n{headers}\r\n'.encode())
    Handler(request,('127.0.0.1',1234),SimpleNamespace(app=app,server_port=8765))
    header,body=request.output.getvalue().split(b'\r\n\r\n',1)
    return int(header.split()[1]),body,header.decode()


class VisualizerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.app=App(MANIFEST,self.root,CONFIGS,mode='demo')
    def tearDown(self):
        self.tmp.cleanup()

    def test_static_page_and_assets_use_local_resources(self):
        for path in ('/','/app.js','/style.css'):
            status,body,headers=get(self.app,path)
            self.assertEqual(status,200)
            self.assertTrue(body)
            self.assertIn('Content-Security-Policy:',headers)
        self.assertEqual(get(self.app,'/.env')[0],404)
        self.assertEqual(get(self.app,'/../../.env')[0],404)

    def test_demo_state_and_replay_are_explicitly_synthetic(self):
        status,body,_=get(self.app,'/api/state')
        self.assertEqual(status,200)
        state=json.loads(body)
        self.assertEqual(state['mode'],'demo')
        self.assertEqual(state['frame']['source'],'demo')
        self.assertEqual(len(state['frame']['nodes']),11)
        replay=json.loads(get(self.app,'/api/replay?run=demo')[1])
        self.assertEqual(len(replay['frames']),27)
        self.assertTrue(all(f['source']=='demo' for f in replay['frames']))
        self.assertFalse(list(self.root.rglob('*.jsonl')))

    def test_probe_routes_are_inventory_bounded(self):
        response=get(self.app,'/api/path?source=sea&destination=sva')
        self.assertEqual(response[0],200)
        self.assertTrue(json.loads(response[1])['synthetic'])
        self.assertEqual(get(self.app,'/api/path?source=sea;echo&destination=sva')[0],400)
        self.assertEqual(get(self.app,'/api/path?source=sea&destination=sea')[0],400)
        self.assertEqual(get(self.app,'/api/path?source=outside&destination=sva')[0],400)

    def test_probe_is_visible_without_an_llm_trial(self):
        get(self.app,'/api/path?source=sea&destination=sva')
        state=json.loads(get(self.app,'/api/state')[1])
        events=state['observer_events']
        self.assertEqual([e['status'] for e in events],['running','complete'])
        self.assertTrue(all(e['stage']=='path_probe' for e in events))
        self.assertEqual(events[0]['id'],events[1]['id'])
        self.assertEqual(state['probe_status']['status'],'complete')

    def test_failed_probe_is_recorded_and_lock_is_released(self):
        with patch.object(self.app.demo,'probe',side_effect=RuntimeError('test')):
            self.assertEqual(get(self.app,'/api/path?source=sea&destination=sva')[0],503)
        self.assertEqual(self.app.probe_status['status'],'failed')
        self.assertEqual(get(self.app,'/api/path?source=sea&destination=sva')[0],200)

    def test_real_trace_maps_all_routers_without_any_config_sample(self):
        monitor=Monitor(MANIFEST)
        trace=('traceroute to 10.64.11.10 (10.64.11.10), 16 hops max, 60 byte packets\n'
               '1 10.64.1.1 0.199 ms\n2 10.65.0.6 3.354 ms\n3 10.65.0.22 5.114 ms\n'
               '4 10.65.0.30 6.363 ms\n5 10.65.0.46 7.894 ms\n6 10.65.0.50 9.264 ms\n7 10.64.11.10 13.637 ms\n')
        def brief(name,cmd,*args):
            self.assertEqual(cmd,'show ip interface brief')
            return '\n'.join(line for line in CONFIGS[name].splitlines() if 'ip address' in line)
        with patch.object(monitor.lab,'cli',side_effect=brief),patch('advnet.observer.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=trace)):
            value=monitor.probe('sea','nyc')
        self.assertEqual(value['nodes'],['h-sea','sea','den','kcy','ind','chi','nyc','h-nyc'])
        self.assertEqual(value['edges'],list(map(list,zip(value['nodes'],value['nodes'][1:]))))
        self.assertEqual(value['unmapped_addresses'],[])
        self.assertTrue(all(n.get('config') is None for n in monitor.nodes.values()))

    def test_no_configuration_or_cross_origin_endpoints(self):
        self.assertEqual(get(self.app,'/api/deploy',method='POST')[0],405)
        self.assertEqual(get(self.app,'/api/state',host='evil.example')[0],403)
        self.assertEqual(get(self.app,'/api/state',headers='Origin: https://evil.example\r\n')[0],403)
        self.assertEqual(get(self.app,'/api/path?source=sea&destination=sva',headers='Sec-Fetch-Site: cross-site\r\n')[0],403)

    def test_files_mode_uses_recorded_time_and_disables_probes(self):
        measurement={'observed_at':'2026-10-01T10:00:00+00:00','all_reachable':True,
                     'ospf_healthy':True,'ospf_neighbors':{},'pairs':[],'traffic':None}
        (self.root/'abilene-check-one.json').write_text(json.dumps(measurement))
        app=App(MANIFEST,self.root,CONFIGS,mode='files')
        frame=app.state()['frame']
        self.assertEqual(frame['sampled_at'],measurement['observed_at'])
        self.assertEqual(frame['source'],'recorded')
        self.assertEqual(frame['nodes']['sea']['config_source'],'baseline source file (not live)')
        self.assertEqual(get(app,'/api/path?source=sea&destination=sva')[0],400)
        self.assertIsNone(app.monitor)

    def test_counter_rates_reset_or_first_sample_are_unknown(self):
        self.assertEqual(rates(None,{'sea':{'Ethernet1':100}},2),{})
        result=rates({'sea':{'Ethernet1':100}},{'sea':{'Ethernet1':250100}},2)
        self.assertEqual(result['sea:Ethernet1'],1)
        self.assertIsNone(rates({'sea':{'Ethernet1':200}},{'sea':{'Ethernet1':100}},2)['sea:Ethernet1'])

    def test_live_monitor_only_uses_read_commands(self):
        monitor=Monitor(MANIFEST)
        inspected=[{'Name':monitor.lab.prefix+n,'State':{'Running':True}}
                   for n in [*MANIFEST['routers'],*[r['host'] for r in MANIFEST['routers'].values()]]]
        def process(argv,**kwargs):
            if argv[1]=='inspect':
                return SimpleNamespace(returncode=0,stdout=json.dumps(inspected))
            self.assertEqual(argv[1],'stats')
            return SimpleNamespace(returncode=0,stdout='')
        def cli(node,command,*args):
            self.assertIn(command,{'show ip ospf neighbor','show running-config'})
            return CONFIGS[node] if command=='show running-config' else ''
        counters={n:{i:100 for i in r['transit_interfaces']} for n,r in MANIFEST['routers'].items()}
        with patch('advnet.observer.subprocess.run',side_effect=process),patch.object(monitor.lab,'cli',side_effect=cli),patch.object(monitor.lab,'counters',return_value=counters),patch.object(monitor.lab,'replace') as replace,patch.object(monitor.lab,'shape') as shape:
            value=monitor.sample()
            self.assertEqual(value['source'],'live')
            self.assertEqual(len(value['nodes']),11)
            replace.assert_not_called()
            shape.assert_not_called()

    def test_missing_docker_is_unknown_not_healthy(self):
        monitor=Monitor(MANIFEST)
        with patch('advnet.observer.subprocess.run',return_value=SimpleNamespace(returncode=1,stdout='')):
            value=monitor.sample()
        self.assertTrue(value['errors'])
        self.assertTrue(all(l['state']=='unknown' for l in value['links']))

    def test_trace_never_bridges_missing_ttl(self):
        manifest={**MANIFEST,'probe_ip_map':{'10.65.0.5':'sea','10.65.0.9':'den'}}
        result=parse_trace('1 10.64.1.1 1 ms\n2 *\n3 10.65.0.9 2 ms\n4 10.64.2.10 3 ms',manifest,'sea','sva')
        self.assertTrue(result['complete'])
        self.assertNotIn(['sea','den'],result['edges'])
        self.assertIn(['h-sea','sea'],result['edges'])
        self.assertIn(['den','h-sva'],result['edges'])
        # It highlights observed adjacent hops only; graph edges still restrict rendering.

    def test_unknown_neighbor_data_is_not_rendered_as_healthy(self):
        frame=recorded_frame(MANIFEST,{'ospf_neighbors':{},'traffic':None})
        self.assertTrue(all(l['state']=='unknown' for l in frame['links']))

    def test_replay_paths_cannot_escape_run_root(self):
        artifacts=Artifacts(self.root)
        for value in ('../secret','/etc','..','telemetry:../secret'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    artifacts.replay(value)
        outside=self.root.parent/'outside-advnet-observer-test'
        outside.mkdir(exist_ok=True)
        try:
            (self.root/'escape').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(ValueError):
                artifacts.replay('escape')
        finally:
            outside.rmdir()

    def test_partial_jsonl_is_ignored_and_recordings_replay(self):
        folder=self.root/'visualizer';folder.mkdir()
        record=self.app.demo.frame(0)
        file=folder/'test.jsonl'
        file.write_text(json.dumps(record)+'\n{"incomplete":')
        self.assertEqual(len(read_lines(file)),1)
        value=Artifacts(self.root).replay('telemetry:test')
        self.assertEqual(len(value['frames']),1)
        self.assertEqual(value['frames'][0]['source'],'demo')

    def test_trial_diffs_and_phase_snapshots_are_reconstructed(self):
        root=self.root/'trial-one';root.mkdir()
        measurement={'observed_at':'2026-11-02T12:00:00+00:00','all_reachable':True,'ospf_healthy':True,'ospf_neighbors':{},'pairs':[]}
        (root/'healthy-measurements.json').write_text(json.dumps(measurement))
        (root/'healthy-configs.json').write_text(json.dumps(CONFIGS))
        (root/'events.jsonl').write_text(json.dumps({'stage':'llm_decision','at':measurement['observed_at'],'status':'running'})+'\n')
        attempt=root/'attempt-1';(attempt/'snapshot/configs').mkdir(parents=True)
        (attempt/'checkpoint.json').write_text(json.dumps({'sea':CONFIGS['sea']}))
        candidate=CONFIGS['sea'].replace('ip ospf cost 10\n','ip ospf cost 100\n',1)
        (attempt/'snapshot/configs/sea.cfg').write_text('! device: sea\n'+candidate)
        (attempt/'decision.json').write_text(json.dumps({'created_at':measurement['observed_at'],'proposal':{'diagnosis':'Test'}}))
        (root/'result.json').write_text(json.dumps({'success':True,'scenario':'test','attempts':1}))
        value=Artifacts(self.root).experiment('trial-one')
        self.assertEqual(value['id'],'trial-one')
        self.assertIn('+   ip ospf cost 100',value['attempts'][0]['diffs']['sea'])
        self.assertTrue(value['frames'][-1]['final'])
        replay=self.app.replay('trial-one')
        self.assertEqual(replay['frames'][0]['telemetry']['source'],'recorded')

    def test_sse_emits_json_without_a_socket(self):
        class StopAfterOne:
            def is_set(self): return False
            def wait(self,seconds): return True
        self.app.stop=StopAfterOne()
        status,body,headers=get(self.app,'/api/events')
        self.assertEqual(status,200)
        self.assertIn('text/event-stream',headers)
        self.assertTrue(body.startswith(b'data: '))
        self.assertEqual(json.loads(body.decode().strip()[6:])['mode'],'demo')


if __name__=='__main__':
    unittest.main()
