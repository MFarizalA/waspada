"""Run the WA-007 benchmark on the real 1M-row portfolio and persist the report.

CPU-only here (no WSL/RAPIDS in the worker container); the GPU column is
requested, found unavailable, and recorded as not_run. On the host/WSL the same
call produces live GPU numbers with no code change.
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from waspada.bench.harness import run_benchmark, save_last_run

AS_OF = dt.date(2024, 12, 1)
RAW = "data/loans.parquet"

print("=== WA-007 benchmark: 100k + 1M rows, CPU + GPU(requested) ===")
report = run_benchmark(
    row_counts=[100_000, 1_000_000],
    stages=["features", "model"],
    raw_source=RAW,
    as_of=AS_OF,
    gpu=True,  # will be not_run in-container; live on host/WSL
)

# Pretty-print a compact summary to stdout.
print(f"\nversion: {report['version']}  generated_at: {report['generated_at']}")
print(f"as_of: {report['as_of']}  stages: {report['stages']}")
for res in report["results"]:
    print(f"\n--- row_count: {res['row_count']:,} ---")
    for stage, b in res["stages"].items():
        cpu = f"{b['cpu_s']:.3f}s" if b['cpu_s'] is not None else "—"
        gpu = f"{b['gpu_s']:.3f}s" if b['gpu_s'] is not None else "not_run"
        sp = f"{b['speedup_x']}x" if b['speedup_x'] is not None else "—"
        print(f"  {stage:9s} cpu={cpu:10s} gpu={gpu:10s} speedup={sp:8s} status={b['status']}")

p = save_last_run(report, "bench/LAST_RUN.json")
print(f"\nwrote {p} ({p.stat().st_size} bytes)")
