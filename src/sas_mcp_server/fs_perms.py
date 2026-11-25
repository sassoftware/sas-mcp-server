# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""File-permission helpers shared by the credential cache and the usage logger.

Like :mod:`sas_mcp_server.env`, this module imports nothing from the rest of the
package, so the lightweight ``sas-mcp-login`` CLI can use it without the server
configuration.
"""

import contextlib
import os
import subprocess
import sys
from pathlib import Path


def win_harden(path: Path) -> None:
    """Best-effort Windows ACL lock-down (POSIX chmod is a no-op on win32).

    Removes inherited ACEs and grants only the current user Full control, so a
    log that may contain SAS code, table rows, and PII is not group/world
    readable on shared or roaming-profile hosts. Silently best-effort.

    Only ever call this on a path we own the lifecycle of — the log FILE itself,
    or a directory this process just created. Never call it on a pre-existing,
    potentially shared directory: ``/inheritance:r`` strips inheritance so that
    NEW sibling subdirectories inherit an empty DACL and become unusable.
    """
    if sys.platform != "win32":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    with contextlib.suppress(Exception):
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            check=False,
            timeout=5,
        )
