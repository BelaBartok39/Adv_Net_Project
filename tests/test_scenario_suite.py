"""Offline suite proofs and simulated control-loop regressions; not model accuracy measurements."""
import importlib.util
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path

from advnet.catalog import experiment_catalog
from advnet.contracts import Observation, DeviceState, Proposal
from advnet.eos import candidate_configs
from advnet.pipeline import DecisionEngine
from advnet.providers import MockJev, MockLLM
from advnet.scenario_model import predict,shortest_paths
from advnet.scenarios import load_scenario
from advnet.trial import Trial

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=json.loads((ROOT/'topologies/abilene.json').read_text())
CONFIGS={n:(ROOT/f'labs/configs/abilene/{n}.cfg').read_text() for n in MANIFEST['routers']}
CATALOG=experiment_catalog(MANIFEST)
SUITE=ROOT/'scenarios/benchmark-v1'
INDEX=json.loads((SUITE/'suite.json').read_text())
spec=importlib.util.spec_from_file_location('scenario_suite',ROOT/'scripts/scenario-suite.py')
suite=importlib.util.module_from_spec(spec); spec.loader.exec_module(suite)


class SimulatedLab:
    """In-memory admin/area/cost model. Never accesses Docker or a model endpoint."""
    def __init__(self):
        self.manifest=MANIFEST
        self.state=dict(CONFIGS)
        self.capacities={link['id']:link['capacity_mbps'] for link in MANIFEST['links']}
        self.writes=[]

    def configs(self,*args): return dict(self.state)
    def replace(self,configs,*args):
        self.state.update(configs); self.writes.append(dict(configs))
    def shape(self,rates,*args):
        self.capacities={link['id']:rates.get(link['id'],100) for link in MANIFEST['links']}
    def wait_convergence(self,*args): pass
    def measure(self,deadline=None,flow=None):
        model=predict(MANIFEST,self.state)
        failed=set(model['unreachable_pairs'])
        pairs=[{'source':a,'destination':b,'reachable':(a,b) not in failed,
                'loss_fraction':1.0 if (a,b) in failed else 0.0,'rtt_avg_ms':None if (a,b) in failed else 4.0}
               for a in MANIFEST['routers'] for b in MANIFEST['routers'] if a!=b]
        traffic=None
        if flow:
            paths=shortest_paths(model['graph'],flow['source'],flow['destination'])
            capacity=min(self.capacities[link] for path in paths for link in path) if paths else 0
            received=min(capacity,flow['offered_mbps'])
            traffic={'received_mbps':received,'loss_fraction':1-received/flow['offered_mbps'],
                     'max_link_utilization':received/capacity if capacity else 0}
        return {'all_reachable':not failed,'ospf_healthy':not model['missing_adjacencies'],
                'mean_rtt_ms':4.0,'pairs':pairs,'traffic':traffic,
                'ospf_neighbors':{node:'' for node in MANIFEST['routers']}}
    def observe(self,identifier,measurements,deadline,objective):
        return Observation(scenario_id=identifier,observed_at=datetime.now(timezone.utc),objective=objective,
            requirements=MANIFEST['requirements'],topology=[],measurements=measurements,
            devices={node:DeviceState(interfaces=MANIFEST['routers'][node]['interfaces'],running_config=text)
                     for node,text in self.state.items()})


class SimulatedVerifier:
    def verify(self,directory,deadline):
        configs={p.stem:p.read_text() for p in (directory/'configs').glob('*.cfg')}
        # Mirrors only the real gate's host-reachability scope, not OSPF health.
        accepted=not predict(MANIFEST,configs)['unreachable_pairs']
        return {'accepted':accepted,'details':['SIMULATED reachability check']}


class ScriptedDecision:
    def __init__(self,scenario,enabled=True,choice=None,forced=False):
        self.system_one_enabled=enabled
        self.settings={'system_one':'jev' if enabled else 'off','forced_escalation':forced}
        expected=scenario['evaluation']
        route=choice or (expected['expected_actions'][0] if expected['expected_route']=='catalog' else 'escalate')
        self.engine=DecisionEngine(MockJev(route) if enabled else None,MockLLM(scenario['reference_repair']),CATALOG,
                                   force_escalation=forced)
        self.states=[]
    def decide(self,observation,feedback,attempt,deadline):
        self.states.append(observation.model_dump(mode='json'))
        return self.engine.decide(observation,feedback,attempt)


class ScenarioSuiteTests(unittest.TestCase):
    def case(self,name): return load_scenario(SUITE/(name+'.json'),MANIFEST,CATALOG)

    def test_all_cases_have_real_impairments_references_and_correct_catalog_coverage(self):
        self.assertEqual(len(INDEX['cases']),30)
        for entry in INDEX['cases']:
            with self.subTest(case=entry['name']):
                _,_,report=suite.validate_case(MANIFEST,CONFIGS,CATALOG,self.case(entry['name']))
                self.assertTrue(report['offline_validation_passed'])

    def test_all_cases_complete_both_simulated_loops_without_ground_truth_leakage(self):
        with tempfile.TemporaryDirectory() as temporary:
            for entry in INDEX['cases']:
                scenario=self.case(entry['name'])
                scenario['evaluation']['explanation']='PRIVATE-LABEL-MUST-NOT-REACH-MODEL'
                for enabled in (False,True):
                    with self.subTest(case=entry['name'],jev=enabled):
                        lab=SimulatedLab(); decision=ScriptedDecision(scenario,enabled)
                        result=Trial(lab,decision,SimulatedVerifier(),Path(temporary)/entry['name']/str(enabled),max_attempts=3).run(scenario)
                        self.assertTrue(result['success'],result)
                        self.assertEqual(result['attempts'],1)
                        self.assertNotIn('PRIVATE-LABEL-MUST-NOT-REACH-MODEL',json.dumps(decision.states))
                        self.assertTrue(predict(MANIFEST,lab.state)['healthy'])
                        metrics=result['inference_metrics']
                        self.assertEqual(metrics['jev_calls'],int(enabled))
                        self.assertEqual(metrics['llm_calls'],int(not enabled or entry['expected_route']=='escalate'))

    def test_wrong_partial_shortcut_is_rolled_back_before_llm_repair(self):
        scenario=self.case('compound-sea-host-and-backbone')
        with tempfile.TemporaryDirectory() as temporary:
            lab=SimulatedLab(); decision=ScriptedDecision(scenario,choice='enable_host_sea')
            result=Trial(lab,decision,SimulatedVerifier(),Path(temporary)/'trial',max_attempts=3).run(scenario)
            self.assertTrue(result['success'])
            self.assertEqual(result['attempts'],2)
            self.assertTrue(result['routing_evaluation']['incorrect_catalog_choice'])
            self.assertIn('rollback_candidate',[event['stage'] for event in result['events']])
            self.assertEqual(result['inference_metrics']['llm_calls'],1)
            self.assertEqual(result['inference_metrics']['jev_calls'],1)

    def test_false_no_change_is_not_accepted_as_repair(self):
        scenario=self.case('routine-host-sea-down')
        with tempfile.TemporaryDirectory() as temporary:
            lab=SimulatedLab(); decision=ScriptedDecision(scenario,choice='no_change')
            result=Trial(lab,decision,SimulatedVerifier(),Path(temporary)/'trial',max_attempts=3).run(scenario)
            self.assertTrue(result['success'])
            self.assertEqual(result['attempts'],2)
            self.assertTrue(result['routing_evaluation']['incorrect_no_change'])

    def test_forced_control_calls_both_models_but_is_not_scored_as_classification(self):
        scenario=self.case('routine-host-sea-down')
        with tempfile.TemporaryDirectory() as temporary:
            decision=ScriptedDecision(scenario,forced=True)
            result=Trial(SimulatedLab(),decision,SimulatedVerifier(),Path(temporary)/'trial').run(scenario)
            self.assertTrue(result['success'])
            self.assertEqual(result['inference_metrics']['jev_calls'],1)
            self.assertEqual(result['inference_metrics']['llm_calls'],1)
            self.assertTrue(result['routing_evaluation']['forced_escalation'])
            self.assertIsNone(result['routing_evaluation']['classifier_route_correct'])
            self.assertEqual(result['decisions'][0]['screening']['selected'],'enable_host_sea')
            self.assertEqual(result['decisions'][0]['screening']['effective_route'],'escalate')

    def test_reset_removes_prior_cost_and_capacity_changes_before_preflight(self):
        scenario=self.case('routine-host-sea-down')
        lab=SimulatedLab()
        lab.state=candidate_configs(lab.state,Proposal(outcome='candidate',diagnosis='test',evidence=['test'],
            changes=[{'device':'sea','interface':'Ethernet2','action':'set_ospf_cost','value':500}],expected_effect='test'))
        lab.shape({'sea-sva':2})
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)/'trial'
            result=Trial(lab,ScriptedDecision(scenario),SimulatedVerifier(),directory,baseline_configs=CONFIGS).run(scenario)
            self.assertTrue(result['success'])
            self.assertTrue(result['baseline_reset_completed'])
            self.assertEqual(json.loads((directory/'healthy-configs.json').read_text()),CONFIGS)
            self.assertTrue(all(value==100 for value in lab.capacities.values()))

    def test_validation_rejects_unknown_links_interfaces_and_catalog_labels(self):
        original=self.case('routine-host-sea-down')
        invalid=[]
        item=deepcopy(original); item['fault']['changes'][0]['interface']='Ethernet99'; invalid.append(item)
        item=deepcopy(original); item['capacities_mbps']={'unknown':2}; invalid.append(item)
        item=deepcopy(original); item['evaluation']['catalog_sha256']='0'*64; invalid.append(item)
        item=deepcopy(original); item['flow']={'source':'sea','destination':'sea','seconds':3,'offered_mbps':5}; invalid.append(item)
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'case.json'
            for item in invalid:
                path.write_text(json.dumps(item))
                with self.assertRaises(ValueError): load_scenario(path,MANIFEST,CATALOG)


if __name__=='__main__': unittest.main()
