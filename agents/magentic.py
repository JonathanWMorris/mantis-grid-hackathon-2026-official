"""Submission agent: actual Magentic orchestration with one tool-equipped investigator."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from run import Solution, format_prediction
from rca.schema import REASONS, clock, parse_case
from rca.store import Store
from rca.tools import InvestigatorTools
from rca.trace import Trace, clean


def parse_json(text):
    decoder = json.JSONDecoder()
    for i, c in enumerate(text):
        if c == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                if isinstance(obj, dict) and isinstance(obj.get("answers"), list):
                    return obj
            except json.JSONDecodeError:
                pass
    raise ValueError("No diagnosis JSON found")


async def investigate(store, case, directory, state, deadline, candidates):
    from agent_framework import Agent
    from agent_framework.orchestrations import MagenticBuilder
    from rca.model import ModelGateway

    from rca.manager import InvestigatorManager

    trace = Trace(directory)
    tools = InvestigatorTools(store, case, directory, deadline, trace=trace)
    gateway = ModelGateway(
        state, directory, deadline, lambda: tools.prolog_failures > 0, trace=trace
    )
    result = {}
    notes = []
    events = []
    try:
        prompt = (
            Path(__file__).resolve().parents[1] / "prompts/investigator.md"
        ).read_text()
        trusted = (
            Path(__file__).resolve().parents[1] / "prolog/trusted.pl"
        ).read_text()
        prompt += "\nTRUSTED RULE DEFINITIONS\n" + trusted
        prompt += (
            "\nWORKED EXAMPLES\n"
            + (
                Path(__file__).resolve().parents[1] / "prompts/prolog_examples.json"
            ).read_text()
        )
        investigator = Agent(
            client=gateway.client("investigator"),
            name="RCAInvestigator",
            instructions=prompt,
            description="Queries telemetry and tests diagnoses using Prolog.",
            tools=tools.all(),
        )
        manager = Agent(
            client=gateway.client("manager"),
            name="RCAManager",
            instructions="Manage a bounded evidence-grounded investigation. Separate observations from guesses. "
            "Use concise plans. Only RCAInvestigator can gather evidence. Require at least one investigator "
            "turn before completion. Every progress-ledger reason must be at most twelve words. "
            "Ask the investigator to record an evidence-linked provisional diagnosis early. "
            "Never invent measurements. Only an accepted finish_investigation tool result establishes completion. "
            "The investigator decides when its diagnosis has executed Prolog support. Delegate until then; do not diagnose from initial candidates.",
        )
        gated_manager = InvestigatorManager(
            tools=tools,
            agent=manager,
            max_round_count=4,
            max_stall_count=1,
            max_reset_count=1,
            task_ledger_facts_prompt="Extract only given facts and unknowns in under 150 words: {task}",
            task_ledger_plan_prompt="Team: {team}. Plan evidence inspection, Prolog test and investigator completion in at most three steps.",
        )
        workflow = MagenticBuilder(
            participants=[investigator],
            manager=gated_manager,
            max_round_count=4,
            intermediate_output_from=[investigator],
        ).build()
        task = (
            case.instruction
            + "\nAllowed reasons: "
            + json.dumps(REASONS)
            + "\nWindow epoch ms: "
            + json.dumps([case.start, case.end])
            + "\nInitial numerical candidates (not diagnoses): "
            + json.dumps(candidates[:8])
            + "\nTest at least one hypothesis with Prolog. Return exactly "
            + str(case.count)
            + " failure events."
        )
        # Non-streaming at the provider; workflow events still expose completed agent turns.
        async with asyncio.timeout(max(0.1, deadline - time.monotonic())):
            async for event in workflow.run(task, stream=True):
                data = getattr(event, "data", None)
                text = getattr(data, "text", None)
                events.append(
                    {
                        "type": str(getattr(event, "type", type(event).__name__)),
                        "executor": getattr(event, "executor_id", None),
                        "text": clean(str(text or data)),
                    }
                )
                trace.emit(
                    "workflow_event",
                    data,
                    event_type=str(getattr(event, "type", type(event).__name__)),
                    executor=getattr(event, "executor_id", None),
                )
                (directory / "orchestration.json").write_text(
                    json.dumps(events, indent=2)
                )
    except Exception as e:
        trace.emit(
            "investigation_error",
            {"type": type(e).__name__, "message": str(e), "cause": repr(e.__cause__)},
        )
        notes.append(
            f"Investigation stopped: {type(e).__name__}. Used the best validated results available."
        )
        import traceback

        # Local developer diagnostic, no request headers or credentials.
        (directory / "exception.txt").write_text(
            clean("".join(traceback.format_exception(e)))
        )
    finally:
        await gateway.close()
        (directory / "orchestration.json").write_text(json.dumps(events, indent=2))
        (directory / "model_calls.json").write_text(json.dumps(gateway.calls, indent=2))
    result = tools.completed_decision or {}
    if not result and tools.latest_decision:
        result = tools.latest_decision
        notes.append(
            "Returned the latest evidence-linked provisional diagnosis saved before investigation ended."
        )
    result.setdefault("investigation_status", "incomplete")
    if not tools.completed_decision:
        notes.append(
            "Investigation incomplete: no accepted investigator diagnosis with current-KB Prolog support."
        )
    trace.emit("investigation_result", result)
    return result, gateway.usage, notes, tools


def validate_answers(raw, case, store, fallback):
    entities, hosts = store.entities(case)
    answers = []
    notes = []
    for i in range(case.count):
        proposals = raw.get("answers", []) if isinstance(raw, dict) else []
        if not isinstance(proposals, list):
            proposals = []
        p = (
            proposals[i]
            if i < len(proposals) and isinstance(proposals[i], dict)
            else {}
        )
        valid = p.get("component") in entities and p.get("reason") in REASONS
        valid = valid and (
            p["reason"].startswith("node") == p["component"].startswith("node-")
        )
        proposed_ids = p.get("evidence_ids", [])
        if not isinstance(proposed_ids, list):
            proposed_ids = []
        ids = [
            k
            for k in proposed_ids
            if isinstance(k, str)
            and k in store.evidence.items
            and store.evidence.items[k].get("case") == case.key
        ]
        valid = valid and any(
            store.evidence.items[k].get("entity") == p.get("component") for k in ids
        )
        if not valid or not ids:
            p = dict(fallback[min(i, len(fallback) - 1)])
            p["confidence"] = "low"
            notes.append(
                f"Failure {i + 1}: numerical fallback used because a valid evidence-linked model answer was unavailable."
            )
        else:
            p = dict(p)
            p["evidence_ids"] = ids
        try:
            onset = int(p.get("onset", case.start))
        except (ValueError, TypeError, OverflowError):
            onset = case.start
        if not case.start <= onset < case.end:
            onset = case.start
            notes.append(
                "Invalid model onset replaced by window start; timing is uncertain."
            )
        p["onset"] = onset
        p["datetime"] = clock(onset)
        p["confidence"] = (
            p.get("confidence")
            if p.get("confidence") in ("low", "medium", "high")
            else "low"
        )
        answers.append(p)
    return sorted(answers, key=lambda x: x["onset"]), notes


def evidence_report(answers, raw, store, notes, tools):
    lines = [
        "## Answer",
        "Investigation status: " + raw.get("investigation_status", "incomplete"),
    ]
    lines.extend(
        f"- {a['component']} / {a['reason']} / {a['datetime']} UTC+8" for a in answers
    )
    lines += ["", "## Confidence"]
    lines.extend(
        f"- {a['component']}: {a['confidence']} (model self-assessment, not a calibrated probability)."
        for a in answers
    )
    lines += ["", "## Evidence"]
    for key in dict.fromkeys(k for a in answers for k in a["evidence_ids"]):
        e = store.evidence.get(key)
        if e.get("kind") == "metric":
            summary = (
                f"{e['source']}: {e['entity']} — {e['signal']}; baseline median {e['baseline_median']}, "
                f"incident median {e['incident_median']}, peak {e['peak_value']}; "
                f"{e['baseline_samples']} baseline / {e['samples']} incident samples. "
                f"Estimated onset {clock(e['onset'][0])}–{clock(e['onset'][1])} UTC+8. "
                f"Transformation: {e['semantics']}."
            )
        elif e.get("kind") == "trace":
            summary = f"trace_span: {e['entity']} {e['operation']}; p95 duration {e['baseline_p95_duration_raw']} → {e['p95_duration_raw']} (raw export units). {e['limitation']}"
        else:
            summary = f"{e.get('source')}: {e.get('entity')}; template {e.get('template')!r}; observed counts {e.get('baseline_count')} → {e.get('count')}. {e.get('limitation', '')}"
        lines.append(f"- [{key}](../artifacts/evidence/{key}.json): {summary}")
    if tools:
        lines += ["", "### Executed logic support"]
        for answer in answers:
            for support in answer.get("prolog_support", []):
                execution = support["execution_id"]
                link = f"../artifacts/cases/{tools.directory.name}/prolog/{execution}/result.json"
                lines.append(
                    f"- [{execution}]({link}), bindings {support['binding_indices']}: {support['explanation']}"
                )
                lines.append("  Assumptions: " + "; ".join(support["assumptions"]))
    lines += [
        "",
        "## Ruled out",
        "No alternative is conclusively ruled out by the automatic report.",
    ]
    alternatives = raw.get("alternatives", [])
    for alt in (alternatives if isinstance(alternatives, list) else [])[:5]:
        if isinstance(alt, dict):
            references = alt.get("evidence_ids", [])
            ids = [
                k
                for k in (references if isinstance(references, list) else [])
                if isinstance(k, str) and k in store.evidence.items
            ]
            if ids:
                lines.append(
                    f"- Alternative {alt.get('component')}: inspect {', '.join(ids)}; model assessment is not independent proof."
                )
    lines += ["", "### Limitations"]
    lines.extend("- " + n for n in notes)
    limitations = raw.get("limitations", [])
    for limitation in (limitations if isinstance(limitations, list) else [])[:5]:
        if isinstance(limitation, str):
            lines.append("- Model-reported limitation: " + limitation[:500])
    if tools:
        lines.append(
            f"- Tool calls: {tools.count}; Prolog failures: {tools.prolog_failures}. Executed queries and proposed rules are saved with the case artifacts."
        )
    lines.append(
        "- Prolog checks encoded assumptions. Correlated changes and timing do not by themselves prove causation. Missing telemetry is not evidence of health."
    )
    return "\n".join(lines) + "\n"


def solve(instruction, dataset_dir, ctx):
    case = parse_case(instruction)
    state = ctx.setdefault("rca_state", {"started": time.monotonic(), "spend": 0.0})
    store = ctx.get("rca_store")
    if store is None:
        store = ctx["rca_store"] = Store(Path(dataset_dir), Path(ctx["out_dir"]))
    directory = (
        Path(ctx["out_dir"]) / "artifacts" / "cases" / str(ctx.get("row_id", case.key))
    )
    directory.mkdir(parents=True, exist_ok=True)
    run_left = max(0, 1080 - (time.monotonic() - state["started"]))
    remaining = max(1, ctx.get("remaining_cases", 20))
    deadline = time.monotonic() + min(
        float(os.getenv("RCA_CASE_SECONDS", "90")), run_left / remaining
    )
    store.deadline = deadline
    candidates = store.candidates(case)
    entities, _ = store.entities(case)
    if not candidates:
        if not entities:
            raise ValueError("Dataset has no observable candidate components")
        candidates = [
            {
                "component": entities[0],
                "reason": "node CPU load"
                if entities[0].startswith("node-")
                else "container CPU load",
                "onset": case.start,
                "evidence_ids": [],
                "score": 0,
            }
        ]
    # Select distinct events for fallback without forcing distinct components.
    fallback = []
    for c in candidates:
        if all(
            c["component"] != p["component"] or abs(c["onset"] - p["onset"]) > 120000
            for p in fallback
        ):
            fallback.append(c)
        if len(fallback) >= case.count:
            break
    raw = {}
    usage = {}
    notes = []
    tools = None
    if os.getenv("RCA_OFFLINE") == "1" or not os.getenv("FEATHERLESS_API_KEY"):
        notes.append(
            "Offline mode or missing API key: deterministic numerical fallback; no Magentic model calls."
        )
    elif time.monotonic() < deadline - 2:
        raw, usage, notes, tools = asyncio.run(
            investigate(store, case, directory, state, deadline, candidates)
        )
    else:
        notes.append("Preparation exhausted this case budget; numerical fallback used.")
    # Cached facts remain accessible for final formatting even after time budget expires.
    store.deadline = float("inf")
    answers, validation = validate_answers(raw, case, store, fallback)
    notes += validation
    notes += store.limitations
    (directory / "decision.json").write_text(
        json.dumps(
            {
                "answers": answers,
                "model_proposal": raw,
                "notes": notes,
                "investigation_status": raw.get("investigation_status", "incomplete"),
            },
            indent=2,
        )
    )
    required = {
        "task_1": ("datetime",),
        "task_2": ("reason",),
        "task_3": ("component",),
        "task_4": ("datetime", "reason"),
        "task_5": ("datetime", "component"),
        "task_6": ("component", "reason"),
        "task_7": ("datetime", "component", "reason"),
    }.get(ctx.get("task_index"), ("datetime", "component", "reason"))
    return Solution(
        format_prediction([{k: a[k] for k in required} for a in answers]),
        evidence_report(answers, raw, store, notes, tools),
        usage,
    )
