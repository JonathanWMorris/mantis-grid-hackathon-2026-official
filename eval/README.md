# Evaluation artifacts

- `comparison-90s/`: frozen implementation, two development cases, two repeats each for routed and fixed GLM-5.2. Complete comparison, but zero completed investigations and zero Prolog executions.
- `partial-70-case-run.json`: interrupted full-development run, 47 saved cases, not a full-catalog evaluation.
- `comparison-300s/`: the completed five-minute, one-repeat comparison. Routed completed 2/2 investigations; fixed completed 0/2. `failure-details.json` records underlying request timeouts.
- `completed-case-replay/`: accepted routed diagnosis and exact symbolic support, with an explanation of why the causal diagnosis was still partly wrong.
- `fork-validation.json`: root-image build and constrained offline smoke verification.
- Other result files and `historical-report.md` preserve earlier implementation measurements. They must not be attributed to the latest frozen code. The earlier held-out comparison was interrupted and is not a complete paired benchmark.

The executable harness is at the repository root, `evaluate_pipeline.py`. Use the README command with a fresh output directory. Full raw traces and telemetry remain local and are excluded from version control and the container.
