"""WA-082 — PD model registry: version, publish, and load the model binary.

Governance, not speed (a ``LogisticRegression.fit`` is fast). The value is
**reproducible, auditable, versioned** scoring — *"this run scored with model
``pd-lr-<id>``, trained as-of T, AUC=Y"* — instead of a subtly-different
in-process re-fit each run. Stays **in-process ($0, no PAI-EAS)** and keeps
``explain()`` intact (the artifact is unchanged; we only add a lineage header
and move the pickled bytes to OSS).

Pure/decoupled: the publish/load functions take a *client* exposing
``put_object(key, data)`` and ``get_bytes(key)`` (the WA-047 OSS write path, or a
fake in tests). No OSS SDK import here — the network stays at the edges.
"""
from __future__ import annotations

import hashlib
import json
import pickle
from typing import Any, Dict, List, Optional

import numpy as np

__all__ = [
    "model_id",
    "model_manifest",
    "dumps_model",
    "loads_model",
    "publish_model",
    "load_published_model",
    "DEFAULT_MODEL_PREFIX",
    "SCHEMA_VERSION",
]

DEFAULT_MODEL_PREFIX = "models/pd"
# The data contract the model was trained against (bump on a FeatureFrame change).
SCHEMA_VERSION = "1"


def model_id(model: Dict) -> str:
    """A deterministic ``pd-lr-<sha12>`` id for the fitted artifact.

    Hashes exactly the things that determine scoring — the linear coefficients +
    intercept, the leakage-safe feature list, the frozen band edges, and the
    calibrator's thresholds (WA-094) — so two identical models get the same id and
    any change (retrain, recalibrate, feature change) yields a new one.
    """
    h = hashlib.sha256()
    try:
        clf = model["pipeline"].named_steps["clf"]
        h.update(np.asarray(clf.coef_, dtype=float).tobytes())
        h.update(np.asarray(clf.intercept_, dtype=float).tobytes())
    except Exception:  # pragma: no cover - defensive; unfitted artifact
        h.update(b"no-clf")
    h.update(",".join(model.get("feature_columns", [])).encode())
    h.update(np.asarray(model.get("band_edges") or [], dtype=float).tobytes())
    cal = model.get("calibrator")
    if cal is not None:
        for attr in ("X_thresholds_", "y_thresholds_"):
            arr = getattr(cal, attr, None)
            if arr is not None:
                h.update(np.asarray(arr, dtype=float).tobytes())
    return f"pd-lr-{h.hexdigest()[:12]}"


def model_manifest(model: Dict, *, mid: Optional[str] = None) -> Dict[str, Any]:
    """A JSON-serialisable lineage header for the artifact — what an auditor
    reads to answer *"which model scored this, and how good was it?"*."""
    mid = mid or model_id(model)
    metrics = model.get("metrics", {}) if isinstance(model, dict) else {}
    return {
        "model_id": mid,
        "kind": "logistic_regression",
        "trained_at": model.get("trained_at"),
        "schema_version": SCHEMA_VERSION,
        "feature_columns": list(model.get("feature_columns", [])),
        "n_train": metrics.get("n_train"),
        "n_test": metrics.get("n_test"),
        "auc": metrics.get("auc"),
        "brier_calibrated": metrics.get("brier_calibrated"),
        "calibrated": bool(metrics.get("calibrated", False)),
        "band_edges": model.get("band_edges"),
    }


def dumps_model(model: Dict) -> bytes:
    """Pickle the artifact (incl. the WA-094 calibrator) to bytes for OSS."""
    return pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL)


def loads_model(data: bytes) -> Dict:
    """Inverse of :func:`dumps_model`."""
    return pickle.loads(data)


def _key(prefix: str, name: str) -> str:
    return f"{prefix.strip('/')}/{name}"


def publish_model(
    model: Dict,
    client: Any,
    *,
    prefix: str = DEFAULT_MODEL_PREFIX,
    bucket: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload the versioned binary + a ``latest.json`` pointer, return the manifest.

    Layout (governance-friendly, immutable per version)::

        {prefix}/{model_id}.pkl     the pickled artifact
        {prefix}/latest.json        manifest pointing at the current model_id

    ``client`` needs ``put_object(key, data, *, bucket=None)`` (the WA-047 write
    path). Fail-loud — a dropped model write is a correctness bug, not enrichment.
    """
    mid = model_id(model)
    manifest = model_manifest(model, mid=mid)
    manifest["key"] = _key(prefix, f"{mid}.pkl")

    client.put_object(manifest["key"], dumps_model(model), bucket=bucket)
    client.put_object(
        _key(prefix, "latest.json"),
        json.dumps(manifest, indent=2).encode("utf-8"),
        bucket=bucket,
    )
    return manifest


def load_published_model(
    client: Any,
    *,
    prefix: str = DEFAULT_MODEL_PREFIX,
    model_id: Optional[str] = None,  # noqa: A002 - matches the concept name
    bucket: Optional[str] = None,
) -> Dict:
    """Load a published model — a pinned ``model_id`` or the ``latest`` pointer.

    ``client`` needs ``get_bytes(key, *, bucket=None) -> bytes``. Raises
    (``FileNotFoundError`` / client error) when the artifact is absent, so the
    caller can fall back to training per-run. The loaded artifact carries its
    ``model_id`` so the run can cite exactly what scored it.
    """
    if model_id is None:
        manifest = json.loads(client.get_bytes(_key(prefix, "latest.json"), bucket=bucket))
        key = manifest.get("key") or _key(prefix, f"{manifest['model_id']}.pkl")
        mid = manifest["model_id"]
    else:
        key = _key(prefix, f"{model_id}.pkl")
        mid = model_id

    model = loads_model(client.get_bytes(key, bucket=bucket))
    model["model_id"] = mid  # stamp for audit even on older pickles
    return model
