"""Bounded local telemetry queries; source-preserving observations, never labels."""

from __future__ import annotations

import json
import math
import re
import time
import threading
from pathlib import Path

import duckdb
import numpy as np

from .schema import VERSION, Case, family, ident, reason_for


def sql_string(value):
    return "'" + str(value).replace("'", "''") + "'"


class BoundedConnection:
    """Interrupt native SQL work too; asyncio alone cannot cancel a CSV scan."""

    def __init__(self, connection, store):
        self.connection = connection
        self.store = store

    def execute(self, *args, **kwargs):
        self.store.check()
        seconds = self.store.deadline - time.monotonic()
        timer = None
        if math.isfinite(seconds):
            timer = threading.Timer(max(0.001, seconds), self.connection.interrupt)
            timer.daemon = True
            timer.start()
        try:
            return self.connection.execute(*args, **kwargs)
        finally:
            if timer:
                timer.cancel()


class Evidence:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.items = {}

    def add(self, item: dict):
        item = json.loads(json.dumps(item, default=str))
        key = "ev_" + ident(item)
        item["id"] = key
        self.items[key] = item
        target = self.directory / f"{key}.json"
        if not target.exists():
            target.write_text(json.dumps(item, indent=2, allow_nan=False))
        return item

    def get(self, key):
        if not re.fullmatch(r"ev_[0-9a-f]{20}", key):
            raise ValueError("Invalid evidence ID")
        if key not in self.items:
            p = self.directory / f"{key}.json"
            if not p.exists():
                return {"error": "Evidence not found"}
            self.items[key] = json.loads(p.read_text())
        return self.items[key]


class Store:
    def __init__(self, dataset: Path, out: Path):
        self.dataset = dataset.resolve()
        files = sorted(
            (str(p.relative_to(dataset)), p.stat().st_size, p.stat().st_mtime_ns)
            for p in dataset.glob("telemetry/*/*/*.csv")
        )
        self.key = ident([str(self.dataset), files, VERSION])
        self.cache = out / "cache" / self.key
        self.cache.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect()
        self.db.execute("SET threads=2")
        self.db.execute("SET memory_limit='2GB'")
        self.db.execute(f"SET temp_directory={sql_string(self.cache / 'spill')}")
        self.evidence = Evidence(out / "artifacts" / "evidence")
        self.prepared = {}
        self.features = {}
        self.topology = {}
        self.deadline = float("inf")
        self.limitations = []
        self.db = BoundedConnection(self.db, self)

    def check(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Analysis deadline reached")

    def source(self, day: str, name: str):
        self.check()
        key = (day, name)
        if key in self.prepared:
            return self.prepared[key]
        category = (
            "metric"
            if name.startswith("metric")
            else "trace"
            if name.startswith("trace")
            else "log"
        )
        src = self.dataset / "telemetry" / day / category / f"{name}.csv"
        if not src.exists():
            return None
        dest = self.cache / f"{day}_{name}.parquet"
        if not dest.exists():
            tmp = dest.with_suffix(".partial")
            if tmp.exists():
                tmp.unlink()
            # DuckDB handles quoted commas and embedded newlines. All-varchar avoids
            # schema guesses that conflate status '0' with 'Ok' or drop identifiers.
            self.db.execute(
                f"COPY (SELECT * FROM read_csv({sql_string(src)}, header=true, all_varchar=true)) "
                f"TO {sql_string(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            tmp.replace(dest)
        self.prepared[key] = dest
        return dest

    def frame(self, case: Case, name: str, baseline=True):
        paths = [self.source(day, name) for day in case.days]
        paths = [str(p) for p in paths if p]
        if not paths:
            import pandas as pd

            return pd.DataFrame()
        scale = 1 if name == "trace_span" else 1000
        lo = case.start - 1800000 if baseline else case.start
        return self.db.execute(
            "SELECT *, TRY_CAST(timestamp AS BIGINT) * ? AS ts FROM read_parquet(?) "
            "WHERE TRY_CAST(timestamp AS BIGINT) * ? >= ? AND TRY_CAST(timestamp AS BIGINT) * ? < ?",
            [scale, paths, scale, lo, scale, case.end],
        ).df()

    def metrics(self, case: Case, sources=None):
        names = sources or ["metric_container", "metric_node", "metric_service"]
        results = []
        for name in names:
            key = (case.key, name)
            if key not in self.features:
                saved = self.cache / f"features_{case.key}_{name}.json"
                if saved.exists():
                    self.features[key] = json.loads(saved.read_text())
                    for e in self.features[key]:
                        self.evidence.items[e["id"]] = e
                else:
                    try:
                        self.features[key] = self._metrics(case, name)
                        saved.write_text(json.dumps(self.features[key]))
                    except (TimeoutError, duckdb.Error) as exc:
                        self.features.setdefault(key, [])
                        self.limitations.append(
                            f"{name}: incomplete preparation ({type(exc).__name__})"
                        )
            results.extend(self.features[key])
        return sorted(results, key=lambda x: x["score"], reverse=True)

    def _metrics(self, case, name):
        answer = []
        self.features[(case.key, name)] = answer
        self.check()
        df = self.frame(case, name)
        if df.empty:
            return []
        if name == "metric_service":
            df = df.melt(
                id_vars=["service", "ts"],
                value_vars=["rr", "sr", "mrt", "count"],
                var_name="kpi_name",
                value_name="value",
            ).rename(columns={"service": "cmdb_id"})
        import pandas as pd

        df["value"] = pd.to_numeric(df.value, errors="coerce")
        df = df.dropna(subset=["value", "ts"])
        for (raw_entity, kpi), group in df.groupby(["cmdb_id", "kpi_name"], sort=False):
            self.check()
            group = group.sort_values("ts").drop_duplicates("ts", keep="last")
            ts = group.ts.to_numpy(dtype=np.int64)
            vals = group.value.to_numpy(dtype=float)
            entity = raw_entity
            if name == "metric_container" and "." in raw_entity:
                node, entity = raw_entity.split(".", 1)
                self.topology[(case.key, entity)] = node
            semantics = "exported_value"
            # Infer cumulative exports only when a named counter is overwhelmingly
            # monotone. Preserve this assumption in evidence; never silently derive twice.
            counter = any(
                x in kpi
                for x in (
                    "cpu_usage_seconds",
                    "cpu_user_seconds",
                    "cpu_system_seconds",
                    "network_receive_bytes",
                    "network_transmit_bytes",
                    "fs_reads_MB",
                    "fs_writes_MB",
                )
            )
            if (
                counter
                and len(vals) > 10
                and np.mean(np.diff(vals) >= 0) > 0.98
                and np.ptp(vals) > 0
            ):
                delta = np.diff(vals)
                dt = np.diff(ts) / 1000
                vals = np.where(delta >= 0, delta / np.maximum(dt, 0.001), np.nan)
                ts = ts[1:]
                semantics = "rate_per_second_inferred_from_monotonic_export"
            valid = np.isfinite(vals)
            vals, ts = vals[valid], ts[valid]
            inside = (ts >= case.start) & (ts < case.end)
            before = ts < case.start
            if inside.sum() == 0:
                continue
            baseline_note = "preceding_30_minutes"
            base = vals[before]
            if len(base) < 5:
                # No invented healthy baseline: expose insufficient coverage.
                baseline_note = "insufficient_preceding_samples"
            iv, it = vals[inside], ts[inside]
            med = float(np.median(base)) if len(base) else None
            mad = float(np.median(np.abs(base - med))) if len(base) else None
            # A one-unit event on an otherwise zero counter must not dwarf every
            # sustained resource anomaly due solely to division by epsilon.
            scale = max(
                1.4826 * (mad or 0), abs(med or 0) * 0.05, 1.0 if mad == 0 else 1e-9
            )
            z = (
                np.abs(iv - med) / scale
                if med is not None and len(base) >= 5
                else np.zeros(len(iv))
            )
            mask = z >= 4
            ix = int(np.argmax(z))
            first = int(np.flatnonzero(mask)[0]) if mask.any() else ix
            cadence = int(np.median(np.diff(ts))) if len(ts) > 1 else 60000
            if not math.isfinite(cadence) or cadence <= 0:
                cadence = 60000
            ev = self.evidence.add(
                {
                    "kind": "metric",
                    "status": "derived",
                    "case": case.key,
                    "entity": entity,
                    "raw_entity": raw_entity,
                    "source": name,
                    "files": [
                        f"telemetry/{d}/metric/{name}.csv"
                        for d in case.days
                        if (
                            self.dataset / "telemetry" / d / "metric" / f"{name}.csv"
                        ).exists()
                    ],
                    "signal": kpi,
                    "family": family(kpi),
                    "semantics": semantics,
                    "window": [case.start, case.end],
                    "baseline_window": [case.start - 1800000, case.start],
                    "baseline_method": baseline_note,
                    "baseline_median": med,
                    "baseline_mad": mad,
                    "baseline_samples": len(base),
                    "samples": len(iv),
                    "expected_samples": max(
                        1, round((case.end - case.start) / cadence)
                    ),
                    "incident_median": float(np.median(iv)),
                    "min": float(iv.min()),
                    "max": float(iv.max()),
                    "peak_value": float(iv[ix]),
                    "score": round(min(float(z[ix]), 1e8), 3),
                    "direction": "increase"
                    if med is not None and iv[ix] >= med
                    else "decrease",
                    "anomalous_samples": int(mask.sum()),
                    "onset": [
                        max(case.start, int(it[first]) - cadence),
                        int(it[first]),
                    ],
                    "method": VERSION
                    + ":median_mad_floor_5pct_zero_mad_floor1_threshold4",
                }
            )
            answer.append(ev)
        return answer

    def entities(self, case):
        observations = self.metrics(case)
        entities = sorted(
            set(
                x["entity"]
                for x in observations
                if x["source"] in ("metric_container", "metric_node")
            )
        )
        # Reconstruct topology also on a cache hit.
        hosts = {}
        for x in observations:
            if x["source"] == "metric_container" and "." in x["raw_entity"]:
                hosts[x["entity"]] = x["raw_entity"].split(".", 1)[0]
        return entities, hosts

    def candidates(self, case):
        observations = self.metrics(case)
        ranked = []
        seen = set()
        for x in observations:
            if (
                x["source"] not in ("metric_container", "metric_node")
                or x["family"] == "other"
            ):
                continue
            if any(
                k in x["signal"].lower()
                for k in ("spec_", "limit", "capacity", "totalphysical")
            ):
                continue
            key = (x["entity"], x["family"], x["onset"][1] // 120000)
            if key in seen:
                continue
            seen.add(key)
            ranked.append(
                {
                    "component": x["entity"],
                    "reason": reason_for(
                        x["entity"], x["family"], 0 < x["anomalous_samples"] <= 2
                    ),
                    "onset": sum(x["onset"]) // 2,
                    "score": x["score"],
                    "evidence_ids": [x["id"]],
                    "family": x["family"],
                }
            )
        return ranked

    def traces(self, case, component="", limit=15):
        df = self.frame(case, "trace_span")
        if df.empty:
            return {"status": "unavailable", "reason": "No trace source"}
        import pandas as pd

        df["duration"] = pd.to_numeric(df.duration, errors="coerce")
        keys = df[["trace_id", "span_id", "cmdb_id", "ts"]].drop_duplicates(
            ["trace_id", "span_id"]
        )
        keys = keys.rename(
            columns={
                "span_id": "parent_span",
                "cmdb_id": "parent_entity",
                "ts": "parent_ts",
            }
        )
        joined = df.merge(keys, on=["trace_id", "parent_span"], how="left")
        joined["start_gap_ms"] = joined.ts - joined.parent_ts
        if component:
            joined = joined[
                (joined.cmdb_id == component) | (joined.parent_entity == component)
            ]
        records = []
        for (entity, op), g in joined.groupby(["cmdb_id", "operation_name"]):
            before = g[g.ts < case.start]
            during = g[g.ts >= case.start]
            if during.empty:
                continue

            def quant(frame, col):
                v = frame[col].dropna()
                return float(v.quantile(0.95)) if len(v) else None

            b, v = quant(before, "duration"), quant(during, "duration")
            item = self.evidence.add(
                {
                    "kind": "trace",
                    "status": "derived",
                    "case": case.key,
                    "entity": entity,
                    "operation": op,
                    "source": "trace_span",
                    "window": [case.start, case.end],
                    "baseline_window": [case.start - 1800000, case.start],
                    "files": [f"telemetry/{d}/trace/trace_span.csv" for d in case.days],
                    "samples": len(during),
                    "baseline_samples": len(before),
                    "p95_duration_raw": v,
                    "baseline_p95_duration_raw": b,
                    "duration_unit": "unverified_export_unit",
                    "p95_parent_start_gap_ms": quant(during, "start_gap_ms"),
                    "missing_parent_fraction": float(
                        during.parent_entity.isna().mean()
                    ),
                    "status_counts": {
                        str(k): int(n)
                        for k, n in during.status_code.value_counts().items()
                    },
                    "parents": sorted(set(during.parent_entity.dropna())),
                    "examples": during.nlargest(3, "duration")[
                        ["trace_id", "span_id", "parent_span", "ts", "duration"]
                    ].to_dict("records"),
                    "ratio": v / max(b, 1e-9)
                    if b is not None and v is not None
                    else None,
                    "limitation": "Start gaps include application work, queueing and clock effects; not pure network latency.",
                }
            )
            records.append(item)
        records.sort(key=lambda x: x["ratio"] or 0, reverse=True)
        return {
            "observations": records[:limit],
            "total_groups": len(records),
            "truncated": len(records) > limit,
        }

    def logs(self, case, component="", source="log_service", limit=12):
        if source not in ("log_service", "log_proxy"):
            raise ValueError("Unknown log source")
        paths = [str(p) for d in case.days if (p := self.source(d, source))]
        if not paths:
            return {"status": "unavailable", "reason": "No log source"}
        # Aggregate exact messages in SQL before bringing bounded examples into Python.
        rows = self.db.execute(
            """SELECT cmdb_id, value, count(*) FILTER (WHERE CAST(timestamp AS BIGINT)*1000 < ?) AS before_n,
            count(*) FILTER (WHERE CAST(timestamp AS BIGINT)*1000 >= ?) AS during_n,
            min(log_id) AS example_id FROM read_parquet(?)
            WHERE CAST(timestamp AS BIGINT)*1000 >= ? AND CAST(timestamp AS BIGINT)*1000 < ?
            AND (? = '' OR cmdb_id = ?) GROUP BY cmdb_id, value
            ORDER BY during_n DESC LIMIT 4000""",
            [
                case.start,
                case.start,
                paths,
                case.start - 1800000,
                case.end,
                component,
                component,
            ],
        ).fetchall()
        from drain3 import TemplateMiner
        from drain3.template_miner_config import TemplateMinerConfig

        miner = TemplateMiner(config=TemplateMinerConfig())
        groups = {}
        for entity, message, b, n, example in rows:
            if source == "log_proxy":
                m = re.match(r'"([^\"]+)"\s+(\d+)\s+(\S+)', message)
                template = f"{m[1]} status={m[2]} flags={m[3]}" if m else message[:250]
            else:
                template = miner.add_log_message(message)["template_mined"]
            k = (entity, template)
            g = groups.setdefault(k, {"before": 0, "during": 0, "examples": []})
            g["before"] += b
            g["during"] += n
            if len(g["examples"]) < 2:
                g["examples"].append({"log_id": example, "value": message[:1500]})
        observations = []
        for (entity, template), g in groups.items():
            ratio = (g["during"] / max(1, (case.end - case.start) / 1800000)) / max(
                1, g["before"]
            )
            observations.append(
                self.evidence.add(
                    {
                        "kind": "log",
                        "status": "derived",
                        "case": case.key,
                        "entity": entity,
                        "source": source,
                        "template": template,
                        "window": [case.start, case.end],
                        "baseline_window": [case.start - 1800000, case.start],
                        "count": g["during"],
                        "baseline_count": g["before"],
                        "ratio": ratio,
                        "examples": g["examples"],
                        "method": "bounded_message_aggregation_then_template",
                        "files": [f"telemetry/{d}/log/{source}.csv" for d in case.days],
                        "limitation": "At most 4000 distinct messages sampled by incident frequency; counts may be partial.",
                    }
                )
            )
        observations.sort(
            key=lambda x: (
                bool(
                    re.search(
                        "error|fail|timeout|reset|refused|killed", x["template"], re.I
                    )
                ),
                x["ratio"],
            ),
            reverse=True,
        )
        return {
            "observations": observations[:limit],
            "total_templates": len(observations),
            "truncated": len(observations) > limit or len(rows) == 4000,
        }
