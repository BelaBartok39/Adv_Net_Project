# NetConfArena comparison and possible Jev evaluation

Reviewed 2026-11-27 against [NetConf.pdf](../NetConf.pdf),
*NetConfArena: An Executable Benchmark for LLM Agents in Closed-Loop Network
Configuration*, Chang Liu, Xiaohui Xie, Xinyi Chen and Yong Cui,
arXiv:2608.23179v1, 24 August 2026, and the
[released repository](https://github.com/liujona/NetConfArena).
Local PDF SHA-256: `cf46f488e7857e73ec0e2c104fea06521039e9694fbc89c0b9afa257f10e68a3`.

## Relationship to this project

NetConfArena is close related work. It already demonstrates an executable,
multi-device closed loop in which LLM agents observe, configure, validate and
retry. We should not claim those features alone distinguish this project.

The paper asks how reliably LLM agents complete configuration tasks and how
they fail. Our experiments ask whether a Jev decision stage helps an LLM-based
controller, and whether a guarded persistent controller can repair and improve
network performance as conditions change. These questions overlap but are not
equivalent. The current evidence supports bounded repair and selected guarded
continuous repairs; repeatable autonomous optimization remains an open result.

| Dimension | NetConfArena | This project |
| --- | --- | --- |
| Primary study | LLM configuration-task capability and interaction failures | Jev contribution, bounded fault repair, and continuous performance management |
| Environment | GNS3 with Cisco IOS task networks | Containerlab with cEOS on Abilene |
| Protocol scope | Static routing, RIP, OSPF, BGP, MPLS and overlays | Current experiments concentrate on OSPF, interface state and traffic steering |
| Agent actions | Configuration retrieval, configuration commands, diagnostic commands, waiting and submission | Typed bounded patches; limited observation requests; persistent policy controls dispatch and deployment |
| Budget | 20 action turns in the paper | Current continuous trials allow three decision attempts per episode and one observation round, within a timed session |
| Correctness | Hidden executable task tests on the terminal state; configuration F1 is secondary | Batfish checks and measured runtime acceptance, configuration readback and rollback |
| Performance | Paper explicitly excludes latency, congestion and transient routing dynamics from its evaluation scope | Probe RTT, receiver delivery/loss, directional utilization and fault-to-acceptance time are measured |
| Lifecycle | A configuration episode ends in submission or budget exhaustion | Continuous controller keeps monitoring after interventions; bounded runs are also supported |
| Jev comparison | No Jev arm reported in the supplied paper | Matched LLM-only and Jev+LLM bounded campaigns; continuous Jev+LLM trials so far |

Paper anchors: agent actions and hidden evaluation, PDF pages 4–5; setup,
page 7; main results, page 8; limitations, page 10. Repository anchors:
[ReAct configuration](https://github.com/liujona/NetConfArena/blob/main/agent/configs/ReAct.yaml)
and [evaluation entry point](https://github.com/liujona/NetConfArena/blob/main/main.py).

The paper's controls and ours are not interchangeable. Its 20 action turns
are not equivalent to our three inference attempts. Configuration completion
is not equivalent to recovering baseline latency under an injected impairment.
Its score must not be compared numerically with our acceptance rate as though
they measured the same task difficulty.

## Findings relevant to our failures

The paper evaluates four models from two families with thinking enabled and
disabled, giving eight settings. It uses five fixed-seed instances for each of
96 templates: 480 instances per setting and 3,840 trajectories overall.
Decoding temperature is zero. These seeds vary task parameters, not the
underlying template topology (PDF page 7).

DeepSeek V4 Pro with thinking disabled achieved a mean test-case score of
0.961 and a full-task pass rate of 0.852. With thinking enabled the full-task
pass rate was 0.738 (Table II, page 8). These are different metrics: a high
partial-test score does not mean nearly all complete tasks pass. Exact model
snapshot, endpoint and reasoning settings must be checked before attempting a
numerical reproduction; the name DeepSeek alone does not establish equivalence
to our `deepseek/deepseek-v4-pro-0813` runs.

The authors identify failures involving protocol knowledge, unproductive
inspection, premature abandonment and deviation from task requirements. They
also report malformed actions and repeated ineffective actions. Our partial
SVA-only repairs and JSON parsing errors are compatible with that broader
picture, but compatibility is not proof of the same underlying cause.

Our repeated DeepSeek failures therefore do not contradict the paper's high
configuration-task scores. We use different tasks, interfaces, observations,
budgets and performance requirements. The paper itself states that rollback
procedures and performance dynamics are outside its evaluated scope (page 10).
Its discussion proposes stronger state management, procedural skills and
pre-execution constraints as future harness improvements; those are useful
connections to our catalog and independent verification design.

## What we can claim now

The 300-trial bounded campaign shows fewer LLM calls with Jev, successful
repair in both arms, and a clearer timing benefit on catalog-covered single-host
cases. It does not show an overall speed or cost advantage. Two Terra trials
demonstrated natural continuous latency repair, while later harmful proposals
were rejected and rolled back. These are results on our benchmark, not on
NetConfArena; see [reviewed results](EXPERIMENT_RESULTS.md).

We can describe independent model selection, modeled verification, runtime
performance acceptance, rollback and continued monitoring as our system design.
We should not claim that combining these is globally novel without a wider
literature review, or that our controller generally outperforms NetConfArena
agents. The latter would require a matched external benchmark evaluation.

## A fair external Jev experiment

The most direct extension uses their task renderer, emulator interface and
hidden evaluator. Add a Jev decision adapter inside their agent loop rather
than porting their tasks into our current bounded EOS patch language. Our
default catalog contains host-interface enable actions and will not solve their
general IOS configuration tasks without new reusable actions.

Use three arms if the Jev arm gets a new playbook catalog:

1. **Published-style baseline:** the released ReAct LLM agent with its original
   interface and prompts.
2. **Catalog control:** the same LLM agent with access to the same public,
   parameterized procedures and candidate actions supplied to Jev.
3. **Jev plus LLM:** the same catalog and LLM, with Jev selecting supported
   actions or escalating uncertain cases to that LLM.

Arm 3 versus arm 2 estimates the contribution of Jev selection under shared
catalog information. Arm 3 versus arm 1 measures the combined Jev-and-catalog
system change; arm 2 versus arm 1 measures the catalog/harness contribution.
A two-arm comparison that gives only Jev the catalog cannot attribute an
improvement solely to Jev.

Jev may choose an existing typed tool action or a procedure whose parameters
are bound from public goals and observed state. It does not generate arbitrary
new IOS commands from a classification label. Unknown or unsupported situations
must escalate. Freeze the action library and threshold before evaluation.
Do not feed either agent reference configurations, hidden tests or evaluator
scores during interaction, and do not map template identifiers directly to
stored solutions.

Keep task instances, initial configurations, model snapshot, provider,
temperature, reasoning mode, system prompt, output budget, tool behavior and
20-action-turn budget matched. Count every action inside a catalog procedure
against that same budget; batching a procedure must not create free turns.
Count Jev latency, classification calls, retries and charges even when no LLM
call occurs. Share tool results/history with the LLM after Jev actions so that
escalation does not lose state.

Use the unmodified hidden evaluator for the primary endpoint: every required
test must pass. Also report partial test-case score, turns, invalid and repeated
actions, budget exhaustion, LLM calls/tokens, Jev overhead and end-to-end time.
Report failures and timeouts alongside successful-run timing. If we add Batfish,
runtime performance constraints or automatic rollback, add them symmetrically
to the relevant comparison arms and label that a separate harness study.
They should not be quietly added to a claimed reproduction of the paper.

## Practical starting point

The [released setup instructions](https://github.com/liujona/NetConfArena#environment-setup)
require GNS3 2.2.x and user-supplied Cisco IOS images; our current cEOS lab
does not supply that environment. Use a separate checkout and Python
environment. First run the upstream reference-validation check without an LLM
to verify local images and evaluators, then reproduce one small baseline run.
Do not run new emulator workloads alongside the active Abilene campaign on the
same resource-constrained host.

Start with an explicitly labeled OSPF subset rather than immediately claiming
coverage of all 96 templates. The supplied suite includes single-area OSPF,
stub/NSSA areas, election and reference-bandwidth tasks. Template 6 was inspected
during this review, including its reference data; treat it as development-only
if it informs catalog design. Hold out other templates as well as parameter
seeds when assessing generalization of newly authored procedures. Changing
only router names or IP addresses is weaker evidence than unseen task families.

A small plumbing pilot could use three development templates, two seeds and
three arms: 18 runs. It would test integration, not establish superiority.
After freezing the catalog, choose a held-out task set and enough paired
repetitions to estimate success differences and timing variability. The full
paper-scale workload is 480 episodes per arm, so two arms would require 960
episodes and three would require 1,440, before additional repetitions.

An EOS/Containerlab port is an alternative if the GNS3/IOS environment is
unavailable. It would require translated commands and revalidated tests and
should be called a NetConfArena-inspired adaptation, not a direct reproduction.

**Current decision:** external Jev evaluation is feasible in principle and
would strengthen generalization claims, but it is a separate experiment stream.
Initial integration has now started in branch `feature/netconfarena-jev`, in
the separate `downloads/worktrees/netconfarena-jev` worktree. Its
[integration progress](../downloads/worktrees/netconfarena-jev/docs/NETCONFARENA_INTEGRATION.md)
records an offline-tested adapter, pinned upstream source and remaining live
readiness work. The reviewed upstream revision
`7f99e17a641f9e598dd0073d776637f361b5cdd5` uses temperature 0.5 in its released
client, whereas the supplied paper specifies 0; this discrepancy must be
resolved and recorded before a reproduction campaign. No live external
benchmark or billable evaluation has been started. Keep the ongoing Abilene
repair and optimization study distinct.
