# Magentic neuro-symbolic RCA

## Summary

This prototype combines source-linked telemetry representations with a Magentic investigator and restricted Prolog execution. With a five-minute case ceiling, routing completed both development investigations with executed Prolog support, scoring 50% strict accuracy and 75% partial credit. Fixed GLM-5.2 completed neither after model-request timeouts exhausted its retry allowance. This demonstrates live end-to-end execution on two cases, but not reliable causal diagnosis or performance under the competition workload.

## Design

CSV telemetry is accessed through DuckDB and a lazy Parquet cache. Metric summaries retain baseline coverage, source filters, transformation assumptions, and onset intervals. Trace parent-child joins use trace and span identity. Logs use bounded aggregation with Drain3 or structured proxy parsing. Raw telemetry remains outside the KB; incident facts summarize observations and relationships.

One Microsoft Agent Framework Magentic manager coordinates one investigator. The investigator can inspect the paginated KB, retrieve source evidence, inspect telemetry, propose Prolog rules, and save a provisional diagnosis. Measured facts are immutable. Proposed rules are checked under a restricted goal whitelist, tabling, time limits, and bounded results. Grounded arithmetic and aggregation are supported.

Accepted completion requires an investigator diagnosis linked to successful execution bindings and the current KB. Provisional and numerical fallback outputs remain explicitly incomplete. Successful Prolog execution establishes consequences of encoded assumptions, not independent proof of causation.

Tracing saves sanitized model requests and responses, tool inputs and outputs, framework errors, and interrupted calls. Symbolic executions retain facts, rules, queries, results, and a KB fingerprint. These artifacts make failures inspectable even when the final answer is a fallback.

## Completed comparison: 90 seconds per case

Two previously inspected development cases, two repeats per configuration, eight case executions total. Each configuration/repeat starts with a separate cold cache. Runtime queries exclude labels; the offline scorer reads labels. This is an integration comparison, not an unseen-deployment benchmark.

| Metric | Routed | Fixed GLM-5.2 |
|---|---:|---:|
| Strict accuracy, mean ± sample SD | 0.50 ± 0.00 | 0.50 ± 0.00 |
| Partial score, mean ± sample SD | 0.50 ± 0.00 | 0.50 ± 0.00 |
| Dollars per case, mean ± sample SD | $0.0863 ± $0.0189 | $0.2171 ± $0.0951 |
| Seconds per case, mean ± sample SD | 89.76 ± 0.63 | 90.12 ± 0.08 |
| Completed investigations / case executions | 0 / 4 | 0 / 4 |
| Numerical fallback outputs | 4 | 3 |
| Provisional outputs | 0 | 1 |
| Successful / attempted Prolog executions | 0 / 0 | 0 / 0 |

SD is calculated across the two repeat-level averages. Routing reduced observed cost per case by 60.3%, with the same submitted-answer score. Equal fallback-heavy scores do not establish equivalent reasoning quality. Both configurations got the same one of the two cases correct in each repeat.

Six case executions stopped with TimeoutError. Two stopped with ChatClientException wrapping a case-deadline BudgetExceeded error. Thus all eight terminal interruptions were deadline-related. Across the traces, tool starts comprised 39 KB inspections, 32 evidence retrievals, 10 change searches, four data descriptions, four topology requests, one trace inspection, one log inspection, and one provisional diagnosis. These counts show evidence interaction occurred, but symbolic execution was never reached. They do not isolate how much time was spent on model latency versus orchestration or tools.

Sources: [results](eval/comparison-90s/results.json), [repeat statistics](eval/comparison-90s/repeat-statistics.json), and [manifest](eval/comparison-90s/manifest.json).

## Completed diagnostic comparison: 300 seconds per case

The same two development cases, routed versus fixed GLM-5.2, one repeat, with separate cold caches. Code, prompts, and submission defaults were unchanged. The experimental case ceiling was raised to five minutes. Existing $1-per-case spending, 25-second model-request timeouts, tool-count, manager-round, and total-run limits remained active.

| Metric | Routed | Fixed GLM-5.2 |
|---|---:|---:|
| Strict accuracy | 50% | 50% |
| Partial score | 75% | 50% |
| Completed investigations / cases | 2 / 2 | 0 / 2 |
| Successful / attempted Prolog executions | 7 / 7 | 11 / 11 |
| Numerical fallback outputs | 0 | 2 |
| Dollars per case | $0.2860 | $0.2042 |
| Seconds per case, including cold-start overhead | 146.99 | 83.86 |

Routed case 0 completed in 215.96 seconds. It identified `shippingservice-1` correctly but diagnosed container memory load instead of container read I/O load, receiving 0.5 partial credit and failing strict accuracy. Routed case 1 completed in 77.61 seconds and received full credit on that case's requested scoring fields. Both saved provisional diagnoses before successful completion; the raw harness field `provisional_cases=2` counts those intermediate saves, not incomplete final outputs.

Fixed case 0 executed eleven successful Prolog queries but ended after 166.39 seconds when repeated model-request timeouts exhausted the permitted model's retries. Its unavailable-model state persisted into case 1, which fell back after 0.94 seconds. The terminal exception names provider/transport retries, but the recorded underlying errors were client timeouts; these logs do not establish an external provider outage. Fixed's lower observed cost and time reflect early termination and are not a clean efficiency advantage.

The comparison cost $0.9803 in total. One repeat cannot estimate repeat variability. These are reused development cases, not a representative test set. The higher completion count compared with the 90-second experiment is consistent with time pressure being a blocker, but stochastic model responses and unequal repeats prevent attributing the entire difference to the changed ceiling. The routed average also exceeds the roughly one-minute-per-case allowance implied by a 20-case, 20-minute judging run. This experiment does not demonstrate competition-runtime readiness.

The [saved completed-case replay](eval/completed-case-replay/) includes the exact query, KB snapshot, bindings, diagnosis, and generated evidence. It illustrates a semantic limitation: the model interpreted a normal `system.mem.total` observation as excluding host memory pressure. A stable total-capacity metric does not justify that inference. The completion gate verifies symbolic execution and evidence linkage, not every causal assumption.

Sources: [results](eval/comparison-300s/results.json), [manifest](eval/comparison-300s/manifest.json), and [failure details](eval/comparison-300s/failure-details.json). All four planned case executions produced outputs; two investigations completed. No additional tuning or rerun followed these results.

## Interrupted full-development run

A separate 70-case invocation was interrupted after 47 saved predictions. All 47 saved investigations were incomplete, with no Prolog executions. Its 18-minute total target initially allocated about 15 seconds per case. This was too restrictive to answer whether the investigator could complete with a generous budget. No full 70-case accuracy result is reported. See [partial-run metadata](eval/partial-70-case-run.json).

An earlier duplicate launch wrote to the same output directory. Those mixed artifacts were archived and excluded; the 47-case partial run came from the subsequent clean restart.

## Validation and earlier diagnostic evidence

The copied root submission built successfully and passed an additional two-case Linux offline smoke check with 2 CPUs, 8 GB, no network, and a read-only root filesystem; see [fork validation](eval/fork-validation.json). Latest readiness records show 46 passing offline tests, official output-format validation with zero warnings, and two Linux offline cases with 2 CPUs, 8 GB, a read-only root filesystem, and no network. Real-data Prolog aggregation/arithmetic counted 41 candidates and doubled that count to 82. Simulated framework tests exercise the investigator completion gate. These checks validate infrastructure, not live diagnosis accuracy.

An earlier implementation's two 180-second development cases produced nine successful Prolog executions, but neither investigation completed. One saved a provisional diagnosis; the other fell back after tool-generation failures. These prior-version results demonstrate actual symbolic execution in a debugging context and must not be attributed to the final paired comparison. Historical measurements and their limitations are retained in [eval/](eval/).

## Limitations and future work

Completion within competition budgets and correct causal discrimination remain unresolved. The five-minute experiment enabled routed completion but exposed fixed-model request timeouts and persistent model unavailability. Future work should examine request-timeout policy and retry-state recovery, then validate the semantic assumptions behind accepted diagnoses. Evidence inspection and orchestration consume time, but this experiment does not isolate their contributions.

Anomaly magnitude can select downstream symptoms. Confidence is not calibrated. Trace duration units remain unverified, bounded log sampling can omit rare clues, and missing baselines remain unknown. No causal-identification guarantee follows from logical consistency. The final implementation has not completed a paired hidden-deployment evaluation.

Future work should first establish repeatable supported completion, then improve diagnosis discrimination and evaluate additional cases under realistic budgets. A specialist Prolog agent remains optional. PyRCA, DoWhy-GCM, Z3, and SCRAM are deferred.

## Reproduction and disclosure

The runtime fingerprint for the evaluated code, prompts, Prolog, dependencies, and Dockerfile is `a2e69d6ecabca792268f7580c2bf6bf7ae0e2a6e8b0cf627cdd587964f5efd51`. Root README commands reproduce the interface and comparison. Cost estimates use recorded provider usage and conservative estimates for interrupted calls; provider billing is authoritative.

The participant selected the architecture and scope. OpenAI Codex generated and debugged implementation, tests, and documentation. Runtime uses Microsoft Agent Framework Magentic and permitted Featherless GLM models. The supplied scorer and baseline examples are retained. See [README.md](README.md) for full disclosure and dependency attribution.
