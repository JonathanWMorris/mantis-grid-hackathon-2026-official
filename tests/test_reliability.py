import asyncio
import json
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest
import test_pipeline
from rca.symbolic import kb_snapshot, inspect_kb, run_prolog, PROLOG_DIR
from rca.tools import InvestigatorTools
from rca.manager import InvestigatorManager
from agent_framework.orchestrations import StandardMagenticManager
from agent_framework_orchestrations._magentic import MagenticProgressLedger


@pytest.fixture
def telemetry(tmp_path):
    return test_pipeline.telemetry.__wrapped__(tmp_path)


def test_kb_pages_and_version(telemetry):
    case, store, _, _ = telemetry
    store.metrics(case)
    snap = kb_snapshot(store, case)
    first = inspect_kb(store, case, limit=2)
    allrows = []
    offset = 0
    while True:
        page = inspect_kb(store, case, offset=offset, limit=2)
        allrows += page["facts"]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert allrows == snap["facts"].splitlines()
    assert first["kb_version"] == snap["kb_version"]
    assert all(
        "'shop-0'" in r for r in inspect_kb(store, case, component="shop-0")["facts"]
    )
    store.evidence.add(
        {
            "kind": "trace",
            "case": case.key,
            "parents": ["shop-0"],
            "entity": "shop-1",
            "ratio": 2,
        }
    )
    assert kb_snapshot(store, case)["kb_version"] != snap["kb_version"]


@pytest.mark.parametrize(
    "query,expected",
    [
        ("X is (10+2)*3//2, X =:= 18.", {"X": "18"}),
        ("aggregate_all(sum(S),observation(_,_,memory,_,S,_,_,_),Sum).", None),
        (
            "findall(P,hosted_on(P,_),Ps),sort(Ps,Unique),length(Unique,N).",
            {"Ps": "['shop-0']", "Unique": "['shop-0']", "N": "1"},
        ),
        (
            "sum_list([1,2,3],S),min_list([1,2,3],A),max_list([1,2,3],B).",
            {"S": "6", "A": "1", "B": "3"},
        ),
    ],
)
def test_grounded_arithmetic_and_aggregation(telemetry, query, expected):
    case, store, _, out = telemetry
    store.metrics(case)
    result = run_prolog(store, case, out, query, [])
    assert result["status"] == "ok", result
    assert result["bindings"]
    if expected is not None:
        assert result["bindings"][0] == expected


@pytest.mark.parametrize(
    "query",
    [
        "X is Y+1.",
        "aggregate_all(count,shell(ls),N).",
        "findall(X,call(X),L).",
        "observation(E,P,F,D,S,A,B).",
        "candidate(shop-0,memory,E).",
        "findall(X,assertz(calls(a,b)),L).",
        r"\+ candidate(P,cpu,E).",
        "length(L,20000).",
    ],
)
def test_diagnostic_failures(telemetry, query):
    case, store, _, out = telemetry
    store.metrics(case)
    result = run_prolog(store, case, out, query, [])
    assert result["status"] == "error", result
    assert result["diagnostic"]["code"]
    assert result["repair_context"]["query"]


def test_all_prompt_examples_execute(tmp_path):
    examples = json.loads(
        (PROLOG_DIR.parent / "prompts/prolog_examples.json").read_text()
    )
    for name in ["base.pl", "trusted.pl"]:
        shutil.copyfile(PROLOG_DIR / name, tmp_path / name)
    (tmp_path / "facts.pl").write_text(examples["facts"])
    for ex in examples["examples"]:
        (tmp_path / "request.json").write_text(
            json.dumps({"query": ex["query"], "rules": ex["rules"]})
        )
        p = subprocess.run(
            [
                "swipl",
                "-q",
                "-s",
                str(tmp_path / "base.pl"),
                "--",
                str(tmp_path / "facts.pl"),
                str(tmp_path / "request.json"),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        result = json.loads(p.stdout)
        assert result["status"] == "ok", result
        assert result["bindings"] == ex["expected"], (ex, result)


def supported_answer(tools, case):
    result = json.loads(tools.run_prolog("candidate(P,memory,E), hosted_on(P,N).", []))
    e = next(
        k
        for k, v in tools.store.evidence.items.items()
        if v.get("entity") == "shop-0" and v.get("score", 0) > 4
    )
    return {
        "component": "shop-0",
        "reason": "container memory load",
        "onset": case.start + 600000,
        "confidence": "low",
        "evidence_ids": [e],
        "prolog_support": [
            {
                "execution_id": result["execution_id"],
                "binding_indices": [0],
                "explanation": "Measured memory anomaly is on this hosted pod; causal mechanism remains a hypothesis.",
                "assumptions": ["Anomaly is causally relevant"],
            }
        ],
    }


def test_completion_support_and_stale_snapshots(telemetry):
    case, store, _, out = telemetry
    store.metrics(case)
    tools = InvestigatorTools(store, case, out, time.monotonic() + 30)
    answer = supported_answer(tools, case)
    bad = {**answer, "reason": "memory"}
    assert json.loads(tools.finish_investigation([bad], []))["status"] == "error"
    fake = {
        **answer,
        "prolog_support": [{**answer["prolog_support"][0], "execution_id": "fake"}],
    }
    assert json.loads(tools.finish_investigation([fake], []))["status"] == "error"
    store.evidence.add(
        {
            "kind": "trace",
            "case": case.key,
            "parents": ["shop-0"],
            "entity": "shop-1",
            "ratio": 2,
        }
    )
    assert "stale" in json.loads(tools.finish_investigation([answer], []))["message"]
    answer = supported_answer(tools, case)
    assert json.loads(tools.finish_investigation([answer], []))["status"] == "ok"
    assert tools.completed_decision["investigation_status"] == "complete"


def test_trace_full_output_budget_errors_redaction(telemetry, monkeypatch):
    case, store, _, out = telemetry
    store.metrics(case)
    monkeypatch.setenv("FEATHERLESS_API_KEY", "secret-test-credential")
    tools = InvestigatorTools(store, case, out, time.monotonic() + 30)
    tools.invoke(
        "sample", {}, lambda: {"text": "x" * 20000, "secret": "secret-test-credential"}
    )
    tools.deadline = 0
    assert json.loads(tools.inspect_kb())["status"] == "unavailable"
    events = [json.loads(l) for l in (out / "trace.jsonl").read_text().splitlines()]
    results = [
        json.loads((out / e["payload"]).read_text())
        for e in events
        if e["kind"] == "tool_result"
    ]
    assert len(results[0]["text"]) == 20000
    assert results[0]["secret"] == "[REDACTED]"
    assert results[1]["status"] == "unavailable"
    assert len([e for e in events if e["kind"] == "tool_start"]) == 2


def test_manager_cannot_skip_or_replace_investigator(telemetry, monkeypatch):
    case, store, _, out = telemetry
    store.metrics(case)
    tools = InvestigatorTools(store, case, out, time.monotonic() + 30)

    async def premature(self, context):
        return MagenticProgressLedger.from_dict(
            {
                k: {"answer": v, "reason": "test"}
                for k, v in {
                    "is_request_satisfied": True,
                    "is_in_loop": False,
                    "is_progress_being_made": True,
                    "next_speaker": "invented",
                    "instruction_or_question": "Conclude now",
                }.items()
            }
        )

    monkeypatch.setattr(StandardMagenticManager, "create_progress_ledger", premature)
    manager = InvestigatorManager(tools=tools, agent=SimpleNamespace())

    async def scenario():
        ledger = await manager.create_progress_ledger(None)
        assert ledger.is_request_satisfied.answer is False
        assert ledger.next_speaker.answer == "RCAInvestigator"
        tools.finish_investigation([supported_answer(tools, case)], [])
        ledger = await manager.create_progress_ledger(None)
        assert ledger.is_request_satisfied.answer is True
        final = await manager.prepare_final_answer(None)
        assert json.loads(final.text) == tools.completed_decision

    asyncio.run(scenario())


def test_real_framework_completes_through_tool_gate(telemetry, monkeypatch):
    from test_model import WireStream, completion
    from openai.types.chat import ChatCompletionMessageFunctionToolCall
    from agents.magentic import investigate

    case, store, _, out = telemetry
    candidates = store.candidates(case)
    e = next(
        k
        for k, v in store.evidence.items.items()
        if v.get("entity") == "shop-0" and v.get("score", 0) > 4
    )
    calls = []

    async def create(**kwargs):
        r = completion()
        names = [t["function"]["name"] for t in kwargs.get("tools", [])]
        if "run_prolog" in names and kwargs.get("tool_choice") != "none":
            step = len(calls)
            calls.append(kwargs)
            if step == 0:
                name = "run_prolog"
                args = {"query": "candidate(P,memory,E).", "rules": []}
            elif step == 1:
                msg = next(
                    m for m in reversed(kwargs["messages"]) if m["role"] == "tool"
                )
                result = json.loads(msg["content"])
                name = "finish_investigation"
                args = {
                    "answers": [
                        {
                            "component": "shop-0",
                            "reason": "container memory load",
                            "onset": case.start + 600000,
                            "confidence": "low",
                            "evidence_ids": [e],
                            "prolog_support": [
                                {
                                    "execution_id": result["execution_id"],
                                    "binding_indices": [0],
                                    "explanation": "Memory change is measured on this pod; cause remains conditional.",
                                    "assumptions": [
                                        "Memory anomaly is diagnostically relevant"
                                    ],
                                }
                            ],
                        }
                    ],
                    "limitations": ["Synthetic test"],
                }
            else:
                r.choices[0].message.content = "Investigator complete."
                return WireStream(r)
            r.choices[0].message.content = None
            r.choices[0].message.tool_calls = [
                ChatCompletionMessageFunctionToolCall(
                    id=f"call_{step}",
                    type="function",
                    function={"name": name, "arguments": json.dumps(args)},
                )
            ]
            r.choices[0].finish_reason = "tool_calls"
        else:
            r.choices[0].message.content = json.dumps(
                {
                    k: {"answer": v, "reason": "test"}
                    for k, v in {
                        "is_request_satisfied": True,
                        "is_in_loop": False,
                        "is_progress_being_made": True,
                        "next_speaker": "RCAInvestigator",
                        "instruction_or_question": "Conclude immediately",
                    }.items()
                }
            )
        return WireStream(r)

    class Fake:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
            self.base_url = "https://example.invalid"

        async def close(self):
            pass

    monkeypatch.setattr("rca.model.AsyncOpenAI", Fake)
    monkeypatch.setenv("FEATHERLESS_API_KEY", "not-real")
    monkeypatch.delenv("RCA_MODEL", raising=False)
    monkeypatch.delenv("RCA_BUDGET_FILE", raising=False)
    raw, usage, notes, tools = asyncio.run(
        investigate(store, case, out, {}, time.monotonic() + 30, candidates)
    )
    assert raw["investigation_status"] == "complete", (raw, notes)
    assert raw == tools.completed_decision
    assert raw["answers"][0]["component"] == "shop-0"
    events = [json.loads(l) for l in (out / "trace.jsonl").read_text().splitlines()]
    executed = [e for e in events if e["kind"] == "tool_start"]
    assert [e["tool_call_id"] for e in executed] == ["call_0", "call_1"]
    assert all(e.get("request_id") for e in executed)


def test_interrupted_provider_preserves_partial_trace(tmp_path, monkeypatch):
    from rca.model import ModelGateway
    from test_model import WireStream, completion

    class InterruptedStream(WireStream):
        async def chunks(self):
            async for chunk in super().chunks():
                yield chunk
            raise TimeoutError("stream interrupted after partial response")

    async def create(**kwargs):
        return InterruptedStream(completion())

    class Fake:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
            self.base_url = "https://example.invalid"

        async def close(self):
            pass

    monkeypatch.setattr("rca.model.AsyncOpenAI", Fake)
    monkeypatch.setenv("FEATHERLESS_API_KEY", "not-real")
    monkeypatch.delenv("RCA_MODEL", raising=False)
    monkeypatch.delenv("RCA_BUDGET_FILE", raising=False)

    async def scenario():
        gateway = ModelGateway({}, tmp_path, time.monotonic() + 30)
        with pytest.raises(Exception):
            await gateway.client("manager").get_response("test")
        await gateway.close()

    asyncio.run(scenario())
    events = [
        json.loads(l) for l in (tmp_path / "trace.jsonl").read_text().splitlines()
    ]
    requests = {e["event_id"] for e in events if e["kind"] == "model_request"}
    chunks = [e for e in events if e["kind"] == "provider_chunk"]
    errors = [e for e in events if e["kind"] == "model_error"]
    assert chunks and errors
    assert all(e["request_id"] in requests for e in chunks + errors)
    assert all(e["category"] == "deadline" for e in errors)


def test_empty_support_and_post_completion_changes_rejected(telemetry):
    case, store, _, out = telemetry
    store.metrics(case)
    tools = InvestigatorTools(store, case, out, time.monotonic() + 30)
    answer = supported_answer(tools, case)
    empty = json.loads(tools.run_prolog("candidate(P,cpu,E).", []))
    assert empty["status"] == "ok" and empty["bindings"] == []
    rejected = {
        **answer,
        "prolog_support": [
            {**answer["prolog_support"][0], "execution_id": empty["execution_id"]}
        ],
    }
    assert json.loads(tools.finish_investigation([rejected], []))["status"] == "error"
    assert tools.completed_decision is None
    assert json.loads(tools.finish_investigation([answer], []))["status"] == "ok"
    before = tools.completed_decision.copy()
    assert json.loads(tools.inspect_traces("shop-0"))["status"] == "unavailable"
    assert tools.completed_decision == before
