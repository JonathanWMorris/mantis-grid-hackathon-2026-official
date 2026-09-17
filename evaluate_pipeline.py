"""Offline grouped split and budgeted paired evaluations. Labels never enter agent prompts."""

from __future__ import annotations

import argparse
import csv
import json
import hashlib
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time
import statistics
from types import SimpleNamespace

from cost import dollars
from rca.schema import parse_case
from score import evaluate


def grouped_split(rows, seed=2026):
    windows = sorted(
        (
            parse_case(r["instruction"]).start,
            parse_case(r["instruction"]).end,
            int(r["row_id"]),
        )
        for r in rows
    )
    groups = []
    for start, end, row in windows:
        if groups and start < groups[-1]["end"]:
            groups[-1]["end"] = max(end, groups[-1]["end"])
            groups[-1]["rows"].append(row)
        else:
            groups.append({"start": start, "end": end, "rows": [row]})
    shuffled = list(groups)
    random.Random(seed).shuffle(shuffled)
    held = set()
    target = max(1, round(0.2 * len(rows)))
    for g in shuffled:
        if len(held) >= target:
            break
        # Initial integration smoke cases are development cases, never holdout.
        if set(g["rows"]) & {0, 1}:
            continue
        held.update(g["rows"])
    return {
        "seed": seed,
        "smoke_rows_reserved_for_development": [0, 1],
        "groups": groups,
        "holdout": sorted(held),
        "development": sorted(
            int(r["row_id"]) for r in rows if int(r["row_id"]) not in held
        ),
    }


def source_manifest(root):
    files = [
        p
        for p in root.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and "tests" not in p.relative_to(root).parts
        and "eval" not in p.relative_to(root).parts
        and (
            p.suffix in (".py", ".pl")
            or "prompts" in p.relative_to(root).parts
            or p.name in ("requirements.txt", "requirements-dev.txt", "Dockerfile")
        )
    ]
    hashes = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(files)
    }
    return {
        "files": hashes,
        "sha256": hashlib.sha256(
            json.dumps(hashes, sort_keys=True).encode()
        ).hexdigest(),
    }


def repeat_statistics(summaries):
    result = {}
    for config in sorted({s["configuration"] for s in summaries}):
        complete = [
            s
            for s in summaries
            if s["configuration"] == config and s["status"] == "complete"
        ]
        metrics = {}
        for field in ("strict", "partial", "dollars_per_case", "seconds_per_case"):
            values = [s[field] for s in complete]
            metrics[field] = {
                "mean": statistics.mean(values) if values else None,
                "sample_sd": statistics.stdev(values) if len(values) > 1 else None,
            }
        result[config] = {"complete_repeats": len(complete), "metrics": metrics}
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument(
        "--split", choices=["development", "holdout"], default="development"
    )
    ap.add_argument("--limit", type=int, default=2)
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument(
        "--configs",
        nargs="+",
        choices=["routed", "fixed", "offline", "baseline"],
        default=["routed", "fixed", "offline"],
    )
    ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument("--case-seconds", type=float, default=90)
    args = ap.parse_args()
    if args.case_seconds <= 0 or args.repeats < 1 or args.limit < 0:
        ap.error("case-seconds and repeats must be positive; limit must be nonnegative")
    args.dataset = args.dataset.resolve()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.dataset / "dev/query_dev.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    split = grouped_split(rows)

    selected = [r for r in rows if int(r["row_id"]) in split[args.split]][
        : args.limit or None
    ]
    root = Path(__file__).parent
    sources = source_manifest(root)
    manifest = {
        "source": sources,
        "dataset": str(args.dataset),
        "row_ids": [int(r["row_id"]) for r in selected],
        "split": args.split,
        "case_seconds": args.case_seconds,
        "configs": args.configs,
        "repeats": args.repeats,
        "budget_file": str(args.out.parent / "development_spend.json"),
        "cumulative_budget_dollars": 10,
        "fixed_model": "zai-org/GLM-5.2",
        "cache": "separate cold cache per configuration/repeat",
        "query_fields": ["row_id", "task_index", "instruction"],
        "development_queries_sha256": hashlib.sha256(
            (args.dataset / "dev/query_dev.csv").read_bytes()
        ).hexdigest(),
    }
    manifest_path = args.out / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
        raise SystemExit(
            "Prepared manifest no longer matches code, prompts, inputs or run settings. Prepare a fresh --out."
        )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    (args.out / "split.json").write_text(json.dumps(split, indent=2))
    query = args.out / "queries.csv"
    with query.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=manifest["query_fields"])
        w.writeheader()
        w.writerows({k: r[k] for k in w.fieldnames} for r in selected)
    if args.prepare_only:
        print(
            f"Prepared {len(selected)} cases; no model calls. Manifest: {manifest_path}"
        )
        return
    lookup = {int(r["row_id"]): r for r in selected}
    summaries = []
    budget = args.out.parent / "development_spend.json"
    stopped = False
    for config in args.configs:
        if stopped:
            break
        for repeat in range(args.repeats if config in ("routed", "fixed") else 1):
            env = os.environ.copy()
            env.pop("RCA_MODEL", None)
            env.pop("RCA_OFFLINE", None)
            env["RCA_BUDGET_FILE"] = str(budget)
            env["RCA_DEV_BUDGET"] = "10"
            env["RCA_CASE_SECONDS"] = str(args.case_seconds)
            if config == "fixed":
                env["RCA_MODEL"] = "zai-org/GLM-5.2"
            if config in ("offline", "baseline"):
                env["RCA_OFFLINE"] = "1"
            if (
                config in ("routed", "fixed")
                and budget.exists()
                and json.loads(budget.read_text())["spent"] >= 9.5
            ):
                stopped = True
                break
            directory = args.out / f"{config}-{repeat}"
            if directory.exists():
                raise SystemExit(
                    f"{directory} already exists; choose a fresh --out for cold-start comparisons"
                )
            if source_manifest(root) != sources:
                raise SystemExit(
                    "Source changed during comparison; stop and prepare a fresh output directory"
                )
            implementation_hash = sources["sha256"]
            start = time.monotonic()
            cmd = [
                sys.executable,
                "run.py",
                "--dataset",
                str(args.dataset),
                "--queries",
                str(query),
                "--out",
                str(directory),
                "--agent",
                "agents.heuristic" if config == "baseline" else "agents.magentic",
            ]
            interrupted = False
            try:
                p = subprocess.run(
                    cmd, cwd=root, env=env, capture_output=True, text=True, timeout=1200
                )
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                interrupted = True
                stopped = True

                def decoded(value):
                    return (
                        value.decode(errors="replace")
                        if isinstance(value, bytes)
                        else (value or "")
                    )

                p = SimpleNamespace(
                    returncode=None,
                    stdout=decoded(getattr(exc, "stdout", "")),
                    stderr=decoded(getattr(exc, "stderr", ""))
                    + "\n"
                    + type(exc).__name__,
                )
            wall = time.monotonic() - start
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "stdout.log").write_text(p.stdout)
            (directory / "stderr.log").write_text(p.stderr)
            detail = []
            if (directory / "predictions.csv").exists():
                with (directory / "predictions.csv").open(newline="") as f:
                    for r in csv.DictReader(f):
                        label = lookup[int(r["row_id"])]
                        partial = evaluate(r["prediction"], label["scoring_points"])[2]
                        detail.append(
                            {
                                "row_id": int(r["row_id"]),
                                "task": label["task_index"],
                                "partial": partial,
                                "strict": int(partial == 1),
                                "seconds": float(r["wall_s"]),
                                "reason_labels": re.findall(
                                    r"The (?:\d+-th|only) predicted root cause reason is ([^\n]+)",
                                    label["scoring_points"],
                                ),
                            }
                        )
            charge = 0.0
            call_count = 0
            if (directory / "usage.jsonl").exists():
                for line in (directory / "usage.jsonl").read_text().splitlines():
                    u = json.loads(line)["models"]
                    charge += dollars(u)
                    call_count += sum(x["calls"] for x in u.values())
            prolog = list(
                (directory / "artifacts/cases").glob("*/prolog/*/result.json")
            )
            prolog_ok = sum(
                json.loads(p.read_text()).get("status") == "ok" for p in prolog
            )
            decisions = [
                json.loads(p.read_text())
                for p in directory.glob("artifacts/cases/*/decision.json")
            ]
            failures = {}
            for decision in decisions:
                for note in decision.get("notes", []):
                    if any(
                        word in note.lower()
                        for word in ("fallback", "stopped", "incomplete")
                    ):
                        failures[note] = failures.get(note, 0) + 1
            summary = {
                "status": "interrupted"
                if interrupted
                else (
                    "complete"
                    if p.returncode == 0 and len(detail) == len(selected)
                    else "failed"
                ),
                "dollars_per_case": charge / max(1, len(selected)),
                "seconds_per_case": wall / max(1, len(selected)),
                "completed_investigations": sum(
                    d.get("investigation_status") == "complete" for d in decisions
                ),
                "provisional_cases": len(
                    list(directory.glob("artifacts/cases/*/provisional.json"))
                ),
                "fallback_cases": sum(
                    any("numerical fallback used" in n for n in d.get("notes", []))
                    for d in decisions
                ),
                "failure_categories": failures,
                "configuration": config,
                "repeat": repeat,
                "split": args.split,
                "returncode": p.returncode,
                "cases": len(detail),
                "expected_cases": len(selected),
                "wall_seconds": round(wall, 3),
                "dollars": charge,
                "model_calls": call_count,
                "prolog_queries": len(prolog),
                "prolog_successes": prolog_ok,
                "strict": sum(x["strict"] for x in detail) / max(1, len(selected)),
                "partial": sum(x["partial"] for x in detail) / max(1, len(selected)),
                "details": detail,
                "implementation_hash": implementation_hash,
                "case_seconds_ceiling": float(env.get("RCA_CASE_SECONDS", "90")),
                "resources": json.loads((directory / "run_summary.json").read_text())
                if (directory / "run_summary.json").exists()
                else None,
                "by_task": {
                    task: {
                        "cases": sum(x["task"] == task for x in detail),
                        "strict": sum(x["strict"] for x in detail if x["task"] == task)
                        / sum(x["task"] == task for x in detail),
                    }
                    for task in sorted({x["task"] for x in detail})
                },
            }
            summaries.append(summary)
            (args.out / "results.json").write_text(json.dumps(summaries, indent=2))
            (args.out / "repeat-statistics.json").write_text(
                json.dumps(repeat_statistics(summaries), indent=2)
            )
            print(
                json.dumps({k: v for k, v in summary.items() if k != "details"}),
                flush=True,
            )
    lines = [
        "# Evaluation results",
        "",
        "Every run starts with its own empty cache. Times include preprocessing.",
        "Split groups overlapping incident windows. Labels are used only by this offline evaluator.",
        "",
        "| Configuration | Repeat | Cases | Strict | Partial | Seconds | Dollars | Prolog successful/attempted |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in summaries:
        lines.append(
            f"| {s['configuration']} | {s['repeat']} | {s['cases']} | {s['strict']:.3f} | {s['partial']:.3f} | {s['wall_seconds']:.1f} | {s['dollars']:.4f} | {s['prolog_successes']}/{s['prolog_queries']} |"
        )
    lines += [
        "",
        "## Repeat statistics",
        "",
        "Statistics use complete runs only; sample SD is unavailable with fewer than two repeats.",
    ]
    for config, stats in repeat_statistics(summaries).items():
        lines.append(
            f"- {config}: {stats['complete_repeats']} complete repeats; "
            + json.dumps(stats["metrics"])
        )
    expected_runs = sum(
        args.repeats if c in ("routed", "fixed") else 1 for c in args.configs
    )
    status = {
        "status": "complete"
        if len(summaries) == expected_runs
        and all(s["status"] == "complete" for s in summaries)
        else "incomplete",
        "expected_runs": expected_runs,
        "recorded_runs": len(summaries),
    }
    (args.out / "comparison-status.json").write_text(json.dumps(status, indent=2))
    lines += [
        "",
        "Small-sample scores are not a claim about hidden-deployment performance. Model confidence is not calibrated.",
        "The supplied heuristic baseline retains its original UTC parsing behavior; the new pipeline uses UTC+8.",
    ]
    (args.out / "REPORT.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
