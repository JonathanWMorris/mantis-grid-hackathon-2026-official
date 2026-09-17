from __future__ import annotations

import json
import time
import threading

from .symbolic import run_prolog, inspect_kb, kb_snapshot
from .trace import Trace
from .diagnosis import Diagnosis, SupportedDiagnosis


class InvestigatorTools:
    def __init__(self, store, case, directory, deadline, trace=None):
        self.store = store
        self.case = case
        self.directory = directory
        self.deadline = deadline
        self.count = 0
        self.prolog_failures = 0
        self.events = []
        self.latest_decision = {}
        self.lock = threading.RLock()
        self.trace = trace or Trace(directory)
        self.executions = {}
        self.completed_decision = None

    def invoke(self, name, args, fn):
        with self.lock:
            return self._invoke(name, args, fn)

    def _invoke(self, name, args, fn):
        correlation = self.trace.claim_tool(name, args)
        event_id = self.trace.emit("tool_start", args, tool=name, **correlation)
        start = time.monotonic()
        before = None
        if self.completed_decision is not None:
            result = {
                "status": "unavailable",
                "reason": "Investigation already completed; use the accepted diagnosis",
            }
        elif self.count >= 20 or time.monotonic() >= self.deadline:
            result = {
                "status": "unavailable",
                "reason": "Investigation budget exhausted; preserve provisional diagnosis",
            }
        else:
            self.count += 1
            try:
                before = kb_snapshot(self.store, self.case)["kb_version"]
                result = fn()
                after = kb_snapshot(self.store, self.case)["kb_version"]
                if isinstance(result, dict):
                    result.setdefault("kb_version", after)
                    if before != after:
                        result.update(kb_changed=True, previous_kb_version=before)
            except Exception as e:
                result = {
                    "status": "error",
                    "error": type(e).__name__,
                    "message": str(e),
                }
        self.events.append(
            {
                "tool": name,
                "arguments": args,
                "seconds": round(time.monotonic() - start, 3),
                "status": result.get("status", "ok")
                if isinstance(result, dict)
                else "ok",
                **correlation,
            }
        )
        self.trace.emit(
            "tool_result",
            result,
            tool=name,
            parent_event_id=event_id,
            seconds=time.monotonic() - start,
            **correlation,
        )
        (self.directory / "tools.json").write_text(
            json.dumps(self.events, indent=2, default=str)
        )
        return json.dumps(result, default=str)

    def inspect_kb(
        self,
        predicate: str = "",
        component: str = "",
        offset: int = 0,
        limit: int = 100,
    ) -> str:
        """Read exact incident Prolog facts and trusted rules. Page through the full KB or filter by predicate/component; maximum 250 facts/page."""
        return self.invoke(
            "inspect_kb",
            dict(predicate=predicate, component=component, offset=offset, limit=limit),
            lambda: inspect_kb(
                self.store, self.case, predicate, component, offset, limit
            ),
        )

    def describe_data(self) -> str:
        """List component IDs, available metric families, incident scope and source files."""

        def work():
            entities, hosts = self.store.entities(self.case)
            return {
                "entities": entities,
                "hosting": hosts,
                "window_ms": [self.case.start, self.case.end],
                "failure_count": self.case.count,
                "files": [
                    str(p.relative_to(self.store.dataset))
                    for p in self.store.dataset.glob("telemetry/*/*/*.csv")
                ],
                "families": sorted(
                    set(x["family"] for x in self.store.metrics(self.case))
                ),
            }

        return self.invoke("describe_data", {}, work)

    def get_topology(self, component: str = "") -> str:
        """Get pod hosting and call edges observed so far; inspect_traces can add call edges."""

        def work():
            _, hosts = self.store.entities(self.case)
            return {
                "hosting": {
                    p: n
                    for p, n in hosts.items()
                    if not component or component in (p, n)
                },
                "calls": [
                    {"caller": p, "callee": e["entity"], "evidence": e["id"]}
                    for e in self.store.evidence.items.values()
                    if e.get("case") == self.case.key and e["kind"] == "trace"
                    for p in e["parents"]
                    if not component or component in (p, e["entity"])
                ],
            }

        return self.invoke("get_topology", {"component": component}, work)

    def find_changes(
        self, component: str = "", signal_family: str = "", source: str = ""
    ) -> str:
        """Find changes; source may be metric_container, metric_node, metric_service, metric_mesh or metric_runtime. Empty means initial sources."""

        def work():
            if source and source not in (
                "metric_container",
                "metric_node",
                "metric_service",
                "metric_mesh",
                "metric_runtime",
            ):
                raise ValueError("Unsupported metric source")
            rows = [
                x
                for x in self.store.metrics(self.case, [source] if source else None)
                if (not component or x["entity"] == component)
                and (not signal_family or x["family"] == signal_family)
            ]
            fields = [
                "id",
                "entity",
                "signal",
                "family",
                "score",
                "direction",
                "onset",
                "baseline_median",
                "incident_median",
                "peak_value",
                "semantics",
                "samples",
                "baseline_samples",
            ]
            return {
                "observations": [{k: r[k] for k in fields} for r in rows[:15]],
                "total": len(rows),
                "truncated": len(rows) > 15,
            }

        return self.invoke(
            "find_changes",
            locals_args(
                component=component, signal_family=signal_family, source=source
            ),
            work,
        )

    def compare_entities(self, components: list[str], signal_family: str) -> str:
        """Compare up to eight specified entities on the same metric family; returns strongest and stable observations."""

        def work():
            rows = self.store.metrics(self.case)
            return {
                "comparisons": {
                    c: [
                        {
                            k: r[k]
                            for k in (
                                "id",
                                "signal",
                                "score",
                                "baseline_median",
                                "incident_median",
                                "samples",
                                "baseline_samples",
                            )
                        }
                        for r in rows
                        if r["entity"] == c and r["family"] == signal_family
                    ][:6]
                    for c in components[:8]
                }
            }

        return self.invoke(
            "compare_entities",
            {"components": components, "family": signal_family},
            work,
        )

    def inspect_traces(self, component: str = "") -> str:
        """Compare operation latency and parent-child relationships for a component; raw duration units remain unverified."""
        return self.invoke(
            "inspect_traces",
            {"component": component},
            lambda: self.store.traces(self.case, component),
        )

    def inspect_logs(self, component: str = "", source: str = "log_service") -> str:
        """Compare log patterns; source is log_service or log_proxy. Counts are bounded-sample counts."""
        return self.invoke(
            "inspect_logs",
            {"component": component, "source": source},
            lambda: self.store.logs(self.case, component, source),
        )

    def get_evidence(self, evidence_id: str) -> str:
        """Retrieve the source filters, coverage, measurements and examples behind an evidence ID."""
        return self.invoke(
            "get_evidence",
            {"evidence_id": evidence_id},
            lambda: self.store.evidence.get(evidence_id),
        )

    def run_prolog(self, query: str, rules: list[str]) -> str:
        """Execute one Prolog query and optional h_-prefixed rule clauses over trusted incident facts. No Markdown or ?- prefix."""

        def work():
            if self.prolog_failures >= 3:
                return {"status": "error", "error": "Prolog repair budget exhausted"}
            result = run_prolog(self.store, self.case, self.directory, query, rules)
            if result.get("execution_id"):
                self.executions[result["execution_id"]] = result
            self.trace.emit(
                "prolog_execution", result, execution_id=result.get("execution_id")
            )
            if result.get("status") != "ok":
                self.prolog_failures += 1
            return result

        return self.invoke("run_prolog", {"query": query, "rules": rules}, work)

    def validate_diagnoses(self, answers, supported=False):
        model = SupportedDiagnosis if supported else Diagnosis
        normalized = [model.model_validate(a).model_dump() for a in answers]
        if not normalized or len(normalized) > self.case.count:
            raise ValueError(f"Expected 1..{self.case.count} diagnoses")
        entities, _ = self.store.entities(self.case)
        for a in normalized:
            if a["component"] not in entities:
                raise ValueError("component must be an observed exact component ID")
            if a["reason"].startswith("node") != a["component"].startswith("node-"):
                raise ValueError("reason must match node/container component scope")
            if not self.case.start <= a["onset"] < self.case.end:
                raise ValueError(
                    "onset must be epoch milliseconds within the incident window"
                )
            for k in a["evidence_ids"]:
                e = self.store.evidence.items.get(k)
                if not e or e.get("case") != self.case.key:
                    raise ValueError(
                        f"evidence_ids contains an unknown or different-incident ID: {k}"
                    )
            if not any(
                self.store.evidence.items[k].get("entity") == a["component"]
                for k in a["evidence_ids"]
            ):
                raise ValueError(
                    "evidence_ids must include measured evidence for the diagnosed component"
                )
        return normalized

    def record_diagnosis(self, answers: list[Diagnosis], limitations: list[str]) -> str:
        """Save provisional diagnoses. reason must be an exact allowed label; this does not complete the investigation."""
        values = [a.model_dump() if isinstance(a, Diagnosis) else a for a in answers]

        def work():
            checked = self.validate_diagnoses(values)
            self.latest_decision = {
                "answers": checked,
                "limitations": limitations[:5],
                "investigation_status": "incomplete",
            }
            (self.directory / "provisional.json").write_text(
                json.dumps(self.latest_decision, indent=2)
            )
            return {
                "status": "ok",
                "recorded": len(checked),
                "note": "Provisional only. Execute supporting Prolog then call finish_investigation.",
            }

        return self.invoke(
            "record_diagnosis", {"answers": values, "limitations": limitations}, work
        )

    def finish_investigation(
        self, answers: list[SupportedDiagnosis], limitations: list[str]
    ) -> str:
        """Complete investigation with exactly the requested diagnoses, measured evidence and successful current-KB Prolog support. Cite execution_id, zero-based binding_indices, explanation and assumptions for each answer."""
        values = [
            a.model_dump() if isinstance(a, SupportedDiagnosis) else a for a in answers
        ]

        def work():
            checked = self.validate_diagnoses(values, supported=True)
            if len(checked) != self.case.count:
                raise ValueError(
                    f"Need exactly {self.case.count} diagnoses to complete"
                )
            version = kb_snapshot(self.store, self.case)["kb_version"]
            for a in checked:
                linked = False
                for support in a["prolog_support"]:
                    result = self.executions.get(support["execution_id"])
                    if (
                        not result
                        or result.get("status") != "ok"
                        or not result.get("bindings")
                    ):
                        raise ValueError(
                            "Support requires an actual successful nonempty Prolog execution"
                        )
                    if result["kb_version"] != version:
                        raise ValueError(
                            "Supporting KB snapshot is stale; rerun the query against the current KB"
                        )
                    for i in support["binding_indices"]:
                        if i < 0 or i >= len(result["bindings"]):
                            raise ValueError(
                                "binding_indices must identify returned bindings"
                            )
                        binding = json.dumps(result["bindings"][i])
                        linked |= any(k in binding for k in a["evidence_ids"])
                if not linked:
                    raise ValueError(
                        "Supporting bindings must expose at least one cited measured evidence ID; include Evidence in the query"
                    )
            self.completed_decision = {
                "answers": checked,
                "limitations": limitations[:5],
                "investigation_status": "complete",
                "kb_version": version,
            }
            (self.directory / "completed.json").write_text(
                json.dumps(self.completed_decision, indent=2)
            )
            self.trace.emit("investigator_completed", self.completed_decision)
            return {
                "status": "ok",
                "investigation_status": "complete",
                "note": "Investigator diagnosis accepted; support is conditional, not proof of causation.",
            }

        return self.invoke(
            "finish_investigation",
            {"answers": values, "limitations": limitations},
            work,
        )

    def all(self):
        return [
            self.inspect_kb,
            self.finish_investigation,
            self.describe_data,
            self.get_topology,
            self.find_changes,
            self.compare_entities,
            self.inspect_traces,
            self.inspect_logs,
            self.get_evidence,
            self.run_prolog,
            self.record_diagnosis,
        ]


def locals_args(**kwargs):
    return kwargs
