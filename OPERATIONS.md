# Operations

The active submission is at the repository root. See [README.md](README.md) for local and Docker commands. The original `track-1/Makefile` belongs to the preserved scaffold and does not target this root implementation.

Use a fresh output directory for each evaluation. Do not launch concurrent paid evaluations against the shared spending ledger. Keep API credentials in the environment, and keep datasets, caches, and full traces outside version control.

For an offline smoke check, set `RCA_OFFLINE=1` and pass `--limit 2` to `run.py`. This validates preprocessing and output production, not live reasoning. Run offline tests with `.venv/bin/python -m pytest -q` after installing `requirements-dev.txt` and SWI-Prolog.

The default runtime divides an 18-minute target across remaining cases and caps each case at 90 seconds. `RCA_CASE_SECONDS` changes the case ceiling but cannot override the remaining total-time allocation. The evaluation harness sets this variable from `--case-seconds`. Model spending also has $1-per-case and $20-per-run limits. A configured `RCA_BUDGET_FILE` and `RCA_DEV_BUDGET` impose an additional cumulative development cap.

Inspect each case's `decision.json` for completion status and fallback notes. Inspect `trace.jsonl` and its payload files for model/tool behavior. Prolog execution snapshots are saved under each case's `prolog/` directory. A completed process or a correct prediction does not imply a completed symbolic investigation.
