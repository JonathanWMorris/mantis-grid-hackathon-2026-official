# Evaluation results

Every run starts with its own empty cache. Times include preprocessing.
Split groups overlapping incident windows. Labels are used only by this offline evaluator.

| Configuration | Repeat | Cases | Strict | Partial | Seconds | Dollars | Prolog successful/attempted |
|---|---:|---:|---:|---:|---:|---:|---:|
| routed | 0 | 2 | 0.500 | 0.500 | 178.6 | 0.1459 | 0/0 |
| routed | 1 | 2 | 0.500 | 0.500 | 180.4 | 0.1992 | 0/0 |
| fixed | 0 | 2 | 0.500 | 0.500 | 180.4 | 0.5688 | 0/0 |
| fixed | 1 | 2 | 0.500 | 0.500 | 180.1 | 0.2997 | 0/0 |

## Repeat statistics

Statistics use complete runs only; sample SD is unavailable with fewer than two repeats.
- fixed: 2 complete repeats; {"strict": {"mean": 0.5, "sample_sd": 0.0}, "partial": {"mean": 0.5, "sample_sd": 0.0}, "dollars_per_case": {"mean": 0.21710609999999997, "sample_sd": 0.09514857131917431}, "seconds_per_case": {"mean": 90.12279103125002, "sample_sd": 0.07569311477936878}}
- routed: 2 complete repeats; {"strict": {"mean": 0.5, "sample_sd": 0.0}, "partial": {"mean": 0.5, "sample_sd": 0.0}, "dollars_per_case": {"mean": 0.08627115875, "sample_sd": 0.01885949068063287}, "seconds_per_case": {"mean": 89.75641587500013, "sample_sd": 0.6341955867643507}}

Small-sample scores are not a claim about hidden-deployment performance. Model confidence is not calibrated.
The supplied heuristic baseline retains its original UTC parsing behavior; the new pipeline uses UTC+8.
