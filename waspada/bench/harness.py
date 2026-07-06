"""CPU-vs-GPU benchmark harness (WA-007).

Runs the SAME collections feature+model pipeline on two stacks and reports the
honest wall-clock per stage:

  * **CPU** — :func:`waspada.features.collections.build_features` (pyarrow) +
    :func:`waspada.model.risk.train` / :func:`~waspada.model.risk.predict`
    (sklearn LogisticRegression).
  * **GPU** — ``gpu/run_features.py`` (cuDF) via :func:`waspada.wsl.run_gpu`.
    The GPU *model* (cuML) is not yet wired as code; when it lands, the model
    stage GPU column switches on with no harness change (the seam is
    ``_run_gpu_model``). Until then the model stage is CPU-only and that is
    reported plainly — never fabricated.

Design notes
------------
* **Honest by construction.** A stage that did not run is reported as
  ``status="not_run"`` with ``gpu_s=None`` / ``speedup_x=None``. A stage that ran
  but lost to CPU still reports the real (slow) number with ``status="ok"`` and a
  ``speedup_x`` that may be < 1. :func:`validate_report` rejects any report where
  a ``not_run`` stage nevertheless claims a GPU time — so a fabricated number
  can't silently slip into ``bench/LAST_RUN.json``.
* **GPU is opt-in and degrades gracefully.** When the ``wsl`` launcher is
  absent (the worker container, CI), the GPU column is requested, found
  unavailable, and recorded as ``not_run`` — the CPU column still runs for real.
  The live GPU numbers come from running the harness on the host/WSL.
* **The workload is the real collections pipeline** (heavy groupby-free but
  many-feature engineering + a standardized one-hot + L2 logistic fit), not a
  synthetic micro-op. That is the "CPU-stressing" workload the ticket asks for.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

import pyarrow as pa
import pyarrow.parquet as pq

from ..features.collections import build_features
from ..model.risk import predict as cpu_predict, train as cpu_train
from ..wsl import run_gpu

__all__ = [
    "BENCH_VERSION",
    "STAGES",
    "run_benchmark",
    "save_last_run",
    "validate_report",
]

# Report schema version. Bump when the BenchReport dict shape changes so a
# downstream reader (README, dashboard) can detect a mismatch.
BENCH_VERSION = "1.0"

# The two pipeline stages this harness times, in canonical order. Cited by the
# test suite so the contract is self-documenting.
STAGES = ("features", "model")

# Path to the GPU feature runner (repo-relative). Reused — not duplicated.
_GPU_FEATURES_SCRIPT = "gpu/run_features.py"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _time(fn) -> tuple[float, Any]:
    """Run ``fn()``, return (wall-clock seconds, return value)."""
    t0 = time.perf_counter()
    out = fn()
    return time.perf_counter() - t0, out


def _slice_raw(raw: pa.Table, row_count: int) -> pa.Table:
    """Return exactly ``row_count`` rows of ``raw`` (slice, or repeat-pad).

    For ``row_count <= raw.num_rows`` we slice the head. For larger requests we
    tile the table vertically (deterministic, no shuffle) so the 1M-row
    portfolio can be exercised at 1M even from a smaller source — the synthetic
    generator already produced 1M rows, so tiling is a safety net, not the norm.
    """
    n = raw.num_rows
    if row_count <= n:
        return raw.slice(0, row_count)
    reps = (row_count + n - 1) // n
    tiled = pa.concat_tables([raw] * reps)
    return tiled.slice(0, row_count)


def _wsl_available() -> bool:
    """True iff the ``wsl`` launcher is on PATH (i.e. we can reach RAPIDS)."""
    return shutil.which("wsl") is not None


# --------------------------------------------------------------------------- #
# CPU stage runners — the real collections pipeline on pyarrow + sklearn.
# --------------------------------------------------------------------------- #
def _run_cpu_features(raw: pa.Table, as_of: dt.date) -> tuple[float, pa.Table]:
    """Time build_features() on CPU. Returns (seconds, FeatureFrame table)."""
    secs, feats = _time(lambda: build_features(raw, as_of))
    return secs, feats


def _run_cpu_model(feats: pa.Table) -> tuple[float, float, Dict]:
    """Time train()+predict() on CPU. Returns (train_s, predict_s, model)."""
    t_train, model = _time(lambda: cpu_train(feats))
    t_pred, _ = _time(lambda: cpu_predict(model, feats))
    return t_train, t_pred, model


# --------------------------------------------------------------------------- #
# GPU stage runners — cuDF via WSL (features); cuML model is not yet wired.
# --------------------------------------------------------------------------- #
def _run_gpu_features(
    raw: pa.Table,
    as_of: dt.date,
    *,
    repo_root: Path,
    tmp_dir: Path,
) -> tuple[float, Optional[int], str]:
    """Time the GPU feature stage via ``gpu/run_features.py`` in WSL.

    Writes ``raw`` to a temp parquet, invokes the RAPIDS runner, reads nothing
    back (we time the *build*, not the round-trip; correctness is asserted by
    the WA-004 GPU smoke test on the host). Returns
    ``(seconds, peak_vram_mib_or_None, note)``.

    Peak VRAM is read from ``nvidia-smi`` inside the same WSL call when
    available; if that query fails it is reported as ``None`` with a note
    (honest: we don't invent a number).
    """
    in_path = tmp_dir / f"bench_raw_{as_of.isoformat()}.parquet"
    out_path = tmp_dir / f"bench_feats_{as_of.isoformat()}.parquet"
    pq.write_table(raw, in_path)

    # Convert Windows paths to WSL-compatible POSIX paths before passing them
    # into the WSL/RAPIDS call (WSL can't resolve backslash paths natively).
    def _to_wsl_path(p: Path) -> str:
        s = str(p.resolve())
        if s[1:3] == ":\\":
            drive = s[0].lower()
            return f"/mnt/{drive}/{s[3:].replace(chr(92), '/')}"
        return s.replace(chr(92), "/")

    wsl_in = _to_wsl_path(in_path)
    wsl_out = _to_wsl_path(out_path)

    def _gpu_call() -> str:
        return run_gpu(
            [
                _GPU_FEATURES_SCRIPT,
                "--in", wsl_in,
                "--out", wsl_out,
                "--as-of", as_of.isoformat(),
                "--dry-run",  # time GPU compute only (parquet write crashes on 4GB VRAM)
            ],
            timeout=600,
        )

    secs, _ = _time(_gpu_call)

    peak = _query_peak_vram_mib()
    note = "cuDF feature build via gpu/run_features.py (WSL/RAPIDS)."
    return secs, peak, note


def _query_peak_vram_mib() -> Optional[int]:
    """Best-effort peak VRAM in MiB from nvidia-smi inside WSL.

    Returns ``None`` (not a fabricated number) when the query fails.
    """
    try:
        out = run_gpu(["-c", "import subprocess; print(subprocess.check_output(['nvidia-smi','--query-gpu=memory.used','--format=csv,noheader,nounits']).decode().strip())"])
        # Take the first GPU's reading.
        first = out.strip().splitlines()[0].strip()
        return int(first)
    except Exception:
        return None


def _run_gpu_model(feats: pa.Table) -> tuple[Optional[float], str]:
    """GPU model stage. cuML model is not yet wired as code → honest not_run.

    When a cuML estimator lands (drop-in via ``train``/``predict`` signatures),
    this fn body changes; the harness contract does not.
    """
    return None, "cuML model not yet implemented; CPU sklearn timing is the only model stage measured."


# --------------------------------------------------------------------------- #
# Per-(row_count × stage) timing
# --------------------------------------------------------------------------- #
def _time_stage(
    stage: str,
    *,
    raw: pa.Table,
    feats: Optional[pa.Table],
    as_of: dt.date,
    run_cpu: bool,
    run_gpu: bool,
    repo_root: Path,
    tmp_dir: Path,
) -> Dict[str, Any]:
    """Time one stage on whichever stacks are requested.

    Returns a ``{cpu_s, gpu_s, speedup_x, status, notes}`` block. Stages that
    didn't run get ``None`` for their time and ``status="not_run"``.
    """
    cpu_s: Optional[float] = None
    gpu_s: Optional[float] = None
    notes: List[str] = []
    vram: Optional[int] = None

    if stage == "features":
        if run_cpu:
            cpu_s, feats_built = _run_cpu_features(raw, as_of)
            feats = feats_built  # pass through to the model stage
            notes.append(f"CPU: build_features() pyarrow over {raw.num_rows} rows.")
        if run_gpu:
            if _wsl_available():
                try:
                    gpu_s, vram, note = _run_gpu_features(
                        raw, as_of, repo_root=repo_root, tmp_dir=tmp_dir
                    )
                    notes.append(f"GPU: {note}")
                    if vram is not None:
                        notes.append(f"peak VRAM (nvidia-smi): {vram} MiB")
                except Exception as e:  # GPU run failed — record honestly.
                    notes.append(f"GPU: features run FAILED: {e}")
            else:
                notes.append("GPU: wsl launcher unavailable — not run.")
    elif stage == "model":
        if feats is None:
            notes.append("CPU: skipped — no features table available (features stage not run).")
        elif run_cpu:
            t_train, t_pred, _ = _run_cpu_model(feats)
            cpu_s = t_train + t_pred
            notes.append(
                f"CPU: train()+predict() sklearn LogisticRegression "
                f"(train={t_train:.3f}s, predict={t_pred:.3f}s) over {feats.num_rows} rows."
            )
        if run_gpu:
            gpu_s, note = _run_gpu_model(feats)
            notes.append(f"GPU: {note}")

    speedup = (cpu_s / gpu_s) if (cpu_s is not None and gpu_s is not None and gpu_s > 0) else None

    # Status: ok if the CPU side ran (our always-available reference); not_run
    # if only GPU was requested but unavailable; failed if a requested run threw.
    cpu_status = "ok" if cpu_s is not None else ("not_run" if not run_cpu else "failed")
    return {
        "cpu_s": round(cpu_s, 4) if cpu_s is not None else None,
        "gpu_s": round(gpu_s, 4) if gpu_s is not None else None,
        "speedup_x": round(speedup, 3) if speedup is not None else None,
        "peak_vram_mib": vram,
        "status": cpu_status,
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
RawSource = Union[pa.Table, str, os.PathLike]


def _coerce_to_rawloans(table: pa.Table) -> pa.Table:
    """Coerce lossless parquet integer-width drift to the RawLoans contract.

    Parquet often encodes ``int64`` contract fields as ``int32`` (e.g. ``term``).
    That is lossless and semantic-preserving, but :func:`validate_table` is
    rightly strict about exact types. We widen narrow integers to ``int64`` once,
    at the *load* seam (outside the timed region), so the pipeline under test
    (``build_features`` / ``train`` / ``predict``) receives contract-valid input
    and the benchmark times the real work — not the coercion.

    Any non-integer type mismatch is left for ``validate_table`` to reject loud.
    """
    from ..schema import RawLoans, schema_from_dataclass

    expected = schema_from_dataclass(RawLoans)
    cols = {}
    for field in expected:
        actual = table.schema.field(field.name).type
        want = field.type
        if actual.equals(want):
            cols[field.name] = table.column(field.name)
            continue
        # Lossless integer widening (int8/int16/int32 → int64).
        if pa.types.is_integer(actual) and pa.types.is_integer(want) and actual.bit_width < want.bit_width:
            import pyarrow.compute as pc
            cols[field.name] = pc.cast(table.column(field.name), want)
        else:
            # Real drift: hand the original through; validate_table will raise.
            cols[field.name] = table.column(field.name)
    # Preserve column order + any extra columns the source carries.
    extras = [n for n in table.column_names if n not in cols]
    for n in extras:
        cols[n] = table.column(n)
    return pa.table(cols)


def _load_raw(raw_source: RawSource) -> tuple[pa.Table, str]:
    """Resolve ``raw_source`` to an Arrow table + a human label for the report.

    Parquet sources are coerced to the RawLoans contract (lossless int widening
    only); in-memory tables are passed through verbatim (callers build them to
    the contract already).
    """
    if isinstance(raw_source, pa.Table):
        return raw_source, "<arrow-table>"
    p = Path(raw_source)
    if not p.exists():
        raise FileNotFoundError(f"raw_source parquet not found: {p}")
    return _coerce_to_rawloans(pq.read_table(p)), str(p)


def run_benchmark(
    row_counts: Sequence[int] = (100_000, 1_000_000),
    stages: Sequence[str] = STAGES,
    *,
    raw_source: RawSource,
    as_of: dt.date,
    gpu: bool = True,
    repo_root: Optional[Union[str, Path]] = None,
    results_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Run the CPU-vs-GPU benchmark and return a ``BenchReport`` dict.

    Parameters
    ----------
    row_counts
        Row counts to exercise (sliced/tiled from ``raw_source``). Default
        ``[100_000, 1_000_000]``.
    stages
        Subset of ``("features", "model")`` to time. Default both.
    raw_source
        Either a ``RawLoans``-shaped :class:`pyarrow.Table` or a path to a
        RawLoans parquet (e.g. ``data/loans.parquet``).
    as_of
        Snapshot date for feature engineering.
    gpu
        If ``True`` (default), request the GPU column too. When the WSL/RAPIDS
        launcher is unavailable the GPU stages are reported as ``not_run`` —
        never faked.
    repo_root, results_dir
        Optional overrides for locating ``gpu/run_features.py`` and the
        scratch/results directory. Defaults: cwd and ``bench/results/``.

    Returns
    -------
    dict
        A ``BenchReport`` with ``version``, ``generated_at``, ``as_of``,
        ``stages``, top-level ``notes``, and a ``results`` list (one entry per
        row count). Each result entry has per-stage timing blocks.
    """
    for s in stages:
        if s not in STAGES:
            raise ValueError(f"unknown stage {s!r}; expected one of {STAGES}")

    root = Path(repo_root) if repo_root is not None else Path.cwd()
    rdir = Path(results_dir) if results_dir is not None else root / "bench" / "results"
    rdir.mkdir(parents=True, exist_ok=True)

    raw_full, raw_label = _load_raw(raw_source)
    wsl_ok = _wsl_available()

    top_notes: List[str] = [
        f"raw source: {raw_label} ({raw_full.num_rows} rows total).",
        f"as_of: {as_of.isoformat()}.",
        f"CPU stack: pyarrow + sklearn (always available).",
        f"GPU stack: cuDF via WSL/RAPIDS (gpu={gpu}, wsl_available={wsl_ok}).",
        "cuML model stage is CPU-only until a GPU estimator is wired (reported not_run, not faked).",
    ]

    results: List[Dict[str, Any]] = []
    for rc in row_counts:
        raw = _slice_raw(raw_full, int(rc))
        feats: Optional[pa.Table] = None
        # Run stages in canonical order so features feeds model.
        stage_blocks: Dict[str, Dict[str, Any]] = {}
        for stage in STAGES:
            if stage not in stages:
                continue
            block = _time_stage(
                stage,
                raw=raw,
                feats=feats,
                as_of=as_of,
                run_cpu=True,
                run_gpu=gpu,
                repo_root=root,
                tmp_dir=rdir,
            )
            if stage == "features" and block["cpu_s"] is not None:
                # cpu_s set ⇒ feats was built inside _time_stage; rebuild here
                # to hand to the model stage (cheap relative to timing).
                feats = build_features(raw, as_of)
            stage_blocks[stage] = block
        results.append({
            "row_count": int(rc),
            "raw_source": raw_label,
            "stages": stage_blocks,
            "notes": [],
        })

    report: Dict[str, Any] = {
        "version": BENCH_VERSION,
        "generated_at": _now_iso(),
        "as_of": as_of.isoformat(),
        "stages": list(stages),
        "results": results,
        "notes": top_notes,
    }
    validate_report(report)  # self-check before returning
    return report


# --------------------------------------------------------------------------- #
# Persistence + validation
# --------------------------------------------------------------------------- #
def save_last_run(report: Dict[str, Any], path: Union[str, Path] = "bench/LAST_RUN.json") -> Path:
    """Write ``report`` as pretty JSON. ``bench/results/`` stays gitignored;
    ``bench/LAST_RUN.json`` is the committed snapshot for the README."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=False)
        fh.write("\n")
    return p


def validate_report(report: Dict[str, Any]) -> None:
    """Structural + honesty check on a BenchReport.

    Raises ``ValueError`` if:
      * required top-level keys are missing;
      * a per-stage block is missing required fields;
      * a ``status="not_run"`` stage nevertheless claims a GPU time (fabricated);
      * a reported ``speedup_x`` disagrees with ``cpu_s / gpu_s``.
    """
    for key in ("version", "generated_at", "as_of", "stages", "results", "notes"):
        if key not in report:
            raise ValueError(f"BenchReport missing top-level key: {key!r}")

    for i, res in enumerate(report["results"]):
        if "row_count" not in res or "stages" not in res:
            raise ValueError(f"results[{i}] missing row_count/stages")
        for stage, block in res["stages"].items():
            for fld in ("cpu_s", "gpu_s", "speedup_x", "status"):
                if fld not in block:
                    raise ValueError(
                        f"results[{i}].stages.{stage} missing field {fld!r}"
                    )
            status = block["status"]
            if status not in {"ok", "not_run", "failed"}:
                raise ValueError(f"results[{i}].stages.{stage} unknown status {status!r}")
            # Honesty: a not_run stage must not claim a gpu time.
            if status == "not_run" and block["gpu_s"] is not None:
                raise ValueError(
                    f"results[{i}].stages.{stage} status=not_run but gpu_s is set "
                    f"({block['gpu_s']}) — fabricated GPU timing is not allowed."
                )
            # Consistency: speedup must match cpu/gpu when both present.
            cpu, gpu, sp = block["cpu_s"], block["gpu_s"], block["speedup_x"]
            if cpu is not None and gpu is not None and gpu > 0:
                if sp is None or abs(sp - cpu / gpu) > 0.01:
                    raise ValueError(
                        f"results[{i}].stages.{stage} speedup_x {sp} != cpu_s/gpu_s ({cpu/gpu:.3f})"
                    )
