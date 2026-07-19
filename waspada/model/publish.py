"""WA-082 — publish entrypoint: train a PD model and upload the versioned binary.

    python -m waspada.model.publish --source path/to/loans.parquet [--bucket <name>]

Trains on a RawLoans parquet (build_features → train) and publishes the versioned
artifact + a ``latest.json`` pointer to OSS via the registry. This is the
**train-offline** step — run it in CI or by hand, never per request. Serving then
loads the frozen model (``WASPADA_PD_MODEL_SOURCE=oss``) instead of re-fitting.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

import pyarrow.parquet as pq

from ..features.collections import build_features
from ..schema import RawLoans, validate_table
from .registry import DEFAULT_MODEL_PREFIX, publish_model
from .risk import train


def publish_from_raw(
    raw_path: str,
    *,
    as_of: dt.date | None = None,
    prefix: str = DEFAULT_MODEL_PREFIX,
    bucket: str | None = None,
) -> dict:
    """Read RawLoans parquet → features → train → publish. Returns the manifest."""
    raw = pq.read_table(raw_path)
    validate_table(raw, RawLoans, name="publish(raw)")
    frame = build_features(raw, as_of or dt.date.today())
    model = train(frame)

    from ..data.oss import OSSClient  # lazy: only the publish path needs OSS

    manifest = publish_model(model, OSSClient(), prefix=prefix, bucket=bucket)
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Train + publish the PD model to OSS (WA-082).")
    ap.add_argument("--source", required=True, help="Path to a RawLoans parquet file.")
    ap.add_argument("--as-of", default=None, help="Snapshot date YYYY-MM-DD (default: today).")
    ap.add_argument("--prefix", default=DEFAULT_MODEL_PREFIX, help="OSS key prefix.")
    ap.add_argument("--bucket", default=None, help="OSS bucket (default: staging via env).")
    args = ap.parse_args(argv)

    as_of = dt.date.fromisoformat(args.as_of) if args.as_of else None
    manifest = publish_from_raw(args.source, as_of=as_of, prefix=args.prefix, bucket=args.bucket)
    print(f"published {manifest['model_id']} → {manifest['key']} "
          f"(auc={manifest.get('auc')}, calibrated={manifest.get('calibrated')})")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
