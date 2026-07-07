"""Config + lane-switch behavior (waspada/config.py)."""

from __future__ import annotations

import importlib

import pytest

from waspada import config as config_mod
from waspada.config import Config, load_config


def test_defaults_when_env_missing():
    """With no env, load_config returns empty OSS fields + lane=collections."""
    cfg = load_config()
    assert cfg == Config(
        oss_bucket="",
        oss_endpoint="",
        oss_key="",
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
    monkeypatch.setenv("OSS_BUCKET", "bucket")
    monkeypatch.setenv("OSS_ENDPOINT", "oss-ap-southeast-1.aliyuncs.com")
    monkeypatch.setenv("OSS_KEY", "collections/loans.parquet")
    cfg = load_config()
    assert cfg.oss_bucket == "bucket"
    assert cfg.oss_endpoint == "oss-ap-southeast-1.aliyuncs.com"
    assert cfg.oss_key == "collections/loans.parquet"


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
