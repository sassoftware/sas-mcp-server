# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SAS Content (folders + files) helpers.

Puts agent-generated artifacts — .flw flows, audit exports — INTO the SAS
Content tree so they surface in SAS Studio's Explorer. This differs from a plain
Files-service upload (``upload_file``), which lands in File Manager only:
creating a file with ``parentFolderUri`` registers it as a folder member, which
is what SAS Studio displays.

Endpoints:

* ``GET  /folders/folders/@item?path=/Public/...``  — resolve folder by path
* ``POST /folders/folders?parentFolderUri=...``     — create child folder
* ``POST /files/files?parentFolderUri=...``         — create file in folder
"""

import httpx

from .config import VIYA_ENDPOINT
from .viya_client import logger

# path -> folder id, so repeated saves don't re-walk the folder tree.
_folder_cache: dict[str, str] = {}


async def ensure_folder(client: httpx.AsyncClient, path: str) -> str:
    """Resolve *path* (e.g. ``/Public/Claude Demo/2026-07-07``) to a folder id,
    creating missing segments. Results are cached per process."""
    path = "/" + path.strip("/")
    if path in _folder_cache:
        return _folder_cache[path]

    # Walk down from the deepest already-known ancestor.
    segments = [s for s in path.split("/") if s]
    current_path = ""
    parent_id: str | None = None
    for seg in segments:
        current_path += f"/{seg}"
        if current_path in _folder_cache:
            parent_id = _folder_cache[current_path]
            continue
        resp = await client.get(f"{VIYA_ENDPOINT}/folders/folders/@item", params={"path": current_path})
        if resp.status_code == 200:
            parent_id = resp.json()["id"]
        elif resp.status_code == 404:
            if parent_id is None:
                # Top-level folders (/Public etc.) are platform-managed; we do
                # not create them so a typo can't silently spawn a new root.
                raise RuntimeError(
                    f"Root folder '{current_path}' does not exist on this Viya environment; check DEMO_FOLDER_PATH."
                )
            create = await client.post(
                f"{VIYA_ENDPOINT}/folders/folders",
                params={"parentFolderUri": f"/folders/folders/{parent_id}"},
                json={"name": seg},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/vnd.sas.content.folder+json",
                },
            )
            create.raise_for_status()
            parent_id = create.json()["id"]
            logger.info("Created SAS Content folder %s (%s)", current_path, parent_id)
        else:
            resp.raise_for_status()
        if parent_id is None:
            raise RuntimeError(f"Could not resolve SAS Content folder '{current_path}'.")
        _folder_cache[current_path] = parent_id
    if parent_id is None:
        raise RuntimeError(f"Invalid SAS Content folder path '{path}'.")
    return parent_id


async def save_text_file(
    client: httpx.AsyncClient,
    folder_path: str,
    filename: str,
    text: str,
    content_type: str = "text/plain",
    type_def_name: str | None = None,
) -> dict[str, str]:
    """Create *filename* with *text* inside the SAS Content folder *folder_path*.

    On a name conflict (409) the name is suffixed ``-2``, ``-3``, … — each save
    keeps its own file; nothing is overwritten.

    Returns ``{"id", "name", "uri"}`` of the created file.
    """
    folder_id = await ensure_folder(client, folder_path)
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    params: dict[str, str] = {"parentFolderUri": f"/folders/folders/{folder_id}"}
    if type_def_name:
        # e.g. "dataFlow" for .flw files — without it SAS Studio refuses to
        # open the file ("unsupported file type").
        params["typeDefName"] = type_def_name
    name = filename
    for attempt in range(2, 12):
        resp = await client.post(
            f"{VIYA_ENDPOINT}/files/files",
            params=params,
            content=text.encode("utf-8"),
            headers={
                "Content-Type": content_type,
                "Content-Disposition": f'attachment; filename="{name}"',
                "Accept": "application/vnd.sas.file+json",
            },
        )
        if resp.status_code == 409:
            name = f"{stem}-{attempt}.{ext}" if ext else f"{stem}-{attempt}"
            continue
        resp.raise_for_status()
        body = resp.json()
        return {"id": body["id"], "name": body.get("name", name), "uri": f"/files/files/{body['id']}"}
    raise RuntimeError(f"Could not create '{filename}' in {folder_path}: name conflicts persisted after 10 retries.")
