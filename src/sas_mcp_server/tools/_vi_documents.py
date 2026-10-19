# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AML document-write tools: SAS Visual Investigator Data Hub (svi-datahub).

Wraps the ``/documents`` write/query operations that ``_vi_entities.py``
deliberately leaves untouched (that module is read-only via plain GETs):

- ``POST /documents`` — create one document of any entity type.
- ``POST /documents/{entityTypeName}`` — filter/fetch-by-id (a "GET via POST";
  read-only despite the verb — POST only keeps filter values out of logs).
- ``POST /documents/bulk`` — create/update many documents in one call.
- ``PATCH /documents/{entityTypeName}/{documentId}`` — update specific fields
  of one existing document via JSON Patch (RFC 6902), gated on the document's
  current version through ``If-Match`` for optimistic concurrency.
- ``POST``/``DELETE``/``GET /locks/documents`` — lock/unlock/query a document
  lock. Locking is optional (Data Hub doesn't enforce it on PATCH/DELETE) but
  is how two concurrent editors avoid clobbering each other's changes.

The actual writes are GATED (``dry_run`` defaults True) — this is generic
document creation/update for any entity type in the deployment's data model,
unlike ``submit_flow_change_for_approval`` which creates one specific,
well-known ``tm_inquiry`` document as part of VI's approval workflow. Prefer
that tool for flow/scenario changes; use these only for entity data (parties,
accounts, etc.) that has no dedicated tool of its own.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..config import VIYA_ENDPOINT
from ..viya_client import logger, make_client

_DH = "/svi-datahub/documents"
_LOCKS = "/svi-datahub/locks/documents"


def _dumps(obj: Any) -> bytes:
    """UTF-8 JSON bytes (ensure_ascii=False preserves non-ASCII names).

    Sent via ``content=``, not ``json=``: httpx's ``json=`` shortcut forces
    ``Content-Type: application/json``, which 415s on these endpoints — they
    require the specific vendor media type set explicitly in ``headers``.
    """
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _raise_for_status(resp: httpx.Response) -> None:
    """``raise_for_status`` that keeps Viya's response body in the error.

    Data Hub explains a 400/415 (bad field value, unsupported media type) only
    in the body; plain ``raise_for_status`` drops it, leaving the caller to guess.
    """
    if resp.is_error:
        raise httpx.HTTPStatusError(
            f"HTTP {resp.status_code} for {resp.request.url}. "
            f"Viya said: {resp.text[:400] or '(no response body)'}",
            request=resp.request, response=resp)


async def _acquire_lock(client: httpx.AsyncClient, entity_type: str, document_id: str) -> None:
    """Place a document lock. Raises (incl. 409 — already locked by another user)."""
    resp = await client.post(
        f"{VIYA_ENDPOINT}{_LOCKS}", content=_dumps({}),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        params={"type": entity_type, "id": document_id})
    _raise_for_status(resp)


async def _release_lock(client: httpx.AsyncClient, entity_type: str, document_id: str) -> None:
    """Release a document lock. Succeeds (204) even if no lock was held."""
    resp = await client.delete(
        f"{VIYA_ENDPOINT}{_LOCKS}", headers={"Accept": "application/json"},
        params={"type": entity_type, "id": document_id})
    _raise_for_status(resp)


def _entity_summary(e: dict[str, Any]) -> dict[str, Any]:
    """id + type + the entity's field values (matches _vi_entities.py's shape)."""
    return {
        "id": e.get("id"),
        "entityType": e.get("objectTypeName"),
        "validFrom": e.get("validFrom"),
        "validTo": e.get("validTo"),
        "fields": e.get("fieldValues", {}),
    }


def register_aml_documents(mcp: FastMCP, get_token) -> None:
    @asynccontextmanager
    async def viya_session(name: str, ctx: Context) -> AsyncIterator[httpx.AsyncClient]:
        logger.info("--- TOOL USED: %s ---", name)
        token = await get_token(ctx)
        async with make_client(token) as client:
            yield client

    @mcp.tool()
    async def create_entity_document(entity_type: str, field_values: dict[str, Any],
                                     ctx: Context, dry_run: bool = True,
                                     valid_from: str | None = None,
                                     valid_to: str | None = None) -> dict[str, Any]:
        """Create a new Data Hub document (entity record) of a given type. GATED WRITE.

        Writes a new record into VI's Data Hub — e.g. a party, account, or any
        other entity type defined in this deployment's data model. This is a
        generic create for entity data; for detection-flow/scenario changes use
        ``submit_flow_change_for_approval`` instead, which routes through VI's
        approval queue with its own validation guardrails.

        Args:
            entity_type: Entity type name to create (``objectTypeName``), e.g. ``PTY``.
            field_values: The document's field values. Shape is defined by the
                entity type's own schema in this deployment — not validated here.
            dry_run: Keep True to preview the document that would be created.
                False actually creates it in VI's Data Hub.
            valid_from: Optional ISO 8601 timestamp for the document's validity start.
            valid_to: Optional ISO 8601 timestamp for the document's validity end.
        """
        body: dict[str, Any] = {"objectTypeName": entity_type, "fieldValues": field_values}
        if valid_from:
            body["validFrom"] = valid_from
        if valid_to:
            body["validTo"] = valid_to
        if dry_run:
            return {"dry_run": True, "would_create": body,
                    "note": "Rerun with dry_run=False to actually create this "
                            "document in VI's Data Hub."}
        async with viya_session("create_entity_document", ctx) as client:
            resp = await client.post(
                f"{VIYA_ENDPOINT}{_DH}", content=_dumps(body),
                headers={"Content-Type": "application/json", "Accept": "application/json"})
            _raise_for_status(resp)
            data = resp.json()
        return {"id": data.get("id"), "entityType": data.get("objectTypeName", entity_type),
                "fields": data.get("fieldValues", {}), "createdAt": data.get("createdAt")}

    @mcp.tool()
    async def update_entity_document(entity_type: str, document_id: str,
                                     field_values: dict[str, Any], ctx: Context,
                                     dry_run: bool = True) -> dict[str, Any]:
        """Update specific fields of one existing Data Hub document. GATED WRITE.

        Sends a JSON Patch (RFC 6902) with a ``replace`` op per given field,
        instead of resending the whole document — only internal documents can
        be updated this way (external, read-only source documents can't).
        Doubly guarded against lost updates: it holds a document lock for the
        duration of the call (released even if the patch fails), on top of
        sending the document's current version back as ``If-Match`` — so a
        concurrent editor is either blocked outright (409 on the lock) or, if
        it slips in between the GET and the lock, caught by the version check
        (412) instead of silently overwritten.

        Args:
            entity_type: Entity type name the document belongs to, e.g. ``PTY``.
            document_id: The Data Hub document id to update (from
                ``list_entities``/``get_entity``/``filter_entity_documents``).
            field_values: Fields to replace, as ``{field_name: new_value}``.
                Only the given fields are touched; everything else is left as-is.
            dry_run: Keep True to preview the patch that would be sent.
                False actually applies it in VI's Data Hub.
        """
        async with viya_session("update_entity_document", ctx) as client:
            get_resp = await client.get(
                f"{VIYA_ENDPOINT}{_DH}/{entity_type}/{document_id}",
                headers={"Accept": "application/json"})
            _raise_for_status(get_resp)
            current = get_resp.json()
            version = current.get("fieldValues", {}).get("version")

            # "add", not "replace": RFC 6902 "replace" fails on a path that doesn't
            # exist yet, and Data Hub omits unset fields from fieldValues entirely.
            # "add" sets the field either way (it replaces an existing member).
            patch = [{"op": "add", "path": f"/fieldValues/{field}", "value": value}
                     for field, value in field_values.items()]
            if dry_run:
                return {"dry_run": True, "documentId": document_id, "entityType": entity_type,
                        "currentVersion": version, "would_patch": patch,
                        "note": "Rerun with dry_run=False to actually lock, patch, and "
                                "unlock this document in VI's Data Hub."}

            await _acquire_lock(client, entity_type, document_id)
            try:
                headers = {"Content-Type": "application/json", "Accept": "application/json"}
                if version is not None:
                    headers["If-Match"] = str(version)
                resp = await client.patch(
                    f"{VIYA_ENDPOINT}{_DH}/{entity_type}/{document_id}",
                    content=_dumps(patch), headers=headers)
                _raise_for_status(resp)
                data = resp.json()
            finally:
                await _release_lock(client, entity_type, document_id)
        return {"id": data.get("id"), "entityType": data.get("objectTypeName", entity_type),
                "fields": data.get("fieldValues", {}), "lastUpdatedAt": data.get("lastUpdatedAt")}

    @mcp.tool()
    async def lock_entity_document(entity_type: str, document_id: str, ctx: Context,
                                   dry_run: bool = True) -> dict[str, Any]:
        """Lock a Data Hub document so only this user can edit/delete it. GATED WRITE.

        ``update_entity_document`` already locks/unlocks around its own patch
        automatically — use this tool directly only when you need the lock to
        span more than one call (e.g. a lock, several manual reads, then an
        update). Fails with a 409 if another user already holds the lock. The
        lock is tied to the caller's session and auto-releases after 2 hours
        or when the session ends, whichever comes first — release it
        explicitly with ``unlock_entity_document`` when done rather than
        relying on the timeout.

        Args:
            entity_type: Entity type name the document belongs to, e.g. ``PTY``.
            document_id: The Data Hub document id to lock.
            dry_run: Keep True to preview. False actually places the lock.
        """
        if dry_run:
            return {"dry_run": True, "entityType": entity_type, "documentId": document_id,
                    "note": "Rerun with dry_run=False to actually lock this document."}
        async with viya_session("lock_entity_document", ctx) as client:
            await _acquire_lock(client, entity_type, document_id)
        return {"locked": True, "entityType": entity_type, "documentId": document_id}

    @mcp.tool()
    async def unlock_entity_document(entity_type: str, document_id: str, ctx: Context) -> dict[str, Any]:
        """Release this user's lock on a Data Hub document.

        Not gated behind ``dry_run``: releasing a lock only gives up access the
        caller already holds, and Data Hub returns success even if no lock was
        held, so there's nothing destructive to preview.

        Args:
            entity_type: Entity type name the document belongs to, e.g. ``PTY``.
            document_id: The Data Hub document id to unlock.
        """
        async with viya_session("unlock_entity_document", ctx) as client:
            await _release_lock(client, entity_type, document_id)
        return {"locked": False, "entityType": entity_type, "documentId": document_id}

    @mcp.tool()
    async def is_entity_document_locked(entity_type: str, document_id: str,
                                        ctx: Context) -> dict[str, Any]:
        """Check whether the caller currently holds a lock on a Data Hub document. Read-only.

        Args:
            entity_type: Entity type name the document belongs to, e.g. ``PTY``.
            document_id: The Data Hub document id to check.
        """
        async with viya_session("is_entity_document_locked", ctx) as client:
            resp = await client.get(
                f"{VIYA_ENDPOINT}{_LOCKS}", headers={"Accept": "application/json"},
                params={"type": entity_type, "id": document_id})
            _raise_for_status(resp)
            locked = resp.json()
        return {"entityType": entity_type, "documentId": document_id, "locked": bool(locked)}

    @mcp.tool()
    async def filter_entity_documents(entity_type: str, ctx: Context,
                                      filter_expr: str | None = None,
                                      document_ids: list[str] | None = None,
                                      limit: int = 20, start: int = 0,
                                      sort_by: str | None = None) -> dict[str, Any]:
        """Query Data Hub documents of a type by filter expression or by id list. Read-only.

        A richer alternative to ``list_entities``: server-side filtering
        (``eq``/``and``/``or`` only — kept narrow to avoid full-table scans) or
        fetching a specific set of documents by id in one call, instead of
        paging through everything. Uses POST only so filter values are not
        logged — the underlying svi-datahub endpoint is a "GET via POST"; this
        tool never writes anything.

        Args:
            entity_type: Entity type name, e.g. ``PTY``.
            filter_expr: A limited filter expression, e.g. ``eq(first_name,'John')``
                or ``and(eq(first_name,'John'),eq(last_name,'Smith'))``. Ignored
                if ``document_ids`` is given.
            document_ids: Fetch these specific document ids for the entity type,
                instead of filtering. Takes priority over ``filter_expr``.
            limit: Max documents to return (default 20).
            start: Pagination offset.
            sort_by: Optional sort, e.g. ``first_name:ascending;last_name:descending``.
        """
        if document_ids:
            body: dict[str, Any] = {"documentIds": document_ids, "start": start, "limit": limit}
            content_type = "application/vnd.sas.investigation.data.document.id.request+json"
        else:
            body = {"filter": filter_expr, "start": start, "limit": limit}
            content_type = "application/vnd.sas.investigation.data.document.filter.request+json"
        if sort_by:
            body["sortBy"] = sort_by
        async with viya_session("filter_entity_documents", ctx) as client:
            resp = await client.post(
                f"{VIYA_ENDPOINT}{_DH}/{entity_type}", content=_dumps(body),
                headers={"Content-Type": content_type, "Accept": "application/json",
                         "Accept-Item": "application/vnd.sas.investigation.data.document"})
            _raise_for_status(resp)
            data = resp.json()
        items = data.get("items", [])
        return {"count": data.get("count", len(items)), "entityType": entity_type,
                "entities": [_entity_summary(e) for e in items]}

    @mcp.tool()
    async def bulk_upsert_entity_documents(entity_type: str, documents: list[dict[str, Any]],
                                           ctx: Context, dry_run: bool = True) -> dict[str, Any]:
        """Create/update multiple Data Hub documents of a type in one call. GATED WRITE.

        Each item in ``documents`` is either a Create (no ``id``) or an Update
        (has ``id``) — the server infers which from the item's shape. Partial
        failure is normal: some items can succeed while others report an
        ``error``, so check each result's ``operation`` rather than assuming
        all-or-nothing.

        Args:
            entity_type: Entity type name the documents belong to — stamped as
                ``objectTypeName`` on any item that doesn't already set its own.
            documents: List of documents, each shaped like
                ``{"id": <optional>, "fieldValues": {...}}``.
            dry_run: Keep True to preview the batch. False actually writes it.
        """
        items = [{**d, "objectTypeName": d.get("objectTypeName", entity_type)}
                 for d in documents]
        if dry_run:
            return {"dry_run": True, "would_upsert_count": len(items), "items": items,
                    "note": "Rerun with dry_run=False to actually create/update "
                            "these documents."}
        async with viya_session("bulk_upsert_entity_documents", ctx) as client:
            resp = await client.post(
                f"{VIYA_ENDPOINT}{_DH}/bulk", content=_dumps({"items": items}),
                headers={"Content-Type": "application/vnd.sas.collection+json",
                         "Accept": "application/json"})
            _raise_for_status(resp)
            data = resp.json()
        results = data.get("items", [])
        failed = [r for r in results if r.get("error")]
        return {"total": len(results), "succeeded": len(results) - len(failed),
                "failed": len(failed), "results": results}
