"""Qwen model-tier resolution (env-configurable, with defaults)."""
from __future__ import annotations

import pytest

from waspada.agents.llm import QWEN_TIER_DEFAULTS, qwen_tier


def _clear(monkeypatch):
    for k in ("QWEN_MODEL_FLASH", "QWEN_MODEL_PLUS", "QWEN_MODEL_MAX"):
        monkeypatch.delenv(k, raising=False)


def test_defaults_match_documented_tiers(monkeypatch):
    _clear(monkeypatch)
    assert qwen_tier("flash") == "qwen3.6-flash"
    assert qwen_tier("plus") == "qwen3.7-plus"
    assert qwen_tier("max") == "qwen3.7-max"
    # Defaults equal the previously hard-coded strings → offline behavior unchanged.
    assert qwen_tier("flash") == QWEN_TIER_DEFAULTS["flash"]


def test_env_override_takes_precedence(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("QWEN_MODEL_FLASH", "qwen-turbo")
    monkeypatch.setenv("QWEN_MODEL_MAX", "qwen-max")
    assert qwen_tier("flash") == "qwen-turbo"
    assert qwen_tier("max") == "qwen-max"
    # An unset tier still falls back to its default.
    assert qwen_tier("plus") == "qwen3.7-plus"


def test_blank_override_falls_back_to_default(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("QWEN_MODEL_PLUS", "   ")  # whitespace-only → treated as unset
    assert qwen_tier("plus") == "qwen3.7-plus"


def test_unknown_tier_raises(monkeypatch):
    _clear(monkeypatch)
    with pytest.raises(ValueError):
        qwen_tier("turbo")
