"""Offline workflow integration; network observations and decisions are synthetic."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from advnet.catalog import experiment_catalog
from advnet.contracts import Proposal
from advnet.experiment import main
from advnet.scenarios import load_scenario, load_traffic_profile
from advnet.trial import Trial, acceptable_concurrent
from test_abilene import FakeLab, FakeDecision, FakeVerifier, MANIFEST, proposal


ROOT=Path(__file__).resolve().parents[1]
PROFILE=ROOT/'experiments/concurrent-traffic.json'
SCENARIO=ROOT/'scenarios/concurrent-sea-sva-congestion.json'


def batch_measurement(flows=None, maximum=0.05, impaired=False):
    flows=flows or json.loads(PROFILE.read_text())['flows']
    records=[]
    for flow in flows:
        rate=flow['offered_mbps']
        received=2 if impaired and flow['source']=='sea' else rate
        records.append({**flow,'destination_port':flow.get('destination_port',5201),
                        'received_mbps':received,'loss_fraction':1-received/rate,
                        'jitter_ms':0.5,'jitter_status':'measured'})
    return {'source':'synthetic workflow fixture','all_reachable':True,'ospf_healthy':True,
            'mean_rtt_ms':4,'pairs':[],'ospf_neighbors':{n:'' for n in MANIFEST['routers']},
            'traffic':{'mode':'concurrent_udp','flows':records,'concurrency_verified':True,
                       'overlap_window':{'start_seconds':1,'end_seconds':11,'duration_seconds':10},
                       'aggregate':{'received_mbps':sum(f['received_mbps'] for f in records),
                                    'loss_fraction':0.1 if impaired else 0},
                       'max_link_utilization':maximum,'link_utilization':{'sea:Ethernet1':maximum},
                       'capacities_enforced':True,'link_capacities_mbps':{'sea-sva':2 if impaired else 100}}}


class WorkflowLab(FakeLab):
    def __init__(self, bad_candidate=False, unverified=False):
        super().__init__()
        self.rates={}
        self.measurements=[]
        self.bad_candidate,self.unverified=bad_candidate,unverified
    def shape(self,rates,*args):
        self.rates=dict(rates)
    def measure(self,deadline=None,flow=None,*,flows=None):
        if flows is None:
            return super().measure(deadline,flow)
        candidate='   ip ospf cost 1000\n' in self.state['sea']
        impaired=bool(self.rates) and not candidate
        value=batch_measurement(flows,1 if impaired else 0.05,impaired)
        value['traffic']['link_capacities_mbps']={'sea-sva':self.rates.get('sea-sva',100)}
        if candidate and self.bad_candidate:
            value['traffic']['flows'][0]['received_mbps']=4
            value['traffic']['flows'][1]['received_mbps']=4
            value['traffic']['aggregate']['received_mbps']=8
        if self.unverified:
            value['traffic']['concurrency_verified']=False
        self.measurements.append(deepcopy(value))
        return value


class TrafficWorkflowTests(unittest.TestCase):
    def scenario(self):
        return load_scenario(SCENARIO,MANIFEST,experiment_catalog(MANIFEST))
    def decision(self):
        value=Proposal(outcome='candidate',diagnosis='Synthetic test-only cost proposal.',
            evidence=['Synthetic congestion observation.'],
            changes=[{'device':n,'interface':'Ethernet1','action':'set_ospf_cost','value':1000}
                     for n in ('sea','sva')],expected_effect='Use the alternate route.')
        return FakeDecision(value)

    def test_profiles_and_new_scenario_validate_without_changing_old_shape(self):
        profile=load_traffic_profile(PROFILE,MANIFEST)
        self.assertEqual(len(profile['flows']),2)
        self.assertTrue(all(flow['destination_port']==5201 for flow in profile['flows']))
        self.assertEqual(self.scenario()['flows'],profile['flows'])
        for path in (ROOT/'scenarios/sea-sva-congestion.json',
                     ROOT/'scenarios/benchmark-v1/congestion-sea-sva.json'):
            scenario=load_scenario(path,MANIFEST,experiment_catalog(MANIFEST))
            self.assertIn('flow',scenario)
            self.assertNotIn('flows',scenario)
            self.assertNotIn('destination_port',scenario['flow'])

    def test_conflicts_and_mixed_profile_modes_reject_before_lab_access(self):
        invalid=[{'flow':{'source':'sea','destination':'sva','offered_mbps':5},
                  **json.loads(PROFILE.read_text())}, {'flows':[]},
                 {'flows':[{'source':'unknown','destination':'sva','offered_mbps':5},
                           {'source':'hou','destination':'atl','offered_mbps':3}]},
                 {'flows':[{'source':'sea','destination':'sva','offered_mbps':5},
                           {'source':'hou','destination':'sva','offered_mbps':3}]}]
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'profile.json'
            for value in invalid:
                path.write_text(json.dumps(value))
                with self.subTest(value=value), patch('advnet.experiment.Lab') as lab, \
                        patch.object(sys,'argv',['experiment','traffic','--traffic-profile',str(path)]):
                    with self.assertRaises(ValueError):
                        main()
                    lab.assert_not_called()
            value=json.loads(SCENARIO.read_text())
            value['flow']={'source':'sea','destination':'sva','offered_mbps':5}
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                load_scenario(path,MANIFEST,experiment_catalog(MANIFEST))

    def test_traffic_cli_needs_no_api_key_and_records_inputs_and_measurements(self):
        for name in ('single-traffic.json','concurrent-traffic.json'):
            with self.subTest(profile=name), tempfile.TemporaryDirectory() as temporary:
                output=Path(temporary)/'collection'
                stub=Mock()
                stub.traffic.return_value={'source':'sea','destination':'sva','received_mbps':5}
                stub.concurrent_traffic.return_value=batch_measurement()['traffic']
                argv=['experiment','traffic','--traffic-profile',str(ROOT/'experiments'/name),'--output',str(output)]
                with patch.dict(os.environ,{},clear=True), patch.object(sys,'argv',argv), \
                        patch('advnet.experiment.Lab',return_value=stub), \
                        patch('advnet.experiment.DecisionProcess') as decision, \
                        patch('advnet.experiment.ProcessVerifier') as verifier, redirect_stdout(io.StringIO()):
                    self.assertEqual(main(),0)
                decision.assert_not_called()
                verifier.assert_not_called()
                evidence=json.loads((output/'evidence.json').read_text())
                self.assertEqual(evidence['status'],'collected')
                self.assertIn('lab.py',evidence['code_sha256'])
                measured=json.loads((output/'measurements.json').read_text())
                self.assertEqual(measured['scope'],'traffic_only')
                self.assertIsNone(measured['mean_rtt_ms'])
                self.assertNotIn('all_reachable',measured)
                stub.shape.assert_not_called()
                stub.replace.assert_not_called()
                stub.wait_convergence.assert_not_called()
                (stub.traffic if name.startswith('single') else stub.concurrent_traffic).assert_called_once()

    def test_traffic_cli_keeps_failed_collection_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)/'collection'
            stub=Mock()
            stub.concurrent_traffic.side_effect=TimeoutError('Synthetic timeout')
            argv=['experiment','traffic','--traffic-profile',str(PROFILE),'--output',str(output)]
            with patch.object(sys,'argv',argv), patch('advnet.experiment.Lab',return_value=stub):
                with self.assertRaisesRegex(TimeoutError,'Synthetic timeout'):
                    main()
            evidence=json.loads((output/'evidence.json').read_text())
            self.assertEqual(evidence['status'],'failed')
            self.assertEqual(evidence['error_type'],'TimeoutError')
            self.assertFalse((output/'measurements.json').exists())

    def test_collect_includes_batch_measurements_in_decision_observation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'observation.json'
            lab=WorkflowLab()
            argv=['experiment','collect','--traffic-profile',str(PROFILE),'--output',str(path)]
            with patch.object(sys,'argv',argv), patch('advnet.experiment.Lab',return_value=lab), redirect_stdout(io.StringIO()):
                self.assertEqual(main(),0)
            observation=json.loads(path.read_text())
            self.assertEqual(observation['measurements']['traffic']['mode'],'concurrent_udp')
            self.assertEqual(len(observation['measurements']['traffic']['flows']),2)

    def test_batch_acceptance_preserves_each_flow_instead_of_trusting_aggregate(self):
        healthy=batch_measurement()
        impaired=batch_measurement(maximum=1,impaired=True)
        current=batch_measurement()
        self.assertTrue(acceptable_concurrent(current,healthy,impaired,'optimization')['accepted'])
        current['traffic']['flows'][0]['received_mbps']=4
        current['traffic']['flows'][1]['received_mbps']=4
        current['traffic']['aggregate']['received_mbps']=8
        check=acceptable_concurrent(current,healthy,impaired,'optimization')
        self.assertFalse(check['accepted'])
        self.assertTrue(any('90%' in detail for detail in check['details']))

    def test_batch_rejects_unknown_identity_load_or_overlap(self):
        healthy=batch_measurement()
        impaired=batch_measurement(maximum=1,impaired=True)
        for field,value in (('destination','nyc'),('offered_mbps',10),('seconds',8),
                            ('received_mbps',None),('received_mbps',float('nan'))):
            current=batch_measurement()
            current['traffic']['flows'][0][field]=value
            with self.subTest(field=field):
                self.assertFalse(acceptable_concurrent(current,healthy,impaired,'optimization')['accepted'])
        current=batch_measurement()
        current['traffic']['concurrency_verified']=False
        self.assertFalse(acceptable_concurrent(current,healthy,impaired,'optimization')['accepted'])
        self.assertFalse(acceptable_concurrent(None,healthy,impaired,'optimization')['accepted'])

    def test_batch_trial_runs_existing_gates_and_writes_automatic_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'trial'
            lab=WorkflowLab()
            verifier=FakeVerifier()
            result=Trial(lab,self.decision(),verifier,path,max_attempts=1).run(self.scenario())
            self.assertTrue(result['success'],result)
            self.assertEqual(verifier.calls,2)
            self.assertTrue(result['cleanup_succeeded'])
            self.assertEqual(result['performance_report']['status'],'written')
            report=json.loads((path/'performance-comparison.json').read_text())
            self.assertEqual(report['accepted_post_attempt'],1)
            self.assertEqual(len(report['comparison']['flows']),2)
            self.assertTrue(report['source_files'])
            self.assertIn('trial.py',report['code_sha256'])
            self.assertTrue(lab.measurements[1]['traffic']['max_link_utilization']>
                            lab.measurements[0]['traffic']['max_link_utilization'])
            self.assertEqual(lab.rates,{})
            self.assertEqual(len(lab.measurements),3)

    def test_existing_run_cli_accepts_batch_scenario_with_mocked_transports(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'trial'
            argv=['experiment','run','--scenario',str(SCENARIO),'--model','synthetic-test-only',
                  '--output',str(path),'--attempts','1']
            with patch.dict(os.environ,{'OPEN_ROUTER_KEY':'test-only'},clear=True), \
                    patch.object(sys,'argv',argv), patch('advnet.experiment.Lab',return_value=WorkflowLab()), \
                    patch('advnet.experiment.DecisionProcess',return_value=self.decision()), \
                    patch('advnet.experiment.ProcessVerifier',return_value=FakeVerifier()), redirect_stdout(io.StringIO()):
                self.assertEqual(main(),0)
            result=json.loads((path/'result.json').read_text())
            self.assertTrue(result['success'])
            self.assertEqual(result['performance_report']['status'],'written')

    def test_batch_runtime_rejection_rolls_back_and_report_does_not_claim_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'trial'
            lab=WorkflowLab(bad_candidate=True)
            original=lab.configs()
            result=Trial(lab,self.decision(),FakeVerifier(),path,max_attempts=1).run(self.scenario())
            self.assertFalse(result['success'])
            self.assertTrue(result['cleanup_succeeded'])
            self.assertIn('rollback_candidate',[event['stage'] for event in result['events']])
            self.assertEqual(lab.configs(),original)
            report=json.loads((path/'performance-comparison.json').read_text())
            self.assertIsNone(report['accepted_post_attempt'])
            self.assertFalse(report['attempts'][0]['recorded_acceptance']['accepted'])

    def test_unverified_healthy_batch_stops_before_injecting_capacity_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            decision=self.decision()
            result=Trial(WorkflowLab(unverified=True),decision,FakeVerifier(),Path(temporary)/'trial').run(self.scenario())
            self.assertFalse(result['success'])
            self.assertNotIn('inject_capacity_change',[event['stage'] for event in result['events']])
            self.assertFalse(decision.states)

    def test_report_failure_never_changes_existing_repair_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'trial'
            scenario=json.loads((ROOT/'scenarios/sea-host-interface-down.json').read_text())
            with patch('advnet.performance.compare_trial_artifacts',side_effect=ValueError('Synthetic report failure')):
                result=Trial(FakeLab(),FakeDecision(proposal()),FakeVerifier(),path,max_attempts=1).run(scenario)
            self.assertTrue(result['success'])
            self.assertEqual(result['performance_report']['status'],'failed')
            self.assertTrue(json.loads((path/'result.json').read_text())['success'])


if __name__=='__main__':
    unittest.main()
