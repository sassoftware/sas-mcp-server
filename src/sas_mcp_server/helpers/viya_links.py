# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Links into the SAS Viya web applications, for a person to open.

A tool result that names an asset is more useful with the page a person would
open to look at it. These are plain browser URLs on the Viya host: the person
signs in to Viya as usual when they follow one.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import VIYA_ENDPOINT

_DECISION_URI = re.compile(r"/decisions/flows/([0-9A-Za-z-]+)(?:/revisions/([0-9A-Za-z-]+))?/?")


def decision_url(decision_id: Any) -> str | None:
    """SAS Intelligent Decisioning's page for a decision flow."""
    if not decision_id:
        return None
    return f"{VIYA_ENDPOINT}/SASDecisionManager/decisions/{decision_id}"


def decision_from_uri(uri: Any) -> tuple[str, str | None] | None:
    """``(decision id, revision id or None)`` for a decision flow URI, else ``None``.

    Published decisions record where they came from as
    ``/decisions/flows/<id>/revisions/<revision id>``.
    """
    match = _DECISION_URI.fullmatch(uri) if isinstance(uri, str) else None
    if match is None:
        return None
    return match.group(1), match.group(2)
