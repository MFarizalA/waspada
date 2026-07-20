#!/usr/bin/env python
"""WA-078 — land a REAL RawLoans-conformant dataset in the OSS Raw bucket.

The live pipeline reads its loan book from OSS as Parquet
(:func:`waspada.data.oss.fetch_loans`). Until a real object exists there,
``/api/run`` returns 503 ("data source unavailable"). This script sources the
public **Lending Club accepted-loans** CSV (Kaggle: ``wordsforthewise/
lending-club`` → ``accepted_2007_to_2018Q4.csv``), maps it to the frozen
:class:`~waspada.schema.RawLoans` schema, writes Parquet, and uploads it to the
object ``fetch_loans`` reads — then verifies the round-trip.

Why Lending Club: it's cross-sectional (one row per loan, final status + payment
totals) and carries collections-lane outcome labels (``loan_status``) — it *is*
our data model, not an approximation.

Design notes
------------
* **stdlib CSV streaming + reservoir sample** — the raw CSV is ~1.7 GB / 2.2M
  rows. We stream it row-by-row (bounded memory) and reservoir-sample to
  ``--sample`` rows with a fixed seed, so a demo-sized book is representative of
  all vintages rather than just the oldest (a head-N slice).
* **No pandas / no Kaggle SDK** — you download the CSV once (any way you like)
  and pass ``--csv``; the transform is pure stdlib + pyarrow.
* **Schema-exact output** — the Arrow table is built with
  ``schema_from_dataclass(RawLoans)`` and passed through the same
  ``validate_table`` the reader uses, so a successful write guarantees a
  successful ``fetch_loans``.

Usage
-----
    # dry-run: transform + write local parquet, DO NOT upload
    python scripts/load_lending_club.py --csv accepted_2007_to_2018Q4.csv \
        --sample 200000 --out raw_loans.parquet

    # full: transform, write, and upload to OSS (needs OSS_* env vars)
    python scripts/load_lending_club.py --csv accepted_2007_to_2018Q4.csv \
        --sample 200000 --upload

Environment for --upload (same vars fetch_loans reads):
    OSS_RAW_BUCKET, OSS_ENDPOINT, OSS_KEY, OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import random
import re
import sys
from typing import Any, Dict, List, Optional

# Make the repo importable when run as a script from anywhere.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import pyarrow as pa
import pyarrow.parquet as pq

from waspada.schema import RawLoans, schema_from_dataclass, validate_table

# Lending Club CSV rows can be very wide; lift the field-size cap.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# --------------------------------------------------------------------------- #
# US state -> census region, so RawLoans.region carries a real segmentation
# dimension (matches the four-region vocabulary the synthetic snapshot used).
# --------------------------------------------------------------------------- #
_STATE_REGION: Dict[str, str] = {}
for _region, _states in {
    "Northeast": "CT ME MA NH RI VT NJ NY PA",
    "Midwest": "IL IN MI OH WI IA KS MN MO NE ND SD",
    "South": "DE FL GA MD NC SC VA DC WV AL KY MS TN AR LA OK TX",
    "West": "AZ CO ID MT NV NM UT WY AK CA HI OR WA",
}.items():
    for _s in _states.split():
        _STATE_REGION[_s] = _region

_TERM_RE = re.compile(r"(\d+)")
_RAW_COLUMNS = [f.name for f in __import__("dataclasses").fields(RawLoans)]


def _to_float(v: Any) -> Optional[float]:
    """Parse a possibly ``%``-suffixed / whitespace-padded number, or None."""
    if v is None:
        return None
    s = str(v).strip().rstrip("%").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_term(v: Any) -> Optional[int]:
    """'  36 months' / '60 months' / '36' -> 36 (months)."""
    if v is None:
        return None
    m = _TERM_RE.search(str(v))
    return int(m.group(1)) if m else None


def _parse_issue_date(v: Any) -> Optional[dt.date]:
    """Lending Club 'issue_d' is 'Mon-YYYY' (e.g. 'Dec-2018'); -> first of month."""
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%b-%Y", "%Y-%m", "%b-%y"):
        try:
            return dt.datetime.strptime(s, fmt).date().replace(day=1)
        except ValueError:
            continue
    return None


def map_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map one Lending Club accepted-loans row to a RawLoans dict.

    Returns ``None`` when a required field is missing/unparseable — the caller
    skips it. Pure and side-effect-free, so it is unit-tested directly without
    the multi-GB CSV.
    """
    loan_id = (row.get("id") or "").strip()
    if not loan_id or not loan_id.isdigit():
        return None  # LC files carry non-loan header/footer lines (URLs, blanks)

    amount = _to_float(row.get("loan_amnt"))
    term = _parse_term(row.get("term"))
    rate = _to_float(row.get("int_rate"))
    grade = (row.get("grade") or "").strip()
    annual_income = _to_float(row.get("annual_inc"))
    dti = _to_float(row.get("dti"))
    issue_date = _parse_issue_date(row.get("issue_d"))
    purpose = (row.get("purpose") or "").strip()
    region = _STATE_REGION.get((row.get("addr_state") or "").strip().upper(), "Other")
    outstanding_principal = _to_float(row.get("out_prncp"))
    total_paid = _to_float(row.get("total_pymnt"))
    current_status = (row.get("loan_status") or "").strip()

    # Required (non-null, correctly typed) fields — drop the row if any is bad.
    if None in (amount, term, rate, annual_income, dti, issue_date,
                outstanding_principal, total_paid):
        return None
    if not (grade and purpose and current_status):
        return None

    return {
        "loan_id": loan_id,
        "amount": amount,
        "term": term,
        "rate": rate,
        "grade": grade,
        "annual_income": annual_income,
        "dti": dti,
        "issue_date": issue_date,
        "purpose": purpose,
        "region": region,
        "outstanding_principal": outstanding_principal,
        "total_paid": total_paid,
        "current_status": current_status,
    }


def rows_to_table(rows: List[Dict[str, Any]]) -> pa.Table:
    """Build a RawLoans-schema Arrow table from mapped row dicts (validated)."""
    schema = schema_from_dataclass(RawLoans)
    cols = {name: [r[name] for r in rows] for name in _RAW_COLUMNS}
    table = pa.table(cols, schema=schema)
    validate_table(table, RawLoans, name="load_lending_club")
    return table


def stream_sample(csv_path: str, sample: Optional[int], seed: int) -> List[Dict[str, Any]]:
    """Stream the CSV, map each row, reservoir-sample to ``sample`` mapped rows.

    ``sample=None`` keeps every valid row. Memory is bounded by the sample size
    (or the full valid set when unbounded).
    """
    rng = random.Random(seed)
    kept: List[Dict[str, Any]] = []
    n_seen = n_valid = 0
    with io.open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            n_seen += 1
            mapped = map_row(raw)
            if mapped is None:
                continue
            n_valid += 1
            if sample is None:
                kept.append(mapped)
            elif len(kept) < sample:
                kept.append(mapped)
            else:
                j = rng.randint(0, n_valid - 1)  # reservoir
                if j < sample:
                    kept[j] = mapped
            if n_seen % 250_000 == 0:
                print(f"  ... {n_seen:,} rows scanned, {n_valid:,} valid, {len(kept):,} kept")
    print(f"scanned {n_seen:,} rows, {n_valid:,} valid, kept {len(kept):,}")
    return kept


def upload_to_oss(local_parquet: str) -> None:
    """Put the parquet at the object fetch_loans reads, then verify round-trip."""
    from waspada.config import load_config
    cfg = load_config()
    try:
        import oss2  # type: ignore
    except ImportError:
        raise SystemExit("oss2 not installed: pip install oss2 (needed only for --upload)")

    ak = os.environ.get("OSS_ACCESS_KEY_ID")
    sk = os.environ.get("OSS_ACCESS_KEY_SECRET")
    if not (ak and sk and cfg.oss_raw_bucket and cfg.oss_endpoint and cfg.oss_key):
        raise SystemExit(
            "OSS not fully configured: need OSS_RAW_BUCKET, OSS_ENDPOINT, OSS_KEY, "
            "OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET.")

    bucket = oss2.Bucket(oss2.Auth(ak, sk), cfg.oss_endpoint, cfg.oss_raw_bucket)
    with io.open(local_parquet, "rb") as f:
        bucket.put_object(cfg.oss_key, f)
    print(f"uploaded -> oss://{cfg.oss_raw_bucket}/{cfg.oss_key}")

    # Verify the reader can actually load what we wrote.
    from waspada.data.oss import fetch_loans
    tbl = fetch_loans(limit=5)
    print(f"verify: fetch_loans() round-trip OK -- {tbl.num_rows} sample rows, "
          f"{len(tbl.column_names)} columns")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Load Lending Club → RawLoans → OSS (WA-078).")
    ap.add_argument("--csv", required=True, help="path to accepted_2007_to_2018Q4.csv")
    ap.add_argument("--sample", type=int, default=200_000,
                    help="reservoir-sample N valid rows (0 = keep all). Default 200000.")
    ap.add_argument("--seed", type=int, default=42, help="sample seed (reproducible).")
    ap.add_argument("--out", default="raw_loans.parquet", help="local parquet output path.")
    ap.add_argument("--upload", action="store_true", help="upload to OSS after writing.")
    args = ap.parse_args(argv)

    if not os.path.exists(args.csv):
        raise SystemExit(f"CSV not found: {args.csv}")

    sample = None if args.sample == 0 else args.sample
    print(f"reading {args.csv} (sample={sample}, seed={args.seed}) ...")
    rows = stream_sample(args.csv, sample, args.seed)
    if not rows:
        raise SystemExit("no valid rows produced — check the CSV is the LC accepted-loans file.")

    table = rows_to_table(rows)
    pq.write_table(table, args.out)
    print(f"wrote {table.num_rows:,} rows -> {args.out} "
          f"({os.path.getsize(args.out) / 1e6:.1f} MB)")

    if args.upload:
        upload_to_oss(args.out)
    else:
        print("dry-run: skipped upload (pass --upload to write to OSS).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
