import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import TypeAdapter

from .contracts import CatalogAction, Feedback, Observation
from .pipeline import DecisionEngine
from .providers import Jev, LLM, MockJev, MockLLM


def main():
    parser = argparse.ArgumentParser(description="Produce an unverified network-change candidate; never deploy it.")
    parser.add_argument("observation", type=Path)
    parser.add_argument("--catalog", type=Path, default=Path("examples/actions.json"))
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--mock-route", default="escalate")
    parser.add_argument("--mock-probability", type=float, default=0.95)
    parser.add_argument("--mock-proposal", type=Path, default=Path("examples/mock-proposal.json"))
    parser.add_argument("--without-jev", action="store_true")
    parser.add_argument("--threshold", type=float, default=0.9)
    parser.add_argument("--feedback", type=Path, help="JSON array of feedback from earlier attempts")
    parser.add_argument("--attempt", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--jev-model", default=os.environ.get("JEV_MODEL", "jev-latest"))
    parser.add_argument("--llm-provider", choices=["openai", "anthropic", "compatible"], default="openai")
    parser.add_argument("--llm-model", default=os.environ.get("LLM_MODEL"))
    parser.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL"))
    parser.add_argument("--timeout", type=float, default=60, help="Per SDK request timeout; not a trial deadline")
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--log", type=Path, default=Path("runs/decisions.jsonl"))
    args = parser.parse_args()
    if args.timeout <= 0 or args.max_tokens < 1:
        parser.error("timeout and max-tokens must be positive")
    if args.mode == "live" and not args.llm_model:
        parser.error("Live mode requires an exact --llm-model ID (or LLM_MODEL)")
    # Check logging before making a potentially billable provider request.
    try:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        with args.log.open("a"):
            pass
    except OSError as error:
        print(json.dumps({"status": "log_error", "error_type": type(error).__name__,
                          "hint": "Choose a writable --log path or correct the log file ownership."}),
              file=sys.stderr)
        return 2
    try:
        observation = Observation.model_validate_json(args.observation.read_text())
        catalog = TypeAdapter(dict[str, CatalogAction]).validate_json(args.catalog.read_text())
        feedback = TypeAdapter(list[Feedback]).validate_json(args.feedback.read_text()) if args.feedback else []
        if args.mode == "mock":
            if args.mock_route not in {"no_change", "escalate", *catalog}:
                parser.error("mock-route must be no_change, escalate, or a catalog action ID")
            if not 0 <= args.mock_probability <= 1:
                parser.error("mock-probability must be between zero and one")
            screener = MockJev(args.mock_route, args.mock_probability)
            llm = MockLLM(json.loads(args.mock_proposal.read_text()))
        else:
            screener = Jev(args.jev_model, args.timeout)
            llm = LLM(args.llm_provider, args.llm_model, args.timeout, args.max_tokens, args.base_url)
        engine = DecisionEngine(None if args.without_jev else screener, llm, catalog, args.threshold)
        result = engine.decide(observation, feedback, args.attempt)
        result["mode"] = args.mode
        result["mock"] = args.mode == "mock"
        result["requested_models"] = {"jev": None if args.without_jev else args.jev_model,
                                      "llm": args.llm_model}
    except (ValueError, OSError) as error:
        # No payloads/exception strings: configuration text or SDK errors may contain secrets.
        print(json.dumps({"status": "input_error", "error_type": type(error).__name__}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    try:
        with args.log.open("a") as stream:
            stream.write(json.dumps(result, sort_keys=True) + "\n")
    except OSError as error:
        print(json.dumps({"status": "log_error", "error_type": type(error).__name__,
                          "hint": "The decision is printed above but could not be saved."}), file=sys.stderr)
        return 2
    return 1 if result["status"] == "decision_error" else 0


if __name__ == "__main__":
    sys.exit(main())
