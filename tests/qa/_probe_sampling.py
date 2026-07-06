"""Investigate why a 20k LIMIT sample shows 100% default rate while the full
table is ~14%. Tests whether the synthetic loans table is clustered by status
(which would make LIMIT-without-ORDER-BY return a biased sample)."""
from dotenv import load_dotenv
load_dotenv("/workspace/.env")

from waspada.data import BigQueryClient
from waspada.config import load_config
from waspada.features.collections import build_label
import pyarrow.compute as pc

cfg = load_config().require_bq()
client = BigQueryClient(cfg)

for lim in [1000, 5000, 20000, 50000]:
    t = client.fetch_loans(lane="collections", limit=lim)
    label = build_label(t)
    n = len(label)
    pos = pc.sum(pc.cast(label, __import__("pyarrow").int64())).as_py()
    print(f"LIMIT {lim:>6}: n={n}, default_rate={pos/n:.4f}, "
          f"first_status={t.column('current_status')[0].as_py()!r}")

# Also peek at the first few statuses to see clustering
t = client.fetch_loans(lane="collections", limit=30)
print("\nFirst 30 current_status values:")
print([t.column('current_status')[i].as_py() for i in range(30)])
