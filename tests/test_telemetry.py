# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.tools.base import ToolResult

from sas_mcp_server.telemetry import (
    GOAL_SCHEMA,
    TelemetryMiddleware,
    install_telemetry,
)


class FakeLogger:
    """Captures records instead of writing to disk."""

    max_field_bytes = 4096
    max_result_bytes = 4096

    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(record)


def _build_server(box):
    mcp = FastMCP("test")

    @mcp.tool()
    def echo(text: str, count: int = 1) -> dict:
        box["received"] = {"text": text, "count": count}
        return {"echoed": text * count, "count": count}

    return mcp


# ----------------------------- OFF by default ------------------------------ #


def test_install_off_by_default(monkeypatch, tmp_path):
    import sas_mcp_server.config as config

    monkeypatch.setattr(config, "COLLECTION_MODE", False, raising=False)
    mcp = FastMCP("test")
    before = list(mcp.middleware)
    assert install_telemetry(mcp, "stdio") is None
    # A fresh FastMCP already carries its own default middleware, so assert the
    # list is UNCHANGED (no telemetry added) rather than empty.
    assert mcp.middleware == before
    assert not any(isinstance(m, TelemetryMiddleware) for m in mcp.middleware)


# ----------------------------- schema injection ---------------------------- #


@pytest.mark.asyncio
async def test_on_list_tools_injects_goal_first_and_required():
    box = {}
    mcp = _build_server(box)
    mcp.add_middleware(
        TelemetryMiddleware(FakeLogger(), require_goal=True, transport="stdio")
    )
    async with Client(mcp) as client:
        tools = await client.list_tools()
        schema = tools[0].inputSchema
        props = schema["properties"]
        assert list(props.keys())[0] == "goal"
        assert props["goal"] == GOAL_SCHEMA
        assert "goal" in schema["required"]
        assert "text" in props and "count" in props
        assert len(tools) == 1  # count unchanged -> coverage guard stays green


@pytest.mark.asyncio
async def test_on_list_tools_require_goal_false_omits_required():
    box = {}
    mcp = _build_server(box)
    mcp.add_middleware(
        TelemetryMiddleware(FakeLogger(), require_goal=False, transport="stdio")
    )
    async with Client(mcp) as client:
        schema = (await client.list_tools())[0].inputSchema
        assert "goal" in schema["properties"]
        assert "goal" not in schema.get("required", [])


@pytest.mark.asyncio
async def test_on_list_tools_idempotent():
    mw = TelemetryMiddleware(FakeLogger(), require_goal=True, transport="stdio")

    tool = SimpleNamespace(
        name="x",
        parameters={"type": "object", "properties": {"a": {"type": "string"}}},
    )

    def _copy(update):
        return SimpleNamespace(name="x", parameters=update["parameters"])

    tool.model_copy = _copy  # type: ignore[attr-defined]

    async def call_next(_):
        return [tool]

    out1 = await mw.on_list_tools(MagicMock(), call_next)
    assert list(out1[0].parameters["properties"].keys()) == ["goal", "a"]

    async def call_next2(_):
        return out1  # already injected

    out2 = await mw.on_list_tools(MagicMock(), call_next2)
    assert list(out2[0].parameters["properties"].keys()).count("goal") == 1


# ----------------------------- call + strip + log -------------------------- #


@pytest.mark.asyncio
async def test_on_call_tool_strips_goal_and_logs():
    box = {}
    logger = FakeLogger()
    mcp = _build_server(box)
    mcp.add_middleware(
        TelemetryMiddleware(logger, require_goal=True, transport="stdio")
    )
    async with Client(mcp) as client:
        res = await client.call_tool(
            "echo", {"goal": "user asked to echo", "text": "ab", "count": 2}
        )
    # underlying tool ran WITHOUT goal
    assert box["received"] == {"text": "ab", "count": 2}
    assert res.structured_content == {"echoed": "abab", "count": 2}
    # a well-formed record was written
    rec = logger.records[-1]
    assert rec["tool"] == "echo"
    assert rec["goal"] == "user asked to echo"
    assert "goal" not in rec["arguments"]
    assert rec["arguments"]["text"] == "ab"
    assert rec["result"] is not None
    assert rec["result_logged"] is True
    assert rec["session_id"]
    assert rec["ts"]
    assert rec["status"] == "success"
    assert isinstance(rec["duration_ms"], float)
    assert rec["transport"] == "stdio"
    assert "arguments_truncated" in rec and "result_truncated" in rec


@pytest.mark.asyncio
async def test_on_call_tool_records_redacted_and_truncated():
    logger = FakeLogger()
    logger.max_field_bytes = 50
    logger.max_result_bytes = 50
    mw = TelemetryMiddleware(logger, require_goal=True, transport="http")

    msg = SimpleNamespace(
        name="echo",
        arguments={"goal": "g", "password": "hunter2", "blob": "x" * 500},
    )
    msg.model_copy = lambda update: SimpleNamespace(
        name="echo", arguments=update["arguments"]
    )
    ctx = SimpleNamespace(message=msg, fastmcp_context=None)
    ctx.copy = lambda **kw: SimpleNamespace(
        message=kw["message"], fastmcp_context=None
    )

    async def call_next(_):
        return ToolResult(structured_content={"ok": True})

    await mw.on_call_tool(ctx, call_next)
    rec = logger.records[-1]
    # object type preserved even when truncated
    assert isinstance(rec["arguments"], dict)
    assert rec["arguments"]["password"] == "[REDACTED]"
    assert rec["arguments_truncated"] is True


@pytest.mark.asyncio
async def test_goal_is_redacted_and_bounded():
    logger = FakeLogger()
    mw = TelemetryMiddleware(logger, require_goal=True, transport="stdio")
    jwt = "eyJ" + "a" * 40

    msg = SimpleNamespace(
        name="echo", arguments={"goal": f"rerun with Bearer {jwt}", "text": "q"}
    )
    msg.model_copy = lambda update: SimpleNamespace(
        name="echo", arguments=update["arguments"]
    )
    ctx = SimpleNamespace(message=msg, fastmcp_context=None)
    ctx.copy = lambda **kw: SimpleNamespace(
        message=kw["message"], fastmcp_context=None
    )

    async def call_next(_):
        return ToolResult(structured_content={"ok": True})

    await mw.on_call_tool(ctx, call_next)
    rec = logger.records[-1]
    assert "[REDACTED]" in rec["goal"] and jwt not in rec["goal"]


@pytest.mark.asyncio
async def test_log_results_false_records_shape_only():
    logger = FakeLogger()
    mw = TelemetryMiddleware(
        logger, require_goal=True, transport="stdio", log_results=False
    )

    msg = SimpleNamespace(name="q", arguments={"goal": "g", "table": "t"})
    msg.model_copy = lambda update: SimpleNamespace(
        name="q", arguments=update["arguments"]
    )
    ctx = SimpleNamespace(message=msg, fastmcp_context=None)
    ctx.copy = lambda **kw: SimpleNamespace(
        message=kw["message"], fastmcp_context=None
    )

    async def call_next(_):
        # structured_content must be a dict for a real ToolResult; wrap the
        # PII-bearing rows so we can assert the shape summary omits them.
        return ToolResult(
            structured_content={"rows": [{"ssn": "123-45-6789", "email": "a@b.com"}]}
        )

    await mw.on_call_tool(ctx, call_next)
    rec = logger.records[-1]
    assert rec["result_logged"] is False
    assert "123-45-6789" not in json.dumps(rec["result"])
    assert rec["result"]["_type"] == "object"


@pytest.mark.asyncio
async def test_on_call_tool_error_records_and_reraises():
    logger = FakeLogger()
    mw = TelemetryMiddleware(logger, require_goal=True, transport="stdio")

    msg = SimpleNamespace(name="boom", arguments={"goal": "g"})
    msg.model_copy = lambda update: SimpleNamespace(
        name="boom", arguments=update["arguments"]
    )
    ctx = SimpleNamespace(message=msg, fastmcp_context=None)
    ctx.copy = lambda **kw: SimpleNamespace(
        message=kw["message"], fastmcp_context=None
    )

    async def call_next(_):
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        await mw.on_call_tool(ctx, call_next)

    rec = logger.records[-1]
    assert rec["status"] == "error"
    assert rec["is_error"] is True
    assert rec["error"] == "ValueError: kaboom"


@pytest.mark.asyncio
async def test_logger_failure_never_breaks_call():
    class Exploding:
        max_field_bytes = 4096
        max_result_bytes = 4096

        def write(self, record):
            raise RuntimeError("disk full")

    mw = TelemetryMiddleware(Exploding(), require_goal=True, transport="stdio")
    msg = SimpleNamespace(name="echo", arguments={"goal": "g", "text": "q"})
    msg.model_copy = lambda update: SimpleNamespace(
        name="echo", arguments=update["arguments"]
    )
    ctx = SimpleNamespace(message=msg, fastmcp_context=None)
    seen = {}

    def _copy(**kw):
        return SimpleNamespace(message=kw["message"], fastmcp_context=None)

    ctx.copy = _copy

    async def call_next(c):
        seen["args"] = dict(c.message.arguments)
        return ToolResult(structured_content={"ok": True})

    res = await mw.on_call_tool(ctx, call_next)
    assert res.structured_content == {"ok": True}
    assert "goal" not in seen["args"]


def test_session_fallback_when_no_context():
    mw = TelemetryMiddleware(FakeLogger(), require_goal=True, transport="stdio")
    ctx = SimpleNamespace(fastmcp_context=None)
    assert mw._resolve_session(ctx) == mw._proc_session


def test_session_fallback_when_session_id_raises():
    mw = TelemetryMiddleware(FakeLogger(), require_goal=True, transport="stdio")

    class RaisingFC:
        @property
        def session_id(self):
            raise RuntimeError("no session")

    ctx = SimpleNamespace(fastmcp_context=RaisingFC())
    assert mw._resolve_session(ctx) == mw._proc_session


def test_extract_output_prefers_structured_then_content():
    mw = TelemetryMiddleware(FakeLogger(), require_goal=True, transport="stdio")
    # structured_content wins
    r1 = SimpleNamespace(structured_content={"a": 1}, content=None)
    assert mw._extract_output(r1) == {"a": 1}
    # falls back to joined text of content blocks
    blocks = [SimpleNamespace(text="line1"), SimpleNamespace(text="line2")]
    r2 = SimpleNamespace(structured_content=None, content=blocks)
    assert mw._extract_output(r2) == "line1\nline2"
    # nothing extractable -> None
    r3 = SimpleNamespace(structured_content=None, content=None)
    assert mw._extract_output(r3) is None


# --------------------- end-to-end install + on-disk write ------------------- #


@pytest.mark.asyncio
async def test_install_enabled_end_to_end_writes_jsonl(monkeypatch, tmp_path):
    """install_telemetry() with COLLECTION_MODE on wires a real logger and a
    call produces one JSONL record on disk (exercises the actual entry point)."""
    import sas_mcp_server.config as config

    log_path = tmp_path / "sub" / "tool-usage.log"
    monkeypatch.setattr(config, "COLLECTION_MODE", True, raising=False)
    monkeypatch.setattr(config, "COLLECTION_LOG_PATH", str(log_path), raising=False)
    monkeypatch.setattr(config, "COLLECTION_REQUIRE_GOAL", True, raising=False)
    monkeypatch.setattr(config, "COLLECTION_LOG_RESULTS", True, raising=False)

    box = {}
    mcp = _build_server(box)
    mw = install_telemetry(mcp, "stdio")
    assert mw is not None
    assert any(isinstance(m, TelemetryMiddleware) for m in mcp.middleware)

    async with Client(mcp) as client:
        await client.call_tool("echo", {"goal": "why echo", "text": "hi", "count": 1})

    # underlying tool ran without goal
    assert box["received"] == {"text": "hi", "count": 1}
    lines = [
        json.loads(x)
        for x in log_path.read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["tool"] == "echo"
    assert rec["goal"] == "why echo"
    assert "goal" not in rec["arguments"]
    assert rec["arguments"]["text"] == "hi"
    assert rec["result_logged"] is True
    assert rec["status"] == "success"
    assert rec["transport"] == "stdio"
    assert rec["session_id"]


def test_install_disabled_log_path_unusable_returns_none(monkeypatch, tmp_path):
    """If the log path can't be opened, install_telemetry disables telemetry
    (returns None) rather than breaking the server."""
    import sas_mcp_server.config as config

    monkeypatch.setattr(config, "COLLECTION_MODE", True, raising=False)
    # Point at a path whose parent is a FILE, so mkdir/open fails with OSError.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(
        config, "COLLECTION_LOG_PATH", str(blocker / "nested" / "x.log"), raising=False
    )
    mcp = FastMCP("test")
    before = list(mcp.middleware)
    assert install_telemetry(mcp, "stdio") is None
    assert mcp.middleware == before
