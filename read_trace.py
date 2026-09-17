"""Read local trace events; --full includes complete sanitized payloads."""

import argparse
import json
from pathlib import Path

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("case_directory", type=Path)
    p.add_argument("--full", action="store_true")
    args = p.parse_args()
    for line in (args.case_directory / "trace.jsonl").read_text().splitlines():
        e = json.loads(line)
        if e["kind"] == "provider_chunk" and not args.full:
            continue
        print(json.dumps(e, ensure_ascii=False))
        if args.full and e.get("payload"):
            print((args.case_directory / e["payload"]).read_text())
