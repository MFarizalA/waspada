"""Reza's QA suite for the WASPADA Collections lane (WA-012).

Advisory-only tests. They assert the quality invariants the frozen contract
and the leakage rules require, and they document (as xfail/skip where
appropriate) findings that fail. The human-readable summary lives in
``REPORT.md``; these tests are the executable backing for every finding there.

Environment notes
-----------------
The worker container runs Linux but the repo's checked-in venv is a Windows
``.venv/Scripts/python.exe``. These tests therefore import the package via
``PYTHONPATH=<repo-root>`` (set by the qa conftest) and rely on the CPU
pyarrow/sklearn stack only — the GPU/cuDF path is exercised by the WSL smoke
check and is out of scope here.

The BigQuery-backed tests skip cleanly when creds are absent; when creds are
present (the sandbox is live) they run real LIMIT queries.
"""
