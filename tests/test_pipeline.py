import json
import unittest
from copy import deepcopy
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from advnet.contracts import CatalogAction, Feedback, Observation, Proposal
from advnet.pipeline import DecisionEngine, safe_provider_error
from advnet.providers import MockJev, MockLLM, Reply

ROOT = Path(__file__).resolve().parents[1]


class BrokenJev:
    def classify(self, state, criteria):
        raise TimeoutError("must not be written to the run log")


class NoCallLLM:
    def propose(self, *args):
        raise AssertionError("LLM must not be called on this route")


class PipelineTests(unittest.TestCase):
    def test_quota_diagnostic_excludes_provider_payload(self):
        class QuotaError(Exception):
            status_code = 429
            code = "insufficient_quota"
        class QuotaLLM:
            def propose(self, *args):
                raise QuotaError("secret provider payload")
        result = self.engine(llm=QuotaLLM()).decide(self.observation)
        self.assertEqual(result["http_status"], 429)
        self.assertEqual(result["provider_error_code"], "insufficient_quota")
        self.assertNotIn("secret provider payload", json.dumps(result))

    def test_unrecognized_provider_code_is_not_logged(self):
        error = RuntimeError("secret")
        error.code = "secret"
        self.assertEqual(safe_provider_error(error), {})

    def setUp(self):
        self.observation = Observation.model_validate_json((ROOT / "examples/congestion.json").read_text())
        self.catalog = TypeAdapter(dict[str, CatalogAction]).validate_json((ROOT / "examples/actions.json").read_text())
        self.patch = json.loads((ROOT / "examples/mock-proposal.json").read_text())

    def engine(self, jev=None, llm=None, threshold=0.9):
        return DecisionEngine(jev, llm or MockLLM(self.patch), self.catalog, threshold)

    def test_catalog_avoids_llm_but_requires_verifier(self):
        result = self.engine(MockJev("avoid_r2_path"), NoCallLLM()).decide(self.observation)
        self.assertEqual(result["handoff"], "verification_required")
        self.assertEqual([c["stage"] for c in result["calls"]], ["jev"])
        self.assertEqual(result["eos_commands"]["r1"], ["interface Ethernet1", "ip ospf cost 300", "exit"])
        self.assertIsNone(result["repair_success"])
        self.assertIsNone(result["cost_usd"])

    def test_low_probability_escalates(self):
        result = self.engine(MockJev("avoid_r2_path", 0.6)).decide(self.observation)
        self.assertEqual([c["stage"] for c in result["calls"]], ["jev", "llm"])

    def test_healthy_route_still_requires_independent_evaluation(self):
        result = self.engine(MockJev("no_change"), NoCallLLM()).decide(self.observation)
        self.assertEqual(result["handoff"], "independent_evaluation_required")
        self.assertEqual(result["eos_commands"], {})
        self.assertIsNone(result["repair_success"])

    def test_jev_timeout_escalates_without_leaking_error(self):
        result = self.engine(BrokenJev()).decide(self.observation)
        self.assertEqual(result["screening"]["error_type"], "TimeoutError")
        self.assertNotIn("must not be written", json.dumps(result))
        self.assertEqual(result["handoff"], "verification_required")

    def test_bypass_jev_comparison(self):
        result = self.engine().decide(self.observation)
        self.assertEqual([c["stage"] for c in result["calls"]], ["llm"])
        self.assertTrue(result["screening"]["bypassed"])

    def test_bad_probabilities_escalate(self):
        class BadJev:
            def classify(self, *args):
                return Reply({"choice": "no_change", "probabilities": {"no_change": float("nan")}}, "bad", 1)
        result = self.engine(BadJev()).decide(self.observation)
        self.assertEqual(result["screening"]["effective_route"], "escalate")

    def test_unknown_device_returns_error_without_commands(self):
        self.patch["changes"][0]["device"] = "outside-lab"
        result = self.engine().decide(self.observation)
        self.assertEqual(result["status"], "decision_error")
        self.assertEqual(result["eos_commands"], {})
        self.assertIsNone(result["proposal"])
        self.assertEqual(result["calls"][0]["model"], "mock-llm")

    def test_unknown_interface_returns_error(self):
        self.patch["changes"][0]["interface"] = "Ethernet99"
        self.assertEqual(self.engine().decide(self.observation)["status"], "decision_error")

    def test_out_of_range_cost_and_bool_cost_rejected(self):
        for value in (0, 65536, True, "10"):
            with self.subTest(value=value):
                proposal = deepcopy(self.patch)
                proposal["changes"][0]["value"] = value
                with self.assertRaises(ValidationError):
                    Proposal.model_validate_json(json.dumps(proposal))

    def test_shell_and_cli_injection_rejected(self):
        self.patch["changes"][0]["interface"] = "Ethernet1\nshutdown"
        self.assertEqual(self.engine().decide(self.observation)["status"], "decision_error")

    def test_arbitrary_commands_rejected(self):
        self.patch["commands"] = ["reload"]
        self.assertEqual(self.engine().decide(self.observation)["status"], "decision_error")

    def test_duplicate_setting_rejected(self):
        self.patch["changes"].append(self.patch["changes"][0])
        self.assertEqual(self.engine().decide(self.observation)["status"], "decision_error")

    def test_observation_request_has_no_commands(self):
        self.patch.update(outcome="needs_observation", changes=[], observation_requests=["Measure both paths under the same offered load."])
        result = self.engine().decide(self.observation)
        self.assertEqual(result["handoff"], "collector_required")
        self.assertEqual(result["eos_commands"], {})

    def test_rejection_forces_llm_and_includes_feedback(self):
        feedback = TypeAdapter(list[Feedback]).validate_json((ROOT / "examples/rejection.json").read_text())
        class CaptureLLM(MockLLM):
            seen = None
            def propose(self, system, state):
                self.seen = state
                return super().propose(system, state)
        llm = CaptureLLM(self.patch)
        result = self.engine(MockJev("no_change"), llm).decide(self.observation, feedback, 2)
        self.assertEqual(result["screening"]["effective_route"], "escalate")
        self.assertFalse(llm.seen["feedback"][0]["accepted"])

    def test_failed_rollback_blocks_next_attempt(self):
        feedback = Feedback(attempt=1, candidate_id="test", stage="runtime", accepted=False,
                            details=["Runtime measurements worsened"], rollback_succeeded=False)
        with self.assertRaises(ValueError):
            self.engine().decide(self.observation, [feedback], 2)

    def test_trial_must_stop_after_runtime_acceptance(self):
        feedback = Feedback(attempt=1, candidate_id="test", stage="runtime", accepted=True,
                            details=["Measurements meet requirements"])
        with self.assertRaises(ValueError):
            self.engine().decide(self.observation, [feedback], 2)

    def test_attempt_bounds_and_feedback_order(self):
        for attempt in (0, 6):
            with self.assertRaises(ValueError):
                self.engine().decide(self.observation, attempt=attempt)
        feedback = TypeAdapter(list[Feedback]).validate_json((ROOT / "examples/rejection.json").read_text())
        with self.assertRaises(ValueError):
            self.engine().decide(self.observation, feedback, 1)

    def test_threshold_must_be_finite(self):
        for threshold in (float("nan"), -0.1, 1.1):
            with self.assertRaises(ValueError):
                self.engine(threshold=threshold)


if __name__ == "__main__":
    unittest.main()
