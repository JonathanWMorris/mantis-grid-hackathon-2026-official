"""Complete, credential-sanitized local traces. No network exporter."""

from __future__ import annotations
import dataclasses
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path


def clean(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]"
            if re.search(
                r"authorization|api[_-]?key|access[_-]?token|secret", str(k), re.I
            )
            else clean(v)
            for k, v in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [clean(v) for v in value]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    value = str(value)
    for key, secret in os.environ.items():
        if (
            secret
            and len(secret) > 6
            and re.search(r"API_KEY|TOKEN|SECRET|PASSWORD", key)
        ):
            value = value.replace(secret, "[REDACTED]")
    return re.sub(r"(?i)Bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", value)


class Trace:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.payloads = self.directory / "trace_payloads"
        self.payloads.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.pending = []
        self.tool_requests = {}
        self.tool_results_seen = set()

    def emit(self, kind, payload=None, **fields):
        event_id = uuid.uuid4().hex
        with self.lock:
            event = {
                "event_id": event_id,
                "timestamp": time.time(),
                "kind": kind,
                **clean(fields),
            }
            if payload is not None:
                path = self.payloads / f"{event_id}.json"
                path.write_text(
                    json.dumps(
                        clean(payload), ensure_ascii=False, indent=2, default=str
                    )
                )
                event["payload"] = str(path.relative_to(self.directory))
            with (self.directory / "trace.jsonl").open("a") as f:
                f.write(json.dumps(event) + "\n")
        return event_id

    def register_tools(self, request_id, calls):
        with self.lock:
            for c in calls:
                self.pending.append(
                    (request_id, c.id, c.function.name, c.function.arguments)
                )
                self.tool_requests[c.id] = request_id

    def claim_tool(self, name, args):
        with self.lock:
            for i, (request, call, tool, raw) in enumerate(self.pending):
                if tool != name:
                    continue
                try:
                    expected = json.loads(raw)
                except ValueError:
                    continue
                if all(k in args and args[k] == v for k, v in expected.items()):
                    self.pending.pop(i)
                    return {"request_id": request, "tool_call_id": call}
        return {"tool_call_id": uuid.uuid4().hex}

    def observe_tool_results(self, messages):
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "tool":
                call = message.get("tool_call_id")
                key = (call, str(message.get("content")))
                if key not in self.tool_results_seen:
                    self.tool_results_seen.add(key)
                    self.emit(
                        "framework_tool_result",
                        message,
                        tool_call_id=call,
                        request_id=self.tool_requests.get(call),
                    )
