"""Benchmark harness smoke test (WA-007 acceptance).

Hermetic by design: builds a tiny synthetic ``RawLoans`` table in Arrow and runs
:func:`waspada.bench.harness.run_benchmark` on it with both stacks requested but
the GPU stack skipped (no RAPIDS in CI / the worker container). Validates that
the harness produces a well-structured ``BenchReport`` and that the CPU path
actually executes (features + model) end-to-end on a 1000-row sample.

The GPU path is exercised by the live harness run on the host (see
``bench/LAST_RUN.json``); here it only needs to be *requested* and *reported*
honestly (as ``not_run``) when the launcher is unavailable.
"""
from __future__ import annotations

import datetime as dt
import shutil

import pyarrow as pa
import pytest

from waspada.bench.harness import (
    BENCH_VERSION,
    STAGES,
    run_benchmark,
    validate_report,
)
from waspada.schema import RawLoans, schema_from_dataclass


# -------------------------------------------------------------------------- #
# Fixtures — a tiny synthetic RawLoans table (1000 rows, both label classes).
# -------------------------------------------------------------------------- #
def _raw_rows(n: int = 1000) -> list[dict]:
    import dataclasses as dc
    import random

    rng = random.Random(7)
    statuses = ["Charged Off", "Current", "Fully Paid", "Default",
                "Late (16-30 days)", "In Grace Period"]
    rows = []
    for i in range(n):
        risky = rng.random() < 0.5
        rows.append(dict(
            loan_id=f"LN{i:07d}",
            amount=float(rng.uniform(2000, 30000)),
            term=int(rng.choice([36, 60])),
            rate=float(rng.uniform(5, 25)),
            grade=rng.choice(["A", "B", "C", "D", "E"]),
            annual_income=float(rng.uniform(30000, 120000)),
            dti=float(rng.uniform(2, 35)),
            issue_date=dt.date(rng.choice(range(2018, 2024)), rng.randint(1, 12), 1),
            purpose=rng.choice(["credit_card", "debt_consolidation", "car", "medical"]),
            region=rng.choice(["West", "South", "Midwest", "Northeast"]),
            outstanding_principal=float(rng.uniform(0, 20000)),
            total_paid=float(rng.uniform(0, 25000)),
            current_status=("Charged Off" if risky else "Current"),
        ))
    return rows


@pytest.fixture
def raw_table() -> pa.Table:
    """A 1000-row RawLoans table with both label classes across vintages."""
    rows = _raw_rows(1000)
    cols = {f.name: [] for f in __import__("dataclasses").fields(RawLoans)}
    for r in rows:
        for name in cols:
            cols[name].append(r[name])
    return pa.table(cols, schema=schema_from_dataclass(RawLoans))


@pytest.fixture
def as_of_date() -> dt.date:
    return dt.date(2024, 12, 1)


# -------------------------------------------------------------------------- #
# 1. Structure — STAGES + BENCH_VERSION module-level constants exist.
# -------------------------------------------------------------------------- #
def test_stages_constant_is_the_two_supported_stages():
    """The harness supports exactly the two pipeline stages in a stable order."""
    assert STAGES == ("features", "model")


def test_bench_version_is_a_string():
    """Report versioning so a future schema change is detectable downstream."""
    assert isinstance(BENCH_VERSION, str)
    assert BENCH_VERSION  # non-empty


# -------------------------------------------------------------------------- #
# 2. run_benchmark — runs on a 1000-row sample, produces a valid BenchReport.
# -------------------------------------------------------------------------- #
def test_run_benchmark_produces_valid_report(raw_table, as_of_date):
    """The harness runs on a tiny sample and returns a structurally-valid dict.

    GPU is requested but expected to be ``not_run`` here (no RAPIDS in the
    container). The report must still be well-formed and the CPU numbers real.
    """
    report = run_benchmark(
        row_counts=[1000],
        stages=["features", "model"],
        raw_source=raw_table,
        as_of=as_of_date,
        gpu=True,
    )

    # Top-level report shape.
    assert report["version"] == BENCH_VERSION
    assert report["as_of"] == as_of_date.isoformat()
    assert report["stages"] == ["features", "model"]
    assert isinstance(report["generated_at"], str)
    assert isinstance(report["notes"], list)

    # One result block per requested row count.
    assert len(report["results"]) == 1
    res = report["results"][0]
    assert res["row_count"] == 1000
    assert res["raw_source"] == "<arrow-table>"
    assert "notes" in res and isinstance(res["notes"], list)

    # Per-stage blocks: cpu_s / gpu_s / speedup_x / status present.
    for stage in ("features", "model"):
        block = res["stages"][stage]
        assert "cpu_s" in block and isinstance(block["cpu_s"], (int, float))
        assert block["cpu_s"] >= 0.0
        assert "gpu_s" in block  # may be None when not run
        assert "speedup_x" in block  # may be None
        assert "status" in block
        assert block["status"] in {"ok", "not_run", "failed"}


def test_run_benchmark_cpu_only_when_gpu_disabled(raw_table, as_of_date):
    """With gpu=False the GPU column is never attempted; CPU stages still run."""
    report = run_benchmark(
        row_counts=[500],
        stages=["features"],
        raw_source=raw_table,
        as_of=as_of_date,
        gpu=False,
    )
    res = report["results"][0]
    feat = res["stages"]["features"]
    assert feat["cpu_s"] >= 0.0
    assert feat["gpu_s"] is None
    assert feat["status"] == "ok"  # CPU ran and produced output


# -------------------------------------------------------------------------- #
# 3. Honest reporting — unavailable GPU is reported, not faked.
# -------------------------------------------------------------------------- #
def test_gpu_unavailable_is_reported_not_run(raw_table, as_of_date):
    """If there's no WSL launcher, the GPU column is absent (never fabricated).

    ``status`` reflects whether the *stage* ran (it did, on CPU); the GPU-not-run
    signal is ``gpu_s is None`` + ``speedup_x is None`` + an explicit note — the
    headline number is honestly CPU-only.
    """
    if shutil.which("wsl") is not None:
        pytest.skip("WSL launcher present — GPU path would actually run here.")
    report = run_benchmark(
        row_counts=[500],
        stages=["features"],
        raw_source=raw_table,
        as_of=as_of_date,
        gpu=True,
    )
    feat = report["results"][0]["stages"]["features"]
    assert feat["gpu_s"] is None          # no fabricated GPU time
    assert feat["speedup_x"] is None      # no fabricated speedup
    assert feat["status"] == "ok"         # the CPU reference ran
    # And the report says *why* the GPU column is empty, in plain text.
    joined = " ".join(feat["notes"])
    assert "unavailable" in joined.lower() or "not run" in joined.lower()


def test_validate_report_rejects_fabricated_gpu_time():
    """A report claiming a GPU time without a GPU run must fail validation."""
    bad = {
        "version": BENCH_VERSION,
        "generated_at": "2026-07-07T00:00:00Z",
        "as_of": "2024-12-01",
        "stages": ["features"],
        "results": [{
            "row_count": 100,
            "raw_source": "<arrow-table>",
            "notes": [],
            "stages": {
                "features": {
                    "cpu_s": 1.0,
                    "gpu_s": 0.5,          # claimed but status says not_run
                    "speedup_x": 2.0,
                    "status": "not_run",   # inconsistency — must fail
                },
            },
        }],
        "notes": [],
    }
    with pytest.raises(ValueError):
        validate_report(bad)


# -------------------------------------------------------------------------- #
# 4. CPU stages actually execute — features+model ran on the sample.
# -------------------------------------------------------------------------- #
def test_cpu_features_stage_ran_for_real(raw_table, as_of_date):
    """The CPU features timing reflects a real build_features() run (> 0 ms)."""
    report = run_benchmark(
        row_counts=[1000],
        stages=["features", "model"],
        raw_source=raw_table,
        as_of=as_of_date,
        gpu=False,
    )
    feat = report["results"][0]["stages"]["features"]
    mdl = report["results"][0]["stages"]["model"]
    assert feat["status"] == "ok" and feat["cpu_s"] > 0.0
    assert mdl["status"] == "ok" and mdl["cpu_s"] > 0.0
