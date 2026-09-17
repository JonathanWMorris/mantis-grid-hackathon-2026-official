from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rca.schema import parse_case, clock
from rca.store import Store
from rca.symbolic import run_prolog
from agents.magentic import solve, parse_json
from score import evaluate

INSTRUCTION = "The system experienced one failure within March 20, 2022, from 09:00 to 09:30. Identify the component and reason."


@pytest.fixture
def telemetry(tmp_path):
    case = parse_case(INSTRUCTION)
    data = tmp_path / "data"
    out = tmp_path / "out"
    folder = data / "telemetry/2022_03_20/metric"
    folder.mkdir(parents=True)
    for name, entity in [
        ("metric_container", "node-1.shop-0"),
        ("metric_node", "node-1"),
    ]:
        with (folder / f"{name}.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "cmdb_id", "kpi_name", "value"])
            for i in range(-30, 30):
                value = 10 if i < 10 or name == "metric_node" else 90
                w.writerow(
                    [
                        (case.start + i * 60000) // 1000,
                        entity,
                        "container_memory_working_set_MB",
                        value,
                    ]
                )
    return case, Store(data, out), data, out


def test_timezone_midnight():
    c = parse_case(INSTRUCTION)
    assert c.start == 1647738000000
    assert clock(c.start) == "2022-03-20 09:00:00"
    midnight = parse_case(
        "one failure on March 20, 2022, from 23:30 to March 21, 2022, at 00:00"
    )
    assert midnight.end - midnight.start == 1800000
    assert parse_case(INSTRUCTION.replace("one failure", "two failures")).count == 2


def test_evidence_and_constant_baseline(telemetry):
    case, store, _, _ = telemetry
    rows = store.metrics(case)
    changed = next(r for r in rows if r["entity"] == "shop-0")
    assert changed["score"] > 4
    assert changed["baseline_mad"] == 0
    assert changed["onset"] == [case.start + 9 * 60000, case.start + 10 * 60000]
    assert store.evidence.get(changed["id"])["peak_value"] == 90
    assert store.entities(case)[1] == {"shop-0": "node-1"}


def test_prolog_queries_rules_and_rejection(telemetry):
    case, store, _, out = telemetry
    store.metrics(case)
    r = run_prolog(store, case, out, "candidate(P,memory,E), hosted_on(P,N).", [])
    assert r["status"] == "ok" and r["bindings"][0]["P"] == "'shop-0'"
    assert run_prolog(store, case, out, "candidate(P,memory,E)", [])["bindings"]
    r = run_prolog(
        store,
        case,
        out,
        "h_test(P,E).",
        ["h_test(P,E) :- candidate(P,memory,E), hosted_on(P,N), normal(N,memory,_)."],
    )
    assert r["status"] == "ok" and r["bindings"]
    assert run_prolog(store, case, out, "shell('touch bad').", [])["status"] == "error"
    assert (
        run_prolog(store, case, out, "h_test(X).", ["observation(a,b,c,d,10,1,2,e)."])[
            "status"
        ]
        == "error"
    )


def test_cycles_and_case_isolation(telemetry):
    case, store, _, out = telemetry
    store.metrics(case)
    for a, b in [("a", "b"), ("b", "a")]:
        store.evidence.add(
            {"kind": "trace", "case": case.key, "entity": b, "parents": [a], "ratio": 2}
        )
    result = run_prolog(store, case, out, "depends_on(a,X).", [])
    assert result["status"] == "ok" and len(result["bindings"]) == 2
    second = run_prolog(store, case, out, "h_old(X).", [])
    assert second["status"] == "error"


def test_offline_submission(telemetry, monkeypatch):
    case, store, data, out = telemetry
    monkeypatch.setenv("RCA_OFFLINE", "1")
    sol = solve(
        INSTRUCTION,
        data,
        {"out_dir": out, "row_id": 11, "remaining_cases": 1, "rca_store": store},
    )
    assert "shop-0" in sol.prediction
    assert all(
        "## " + s in sol.evidence
        for s in ["Answer", "Confidence", "Evidence", "Ruled out"]
    )
    score = evaluate(
        sol.prediction,
        "The only predicted root cause component is shop-0\nThe only predicted root cause reason is container memory load",
    )[2]
    assert score == 1
    assert sol.usage == {}


def test_parse_json():
    assert parse_json('some text ```json\n{"answers":[]}\n```') == {"answers": []}
    with pytest.raises(ValueError):
        parse_json("not json")


def test_full_query_catalog_parses():
    path = Path(__file__).resolve().parents[2] / "data/Market-cloudbed-1/query.csv"
    if not path.exists():
        pytest.skip("Real dataset not mounted")
    with path.open() as f:
        cases = [parse_case(r["instruction"]) for r in csv.DictReader(f)]
    assert len(cases) == 70
    assert all(c.end - c.start == 1800000 for c in cases)


def test_grouped_split_keeps_overlaps():
    from evaluate_pipeline import grouped_split

    rows = [{"row_id": str(i), "instruction": INSTRUCTION} for i in range(3)]
    rows.append(
        {
            "row_id": "3",
            "instruction": INSTRUCTION.replace("09:00 to 09:30", "09:15 to 09:45"),
        }
    )
    rows.append(
        {
            "row_id": "4",
            "instruction": INSTRUCTION.replace("09:00 to 09:30", "11:00 to 11:30"),
        }
    )
    result = grouped_split(rows)
    assert any(set(g["rows"]) == {0, 1, 2, 3} for g in result["groups"])
    assert set(result["development"]).isdisjoint(result["holdout"])


def test_trace_join_uses_trace_id_and_milliseconds(telemetry):
    case, store, data, _ = telemetry
    folder = data / "telemetry/2022_03_20/trace"
    folder.mkdir()
    with (folder / "trace_span.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "timestamp",
                "cmdb_id",
                "span_id",
                "trace_id",
                "duration",
                "type",
                "status_code",
                "operation_name",
                "parent_span",
            ]
        )
        for trace, entity in [("t1", "caller-a"), ("t2", "caller-b")]:
            w.writerow([case.start, entity, "p", trace, 500, "rpc", "0", "call", ""])
            w.writerow(
                [case.start + 10, "shop-0", "c", trace, 100, "rpc", "0", "serve", "p"]
            )
    result = store.traces(case, "shop-0")["observations"][0]
    assert result["samples"] == 2
    assert result["parents"] == ["caller-a", "caller-b"]
    assert result["p95_parent_start_gap_ms"] == 10


def test_log_csv_embedded_commas_and_newlines(telemetry):
    case, store, data, _ = telemetry
    folder = data / "telemetry/2022_03_20/log"
    folder.mkdir()
    message = "error: connection refused, upstream\nretry failed"
    with (folder / "log_service.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["log_id", "timestamp", "cmdb_id", "log_name", "value"])
        w.writerow(["log1", case.start // 1000, "shop-0", "application", message])
    result = store.logs(case, "shop-0")["observations"][0]
    assert result["count"] == 1
    assert result["examples"][0]["value"] == message


def test_deadline_and_tool_limits(telemetry):
    import time
    from rca.tools import InvestigatorTools

    case, store, _, out = telemetry
    out.mkdir(exist_ok=True)
    tools = InvestigatorTools(store, case, out, time.monotonic() - 1)
    assert json.loads(tools.find_changes())["status"] == "unavailable"
    assert tools.count == 0


def test_multiple_events_preserved(telemetry, monkeypatch):
    _, store, data, out = telemetry
    monkeypatch.setenv("RCA_OFFLINE", "1")
    sol = solve(
        INSTRUCTION.replace("one failure", "two failures"),
        data,
        {"out_dir": out, "row_id": 22, "remaining_cases": 1, "rca_store": store},
    )
    import re

    assert len(re.findall("root cause component", sol.prediction)) == 2


def test_malformed_model_payload_falls_back(telemetry):
    from agents.magentic import validate_answers, evidence_report

    case, store, _, _ = telemetry
    fallback = store.candidates(case)[:1]
    for bad in [
        {"answers": None},
        {"answers": {}},
        {"answers": [{"evidence_ids": None}]},
    ]:
        answers, notes = validate_answers(bad, case, store, fallback)
        assert len(answers) == 1 and answers[0]["component"] == "shop-0"
        assert "## Evidence" in evidence_report(
            answers, {"alternatives": None, "limitations": None}, store, notes, None
        )


def test_unquoted_atom_and_extra_terms_are_rejected(telemetry):
    case, store, _, out = telemetry
    store.metrics(case)
    assert (
        run_prolog(store, case, out, "candidate(shop-0,memory,E).", [])["status"]
        == "error"
    )
    assert (
        run_prolog(store, case, out, "candidate(P,memory,E). true.", [])["status"]
        == "error"
    )
