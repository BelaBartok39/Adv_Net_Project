"""Exercise the reproducible offline harness and inspect real policy evidence."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from advnet.stability_policy import StabilityPolicy
from advnet.stability_simulation import ANCHOR, main, run_simulation


class StabilitySimulationTests(unittest.TestCase):
    def test_reproducible_real_policy_milestones(self):
        first = run_simulation()
        self.assertEqual(first, run_simulation())
        summary = first['summary']
        self.assertEqual(summary['evaluations'], 62)
        self.assertEqual(summary['milestone_checks_passed'], 20)
        self.assertEqual(summary['outcomes'], {'success': 3, 'rejected': 2, 'failed': 1,
                                              'rollback_unconfirmed': 1, 'rolled_back': 1})
        self.assertIsNone(summary['final_active_intervention'])
        for row in first['trace']:
            self.assertTrue(row['synthetic'])
            self.assertFalse(row['decision']['deployment_authorized'])
            for assessment in row['proposal_assessments']:
                self.assertFalse(assessment['decision']['deployment_authorized'])

    def test_trace_captures_lifecycle_cooldown_and_actual_history(self):
        rows = {row['at_seconds']: row for row in run_simulation()['trace']}
        self.assertNotEqual(rows[60]['state_before'], rows[60]['state_after'])
        self.assertEqual(rows[60]['state_after']['configuration_change_count'], 1)
        self.assertEqual(rows[70]['decision']['recommendation'], 'suppress_cooldown')
        self.assertEqual(rows[240]['state_after']['active']['status'], 'rollback_unconfirmed')
        self.assertIsNone(rows[250]['state_after']['active'])
        self.assertEqual(rows[545]['decision']['reasons'][0], 'frequency_limit')
        self.assertEqual(rows[640]['proposal_assessments'][0]['decision']['reasons'], ['state_switching'])
        self.assertIn('proposal_eligible', rows[640]['proposal_assessments'][1]['decision']['reasons'])
        self.assertIn('missing_utilization', rows[800]['decision']['reasons'])

    def test_cli_artifacts_and_restorable_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'synthetic'
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--output', str(output)]), 0)
            summary = json.loads((output / 'summary.json').read_text())
            rows = [json.loads(line) for line in (output / 'trace.jsonl').read_text().splitlines()]
            self.assertEqual(summary['evaluations'], len(rows))
            restored = StabilityPolicy.restore((output / 'final-policy-state.json').read_text(), clock=lambda: ANCHOR)
            self.assertEqual(len(restored.state.interventions), 7)
            self.assertEqual(len(restored.state.changes), 5)

    def test_cli_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            sentinel = Path(temporary) / 'sentinel'
            sentinel.write_text('keep')
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(['--output', temporary])
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(sentinel.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
