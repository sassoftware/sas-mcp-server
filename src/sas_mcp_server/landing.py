# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Browser landing page for the MCP endpoint.

An MCP client speaks JSON-RPC over ``POST /mcp`` and, optionally, opens the
server-to-client event stream with ``GET /mcp`` and ``Accept: text/event-stream``.
A *person* who pastes the endpoint URL into a browser sends neither — just a
``GET`` asking for ``text/html`` — and, until now, got FastMCP's bare
``401 {"error": "invalid_token", ...}`` back, which reads as "broken" rather
than "not meant for browsers".

:class:`LandingPageMiddleware` intercepts exactly that request shape and serves
a self-contained page instead: what the server is, which SAS Viya it talks to,
which tool tiers this deployment exposes, and copy-paste snippets that connect
the common MCP clients. Everything else — every ``POST``, every ``GET`` that
does not explicitly ask for HTML, every other path — passes through untouched,
so the MCP transport and the OAuth routes never see a difference.

The page is unauthenticated by construction (an unauthenticated visitor is the
whole point), so it shows only deployment *shape*: server name and version,
Viya host, tier/read-only configuration, and tool names with their one-line
summaries — the same catalogue any authenticated ``tools/list`` returns. No user
data, no tokens, nothing derived from a request. Deployments that would rather
not advertise even that set ``MCP_LANDING_PAGE=false`` and get the 401 back.

The middleware keys on the ``Accept`` header, not the ``User-Agent``: UA
sniffing is brittle, and unnecessary — MCP clients advertise
``application/json, text/event-stream`` and browsers ``text/html``, so the two
never collide. A request that names ``text/event-stream`` anywhere in ``Accept``
is always treated as MCP, whatever else it lists.
"""

from __future__ import annotations

import html
import json
import secrets
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from starlette.responses import HTMLResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import DEFAULT_SERVER_NAME
from .helpers.telemetry_helpers import server_version
from .tools import ALL_TIERS, TIER_TITLES, TOOL_TIERS, resolve_enabled_tiers

__all__ = [
    "LandingPageMiddleware",
    "ServerFacts",
    "ToolEntry",
    "TierGroup",
    "PromptEntry",
    "ClientSnippet",
    "collect_facts",
    "client_snippets",
    "render_page",
    "summarize",
    "wants_html",
]

# Tools that no tier registrar claimed (should not happen; kept so an oversight
# shows up on the page under an honest heading rather than vanishing).
_UNTIERED_TITLE = "Other tools"
_SUMMARY_MAX = 200

REPO_URL = "https://github.com/sassoftware/sas-mcp-server"
# The page's headline is the product, not serverInfo.name: MCP_SERVER_NAME
# exists so admins can tell deployments apart in a client's server list, and
# it is shown as a subtitle only when someone actually set it.
PAGE_TITLE = "SAS Viya MCP Server"


# --- data --------------------------------------------------------------------


@dataclass(frozen=True)
class ToolEntry:
    name: str
    summary: str
    # From the tool's MCP annotations (read_only_hint / destructive_hint); None
    # when the tool carries none, so the page never invents a classification.
    read_only: bool | None = None
    destructive: bool | None = None
    # True when the tool carries an MCP Apps view (``_meta.ui.resourceUri``)
    # that a supporting client renders beside the result.
    interactive: bool = False

    @property
    def kind(self) -> str:
        """``"read-only"``, ``"destructive"``, ``"write"`` or ``""`` (unknown)."""
        if self.read_only:
            return "read-only"
        if self.read_only is None and self.destructive is None:
            return ""
        return "destructive" if self.destructive else "write"


@dataclass(frozen=True)
class PromptEntry:
    name: str
    summary: str


@dataclass(frozen=True)
class TierGroup:
    """One tier's slice of the exposed catalogue. ``tier`` is ``None`` for
    tools no registrar claimed."""

    tier: int | None
    title: str
    tools: tuple[ToolEntry, ...]


@dataclass(frozen=True)
class ClientSnippet:
    """A copy-paste connection recipe for one MCP client."""

    key: str  # stable id, used for DOM ids
    client: str  # human name, e.g. "VS Code"
    where: str  # file / place the snippet goes, e.g. ".vscode/mcp.json"
    body: str  # the text to copy
    note: str = ""  # optional one-line hint rendered under the snippet


@dataclass(frozen=True)
class ServerFacts:
    """Everything the page renders, gathered once at first request.

    Nothing here comes from a request — the page is identical for every
    visitor, which is what makes it safe to serve without authentication.
    """

    server_name: str
    version: str | None
    mcp_url: str
    viya_endpoint: str
    auth_enabled: bool
    allow_raw_bearer: bool
    enabled_tiers: frozenset[int]
    read_only: bool
    tiers: tuple[TierGroup, ...]
    prompts: tuple[PromptEntry, ...]
    # Hosted on Posit Connect (POSIT_CONNECT_OAUTH): Connect signs users in.
    posit_connect: bool = False

    @property
    def tool_count(self) -> int:
        return sum(len(g.tools) for g in self.tiers)

    @property
    def effective_tiers(self) -> frozenset[int]:
        """Enabled tiers plus Tier 8 whenever Tier 0 is on — Tier 8's only
        tool (``execute_sas_code``) is part of Tier 0, so ``MCP_TIERS=0-7``
        exposes everything and should read that way."""
        if 0 in self.enabled_tiers:
            return self.enabled_tiers | {8}
        return self.enabled_tiers

    @property
    def all_tiers_enabled(self) -> bool:
        return self.effective_tiers >= ALL_TIERS


def summarize(description: str | None, *, max_len: int = _SUMMARY_MAX) -> str:
    """First sentence of the first paragraph of a tool/prompt description.

    Tool docstrings run to several paragraphs of guidance for the model; the
    page wants the one line a person scans. Falls back to a hard cap with an
    ellipsis when the first sentence is itself long.
    """
    if not description:
        return ""
    first_para = description.strip().split("\n\n", 1)[0]
    # Collapse the docstring's hard wraps into a single line.
    text = " ".join(line.strip() for line in first_para.splitlines() if line.strip())
    # First sentence: a period followed by whitespace (avoids "e.g. x" and
    # dotted names like ``work.students`` splitting the summary early).
    for i, ch in enumerate(text):
        if ch in ".!?" and i + 1 < len(text) and text[i + 1].isspace():
            text = text[: i + 1]
            break
    # Docstrings carry light markdown for the model; a person reads plain text.
    text = text.replace("**", "").replace("`", "")
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def _hint(annotations: Any, field: str) -> bool | None:
    """Read one MCP tool annotation, or ``None`` when the tool carries none.

    The field name is passed in rather than accessed inline because ``getattr``
    with a ``None`` default cannot tell "this tool declares nothing" from "this
    field no longer exists", so a rename upstream would blank every badge on the
    page instead of failing. MCP SDK v2 renamed these from camelCase once
    already; :data:`_READ_ONLY_HINT` and :data:`_DESTRUCTIVE_HINT` are asserted
    against the model in test_landing.py so a second rename fails a test.
    """
    if annotations is None:
        return None
    return getattr(annotations, field, None)


_READ_ONLY_HINT = "read_only_hint"
_DESTRUCTIVE_HINT = "destructive_hint"


def _has_view(tool: Any) -> bool:
    """Whether the tool's metadata names an MCP Apps view resource."""
    meta = getattr(tool, "meta", None)
    ui = meta.get("ui") if isinstance(meta, dict) else None
    return isinstance(ui, dict) and bool(ui.get("resourceUri"))


async def collect_facts(
    mcp: Any,
    *,
    server_name: str,
    mcp_url: str,
    viya_endpoint: str,
    auth_enabled: bool,
    allow_raw_bearer: bool,
    read_only: bool,
    posit_connect: bool = False,
    enabled_tiers: Iterable[int] | None = None,
    public_name: Callable[[str], str] | None = None,
    version: str | None = None,
) -> ServerFacts:
    """Snapshot the registered catalogue into a :class:`ServerFacts`.

    Reads FastMCP's own tool/prompt lists (``run_middleware=False`` so the
    telemetry middleware does not decorate schemas for a page nobody scores)
    and groups tools by the tier that registered them (:data:`TOOL_TIERS`).
    """
    tools = await mcp.list_tools(run_middleware=False)
    prompts = await mcp.list_prompts(run_middleware=False)

    by_tier: dict[int | None, list[ToolEntry]] = {}
    for t in tools:
        ann = getattr(t, "annotations", None)
        by_tier.setdefault(TOOL_TIERS.get(t.name), []).append(
            ToolEntry(
                # What a client is shown, when MCP_TOOL_RENAME is in play.
                name=public_name(t.name) if public_name else t.name,
                summary=summarize(t.description),
                read_only=_hint(ann, _READ_ONLY_HINT),
                destructive=_hint(ann, _DESTRUCTIVE_HINT),
                interactive=_has_view(t),
            )
        )
    groups: list[TierGroup] = []
    for tier in sorted(k for k in by_tier if k is not None):
        groups.append(
            TierGroup(
                tier=tier,
                title=TIER_TITLES.get(tier, f"Tier {tier}"),
                tools=tuple(sorted(by_tier[tier], key=lambda e: e.name)),
            )
        )
    if None in by_tier:
        groups.append(
            TierGroup(
                tier=None,
                title=_UNTIERED_TITLE,
                tools=tuple(sorted(by_tier[None], key=lambda e: e.name)),
            )
        )

    enabled = frozenset(enabled_tiers) if enabled_tiers is not None else frozenset(resolve_enabled_tiers())
    return ServerFacts(
        server_name=server_name,
        version=version if version is not None else server_version(),
        mcp_url=mcp_url,
        viya_endpoint=viya_endpoint,
        auth_enabled=auth_enabled,
        allow_raw_bearer=allow_raw_bearer,
        enabled_tiers=enabled,
        read_only=read_only,
        tiers=tuple(groups),
        posit_connect=posit_connect,
        prompts=tuple(
            PromptEntry(name=p.name, summary=summarize(p.description)) for p in sorted(prompts, key=lambda p: p.name)
        ),
    )


# --- client snippets ---------------------------------------------------------

_SERVER_KEY = "sas-viya"


def client_snippets(mcp_url: str, *, server_key: str = _SERVER_KEY) -> list[ClientSnippet]:
    """Connection recipes for the common MCP clients, all pointing at *mcp_url*.

    Kept as data so the page and the tests share one source of truth. Every
    body is valid as-is: JSON via ``json.dumps`` (so a URL with odd characters
    is escaped correctly), the CLI line quoted for both POSIX shells and
    PowerShell.
    """
    quoted_url = json.dumps(mcp_url)  # a JSON string is also a valid shell arg
    return [
        ClientSnippet(
            key="claude-code",
            client="Claude Code",
            where="terminal",
            body=f"claude mcp add --transport http {server_key} {quoted_url}",
            note="Add --scope user to make it available in every project.",
        ),
        ClientSnippet(
            key="vscode",
            client="VS Code (GitHub Copilot)",
            where=".vscode/mcp.json",
            body=json.dumps(
                {"servers": {server_key: {"type": "http", "url": mcp_url}}},
                indent=2,
            ),
        ),
        ClientSnippet(
            key="cursor",
            client="Cursor",
            where="~/.cursor/mcp.json (or .cursor/mcp.json in a project)",
            body=json.dumps({"mcpServers": {server_key: {"url": mcp_url}}}, indent=2),
        ),
        ClientSnippet(
            key="claude-connector",
            client="Claude (claude.ai / Claude Desktop)",
            where="Settings → Connectors → Add custom connector → Remote MCP server URL",
            body=mcp_url,
        ),
        ClientSnippet(
            key="generic",
            client="Other clients (mcp.json)",
            where="the client's MCP config file",
            body=json.dumps(
                {"mcpServers": {server_key: {"type": "http", "url": mcp_url}}},
                indent=2,
            ),
            note=(
                "Windsurf, Gemini CLI, Codex and most others accept this shape; some spell "
                "the key url, httpUrl or serverUrl — check the client's docs."
            ),
        ),
    ]


# --- rendering ---------------------------------------------------------------


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _tier_range(tiers: frozenset[int]) -> str:
    """``{0,1,2,3,7}`` → ``"0–3, 7"``."""
    out: list[str] = []
    run: list[int] = []
    for t in sorted(tiers):
        if run and t == run[-1] + 1:
            run.append(t)
            continue
        if run:
            out.append(f"{run[0]}–{run[-1]}" if len(run) > 1 else str(run[0]))
        run = [t]
    if run:
        out.append(f"{run[0]}–{run[-1]}" if len(run) > 1 else str(run[0]))
    return ", ".join(out)


_CSS = """
:root {
  --bg: #f6f7f9; --card: #ffffff; --ink: #1c2330; --muted: #5b6577;
  --line: #e2e6ec; --accent: #0766d1; --accent-ink: #ffffff; --code-bg: #f0f2f5;
  --ok: #1a7f4b; --warn: #a15c00; --chip: #e8f0fb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f141b; --card: #171d26; --ink: #e6eaf0; --muted: #98a2b3;
    --line: #2a3340; --accent: #5aa2ff; --accent-ink: #0b1220; --code-bg: #0b1017;
    --ok: #4cc38a; --warn: #f2b264; --chip: #1e2a3d;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: var(--ink); background: var(--bg);
}
main { max-width: 900px; margin: 0 auto; padding: 32px 20px 64px; }
header { margin-bottom: 28px; }
h1 { font-size: 1.85rem; line-height: 1.2; margin: 0 0 8px; letter-spacing: -0.01em; }
.deployment { margin: -4px 0 8px; color: var(--muted); }
h2 { font-size: 1.25rem; margin: 36px 0 12px; }
h3 { font-size: 1rem; margin: 20px 0 6px; }
p { margin: 8px 0; }
.lede { color: var(--muted); font-size: 1.05rem; }
.badges { display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 0; padding: 0; list-style: none; }
.badge {
  display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 0.85rem;
  background: var(--chip); color: var(--ink); border: 1px solid var(--line);
}
.badge.ok { color: var(--ok); }
.badge.warn { color: var(--warn); }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 18px 20px; margin: 14px 0;
}
.endpoint { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.endpoint code { font-size: 1rem; }
.viya { margin-top: 10px; }
code, pre {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 0.9rem;
}
code { background: var(--code-bg); padding: 2px 6px; border-radius: 6px; }
pre {
  background: var(--code-bg); border: 1px solid var(--line); border-radius: 8px;
  padding: 12px 14px; margin: 8px 0 0; overflow-x: auto; white-space: pre;
}
.snippet h3 { margin-top: 0; }
.snippet .where { color: var(--muted); font-size: 0.9rem; }
.snippet .code { position: relative; }
.snippet .note { color: var(--muted); font-size: 0.85rem; margin-top: 6px; }
button.copy {
  font: inherit; font-size: 0.85rem; padding: 4px 10px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--line); background: var(--card); color: var(--ink);
}
button.copy:hover { border-color: var(--accent); color: var(--accent); }
button.copy.done { border-color: var(--ok); color: var(--ok); }
.snippet .code button.copy { position: absolute; right: 8px; top: 16px; }
.snippet pre { padding-right: 90px; }
details { border: 1px solid var(--line); border-radius: 10px; background: var(--card); margin: 8px 0; }
details summary {
  cursor: pointer; padding: 12px 16px; font-weight: 600; list-style: none;
  display: flex; gap: 10px; align-items: baseline;
}
details summary::-webkit-details-marker { display: none; }
details summary::before { content: "▸"; color: var(--muted); font-weight: 400; }
details[open] summary::before { content: "▾"; }
details summary .count { color: var(--muted); font-weight: 400; font-size: 0.9rem; margin-left: auto; }
details .tools { margin: 0; padding: 0 16px 12px; list-style: none; }
details .tools li {
  padding: 6px 0; border-top: 1px solid var(--line);
  display: grid; grid-template-columns: minmax(180px, 32%) 6.5em 1fr; gap: 12px; align-items: baseline;
}
details .tools li code { background: none; padding: 0; }
details .tools li span { color: var(--muted); font-size: 0.95rem; }
details .tools li .kind {
  font-style: normal; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--muted); white-space: nowrap;
}
details .tools li .kind.read-only { color: var(--ok); }
details .tools li .kind.destructive { color: var(--warn); }
details .tools li .view, .legend .view {
  font-style: normal; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--accent); border: 1px solid currentColor; border-radius: 999px; padding: 0 6px;
  margin-left: 6px; white-space: nowrap;
}
.legend { color: var(--muted); font-size: 0.9rem; margin: 6px 0 12px; }
.legend .kind { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em; font-style: normal; }
.legend .kind.read-only { color: var(--ok); }
.legend .kind.destructive { color: var(--warn); }
@media (max-width: 600px) {
  details .tools li { grid-template-columns: 1fr auto; gap: 2px 8px; }
  details .tools li span { grid-column: 1 / -1; }
}
.steps { padding-left: 20px; }
.steps li { margin: 6px 0; }
footer {
  margin-top: 48px; color: var(--muted); font-size: 0.9rem;
  border-top: 1px solid var(--line); padding-top: 16px;
}
a { color: var(--accent); }
"""

_JS = """
document.addEventListener('click', function (ev) {
  var btn = ev.target.closest('button.copy');
  if (!btn) return;
  var src = document.getElementById(btn.getAttribute('data-copy'));
  if (!src) return;
  var text = src.textContent;
  var done = function () {
    btn.classList.add('done'); btn.textContent = 'Copied';
    setTimeout(function () { btn.classList.remove('done'); btn.textContent = 'Copy'; }, 1500);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, function () { fallback(text, done); });
  } else { fallback(text, done); }
  function fallback(t, cb) {
    var ta = document.createElement('textarea'); ta.value = t; ta.setAttribute('readonly', '');
    ta.style.position = 'fixed'; ta.style.top = '-1000px'; document.body.appendChild(ta);
    ta.select(); try { document.execCommand('copy'); cb(); } catch (e) {} document.body.removeChild(ta);
  }
});
"""


def _render_snippet(s: ClientSnippet) -> str:
    pre_id = f"snippet-{_e(s.key)}"
    note = f'<div class="note">{_e(s.note)}</div>' if s.note else ""
    return (
        f'<div class="snippet card">'
        f"<h3>{_e(s.client)}</h3>"
        f'<div class="where">{_e(s.where)}</div>'
        f'<div class="code">'
        f'<pre id="{pre_id}">{_e(s.body)}</pre>'
        f'<button class="copy" type="button" data-copy="{pre_id}" '
        f'aria-label="Copy {_e(s.client)} snippet">Copy</button>'
        f"</div>"
        f"{note}"
        f"</div>"
    )


def _render_tier(g: TierGroup, *, open_: bool) -> str:
    label = f"Tier {g.tier} — {g.title}" if g.tier is not None else g.title
    items = "".join(
        f'<li><code>{_e(t.name)}</code><em class="kind {_e(t.kind)}">{_e(t.kind)}</em>'
        f'<span>{_e(t.summary)}{"<em class=\"view\">interactive</em>" if t.interactive else ""}</span></li>'
        for t in g.tools
    )
    n = len(g.tools)
    return (
        f"<details{' open' if open_ else ''}>"
        f'<summary>{_e(label)}<span class="count">{n} tool{"s" if n != 1 else ""}</span></summary>'
        f'<ul class="tools">{items}</ul>'
        f"</details>"
    )


def render_page(facts: ServerFacts, *, nonce: str) -> str:
    """Render the full HTML document. *nonce* ties the inline style/script to
    the response's Content-Security-Policy."""
    name = _e(PAGE_TITLE)
    custom_name = facts.server_name if facts.server_name != DEFAULT_SERVER_NAME else ""
    deployment = f'<p class="deployment">Deployment: <strong>{_e(custom_name)}</strong></p>' if custom_name else ""
    title = f"{PAGE_TITLE} · {custom_name}" if custom_name else PAGE_TITLE
    version = f'<span class="badge">v{_e(facts.version)}</span>' if facts.version else ""
    tiers_badge = (
        '<span class="badge">All tool tiers</span>'
        if facts.all_tiers_enabled
        else (
            f'<span class="badge">Tiers {_e(_tier_range(facts.enabled_tiers))} '
            f"of {_e(_tier_range(frozenset(ALL_TIERS)))}</span>"
        )
    )
    ro_badge = (
        '<span class="badge warn">Read-only mode</span>'
        if facts.read_only
        else '<span class="badge ok">Read &amp; write tools</span>'
    )
    if facts.posit_connect:
        auth_badge = '<span class="badge">Sign-in: Posit Connect + SAS Viya</span>'
    elif facts.auth_enabled:
        auth_badge = '<span class="badge">Sign-in: SAS Viya (OAuth 2.0 + PKCE)</span>'
    else:
        auth_badge = '<span class="badge warn">Authentication disabled</span>'
    tools_badge = f'<span class="badge">{facts.tool_count} tools · {len(facts.prompts)} prompts</span>'

    if facts.posit_connect:
        auth_para = (
            "<p>This server is hosted on Posit Connect. Your MCP client signs in to Connect "
            "(or sends a Connect API key), and Connect asks you once to authorize SAS Viya "
            "through SAS Logon. Every action then runs under <em>your</em> Viya identity and "
            "permissions; this server never sees your password.</p>"
        )
    elif facts.auth_enabled:
        auth_para = (
            "<p>Your MCP client will open a browser window and ask you to sign in to SAS Viya "
            "the first time it connects. The sign-in happens on SAS Logon, not here — this "
            "server never sees your password, and every action it performs runs under "
            "<em>your</em> Viya identity and permissions.</p>"
        )
        if facts.allow_raw_bearer:
            auth_para += (
                "<p>Scripts and automation that already hold a SAS Viya access token can skip the "
                "browser step and send it directly: "
                "<code>Authorization: Bearer &lt;viya-token&gt;</code>.</p>"
            )
    else:
        auth_para = (
            "<p>This deployment runs with authentication disabled (<code>VIYA_AUTH=false</code>); "
            "requests are forwarded to SAS Viya without credentials.</p>"
        )

    scope_para = ""
    if facts.read_only:
        scope_para += (
            "<p><strong>Read-only mode is on.</strong> Only tools that neither change "
            "server-side state nor start server-side work are exposed — that excludes "
            "code execution and batch jobs as well as create/update/delete operations.</p>"
        )
    if not facts.all_tiers_enabled:
        scope_para += (
            f"<p>The administrator has limited this deployment to tool tiers "
            f"<strong>{_e(_tier_range(facts.enabled_tiers))}</strong>; the groups below are what "
            f"you will see in your client.</p>"
        )

    snippets = "".join(_render_snippet(s) for s in client_snippets(facts.mcp_url))
    tier_html = "".join(_render_tier(g, open_=(i == 0)) for i, g in enumerate(facts.tiers))
    kinds = {t.kind for g in facts.tiers for t in g.tools}
    legend = (
        '<p class="legend">Each tool advertises its behaviour to your client as MCP tool annotations — '
        '<em class="kind read-only">read-only</em> tools only read; '
        '<em class="kind write">write</em> tools add or start something; '
        '<em class="kind destructive">destructive</em> tools can change or remove what already exists. '
        "Clients use these to group tools and to decide when to ask you before running one.</p>"
        if kinds - {""}
        else ""
    )
    if any(t.interactive for g in facts.tiers for t in g.tools):
        legend += (
            '<p class="legend">Tools marked <em class="view">interactive</em> also carry a small view — '
            "a data grid, a SAS log, a glossary form — that clients supporting MCP Apps (Claude, ChatGPT, "
            "Microsoft 365 Copilot, VS Code, Cursor) render beside the result. Other clients see the same "
            "text result as always.</p>"
        )
    prompt_items = "".join(f"<li><code>{_e(p.name)}</code><span>{_e(p.summary)}</span></li>" for p in facts.prompts)
    prompts_html = (
        f'<details><summary>Prompt templates<span class="count">{len(facts.prompts)}</span></summary>'
        f'<ul class="tools">{prompt_items}</ul></details>'
        if facts.prompts
        else ""
    )
    viya = _e(facts.viya_endpoint) if facts.viya_endpoint else "(not configured)"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{_e(title)}</title>
<style nonce="{nonce}">{_CSS}</style>
</head>
<body>
<main>
<header>
  <h1>{name}</h1>
  {deployment}
  <p class="lede">A <a href="https://modelcontextprotocol.io/" rel="noopener">Model Context Protocol</a> server
  that lets AI assistants work with SAS Viya on your behalf — run SAS code, explore and query data,
  build reports, manage models and decisions — through the tools listed below.</p>
  <ul class="badges" aria-label="Deployment summary">
    <li>{version}</li>
    <li>{tools_badge}</li>
    <li>{tiers_badge}</li>
    <li>{ro_badge}</li>
    <li>{auth_badge}</li>
  </ul>
</header>

<section class="card">
  <div class="endpoint">
    <strong>MCP endpoint</strong>
    <code id="endpoint-url">{_e(facts.mcp_url)}</code>
    <button class="copy" type="button" data-copy="endpoint-url" aria-label="Copy endpoint URL">Copy</button>
  </div>
  <p class="lede viya">Connected to SAS Viya at <code>{viya}</code>.</p>
  <p>You are seeing this page because you opened the endpoint in a web browser. There is nothing to
  click here — the endpoint is meant for an MCP-capable AI client, which sends it JSON-RPC requests.
  Add it to your client with one of the snippets below.</p>
</section>

<h2>Connect your client</h2>
{snippets}

<h2>Signing in</h2>
<section class="card">
{auth_para}
</section>

<h2>What it can do</h2>
{scope_para}
{legend}
{tier_html}
{prompts_html}

<footer>
  <p>{_e(title)}{" · v" + _e(facts.version) if facts.version else ""} ·
  <a href="{REPO_URL}" rel="noopener">sassoftware/sas-mcp-server</a> on GitHub.</p>
  <p>Administrators: this page shows only the deployment's configuration and tool catalogue, never user data.
  Set <code>MCP_LANDING_PAGE=false</code> to switch it off and return the plain 401 to browsers.</p>
</footer>
</main>
<script nonce="{nonce}">{_JS}</script>
</body>
</html>
"""


# --- middleware --------------------------------------------------------------


def _route_path(scope: Scope) -> str:
    """The request path relative to any mount ``root_path`` (what Starlette's
    router itself matches against)."""
    path: str = scope.get("path", "")
    root: str = scope.get("root_path", "") or ""
    if root and path.startswith(root):
        return path[len(root) :] or "/"
    return path


def _header(scope: Scope, name: bytes) -> str:
    for k, v in scope.get("headers", ()):
        if k.lower() == name:
            return v.decode("latin-1")
    return ""


def wants_html(accept: str) -> bool:
    """True when an ``Accept`` header explicitly asks for HTML.

    ``*/*`` alone (curl, most SDKs) does not count — that keeps the existing
    401 for anything that is not plainly a browser. Any mention of
    ``text/event-stream`` wins: that is the MCP client shape, and an MCP
    client must never receive HTML.
    """
    if not accept:
        return False
    media = [part.split(";", 1)[0].strip().lower() for part in accept.split(",")]
    if "text/event-stream" in media:
        return False
    return "text/html" in media or "application/xhtml+xml" in media


FactsProvider = Callable[[], Awaitable[ServerFacts]]


class LandingPageMiddleware:
    """Pure-ASGI middleware serving the landing page for browser ``GET``s.

    Sits *outside* the Starlette router (passed via ``mcp.http_app(middleware=…)``),
    which is what lets it answer before the ``RequireAuthMiddleware`` FastMCP
    wraps around the MCP route. Everything that is not
    ``GET <path>`` + ``Accept: text/html`` is forwarded to the wrapped app
    unchanged.

    Args:
        app: The wrapped ASGI application.
        path: The MCP endpoint path (FastMCP's ``streamable_http_path``).
        facts: Async callable returning the :class:`ServerFacts` to render.
            Called on first use and cached; the catalogue is fixed for the
            process lifetime, so re-reading it per request would only spend
            time.
    """

    def __init__(self, app: ASGIApp, *, path: str, facts: FactsProvider) -> None:
        self.app = app
        self._path = "/" + path.strip("/")
        self._facts_provider = facts
        self._facts: ServerFacts | None = None

    def _matches(self, scope: Scope) -> bool:
        if scope.get("type") != "http" or scope.get("method") != "GET":
            return False
        route_path = _route_path(scope)
        if ("/" + route_path.strip("/")) != self._path:
            return False
        return wants_html(_header(scope, b"accept"))

    async def _facts_cached(self) -> ServerFacts:
        if self._facts is None:
            self._facts = await self._facts_provider()
        return self._facts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._matches(scope):
            await self.app(scope, receive, send)
            return
        facts = await self._facts_cached()
        nonce = secrets.token_urlsafe(16)
        body = render_page(facts, nonce=nonce)
        response = HTMLResponse(
            body,
            headers={
                # Inline style/script only, and only the ones we stamped with
                # this response's nonce; nothing may be fetched from anywhere.
                "Content-Security-Policy": (
                    f"default-src 'none'; style-src 'nonce-{nonce}'; "
                    f"script-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'"
                ),
                "X-Content-Type-Options": "nosniff",
                "X-Robots-Tag": "noindex, nofollow",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            },
        )
        await response(scope, receive, send)
