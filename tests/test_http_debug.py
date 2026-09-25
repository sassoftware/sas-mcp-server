# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the opt-in HTTP debug trace (``HTTP_DEBUG``).

Config tests reload ``sas_mcp_server.config`` in place, as ``test_config.py``
does (see its docstring for why reload rather than re-import). Tracer tests
drive a real :class:`httpx.AsyncClient` over :class:`httpx.MockTransport`, so
the hooks run exactly where httpx calls them.
"""

import importlib
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from dotenv import load_dotenv as real_load_dotenv

from sas_mcp_server import http_debug
from sas_mcp_server.http_debug import (
    HttpDebugTracer,
    body_preview,
    install_http_debug,
    redact_headers,
    redact_url,
    uninstall_http_debug,
)
from sas_mcp_server.usage_logger import UsageLogger

_HTTP_DEBUG_VARS = (
    "HTTP_DEBUG",
    "HTTP_DEBUG_LOG_PATH",
    "HTTP_DEBUG_MAX_BODY_BYTES",
    "HTTP_DEBUG_MAX_LOG_BYTES",
    "HTTP_DEBUG_LOG_BACKUPS",
)


@pytest.fixture(autouse=True)
def _no_tracer_leaks():
    """A tracer left installed would trace every later test's make_client."""
    uninstall_http_debug()
    yield
    uninstall_http_debug()


def _reload_config():
    if "sas_mcp_server.config" in sys.modules:
        return importlib.reload(sys.modules["sas_mcp_server.config"])
    import sas_mcp_server.config as cfg

    return cfg


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _tracer(tmp_path: Path, max_body_bytes: int = 4096) -> tuple[HttpDebugTracer, Path]:
    log = tmp_path / "http-debug.log"
    writer = UsageLogger(
        str(log), max_log_bytes=1_000_000, backup_count=1, max_field_bytes=max_body_bytes
    )
    return HttpDebugTracer(writer, max_body_bytes=max_body_bytes), log


def _client(tracer: HttpDebugTracer, handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        event_hooks=tracer.event_hooks(),
        headers={"Authorization": "Bearer secret-token-value"},
    )


# ------------------------------- configuration ------------------------------ #


def test_http_debug_defaults_off(monkeypatch):
    monkeypatch.setenv("VIYA_ENDPOINT", "https://test.viya.com")
    for name in _HTTP_DEBUG_VARS:
        monkeypatch.delenv(name, raising=False)
    with patch("dotenv.load_dotenv"):
        cfg = _reload_config()
    assert cfg.HTTP_DEBUG is False
    assert cfg.HTTP_DEBUG_LOG_PATH == "~/.sas-mcp-server/http-debug.log"
    assert cfg.HTTP_DEBUG_MAX_BODY_BYTES == 4096
    assert cfg.HTTP_DEBUG_MAX_LOG_BYTES == 10 * 1024 * 1024
    assert cfg.HTTP_DEBUG_LOG_BACKUPS == 3


def test_http_debug_settings_are_read_from_the_env_file(monkeypatch, tmp_path):
    """The flag and the trace path come from .env, through config's load_dotenv."""
    monkeypatch.setenv("VIYA_ENDPOINT", "https://test.viya.com")
    for name in _HTTP_DEBUG_VARS:
        monkeypatch.delenv(name, raising=False)
    trace_path = tmp_path / "traces" / "viya-http.log"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HTTP_DEBUG=true\n"
        f"HTTP_DEBUG_LOG_PATH={trace_path}\n"
        "HTTP_DEBUG_MAX_BODY_BYTES=512\n"
        "HTTP_DEBUG_MAX_LOG_BYTES=2048\n"
        "HTTP_DEBUG_LOG_BACKUPS=5\n",
        encoding="utf-8",
    )
    with patch("dotenv.load_dotenv", lambda *a, **k: real_load_dotenv(env_file)):
        cfg = _reload_config()
    try:
        assert cfg.HTTP_DEBUG is True
        assert str(trace_path) == cfg.HTTP_DEBUG_LOG_PATH
        assert cfg.HTTP_DEBUG_MAX_BODY_BYTES == 512
        assert cfg.HTTP_DEBUG_MAX_LOG_BYTES == 2048
        assert cfg.HTTP_DEBUG_LOG_BACKUPS == 5

        assert install_http_debug() is not None
        assert trace_path.exists()  # the file named in .env, created at install
    finally:
        # load_dotenv wrote to os.environ behind monkeypatch's back: drop the
        # values and reload, so later tests see HTTP_DEBUG off again.
        for name in _HTTP_DEBUG_VARS:
            monkeypatch.delenv(name, raising=False)
        with patch("dotenv.load_dotenv"):
            _reload_config()


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", "bogus"])
def test_http_debug_off_spellings(monkeypatch, raw):
    monkeypatch.setenv("VIYA_ENDPOINT", "https://test.viya.com")
    monkeypatch.setenv("HTTP_DEBUG", raw)
    with patch("dotenv.load_dotenv"):
        cfg = _reload_config()
    assert cfg.HTTP_DEBUG is False


# ------------------------------- installation ------------------------------- #


def test_install_off_returns_none_and_clients_have_no_hooks(monkeypatch):
    import sas_mcp_server.config as config
    from sas_mcp_server.viya_client import make_client

    monkeypatch.setattr(config, "HTTP_DEBUG", False)
    assert install_http_debug() is None
    assert http_debug.event_hooks() is None
    client = make_client("tok")
    assert client.event_hooks == {"request": [], "response": []}


async def test_install_on_opens_file_warns_and_hooks_make_client(
    monkeypatch, tmp_path, caplog
):
    import sas_mcp_server.config as config
    from sas_mcp_server.viya_client import make_client

    log = tmp_path / "sub" / "http-debug.log"
    monkeypatch.setattr(config, "HTTP_DEBUG", True)
    monkeypatch.setattr(config, "HTTP_DEBUG_LOG_PATH", str(log))
    with caplog.at_level(logging.WARNING, logger="sas_mcp_server.http_debug"):
        tracer = install_http_debug()
    assert tracer is not None
    assert log.exists()
    assert any("HTTP_DEBUG is on" in r.getMessage() for r in caplog.records)

    async with make_client("tok") as client:
        assert client.event_hooks["request"] == [tracer.on_request]
        assert client.event_hooks["response"] == [tracer.on_response]


def test_install_with_unusable_path_disables_tracing(monkeypatch, tmp_path, caplog):
    """Like collection mode: a bad path turns tracing off, never the server."""
    import sas_mcp_server.config as config

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(config, "HTTP_DEBUG", True)
    monkeypatch.setattr(
        config, "HTTP_DEBUG_LOG_PATH", str(blocker / "nested" / "x.log")
    )
    with caplog.at_level(logging.WARNING, logger="sas_mcp_server.http_debug"):
        assert install_http_debug() is None
    assert http_debug.event_hooks() is None
    assert any("HTTP tracing disabled" in r.getMessage() for r in caplog.records)


# --------------------------------- tracing ---------------------------------- #


async def test_request_and_response_are_recorded_with_one_id(tmp_path):
    tracer, log = _tracer(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"id": "job-1", "state": "running"},
            headers={"Set-Cookie": "JSESSIONID=abc"},
        )

    async with _client(tracer, handler) as client:
        resp = await client.post(
            "https://viya.example.com/compute/sessions/s1/jobs",
            json={"code": "data _null_; run;"},
            headers={"Accept": "application/vnd.sas.compute.job+json"},
        )
    # The caller still gets the body the hook already read.
    assert resp.json() == {"id": "job-1", "state": "running"}

    req, res = _records(log)
    assert req["event"] == "request" and res["event"] == "response"
    assert req["id"] == res["id"] == 1
    assert req["method"] == "POST"
    assert req["url"] == "https://viya.example.com/compute/sessions/s1/jobs"
    assert req["headers"]["accept"] == "application/vnd.sas.compute.job+json"
    assert req["headers"]["authorization"] == "[REDACTED]"
    assert req["body"] == {"code": "data _null_; run;"}
    assert res["status"] == 201
    assert isinstance(res["elapsed_ms"], float)
    assert res["body"] == {"id": "job-1", "state": "running"}
    assert res["headers"]["set-cookie"] == "[REDACTED]"
    assert "secret-token-value" not in log.read_text(encoding="utf-8")


async def test_ids_increase_across_requests(tmp_path):
    tracer, log = _tracer(tmp_path)
    async with _client(tracer, lambda r: httpx.Response(204)) as client:
        await client.get("https://viya.example.com/a")
        await client.get("https://viya.example.com/b")
    assert [r["id"] for r in _records(log)] == [1, 1, 2, 2]


async def test_secrets_in_query_and_body_are_redacted(tmp_path):
    tracer, log = _tracer(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "eyJhbGciOiJSUzI1NiJ9.payload.signature", "n": 1}
        )

    async with _client(tracer, handler) as client:
        await client.post(
            "https://viya.example.com/x",
            params={"limit": "5", "api_key": "k-123"},
            json={"user": "sasdemo", "password": "pw-123", "note": "Bearer abc.def"},
        )
    req, res = _records(log)
    assert "limit=5" in req["url"]
    assert "k-123" not in req["url"]
    assert req["body"]["password"] == "[REDACTED]"
    assert req["body"]["user"] == "sasdemo"
    assert "abc.def" not in json.dumps(req["body"])
    assert res["body"]["access_token"] == "[REDACTED]"
    assert res["body"]["n"] == 1


async def test_form_body_keys_are_redacted(tmp_path):
    tracer, log = _tracer(tmp_path)
    async with _client(tracer, lambda r: httpx.Response(200)) as client:
        await client.post(
            "https://viya.example.com/form",
            data={"grant_type": "refresh_token", "refresh_token": "r-999"},
        )
    req = _records(log)[0]
    assert req["body"]["grant_type"] == "refresh_token"
    assert req["body"]["refresh_token"] == "[REDACTED]"


async def test_large_body_is_capped_and_flagged(tmp_path):
    tracer, log = _tracer(tmp_path, max_body_bytes=64)
    listing = "x" * 5000
    async with _client(
        tracer, lambda r: httpx.Response(200, text=listing, headers={"Content-Type": "text/plain"})
    ) as client:
        resp = await client.get("https://viya.example.com/log")
    assert resp.text == listing  # the caller is unaffected by the cap
    res = _records(log)[1]
    assert res["body_truncated"] is True
    assert len(res["body"]) < 200


async def test_binary_body_is_described_not_recorded(tmp_path):
    tracer, log = _tracer(tmp_path)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    async with _client(
        tracer, lambda r: httpx.Response(200, content=png, headers={"Content-Type": "image/png"})
    ) as client:
        resp = await client.get("https://viya.example.com/img")
    assert resp.content == png
    assert _records(log)[1]["body"] == "<108 bytes of image/png>"


async def test_zero_body_cap_records_no_bodies(tmp_path):
    tracer, log = _tracer(tmp_path, max_body_bytes=0)
    async with _client(tracer, lambda r: httpx.Response(200, json={"a": 1})) as client:
        await client.post("https://viya.example.com/x", json={"secret_sauce": 1})
    req, res = _records(log)
    assert "not recorded" in req["body"]
    assert "not recorded" in res["body"]


async def test_multipart_upload_is_not_buffered(tmp_path):
    tracer, log = _tracer(tmp_path)
    seen: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(await request.aread())
        return httpx.Response(201)

    async with _client(tracer, handler) as client:
        await client.post(
            "https://viya.example.com/files/files",
            files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")},
        )
    assert b"a,b" in seen[0]  # the upload itself went through intact
    assert _records(log)[0]["body"] == "<streamed body, not recorded>"


async def test_transport_failure_leaves_a_request_without_response(tmp_path):
    """The case a trace exists for: the error still propagates unchanged."""
    tracer, log = _tracer(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _client(tracer, handler) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get("https://viya.example.com/down")
    records = _records(log)
    assert [r["event"] for r in records] == ["request"]


async def test_hooks_never_raise(tmp_path):
    class BrokenWriter:
        def write(self, record):
            raise RuntimeError("disk full")

    tracer = HttpDebugTracer(BrokenWriter(), max_body_bytes=100)
    async with _client(tracer, lambda r: httpx.Response(200, json={"ok": True})) as client:
        resp = await client.get("https://viya.example.com/x")
    assert resp.json() == {"ok": True}


async def test_response_without_request_record_still_logs(tmp_path):
    """A response whose request skipped on_request (no extension) is recorded
    with a null id and elapsed time rather than dropped."""
    tracer, log = _tracer(tmp_path)
    response = httpx.Response(
        200, json={"a": 1}, request=httpx.Request("GET", "https://viya.example.com/x")
    )
    await tracer.on_response(response)
    rec = _records(log)[0]
    assert rec["id"] is None and rec["elapsed_ms"] is None
    assert rec["body"] == {"a": 1}


# --------------------------------- helpers ---------------------------------- #


def test_redact_headers_masks_credentials_only():
    headers = httpx.Headers(
        {"Authorization": "Bearer x", "Cookie": "c=1", "X-Api-Key": "k", "Accept": "a/b"}
    )
    out = redact_headers(headers)
    assert out["authorization"] == out["cookie"] == out["x-api-key"] == "[REDACTED]"
    assert out["accept"] == "a/b"


def test_redact_url_without_query_is_unchanged():
    url = httpx.URL("https://viya.example.com/reports/reports/abc")
    assert redact_url(url) == "https://viya.example.com/reports/reports/abc"


def test_body_preview_empty_and_invalid_json():
    assert body_preview(b"", "application/json", 100) == (None, False)
    assert body_preview(b"not json", "application/json", 100) == ("not json", False)


# ------------------------- review findings on PR #64 ------------------------ #


async def test_a_read_that_fails_mid_body_raises_the_real_error(tmp_path):
    """httpx runs the response hook before its own read. A hook that swallowed
    a mid-body failure would leave the stream consumed and the caller with a
    StreamConsumed instead of the timeout it is debugging."""
    tracer, log = _tracer(tmp_path)

    class Stalls(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"partial":'
            raise httpx.ReadTimeout("stalled mid-body")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=Stalls())

    async with _client(tracer, handler) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.get("https://viya.example.com/compute/sessions/s1/jobs/j1/log")
    req, res = _records(log)
    assert req["event"] == "request" and res["event"] == "response"
    assert res["status"] == 200
    assert res["error"].startswith("ReadTimeout")
    assert res["body"] == "<body not received>"


async def test_url_is_recorded_as_sent_when_nothing_is_masked(tmp_path):
    """Rebuilding the query would percent-encode what httpx sends raw, and a
    traced URL that differs from the wire cannot reproduce the failure."""
    tracer, log = _tracer(tmp_path)
    url = "https://viya.example.com/reports/reports?filter=eq(name,'Sales')&limit=5"
    async with _client(tracer, lambda r: httpx.Response(200)) as client:
        await client.get(url)
    assert _records(log)[0]["url"] == url
    # Masking still rebuilds, and masks only the credential-shaped key.
    masked = redact_url(httpx.URL(url + "&access_token=abc"))
    assert "abc" not in masked and "limit=5" in masked


def test_oversized_body_is_clipped_without_being_parsed():
    """A 100 MiB CSV upload or a million-row JSON result is not decoded or
    parsed whole on the event loop to keep 4 KiB of it."""
    big = b'{"rows": [' + b'"x",' * (2 * 1024 * 1024 // 4) + b'"x"]}'
    with patch("sas_mcp_server.http_debug.json.loads") as loads:
        value, truncated = body_preview(big, "application/json", 64)
    loads.assert_not_called()
    assert truncated is True
    assert isinstance(value, str) and value.startswith('{"rows": [')
    assert len(value) < 200


async def test_writes_are_offloaded_from_the_event_loop(tmp_path):
    tracer, _ = _tracer(tmp_path)
    with patch("sas_mcp_server.http_debug.anyio.to_thread.run_sync") as run_sync:
        async with _client(tracer, lambda r: httpx.Response(204)) as client:
            await client.get("https://viya.example.com/a")
    assert run_sync.call_count == 2
    assert all(call.args[0] == tracer.writer.write for call in run_sync.call_args_list)


def test_install_is_idempotent_and_uninstall_releases_the_file(monkeypatch, tmp_path):
    """A second install must not open a second handle on the same file, and
    uninstall must close it: two live handles break rollover on Windows, and
    a test's tmp_path could not be removed."""
    import sas_mcp_server.config as config

    log = tmp_path / "http-debug.log"
    monkeypatch.setattr(config, "HTTP_DEBUG", True)
    monkeypatch.setattr(config, "HTTP_DEBUG_LOG_PATH", str(log))
    first = install_http_debug()
    assert install_http_debug() is first
    assert first is not None
    assert len(first.writer._logger.handlers) == 1
    uninstall_http_debug()
    assert first.writer._logger.handlers == []
    assert http_debug.event_hooks() is None
    log.unlink()  # closed: removable on Windows too
