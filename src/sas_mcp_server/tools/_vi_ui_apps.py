# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Interactive MCP App providers (human-in-the-loop UI).

Registers FastMCP's prebuilt MCP-Apps providers so that clients advertising the
``io.modelcontextprotocol/ui`` extension (e.g. Claude Desktop) get real
in-conversation UI for human-in-the-loop control:

- ``Approval`` -> a ``request_approval`` tool that renders an Approve/Reject card.
  The model is instructed to call it before any irreversible action (create/run)
  and to stop until the user clicks a button — the decision returns as a message.
- ``Choice`` -> a tool that renders a pick-one card, for letting the user select
  among proposed options (e.g. model candidates).

``choose`` here is a LENIENT subclass of the prefab Choice: models sometimes
pass ``options`` as one string ("<option>…</option>" markup or newline-separated
text) instead of a list. The stock tool rejects that with a ValidationError, and
in Claude Desktop an errored ui-tool call has been observed to leave the NEXT
choice card stuck at "Waiting for content…" — so we coerce instead of erroring.

This requires the ``fastmcp[apps]`` extra (prefab-ui). If it is not installed,
registration is skipped with a warning so the core server still starts.
"""

import os
import re

from fastmcp import FastMCP

from ..viya_client import logger

# Prefab's default "cdn" renderer mode makes the card iframe load its JS/CSS
# from cdn.jsdelivr.net at view time. Claude Desktop sandboxes MCP-App iframes
# (external fetches can be CSP-blocked), which leaves the card stuck at
# "Waiting for content…" even though the tool call and resources/read both
# succeeded. Bundled mode inlines everything (~6 MB, zero network) and renders
# regardless of CSP/proxy. setdefault keeps an explicit env override possible.
os.environ.setdefault("PREFAB_BUNDLED_RENDERER", "1")

_OPTION_TAG = re.compile(r"<option[^>]*>(.*?)</option>", re.S | re.I)


def _coerce_options(options) -> list[str]:
    """Accept a proper list, ``<option>`` markup, or newline/;-separated text."""
    if isinstance(options, str):
        found = _OPTION_TAG.findall(options)
        options = found if found else re.split(r"[\n;]+", options)
    out = [str(o).strip() for o in options if str(o).strip()]
    if not out:
        raise ValueError("options must contain at least one non-empty choice")
    return out


def register_apps(mcp: FastMCP) -> None:
    """Register interactive approval/choice UI providers on *mcp* (best-effort)."""
    try:
        from fastmcp.apps.approval import Approval
        from fastmcp.apps.choice import Choice
        from prefab_ui.actions import SetState  # pyright: ignore[reportMissingImports]
        from prefab_ui.actions.mcp import SendMessage  # pyright: ignore[reportMissingImports]
        from prefab_ui.app import PrefabApp  # pyright: ignore[reportMissingImports]
        from prefab_ui.components import (  # pyright: ignore[reportMissingImports]
            H3,
            Button,
            Card,
            CardContent,
            CardFooter,
            CardHeader,
            Column,
            Muted,
            Text,
        )
        from prefab_ui.components.control_flow import If  # pyright: ignore[reportMissingImports]
        from prefab_ui.rx import STATE  # pyright: ignore[reportMissingImports]
    except ImportError:
        logger.warning(
            "fastmcp[apps] (prefab-ui) not installed; interactive approval/choice "
            "UIs are disabled. Install with: uv pip install prefab-ui"
        )
        return

    class LenientChoice(Choice):
        """Choice whose ``choose`` tool coerces sloppy ``options`` input.

        Same card as the prefab Choice; the only difference is the input
        contract (``options: list[str] | str`` + coercion) so a malformed
        call degrades to a working card instead of a ValidationError.
        """

        def _register_tools(self) -> None:
            provider = self

            @self.ui()
            def choose(
                prompt: str,
                options: list[str] | str,
                title: str | None = None,
            ) -> PrefabApp:
                """Present the user with a set of options to choose from.

                Call this tool when you need the user to make a decision
                between discrete alternatives. Use it proactively — don't
                ask the user to type their choice in chat when you can
                present clean, clickable options instead.

                The user will see a card with one button per option. When
                they click one, their choice appears as a message in the
                conversation (as if the user typed it), like:

                    "Which deployment strategy?" — I selected: Blue-green

                IMPORTANT: After calling this tool, you MUST stop and wait
                for the user's response. Do not continue or take any other
                actions until you see the "I selected:" message.

                Args:
                    prompt: The question or decision to present to the user.
                    options: List of options the user can choose from
                        (a plain list of strings — NOT markup).
                    title: Optional heading for the card.
                """
                opts = _coerce_options(options)
                _title = title or provider._title

                with Card(css_class="max-w-lg mx-auto") as view:
                    with CardHeader():
                        H3(_title)

                    with CardContent():
                        Text(prompt, css_class="font-medium")

                    with CardFooter():
                        with If(STATE.decided):
                            Muted("Response sent.")
                        with If(~STATE.decided):  # noqa: SIM117
                            with Column(gap=2, css_class="w-full"):
                                for option in opts:
                                    Button(
                                        option,
                                        variant=provider._variant,
                                        css_class="w-full justify-start",
                                        on_click=[
                                            SendMessage(
                                                f'"{prompt}" — I selected: {option}'
                                            ),
                                            SetState("decided", True),
                                        ],
                                    )

                return PrefabApp(
                    view=view,
                    state={"decided": False},
                )

    mcp.add_provider(
        Approval(
            title="Viya Operation Approval",
            approve_text="Approve",
            reject_text="Reject",
            reject_variant="destructive",
        )
    )
    mcp.add_provider(LenientChoice(title="Please choose"))
    logger.info(
        "Registered interactive MCP App providers: Approval, LenientChoice"
    )
