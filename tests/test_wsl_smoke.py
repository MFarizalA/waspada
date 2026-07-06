"""Smoke test for the WSL → RAPIDS bridge (WA-001 acceptance).

Skipped inside the worker container — there is no GPU and no ``wsl`` launcher
here (GPU runs in WSL on the host; see HACKATHON.md option A). Enable on the
host/WSL by setting ``WASPADA_RUN_GPU_TESTS=1``; it then asserts the
Python → WSL → RAPIDS bridge works (cuDF + cuML import cleanly).

The in-container GPU run is a separate ticket; this test is written now and
gated so the suite stays green until that ticket lands.
"""
from __future__ import annotations

import os
import shutil

import pytest

from waspada.wsl import run_gpu

_ENABLED = os.environ.get("WASPADA_RUN_GPU_TESTS") == "1"
_WSL_PRESENT = shutil.which("wsl") is not None


@pytest.mark.skipif(
    not (_ENABLED or _WSL_PRESENT),
    reason="WSL/RAPIDS unavailable here; set WASPADA_RUN_GPU_TESTS=1 on the host/WSL.",
)
def test_run_gpu_imports_rapids():
    out = run_gpu(["-c", "import cudf, cuml; print('ok')"])
    assert out.strip() == "ok"
