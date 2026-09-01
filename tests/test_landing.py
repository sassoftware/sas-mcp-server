# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the browser landing page served on the MCP endpoint
(``sas_mcp_server.landing`` and its wiring in ``mcp_server``)."""

import json
import re

import httpx
import pytest
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from sas_mcp_server import landing, mcp_server, tools
from sas_mcp_server.landing import (
    ClientSnippet,
    LandingPageMiddleware,
    PromptEntry,
    ServerFacts,
    TierGroup,
    ToolEntry,
    client_snippets,
    collect_facts,
    render_page,
    summarize,
    wants_html,
)

MCP_URL = "https://mcp.example.com/mcp"


def _facts(**overrides) -> ServerFacts:
    base = dict(
        server_name="Test <Server>",
        version="9.9.9",
        mcp_url=MCP_URL,
        viya_endpoint="https://viya.example.com",
        auth_enabled=True,
        allow_raw_bearer=False,
        enabled_tiers=frozenset(tools.ALL_TIERS),
        read_only=False,
        tiers=(
            TierGroup(
                tier=0,
                title="Compute",
                tools=(ToolEntry("execute_sas_code", "Run <b>SAS</b> code."),),
            ),
            TierGroup(tier=3, title="Reports", tools=(ToolEntry("list_reports", "List reports."),)),
        ),
        prompts=(PromptEntry("debug_sas_log", "Analyze a log."),),
    )
    base.update(overrides)
    return ServerFacts(**base)


# ---------------------------------------------------------------------------
# wants_html — the routing decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "accept",
    [
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",  # Chrome/Edge
        "text/html",
        "TEXT/HTML; charset=utf-8",
        "application/xhtml+xml",
    ],
)
def test_wants_html_for_browser_accepts(accept):
    assert wants_html(accept) is True


@pytest.mark.parametrize(
    "accept",
    [
        "",  # no header
        "*/*",  # curl / SDKs — keep the 401
        "application/json",
        "application/json, text/event-stream",  # MCP client POST shape
        "text/event-stream",  # MCP client GET (SSE listen stream)
        "text/html, text/event-stream",  # anything naming SSE is MCP
    ],
)
def test_wants_html_false_for_non_browser_accepts(accept):
    assert wants_html(accept) is False


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


def test_summarize_first_sentence_of_first_paragraph():
    desc = "Run a FedSQL SELECT against work.students and return rows. More words.\n\nSecond para."
    assert summarize(desc) == "Run a FedSQL SELECT against work.students and return rows."


def test_summarize_collapses_hard_wraps_and_strips_markdown():
    desc = "Upload a data file — read **by the server**,\n    not the ``model``.\n\nDetails."
    assert summarize(desc) == "Upload a data file — read by the server, not the model."


def test_summarize_caps_long_first_sentence():
    out = summarize("word " * 100, max_len=40)
    assert len(out) <= 40 and out.endswith("…")


def test_summarize_empty():
    assert summarize(None) == ""
    assert summarize("   ") == ""


# ---------------------------------------------------------------------------
# client_snippets
# ---------------------------------------------------------------------------


def test_client_snippets_all_point_at_the_url_and_json_ones_parse():
    snippets = client_snippets(MCP_URL)
    keys = [s.key for s in snippets]
    assert keys == ["claude-code", "vscode", "cursor", "claude-connector", "generic"]
    for s in snippets:
        assert isinstance(s, ClientSnippet)
        assert MCP_URL in s.body
    by_key = {s.key: s for s in snippets}
    assert json.loads(by_key["vscode"].body) == {"servers": {"sas-viya": {"type": "http", "url": MCP_URL}}}
    assert json.loads(by_key["cursor"].body) == {"mcpServers": {"sas-viya": {"url": MCP_URL}}}
    assert json.loads(by_key["generic"].body)["mcpServers"]["sas-viya"]["url"] == MCP_URL
    assert by_key["claude-code"].body.startswith("claude mcp add --transport http sas-viya ")
    assert by_key["claude-connector"].body == MCP_URL


def test_client_snippets_custom_server_key():
    body = client_snippets(MCP_URL, server_key="viya-prod")[1].body
    assert "viya-prod" in json.loads(body)["servers"]


# ---------------------------------------------------------------------------
# render_page
# ---------------------------------------------------------------------------


def test_render_page_escapes_everything_dynamic():
    page = render_page(_facts(), nonce="abc")
    assert "<Server>" not in page and "Test &lt;Server&gt;" in page
    assert "<b>SAS</b>" not in page and "Run &lt;b&gt;SAS&lt;/b&gt; code." in page
    assert page.startswith("<!DOCTYPE html>")


def test_render_page_headline_is_the_product_name():
    """The headline is fixed; MCP_SERVER_NAME shows as a subtitle only when customised."""
    from sas_mcp_server.config import DEFAULT_SERVER_NAME

    default = render_page(_facts(server_name=DEFAULT_SERVER_NAME), nonce="n")
    assert "<h1>SAS Viya MCP Server</h1>" in default
    assert "<title>SAS Viya MCP Server</title>" in default
    assert "Deployment:" not in default
    assert "Execution" not in default

    custom = render_page(_facts(server_name="Viya Prod <EU>"), nonce="n")
    assert "<h1>SAS Viya MCP Server</h1>" in custom
    assert "Deployment: <strong>Viya Prod &lt;EU&gt;</strong>" in custom
    assert "<title>SAS Viya MCP Server · Viya Prod &lt;EU&gt;</title>" in custom


def test_render_page_nonce_on_style_and_script_only():
    page = render_page(_facts(), nonce="N0NCE")
    assert '<style nonce="N0NCE">' in page
    assert '<script nonce="N0NCE">' in page
    # No inline style attributes — the CSP would block them silently.
    assert ' style="' not in page
    # No external assets — the page must be self-contained under default-src 'none'.
    assert "<link" not in page and " src=" not in page and "@import" not in page


def test_render_page_contains_endpoint_snippets_tiers_and_prompts():
    page = render_page(_facts(), nonce="n")
    assert MCP_URL in page
    assert 'id="snippet-vscode"' in page and 'id="snippet-claude-code"' in page
    assert "Tier 0 — Compute" in page and "Tier 3 — Reports" in page
    assert "<code>execute_sas_code</code>" in page
    assert "<code>debug_sas_log</code>" in page
    assert "v9.9.9" in page
    assert "https://viya.example.com" in page
    assert "All tool tiers" in page
    assert "Read &amp; write tools" in page
    assert "OAuth 2.0 + PKCE" in page


def test_render_page_read_only_and_partial_tiers_are_called_out():
    page = render_page(_facts(read_only=True, enabled_tiers=frozenset({0, 1, 2, 3, 7})), nonce="n")
    assert "Read-only mode" in page
    assert "Read-only mode is on." in page
    assert "Tiers 0–3, 7 of 0–10" in page
    assert "limited this deployment to tool tiers" in page


def test_render_page_tier_0_implies_tier_8():
    """Every tier but 8 still reads as 'all' — Tier 8's only tool is inside Tier 0.

    The selection deliberately omits 8 and nothing else, so this keeps testing
    the implication rather than merely tracking however many tiers exist.
    """
    facts = _facts(enabled_tiers=frozenset(tools.ALL_TIERS) - {8})
    assert facts.all_tiers_enabled is True
    assert "All tool tiers" in render_page(facts, nonce="n")
    assert _facts(enabled_tiers=frozenset({3, 8})).all_tiers_enabled is False


def test_render_page_auth_disabled_and_raw_bearer_notes():
    off = render_page(_facts(auth_enabled=False), nonce="n")
    assert "Authentication disabled" in off and "VIYA_AUTH=false" in off
    raw = render_page(_facts(allow_raw_bearer=True), nonce="n")
    assert "Authorization: Bearer" in raw
    assert "Authorization: Bearer" not in render_page(_facts(), nonce="n")


def test_render_page_without_version_or_prompts():
    page = render_page(_facts(version=None, prompts=()), nonce="n")
    assert "vNone" not in page and "· v" not in page
    assert "Prompt templates" not in page


def test_tool_entry_kind():
    assert ToolEntry("a", "s").kind == ""  # no annotations → no claim
    assert ToolEntry("a", "s", read_only=True, destructive=False).kind == "read-only"
    assert ToolEntry("a", "s", read_only=False, destructive=False).kind == "write"
    assert ToolEntry("a", "s", read_only=False, destructive=True).kind == "destructive"


def test_render_page_shows_behaviour_markers_and_legend():
    facts = _facts(
        tiers=(
            TierGroup(
                tier=0,
                title="T",
                tools=(
                    ToolEntry("execute_sas_code", "Run.", read_only=False, destructive=True),
                    ToolEntry("list_caslibs", "List.", read_only=True, destructive=False),
                    ToolEntry("upload_data", "Up.", read_only=False, destructive=False),
                ),
            ),
        )
    )
    page = render_page(facts, nonce="n")
    assert '<em class="kind destructive">destructive</em>' in page
    assert '<em class="kind read-only">read-only</em>' in page
    assert '<em class="kind write">write</em>' in page
    assert 'class="legend"' in page and "MCP tool annotations" in page


def test_render_page_without_annotations_makes_no_claims():
    page = render_page(_facts(), nonce="n")  # fixture tools carry no hints
    assert 'class="legend"' not in page
    assert '<em class="kind "></em>' in page  # empty marker cell, nothing invented


def test_tier_range_formatting():
    assert landing._tier_range(frozenset()) == ""
    assert landing._tier_range(frozenset({4})) == "4"
    assert landing._tier_range(frozenset({0, 1, 2, 5, 7, 8})) == "0–2, 5, 7–8"


# ---------------------------------------------------------------------------
# collect_facts — against a real FastMCP with the tier registrars
# ---------------------------------------------------------------------------


async def test_collect_facts_groups_registered_tools_by_tier():
    mcp = FastMCP("landing-facts")

    async def get_token(ctx):
        return "t"

    tools.register_tools(mcp, get_token, tiers="0,3", read_only=False)
    facts = await collect_facts(
        mcp,
        server_name="X",
        mcp_url=MCP_URL,
        viya_endpoint="https://viya",
        auth_enabled=True,
        allow_raw_bearer=False,
        read_only=False,
        enabled_tiers={0, 3},
        version="1.0",
    )
    assert [g.tier for g in facts.tiers] == [0, 3]
    assert facts.tiers[0].title == tools.TIER_TITLES[0]
    names0 = {t.name for t in facts.tiers[0].tools}
    assert "execute_sas_code" in names0
    by_name = {t.name: t for g in facts.tiers for t in g.tools}
    assert by_name["execute_sas_code"].kind == "destructive"  # picked up from the tool's annotations
    assert by_name["list_compute_contexts"].kind == "read-only"
    assert all(t.summary for t in facts.tiers[0].tools)  # every tool has a one-liner
    # sorted within a tier
    assert [t.name for t in facts.tiers[1].tools] == sorted(t.name for t in facts.tiers[1].tools)
    assert facts.tool_count == sum(len(g.tools) for g in facts.tiers)
    assert facts.prompts == ()  # no prompts registered on this instance
    assert facts.version == "1.0"
    assert facts.all_tiers_enabled is False


async def test_collect_facts_untiered_tool_lands_under_other():
    mcp = FastMCP("landing-untiered")

    @mcp.tool
    def rogue_tool() -> str:
        """Not registered through a tier."""
        return "x"

    facts = await collect_facts(
        mcp,
        server_name="X",
        mcp_url=MCP_URL,
        viya_endpoint="",
        auth_enabled=False,
        allow_raw_bearer=False,
        read_only=False,
        enabled_tiers=set(),
    )
    assert facts.tiers[-1].tier is None
    assert facts.tiers[-1].title == "Other tools"
    assert facts.tiers[-1].tools[0].name == "rogue_tool"


# ---------------------------------------------------------------------------
# LandingPageMiddleware — routing, isolated from FastMCP
# ---------------------------------------------------------------------------


def _downstream(request):
    return PlainTextResponse("downstream", status_code=418)


def _make_app(path="/mcp", calls=None):
    calls = calls if calls is not None else []

    async def facts():
        calls.append(1)
        return _facts()

    return Starlette(
        routes=[
            Route(path, _downstream, methods=["GET", "POST", "DELETE"]),
            Route("/health", _downstream, methods=["GET"]),
        ],
        middleware=[Middleware(LandingPageMiddleware, path=path, facts=facts)],
    )


async def _get(app, url, **kw):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return await c.request(kw.pop("method", "GET"), url, **kw)


async def test_middleware_serves_html_for_browser_get():
    r = await _get(_make_app(), "/mcp", headers={"Accept": "text/html,*/*;q=0.8"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Test &lt;Server&gt;" in r.text
    csp = r.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert csp.startswith("default-src 'none'")
    assert f'<style nonce="{nonce}">' in r.text and f'<script nonce="{nonce}">' in r.text
    assert r.headers["x-robots-tag"].startswith("noindex")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_middleware_nonce_differs_per_response():
    app = _make_app()
    a = await _get(app, "/mcp", headers={"Accept": "text/html"})
    b = await _get(app, "/mcp", headers={"Accept": "text/html"})
    assert a.headers["content-security-policy"] != b.headers["content-security-policy"]


async def test_middleware_trailing_slash_and_root_path():
    app = _make_app()
    r = await _get(app, "/mcp/", headers={"Accept": "text/html"})
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


@pytest.mark.parametrize(
    "method,url,headers",
    [
        ("GET", "/mcp", {"Accept": "application/json, text/event-stream"}),  # MCP GET
        ("GET", "/mcp", {"Accept": "*/*"}),  # curl
        ("GET", "/mcp", {}),  # no Accept at all
        ("POST", "/mcp", {"Accept": "text/html"}),  # never intercept POST
        ("DELETE", "/mcp", {"Accept": "text/html"}),
        ("GET", "/health", {"Accept": "text/html"}),  # other paths untouched
    ],
)
async def test_middleware_passes_everything_else_through(method, url, headers):
    r = await _get(_make_app(), url, method=method, headers=headers)
    assert r.status_code == 418 and r.text == "downstream"


async def test_middleware_prefix_is_not_a_match():
    r = await _get(_make_app(), "/mcpx", headers={"Accept": "text/html"})
    assert r.status_code == 404  # the downstream router's answer, not the page
    assert not r.headers["content-type"].startswith("text/html")


async def test_middleware_collects_facts_once():
    calls = []
    app = _make_app(calls=calls)
    for _ in range(3):
        await _get(app, "/mcp", headers={"Accept": "text/html"})
    assert len(calls) == 1


async def test_middleware_honours_custom_path():
    app = _make_app(path="/api/mcp")
    ok = await _get(app, "/api/mcp", headers={"Accept": "text/html"})
    assert ok.status_code == 200
    # a downstream route exists at /health only, so /mcp is a plain 404 here
    other = await _get(app, "/mcp", headers={"Accept": "text/html"})
    assert other.status_code == 404


# ---------------------------------------------------------------------------
# Wiring in mcp_server — the real app
# ---------------------------------------------------------------------------


async def test_real_app_serves_landing_page_and_keeps_mcp_behaviour():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp_server.app), base_url="http://t") as c:
        page = await c.get("/mcp", headers={"Accept": "text/html,*/*;q=0.8"})
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert mcp_server.SERVER_NAME in page.text or "&" in page.text
        assert mcp_server.MCP_BASE_URL.rstrip("/") + mcp_server.MCP_PATH in page.text
        assert "<code>execute_sas_code</code>" in page.text or "Read-only" in page.text
        # An MCP-shaped GET is not intercepted: whatever FastMCP does today
        # (401 with auth on) still happens, and it is never HTML.
        mcp_get = await c.get("/mcp", headers={"Accept": "application/json, text/event-stream"})
        assert not mcp_get.headers.get("content-type", "").startswith("text/html")
        health = await c.get("/health", headers={"Accept": "text/html"})
        assert health.status_code == 200 and health.json()["status"] == "healthy"


def test_landing_middleware_is_installed_when_enabled():
    """The flag is read at import; assert the wiring matches its current value."""
    installed = any(m.cls is LandingPageMiddleware for m in mcp_server._http_middleware)
    assert installed is mcp_server.MCP_LANDING_PAGE
    assert mcp_server.MCP_PATH.startswith("/")


def test_annotation_hint_field_names_exist_on_the_model():
    """The two hint field names must be real fields on ``ToolAnnotations``.

    ``_hint`` reads them with ``getattr(..., None)``, which cannot distinguish
    "this tool declares nothing" from "this field was renamed". Without this
    assertion an SDK rename would blank every read-only and destructive badge on
    the page and every other test would still pass. MCP SDK v2 already renamed
    these once, from camelCase.
    """
    from mcp.types import ToolAnnotations

    fields = set(ToolAnnotations.model_fields)
    assert landing._READ_ONLY_HINT in fields
    assert landing._DESTRUCTIVE_HINT in fields


def test_hint_reads_declared_values_and_tolerates_no_annotations():
    from mcp.types import ToolAnnotations

    ann = ToolAnnotations(read_only_hint=True, destructive_hint=False)
    assert landing._hint(ann, landing._READ_ONLY_HINT) is True
    assert landing._hint(ann, landing._DESTRUCTIVE_HINT) is False
    assert landing._hint(None, landing._READ_ONLY_HINT) is None
    assert landing._hint(ToolAnnotations(), landing._READ_ONLY_HINT) is None
