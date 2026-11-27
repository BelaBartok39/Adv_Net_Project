"""Advisory path evidence must remain directional, bounded and answer-blind."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from advnet.contracts import Observation
from advnet.evidence_summary import path_summary
from advnet.pipeline import DecisionEngine
from advnet.persistent_controller import EngineProvider, _infer
from advnet.providers import MockLLM


ROOT = Path(__file__).resolve().parents[1]


def fixture():
    manifest = json.loads((ROOT/'topologies/abilene.json').read_text())
    observation = json.loads((ROOT/'examples/congestion.json').read_text())
    observation['devices'] = {name: {'interfaces': router['interfaces'],
        'running_config': (ROOT/'labs/configs/abilene'/f'{name}.cfg').read_text(), 'telemetry': {}}
        for name, router in manifest['routers'].items()}
    observation['topology'] = [f'{l["a"]}:{l["a_interface"]} -- {l["b"]}:{l["b_interface"]}' for l in manifest['links']]
    observation['measurements'] = {'all_reachable': True, 'ospf_healthy': True, 'mean_rtt_ms': 80,
        'performance': {'latency_limit_ms': 13},
        'pairs': [{'source': 'sea', 'destination': 'nyc', 'rtt_avg_ms': 205},
                  {'source': 'den', 'destination': 'kcy', 'rtt_avg_ms': 203}]}
    return observation


class SummaryTests(unittest.TestCase):
    def test_directional_costs_and_both_rtt_directions_are_distinct(self):
        obs = fixture()
        summary = path_summary(obs)
        self.assertEqual(summary['status'], 'available')
        self.assertEqual(len(summary['directed_backbone_costs']), 28)
        direct = summary['largest_measured_pair_rtts'][1]
        self.assertEqual(direct['forward_estimates']['paths'][0]['directed_interfaces'], ['den:Ethernet3'])
        self.assertEqual(direct['return_estimates']['paths'][0]['directed_interfaces'], ['kcy:Ethernet1'])
        # Change only one direction: return route/cost stays independent.
        config = obs['devices']['den']['running_config']
        start = config.index('interface Ethernet3\n')
        obs['devices']['den']['running_config'] = config[:start]+config[start:].replace('ip ospf cost 10', 'ip ospf cost 50', 1)
        direct = path_summary(obs)['largest_measured_pair_rtts'][1]
        self.assertNotIn('den:Ethernet3', direct['forward_estimates']['paths'][0]['directed_interfaces'])
        self.assertEqual(direct['return_estimates']['paths'][0]['directed_interfaces'], ['kcy:Ethernet1'])

    def test_equal_cost_paths_are_retained_and_summary_is_bounded(self):
        obs = fixture()
        obs['measurements']['pairs'] = [{'source': a, 'destination': b, 'rtt_avg_ms': 5}
            for a in obs['devices'] for b in obs['devices'] if a != b]
        summary = path_summary(obs)
        self.assertEqual(summary['measured_pair_count'], 110)
        self.assertEqual(summary['displayed_pair_count'], 12)
        self.assertTrue(any(len(p['forward_estimates']['paths']) > 1 for p in summary['largest_measured_pair_rtts']))
        self.assertTrue(all(len(p['forward_estimates']['paths']) <= 4 for p in summary['largest_measured_pair_rtts']))
        self.assertNotIn('recommended', json.dumps(summary))

    def test_unknown_or_unhealthy_evidence_has_no_path_estimates(self):
        for change in ('health', 'cost', 'area', 'attachment'):
            obs = fixture()
            if change == 'health': obs['measurements']['ospf_healthy'] = False
            if change == 'cost': obs['devices']['sea']['running_config'] = obs['devices']['sea']['running_config'].replace('ip ospf cost 10', 'no ip ospf cost')
            if change == 'area': obs['devices']['sea']['running_config'] = obs['devices']['sea']['running_config'].replace('ip ospf area 0.0.0.0', 'ip ospf area 1.1.1.1')
            if change == 'attachment': obs['topology'].append(obs['topology'][0])
            with self.subTest(change=change):
                summary = path_summary(obs)
                self.assertEqual(summary['status'], 'unavailable')
                self.assertNotIn('largest_measured_pair_rtts', summary)

    def test_summary_does_not_use_scenario_labels_or_reference_actions(self):
        obs = fixture()
        expected = path_summary(obs)
        obs['scenario_id'] = 'hidden-fault-location'
        obs['objective'] = 'private reference action'
        obs['measurements']['fault_schedule'] = {'link': 'den-kcy', 'delay_ms': 200}
        obs['measurements']['reference_repair'] = {'device': 'den', 'cost': 100}
        self.assertEqual(path_summary(obs), expected)

    def test_only_llm_receives_augmentation_and_raw_evidence_is_unchanged(self):
        seen = {}
        patch = {'outcome': 'needs_observation', 'diagnosis': 'Need routes', 'evidence': ['Current observations'],
                 'expected_effect': 'Collect routes', 'observation_requests': ['routing_tables']}
        class LLM(MockLLM):
            def propose(self, system, state):
                seen['llm'] = deepcopy(state)
                return super().propose(system, state)
        class Jev:
            def classify(self, state, criteria):
                from advnet.providers import MockJev
                seen['jev'] = deepcopy(state)
                return MockJev().classify(state, criteria)
        obs = Observation.model_validate_json(json.dumps(fixture()))
        original = obs.model_dump(mode='json')
        with TemporaryDirectory() as folder:
            destination = Path(folder)/'decision.json'
            _infer(EngineProvider(LLM(patch), Jev(), llm_evidence='path-summary'),
                   ('jev_llm', obs, [], 1), destination)
            result = json.loads(destination.read_text())
        self.assertNotIn('derived_evidence_summary', seen['jev'])
        self.assertEqual(seen['llm']['observation'], original)
        self.assertEqual(obs.model_dump(mode='json'), original)
        self.assertEqual(result['evidence_summary']['status'], 'available')
        self.assertEqual(result['llm_evidence'], 'path-summary')
        self.assertEqual(len(result['llm_input_sha256']), 64)
        raw = DecisionEngine(None, LLM(patch), {}).decide(obs)
        self.assertNotIn('derived_evidence_summary', seen['llm'])
        self.assertNotEqual(raw['llm_input_sha256'], result['llm_input_sha256'])


if __name__ == '__main__':
    unittest.main()
