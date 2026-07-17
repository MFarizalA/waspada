"""WA-046 regression — cross-process dispute-memory wiring.

Two separate Python processes share the same dispute-memory JSON file:

  * **run 1** — a subprocess writes a resolved (human-ruled) dispute into a
    :class:`LocalFileMemory` backend (simulating a real entrypoint persisting
    a freshly-resolved dispute after WA-046's wiring fix).
  * **run 2** — a *fresh* subprocess loads the same file and consults the
    memory; the prior **human** ruling must short-circuit the debate, so
    ``short_circuited`` is > 0.

Before WA-046, both entrypoints (``api/main.py:_build_demo_orchestrator`` and
``waspada/agents/__main__.py:main``) passed no ``memory_backend`` — the
orchestrator fell back to ``InMemoryMemory`` and no dispute ever persisted
across a process restart. After the fix, ``get_memory_backend()`` returns a
``LocalFileMemory`` wired into both entrypoints.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from waspada.agents.dispute_memory import DEFAULT_MEMORY_PATH, LocalFileMemory

_REPO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Subprocess scripts
# --------------------------------------------------------------------------- #
_RUN1 = """\
import sys
sys.path.insert(0, {repo!r})
from waspada.agents.dispute_memory import LocalFileMemory, DisputeMemory
from waspada.agents.protocol import Dispute

mem = LocalFileMemory({path!r})
dm = DisputeMemory(mem)
d = Dispute(
    loan_id="LN00000001",
    opened_by="risk_auditor",
    resolution="escalated_approved",
    resolved_by="human",
    rationale="run-1 human ruling",
    model_band="High",
    auditor_view="Very High",
)
dm.record_resolved(d)
dm.persist()
print("run1 size=", dm.size)
"""

_RUN2 = """\
import sys
sys.path.insert(0, {repo!r})
from waspada.agents.dispute_memory import LocalFileMemory, DisputeMemory
from waspada.agents.protocol import Dispute

mem = LocalFileMemory({path!r})
dm = DisputeMemory(mem)
# A fresh dispute on the SAME loan_id — run-2's auditor would open this again.
d = Dispute(loan_id="LN00000001", opened_by="risk_auditor",
            model_band="High", auditor_view="Very High")
recalled = dm.short_circuit(d)
assert recalled is not None, "run-2 must short-circuit on the human precedent"
assert recalled["from_memory"] is True
assert recalled["resolved_by"] == "human"
assert dm.short_circuited > 0, "short_circuited counter must be > 0"
print("run2 short_circuited=", dm.short_circuited)
"""


def _run(script_tmpl: str, mem_file: Path) -> subprocess.CompletedProcess:
    script = script_tmpl.format(repo=str(_REPO), path=str(mem_file))
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(_REPO),
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_wa046_cross_process_memory(tmp_path: Path) -> None:
    """Run-1 subprocess writes; run-2 fresh subprocess loads + short-circuits."""
    mem_file = tmp_path / "dispute_memory.json"

    # --- run 1: write a human-resolved dispute in a subprocess ---
    r1 = _run(_RUN1, mem_file)
    assert r1.returncode == 0, f"run-1 failed:\n{r1.stdout}\n{r1.stderr}"
    assert mem_file.exists(), "run-1 should have created the memory file"

    # --- run 2: a fresh process loads the file and short-circuits ---
    r2 = _run(_RUN2, mem_file)
    assert r2.returncode == 0, f"run-2 failed:\n{r2.stdout}\n{r2.stderr}"
    assert "short_circuited= 1" in r2.stdout


def test_wa046_default_path_is_data_dir() -> None:
    """The default memory path lives under the repo-root ``data/`` dir."""
    assert DEFAULT_MEMORY_PATH == "data/dispute_memory.json"
    mem = LocalFileMemory()
    # Compare as Path so the test is OS-agnostic (Windows uses backslashes).
    assert mem.path == Path(DEFAULT_MEMORY_PATH)


def test_wa046_local_file_memory_roundtrip(tmp_path: Path) -> None:
    """In-process sanity: write + read back through the same file."""
    mem_file = tmp_path / "roundtrip.json"
    mem = LocalFileMemory(str(mem_file))
    mem.save_memory({"LN1": {"resolution": "upheld", "resolved_by": "human"}})
    loaded = mem.load_memory()
    assert loaded == {"LN1": {"resolution": "upheld", "resolved_by": "human"}}


def test_wa046_get_memory_backend_returns_local_file() -> None:
    """``get_memory_backend()`` — the entrypoint wiring helper (WA-046)."""
    from waspada.agents.dispute_memory import get_memory_backend
    backend = get_memory_backend()
    assert isinstance(backend, LocalFileMemory), (
        "until the OSS backend lands (WA-047), the helper must always return "
        "a LocalFileMemory — never InMemoryMemory"
    )
