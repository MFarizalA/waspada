"""Live BQ data-quality probe — gathers evidence for the QA report.

Runs a few aggregate queries against the sandbox loans table so the REPORT.md
findings on null rates, label distribution, out-of-range values, and vintage
coverage are backed by real numbers, not guesses. Output is a JSON dict printed
to stdout.
"""
from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv

load_dotenv("/workspace/.env")

import pyarrow as pa  # noqa: E402

from waspada.config import load_config  # noqa: E402
from waspada.data import BigQueryClient  # noqa: E402


def main() -> int:
    cfg = load_config().require_bq()
    client = BigQueryClient(cfg)
    table = f"{cfg.bq_project}.{cfg.bq_dataset}.{cfg.bq_table}"

    def q(sql: str):
        return client.query(sql).to_pylist()

    out: dict = {"table": table}

    # 1. row count
    out["n_rows"] = q(f"SELECT COUNT(*) AS n FROM `{table}`")[0]["n"]

    # 2. null rates per RawLoans field
    from waspada.schema import RawLoans
    import dataclasses
    fields = [f.name for f in dataclasses.fields(RawLoans)]
    null_exprs = ", ".join(f"COUNTIF({f} IS NULL) AS null_{f}" for f in fields)
    nulls = q(f"SELECT {null_exprs} FROM `{table}`")[0]
    out["null_rates"] = {
        f: (nulls[f"null_{f}"] / out["n_rows"] if out["n_rows"] else None)
        for f in fields
    }

    # 3. label distribution (current_status)
    out["status_distribution"] = {
        r["current_status"]: r["n"]
        for r in q(
            f"SELECT current_status, COUNT(*) AS n FROM `{table}` "
            f"GROUP BY current_status ORDER BY n DESC"
        )
    }

    # 4. range checks — min/max of key numerics
    ranges = q(
        f"SELECT "
        f"MIN(amount) AS min_amount, MAX(amount) AS max_amount, "
        f"MIN(term) AS min_term, MAX(term) AS max_term, "
        f"MIN(rate) AS min_rate, MAX(rate) AS max_rate, "
        f"MIN(dti) AS min_dti, MAX(dti) AS max_dti, "
        f"MIN(annual_income) AS min_inc, MAX(annual_income) AS max_inc, "
        f"MIN(issue_date) AS min_issue, MAX(issue_date) AS max_issue, "
        f"MIN(outstanding_principal) AS min_outp, MAX(outstanding_principal) AS max_outp, "
        f"MIN(total_paid) AS min_paid, MAX(total_paid) AS max_paid "
        f"FROM `{table}`"
    )[0]
    out["ranges"] = ranges

    # 5. negative / degenerate value counts
    out["degenerate"] = q(
        f"SELECT "
        f"COUNTIF(amount <= 0) AS n_amt_le0, "
        f"COUNTIF(term <= 0) AS n_term_le0, "
        f"COUNTIF(rate < 0) AS n_rate_lt0, "
        f"COUNTIF(annual_income < 0) AS n_inc_lt0, "
        f"COUNTIF(outstanding_principal < 0) AS n_outp_lt0, "
        f"COUNTIF(total_paid < 0) AS n_paid_lt0, "
        f"COUNTIF(outstanding_principal > amount) AS n_outp_gt_amt, "
        f"COUNTIF(total_paid > amount) AS n_paid_gt_amt "
        f"FROM `{table}`"
    )[0]

    # 6. issue_date / vintage coverage
    out["vintage_counts"] = {
        str(r["y"]): r["n"]
        for r in q(
            f"SELECT EXTRACT(YEAR FROM issue_date) AS y, COUNT(*) AS n "
            f"FROM `{table}` GROUP BY y ORDER BY y"
        )
    }

    # 7. duplicate loan_id check
    out["n_duplicate_loan_ids"] = q(
        f"SELECT COUNT(*) AS n FROM ("
        f"SELECT loan_id FROM `{table}` GROUP BY loan_id HAVING COUNT(*) > 1)"
    )[0]["n"]

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
