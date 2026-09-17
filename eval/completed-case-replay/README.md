# Demo: one root cause investigation

> **Recorded execution, not a live run.** This walkthrough summarizes actual saved tool calls and results. The investigator completed its evidence checks, but identified the wrong failure reason.

| Run | Result |
|---|---|
| Agent | Routed GLM models with Magentic and Prolog |
| Duration | **3 minutes 36 seconds** |
| Tool calls | **15**, including **6 successful Prolog executions** |
| Investigation | **Completed with linked Prolog support** |
| Scored answer | **Correct component; incorrect failure reason** |

## 1. The incident

The supplied question asked us to identify **one failed component and its failure reason** in cloudbed-1, between **09:00 and 09:30 on March 20, 2022**.

The agent received telemetry observations and access to evidence tools. Ground-truth labels were used afterward by the scorer, not supplied to the investigator.

## 2. Inspect the evidence · 00:23

The agent retrieved two observations on `shippingservice-1`:

| Signal | Saved observation | Why it mattered |
|---|---|---|
| Memory page faults | Peak exported value **51,694.5** | A possible memory-related symptom |
| Disk reads | Anomaly score **48,608.0** in the saved Prolog binding | A competing read-I/O explanation |

The memory observation had a baseline median of **0**, incident median of **0**, and a spike in the incident window. Its estimated onset interval was **09:08–09:09 UTC+8**. These are exported measurements and derived summaries, not a claim about physical units or the injected cause.

[Memory source record](sources/ev_59fd8948f4fc12b4da6c.json) · [Read-I/O source record](sources/ev_e705d1ef6494aac0c51c.json)

## 3. Test the observations · 00:25–01:20

The investigator used Prolog to retrieve both observations and check their timing. Its first query executed successfully but returned **no matches** under its strict time-window filter. Later queries retrieved the observations and confirmed a 60-second onset interval.

It also inspected the knowledge base and the component's host, `node-6`, then tested a rule relating a container anomaly to a normal host observation.

Two later model requests timed out. The run continued, and the investigator saved a provisional diagnosis before returning to the evidence.

## 4. Execute the final supporting query · 03:10

This is the **actual final supporting query**, with line breaks added for readability:

```prolog
pod_local_support('shippingservice-1', memory, [E, HostE]),
E = 'ev_59fd8948f4fc12b4da6c',
observation(
    E, 'shippingservice-1', memory, increase,
    Score, OnsetStart, OnsetEnd,
    'container_memory_failures.container.pgfault'
),
Width is OnsetEnd - OnsetStart,
Width =< 60000.
```

**Execution succeeded.** The selected result was binding 0, excerpted below:

```json
{
  "E": "ev_59fd8948f4fc12b4da6c",
  "HostE": "ev_06eabad8d7ff85b10639",
  "Score": "51694.5",
  "OnsetStart": "1647738480000",
  "OnsetEnd": "1647738540000",
  "Width": "60000"
}
```

The result says the encoded conditions hold for these observations. It does **not** establish that memory load caused the incident.

[Exact request](prolog/request.json) · [Full execution result](prolog/result.json) · [Facts used](prolog/facts.pl) · [Trusted rules](prolog/trusted.pl)

## 5. Submit the diagnosis · 03:25

The agent called `finish_investigation`, referencing the successful execution and its selected binding. The saved final decision was:

```text
Investigation:  complete
Component:      shippingservice-1
Failure reason: container memory load
Estimated onset: 2022-03-20 09:08:00 UTC+8
Support:        execution 957b1b7bb7e34e3ba232acf60649c5e5, binding 0
```

The workflow finished and wrote the final outputs. “Complete” means the investigator satisfied the execution-and-evidence checks; it does not mean the answer matched the ground truth.

[Saved decision and assumptions](decision.json) · [Original generated evidence report](evidence.md)

## 6. What the evaluation revealed

| | Agent's answer | Ground truth |
|---|---|---|
| Component | shippingservice-1 | shippingservice-1 |
| Failure reason | container memory load | container read I/O load |

**Partial credit: 50%. Strict accuracy for this case: 0%.**

The agent observed the competing read-I/O signal but selected memory load. It also treated a normal `system.mem.total` observation as evidence against host memory pressure. Stable total memory capacity does not justify that conclusion. No alternative was conclusively ruled out, and this run did not inspect logs or traces for the component.

**The contribution demonstrated here is an inspectable investigation: we can see the evidence, execute the logic, and locate the unsupported causal assumption.**

---

<details>
<summary><strong>Expand the actual event timeline</strong></summary>

Times are elapsed from workflow start, rounded to the nearest second. Tool rows mark invocation times, not completion times. This is an edited view of recorded events, not a transcript of hidden model reasoning.

| Time | Recorded event | Action |
|---|---|---|
| 00:23 | `get_evidence` | Retrieve memory observation |
| 00:23 | `get_evidence` | Retrieve read-I/O observation |
| 00:25 | `run_prolog` | Apply strict time-window filter; no matches |
| 00:31 | `run_prolog` | Retrieve memory observation |
| 00:32 | `run_prolog` | Retrieve read-I/O observation |
| 00:38 | `run_prolog` | Check memory candidate and onset width |
| 01:08 | `inspect_kb` | Inspect shippingservice-1 facts |
| 01:08 | `get_topology` | Inspect component topology |
| 01:15 | `inspect_kb` | Inspect node-6 facts |
| 01:20 | `run_prolog` | Test container/host support and timing |
| 01:45 | Model timeout | A model request exceeded its timeout |
| 02:07 | `record_diagnosis` | Save a provisional memory-load diagnosis |
| 02:32 | Model timeout | Another model request exceeded its timeout |
| 03:04 | `inspect_kb` | Revisit component observations |
| 03:04 | `inspect_kb` | Revisit normal host observations |
| 03:10 | `run_prolog` | Execute the final supporting query |
| 03:25 | `finish_investigation` | Submit diagnosis with execution reference |

[Machine-readable timeline with exact input payloads](timeline.json)

</details>

<details>
<summary><strong>Suggested 40-second narration</strong></summary>

“This is a saved run of our agent. It inspected memory and disk-read evidence on the same service, then queried the knowledge base and tested its hypothesis in Prolog. Here is the actual query and the returned binding. The agent linked that execution to its final diagnosis. It correctly identified the service, but confused the failure reason. That is useful feedback: we can trace the mistake to its assumptions, rather than receiving only an unexplained answer. Executable logic makes the reasoning inspectable; it does not automatically make the causal diagnosis correct.”

</details>

This is development case 0 from the five-minute comparison, using frozen implementation `a2e69d6ecabca792268f7580c2bf6bf7ae0e2a6e8b0cf627cdd587964f5efd51`. See [the full report](../../REPORT.md) for both cases and the single-model comparison. Raw provider streams are omitted from this readable walkthrough.
