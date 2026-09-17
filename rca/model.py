"""Featherless transport adapter shared by manager and investigator."""

from __future__ import annotations

import asyncio
import json
import os
import time
import re
from pathlib import Path

from openai import AsyncOpenAI
from agent_framework.openai import OpenAIChatCompletionClient
from cost import PRICES, dollars
from .schema import ident
from .trace import Trace, clean

CHEAP = ["zai-org/GLM-4.7-Flash", "zai-org/GLM-5.3-Flash"]
STRONG = ["zai-org/GLM-5.2", "zai-org/GLM-5.1"]


class BudgetExceeded(RuntimeError):
    pass


class GenerationExhausted(BudgetExceeded):
    """Malformed output exhausted its own repair budget, not provider availability."""


class ProviderResponseError(RuntimeError):
    pass


async def collect_completion(create, kwargs, trace=None, request_id=None):
    """Accumulate the wire stream; Featherless's non-stream aggregator can omit
    initial function names/IDs, whereas the original deltas preserve them."""
    from openai.types.chat import ChatCompletion

    options = {**kwargs, "stream": True, "stream_options": {"include_usage": True}}
    stream = await create(**options)
    text = []
    reasoning = []
    calls = {}
    finish = "stop"
    usage = None
    cid = "response"
    created = 0
    try:
        async for chunk in stream:
            if trace:
                trace.emit("provider_chunk", chunk, request_id=request_id)
            cid = chunk.id or cid
            created = chunk.created or created
            if chunk.usage:
                usage = chunk.usage
            for choice in chunk.choices:
                if choice.finish_reason:
                    finish = choice.finish_reason
                d = choice.delta
                if d.content:
                    text.append(d.content)
                extra = getattr(d, "reasoning", None) or getattr(
                    d, "reasoning_content", None
                )
                if isinstance(extra, str):
                    reasoning.append(extra)
                for t in d.tool_calls or []:
                    item = calls.setdefault(
                        t.index,
                        {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    if t.id:
                        item["id"] += t.id
                    if t.function:
                        if t.function.name:
                            item["function"]["name"] += t.function.name
                        if t.function.arguments:
                            item["function"]["arguments"] += t.function.arguments
    finally:
        await stream.close()
    for index, t in calls.items():
        if not t["id"]:
            t["id"] = "call_" + ident([cid, index, t["function"]])
    return ChatCompletion(
        id=cid,
        created=created,
        object="chat.completion",
        model=kwargs["model"],
        usage=usage,
        choices=[
            {
                "index": 0,
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": "".join(text) or None,
                    "reasoning": "".join(reasoning),
                    "tool_calls": list(calls.values()) or None,
                },
            }
        ],
    )


def recover_tool_calls(message, schemas):
    """Decode GLM's documented-style tagged tool output if the endpoint parser did not.

    Preserve requested names and arguments, including invalid ones, for framework
    validation. The framework only executes offered tools; unknown names are never mapped.
    """
    if (
        message.tool_calls
        or not message.content
        or "<tool_call>" not in message.content
    ):
        return False
    from openai.types.chat import ChatCompletionMessageFunctionToolCall

    allowed = {
        s["function"]["name"]: s["function"].get("parameters", {})
        for s in schemas
        if s.get("type") == "function"
    }
    calls = []
    for body in re.findall(r"<tool_call>(.*?)</tool_call>", message.content, re.S):
        m = re.match(r"\s*(\w+)\s*(.*)", body, re.S)
        if not m:
            raise ValueError("Malformed tagged tool name")
        name, tail = m.groups()
        args = {}
        for key, value in re.findall(
            r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>", tail, re.S
        ):
            key = key.strip()
            value = value.strip()
            spec = allowed.get(name, {}).get("properties", {}).get(key, {})
            if spec.get("type") == "string":
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    parsed = value
                args[key] = parsed if isinstance(parsed, str) else value
            else:
                try:
                    args[key] = json.loads(value)
                except json.JSONDecodeError:
                    args[key] = value
        calls.append(
            ChatCompletionMessageFunctionToolCall(
                id="call_" + ident([name, args, len(calls)]),
                type="function",
                function={"name": name, "arguments": json.dumps(args)},
            )
        )
    if not calls:
        raise ValueError("Incomplete tagged tool call")
    message.tool_calls = calls
    message.content = None
    return True


class ModelGateway:
    def __init__(
        self, state, directory, deadline, repair_needed=lambda: False, trace=None
    ):
        self.trace = trace or Trace(directory)
        self.state = state
        self.directory = directory
        self.deadline = deadline
        self.repair_needed = repair_needed
        self.usage = {}
        self.spent = 0.0
        self.calls = []
        self.clients = []
        self.counter = {}
        self.generation_failures = 0
        self.seen_generation_errors = set()
        state.setdefault("down", {})
        state.setdefault("spend", 0.0)
        self.pin = os.environ.get("RCA_MODEL", "")
        if self.pin and self.pin not in PRICES:
            raise ValueError("RCA_MODEL must be a permitted GLM model")
        budget_file = os.environ.get("RCA_BUDGET_FILE")
        self.budget_file = Path(budget_file) if budget_file else None

    def remaining_budget(self):
        remaining = min(1.0 - self.spent, 20.0 - self.state["spend"])
        if self.budget_file:
            ledger = (
                json.loads(self.budget_file.read_text())
                if self.budget_file.exists()
                else {"spent": 0}
            )
            remaining = min(
                remaining, float(os.getenv("RCA_DEV_BUDGET", "10")) - ledger["spent"]
            )
        return remaining

    def record(self, name, response, seconds):
        usage = getattr(response, "usage", None)
        # Reserve conservative estimates if an otherwise successful response omits usage.
        counts = {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "calls": 1,
        }
        entry = self.usage.setdefault(name, dict.fromkeys(counts, 0))
        for k, v in counts.items():
            entry[k] += v
        charge = dollars({name: counts})
        self.spent += charge
        self.state["spend"] += charge
        if self.budget_file:
            self.budget_file.parent.mkdir(parents=True, exist_ok=True)
            ledger = (
                json.loads(self.budget_file.read_text())
                if self.budget_file.exists()
                else {"spent": 0, "calls": 0}
            )
            ledger["spent"] += charge
            ledger["calls"] += 1
            tmp = self.budget_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(ledger))
            tmp.replace(self.budget_file)
        self.calls.append(
            {"model": name, **counts, "dollars": charge, "seconds": round(seconds, 3)}
        )
        (self.directory / "model_calls.json").write_text(
            json.dumps(self.calls, indent=2)
        )

    def reserve_unknown(self, name, text, max_tokens, seconds):
        from types import SimpleNamespace

        proxy = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=len(text), completion_tokens=max_tokens)
        )
        self.record(name, proxy, seconds)
        self.calls[-1]["usage_estimated"] = True

    def generation_error(self, detail, request_id=None):
        self.generation_failures += 1
        self.trace.emit(
            "generation_error",
            {
                "detail": detail,
                "failures": self.generation_failures,
                "corrective_attempts_remaining": max(0, 3 - self.generation_failures),
            },
            request_id=request_id,
        )
        if self.generation_failures >= 3:
            raise GenerationExhausted(
                "Malformed tool output exhausted initial attempt plus two corrections; investigation incomplete"
            )

    def prepare_request(self, kwargs):
        kwargs["messages"] = list(kwargs.get("messages", []))
        latest_errors = []
        for message in kwargs["messages"]:
            if message.get("role") != "tool":
                continue
            content = str(message.get("content", ""))
            key = (message.get("tool_call_id"), content)
            if content.startswith("Error:") and key not in self.seen_generation_errors:
                self.seen_generation_errors.add(key)
                latest_errors.append(content)
                self.generation_error(content)
        if kwargs.get("tool_choice") == "none":
            kwargs.pop("tools", None)
            kwargs.pop("tool_choice", None)
            kwargs.pop("parallel_tool_calls", None)
            kwargs["messages"].append(
                {
                    "role": "user",
                    "content": "Tool use is disabled for this turn. Return a plain-text summary of actual progress and remaining work. Do not emit tool calls or claim completion unless finish_investigation was already accepted. Otherwise explicitly report incomplete.",
                }
            )
            return False
        if latest_errors:
            names = [t.get("function", {}).get("name") for t in kwargs.get("tools", [])]
            kwargs["messages"].append(
                {
                    "role": "user",
                    "content": "Repair only the tool errors above. Allowed tool names: "
                    + json.dumps(names)
                    + ". Use exact names and argument schemas; rules must be a JSON array, not a string. Never invent a replacement tool. Remaining malformed-call corrections: "
                    + str(3 - self.generation_failures),
                }
            )
        return bool(kwargs.get("tools"))

    def client(self, role):
        raw = AsyncOpenAI(
            api_key=os.environ["FEATHERLESS_API_KEY"],
            base_url=os.getenv("FEATHERLESS_BASE_URL", "https://api.featherless.ai/v1"),
            max_retries=0,
            timeout=25.0,
        )
        self.clients.append(raw)
        original = raw.chat.completions.create

        async def routed(**kwargs):
            self.trace.observe_tool_results(kwargs.get("messages", []))
            self.trace.emit("framework_model_request", kwargs, role=role)
            wants_stream = kwargs.get("stream", False)
            kwargs.pop("stream_options", None)
            tools_allowed = self.prepare_request(kwargs)
            self.counter[role] = self.counter.get(role, 0) + 1
            text = json.dumps(kwargs.get("messages", []))
            strong = (
                role == "investigator"
                and (self.counter[role] >= 3 or self.repair_needed())
            ) or (role == "manager" and "FINAL_DIAGNOSIS" in text[-6000:])
            models = [self.pin] if self.pin else (STRONG if strong else CHEAP)
            kwargs.pop("max_completion_tokens", None)
            kwargs["max_tokens"] = (
                900 if role == "manager" else (1600 if strong else 1200)
            )
            kwargs["stream"] = False
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
            if tools_allowed:
                kwargs["parallel_tool_calls"] = False
            # GLM's provider accepts the chat-completions JSON object contract;
            # local framework validation retains the actual manager schema.
            if kwargs.get("response_format", {}).get("type") == "json_schema":
                kwargs["response_format"] = {"type": "json_object"}
            kwargs.pop("store", None)
            for name in models:
                if self.state["down"].get(name, 0) >= 2:
                    continue
                for attempt in range(2):
                    left = self.deadline - time.monotonic()
                    if left < 2:
                        self.trace.emit(
                            "model_rejected",
                            {"reason": "Case deadline reached"},
                            role=role,
                            model=name,
                        )
                        raise BudgetExceeded("Case deadline reached")
                    estimated = (
                        len(text) * PRICES[name][0]
                        + kwargs["max_tokens"] * PRICES[name][1]
                    ) / 1e6
                    if estimated > self.remaining_budget():
                        self.trace.emit(
                            "model_rejected",
                            {"reason": "Spending limit reached"},
                            role=role,
                            model=name,
                        )
                        raise BudgetExceeded("Spending limit reached")
                    kwargs["model"] = name
                    kwargs["timeout"] = min(25, left)
                    start = time.monotonic()
                    accounted = False
                    request_id = self.trace.emit(
                        "model_request",
                        {
                            **kwargs,
                            "stream": True,
                            "stream_options": {"include_usage": True},
                        },
                        role=role,
                        model=name,
                        attempt=attempt,
                    )
                    try:
                        response = await asyncio.wait_for(
                            collect_completion(
                                original, kwargs, self.trace, request_id
                            ),
                            timeout=min(25, left),
                        )
                        self.trace.emit(
                            "provider_response", response, request_id=request_id
                        )
                        if getattr(response, "usage", None):
                            self.record(name, response, time.monotonic() - start)
                            accounted = True
                        if not getattr(response, "choices", None):
                            raise ProviderResponseError(
                                "Provider returned no choices (capacity/error body)"
                            )
                        if not getattr(response, "usage", None):
                            self.reserve_unknown(
                                name,
                                text,
                                kwargs["max_tokens"],
                                time.monotonic() - start,
                            )
                            accounted = True
                        message = response.choices[0].message
                        # Featherless's current GLM-4.7-Flash parser places even
                        # non-thinking final text in `reasoning`. Only recover it
                        # when thinking was explicitly disabled and no tool call
                        # or ordinary final content exists. Do not combine channels.
                        recovered = False
                        if not message.content and not message.tool_calls:
                            misplaced = getattr(message, "reasoning", None)
                            if isinstance(misplaced, str) and misplaced.strip():
                                message.content = misplaced
                                recovered = True
                        try:
                            if not message.content and not message.tool_calls:
                                raise ValueError("Empty assistant response")
                            has_call = bool(message.tool_calls) or "<tool_call>" in (
                                message.content or ""
                            )
                            if not tools_allowed and has_call:
                                raise ValueError(
                                    "Model emitted tool calls while tool use was disabled"
                                )
                            if tools_allowed and recover_tool_calls(
                                message, kwargs.get("tools", [])
                            ):
                                response.choices[0].finish_reason = "tool_calls"
                        except ValueError as exc:
                            self.generation_error(str(exc), request_id)
                            message.tool_calls = None
                            message.content = (
                                "Investigation incomplete for this turn: "
                                + str(exc)
                                + ". No rejected tool call was executed. Preserve provisional evidence and delegate a correction if budget permits."
                            )
                            response.choices[0].finish_reason = "stop"
                        self.trace.emit(
                            "model_response", response, request_id=request_id, role=role
                        )
                        self.trace.register_tools(request_id, message.tool_calls or [])
                        self.calls[-1].update(
                            role=role,
                            finish_reason=response.choices[0].finish_reason,
                            content=message.content,
                            tool_names=[
                                t.function.name for t in (message.tool_calls or [])
                            ],
                            tool_calls=[
                                t.model_dump() for t in (message.tool_calls or [])
                            ],
                            recovered_non_thinking_text=recovered,
                            field_sizes={
                                k: len(str(v))
                                for k, v in message.model_dump().items()
                                if v is not None
                            },
                        )
                        (self.directory / "model_calls.json").write_text(
                            json.dumps(self.calls, indent=2)
                        )
                        if wants_stream:
                            from openai.types.chat import ChatCompletionChunk

                            async def one_chunk():
                                delta = {
                                    "role": "assistant",
                                    "content": message.content,
                                }
                                if message.tool_calls:
                                    delta["tool_calls"] = [
                                        {"index": i, **t.model_dump()}
                                        for i, t in enumerate(message.tool_calls)
                                    ]
                                yield ChatCompletionChunk(
                                    id=response.id,
                                    object="chat.completion.chunk",
                                    created=response.created,
                                    model=name,
                                    usage=response.usage,
                                    choices=[
                                        {
                                            "index": 0,
                                            "delta": delta,
                                            "finish_reason": response.choices[
                                                0
                                            ].finish_reason,
                                        }
                                    ],
                                )

                            return one_chunk()
                        return response
                    except BudgetExceeded:
                        raise
                    except asyncio.CancelledError:
                        self.trace.emit(
                            "model_interrupted",
                            {"reason": "Cancelled or case deadline"},
                            request_id=request_id,
                        )
                        if not accounted:
                            self.reserve_unknown(
                                name,
                                text,
                                kwargs["max_tokens"],
                                time.monotonic() - start,
                            )
                        raise
                    except Exception as exc:
                        category = (
                            "deadline"
                            if isinstance(exc, (TimeoutError, asyncio.TimeoutError))
                            else (
                                "response_processing"
                                if isinstance(
                                    exc, (ValueError, TypeError, RuntimeError)
                                )
                                else "provider"
                            )
                        )
                        self.trace.emit(
                            "model_error",
                            {
                                "type": type(exc).__name__,
                                "message": str(exc),
                                "cause": repr(exc.__cause__),
                            },
                            request_id=request_id,
                            category=category,
                        )
                        if (
                            isinstance(exc, (TimeoutError, asyncio.TimeoutError))
                            and not accounted
                        ):
                            self.reserve_unknown(
                                name,
                                text,
                                kwargs["max_tokens"],
                                time.monotonic() - start,
                            )
                        if category == "response_processing" and not isinstance(
                            exc, ProviderResponseError
                        ):
                            self.generation_error(str(exc), request_id)
                            raise GenerationExhausted(
                                "Response processing failed; investigation incomplete, model availability unchanged"
                            ) from exc
                        self.state["down"][name] = self.state["down"].get(name, 0) + 1
                        # Log exception class only: provider exceptions may contain credentials or huge prompts.
                        self.calls.append(
                            {
                                "model": name,
                                "error": type(exc).__name__,
                                "message": clean(str(exc)),
                                "request_id": request_id,
                                "category": category,
                            }
                        )
                        if attempt == 0 and self.state["down"][name] < 2:
                            await asyncio.sleep(
                                min(0.5, max(0, self.deadline - time.monotonic()))
                            )
            raise RuntimeError(
                "Configured models exhausted provider/transport retries: "
                + json.dumps({name: self.state["down"].get(name, 0) for name in models})
            )

        raw.chat.completions.create = routed
        return OpenAIChatCompletionClient(
            model=self.pin or CHEAP[0],
            async_client=raw,
            function_invocation_configuration={
                "max_iterations": 4,
                "max_function_calls": 20,
                "include_detailed_errors": True,
            },
        )

    async def close(self):
        for c in self.clients:
            await c.close()
