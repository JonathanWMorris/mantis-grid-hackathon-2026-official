# Evaluation results

Every run starts with its own empty cache. Times include preprocessing.
Split groups overlapping incident windows. Labels are used only by this offline evaluator.

| Configuration | Repeat | Cases | Strict | Partial | Seconds | Dollars | Prolog successful/attempted |
|---|---:|---:|---:|---:|---:|---:|---:|
| routed | 0 | 2 | 0.500 | 0.750 | 294.0 | 0.5720 | 7/7 |
| fixed | 0 | 2 | 0.500 | 0.500 | 167.7 | 0.4083 | 11/11 |

## Repeat statistics

Statistics use complete runs only; sample SD is unavailable with fewer than two repeats.
- fixed: 1 complete repeats; {"strict": {"mean": 0.5, "sample_sd": null}, "partial": {"mean": 0.5, "sample_sd": null}, "dollars_per_case": {"mean": 0.2041713, "sample_sd": null}, "seconds_per_case": {"mean": 83.85735712499991, "sample_sd": null}}
- routed: 1 complete repeats; {"strict": {"mean": 0.5, "sample_sd": null}, "partial": {"mean": 0.75, "sample_sd": null}, "dollars_per_case": {"mean": 0.285998095, "sample_sd": null}, "seconds_per_case": {"mean": 146.99058862499987, "sample_sd": null}}

Small-sample scores are not a claim about hidden-deployment performance. Model confidence is not calibrated.
The supplied heuristic baseline retains its original UTC parsing behavior; the new pipeline uses UTC+8.
