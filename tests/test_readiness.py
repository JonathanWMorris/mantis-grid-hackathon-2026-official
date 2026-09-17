import asyncio
import csv
import json
import sys
import time
from types import SimpleNamespace

import pytest
from test_model import WireStream, completion
from rca.model import ModelGateway, GenerationExhausted, recover_tool_calls
from evaluate_pipeline import repeat_statistics, source_manifest, main


@pytest.fixture
def fake_wire(monkeypatch):
    def install(respond):
        class Fake:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=respond))
                self.base_url = "https://example.invalid"

            async def close(self):
                pass

        monkeypatch.setattr("rca.model.AsyncOpenAI", Fake)
        monkeypatch.setenv("FEATHERLESS_API_KEY", "test-only")
        monkeypatch.delenv("RCA_MODEL", raising=False)
        monkeypatch.delenv("RCA_BUDGET_FILE", raising=False)

    return install


def test_unknown_tagged_tool_preserved_for_framework():
    r = completion()
    m = r.choices[0].message
    m.content = '<tool_call>inspect_evidence<arg_key>evidence_ids</arg_key><arg_value>["metric_container"]</arg_value></tool_call>'
    assert recover_tool_calls(m, [])
    assert m.tool_calls[0].function.name == "inspect_evidence"
    assert json.loads(m.tool_calls[0].function.arguments) == {
        "evidence_ids": ["metric_container"]
    }


def test_framework_repairs_unknown_tool_and_invalid_arguments(tmp_path, fake_wire):
    from agent_framework import Agent
    from openai.types.chat import ChatCompletionMessageFunctionToolCall

    requests = []
    executed = []

    async def respond(**kwargs):
        requests.append(kwargs)
        r = completion()
        i = len(requests)
        if i <= 3:
            names = ["inspect_evidence", "query", "query"]
            args = [{}, {"rules": "[]"}, {"rules": []}]
            r.choices[0].message.content = None
            r.choices[0].message.tool_calls = [
                ChatCompletionMessageFunctionToolCall(
                    id=f"call_{i}",
                    type="function",
                    function={
                        "name": names[i - 1],
                        "arguments": json.dumps(args[i - 1]),
                    },
                )
            ]
            r.choices[0].finish_reason = "tool_calls"
        return WireStream(r)

    fake_wire(respond)

    def query(rules: list[str]) -> str:
        """Test query accepting an array."""
        executed.append(rules)
        return "ok"

    async def scenario():
        state = {}
        g = ModelGateway(state, tmp_path, time.monotonic() + 30)
        agent = Agent(client=g.client("investigator"), name="Test", tools=[query])
        await agent.run("Query")
        assert executed == [[]]
        assert g.generation_failures == 2
        assert state["down"] == {}
        assert any("Allowed tool names" in str(r["messages"]) for r in requests)
        await g.close()
        later = ModelGateway(state, tmp_path / "later", time.monotonic() + 30)
        await later.client("manager").get_response("Summary")
        assert requests[-1]["model"] == "zai-org/GLM-4.7-Flash"
        await later.close()

    asyncio.run(scenario())


def test_disabled_tools_never_execute_or_disable_model(tmp_path, fake_wire):
    requests = []

    async def respond(**kwargs):
        requests.append(kwargs)
        r = completion()
        r.choices[
            0
        ].message.content = "<tool_call>inspect_evidence<arg_key>x</arg_key><arg_value>1</arg_value></tool_call>"
        return WireStream(r)

    fake_wire(respond)

    async def scenario():
        state = {}
        g = ModelGateway(state, tmp_path, time.monotonic() + 30)
        client = g.client("investigator")
        for _ in range(2):
            r = await client.get_response("Summarize", options={"tool_choice": "none"})
            assert "incomplete" in r.text.lower()
        with pytest.raises(Exception, match="Malformed tool output"):
            await client.get_response("Summarize", options={"tool_choice": "none"})
        assert state["down"] == {}
        assert len(requests) == 3
        assert all(
            "tools" not in r and "parallel_tool_calls" not in r for r in requests
        )
        await g.close()

    asyncio.run(scenario())


def test_repair_budget_counts_errors_once(tmp_path):
    g = ModelGateway({}, tmp_path, time.monotonic() + 30)
    message = {
        "role": "tool",
        "tool_call_id": "a",
        "content": "Error: rules must be a list",
    }
    g.prepare_request({"messages": [message]})
    g.prepare_request({"messages": [message]})
    assert g.generation_failures == 1
    g.prepare_request({"messages": [{**message, "tool_call_id": "b"}]})
    with pytest.raises(GenerationExhausted):
        g.prepare_request({"messages": [{**message, "tool_call_id": "c"}]})
    assert g.state["down"] == {}


def test_prepare_only_creates_manifest_without_execution(tmp_path, monkeypatch):
    dataset = tmp_path / "data"
    (dataset / "dev").mkdir(parents=True)
    fields = ["row_id", "task_index", "instruction", "scoring_points"]
    with (dataset / "dev/query_dev.csv").open("w") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i in range(2):
            w.writerow(
                dict(
                    row_id=i,
                    task_index="task_6",
                    instruction="The system experienced one failure within March 20, 2022, from 09:00 to 09:30. Identify the component and reason.",
                    scoring_points="ANSWER SECRET",
                )
            )
    monkeypatch.setattr(
        "evaluate_pipeline.subprocess.run",
        lambda *a, **k: pytest.fail("Preparation executed a process"),
    )
    out = tmp_path / "prepared"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_pipeline.py",
            "--dataset",
            str(dataset),
            "--out",
            str(out),
            "--configs",
            "routed",
            "fixed",
            "--prepare-only",
        ],
    )
    main()
    m = json.loads((out / "manifest.json").read_text())
    assert m["row_ids"] == [0, 1] and m["case_seconds"] == 90
    assert "ANSWER SECRET" not in (out / "queries.csv").read_text()
    assert not (out / "results.json").exists()
    assert any(k.startswith("prompts/") for k in m["source"]["files"])


def test_repeat_statistics_exclude_interrupted():
    rows = [
        dict(
            configuration="routed",
            status=status,
            strict=v,
            partial=v,
            dollars_per_case=v,
            seconds_per_case=v,
        )
        for status, v in [("complete", 0), ("complete", 1), ("interrupted", 100)]
    ]
    s = repeat_statistics(rows)["routed"]
    assert s["complete_repeats"] == 2
    assert s["metrics"]["strict"]["mean"] == 0.5
    assert s["metrics"]["strict"]["sample_sd"] == pytest.approx(2**-0.5)


def test_manifest_detects_prompt_changes(tmp_path):
    (tmp_path / "prompts").mkdir()
    p = tmp_path / "prompts/test.md"
    p.write_text("first")
    before = source_manifest(tmp_path)
    p.write_text("second")
    assert before["sha256"] != source_manifest(tmp_path)["sha256"]
