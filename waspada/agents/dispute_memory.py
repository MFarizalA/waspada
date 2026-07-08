"""Cross-run dispute memory (WA-026) — institutional memory, NOT self-improvement.

The society remembers resolved disputes between runs so a re-run of the SAME
book spends measurably fewer LLM calls (the second headline efficiency number).
Honest framing (load-bearing): this is **decision consistency + institutional
memory** (a lender applying its own precedent uniformly), NOT the model getting
smarter. Do not claim self-improvement.

What the memory stores, keyed by ``loan_id``:

  * The terminal ``resolution`` (``upheld`` / ``overridden`` /
    ``escalated_approved`` / ``escalated_rejected``), ``resolved_by``
    (``arbiter`` / ``risk_model`` / ``human``), and ``rationale``.
  * The model/auditor band view at the time, so a re-scored band can be
    compared against the remembered one.

How the orchestrator uses it (see :meth:`Orchestrator._resolve_disputes`):

  1. **Before** opening a debate, it asks the memory whether this ``loan_id`` was
     previously settled by a HUMAN. If so (and ``resolved_by == "human"``), the
     dispute is **short-circuited** — the prior ruling is reused and no
     debate/arbiter calls are spent. This is the only short-circuit path: only
     human-settled cases (the strongest precedent) skip the debate, which keeps
     the demo's disputes visibly happening for everything else.
  2. Otherwise (arbitrator/model precedent, or a fresh account) the debate runs
     as normal, BUT a remembered precedent is **injected as context** so the
     Arbiter/Skeptic see it (the rounds carry the prior ruling). The memory
     INFORMS; it does not silence.
  3. **After** the run completes, every freshly resolved dispute is persisted so
     the next run sees it.

The storage backend is swappable via a minimal interface:

  * :meth:`load_memory() -> dict`
  * :meth:`save_memory(dict) -> None`

Two concrete backends ship:

  * :class:`LocalFileMemory` — a local JSON file (``data/dispute_memory.json``).
    This is the demo-time adaptation of the ticket's "persist to OSS" path; the
    OSS backend is deferred until deploy and drops in behind the same interface.
  * :class:`InMemoryMemory` — a dict held in process (used by tests + the
    default when no path is configured, so an accidental import never touches
    the disk).
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

from .protocol import Dispute

__all__ = [
    "MemoryBackend",
    "InMemoryMemory",
    "LocalFileMemory",
    "DisputeMemory",
    "DEFAULT_MEMORY_PATH",
]

# Default on-disk location for the demo. Resolves under the repo-root ``data/``
# (gitignored — large/runtime artifacts live there, not under the source
# package). ``data/dispute_memory.json``.
DEFAULT_MEMORY_PATH = "data/dispute_memory.json"

# Schema version of the on-disk file. Bumped only on a breaking shape change;
# :meth:`DisputeMemory.load` tolerates older shapes defensively.
_MEMORY_VERSION = "1"


# --------------------------------------------------------------------------- #
# Backend interface — load_memory() / save_memory(dict)
# --------------------------------------------------------------------------- #
class MemoryBackend(ABC):
    """The swappable storage interface (WA-026 adaptation note).

    Two methods, both synchronous: the dispute set per run is small (the audit
    top-K, typically ≤ 16), so a blocking file read/write is plenty for the
    demo. An OSS backend (deferred) implements the same two methods against
    ``oss2`` and needs no orchestrator changes.
    """

    @abstractmethod
    def load_memory(self) -> Dict[str, Any]:
        """Return the remembered disputes as ``{loan_id: {...}}`` (empty if none)."""
        raise NotImplementedError

    @abstractmethod
    def save_memory(self, data: Dict[str, Any]) -> None:
        """Persist the full memory dict (overwrite)."""
        raise NotImplementedError


class InMemoryMemory(MemoryBackend):
    """Dict-backed memory — used by tests and as the no-disk default.

    Holds the dict in process; ``save_memory`` replaces it wholesale. Never
    touches the filesystem, so an unconfigured import is side-effect-free.
    """

    def __init__(self, seed: Optional[Dict[str, Any]] = None) -> None:
        self._data: Dict[str, Any] = dict(seed or {})

    def load_memory(self) -> Dict[str, Any]:
        return dict(self._data)

    def save_memory(self, data: Dict[str, Any]) -> None:
        self._data = dict(data or {})


class LocalFileMemory(MemoryBackend):
    """JSON-file-backed memory (``data/dispute_memory.json`` by default).

    This is the demo-time stand-in for the ticket's OSS path: same two-method
    interface, local file storage. The file is created lazily on first save;
    a missing or empty file reads back as ``{}`` (cold start — no memory yet).
    Atomic write via a temp rename so a crash mid-write never leaves a
    half-written file.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or DEFAULT_MEMORY_PATH)

    def load_memory(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return {}
        if not text.strip():
            return {}
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            # A corrupt file never crashes a run — degrade to cold start and
            # let the next save overwrite it cleanly.
            return {}
        if not isinstance(obj, dict):
            return {}
        # Tolerate a versioned wrapper (``{"version": .., "disputes": {..}}``)
        # or a bare ``{loan_id: {...}}`` mapping.
        disputes = obj.get("disputes") if "disputes" in obj else obj
        if not isinstance(disputes, dict):
            return {}
        return {str(k): v for k, v in disputes.items() if isinstance(v, dict)}

    def save_memory(self, data: Dict[str, Any]) -> None:
        payload = {
            "version": _MEMORY_VERSION,
            "disputes": dict(data or {}),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# DisputeMemory — the facade the orchestrator talks to
# --------------------------------------------------------------------------- #
class DisputeMemory:
    """Cross-run dispute memory: lookup / short-circuit / record / precedent.

    The orchestrator holds one of these. Per dispute it asks three questions:

      * :meth:`short_circuit` — is there a HUMAN precedent strong enough to
        reuse the prior resolution and skip the debate entirely? Returns the
        reused resolution dict, or ``None`` to let the debate run.
      * :meth:`precedent` — is there ANY prior ruling to inject as context
        (so the Arbiter/Skeptic see precedent)? Returns a short context dict,
        or ``None`` when the account is unseen.

    After the run resolves its disputes, the orchestrator calls
    :meth:`record_resolved` per dispute and then :meth:`persist` to flush.

    The ``short_circuited`` / ``precedent_hits`` counters are surfaced on the
    facade so the run report / benchmark can report the second efficiency axis
    (run-2 spends fewer LLM calls than run-1 on the same book).
    """

    # Only a prior HUMAN ruling is strong enough to short-circuit (skip the
    # debate). Arbitrator/model precedent still INFORMS (injected as context)
    # but the debate runs — the demo must keep showing disputes happening.
    _SHORT_CIRCUIT_BY = "human"

    def __init__(self, backend: Optional[MemoryBackend] = None) -> None:
        self.backend: MemoryBackend = backend if backend is not None else InMemoryMemory()
        self._loaded: Dict[str, Any] = {}
        self._dirty: bool = False
        # Efficiency counters surfaced for the run report / benchmark.
        self.short_circuited: int = 0
        self.precedent_hits: int = 0
        self.misses: int = 0

    # ------------------------------------------------------------ lifecycle
    def load(self) -> Dict[str, Any]:
        """Lazily read the backend once per facade (cached for the run)."""
        if not self._loaded:
            self._loaded = self.backend.load_memory()
        return self._loaded

    def persist(self) -> None:
        """Flush the in-memory dict to the backend if anything changed."""
        if not self._dirty:
            return
        self.backend.save_memory(self._loaded)
        self._dirty = False

    # ----------------------------------------------------------------- reads
    def lookup(self, loan_id: str) -> Optional[Dict[str, Any]]:
        """Return the remembered record for ``loan_id``, or ``None`` (unseen)."""
        if not loan_id:
            return None
        return self.load().get(loan_id)

    def short_circuit(self, dispute: Dispute) -> Optional[Dict[str, Any]]:
        """Return a reused resolution to skip the debate, or ``None``.

        Short-circuits ONLY when a prior ruling was made by a human (the
        strongest precedent — the lender's own settled call). The reused dict
        carries ``resolution``, ``resolved_by``, ``rationale``, and a
        ``from_memory=True`` marker so the audit log distinguishes a recalled
        ruling from a freshly-debated one.
        """
        prior = self.lookup(dispute.loan_id)
        if prior is None:
            self.misses += 1
            return None
        if str(prior.get("resolved_by", "")) != self._SHORT_CIRCUIT_BY:
            # Non-human precedent → record the hit (informs) but don't silence.
            self.precedent_hits += 1
            return None
        self.short_circuited += 1
        return {
            "resolution": prior.get("resolution", ""),
            "resolved_by": prior.get("resolved_by", ""),
            "rationale": prior.get("rationale", ""),
            "from_memory": True,
        }

    def precedent(self, dispute: Dispute) -> Optional[Dict[str, Any]]:
        """Return prior-ruling context to inject into the debate, or ``None``.

        Used for non-short-circuiting precedent (arbitrator/model rulings, or a
        human ruling whose loan_id differs — though :meth:`short_circuit`
        already captures same-loan human hits). The orchestrator stamps a note
        onto the dispute so the Arbiter/Skeptic prompts can cite it.
        """
        prior = self.lookup(dispute.loan_id)
        if prior is None:
            return None
        return {
            "resolved_by": prior.get("resolved_by", ""),
            "resolution": prior.get("resolution", ""),
            "rationale": prior.get("rationale", ""),
            "model_band": prior.get("model_band", ""),
            "auditor_view": prior.get("auditor_view", ""),
        }

    # ---------------------------------------------------------------- writes
    def record_resolved(self, dispute: Dispute) -> None:
        """Remember one freshly-resolved dispute, keyed by ``loan_id``.

        Only disputes with a terminal ``resolution`` are remembered; an
        unresolved one (empty resolution) is a no-op so the memory never holds
        a half-finished record that would mislead the next run.
        """
        if not dispute.loan_id or not dispute.resolution:
            return
        self._loaded[dispute.loan_id] = {
            "resolution": dispute.resolution,
            "resolved_by": dispute.resolved_by,
            "rationale": dispute.rationale,
            "model_band": dispute.model_band,
            "auditor_view": dispute.auditor_view,
            # Round count + the speakers, for an audit trail without the full
            # transcript (keeps the memory file small and diffable).
            "rounds": len(dispute.rounds),
            "speakers": [r.speaker for r in dispute.rounds],
        }
        self._dirty = True

    def record_many(self, disputes: List[Dispute]) -> int:
        """Record every resolved dispute in ``disputes``. Returns the count stored."""
        n = 0
        for d in disputes:
            if d.loan_id and d.resolution:
                self.record_resolved(d)
                n += 1
        return n

    # ------------------------------------------------------------------ misc
    def reset_counters(self) -> None:
        """Zero the efficiency counters (per-run bookkeeping)."""
        self.short_circuited = 0
        self.precedent_hits = 0
        self.misses = 0

    @property
    def size(self) -> int:
        """Number of remembered accounts (use this, not ``len(mem)`` — a
        :class:`DisputeMemory` is always truthy regardless of how many
        accounts it holds)."""
        return len(self.load())
