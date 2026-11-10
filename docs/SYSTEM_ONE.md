# Optional Jev screening in the Abilene closed loop

`python -m advnet.experiment run --system-one off` is the default LLM-only arm.
`--system-one jev` inserts the official TypeSafe Jev classifier before the LLM.
Use the README repair command and change only this flag between matched runs.

## Credentials and dependencies

Install `uv sync --extra openai --extra batfish --extra jev`. Both models use
`OPEN_ROUTER_KEY`, passed explicitly to their SDK clients. `LLM_MODEL` selects
Nemotron (or another chat model); `SYSONE_MODEL` independently selects Jev.
Neither `TYPESAFE_API_KEY` nor the old OpenAI key variable is read by this app.
Missing shared credentials or a missing System One model in Jev mode are
rejected before fault injection.

The LLM uses OpenRouter's `/api/v1/chat/completions` endpoint. Jev uses the
[official TypeSafe SDK through OpenRouter](https://openrouter.ai/docs/guides/community/typesafe-sdk),
pointed at `https://openrouter.ai/api`; it appends `/v1/systemone`. Request
compression is disabled for gateway compatibility. Both requests are billed
through OpenRouter, with SDK retries disabled.

```bash
read -rsp "OpenRouter API key: " OPEN_ROUTER_KEY
printf '\n'
export OPEN_ROUTER_KEY
export LLM_MODEL="nvidia/nemotron-3-ultra-550b-a55b:free"
export SYSONE_MODEL="typesafe/jev-1.14"
```

`--sysone-model` overrides `SYSONE_MODEL`; `--jev-model` remains a CLI alias.
`--model` overrides `LLM_MODEL`. Jev mode requires a System One ID; baseline mode
needs only the LLM ID and shared key. `--jev-threshold` defaults to 0.9 and
compares the selected option's probability, not the SDK confidence field. It is
uncalibrated; tune it on development cases and freeze it for held-out evaluation.
`--jev-timeout` defaults to 10 seconds before LLM fallback.

## Decision behavior

Both modes receive an identical catalog generated only from public topology
inventory. Initially it has one enable-host-interface action per Abilene router
(11 total), plus Jev's `no_change` and `escalate` choices. The classifier sees live
configs and measurements and chooses whether a routine action is warranted.
The catalog contains no scenario names, injected-fault labels, or private
ground-truth repairs. It does not yet contain OSPF or congestion actions.

- A valid action above the threshold returns a bounded candidate without an LLM call.
- Escalation, low probability, malformed distributions, or Jev errors invoke the LLM.
- After any rejected attempt, screening is bypassed and the next attempt uses
  the LLM with fresh observations and rejection feedback.
- A `no_change` claim cannot mark an impaired trial successful. Independent
  measurements and the existing trial controller determine the outcome.
- Both arms use identical Batfish, exact-config readback, deployment, live
  acceptance, rollback, attempt limits, and time caps. System One never bypasses them.

`--catalog path.json` supplies a custom catalog to both arms. The schema is
`dict[str, CatalogAction]` from `advnet/contracts.py`. Invalid/reserved actions
and unknown device/interfaces are rejected. Use `{}` to reproduce the historical
empty-catalog LLM inputs. Custom catalogs must be held fixed across comparisons.

## Comparison records

Each result records `mode` (`llm_only` or `jev_llm`), `system_one_enabled`,
requested models/settings, a catalog SHA-256, per-attempt screening outcomes,
and per-provider usage/timing. The actual catalog is saved as `catalog.json`.
Raw model-call records retain resolved model IDs and available provider usage.
`inference_metrics` separates Jev and LLM calls, input/output tokens, and seconds.
Jev trial journals use the `system_one_decision` stage; baseline journals retain
`llm_decision`. The existing visualizer can display both journals and candidates.

`scripts/summarize-trials.py` exports both modes in one CSV, including unsuccessful
trials. It supports OpenAI/OpenRouter `prompt_tokens`/`completion_tokens` as well
as TypeSafe `input_tokens`/`output_tokens`. Inference timings come from individual
calls; `decision_seconds` includes the whole decision subprocess. Total repair
time remains fault-to-runtime-acceptance, not model latency alone.

Prices are only summed when every recorded call reports them. OpenRouter's numeric `usage.cost` is retained for both models, including when
the TypeSafe SDK omits this gateway-specific field from its parsed usage model.
If either call lacks a reported cost, the combined cost stays unknown. No hardcoded pricing is used. Failed
requests with missing usage stay unknown. There is no batch-trial automation or
automatic healthy reset after a successful repair; run each comparison from a
known, equivalent state with the same fault, load, model, budgets, and observer
settings. Historical empty-catalog runs are not matched to the new default inputs.

The [fault benchmark](SCENARIO_SUITE.md) adds 30 validated cases and private
routing labels. Use `--reset-baseline` to restore source configs and normalized
capacities before each arm. `--force-escalation` is a separate controlled probe:
it still calls Jev before forcing LLM fallback and is excluded from natural
routing-accuracy scores.

## Local checks

```bash
.venv/bin/python -m unittest discover -s tests
RUN_BATFISH_INTEGRATION=1 ADVNET_TEST_OUTPUT_DIR=captures/jev-integration-tests \
  .venv/bin/python -m unittest discover -s tests
```

Tests cover shared inputs, credential preflight, the official SDK over a mock
HTTP transport, high-confidence catalog selection through the simulated full
loop, verifier rejection, fallback, and stage-specific accounting. The opt-in
Batfish test uses a real local verifier with a simulated lab. Real Jev trials
require the shared OpenRouter key; no paid request is needed for these tests.
