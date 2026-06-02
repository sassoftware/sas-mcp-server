# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the env_bool helper."""
from sas_mcp_server.env import env_bool


def test_env_bool_unset_returns_default(monkeypatch):
    monkeypatch.delenv("X_FLAG", raising=False)
    assert env_bool("X_FLAG", True) is True
    assert env_bool("X_FLAG", False) is False


def test_env_bool_truthy_values(monkeypatch):
    for value in ("true", "1", "yes", "on", "TRUE", "On"):
        monkeypatch.setenv("X_FLAG", value)
        assert env_bool("X_FLAG", False) is True


def test_env_bool_falsey_values(monkeypatch):
    for value in ("false", "0", "no", "off", "FALSE"):
        monkeypatch.setenv("X_FLAG", value)
        assert env_bool("X_FLAG", True) is False


def test_env_bool_unrecognized_returns_default(monkeypatch):
    monkeypatch.setenv("X_FLAG", "maybe")
    assert env_bool("X_FLAG", True) is True
    assert env_bool("X_FLAG", False) is False
