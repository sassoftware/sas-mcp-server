# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import json

from sas_mcp_server.usage_logger import (
    UsageLogger,
    bounded_redact,
    redact,
    resolve_log_path,
    truncate,
)


def test_redact_masks_secret_keys():
    r = redact(
        {
            "password": "hunter2",
            "authorization": "Bearer abc",
            "api_key": "k",
            "refresh_token": "rt",
            "safe": "keep-me",
        }
    )
    assert r["password"] == "[REDACTED]"
    assert r["authorization"] == "[REDACTED]"
    assert r["api_key"] == "[REDACTED]"
    assert r["refresh_token"] == "[REDACTED]"
    assert r["safe"] == "keep-me"


def test_redact_scrubs_inline_jwt_and_bearer():
    jwt = "eyJ" + "a" * 40
    r = redact({"note": f"header Bearer xyz and {jwt} inside"})
    assert "[REDACTED]" in r["note"]
    assert jwt not in r["note"]
    assert "Bearer xyz" not in r["note"]


def test_redact_recurses_into_nested_and_lists():
    r = redact({"outer": {"secret": "s"}, "items": [{"token": "t"}, "plain"]})
    assert r["outer"]["secret"] == "[REDACTED]"
    assert r["items"][0]["token"] == "[REDACTED]"
    assert r["items"][1] == "plain"


def test_truncate_caps_and_flags():
    value, truncated = truncate("x" * 5000, 100)
    assert truncated is True
    assert "truncated" in value
    assert len(value.encode("utf-8")) < 5000


def test_truncate_leaves_small_values():
    value, truncated = truncate("short", 100)
    assert truncated is False
    assert value == "short"


def test_truncate_none():
    assert truncate(None, 100) == (None, False)


# ---------------------------- bounded_redact ------------------------------- #


def test_bounded_redact_masks_and_scrubs():
    jwt = "eyJ" + "a" * 40
    v, trunc = bounded_redact(
        {"password": "hunter2", "note": f"use Bearer {jwt} now"}, 4096
    )
    assert v["password"] == "[REDACTED]"
    assert "[REDACTED]" in v["note"] and jwt not in v["note"]
    assert trunc is False


def test_bounded_redact_clips_huge_string_without_full_scan():
    big = "z" * (25 * 1024 * 1024)  # ~25 MiB (export_report shape)
    v, trunc = bounded_redact({"safe": "keep", "blob": big}, 4096)
    assert trunc is True
    assert isinstance(v, dict)  # object type preserved, not a bare string
    assert v["safe"] == "keep"  # small field ordered first is captured
    assert isinstance(v["blob"], str) and len(v["blob"]) < 10_000


def test_bounded_redact_bounds_million_row_list():
    rows = [{"id": i, "email": f"u{i}@x.com"} for i in range(1_000_000)]
    v, trunc = bounded_redact(rows, 4096)
    assert isinstance(v, list)  # stays a list
    assert trunc is True
    assert len(v) < 5000  # bounded prefix only


def test_bounded_redact_small_value_unchanged_and_object_typed():
    v, trunc = bounded_redact({"sas_code": "data _null_;"}, 4096)
    assert v == {"sas_code": "data _null_;"}
    assert trunc is False


def test_resolve_log_path_creates_parent(tmp_path):
    target = tmp_path / "a" / "b" / "tool-usage.log"
    resolved = resolve_log_path(str(target))
    assert resolved.parent.exists()


def test_resolve_log_path_hardens_only_dirs_it_creates(tmp_path, monkeypatch):
    """We lock down a directory we create, but never a pre-existing (possibly
    shared) parent such as ~ or the system temp dir — stripping its ACL
    inheritance would break newly-created sibling directories on Windows."""
    import sas_mcp_server.usage_logger as usage_logger

    hardened: list = []
    monkeypatch.setattr(usage_logger, "_win_harden", hardened.append)

    # A parent that already exists must be left untouched.
    existing = tmp_path / "existing"
    existing.mkdir()
    resolve_log_path(str(existing / "tool-usage.log"))
    assert existing not in hardened

    # A parent we create ourselves is hardened.
    fresh = tmp_path / "fresh"
    resolve_log_path(str(fresh / "tool-usage.log"))
    assert fresh in hardened


def test_win_harden_noop_off_windows(tmp_path, monkeypatch):
    """On non-Windows, _win_harden returns immediately without shelling out."""
    import sas_mcp_server.usage_logger as usage_logger

    calls: list = []
    monkeypatch.setattr(usage_logger.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(usage_logger.sys, "platform", "linux")
    usage_logger._win_harden(tmp_path / "tool-usage.log")
    assert calls == []


def test_win_harden_noop_without_username(tmp_path, monkeypatch):
    """On Windows with no USERNAME, _win_harden bails before calling icacls."""
    import sas_mcp_server.usage_logger as usage_logger

    calls: list = []
    monkeypatch.setattr(usage_logger.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(usage_logger.sys, "platform", "win32")
    monkeypatch.delenv("USERNAME", raising=False)
    usage_logger._win_harden(tmp_path / "tool-usage.log")
    assert calls == []


def test_from_config_builds_working_logger(tmp_path):
    """The from_config classmethod constructs a UsageLogger that writes."""
    log = tmp_path / "tool-usage.log"
    lg = UsageLogger.from_config(
        path=str(log),
        max_log_bytes=10_000,
        backup_count=1,
        max_field_bytes=1024,
    )
    lg.write({"ok": True})
    assert log.exists()
    assert '"ok": true' in log.read_text(encoding="utf-8")


def test_write_emits_one_valid_json_line_no_bom(tmp_path):
    log = tmp_path / "tool-usage.log"
    ul = UsageLogger(
        str(log), max_log_bytes=10_000, backup_count=1, max_field_bytes=4096
    )
    ul.write({"tool": "echo", "n": 1})
    ul.write({"tool": "echo", "n": 2})
    data = log.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")  # no BOM
    assert data.endswith(b"\n")
    lines = [json.loads(x) for x in data.decode("utf-8").splitlines() if x.strip()]
    assert len(lines) == 2
    assert lines[0]["tool"] == "echo"


def test_usage_logger_has_result_cap(tmp_path):
    log = tmp_path / "tool-usage.log"
    ul = UsageLogger(
        str(log),
        max_log_bytes=10_000,
        backup_count=1,
        max_field_bytes=16384,
        max_result_bytes=8192,
    )
    assert ul.max_field_bytes == 16384
    assert ul.max_result_bytes == 8192


def test_result_cap_defaults_to_field_cap(tmp_path):
    log = tmp_path / "tool-usage.log"
    ul = UsageLogger(
        str(log), max_log_bytes=10_000, backup_count=1, max_field_bytes=4096
    )
    assert ul.max_result_bytes == 4096


def test_write_swallows_errors(tmp_path):
    log = tmp_path / "tool-usage.log"
    ul = UsageLogger(
        str(log), max_log_bytes=10_000, backup_count=1, max_field_bytes=4096
    )
    # Force an internal failure; write() must not raise.
    ul._logger = None  # type: ignore[assignment]
    ul.write({"tool": "echo"})  # returns None, no exception
