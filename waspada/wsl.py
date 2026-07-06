"""WSL->GPU entrypoint helper.

The single chokepoint for all GPU/data steps: shells out to the RAPIDS Python
interpreter inside WSL, captures stdout/stderr, and surfaces non-zero exits as
:class:`GpuError`. cuDF/cuML are never imported on the host -- every GPU call
goes through :func:`run_gpu`.

Resolution order for the WSL invocation (env-overridable for tests and for
running outside WSL):
    1. ``WASPADA_WSL_BIN``  -- the wsl executable path (default: ``"wsl"``).
    2. ``WASPADA_GPU_PY``   -- the GPU-side python (default:
       ``"/root/rapids/bin/python"``).

Example::

    out = run_gpu(["-c", "import cudf, cuml; print('ok')"])
    assert out.strip() == "ok"
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

__all__ = ["GpuResult", "GpuError", "run_gpu"]


@dataclass(frozen=True)
class GpuResult:
    """Captured result of a WSL->RAPIDS invocation."""

    stdout: str
    stderr: str
    returncode: int


class GpuError(RuntimeError):
    """Raised when a GPU/WSL subprocess exits non-zero.

    Carries the captured :class:`GpuResult` so callers can inspect output.
    """

    def __init__(self, result: GpuResult):
        self.result = result
        super().__init__(
            f"wsl python exited {result.returncode}: {result.stderr.strip()}"
        )


def _wsl_bin() -> str:
    return os.environ.get("WASPADA_WSL_BIN", "wsl")


def _gpu_python() -> str:
    return os.environ.get("WASPADA_GPU_PY", "/root/rapids/bin/python")


def run_gpu(args: list[str]) -> str:
    """Run a script/snippet in the WSL RAPIDS interpreter; return its stdout.

    Builds the command ``wsl -e <gpu_python> <args...>``. On non-zero exit,
    raises :class:`GpuError` (with full stderr). Designed to be easy to mock in
    tests -- the only external surface is :mod:`subprocess`.

    Args:
        args: Arguments forwarded to the GPU-side python interpreter
            (e.g. ``["-c", "import cudf; ..."]`` or ``["scripts/x.py", "--n",
            "1e6"]``).

    Returns:
        The subprocess stdout as a string.

    Raises:
        GpuError: if the subprocess returns a non-zero exit code.
        FileNotFoundError: if the configured wsl binary is not on PATH
            (i.e. WSL is unavailable in the current environment).
    """
    cmd = [_wsl_bin(), "-e", _gpu_python(), *args]
    completed = subprocess.run(  # noqa: S603 -- command built from trusted env
        cmd,
        capture_output=True,
        text=True,
    )
    result = GpuResult(
        stdout=completed.stdout,
        stderr=completed.stderr,
        returncode=completed.returncode,
    )
    if result.returncode != 0:
        raise GpuError(result)
    return result.stdout


def run_gpu_captured(args: list[str]) -> GpuResult:
    """Like :func:`run_gpu` but returns the full :class:`GpuResult`.

    Use this when a caller needs stderr even on success (e.g. for benchmark
    logging). Raises :class:`GpuError` on non-zero exit, same as :func:`run_gpu`.
    """
    cmd = [_wsl_bin(), "-e", _gpu_python(), *args]
    completed = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True
    )
    result = GpuResult(
        stdout=completed.stdout,
        stderr=completed.stderr,
        returncode=completed.returncode,
    )
    if result.returncode != 0:
        raise GpuError(result)
    return result
