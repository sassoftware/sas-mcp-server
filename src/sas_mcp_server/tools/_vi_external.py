# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""External OSINT signals for AML scenario ideation (read-only).

`gdelt_trends` queries the public GDELT DOC 2.0 API (global news / events) to
surface emerging typologies, geopolitical events and adverse-media trends. It is
a PUBLIC internet API (no auth, no Viya). Only topic/keyword queries are sent —
**no customer PII is transmitted**. Use trends to inform scenario proposals
(pair with the internal coverage-gap analysis).

GDELT is rate-limited; call sparingly (a handful of queries per analysis).
"""

import os
from pathlib import Path
from typing import Any

import httpx

from ..viya_client import logger

_CURRENTS = "https://api.currentsapi.services/v1"
_GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"


def _currents_key() -> str:
    """API key from env, falling back to reading the repo .env directly.

    Robust to a process that started before the key was added to .env (reads the
    file at call time), so editing .env doesn't require a full MCP restart.
    """
    key = os.getenv("CURRENTS_API_KEY", "").strip()
    if key:
        return key
    try:
        from dotenv import dotenv_values
        # parents[0]=tools/, [1]=sas_mcp_server/, [2]=src/, [3]=sas-mcp-server/ (where .env lives)
        env_path = Path(__file__).resolve().parents[3] / ".env"
        return (dotenv_values(env_path).get("CURRENTS_API_KEY") or "").strip()
    except Exception:
        return ""


_MODES = {"artlist": "ArtList", "timelinevol": "TimelineVol", "tonechart": "ToneChart"}


def register_aml_external(mcp, get_token=None) -> None:
    @mcp.tool()
    async def currents_news(keywords: str, language: str = "en",
                            category: str | None = None,
                            max_records: int = 20) -> dict[str, Any]:
        """Query Currents News API for recent news on an AML topic. Read-only, no customer PII.

        Real-time global news for spotting rising crime typologies, geographies,
        sanctions/geopolitical events and adverse-media themes — pair with the
        internal coverage-gap analysis to propose scenarios. Only your topic
        keywords are sent (no customer data).

        Requires the API key in the environment as ``CURRENTS_API_KEY`` (set it in
        the sas-mcp-server ``.env``; do not hardcode it).

        Args:
            keywords: Topic keywords, e.g. ``trade based money laundering`` or
                ``sanctions evasion crypto``.
            language: Language code (default ``en``).
            category: Optional Currents category (e.g. ``finance``, ``business``).
            max_records: Max articles (1–20; free tier caps page_size at 20).
        """
        key = _currents_key()
        if not key:
            return {"error": "CURRENTS_API_KEY not set — add it to sas-mcp-server/.env "
                             "and fully restart Claude Desktop (respawns the MCP)."}
        # Free tier limits date_range to 7 days and start_date must be RFC 3339;
        # to keep it simple and robust we omit dates and return the most recent news.
        params: dict[str, Any] = {"keywords": keywords, "language": language,
                                  "page_size": max(1, min(int(max_records), 20))}
        if category:
            params["category"] = category
        logger.info("--- TOOL USED: currents_news ---")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{_CURRENTS}/search", params=params,
                                    headers={"Authorization": key})
        if resp.status_code != 200 or not resp.text.strip().startswith("{"):
            return {"error": "Currents non-JSON/error response",
                    "status": resp.status_code, "body_head": resp.text[:200]}
        data = resp.json()
        if data.get("status") != "ok":
            return {"error": "Currents API error", "status_field": data.get("status"),
                    "raw": data}
        news = data.get("news", [])
        return {"query": keywords, "language": language, "count": len(news),
                "articles": [{"title": a.get("title"), "description": a.get("description"),
                              "url": a.get("url"), "published": a.get("published"),
                              "category": a.get("category"), "language": a.get("language"),
                              "author": a.get("author")} for a in news]}

    @mcp.tool()
    async def gdelt_trends(query: str, mode: str = "artlist",
                           timespan: str = "1w", max_records: int = 25) -> dict[str, Any]:
        """Query GDELT global news for emerging AML typologies/events/adverse-media. Read-only, no PII.

        Public GDELT DOC 2.0 API — sends only your topic query (no customer data).
        Use to spot rising crime typologies, geographies or adverse-media themes,
        then align scenario proposals to them (with internal coverage-gap analysis).

        Args:
            query: GDELT query, e.g. ``"trade based money laundering"`` or
                ``money laundering (country:CH OR country:AE)``. Phrases in quotes.
            mode: ``artlist`` (recent matching articles) | ``timelinevol``
                (coverage-volume trend over time) | ``tonechart`` (tone distribution).
            timespan: Look-back window, e.g. ``1w`` / ``1m`` / ``3m`` / ``24h``.
            max_records: Max articles for artlist (1–250, default 25).
        """
        m = _MODES.get(mode.lower(), "ArtList")
        params: dict[str, Any] = {"query": query, "mode": m, "format": "json",
                                  "timespan": timespan}
        if m == "ArtList":
            params["maxrecords"] = max(1, min(int(max_records), 250))
        logger.info("--- TOOL USED: gdelt_trends (mode=%s) ---", m)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                _GDELT_DOC, params=params,
                headers={"User-Agent": "viya-aml-agent/1.0"})
        if resp.status_code != 200 or not resp.text.strip().startswith("{"):
            return {"error": "GDELT non-JSON/error response (possibly rate-limited)",
                    "status": resp.status_code, "body_head": resp.text[:200]}
        data = resp.json()
        if m == "ArtList":
            arts = data.get("articles", [])
            return {"mode": "ArtList", "query": query, "count": len(arts),
                    "articles": [{"title": a.get("title"), "url": a.get("url"),
                                  "domain": a.get("domain"),
                                  "sourceCountry": a.get("sourcecountry"),
                                  "language": a.get("language"),
                                  "seenDate": a.get("seendate")} for a in arts]}
        if m == "TimelineVol":
            tl = data.get("timeline", [])
            series = tl[0].get("data", []) if tl else []
            return {"mode": "TimelineVol", "query": query,
                    "timeline": [{"date": p.get("date"), "value": p.get("value")}
                                 for p in series]}
        return {"mode": m, "query": query, "raw": data}
