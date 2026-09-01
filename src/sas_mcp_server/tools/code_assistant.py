# Copyright © 2026, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 10 — Code Assistance & Documentation tools.

Two tools over the SAS Code Assistant copilot: one answers a SAS documentation
question, the other generates code from natural-language requirements. Both go
through Viya's own GenAI Gateway (``/genAiGateway/v1/copilotRequest``) with the
authenticated user's bearer token, so a deployment needs no separate GenAI or
LLM API key and no RAG endpoint — Viya owns model selection and routing, and
neither a credential nor the submitted source is persisted here. The tier needs
the GenAI Gateway provisioned on the instance, as Tier 7 needs SAS Intelligent
Decisioning and Tier 9 needs SAS Data Governance.

The tier deliberately ships no execution tool: ``execute_sas_code`` in Tier 0 or
Tier 8 remains the only way to run what the assistant produces.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from pydantic import BeforeValidator

from ..viya_client import post_json
from ._common import coerce_str_or_json_list, make_session_helpers

# Tolerant alias for a list param some MCP clients deliver as a JSON-encoded
# string, or as a bare string (see _common.coerce_str_or_json_list). The
# published schema is unchanged.
ProductParam = Annotated[list[str], BeforeValidator(coerce_str_or_json_list)]

_COPILOT_PATH = "/genAiGateway/v1/copilotRequest"
_APPLICATION_NAME = "SAS MCP Server"
_COPILOT_ID = "codeAssistant"
_COPILOT_VERSION = "v1"

# Documentation sets the copilot searches when the caller names none.
_DEFAULT_PRODUCTS = ["SAS Studio", "SAS Studio with SAS Viya Platform Programming Documentation"]

# The gateway validates UserRequest.content as a required, non-empty string. For
# a code command the copilot drives the action from context.commandId and treats
# content as an optional user message, so this fixed instruction satisfies the
# gateway without changing what the copilot does.
_GENERATE_PROMPT = "Generate code from the provided requirements."


def _envelope(message: dict[str, Any]) -> dict[str, Any]:
    """Wrap a copilot ``message`` in the GenAI Gateway request envelope.

    The gateway routes to a registered copilot identified by ``copilot.id`` /
    ``copilot.version`` on behalf of ``applicationName``.
    """
    return {
        "applicationName": _APPLICATION_NAME,
        "copilot": {"id": _COPILOT_ID, "version": _COPILOT_VERSION},
        "message": message,
    }


def _content(payload: Any, *, operation: str) -> str:
    """Extract the copilot's textual reply from a gateway response body.

    The reply is returned either at the top level or nested under ``message``;
    both shapes are accepted so a gateway that moves it does not break the tool.
    An empty or absent reply is an error rather than an empty string, so a
    caller is never handed silence that looks like an answer.
    """
    content: Any = None
    if isinstance(payload, dict):
        if isinstance(payload.get("content"), str):
            content = payload["content"]
        elif isinstance(payload.get("message"), dict) and isinstance(payload["message"].get("content"), str):
            content = payload["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"The Code Assistant {operation} service returned an empty response.")
    return content


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 10 (Code Assistance & Documentation) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

    @mcp.tool()
    async def get_doc_answer(question: str, ctx: Context, product: ProductParam | None = None) -> dict[str, str]:
        """Answer a SAS documentation question using the Code Assistant knowledge base.

        Args:
            question: The documentation question to answer.
            product: Optional documentation sets to search (defaults to the SAS
                Studio and SAS Viya Platform Programming documentation).
        """
        if not question.strip():
            raise ValueError("question must not be empty.")
        async with viya_session("get_doc_answer", ctx) as client:
            body = _envelope(
                {
                    "type": "userRequest",
                    "content": question,
                    "context": {"type": "doc", "product": product or _DEFAULT_PRODUCTS},
                }
            )
            payload = await post_json(_COPILOT_PATH, client, body)
        return {"answer": _content(payload, operation="documentation")}

    @mcp.tool()
    async def generate_sas_code(
        prompt: str,
        ctx: Context,
        language: Literal["sas", "python", "r"] = "sas",
        use_rag_for_sas: bool = True,
        product: ProductParam | None = None,
    ) -> dict[str, str]:
        """Generate SAS, Python, or R code from natural-language requirements.

        Returns code only — nothing is executed. Run the result with
        ``execute_sas_code`` (Tier 0 or Tier 8) when execution is required.

        Args:
            prompt: The requirements to generate code from.
            language: Target language (default "sas").
            use_rag_for_sas: Ground SAS generation in the documentation
                (default True).
            product: Optional documentation sets for the grounding above.
        """
        if not prompt.strip():
            raise ValueError("prompt must not be empty.")
        async with viya_session("generate_sas_code", ctx) as client:
            context: dict[str, Any] = {
                "type": "code",
                "commandId": "generate",
                "useRAGForSAS": use_rag_for_sas,
                "languageId": language,
                "selectedText": prompt,
                "currentFile": {"name": f"mcp-request.{language}", "language": language, "content": prompt},
            }
            if product:
                context["product"] = product
            body = _envelope({"type": "userRequest", "content": _GENERATE_PROMPT, "context": context})
            payload = await post_json(_COPILOT_PATH, client, body)
        return {"generated_code": _content(payload, operation="generate")}
