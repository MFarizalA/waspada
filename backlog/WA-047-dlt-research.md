---
id: WA-047-dlt-research
state: research-complete
priority: P1
owner: claude
researched: 2026-07-19
scope: feasibility of using dlt with the Data Engineer + Data Analyst agents
verification: dlt 1.28.2 installed; end-to-end PoC + API introspection (not a live-cred OSS fetch)
---

# WA-047 · Can we actually use dlt? — research + PoC

**Verdict: YES — genuinely integrable, end-to-end, and free.** Making dlt real
(not just a declared dependency) closes the biggest doc-vs-reality gap WA-070
flagged, and it activates the Staging/Mart medallion buckets the IaC provisions
but code never writes to. The original "dlt is dead code" reputation came from a
**wrong import** in the removed scaffold (`dlt.readers.filesystem` — never existed;
the real path is `dlt.sources.filesystem`), not a structural blocker.

Recommended shape: **hybrid** — Option A for the load (Data Engineer), Option B's
filesystem *destination* for the medallion write (Data Analyst).

---

## 1. What dlt gives us — verified against dlt 1.28.2 (installed)

| Capability | Status | Evidence |
|---|---|---|
| `dlt.pipeline` / `resource` / `source` | ✅ | present |
| **DuckDB destination** | ✅ | `dlt.destinations.duckdb` importable; PoC loaded into queryable DuckDB |
| **Merge dedup on `loan_id`** | ✅ PoC | `write_disposition="merge", primary_key="loan_id"`: merge(5 rows) then merge(7 overlapping) → **exactly 7 rows** |
| **Schema contract (freeze)** | ✅ PoC | `schema_contract={"columns":"freeze",...}`: a rogue extra column was **rejected** (`PipelineStepFailed`) |
| **`load_info` audit metadata** | ✅ PoC | `_dlt_loads` table, 1 row per load — real lineage/audit rows |
| **Incremental cursors** | ✅ (API) | `filesystem(..., incremental=...)` param exists |
| **Filesystem source** | ✅ | `dlt.sources.filesystem` exports `filesystem`, `read_parquet`, `read_csv`, `read_jsonl`, `readers`, `fsspec_filesystem` |
| **OSS routing (`oss://`)** | ✅ (construct) | `FilesystemConfiguration(bucket_url="oss://…")` → `protocol='oss'`; `fsspec_from_config` builds a real `OSSFileSystem` (read AND write side) |
| **OSS typed credentials** | ❌ | `oss` is NOT in dlt's `PROTOCOL_CREDENTIALS` (`gs/s3/az/…`) — creds are hand-wired via raw `kwargs`, not dlt secrets |

---

## 2. Where dlt fits each agent

**Data Engineer — the natural home (LOAD).** Today `oss.py` bulk-reads the parquet
and `lakehouse.py` registers an Arrow table in an in-memory DuckDB. dlt replaces the
*load step*: source → merge-on-`loan_id` + schema-contract(freeze) + DuckDB destination;
the Engineer's quality tools then query the dlt-loaded dataset. Real (not cosmetic)
gains: a dlt-enforced schema contract (stronger than the Python `validate_table` gate),
idempotent re-loads (merge dedup), `_dlt_loads` lineage as genuine audit metadata
(feeds the SLS story), and incremental cursors (matters at the 1M-loan book).

**Data Analyst — two honest roles.**
- *Indirect (free):* queries the dlt-loaded DuckDB and can **cite dlt's schema/load
  metadata** (rows-per-load, schema version, freshness) as debate evidence.
- *Direct + high value:* dlt's **filesystem destination** writes the Analyst's derived
  features/aggregates to **OSS Staging/Mart** as parquet — which **activates the medallion
  buckets the IaC grants PutObject to but code never uses** (closes a second WA-070 gap).
  dlt is EL, not a query engine, so it's the *write path*, not an Analyst query tool.

---

## 3. Option A — Arrow → dlt → DuckDB (LOAD; PoC-proven, low risk)

Keep `oss.py`'s proven `oss2` bulk-read, then pipe the Arrow table through dlt.
Sidesteps fsspec/ossfs entirely.

```python
import dlt
pipe = dlt.pipeline("waspada_loans", destination="duckdb", dataset_name="lakehouse",
                    pipelines_dir="/tmp")   # FC: use the writable /tmp
pipe.run(arrow_table, table_name="raw_loans",
         write_disposition="merge", primary_key="loan_id",
         schema_contract={"tables":"evolve","columns":"freeze","data_type":"freeze"})
# Data Engineer quality tools then query pipe.dataset() / the DuckDB destination.
```
**Risk: low** — this is exactly the shape the PoC ran green. Only wrinkle: dlt logs a
cosmetic "arrow schema vs dlt schema hints differ" warning (it coerces at load; silence-able).

---

## 4. Option B — pure dlt filesystem over OSS (READ + WRITE; feasible, config-fragile)

dlt routes `oss://` to `ossfs` correctly (verified: real `OSSFileSystem` on both read
and write). Needs the `ossfs` backend + **manual** endpoint/key/secret (no dlt typed creds).

**Read (Data Engineer, replaces oss2):**
```python
from dlt.sources.filesystem import filesystem, read_parquet
src = filesystem(bucket_url="oss://waspada-prod-raw", file_glob="loans.parquet",
                 kwargs={"key": OSS_ACCESS_KEY_ID, "secret": OSS_ACCESS_KEY_SECRET,
                         "endpoint": "https://oss-ap-southeast-1.aliyuncs.com"}) | read_parquet()
pipe.run(src, table_name="raw_loans", write_disposition="merge", primary_key="loan_id",
         schema_contract={"columns":"freeze", ...})
```

**Write (Data Analyst → medallion, the compelling case — no incumbent to beat):**
```python
mart = dlt.pipeline("waspada_mart",
    destination=dlt.destinations.filesystem(bucket_url="oss://waspada-prod-staging"),
    dataset_name="features")
mart.run(feature_arrow, table_name="feature_frame",
         write_disposition="replace", loader_file_format="parquet")   # parquet verified supported
```

**Residual risks (Option B only):**
1. **New dependency `ossfs`** (2025.5.0; installs clean) — smaller/less-tested than `s3fs`.
2. **Untyped credentials** — `oss` absent from dlt `PROTOCOL_CREDENTIALS`; endpoint/key/secret
   passed as raw `kwargs`. The **"OSS endpoint is not set" warning is the classic trap** (must
   set it explicitly).
3. **Unproven on a live GET/PUT** — this research verified routing + FS construction + loader
   format, NOT an authenticated OSS transfer (no creds in the research env). The last 5%
   (auth handshake, region/endpoint correctness) is where OSS-fsspec configs bite.

---

## 5. Recommendation — split by risk profile

| Path | Recommended | Why |
|---|---|---|
| **LOAD** (OSS → DuckDB, Data Engineer) | **Option A** | Reuses the proven `oss2` read; PoC-green; no ossfs. |
| **WRITE** (features → Staging/Mart, Data Analyst) | **Option B destination** | No existing oss2 write path; cleanest way to populate the medallion; closes the dead-bucket gap. |

**Cost:** runtime ≈ zero — dlt is an in-process library, DuckDB is embedded, **no new cloud
service** (contrast PAI-EAS, which is a cost *add*). FC caveat: point `pipelines_dir` at the
writable `/tmp`. Effort: **~0.5–1 day** for Option A + Engineer wiring + tests (low risk);
the Option-B write path adds ossfs + a **live-cred OSS smoke test** (required before trusting
it in the demo).

**Sequencing / ownership:** do this AFTER the submission-critical items. The Data Engineer is
Bimo's and the load/write layer is Stefanie's data lane — this is a dispatched ticket, not a
solo-start. This IS the genuine "WA-047 dlt pipeline" the architecture always described.

## 6. Open item before implementation
- **Live-cred OSS smoke test** for Option B: one authenticated `read_parquet` from
  `oss://waspada-prod-raw` and one `put` to `oss://waspada-prod-staging`, with the real
  endpoint + AccessKey. Everything else is verified.
