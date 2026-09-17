import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rca.model import ModelGateway
from openai.types.chat import ChatCompletion


class WireStream:
    def __init__(self, response):
        self.response = response

    def __aiter__(self):
        return self.chunks()

    async def chunks(self):
        from openai.types.chat import ChatCompletionChunk

        r = self.response
        m = r.choices[0].message
        delta = {"role": "assistant", "content": m.content}
        if m.tool_calls:
            delta["tool_calls"] = [
                {"index": i, **t.model_dump()} for i, t in enumerate(m.tool_calls)
            ]
        yield ChatCompletionChunk(
            id=r.id,
            created=r.created,
            model=r.model,
            object="chat.completion.chunk",
            choices=[
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": r.choices[0].finish_reason,
                }
            ],
            usage=r.usage,
        )

    async def close(self):
        pass


def completion():
    return ChatCompletion(
        id="test",
        created=0,
        model="test",
        object="chat.completion",
        choices=[
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"ok":true}'},
            }
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )


def test_transport_preserves_response_and_routes(monkeypatch, tmp_path):
    seen = []

    async def create(**kwargs):
        seen.append(kwargs)
        return WireStream(completion())

    class Fake:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
            self.base_url = "https://example.invalid/v1"

        async def close(self):
            pass

    monkeypatch.setattr("rca.model.AsyncOpenAI", Fake)
    monkeypatch.setenv("FEATHERLESS_API_KEY", "not-a-real-key")
    monkeypatch.delenv("RCA_MODEL", raising=False)
    monkeypatch.delenv("RCA_BUDGET_FILE", raising=False)

    async def run():
        g = ModelGateway({}, tmp_path, time.monotonic() + 30)
        c = g.client("manager")
        response = await c.get_response("Return JSON")
        assert response.text == '{"ok":true}'
        assert seen[0]["model"] == "zai-org/GLM-4.7-Flash"
        assert g.usage[seen[0]["model"]]["calls"] == 1
        await g.close()

    asyncio.run(run())


def test_spend_limit_prevents_request(monkeypatch, tmp_path):
    monkeypatch.delenv("RCA_BUDGET_FILE", raising=False)
    gateway = ModelGateway({"spend": 20}, tmp_path, time.monotonic() + 30)
    assert gateway.remaining_budget() == 0


def test_non_glm_pin_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("RCA_MODEL", "some-other-model")
    with pytest.raises(ValueError):
        ModelGateway({}, tmp_path, time.monotonic() + 30)


def test_glm_tagged_tools_are_normalized():
    from rca.model import recover_tool_calls

    response = completion()
    msg = response.choices[0].message
    msg.content = "<tool_call>run_prolog<arg_key>query</arg_key><arg_value>candidate(P,memory,E).</arg_value><arg_key>rules</arg_key><arg_value>[]</arg_value></tool_call>"
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "run_prolog",
                "parameters": {
                    "properties": {
                        "query": {"type": "string"},
                        "rules": {"type": "array"},
                    },
                    "required": ["query", "rules"],
                },
            },
        }
    ]
    assert recover_tool_calls(msg, schemas)
    assert msg.tool_calls[0].function.name == "run_prolog"
    assert msg.content is None


def test_agent_stream_executes_native_tool(monkeypatch, tmp_path):
    from agent_framework import Agent

    seen = []
    steps = []

    async def create(**kwargs):
        steps.append(kwargs)
        if len(steps) == 1:
            r = completion()
            r.choices[0].message.content = None
            from openai.types.chat import ChatCompletionMessageFunctionToolCall

            r.choices[0].message.tool_calls = [
                ChatCompletionMessageFunctionToolCall(
                    id="call_one",
                    type="function",
                    function={"name": "lookup", "arguments": '{"value":"x"}'},
                )
            ]
            r.choices[0].finish_reason = "tool_calls"
            return WireStream(r)
        return WireStream(completion())

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

    def lookup(value: str) -> str:
        """Lookup evidence."""
        seen.append(value)
        return "found"

    async def run():
        g = ModelGateway({}, tmp_path, time.monotonic() + 30)
        a = Agent(client=g.client("investigator"), name="Test", tools=[lookup])
        async for _ in a.run("look up x", stream=True):
            pass
        await g.close()

    asyncio.run(run())
    assert seen == ["x"]
    import json

    events = [
        json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()
    ]
    assert any(e["kind"] == "provider_response" for e in events)
    requests = [e for e in events if e["kind"] == "model_request"]
    results = [e for e in events if e["kind"] == "framework_tool_result"]
    assert results[0]["tool_call_id"] == "call_one"
    assert results[0]["request_id"] == requests[0]["event_id"]
    assert "found" in (tmp_path / results[0]["payload"]).read_text()
