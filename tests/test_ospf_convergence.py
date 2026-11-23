"""Stable alternate routes must not cause premature OSPF recovery acceptance."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from advnet.lab import Deadline, Lab
from advnet.trial import Trial
from test_abilene import FakeDecision, FakeLab, FakeVerifier, proposal


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ConvergenceTests(unittest.TestCase):
    def lab(self):
        lab = Lab({'lab': 'convergence-test',
                   'routers': {'a': {'router_id': '10.0.0.1'}, 'b': {'router_id': '10.0.0.2'}},
                   'links': [{'id': 'a-b', 'a': 'a', 'b': 'b', 'capacity_mbps': 100}]})
        lab.cli = Mock(return_value='stable alternate routes')
        return lab

    def peers(self, state):
        return {'a': f'10.0.0.2 1 default 0 {state} 00:00:35 10.1.0.2 Ethernet1\n',
                'b': f'10.0.0.1 1 default 0 {state} 00:00:35 10.1.0.1 Ethernet1\n'}

    def test_recovery_waits_through_init_and_exchange_for_two_full_samples(self):
        clock = Clock()
        lab = self.lab()
        lab.neighbors = Mock(side_effect=[self.peers('INIT'), self.peers('EXCH START'),
                                          self.peers('FULL'), self.peers('FULL')])
        with patch('advnet.lab.time.monotonic', clock.monotonic), patch('advnet.lab.time.sleep', clock.sleep):
            self.assertTrue(lab.wait_convergence(Deadline(300), require_healthy=True))
        self.assertEqual(lab.neighbors.call_count, 4)
        self.assertGreaterEqual(clock.now, 10)

    def test_fault_observation_can_stabilize_with_missing_neighbors(self):
        clock = Clock()
        lab = self.lab()
        lab.neighbors = Mock(return_value={})
        with patch('advnet.lab.time.monotonic', clock.monotonic), patch('advnet.lab.time.sleep', clock.sleep):
            self.assertTrue(lab.wait_convergence(Deadline(300)))
        lab.neighbors.assert_not_called()

    def test_unrepaired_candidate_has_bounded_wait_and_can_reach_runtime_rejection(self):
        clock = Clock()
        lab = self.lab()
        lab.neighbors = Mock(return_value=self.peers('INIT'))
        with patch('advnet.lab.time.monotonic', clock.monotonic), patch('advnet.lab.time.sleep', clock.sleep):
            self.assertFalse(lab.wait_convergence(Deadline(300), seconds=10, require_healthy=True))
        self.assertEqual(clock.now, 10)

    def test_overall_deadline_is_not_treated_as_local_convergence_expiry(self):
        clock = Clock()
        lab = self.lab()
        lab.neighbors = Mock(return_value=self.peers('INIT'))
        with patch('advnet.lab.time.monotonic', clock.monotonic), patch('advnet.lab.time.sleep', clock.sleep):
            with self.assertRaises(TimeoutError):
                lab.wait_convergence(Deadline(8), seconds=120, require_healthy=True)

    def test_trial_requires_full_neighbors_for_preflight_candidate_and_final_reset(self):
        class RecordingLab(FakeLab):
            def __init__(self):
                super().__init__()
                self.waits = []

            def wait_convergence(self, deadline, seconds=60, *, require_healthy=False):
                self.waits.append((seconds, require_healthy))
                return True

        for rejected in (False, True):
            with self.subTest(rejected=rejected), tempfile.TemporaryDirectory() as temporary:
                lab = RecordingLab()
                directory = Path(temporary)/'trial'
                result = Trial(lab, FakeDecision(proposal()), FakeVerifier(rejected),
                               directory, max_attempts=1).run({
                    'name': 'test', 'objective': 'repair', 'objective_text': 'Restore connectivity',
                    'fault': proposal(False).model_dump(mode='json')})
                self.assertEqual(lab.waits, [(120, True), (60, False), (120, True)])
                self.assertEqual(result['success'], not rejected)
                self.assertTrue(result['cleanup_succeeded'])
                self.assertTrue((directory/('reset-convergence.json' if rejected
                                           else 'attempt-1/convergence.json')).exists())
