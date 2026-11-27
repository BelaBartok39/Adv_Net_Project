"""Action-memory lifecycle, contamination guards, and actual decision reuse."""
from copy import deepcopy
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from advnet.contracts import Observation, Proposal
from advnet.persistent_controller import EngineProvider, _infer
from advnet.providers import MockJev, MockLLM
from advnet.thinking_cache import ThinkingCache, context, main

ROOT = Path(__file__).resolve().parents[1]


class NeverLLM:
    def propose(self, *args):
        raise AssertionError('Cache hit must not invoke the LLM')


class ThinkingCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'cache.sqlite'
        self.cache = ThinkingCache(self.path, 'protocol-A', 'adaptive')
        self.obs = Observation.model_validate_json((ROOT/'examples/congestion.json').read_text())
        self.proposal = Proposal.model_validate_json((ROOT/'examples/mock-proposal.json').read_text())
        self.obs.measurements = dict(error=None, demand_id='35+25Mbps', capacity_sha256='a'*64,
                                     all_reachable=True, ospf_healthy=True, failed_pairs=[], mismatched_ospf=[],
                                     mean_rtt_ms=87.0, performance=dict(healthy=False, latency_limit_ms=13.3),
                                     pairs=[dict(source='r1', destination='r2', rtt_avg_ms=87.0)],
                                     stability_policy=dict(kind='repair', links=[]))
        self.before_id = context(self.obs)['config']
        before = dict(source='live', config_sha256=self.before_id, demand_id='35+25Mbps', demand_epoch='epoch-A',
                      capacity_sha256='a'*64)
        after = dict(before, config_sha256='b'*64, all_reachable=True, ospf_healthy=True)
        self.args = dict(observation=self.obs, proposal=self.proposal,
                         decision=dict(route='escalate', metrics=dict(llm_calls=1)), status='success',
                         reason='verified_readback_and_runtime', runtime=dict(before=before, after=after,
                         acceptance=dict(accepted=True)), trial='trial-1', intervention='attempt-1',
                         before_id=self.before_id, after_id='b'*64)

    def promote(self):
        self.cache.observe(**self.args)
        return self.cache.observe(**dict(self.args, trial='trial-2'))['action']

    def test_independent_trials_required_not_attempts_or_replays(self):
        a = self.cache.observe(**self.args)
        self.assertEqual(a['status'], 'quarantine')
        self.assertEqual(self.cache.eligible(self.obs), {})
        self.cache.observe(**dict(self.args, intervention='attempt-2'))
        self.assertEqual(self.cache.observe(**self.args)['independent_successes'], 1)
        self.assertEqual(self.cache.eligible(self.obs), {})
        b = self.cache.observe(**dict(self.args, trial='trial-2'))
        self.assertEqual(b['status'], 'active')
        self.assertEqual(list(self.cache.eligible(self.obs)), [a['action']])

    def test_real_engine_selects_promoted_action_without_llm_and_keeps_gate(self):
        name = self.promote()
        provider = EngineProvider(NeverLLM(), MockJev(name), thinking_cache=self.cache)
        path = Path(self.temp.name)/'decision.json'
        _infer(provider, ('jev_llm', self.obs, [], 1), path)
        result = json.loads(path.read_text())
        self.assertEqual(result['status'], 'decision_complete')
        self.assertEqual(result['route'], 'catalog')
        self.assertEqual(result['selected_action'], name)
        self.assertEqual(result['metrics']['llm_calls'], 0)
        self.assertEqual(result['thinking_cache']['eligible_actions'], [name])
        raw = provider('jev_llm', self.obs, [], 1)
        self.assertEqual(raw['handoff'], 'verification_required')
        self.assertIsNone(raw['repair_success'])

    def test_context_changes_suppress_reuse_but_timestamp_and_epoch_do_not(self):
        self.promote()
        variations = [dict(demand_id='different'), dict(capacity_sha256='c'*64),
                      dict(pairs=[dict(source='r2', destination='r1', rtt_avg_ms=87.0)]),
                      dict(failed_pairs=[['sea','nyc']]), dict(mean_rtt_ms=5),
                      dict(ospf_healthy=False), dict(performance=dict(healthy=True, latency_limit_ms=13.3)),
                      dict(error='unknown'), dict(capacity_sha256=None),
                      dict(stability_policy=dict(kind='optimization', links=[]))]
        for update in variations:
            obs = self.obs.model_copy(deep=True)
            obs.measurements.update(update)
            self.assertFalse(self.cache.eligible(obs), update)
        for field in ('objective','requirements','topology'):
            obs = self.obs.model_copy(deep=True)
            setattr(obs, field, 'Different goal' if field == 'objective' else ['different'])
            self.assertFalse(self.cache.eligible(obs))
        obs = self.obs.model_copy(deep=True)
        next(iter(obs.devices.values())).running_config += '\n! Changed configuration\n'
        self.assertFalse(self.cache.eligible(obs))
        obs = self.obs.model_copy(deep=True)
        obs.scenario_id = 'new-observation'
        obs.measurements.update(demand_epoch='another-epoch')
        self.assertTrue(self.cache.eligible(obs))
        self.assertFalse(ThinkingCache(self.path, 'different-protocol', 'adaptive').eligible(self.obs))

    def test_incomplete_or_unaccepted_evidence_never_seeds(self):
        for update in [dict(status='rolled_back'), dict(status='rejected'), dict(reason='unconfirmed'),
                       dict(decision=dict(route='catalog', metrics=dict(llm_calls=0))),
                       dict(runtime={}), dict(after_id='c'*64)]:
            self.assertIsNone(self.cache.observe(**dict(self.args, **update)))
        for key, value in [('capacity_sha256','c'*64), ('demand_epoch','changed'),
                           ('ospf_healthy',False), ('config_sha256',self.before_id)]:
            args = deepcopy(self.args)
            args['runtime']['after'][key] = value
            self.assertIsNone(self.cache.observe(**args))
        self.assertFalse(self.cache.summary())

    def test_failure_retires_entry_and_success_does_not_rehabilitate(self):
        name = self.promote()
        decision = dict(selected_action=name, route='catalog', metrics=dict(llm_calls=0))
        self.assertIsNone(self.cache.observe(**dict(self.args, decision=decision)))
        result = self.cache.observe(**dict(self.args, decision=decision, status='rolled_back'))
        self.assertEqual(result['status'], 'retired')
        self.assertFalse(self.cache.eligible(self.obs))
        result = self.cache.observe(**dict(self.args, trial='trial-3'))
        self.assertEqual(result['status'], 'retired')

    def test_collect_and_frozen_modes_and_same_actions_for_llm_baseline(self):
        name = self.promote()
        collector = ThinkingCache(self.path, 'protocol-A', 'collect')
        self.assertFalse(collector.eligible(self.obs))
        frozen = ThinkingCache(self.path, 'protocol-A', 'frozen')
        before = self.path.read_bytes()
        self.assertIsNone(frozen.observe(**dict(self.args, trial='third')))
        self.assertTrue(frozen.eligible(self.obs))
        self.assertEqual(self.path.read_bytes(), before)
        class CaptureLLM(MockLLM):
            def propose(inner, system, state):
                self.assertIn(name, state['available_actions'])
                return super().propose(system, state)
        result = EngineProvider(CaptureLLM(self.proposal.model_dump()), thinking_cache=frozen)(
            'llm_only', self.obs, [], 1)
        self.assertEqual(result['status'], 'decision_complete')

    def test_corrupt_database_falls_back_without_skipping_llm(self):
        self.path.write_text('corrupt')
        provider = EngineProvider(MockLLM(self.proposal.model_dump()), MockJev(), thinking_cache=self.cache)
        result = provider('jev_llm', self.obs, [], 1)
        self.assertEqual(result['status'], 'decision_complete')
        self.assertEqual(result['thinking_cache']['error_type'], 'DatabaseError')
        self.assertTrue(any(c['stage'] == 'llm' for c in result['calls']))

    def test_store_does_not_contain_configs_or_llm_prose(self):
        self.proposal.diagnosis = 'SECRET MODEL PROSE'
        self.proposal.evidence = ['SECRET MODEL PROSE']
        self.promote()
        with closing(sqlite3.connect(self.path)) as db:
            stored = str(db.execute('SELECT * FROM entries').fetchall())
        self.assertNotIn('SECRET MODEL PROSE', stored)
        self.assertNotIn('running_config', stored)
        self.assertNotIn('interface Ethernet', stored)

    def test_freeze_is_consistent_immutable_and_cannot_overwrite(self):
        name = self.promote()
        target = Path(self.temp.name)/'evaluation.sqlite'
        with redirect_stdout(io.StringIO()):
            main(['freeze', str(self.path), str(target)])
            frozen = ThinkingCache(target, 'protocol-A', 'frozen')
            self.assertIn(name, frozen.eligible(self.obs))
            with self.assertRaises(FileExistsError):
                main(['freeze', str(self.path), str(target)])
        with target.open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'Frozen thinking cache changed'):
            frozen.eligible(self.obs)
