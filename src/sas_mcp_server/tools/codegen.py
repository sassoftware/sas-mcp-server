# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 9 — Code Generation (SAS RAG Assistant).

Ports the ``sas-rag-generate-code`` tool from the ``tmp-sas-mcp-codegen``
prototype. That server only ran inside a Viya cluster, behind a gateway that
authenticated the caller and forwarded their bearer token; the tool body
itself made a plain Viya REST call once that token was in hand. Here the same
request is made through this server's own ``viya_session`` — the same helper
every other tier uses — so no cluster or gateway dependency carries over.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import Context, FastMCP

from ..viya_client import post_json
from ._common import make_session_helpers

# The GenAI Gateway routes this to the ragServer copilot on the GAP platform
# (/copilots/ragServer/v1/completion), holding the GAP credentials server-side.
# This is the "from inside Viya" path — it needs only a normal Viya bearer
# token, unlike /genAiGateway/v1/accessInfo which is reserved for approved
# external SAS applications.
_COPILOT_REQUEST_PATH = "/genAiGateway/v1/copilotRequest"
_RAG_COPILOT_ID = "ragServer"
_RAG_COPILOT_VERSION = "v1"
_APPLICATION_NAME = "Code Assistance"


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 9 (Code Generation) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

    @mcp.tool()
    async def generate_sas_code(
        prompt: str, ctx: Context, filters: str = "pgmsascdc"
    ) -> dict[str, Any]:
        """Generate SAS code from a natural-language prompt via the Viya GenAI Gateway.

        Sends *prompt* to the ragServer copilot. Requires SAS Viya Copilot /
        the GenAI Gateway service to be activated on the target Viya order —
        a Viya administrator does this via SAS Environment Manager (Manage
        Environment > Activate SAS Viya Copilot), which also shows current
        activation status. An HTTP error from this call (e.g. 404/501) most
        likely means Copilot isn't activated on this order, rather than a bug
        in the request — ask a Viya admin to confirm via Environment Manager.

        Args:
            prompt: Natural language description of the SAS code to generate
                (e.g. "Write a SAS program to read a CSV file and compute
                summary statistics").
            filters: Documentation filter corpus to search (default: pgmsascdc).

        Returns:
            Dict with ``content`` — the generated SAS code / explanation text.
        """
        body = {
            "copilot": {"id": _RAG_COPILOT_ID, "version": _RAG_COPILOT_VERSION},
            "applicationName": _APPLICATION_NAME,
            "message": {
                "type": "userRequest",
                "content": prompt,
                "context": {
                    "type": "code",
                    "filters": filters,
                    "command": "generate_code",
                    "metadata": "N/A",
                    "n": 1,
                    "temperature": 0.7,
                },
            },
        }
        async with viya_session("generate_sas_code", ctx) as client:
            data = await post_json(_COPILOT_REQUEST_PATH, client, body=body)

        content = data.get("content")
        if not content:
            raise ValueError(f"GenAI Gateway returned no content: {data}")
        return {"content": content}
