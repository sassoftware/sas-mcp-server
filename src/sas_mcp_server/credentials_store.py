# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for the token cache ``~/.sas-mcp-server/credentials.json``.

The cache holds one block per SAS Viya deployment next to the legacy ``Default``
block, keyed by the normalised endpoint URL, so several stdio servers can each
keep their own token in the same file.

Like :mod:`sas_mcp_server.env`, this module imports nothing from the server
configuration, so the lightweight ``sas-mcp-login`` CLI can use it without
``VIYA_ENDPOINT`` being set.
"""

import contextlib
import errno
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .fs_perms import win_harden

logger = logging.getLogger(__name__)

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_endpoint(url: str) -> str:
    """Return *url* in the form used as a cache key.

    Lower-cases scheme and host, drops a default port and trailing slashes, and
    drops any query or fragment. A value that is not a full URL is returned
    stripped of surrounding whitespace and trailing slashes.
    """
    raw = url.strip()
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return raw.rstrip("/")
    if not parts.scheme or not host:
        return raw.rstrip("/")
    scheme = parts.scheme.lower()
    if ":" in host:  # IPv6 literal: urlsplit strips the brackets
        host = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
    return f"{scheme}://{host}{parts.path.rstrip('/')}"


def load_blocks(path: Path) -> dict[str, Any]:
    """Return the whole credentials file as a dict, or ``{}`` if it does not exist.

    Raises :class:`OSError` if the file cannot be read and :class:`ValueError` if it
    is not a JSON object. Only a missing file counts as empty: a file that is merely
    unreadable at the moment must never be mistaken for an empty one, or the next
    write would erase every login in it.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")  # noqa: TRY004 - same type as a JSON syntax error
    return data


def _blocks_for_update(path: Path) -> dict[str, Any]:
    """Like :func:`load_blocks`, but set aside a file that is not valid JSON.

    The file is moved to ``<name>.corrupt`` rather than overwritten, so a hand-edit
    mistake costs the user nothing they cannot recover. A read error still raises,
    leaving the file untouched.
    """
    try:
        return load_blocks(path)
    except ValueError:
        path = _resolved(path)
        backup = path.with_name(f"{path.name}.corrupt")
        os.replace(path, backup)
        logger.warning("%s is not a valid credentials file; moved it to %s and starting a new one", path, backup)
        return {}


def _replace(src: str, dst: Path) -> None:
    """``os.replace`` that rides out a brief lock on *dst*.

    On Windows a virus scanner or indexer that has just opened the file makes the
    rename fail with ``PermissionError`` for a few milliseconds; a persistent failure
    still raises.
    """
    attempts = 5 if sys.platform == "win32" else 1
    for attempt in range(1, attempts + 1):
        try:
            os.replace(src, dst)
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(0.05 * attempt)
        else:
            return


def _resolved(path: Path) -> Path:
    """*path* with any symlink followed, so a link to a managed file is written through."""
    return Path(os.path.realpath(path))


# The rename cannot work here (a bind-mounted file, a read-only directory, a lock that does not
# clear), but the file itself may still be writable.
_RENAME_UNAVAILABLE = (errno.EBUSY, errno.EXDEV, errno.EROFS)


def _write_via_temp(path: Path, text: str) -> None:
    # mkstemp creates the file with mode 0600, so the tokens are never readable by others.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        _replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    """Replace *path* with *data* without ever exposing a partial or world-readable file.

    Where a rename is impossible (see ``_RENAME_UNAVAILABLE``) the file is rewritten in place
    instead, as it was before, so a setup that could update the file still can. The Windows
    ACL is left to the caller.
    """
    path = _resolved(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2)
    try:
        _write_via_temp(path, text)
    except OSError as exc:
        if not (isinstance(exc, PermissionError) or exc.errno in _RENAME_UNAVAILABLE):
            raise
        logger.debug("Could not replace %s (%s); writing it in place", path, exc)
        path.write_text(text, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


def update_block(path: Path, key: str, block: dict[str, Any], *, harden: bool = True) -> None:
    """Set ``key`` to *block* in the credentials file, keeping every other key.

    *harden* restricts the file's Windows ACL to the current user. Pass ``False`` for a
    file that belongs to another tool, such as the SAS Viya CLI's own cache.
    """
    path = _resolved(path)
    data = _blocks_for_update(path)
    data[key] = block
    _write_atomic(path, data)
    if harden:
        win_harden(path)  # POSIX chmod is a no-op on Windows


def remove_login(path: Path, endpoint: str) -> tuple[list[str], bool]:
    """Remove the login for *endpoint* from the credentials file.

    Removes the block keyed by the endpoint, and the legacy ``Default`` block too when
    it is recorded as belonging to that endpoint. Returns the names of the blocks
    removed and whether an unbound ``Default`` block is left in the file (it cannot be
    attributed to any endpoint, so it is kept).

    Raises :class:`OSError` or :class:`ValueError` if the file cannot be read or is
    not valid, leaving it untouched.
    """
    key = normalize_endpoint(endpoint)
    path = _resolved(path)
    data = load_blocks(path)
    removed = [key] if data.pop(key, None) is not None else []
    default = data.get("Default")
    if isinstance(default, dict):
        bound = default.get("endpoint")
        if isinstance(bound, str) and normalize_endpoint(bound) == key:
            del data["Default"]
            removed.append("Default")
    if removed:
        _write_atomic(path, data)
        win_harden(path)
    return removed, isinstance(data.get("Default"), dict) and not data["Default"].get("endpoint")
