"""Replay sampled metric evidence directly against original CSV records, without labels."""

import argparse
import json
from pathlib import Path

import duckdb


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()
    db = duckdb.connect()
    db.execute("SET threads=2")
    db.execute("SET memory_limit='1GB'")
    checked = []
    for p in sorted((args.out / "artifacts/evidence").glob("*.json")):
        e = json.loads(p.read_text())
        if (
            e.get("kind") != "metric"
            or e.get("semantics") != "exported_value"
            or e["source"] == "metric_service"
        ):
            continue
        files = [str(args.dataset / f) for f in e["files"]]
        lo, hi = e["window"]
        b0, b1 = e["baseline_window"]
        result = db.execute(
            """SELECT
          median(TRY_CAST(value AS DOUBLE)) FILTER (WHERE CAST(timestamp AS BIGINT)*1000 < ?) AS baseline,
          median(TRY_CAST(value AS DOUBLE)) FILTER (WHERE CAST(timestamp AS BIGINT)*1000 >= ?) AS incident,
          max(TRY_CAST(value AS DOUBLE)) FILTER (WHERE CAST(timestamp AS BIGINT)*1000 >= ?) AS peak,
          min(TRY_CAST(value AS DOUBLE)) FILTER (WHERE CAST(timestamp AS BIGINT)*1000 >= ?) AS minimum
          FROM read_csv(?,header=true,all_varchar=true)
          WHERE cmdb_id=? AND kpi_name=? AND CAST(timestamp AS BIGINT)*1000>=? AND CAST(timestamp AS BIGINT)*1000<?""",
            [lo, lo, lo, lo, files, e["raw_entity"], e["signal"], b0, hi],
        ).fetchone()
        expected = [e["baseline_median"], e["incident_median"], e["max"], e["min"]]
        valid = all(
            a == b
            or (
                a is not None
                and b is not None
                and abs(a - b) <= max(1e-8, abs(b) * 1e-9)
            )
            for a, b in zip(result, expected)
        )
        checked.append({"evidence": e["id"], "valid": valid, "source": e["source"]})
        if len(checked) >= args.limit:
            break
    report = {
        "checked": len(checked),
        "passed": sum(x["valid"] for x in checked),
        "details": checked,
        "scope": "Sampled untransformed metric summaries replayed directly from original CSVs; not a causal validation.",
    }
    (args.out / "evidence_validation.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    if not checked or report["passed"] != len(checked):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
