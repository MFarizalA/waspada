"""WSL → GPU entrypoint helper.

The single chokepoint for every GPU/RAPIDS step. The host worker containers have
no GPU; cuDF/cuML live inside WSL (proven on the MX570 — see HACKATHON.md option
A). So nothing on the host imports cuDF/cuML — all GPU/data work runs through
:func:`run_gpu`, which shells out to ``wsl -e <rapids-python> <args>``.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import List, Optional, Sequence

# The RAPIDS interpreter inside WSL (proven path; see HACKATHON.md).
DEFAULT_PYTHON = "/root/rapids/bin/python"


def run_gpu(
    args: Sequence[str],
    *,
    python: str = DEFAULT_PYTHON,
    check: bool = True,
    timeout: Optional[float] = None,
) -> str:
    """Run a Python invocation inside WSL on the RAPIDS interpreter.

    ``args`` is forwarded verbatim after the interpreter, so both inline code
    and script entry points work::

        run_gpu(["-c", "import cudf, cuml; print('ok')"])           # inline smoke
        run_gpu(["gpu/run_features.py", "--in", x, "--out", y])     # script entry

    Returns stdout (trailing whitespace stripped). On non-zero exit with
    ``check=True`` (default), raises :class:`RuntimeError` carrying stderr.
    Raises :class:`RuntimeError` if the ``wsl`` launcher is unavailable (e.g.
    inside a worker container) — never a bare ``FileNotFoundError``.
    """
    if shutil.which("wsl") is None:
        raise RuntimeError(
            "WSL launcher 'wsl' not found on PATH — run_gpu can only execute on a "
            "host with WSL+RAPIDS, not inside this container. "
            "(GPU steps run via WSL; see HACKATHON.md option A.)"
        )
    cmd: List[str] = ["wsl", "-e", python, *list(args)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"run_gpu failed (exit {proc.returncode}): {' '.join(cmd)}\n"
            f"--- stderr ---\n{proc.stderr}"
        )
    return proc.stdout.rstrip()
