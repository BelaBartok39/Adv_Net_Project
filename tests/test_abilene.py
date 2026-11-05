import importlib.util
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from advnet.contracts import DeviceState, Observation, Proposal
from advnet.eos import materialize
from advnet.lab import Deadline, Lab, command, full_neighbors
from advnet.trial import Trial, acceptable, snapshot

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=json.loads((ROOT/'topologies/abilene.json').read_text())


def proposal(enabled=True):
    return Proposal(outcome='candidate',diagnosis='A disabled host-facing interface explains the observed loss.',
                    evidence=['The supplied running configuration has shutdown on the host interface.'],
                    changes=[{'device':'sea','interface':'Ethernet3','action':'set_interface_enabled','value':enabled}],
                    expected_effect='Restore host connectivity.')


class FakeLab:
    def __init__(self):
        self.manifest=MANIFEST
        self.state={n:(ROOT/f'labs/configs/abilene/{n}.cfg').read_text() for n in MANIFEST['routers']}
        self.replacements=[]

    def configs(self,*args):
        return dict(self.state)

    def shape(self,*args):
        pass

    def replace(self,configs,*args):
        self.state.update(configs)
        self.replacements.append(dict(configs))

    def wait_convergence(self,*args):
        pass

    def measure(self,*args):
        disabled='\n   shutdown\n' in self.state['sea']
        return {'all_reachable':not disabled,'ospf_healthy':True,'mean_rtt_ms':4.0,
                'traffic':None,'ospf_neighbors':{n:'' for n in self.state},'pairs':[]}

    def observe(self,identifier,measurement,deadline,objective):
        return Observation(scenario_id=identifier,observed_at=datetime.now(timezone.utc),objective=objective,
                           requirements=MANIFEST['requirements'],topology=[],measurements=measurement,
                           devices={n:DeviceState(interfaces=MANIFEST['routers'][n]['interfaces'],running_config=c)
                                    for n,c in self.state.items()})


class FakeDecision:
    def __init__(self,value):
        self.value=value
        self.states=[]

    def decide(self,observation,feedback,attempt,deadline):
        self.states.append(observation.model_dump(mode='json'))
        return {'status':'decision_complete','decision_id':f'd{attempt}',
                'proposal':self.value.model_dump(mode='json'), 'calls':[{'model':'test-only','usage':{}}]}


class FakeVerifier:
    def __init__(self,reject_candidate=False):
        self.reject=reject_candidate
        self.calls=0

    def verify(self,path,deadline):
        self.calls+=1
        accepted=self.calls==1 or not self.reject
        return {'accepted':accepted,'details':['Test-only verifier result']}


class AbileneTests(unittest.TestCase):
    def test_udp_uses_receiver_report_with_text_mode_server_and_safe_datagram_size(self):
        lab=Lab(MANIFEST)
        counters={n:{i:0 for i in r['transit_interfaces']} for n,r in MANIFEST['routers'].items()}
        report={'end':{'sum_received':{'sender':False,'bits_per_second':4_900_000,'lost_percent':0.2}},
                'server_output_text':'Text-mode server output'}
        with patch.object(lab,'counters',return_value=counters), patch('advnet.lab.command',return_value=json.dumps(report)) as run:
            result=lab.traffic({'source':'sea','destination':'nyc','seconds':3,'offered_mbps':5},Deadline(30))
        self.assertEqual(result['received_mbps'],4.9)
        self.assertEqual(result['loss_fraction'],0.002)
        argv=run.call_args.args[0]
        self.assertEqual(argv[argv.index('-l')+1],'1200')

    def test_udp_does_not_substitute_sender_throughput_for_missing_receiver_report(self):
        lab=Lab(MANIFEST)
        counters={n:{i:0 for i in r['transit_interfaces']} for n,r in MANIFEST['routers'].items()}
        for end in ({'sum_sent':{'sender':True,'bits_per_second':5_000_000,'lost_percent':0}},
                    {'sum_received':{'sender':True,'bits_per_second':5_000_000,'lost_percent':0}}):
            with self.subTest(end=end), patch.object(lab,'counters',return_value=counters), patch('advnet.lab.command',return_value=json.dumps({'end':end})):
                with self.assertRaisesRegex(ValueError,'Receiver-side'):
                    lab.traffic({'source':'sea','destination':'nyc','seconds':3,'offered_mbps':5},Deadline(30))

    def test_cli_starts_privileged_instead_of_repeating_command_option(self):
        lab=Lab(MANIFEST)
        with patch.object(lab,'execute',return_value='hostname sea\n') as execute:
            self.assertEqual(lab.cli('sea','show running-config'),'hostname sea\n')
        execute.assert_called_once_with('sea','Cli','-p','15','-M','--abort-on-error','-c','show running-config',deadline=None)

    def test_command_reports_eos_stdout_errors_when_stderr_is_empty(self):
        from types import SimpleNamespace
        with patch('advnet.lab.subprocess.run',return_value=SimpleNamespace(returncode=1,stderr='',stdout='% Invalid input (privileged mode required)')):
            with self.assertRaisesRegex(RuntimeError,'privileged mode required'):
                command(['docker','exec'])

    def test_source_graph_matches_manifest_and_connected(self):
        source=json.loads((ROOT/'data/abilene-source.json').read_text())
        names={n['id']:n['router'] for n in source['nodes']}
        expected={frozenset((names[a],names[b])) for a,b in source['edges']}
        actual={frozenset((l['a'],l['b'])) for l in MANIFEST['links']}
        self.assertEqual((len(MANIFEST['routers']),len(actual)),(11,14))
        self.assertEqual(actual,expected)
        reached={'sea'}
        while True:
            new=reached|{x for edge in actual if edge&reached for x in edge}
            if reached==new:break
            reached=new
        self.assertEqual(reached,set(MANIFEST['routers']))

    def test_all_links_addressing_and_unique_ports(self):
        import ipaddress
        ports=set()
        for l in MANIFEST['links']:
            addresses=[]
            for side in ('a','b'):
                n,i=l[side],l[side+'_interface']
                self.assertNotIn((n,i),ports)
                ports.add((n,i))
                text=(ROOT/f'labs/configs/abilene/{n}.cfg').read_text()
                block=text.split('interface '+i+'\n')[1].split('!')[0]
                address=next(line.strip().split()[-1] for line in block.splitlines() if line.strip().startswith('ip address'))
                addresses.append(ipaddress.ip_interface(address))
            self.assertEqual(addresses[0].network,addresses[1].network)
            self.assertNotEqual(addresses[0].ip,addresses[1].ip)

    def test_materialize_preserves_other_interfaces_and_no_duplicates(self):
        cfg=(ROOT/'labs/configs/abilene/sea.cfg').read_text()
        changed=materialize(cfg,proposal(False).changes)
        self.assertEqual(changed.count('\n   shutdown\n'),1)
        restored=materialize(changed,proposal().changes)
        self.assertEqual(restored,cfg)
        self.assertEqual(materialize(restored,proposal().changes),restored)

    def test_full_neighbor_parser(self):
        self.assertEqual(full_neighbors(' 10.255.0.2 1 default 0 FULL 00:00:30 10.65.0.2 Ethernet1\n10.255.0.3 1 default 0 INIT x'),{'10.255.0.2'})

    def test_snapshot_includes_all_hosts_and_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            lab=FakeLab()
            directory=snapshot(Path(tmp)/'snapshot',lab.configs(),MANIFEST)
            self.assertEqual(len(list((directory/'configs').glob('*.cfg'))),11)
            self.assertEqual(len(list((directory/'hosts').glob('*.json'))),11)
            self.assertEqual(json.loads((directory/'hosts/h-sea.json').read_text())['hostInterfaces']['eth1']['gateway'],'10.64.1.1')

    def trial(self,tmp,decision,verifier,attempts=2):
        lab=FakeLab()
        scenario=json.loads((ROOT/'scenarios/sea-host-interface-down.json').read_text())
        result=Trial(lab,decision,verifier,Path(tmp)/'trial',max_attempts=attempts).run(scenario)
        return lab,result

    def test_successful_closed_loop_and_no_ground_truth_in_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            decision=FakeDecision(proposal())
            lab,result=self.trial(tmp,decision,FakeVerifier())
            self.assertTrue(result['success'])
            self.assertEqual(result['attempts'],1)
            self.assertIsNotNone(result['fault_to_acceptance_seconds'])
            self.assertFalse(result['system_one_enabled'])
            self.assertNotIn('private-scenario',json.dumps(decision.states))
            self.assertTrue(lab.measure()['all_reachable'])

    def test_verification_rejection_never_deploys_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            lab,result=self.trial(tmp,FakeDecision(proposal()),FakeVerifier(True))
            self.assertFalse(result['success'])
            self.assertIsNone(result['fault_to_acceptance_seconds'])
            stages=[e['stage'] for e in result['events']]
            self.assertNotIn('deploy',stages)
            self.assertIn('harness_reset_not_a_repair',stages)
            self.assertTrue(lab.measure()['all_reachable'])

    def test_runtime_rejection_rolls_back_and_duplicate_is_not_redeployed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad=proposal(False)
            lab,result=self.trial(tmp,FakeDecision(bad),FakeVerifier())
            self.assertFalse(result['success'])
            stages=[e['stage'] for e in result['events']]
            self.assertEqual(stages.count('deploy'),1)
            self.assertIn('rollback_candidate',stages)
            self.assertTrue(result['cleanup_succeeded'])
            self.assertTrue(lab.measure()['all_reachable'])

    def test_low_utilization_with_dropped_traffic_is_not_improvement(self):
        base={'all_reachable':True,'ospf_healthy':True,'mean_rtt_ms':4,
              'traffic':{'received_mbps':5,'loss_fraction':0,'max_link_utilization':0.05}}
        impaired=deepcopy(base)
        impaired['traffic']['max_link_utilization']=1
        current=deepcopy(base)
        current['traffic'].update(received_mbps=1,max_link_utilization=0.01)
        self.assertFalse(acceptable(current,base,impaired,'optimization')['accepted'])


if __name__=='__main__':
    unittest.main()
