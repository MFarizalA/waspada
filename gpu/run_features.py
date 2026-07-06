"""GPU/cuDF entry script for Collections feature engineering (WA-004).

Runs INSIDE WSL on the RAPIDS interpreter, invoked by
:func:`waspada.wsl.run_gpu` ::

    wsl -u root -e /root/rapids/bin/python gpu/run_features.py \
        --in data/raw_collections.parquet \
        --out data/features_collections.parquet \
        --as-of 2024-12-01

It reads a ``RawLoans``-shaped parquet, builds the cross-sectional
:class:`~waspada.schema.FeatureFrame` on GPU (cuDF), and writes a
``FeatureFrame``-shaped parquet out. The host NEVER imports cuDF — this script
is the only place the GPU stack is touched.

Single source of truth for the default label / delinquency bucket: this module
imports ``DEFAULT_STATUSES`` and ``delinquency_bucket`` from the host package
(:mod:`waspada.features.collections`) so the CPU and GPU paths cannot disagree
on what counts as a default (the CRITICAL invariant from WA-001/WA-004).

VRAM-safe (4 GB MX570 cap): the input is processed in row-slices and the
feature frames are concatenated at the end, so peak memory ≈ one slice, not the
whole book. Slice size is configurable (``--slice-rows``, default 200_000).

This file is import-safe on the CPU too (cudf import is deferred to ``main``),
so it can be syntax-checked / unit-grepped without a GPU. It only *runs* on GPU.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

# Make the host ``waspada`` package importable from WSL. The repo is mounted at
# /mnt/c/<windows repo path> inside WSL; this script's parent dir is the repo
# root, so inserting it lets us import the shared status logic below.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Shared business logic — the single source of truth (no re-implementation).
from waspada.features.collections import (  # noqa: E402
    DEFAULT_STATUSES,
    delinquency_bucket,
)

# Default slice size tuned for a 4 GB VRAM cap (MX570). ~13 RawLoans float/str
# cols → a 200k-row slice is well under 1 GB in cuDF; concat at the end.
DEFAULT_SLICE_ROWS = 200_000


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Collections FeatureFrame builder (GPU/cuDF, WA-004)."
    )
    p.add_argument("--in", dest="inp", required=True, help="input RawLoans parquet path")
    p.add_argument("--out", required=True, help="output FeatureFrame parquet path")
    p.add_argument(
        "--as-of",
        required=True,
        help="snapshot/scoring date YYYY-MM-DD (loan_age is measured to this date)",
    )
    p.add_argument(
        "--slice-rows",
        type=int,
        default=DEFAULT_SLICE_ROWS,
        help=f"rows per VRAM slice (default {DEFAULT_SLICE_ROWS})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="build features on GPU but skip parquet write (avoids VRAM crash on 4GB cards during write)",
    )
    return p.parse_args(argv)


def _build_slice(gdf, as_of: dt.date, default_set_lower: set[str]) -> "gdf.__class__":
    """Build FeatureFrame columns for one cuDF slice (pure GPU ops).

    ``default_set_lower`` is ``DEFAULT_STATUSES`` lowercased, passed in to avoid
    re-importing on every slice. The delinquency bucket reuses the shared
    ``delinquency_bucket`` python fn mapped over the *unique* statuses in the
    slice (small cardinality), so the bucket logic is never duplicated.
    """
    import cudf  # imported here so the module is import-safe without cuDF

    # --- behavioral features (vectorized on GPU) ---
    amount = gdf["amount"]
    safe_amt = amount.clip(lower=1e-9)  # avoid div-by-zero → inf/nan
    payment_ratio = (gdf["total_paid"] / safe_amt).fillna(0.0)
    outstanding_ratio = (gdf["outstanding_principal"] / safe_amt).fillna(0.0)

    # loan_age: whole months from issue_date to as_of, clamped at 0.
    issue = gdf["issue_date"]
    # cuDF supports .dt.year / .dt.month on datetime64; issue_date may be date32
    # → cast to datetime64[ms] for .dt accessors.
    issue_dt = cudf.to_datetime(issue)
    issue_ym = issue_dt.dt.year * 12 + issue_dt.dt.month
    as_of_ym = as_of.year * 12 + as_of.month
    loan_age = (as_of_ym - issue_ym).clip(lower=0).astype("int64")

    # label_default via the shared DEFAULT set (lowercased match).
    norm = gdf["current_status"].astype("string").str.strip().str.lower()
    label_default = norm.isin(list(default_set_lower))

    # delinquency bucket reusing the shared fn over the slice's unique statuses.
    uniq = gdf["current_status"].dropna().unique().to_pandas().tolist()
    bucket_map = {s: delinquency_bucket(s) for s in uniq}
    delinq = gdf["current_status"].map(bucket_map).fillna("other").astype("string")

    out = gdf[
        ["loan_id", "amount", "term", "rate", "grade", "annual_income",
         "dti", "purpose", "region"]
    ].copy()
    out["loan_age"] = loan_age
    out["payment_ratio"] = payment_ratio.astype("float64")
    out["outstanding_ratio"] = outstanding_ratio.astype("float64")
    out["delinquency_status"] = delinq
    out["label_default"] = label_default.astype("boolean")
    out["as_of_date"] = cudf.Series([as_of] * len(gdf), dtype="datetime64[ms]")
    return out


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    as_of = dt.date.fromisoformat(args.as_of)
    default_set_lower = {s.lower() for s in DEFAULT_STATUSES}

    import cudf  # GPU only at run time; host never reaches this line.

    raw = cudf.read_parquet(args.inp)
    n = len(raw)
    if n == 0:
        raise SystemExit(f"input parquet {args.inp!r} has 0 rows")

    slice_rows = max(1, args.slice_rows)
    parts = []
    for start in range(0, n, slice_rows):
        slc = raw.iloc[start : start + slice_rows]
        parts.append(_build_slice(slc, as_of, default_set_lower))

    feats = cudf.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]

    if args.dry_run:
        print(f"[dry-run] built {len(feats)} FeatureFrame rows on GPU (skipped parquet write)")
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    feats.to_parquet(args.out)
    print(f"wrote {len(feats)} FeatureFrame rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
