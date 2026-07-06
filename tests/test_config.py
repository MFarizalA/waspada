"""Config + lane-switch behavior (waspada/config.py)."""

from __future__ import annotations

import importlib

import pytest

from waspada import config as config_mod
from waspada.config import Config, load_config


def test_defaults_when_env_missing():
    """With no env, load_config returns empty BQ fields + lane=collections."""
    cfg = load_config()
    assert cfg == Config(
        bq_project="",
        bq_dataset="",
        bq_table="",
        lane="collections",
    )


def test_lane_switch_collections(monkeypatch):
    monkeypatch.setenv("WASPADA_LANE", "collections")
    assert load_config().lane == "collections"


def test_lane_switch_origination(monkeypatch):
    monkeypatch.setenv("WASPADA_LANE", "origination")
    assert load_config().lane == "origination"


def test_lane_invalid_raises(monkeypatch):
    monkeypatch.setenv("WASPADA_LANE", "bogus")
    with pytest.raises(ValueError):
        load_config()


def test_lane_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("WASPADA_LANE", "  Origination  ")
    assert load_config().lane == "origination"


def test_env_vars_flow_through(monkeypatch):
    monkeypatch.setenv("BQ_PROJECT", "proj")
    monkeypatch.setenv("BQ_DATASET", "ds")
    monkeypatch.setenv("BQ_TABLE", "tbl")
    cfg = load_config()
    assert cfg.bq_project == "proj"
    assert cfg.bq_dataset == "ds"
    assert cfg.bq_table == "tbl"


def test_module_level_config_reloadable(monkeypatch):
    """Importing the module picks up env set before reload (no .env present)."""
    monkeypatch.setenv("WASPADA_LANE", "origination")
    importlib.reload(config_mod)
    try:
        assert config_mod.config.lane == "origination"
    finally:
        # restore the module-level config to default for other tests
        monkeypatch.delenv("WASPADA_LANE", raising=False)
        importlib.reload(config_mod)
