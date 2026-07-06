"""WSL->GPU smoke test (waspada/wsl.py).

WA-001 AC: ``run_gpu(["-c", "import cudf, cuml; print('ok')"])`` prints ``ok``.
That requires WSL+RAPIDS on the host. In environments without WSL (e.g. the
docker worker container), this test skips cleanly rather than failing -- the
task body explicitly defers the GPU smoke to in-container/later runs.
"""

from __future__ import annotations

import shutil

import pytest

from waspada import wsl as wsl_mod


def _wsl_available() -> bool:
    return shutil.which("wsl") is not None


def test_run_gpu_smoke():
    """AC: run_gpu bridges to RAPIDS and prints 'ok'.

    Skipped when ``wsl`` is not on PATH (the GPU bridge runs on the host/WSL,
    not in the worker container). On WSL-capable hosts this is the real
    Python->WSL->RAPIDS proof called for by WA-001.
    """
    if not _wsl_available():
        pytest.skip("wsl not available in this environment (GPU smoke deferred)")
    out = wsl_mod.run_gpu(["-c", "import cudf, cuml; print('ok')"])
    assert out.strip() == "ok"


def test_run_gpu_raises_on_nonzero(monkeypatch):
    """run_gpu wraps subprocess and raises GpuError on non-zero exit."""
    import subprocess

    class _Fake:
        returncode = 2
        stdout = ""
        stderr = "boom"

    def fake_run(cmd, capture_output, text):
        assert cmd[1:3] == ["-e", "/root/rapids/bin/python"]
        return _Fake()

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(wsl_mod.GpuError) as exc:
        wsl_mod.run_gpu(["-c", "raise SystemExit"])
    assert exc.value.result.returncode == 2


def test_run_gpu_forwards_args_and_returns_stdout(monkeypatch):
    """run_gpu returns captured stdout on a zero-exit subprocess."""
    import subprocess

    class _Fake:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Fake())
    out = wsl_mod.run_gpu(["-c", "print('ok')"])
    assert out == "ok\n"
