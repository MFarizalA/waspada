"""Config + lane-switch behavior (waspada/config.py)."""

from __future__ import annotations

import importlib

import pytest

from waspada import config as config_mod
from waspada.config import Config, load_config


def test_defaults_when_env_missing(monkeypatch):
    """With no env, load_config returns empty OSS fields + lane=collections."""
    for var in (
        "OSS_BUCKET", "OSS_RAW_BUCKET", "OSS_STAGING_BUCKET", "OSS_MART_BUCKET",
        "OSS_ENDPOINT", "OSS_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config()
    # Compare field-by-field (not ==) — importlib.reload in another test can
    # create a stale Config class reference, breaking identity-based __eq__.
    assert cfg.lane == "collections"
    assert cfg.oss_raw_bucket == ""
    assert cfg.oss_staging_bucket == ""
    assert cfg.oss_mart_bucket == ""
    assert cfg.oss_endpoint == ""
    assert cfg.oss_key == ""


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
    monkeypatch.setenv("OSS_RAW_BUCKET", "waspada-prod-raw")
    monkeypatch.setenv("OSS_ENDPOINT", "oss-ap-southeast-1.aliyuncs.com")
    monkeypatch.setenv("OSS_KEY", "loans.parquet")
    cfg = load_config()
    assert cfg.oss_raw_bucket == "waspada-prod-raw"
    assert cfg.oss_endpoint == "oss-ap-southeast-1.aliyuncs.com"
    assert cfg.oss_key == "loans.parquet"


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
