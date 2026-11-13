# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Where an asset is stored: resolve a folder reference, and keep assets in folders.

SAS Content folders are how people find, share and secure what they build. A
rule set or decision flow created without ``parentFolderUri`` belongs to no
folder: it exists, but no folder view shows it. The tools that create such
assets therefore always place them, and this module turns whatever the caller
gave into a folder.

A folder reference may be:

* a folder URI, ``/folders/folders/<id>``, including ``/folders/folders/@myFolder``;
* a bare folder ID (a UUID);
* ``My Folder`` (also ``@myFolder`` or ``~``), optionally followed by a path
  below it: ``My Folder/Risk``;
* a path from the root, ``/Public/Content/Decisions`` (the leading slash is
  optional).

A tool given no folder falls back to the deployment's ``MCP_DEFAULT_FOLDER``.
When that is unset as well, :func:`resolve_target_folder` refuses with a message
that tells the model to ask the user, so nothing is created outside a folder.

Behaviour this relies on, probed against SAS Viya in October 2026:

* ``POST /businessRules/ruleSets`` and ``POST /decisions/flows`` take
  ``parentFolderUri``, accept the ``@myFolder`` alias in it, and refuse an
  unknown folder with 404 before creating anything.
* ``PUT`` updates and revision locks leave an asset in its folder.
* ``GET /folders/ancestors?childUri=`` lists the parents nearest first and the
  root last, without the item itself; 404 means the item is in no folder.
* ``POST /folders/paths`` finds a folder from its path segments.
"""

import re
from dataclasses import dataclass
from typing import Any

import httpx

from .. import config
from ..viya_client import post_json, raise_for_viya_status

FOLDER_URI_PREFIX = "/folders/folders/"
MY_FOLDER_URI = f"{FOLDER_URI_PREFIX}@myFolder"
_MY_FOLDER_ALIASES = frozenset({"my folder", "@myfolder", "~"})
_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_JSON = {"Accept": "application/json"}

# {"id", "uri", "name", "path"}: what a tool reports about a folder.
FolderInfo = dict[str, str]


class FolderError(ValueError):
    """A folder that cannot be used. The message says what to do instead."""


@dataclass(frozen=True)
class FolderReference:
    """A parsed folder reference: a URI to look up, or a path to find."""

    uri: str | None = None
    segments: tuple[str, ...] = ()
    under_my_folder: bool = False


def parse_folder_reference(reference: str) -> FolderReference:
    """Classify *reference* without calling Viya. Raises FolderError if it names no folder."""
    value = reference.strip()
    if not value:
        raise FolderError("The folder is empty. Give a folder path, 'My Folder' or a folder ID.")
    if value.lower().startswith(FOLDER_URI_PREFIX):
        ident = value[len(FOLDER_URI_PREFIX):].strip("/")
        if not ident or "/" in ident:
            raise FolderError(f"{reference!r} is not a folder URI. A folder URI is /folders/folders/<id>.")
        if ident.lower() == "@myfolder":
            return FolderReference(uri=MY_FOLDER_URI)
        return FolderReference(uri=FOLDER_URI_PREFIX + ident)
    if _UUID.fullmatch(value):
        return FolderReference(uri=FOLDER_URI_PREFIX + value)
    segments = tuple(part.strip() for part in value.split("/") if part.strip())
    if not segments:
        raise FolderError(f"{reference!r} names no folder. Give a folder path, 'My Folder' or a folder ID.")
    if segments[0].lower() in _MY_FOLDER_ALIASES:
        if len(segments) == 1:
            return FolderReference(uri=MY_FOLDER_URI)
        return FolderReference(segments=segments[1:], under_my_folder=True)
    return FolderReference(segments=segments)


def _info(folder: dict[str, Any], names: list[str]) -> FolderInfo:
    folder_id = folder["id"]
    name = folder.get("name") or (names[-1] if names else "")
    return {
        "id": folder_id,
        "uri": FOLDER_URI_PREFIX + folder_id,
        "name": name,
        "path": "/" + "/".join(names),
    }


def _ancestors(data: Any) -> list[dict[str, Any]]:
    """The ancestor list, nearest first, from either response shape."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("ancestors") or data.get("items") or []
    return []


def _viya_message(resp: httpx.Response) -> str:
    try:
        message = resp.json().get("message")
    except (ValueError, AttributeError):
        message = None
    return message or f"HTTP {resp.status_code}"


async def _get_folder(client: httpx.AsyncClient, uri: str, reference: str) -> dict[str, Any]:
    resp = await client.get(f"{config.VIYA_ENDPOINT}{uri}", headers=_JSON)
    if resp.status_code == 404:
        if uri == MY_FOLDER_URI:
            raise FolderError("Your My Folder could not be found. Give another folder.")
        raise FolderError(
            f"No folder {reference!r} exists, or you are not allowed to see it. "
            "Ask the user for the folder's path, such as /Public/Content/Decisions."
        )
    raise_for_viya_status(resp)
    return resp.json()


async def _path_names(client: httpx.AsyncClient, folder_id: str) -> list[str]:
    """Names of the folders above *folder_id*, root first."""
    resp = await client.get(
        f"{config.VIYA_ENDPOINT}/folders/ancestors",
        params={"childUri": FOLDER_URI_PREFIX + folder_id},
        headers=_JSON,
    )
    if resp.status_code == 404:  # a root folder has no ancestors
        return []
    raise_for_viya_status(resp)
    return [a.get("name", "") for a in reversed(_ancestors(resp.json()))]


async def _find_by_path(client: httpx.AsyncClient, names: list[str]) -> dict[str, Any]:
    resp = await client.post(
        f"{config.VIYA_ENDPOINT}/folders/paths",
        json={"items": names, "contentType": "folder"},
        headers={"Content-Type": "application/json", **_JSON},
    )
    if resp.status_code == 404:
        raise FolderError(
            f"There is no folder {'/' + '/'.join(names)!r} ({_viya_message(resp)}). "
            "Ask the user for the correct folder path."
        )
    raise_for_viya_status(resp)
    return resp.json()


async def resolve_folder(client: httpx.AsyncClient, reference: str) -> FolderInfo:
    """Turn a folder reference into the folder's ID, URI, name and full path."""
    ref = parse_folder_reference(reference)
    if ref.uri is not None:
        folder = await _get_folder(client, ref.uri, reference)
        names = [*await _path_names(client, folder["id"]), folder.get("name", "")]
        return _info(folder, names)
    if ref.under_my_folder:
        my_folder = await _get_folder(client, MY_FOLDER_URI, reference)
        base = [*await _path_names(client, my_folder["id"]), my_folder.get("name", "My Folder")]
        names = [*base, *ref.segments]
    else:
        names = list(ref.segments)
    folder = await _find_by_path(client, names)
    return _info(folder, [*names[:-1], folder.get("name") or names[-1]])


def no_folder_message(what: str) -> str:
    return (
        f"No folder was given for the {what}, and this deployment has no default folder "
        "(MCP_DEFAULT_FOLDER). Ask the user where to store it: a folder path such as "
        "/Public/Content/Decisions, 'My Folder', or a folder ID. Then call again with "
        "folder set to their answer."
    )


async def resolve_target_folder(client: httpx.AsyncClient, folder: str | None, what: str) -> FolderInfo:
    """The folder to store a new *what* in: *folder* if given, else MCP_DEFAULT_FOLDER.

    Raises FolderError when neither is set, so the asset is never created
    outside a folder, and when the folder cannot be found.
    """
    if folder is not None and folder.strip():
        return await resolve_folder(client, folder)
    default = config.MCP_DEFAULT_FOLDER
    if not default:
        raise FolderError(no_folder_message(what))
    try:
        return await resolve_folder(client, default)
    except FolderError as exc:
        raise FolderError(
            f"The default folder MCP_DEFAULT_FOLDER={default!r} cannot be used: {exc} "
            "Give a folder explicitly, or ask an administrator to correct the setting."
        ) from None


def _relative_without_default(path: str) -> str:
    return (
        f"{path!r} is a relative path, and relative paths are created under the deployment's "
        "default folder (MCP_DEFAULT_FOLDER), which is not set. Give the full path, starting with "
        "/ (such as /Public/Content/Risk) or with 'My Folder/', or ask the user where to create it."
    )


async def _deepest_existing(client: httpx.AsyncClient, names: list[str], floor: int) -> tuple[int, dict[str, Any]]:
    """The longest prefix of *names*, at least *floor* long, that exists: (its length, the folder)."""
    for depth in range(len(names), floor - 1, -1):
        try:
            return depth, await _find_by_path(client, names[:depth])
        except FolderError:
            continue
    raise FolderError(
        f"There is no folder {'/' + '/'.join(names[:floor])!r} to create {'/' + '/'.join(names)!r} in. "
        "Folders at the top level, such as Public, are created by administrators."
    )


async def create_folder_path(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    """Create the SAS Content folder *path*, with any missing levels, and report it.

    ``/Public/Content/Risk`` and ``My Folder/Risk`` are created where they say;
    a relative path such as ``Risk/2026`` under MCP_DEFAULT_FOLDER, and not at
    all when that is unset. A folder that already exists is returned unchanged,
    so the call can be repeated. A folder URI or ID names an existing folder.
    """
    value = path.strip()
    ref = parse_folder_reference(value)
    if ref.uri is not None:
        return {**await resolve_folder(client, value), "status": "exists", "created": []}
    if value.startswith("/"):
        base: list[str] = []
        segments = list(ref.segments)
        floor = 1
    elif ref.under_my_folder:
        my_folder = await _get_folder(client, MY_FOLDER_URI, value)
        base = [*await _path_names(client, my_folder["id"]), my_folder.get("name", "My Folder")]
        segments = list(ref.segments)
        floor = len(base)
    else:
        default = config.MCP_DEFAULT_FOLDER
        if not default:
            raise FolderError(_relative_without_default(value))
        try:
            anchor = await resolve_folder(client, default)
        except FolderError as exc:
            raise FolderError(
                f"The default folder MCP_DEFAULT_FOLDER={default!r} cannot be used: {exc} "
                "Give the full path instead, starting with / or 'My Folder/'."
            ) from None
        base = [name for name in anchor["path"].split("/") if name]
        segments = list(ref.segments)
        floor = len(base)
    bad = [s for s in segments if s in (".", "..")]
    if bad:
        raise FolderError(f"{path!r} contains {bad[0]!r}. Give the folder names themselves.")
    names = [*base, *segments]
    depth, folder = await _deepest_existing(client, names, floor)
    created: list[str] = []
    for index in range(depth, len(names)):
        resp = await client.post(
            f"{config.VIYA_ENDPOINT}/folders/folders",
            params={"parentFolderUri": FOLDER_URI_PREFIX + folder["id"]},
            json={"name": names[index], "type": "folder"},
            headers={"Content-Type": "application/vnd.sas.content.folder+json", **_JSON},
        )
        if resp.status_code == 409:  # created meanwhile by someone else: use it
            folder = await _find_by_path(client, names[: index + 1])
            continue
        raise_for_viya_status(resp)
        folder = resp.json()
        created.append("/" + "/".join(names[: index + 1]))
    return {**_info(folder, names), "status": "created" if created else "exists", "created": created}


async def current_folder(client: httpx.AsyncClient, asset_uri: str) -> FolderInfo | None:
    """The folder *asset_uri* sits in, or None when it is in no folder."""
    resp = await client.get(
        f"{config.VIYA_ENDPOINT}/folders/ancestors",
        params={"childUri": asset_uri},
        headers=_JSON,
    )
    if resp.status_code == 404:
        return None
    raise_for_viya_status(resp)
    ancestors = _ancestors(resp.json())
    if not ancestors:
        return None
    return _info(ancestors[0], [a.get("name", "") for a in reversed(ancestors)])


async def file_into_folder(
    client: httpx.AsyncClient, folder: FolderInfo, asset_uri: str, name: str, content_type: str
) -> None:
    """Add *asset_uri* to *folder* as a child member."""
    await post_json(
        f"{FOLDER_URI_PREFIX}{folder['id']}/members",
        client,
        body={"uri": asset_uri, "type": "child", "name": name, "contentType": content_type},
    )


async def keep_in_folder(
    client: httpx.AsyncClient,
    *,
    asset_uri: str,
    name: str,
    content_type: str,
    folder: str | None,
    what: str,
) -> dict[str, str]:
    """After an update: report the asset's folder, filing it first if it has none.

    An asset already in a folder stays there, whatever *folder* says. One in no
    folder, created before folders were required, is filed into *folder* or the
    default folder. When neither is available the update still stands and the
    result says how to file it, rather than failing an update that succeeded.
    """
    existing = await current_folder(client, asset_uri)
    if existing is not None:
        result: dict[str, str] = {**existing, "status": "unchanged"}
        if folder is not None and folder.strip():
            result["note"] = (
                f"The {what} stays in {existing['path']}: an asset that is already in a "
                "folder keeps it, so the folder argument was not used."
            )
        return result
    try:
        target = await resolve_target_folder(client, folder, what)
    except FolderError as exc:
        return {"status": "none", "note": f"The {what} is in no folder and was not filed. {exc}"}
    await file_into_folder(client, target, asset_uri, name, content_type)
    return {**target, "status": "filed"}
