"""Campaign ordering, retained failures, and stopping before unsafe continuation."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('bounded_experiments', ROOT / 'scripts/run-bounded-experiments.py')
campaign = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(campaign)


class BoundedCampaignTests(unittest.TestCase):
    def plan(self, repetitions=1):
        return campaign.make_plan('smoke', repetitions, True, 'test-llm', 'test-jev', 'https://example.test/v1')

    def test_pilot_pairs_same_scenario_and_balances_order_across_repetitions(self):
        plan = self.plan(2)
        self.assertEqual(plan['job_count'], 28)
        self.assertEqual(len({job['id'] for job in plan['jobs']}), 28)
        for offset in range(0, 14, 2):
            left, right = plan['jobs'][offset:offset+2]
            self.assertEqual(left['scenario'], right['scenario'])
            self.assertEqual({left['system_one'], right['system_one']}, {'off', 'jev'})
            self.assertNotEqual(left['system_one'], plan['jobs'][offset+14]['system_one'])

    def test_each_command_restores_baseline_and_uses_frozen_models_and_budgets(self):
        plan = self.plan()
        for job in plan['jobs']:
            argv = campaign.command(plan, job, Path('/tmp/test-output'))
            self.assertIn('--reset-baseline', argv)
            for flag, value in [('--model', 'test-llm'), ('--sysone-model', 'test-jev'),
                                ('--attempts', '3'), ('--seconds', '900')]:
                self.assertEqual(argv[argv.index(flag)+1], value)
            self.assertNotIn('--force-escalation', argv)

    def test_failed_trial_is_retained_and_not_retried_when_cleanup_succeeded(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = {'success': False, 'cleanup_succeeded': True,
                      'baseline_reset_completed': True, 'attempts': 3, 'model_calls': []}
            (directory/'result.json').write_text(json.dumps(result))
            self.assertEqual(campaign.saved_outcome(directory), result)
            self.assertIsNone(campaign.stop_reason(result))

    def test_missing_or_malformed_result_prevents_resuming_past_uncertain_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with self.assertRaisesRegex(ValueError, 'Incomplete trial'):
                campaign.saved_outcome(directory)
            (directory/'result.json').write_text('{"success": "false", "cleanup_succeeded": true}')
            with self.assertRaisesRegex(ValueError, 'Malformed trial'):
                campaign.saved_outcome(directory)

    def test_cleanup_setup_and_authentication_failures_stop_campaign(self):
        good = {'success': True, 'cleanup_succeeded': True, 'baseline_reset_completed': True,
                'attempts': 1, 'model_calls': []}
        self.assertIsNone(campaign.stop_reason(good))
        for changes in ({'cleanup_succeeded': False}, {'baseline_reset_completed': False},
                        {'attempts': 0}, {'model_calls': [{'error_type': 'TypeSafeAuthenticationError'}]}):
            with self.subTest(changes=changes):
                self.assertIsNotNone(campaign.stop_reason({**good, **changes}))

    def test_summary_includes_failed_and_successful_arms(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            plan = self.plan()
            for job, success in zip(plan['jobs'][:2], (False, True)):
                path = directory / 'trials' / job['id']
                path.mkdir(parents=True)
                campaign.save(path/'result.json', {'success': success, 'cleanup_succeeded': True,
                              'trial_id': job['id'], 'mode': job['system_one']})
            summary = campaign.summarize(directory, plan)
            self.assertEqual((summary['recorded'], summary['failed'], summary['successful']), (2, 1, 1))
            self.assertEqual(len((directory/'comparison.csv').read_text().splitlines()), 3)
