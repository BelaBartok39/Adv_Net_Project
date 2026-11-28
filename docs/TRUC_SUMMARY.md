# What we’ve learned from the Abilene experiments

We started with two questions. Can a network repair itself and improve performance while traffic is running? And when does it help to put Jev in front of an LLM? Jev can choose a known repair from a catalog or ask the LLM for help. Batfish checks the proposal against the network’s rules. The controller counts a repair as successful only after it passes checks on the live network.

### 1. Could we trust the lab to recognize a good repair?

On the healthy network, all 110 directed host pairs could reach each other. We then forced Jev to propose a path through Los Angeles. Batfish accepted it when the path was allowed and blocked deployment when the same path was forbidden. An early pilot also caught us checking some correct repairs too soon, while OSPF neighbors were still reconnecting. Once we waited for them to finish, all 14 replacement pilot trials passed. [Verification results](EXPERIMENT_RESULTS.md#verification-can-reject-a-model-selected-forbidden-path)

### 2. Could Jev take routine repairs off the LLM?

We tested 30 scenarios one fault at a time, from disabled host interfaces to routing failures and congestion. After 60 coverage trials, we ran 300 trials with Luna. **Luna alone and Jev plus Luna each repaired 150/150 faults. Jev reduced LLM calls from 159 to 102**, using its catalog in 52 of 55 single-host cases. Median repair time was similar at 49 and 51 seconds, respectively. Smaller six-case comparisons gave DeepSeek Flash 6/6 alone and 5/6 with Jev, and Gemini Flash-Lite 5/6 each way. The clearest benefit was fewer LLM calls on familiar faults. [Bounded campaign results](EXPERIMENT_RESULTS.md#bounded-repair-and-jev-comparison)

### 3. Could the network optimize while carrying traffic?

First we had to trust the measurements. Under the same traffic load, reducing simultaneous probes from 24 to four brought measured round-trip time from 21.36 to 4.18 milliseconds in a New York test. We used four for later trials. A 20-minute Luna run rolled back three optimization attempts. When we tested a known route change directly, splitting traffic across paths reduced peak load on the busiest link from **89% to 52%** while keeping delay under 5 milliseconds. That gave us a concrete improvement for the controller to find. [Probe and route-split results](EXPERIMENT_RESULTS.md#continuous-measurement-and-optimization-findings)

### 4. Could it repair a slow network when every host was still reachable?

We added a delay from Denver to Kansas City. OSPF still saw a working link, but average round-trip time across the network rose to about 87 milliseconds. Luna and DeepSeek Pro found partial fixes that left delay around 58 milliseconds. Pinning DeepSeek to one provider and giving it a summary of the slow paths also fell short. Terra found a route around the delay. In three fresh repetitions it restored round-trip time to **5.58–6.29 milliseconds while the fault was still present**. [Model comparisons](EXPERIMENT_RESULTS.md#reviewed-model-results) · [Repeated Terra trials](EXPERIMENT_RESULTS.md#repeated-terra-campaign)

Repair and optimization sometimes pulled in different directions. In one run, a later change brought peak load down to 52% but sent delay back to 87 milliseconds, so the controller rolled it back. After the fault was removed, two of the three runs lowered peak load to about 52% and kept delay low. [Optimization results](EXPERIMENT_RESULTS.md#repeated-terra-campaign)

### 5. What did Jev add on that harder fault?

Terra alone and Jev plus Terra both repaired **4/4** in a matched comparison. Jev escalated all four times. Median time to acceptance was 100 seconds with Terra alone and 142 seconds with Jev plus Terra. The catalog knew host repairs. This fault needed a routing change. That raised the question behind the “thinking cache.” **If Terra solves a fault once, can Jev reuse the verified repair next time?** [Four-pair comparison](EXPERIMENT_RESULTS.md#completed-terra-only-versus-jevterra-comparison)

### 6. Could we teach the catalog a repair?

In the first two training runs, Terra found the same fix twice, but two host pairs differed in whether their probes counted as slow. The cache treated them as different cases. We allowed a little variation in the symptoms and tried again. This time the symptoms matched exactly, but Terra chose different routing costs. Both fixes worked and gave the same modeled next hops for all 110 router pairs. To establish that one exact fix worked twice, we selected it in advance and verified it on a fresh fault. It then qualified for Jev’s catalog. [First training pilot](EXPERIMENT_RESULTS.md#first-live-action-memory-training-pilot-two-repairs-no-promotion) · [Second training campaign](EXPERIMENT_RESULTS.md#version-2-live-training-results) · [Independent validation](EXPERIMENT_RESULTS.md#designated-validation-promoted-the-first-v2-repair)

### 7. Would Jev use the saved repair?

On the next live recurrence, the cache offered the right repair. Jev asked Terra for help, and Terra proposed the **exact saved change**. The network recovered in **84.72 seconds**, with over three minutes left before fault removal. To understand the handoff, we replayed Jev’s decision using reconstructed measurements. It ranked the repair first at **0.65** and escalation second at **0.35**. Using the catalog requires **0.90**. The live trial recorded only its top score, so we know the winning choice for the replay only. [Live trial and first replay](EXPERIMENT_RESULTS.md#first-natural-jev-decision-with-a-frozen-promoted-repair)

### 8. Would explaining when the repair fits help Jev choose it?

We changed only the description and asked Jev about two cases. For the reconstructed Denver fault, describing the symptoms raised the saved repair’s score from **0.68 to 0.73**. For a modeled Atlanta–Washington delay that needed a different fix, the same wording lowered its score from **0.59 to 0.44**. Jev now ranked escalation first on that second case. All four decisions would still escalate under the 0.90 rule. [Paired replay results](EXPERIMENT_RESULTS.md#paired-jev-replay-with-a-symptom-specific-catalog-description)

**Where we are now.** We’ve shown that the network can recover, the cache can retrieve a verified repair, and Jev can rank it first on a replay. The wording comparison had one call per condition. Repeating it and checking a second measured fault would tell us whether the distinction holds up. The live milestone is a recurring fault that Jev handles from the learned catalog with zero LLM calls and a repair that passes the network checks.
