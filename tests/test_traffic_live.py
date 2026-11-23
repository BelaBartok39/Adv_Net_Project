"""Opt-in live CLI verification on two Linux hosts; never deploys Abilene/cEOS."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import uuid4


ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('RUN_TRAFFIC_INTEGRATION')=='1',
                     'Set RUN_TRAFFIC_INTEGRATION=1 for the disposable Linux-only traffic test')
class LiveTrafficTests(unittest.TestCase):
    def test_single_and_concurrent_profiles_through_actual_cli(self):
        token=uuid4().hex
        label='traffic-live-'+token[:12]
        names={node:'clab-'+label+'-'+node for node in ('a','b')}
        network=label+'-data'
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+token[:6]
        root=Path(os.environ.get('ADVNET_TEST_OUTPUT_DIR',str(ROOT/'runs')))/('traffic-linux-live-'+stamp)
        root.mkdir(parents=True,exist_ok=False)
        evidence={'source':'Actual Docker/Linux iperf3 and ping through the experiment CLI',
                  'ceos':False,'abilene':False,'batfish':False,'model_calls':False,
                  'shaping':False,'verification_passed':False,'cleanup_succeeded':False}
        def docker(*args,check=True):
            process=subprocess.run(['docker',*args],capture_output=True,text=True,timeout=30)
            if check and process.returncode:
                raise RuntimeError('docker '+' '.join(args)+': '+(process.stderr or process.stdout)[-1200:])
            return process
        def save(path,value):
            path.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n')
        before=set(docker('ps','-q').stdout.split())
        try:
            evidence['image_id']=docker('image','inspect','--format','{{.Id}}','advnet-host:0.1').stdout.strip()
            docker('network','create','--driver','bridge','--label','advnet.traffic-test='+token,network)
            ips={}
            for node,name in names.items():
                docker('run','-d','--name',name,'--network',network,'--cap-add','NET_ADMIN',
                       '--memory','128m','--pids-limit','64','--label','advnet.traffic-test='+token,
                       'advnet-host:0.1','sleep','infinity')
                ips[node]=json.loads(docker('inspect','--format','{{json .NetworkSettings.Networks}}',name).stdout)[network]['IPAddress']
                docker('exec',name,'ip','link','set','dev','eth0','down')
                docker('exec',name,'ip','link','set','dev','eth0','name','eth1')
                docker('exec',name,'ip','link','set','dev','eth1','up')
                docker('exec',name,'iperf3','-s','-B',ips[node],'-D')
                self.assertIn(':5201',docker('exec',name,'ss','-H','-lnt').stdout)
            evidence['iperf_version']=docker('exec',names['a'],'iperf3','--version').stdout
            # Adapter uses Linux endpoint counters in Lab's inventory slots.
            # It does not pretend these containers run EOS or route traffic.
            manifest={'lab':label,'routers':{node:{'host':node,'host_ip':ip,
                       'transit_interfaces':['Ethernet1']} for node,ip in ips.items()},
                      'links':[{'id':'linux-bridge','a':'a','b':'b','a_interface':'Ethernet1',
                                'b_interface':'Ethernet1','capacity_mbps':100}]}
            save(root/'manifest.json',manifest)
            profiles={'single':{'flow':{'source':'a','destination':'b','offered_mbps':5,'seconds':6}},
                      'concurrent':{'flows':[{'source':'a','destination':'b','offered_mbps':5,'seconds':6},
                                             {'source':'b','destination':'a','offered_mbps':3,'seconds':4}]}}
            results={}
            for name,profile in profiles.items():
                save(root/(name+'-profile.json'),profile)
                argv=[sys.executable,'-m','advnet.experiment','traffic',
                      '--manifest',str(root/'manifest.json'),'--traffic-profile',str(root/(name+'-profile.json')),
                      '--output',str(root/name),'--seconds','60']
                process=subprocess.run(argv,cwd=ROOT,capture_output=True,text=True,timeout=90,
                                       env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
                (root/(name+'-cli.log')).write_text(process.stdout+process.stderr)
                self.assertEqual(process.returncode,0,process.stderr)
                results[name]=json.loads((root/name/'measurements.json').read_text())
                self.assertEqual(json.loads((root/name/'evidence.json').read_text())['status'],'collected')
            single=results['single']['traffic']
            self.assertGreater(single['received_mbps'],0)
            self.assertEqual(single['jitter_status'],'measured')
            self.assertIn(single['loaded_rtt']['status'],('measured','partial'))
            self.assertGreater(single['loaded_rtt']['samples_used'],0)
            concurrent=results['concurrent']['traffic']
            self.assertTrue(concurrent['concurrency_verified'])
            self.assertGreater(concurrent['overlap_window']['duration_seconds'],0)
            self.assertGreater(concurrent['aggregate']['received_mbps'],0)
            for flow in concurrent['flows']:
                self.assertGreater(flow['received_mbps'],0)
                self.assertEqual(flow['jitter_status'],'measured')
                self.assertIn('end',json.loads(flow['raw_iperf_json']))
                absent=docker('inspect',flow['client_container'],check=False)
                self.assertNotEqual(absent.returncode,0)
                self.assertIn('no such',absent.stderr)
            evidence.update(verification_passed=True,results=results,
                            limitation='One unshaped Linux bridge run; not Abilene, congestion repair, or optimization evidence. The 100 Mbps utilization denominator is an unenforced reference.')
        except BaseException as error:
            evidence['error']=type(error).__name__+': '+str(error)
            raise
        finally:
            errors=[]
            for name in reversed(list(names.values())):
                owned=docker('inspect','--format','{{ index .Config.Labels "advnet.traffic-test" }}',name,check=False)
                if owned.returncode:
                    if 'No such' not in owned.stderr:
                        errors.append(owned.stderr)
                elif owned.stdout.strip()==token:
                    removed=docker('rm','-f',name,check=False)
                    if removed.returncode:
                        errors.append(removed.stderr)
                else:
                    errors.append('Refusing to remove an unowned container: '+name)
            owned=docker('network','inspect','--format','{{ index .Labels "advnet.traffic-test" }}',network,check=False)
            if not owned.returncode and owned.stdout.strip()==token:
                removed=docker('network','rm',network,check=False)
                if removed.returncode:
                    errors.append(removed.stderr)
            elif owned.returncode and 'not found' not in owned.stderr.lower() and 'no such' not in owned.stderr.lower():
                errors.append(owned.stderr)
            elif not owned.returncode:
                errors.append('Refusing to remove an unowned network: '+network)
            evidence['cleanup_errors']=errors
            evidence['cleanup_succeeded']=not errors
            evidence['preexisting_containers_preserved']=before<=set(docker('ps','-q').stdout.split())
            save(root/'evidence.json',evidence)
            print('\nLinux-only traffic evidence: '+str(root/'evidence.json'),flush=True)
            self.assertFalse(errors,errors)
            self.assertTrue(evidence['preexisting_containers_preserved'])


if __name__=='__main__':
    unittest.main()
