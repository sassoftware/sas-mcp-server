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
import re
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
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
    write would erase every login in it. A UTF-8 byte-order mark, which some
    editors add, is accepted; a file in another encoding raises
    :class:`UnicodeDecodeError` (a :class:`ValueError`).
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")  # noqa: TRY004 - same type as a JSON syntax error
    return data


def _blocks_for_update(path: Path) -> dict[str, Any]:
    """Like :func:`load_blocks`, but set aside a file that is not valid JSON.

    The file is moved to ``<name>.corrupt-<timestamp>`` rather than overwritten, so a
    hand-edit mistake costs the user nothing they cannot recover, and a later incident
    does not replace an earlier backup. A read error still raises, leaving the file
    untouched, and so does a file in another encoding: it may hold valid logins, so it
    is the user's to convert, not ours to discard.
    """
    try:
        return load_blocks(path)
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not UTF-8 text, so it was left as it is ({exc.reason})") from exc
    except ValueError:
        path = _resolved(path)
        stamp = time.strftime("%Y%m%dT%H%M%S")
        backup = path.with_name(f"{path.name}.corrupt-{stamp}")
        n = 1
        while backup.exists():
            n += 1
            backup = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
        os.replace(path, backup)
        logger.warning("%s is not a valid credentials file; moved it to %s and starting a new one", path, backup)
        return {}


# How long a writer waits for another process's update, and after how long a lock
# file is taken to be left behind by a process that died mid-update. An update holds
# the lock for milliseconds (a few seconds at most, when icacls is slow on Windows).
_LOCK_TIMEOUT = 15.0
_LOCK_STALE_AFTER = 60.0


def _acquire(lock: Path) -> int | None:
    """Create *lock* exclusively and return its descriptor.

    Returns ``None`` when no lock file can be created at all (a read-only directory),
    so the update goes ahead unlocked, as it did before locking existed. Raises
    :class:`TimeoutError` (an :class:`OSError`) when another process holds the lock
    for longer than ``_LOCK_TIMEOUT``.
    """
    deadline = time.monotonic() + _LOCK_TIMEOUT
    while True:
        try:
            return os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        except PermissionError:
            # With no lock file there, the directory is not writable. (On Windows an
            # existing lock file can also answer this way; that is waited for below.)
            if not lock.exists():
                logger.debug("Cannot create %s; updating without a lock", lock)
                return None
        except OSError as exc:
            logger.debug("Cannot lock %s (%s); updating without a lock", lock, exc)
            return None
        try:
            age = time.time() - os.stat(lock).st_mtime
        except FileNotFoundError:
            continue  # released between our attempt and the stat
        except OSError:
            age = 0.0
        if age > _LOCK_STALE_AFTER:
            logger.warning("Removing %s, left behind by an update that did not finish", lock)
            with contextlib.suppress(OSError):
                os.unlink(lock)
            continue
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{lock} is held by another process; try again")
        time.sleep(0.05)


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold ``<path>.lock`` across a read-modify-write of *path*.

    Several stdio servers share the file. Without the lock, two refreshes at the same
    moment could each write the file from a copy read before the other's write, and
    the loser's new tokens would be lost. With SAS Logon rotating refresh tokens, that
    leaves a revoked refresh token in the file and costs a fresh login.
    """
    lock = path.with_name(f"{path.name}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = _acquire(lock)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(lock)


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


def _write_via_temp(path: Path, text: str, *, harden: bool) -> None:
    """Write *text* to a temp file next to *path*, flush it to disk, then rename it over *path*.

    The temp file gets its final permissions before it holds any token: on POSIX mkstemp
    creates it with mode 0600, and on Windows (where that mode is ignored and the file
    would take the directory's inherited ACL) *harden* restricts its ACL first. A file
    that is not ours (*harden* false) keeps its existing POSIX permission bits instead.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        if harden:
            win_harden(Path(tmp))
        else:
            with contextlib.suppress(OSError):
                os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            # Without this a crash right after the rename can leave an empty file on
            # filesystems that may persist the rename before the data (XFS, btrfs, NFS).
            os.fsync(fh.fileno())
        _replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_in_place(path: Path, text: str, *, harden: bool) -> None:
    path.write_text(text, encoding="utf-8")
    if harden:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        win_harden(path)


def _write_atomic(path: Path, data: dict[str, Any], *, harden: bool) -> None:
    """Replace *path* with *data* without ever exposing a partial or world-readable file.

    Where a rename is impossible (see ``_RENAME_UNAVAILABLE``) the file is rewritten in place
    instead, as it was before, so a setup that could update the file still can.

    A file that belongs to another tool (*harden* false) is rewritten in place on Windows:
    a renamed-in file would carry the directory's inherited ACL and silently replace
    whatever ACL that tool or an administrator set on it.
    """
    path = _resolved(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2)
    if not harden and sys.platform == "win32" and path.exists():
        _write_in_place(path, text, harden=False)
        return
    try:
        _write_via_temp(path, text, harden=harden)
    except OSError as exc:
        if not (isinstance(exc, PermissionError) or exc.errno in _RENAME_UNAVAILABLE):
            raise
        logger.debug("Could not replace %s (%s); writing it in place", path, exc)
        _write_in_place(path, text, harden=harden)


def update_block(path: Path, key: str, block: dict[str, Any], *, harden: bool = True) -> None:
    """Set ``key`` to *block* in the credentials file, keeping every other key.

    *harden* restricts the file to the current user (mode 0600, and an owner-only ACL on
    Windows). Pass ``False`` for a file that belongs to another tool, such as the SAS
    Viya CLI's own cache: its permissions are kept as they are.

    The read and the write happen under a lock file, so concurrent updates from several
    servers cannot undo each other. Raises :class:`OSError` (including
    :class:`TimeoutError` when the lock stays taken) or :class:`ValueError` (a file
    that is not UTF-8 text) and leaves the file untouched.
    """
    path = _resolved(path)
    with _locked(path):
        data = _blocks_for_update(path)
        data[key] = block
        _write_atomic(path, data, harden=harden)


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
    with _locked(path):
        data = load_blocks(path)
        removed = [key] if data.pop(key, None) is not None else []
        default = data.get("Default")
        if isinstance(default, dict):
            bound = default.get("endpoint")
            if isinstance(bound, str) and normalize_endpoint(bound) == key:
                del data["Default"]
                removed.append("Default")
        if removed:
            _write_atomic(path, data, harden=True)
    return removed, isinstance(data.get("Default"), dict) and not data["Default"].get("endpoint")


def endpoint_keys(data: dict[str, Any]) -> list[str]:
    """The keys of *data* that hold a per-endpoint login (not ``Default`` or other entries)."""
    return [k for k, v in data.items() if k != "Default" and "://" in k and isinstance(v, dict)]


# --- names for endpoints (``sas-mcp-login zeus``) ------------------------------------
#
# Kept in their own file, ``endpoints.json`` next to the credentials: a name is a
# convenience for typing the login command, not a secret, and token refreshes never
# rewrite it. The stdio server does not read it; VIYA_ENDPOINT stays a real URL.

_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class EndpointNotFoundError(ValueError):
    """A name that is neither a saved name nor a single stored endpoint's host."""


def valid_name(name: str) -> bool:
    """Whether *name* can name an endpoint: letters, digits, ``.``, ``_`` and ``-``."""
    return bool(_NAME.fullmatch(name.lower()))


def load_names(path: Path) -> dict[str, str]:
    """``{name: normalised endpoint}`` from the names file; ``{}`` when it is missing or unreadable."""
    try:
        data = load_blocks(path)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read endpoint names at %s: %s", path, exc)
        return {}
    return {
        k.lower(): normalize_endpoint(v)
        for k, v in data.items()
        if isinstance(v, str) and "://" in v and valid_name(k)
    }


def save_name(path: Path, name: str, endpoint: str) -> str | None:
    """Point *name* at *endpoint*; return the endpoint it pointed at before, if another."""
    name = name.lower()
    target = normalize_endpoint(endpoint)
    path = _resolved(path)
    with _locked(path):
        data = _blocks_for_update(path)
        previous = data.get(name)
        data[name] = target
        _write_atomic(path, data, harden=True)
    if isinstance(previous, str) and normalize_endpoint(previous) != target:
        return normalize_endpoint(previous)
    return None


def _host_labels(endpoint: str) -> set[str]:
    try:
        host = (urlsplit(endpoint).hostname or "").lower()
    except ValueError:
        return set()
    return {host, *host.split(".")} - {""}


def resolve_endpoint(value: str, names: dict[str, str], known: list[str]) -> tuple[str, str]:
    """Turn what the user typed into an endpoint URL: ``(url, how)``.

    *how* is ``"url"`` for a full URL (returned as typed), ``"name"`` for a saved
    name, or ``"host"`` when *value* is one dot-separated part of the host of exactly
    one endpoint in *known* (``zeus`` for ``https://viya4-s2.zeus.example.com``).
    Raises :class:`EndpointNotFoundError` for anything else, naming the candidates when
    the word fits several endpoints.
    """
    raw = value.strip()
    if "://" in raw:
        return raw, "url"
    word = raw.lower()
    if word in names:
        return names[word], "name"
    matches = sorted({ep for ep in known if word in _host_labels(ep)})
    if len(matches) == 1:
        return matches[0], "host"
    if matches:
        raise EndpointNotFoundError(
            f"'{raw}' matches several endpoints: {', '.join(matches)}. "
            "Give the full URL, or save a name with --name."
        )
    raise EndpointNotFoundError(
        f"'{raw}' is not a saved name or part of a stored endpoint's host. Give the full URL "
        "(https://...) the first time, optionally with --name to save a name for it."
    )
