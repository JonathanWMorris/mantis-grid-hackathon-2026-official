# Magentic neuro-symbolic root cause analysis

Track 1 submission for the MantisGrid Hackathon 2026. The system converts metrics, logs, and traces into source-linked observations, then lets a Microsoft Agent Framework Magentic manager and one investigator inspect evidence and test hypotheses with SWI-Prolog.

The implementation is a research prototype. In the latest comparison with a five-minute case ceiling, routing completed **2/2 investigations with Prolog support**, scoring **50% strict accuracy and 75% partial credit** at **$0.286 per case**. Fixed GLM-5.2 completed neither investigation after model-request timeouts, scoring 50% strict accuracy through fallback. These are two reused development cases with one repeat, not hidden-deployment validation. See [REPORT.md](REPORT.md) for both this experiment and the earlier 90-second comparison.

A [saved completed investigation](eval/completed-case-replay/) includes the diagnosis, evidence, and exact supporting Prolog snapshot. Its component was correct but its failure reason was wrong: successful logic execution does not guarantee a correct causal explanation.

## Pipeline

1. **Represent telemetry.** DuckDB and Parquet support bounded data access. Metric changes, trace relationships, and Drain3 log templates become incident-local observations with source references.
2. **Investigate.** One investigator queries telemetry and paginated Prolog facts, inspects competing explanations, and proposes hypotheses. The Magentic manager coordinates its work.
3. **Check support.** Restricted Prolog executes proposed rules against measured facts. Grounded arithmetic and aggregation are supported. Accepted completion requires a diagnosis linked to successful Prolog bindings and current evidence.
4. **Return an answer.** Predictions, evidence, model usage, and execution traces are saved. Incomplete investigations retain a provisional answer or explicitly labeled numerical fallback. Successful logic execution checks assumptions; it does not establish causality by itself.

## Run the submission

Build from this repository root:

```bash
docker build -t magentic-rca .
mkdir -p out
docker run --rm --cpus 2 --memory 8g \
  -e FEATHERLESS_API_KEY -e FEATHERLESS_BASE_URL \
  -v /absolute/path/to/dataset:/data:ro \
  -v "$PWD/out":/out magentic-rca \
  python run.py --dataset /data --queries /data/query.csv --out /out
```

Set `FEATHERLESS_API_KEY` in your shell. `FEATHERLESS_BASE_URL` is optional and defaults to the permitted Featherless endpoint. Without a key, the runner uses the numerical fallback; this is not a live agent evaluation. The dataset is not included. See the preserved [data instructions](track-1/GET_DATA.md).

For local development, install Python 3.12 and SWI-Prolog, then:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python run.py --dataset /absolute/path/to/dataset \
  --queries /absolute/path/to/dataset/dev/query_dev.csv --out out/dev
.venv/bin/python score.py --predictions out/dev/predictions.csv \
  --queries /absolute/path/to/dataset/dev/query_dev.csv
.venv/bin/python cost.py out/dev/usage.jsonl
```

Use shell environment variables for credentials. The runtime's inherited dotenv lookup is relative to its original location; do not rely on a root `.env` being loaded in this flattened checkout. Never commit credentials.

## Evaluation and artifacts

[REPORT.md](REPORT.md) describes the measurements. [eval/](eval/) contains compact results and experiment manifests. To reproduce the longer-budget comparison with a new output directory:

```bash
.venv/bin/python evaluate_pipeline.py --dataset /absolute/path/to/dataset \
  --out out/comparison-300s --limit 2 --repeats 1 \
  --configs routed fixed --case-seconds 300
```

This runs two development cases under two configurations, with separate cold caches. It is a diagnostic experiment, not hidden-deployment validation. The harness uses `out/development_spend.json` to enforce a shared $10 development cap. Avoid concurrent runs against that ledger or output directory.

The submitted runtime targets 18 minutes total, divided across remaining cases, with a default 90-second case ceiling. It also limits model spending to $1 per case and $20 per run, with 25-second model-request timeouts, bounded tool calls, and manager rounds. Thus a 70-case invocation initially gives each case about 15 seconds. Increasing the experiment's case ceiling does not remove other limits or change submission defaults.

Outputs include `predictions.csv`, `evidence/<row_id>.md`, `usage.jsonl`, and per-case artifacts. `trace.jsonl` indexes sanitized model requests/responses and tool inputs/outputs, with full payloads stored beside it. Prolog artifacts retain the exact facts, rules, query, results, and KB version. Read a trace with `python read_trace.py --help` for the supported options.

## Validation and limitations

The latest readiness checks recorded 46 passing offline tests, output-format validation, and two constrained Linux offline cases. The latest live comparison separately produced seven successful Prolog executions and two completed routed investigations. Offline checks validate processing and packaging; the two-case live result does not establish general accuracy or performance under the judging time limit.

Time limits, model/tool-loop behavior, and evidence browsing can prevent completion. Anomaly ranking can confuse downstream symptoms with causes. Confidence is uncalibrated, trace duration units remain unverified, and bounded log sampling can omit rare clues. PyRCA, DoWhy-GCM, Z3, SCRAM, and a specialist Prolog agent are not implemented.

## AI use and attribution

The participant selected the architecture, research direction, and scope, and reviewed the work. OpenAI Codex generated and debugged implementation, tests, and documentation. Runtime orchestration uses Microsoft Agent Framework Magentic, with Featherless GLM-4.7-Flash, GLM-5.3-Flash, GLM-5.2, and GLM-5.1. The fixed comparison pins GLM-5.2.

The project retains the supplied starter interfaces, scorer, and baseline examples, with integration changes. Representation dependencies include DuckDB, pandas, NumPy, Drain3, and SWI-Prolog. Original competition materials remain under `track-1/` and `track-2/`; their Dockerfiles are renamed as references. The active submission is at this repository root. See [LICENSE](LICENSE), [ATTRIBUTION.md](ATTRIBUTION.md), and [PARTICIPANT_AGREEMENT.md](PARTICIPANT_AGREEMENT.md).
