from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

from .schema import ident


def atom(text):
    return (
        "'"
        + str(text).replace("\\", "\\\\").replace("'", "''").replace("\n", "\\n")
        + "'"
    )


def facts_for(store, case):
    facts = []
    entities, hosts = store.entities(case)
    for pod, node in sorted(hosts.items()):
        facts.append(f"hosted_on({atom(pod)},{atom(node)}).")
    for e in store.evidence.items.values():
        if e.get("case") != case.key:
            continue
        if e["kind"] == "metric":
            facts.append(
                f"observation({atom(e['id'])},{atom(e['entity'])},{atom(e['family'])},"
                f"{atom(e['direction'])},{e['score']},{e['onset'][0]},{e['onset'][1]},"
                f"{atom(e['signal'])})."
            )
            if (
                e["score"] < 2
                and e["baseline_samples"] >= 10
                and e["samples"] >= 0.8 * e["expected_samples"]
            ):
                facts.append(
                    f"normal({atom(e['entity'])},{atom(e['family'])},{atom(e['id'])})."
                )
        elif e["kind"] == "trace":
            for parent in e["parents"]:
                facts.append(f"calls({atom(parent)},{atom(e['entity'])}).")
            facts.append(
                f"trace_change({atom(e['entity'])},{e['ratio'] or 0},{atom(e['id'])})."
            )
        elif e["kind"] == "log":
            facts.append(
                f"log_pattern({atom(e['entity'])},{atom(e['template'])},{atom(e['id'])})."
            )
    return "\n".join(sorted(set(facts)))


PROLOG_DIR = Path(__file__).resolve().parents[1] / "prolog"


def kb_snapshot(store, case):
    facts = facts_for(store, case)
    rules = (PROLOG_DIR / "trusted.pl").read_text()
    engine = (PROLOG_DIR / "base.pl").read_text()
    return {"kb_version": ident([facts, rules, engine]), "facts": facts, "rules": rules}


def inspect_kb(store, case, predicate="", component="", offset=0, limit=100):
    if offset < 0 or not 1 <= limit <= 250:
        raise ValueError("offset must be nonnegative; limit must be 1..250")
    snap = kb_snapshot(store, case)
    rows = [
        line
        for line in snap["facts"].splitlines()
        if (not predicate or line.startswith(predicate + "("))
        and (not component or atom(component) in line)
    ]
    return {
        "kb_version": snap["kb_version"],
        "facts": rows[offset : offset + limit],
        "matching_facts": len(rows),
        "offset": offset,
        "next_offset": offset + limit if offset + limit < len(rows) else None,
        "trusted_rules": snap["rules"],
    }


def diagnostic(error):
    error = str(error)
    code = "prolog_error"
    for marker, category in [
        ("wrong_arity", "wrong_arity"),
        ("syntax_error", "syntax_error"),
        ("instantiation_error", "ungrounded_arithmetic_or_argument"),
        ("existence_error", "unknown_predicate"),
        ("limit", "execution_or_result_limit"),
        ("disallowed_rule", "unsupported_rule"),
        ("unsupported_goal", "unsupported_goal_or_unquoted_atom"),
    ]:
        if marker in error:
            code = category
            break
    return {
        "code": code,
        "message": error,
        "expected_observation": "observation(Evidence,Entity,Family,Direction,Score,OnsetStartMs,OnsetEndMs,OriginalSignal)",
        "repair": "Change only the identified defect; quote component atoms, preserve measured facts, and consult inspect_kb for exact predicates.",
    }


def run_prolog(store, case, directory: Path, query: str, rules: list[str]):
    if len(query) > 8000 or len(rules) > 15 or sum(map(len, rules)) > 20000:
        return {"status": "error", "error": "Query/rule size limit exceeded"}
    query = query.strip()
    if not query.endswith("."):
        query += "\n."
    snap = kb_snapshot(store, case)
    key = uuid.uuid4().hex
    directory = directory / "prolog" / key
    directory.mkdir(parents=True, exist_ok=True)
    facts = directory / "facts.pl"
    request = directory / "request.json"
    facts.write_text(snap["facts"])
    (directory / "trusted.pl").write_text(snap["rules"])
    (directory / "base.pl").write_text((PROLOG_DIR / "base.pl").read_text())
    request.write_text(
        json.dumps({"query": query, "rules": rules, "kb_version": snap["kb_version"]})
    )
    try:
        p = subprocess.run(
            [
                "swipl",
                "--stack-limit=128m",
                "-q",
                "-s",
                str((directory / "base.pl").resolve()),
                "--",
                str(facts.resolve()),
                str(request.resolve()),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=directory,
        )
        (directory / "stdout.txt").write_text(p.stdout)
        (directory / "stderr.txt").write_text(p.stderr)
        result = (
            json.loads(p.stdout)
            if p.returncode == 0
            else {"status": "error", "error": p.stderr}
        )
        result["returncode"] = p.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        result = {"status": "error", "error": type(e).__name__ + ": " + str(e)}
    if "bindings" in result:
        result["truncated"] = len(result["bindings"]) > 100
        result["bindings"] = result["bindings"][:100]
    result.update(
        execution_id=key,
        kb_version=snap["kb_version"],
        proposed_assumptions=rules,
        meaning="Logical result conditional on supplied observations and rules, not causal proof.",
    )
    if result.get("status") != "ok":
        result["diagnostic"] = diagnostic(result.get("error", "Unknown failure"))
        result["repair_context"] = {
            "query": query,
            "rules": rules,
            "kb": inspect_kb(store, case, limit=5),
        }
    (directory / "result.json").write_text(json.dumps(result, indent=2))
    return result
