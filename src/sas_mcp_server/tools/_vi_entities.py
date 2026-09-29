# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AML entity tools: SAS Visual Investigator Data Hub documents (read-only).

The Data Hub exposes the productised entities (party/account/transaction/case…)
as "documents" of an entity type. These read the entity's full field values and
traverse relationships (network/link analysis) via the product API, instead of
re-deriving them from raw CAS tables. READ-ONLY.

Confirmed against this Viya:
- Entity type name for party = ``PTY`` (from an alert's ``actionableEntityType``).
- List: ``GET /svi-datahub/documents/{type}`` needs ``Accept: application/json``
  AND ``Accept-Item: application/vnd.sas.investigation.data.enriched.document``.
- Single: ``GET /svi-datahub/documents/{type}/{id}`` needs ``Accept: application/json``
  (the enriched.document media type 500s on this box — use application/json).
- Relationship traversal: ``GET /svi-datahub/documents/{type}/{id}/{relationshipType}``.
- Entity type metadata (schema, not data): ``GET /svi-datahub/admin/storedObjects/listAll``
  (summary, all types), ``.../all`` (full, all types), ``.../storedObjects?name=``
  (full, by name), ``.../storedObjects/{id}`` (full, by id).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..config import VIYA_ENDPOINT
from ..viya_client import get_json, logger, make_client

_DH = "/svi-datahub/documents"
_DOC_ITEM = "application/vnd.sas.investigation.data.enriched.document"


def _entity_summary(e: dict[str, Any]) -> dict[str, Any]:
    """id + type + the entity's field values (the useful KYC/entity content)."""
    return {
        "id": e.get("id"),
        "entityType": e.get("objectTypeName"),
        "validFrom": e.get("validFrom"),
        "validTo": e.get("validTo"),
        "fields": e.get("fieldValues", {}),
    }


def _entity_type_field_summary(f: dict[str, Any]) -> dict[str, Any]:
    return {"name": f.get("name"), "label": f.get("label"), "dataType": f.get("dataType"),
            "required": f.get("required"), "primaryKeyField": f.get("primaryKeyField")}


def _entity_type_summary(et: dict[str, Any], *, detailed: bool = False) -> dict[str, Any]:
    """Trim an entity type (``storedObjects``) object down to the useful metadata.

    ``detailed=True`` adds fields/relationships for the single-object endpoints;
    the ``listAll`` summary representation never has them, so omit there.
    """
    summary = {
        "id": et.get("id"),
        "name": et.get("name"),
        "label": et.get("label"),
        "type": et.get("type"),
        "dataStoreName": et.get("dataStoreName"),
        "tableName": et.get("tableName"),
        "systemReserved": et.get("systemReserved"),
        "parentName": et.get("parentName"),
    }
    if detailed:
        summary["fields"] = [_entity_type_field_summary(f) for f in et.get("fields", [])]
        summary["relationshipsFrom"] = [r.get("name") for r in et.get("relationshipsFrom", [])]
        summary["relationshipsTo"] = [r.get("name") for r in et.get("relationshipsTo", [])]
    return summary


def register_aml_entities(mcp: FastMCP, get_token) -> None:
    @asynccontextmanager
    async def viya_session(name: str, ctx: Context) -> AsyncIterator[httpx.AsyncClient]:
        logger.info("--- TOOL USED: %s ---", name)
        token = await get_token(ctx)
        async with make_client(token) as client:
            yield client

    @mcp.tool()
    async def list_entities(entity_type: str, ctx: Context,
                            limit: int = 20, start: int = 0) -> dict[str, Any]:
        """List Data Hub entity documents of a type (product entities). Read-only.

        Use instead of raw CAS party tables — this returns the productised entity
        with all its field values.

        Args:
            entity_type: Entity type name, e.g. ``PTY`` (party). Types are
                deployment-specific; an alert's ``actionableEntityType`` gives one.
            limit: Max entities (default 20).
            start: Pagination offset.
        """
        url = f"{VIYA_ENDPOINT}{_DH}/{entity_type}"
        async with viya_session("list_entities", ctx) as client:
            resp = await client.get(
                url, headers={"Accept": "application/json", "Accept-Item": _DOC_ITEM},
                params={"start": start, "limit": limit})
            resp.raise_for_status()
            data = resp.json()
        items = data.get("items", []) if isinstance(data, dict) else data
        count = data.get("count", len(items)) if isinstance(data, dict) else len(items)
        return {"count": count, "entityType": entity_type,
                "entities": [_entity_summary(e) for e in items]}

    @mcp.tool()
    async def get_entity(entity_type: str, document_id: str, ctx: Context) -> dict[str, Any]:
        """Get one Data Hub entity document by type + id (full field values). Read-only.

        For a party (``PTY``) this is the API-backed KYC record: party_name,
        party_number, PEP indicator, residence/citizenship country, occupation,
        income, tax id, etc.

        Args:
            entity_type: Entity type name (e.g. ``PTY``).
            document_id: The Data Hub document id (``id`` field, from ``list_entities``).
                Note this is the Data Hub id, not an alert's actionableEntityId.
        """
        async with viya_session("get_entity", ctx) as client:
            data = await get_json(f"{_DH}/{entity_type}/{document_id}", client)
        return {"id": data.get("id"),
                "entityType": data.get("objectTypeName", entity_type),
                "validFrom": data.get("validFrom"), "validTo": data.get("validTo"),
                "fields": data.get("fieldValues", {})}

    @mcp.tool()
    async def get_related_entities(entity_type: str, document_id: str,
                                   relationship_type: str, ctx: Context,
                                   limit: int = 20, start: int = 0) -> dict[str, Any]:
        """Traverse a relationship from an entity to its related entities (network/link). Read-only.

        Given an entity and a relationship type defined for it, returns the
        related entity documents — the product-API basis for network/link
        analysis (related parties, shared counterparties, etc.).

        Args:
            entity_type: Source entity type (e.g. ``PTY``).
            document_id: Source Data Hub document id.
            relationship_type: A relationship type name defined for this entity
                type in the deployment's data model.
            limit: Max related entities (default 20).
            start: Pagination offset.
        """
        url = f"{VIYA_ENDPOINT}{_DH}/{entity_type}/{document_id}/{relationship_type}"
        async with viya_session("get_related_entities", ctx) as client:
            # The relationship endpoint returns relationship-link items; it wants
            # Accept-Item: application/json (enriched.document → 415 here).
            resp = await client.get(
                url, headers={"Accept": "application/json", "Accept-Item": "application/json"},
                params={"start": start, "limit": limit})
            resp.raise_for_status()
            data = resp.json()
        items = data.get("items", []) if isinstance(data, dict) else data
        count = data.get("count", len(items)) if isinstance(data, dict) else len(items)
        return {"count": count, "relationshipType": relationship_type, "related": items}

    @mcp.tool()
    async def list_relationships(ctx: Context, from_type: str | None = None,
                                 limit: int = 200) -> dict[str, Any]:
        """List Data Hub relationship types (name + from/to entity types). Read-only.

        Use this to discover valid ``relationship_type`` names (and entity type
        names) for ``get_related_entities``. E.g. for a case (``tm_cases``) the
        relationship named ``alerts`` traverses to its alerts.

        Args:
            from_type: Optional — only relationships whose source is this entity
                type (e.g. ``tm_cases``, ``PTY``, ``ACC``).
            limit: Max relationships to return (default 200).
        """
        url = f"{VIYA_ENDPOINT}/svi-datahub/admin/relationships"
        async with viya_session("list_relationships", ctx) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            data = resp.json()
        rels = data if isinstance(data, list) else data.get("items", [])
        out = [{"name": r.get("name"), "label": r.get("label"),
                "fromType": r.get("fromObjectName"), "toType": r.get("toObjectName"),
                "cardinality": r.get("cardinality")} for r in rels]
        if from_type:
            out = [r for r in out if r.get("fromType") == from_type]
        return {"count": len(out), "relationships": out[:limit]}

    @mcp.tool()
    async def list_entity_types(ctx: Context, exclude_unauthorized: bool = False) -> dict[str, Any]:
        """List all Data Hub entity types in summary form (name, label, data store...). Read-only.

        Use this to discover valid ``entity_type`` names (e.g. ``PTY``) for
        ``list_entities``/``get_entity`` without pulling each type's full field
        list — for that, use ``get_entity_type``/``list_entity_types_detailed``.

        Args:
            exclude_unauthorized: Drop types the caller isn't authorized to view.
                Only admin users can set this to True; non-admins get it ignored
                or an error from Viya.
        """
        url = f"{VIYA_ENDPOINT}/svi-datahub/admin/storedObjects/listAll"
        async with viya_session("list_entity_types", ctx) as client:
            resp = await client.get(
                url, headers={"Accept": "application/json"},
                params={"excludeUnauthorized": exclude_unauthorized})
            resp.raise_for_status()
            data = resp.json()
        types = data if isinstance(data, list) else data.get("items", [])
        return {"count": len(types), "entityTypes": [_entity_type_summary(t) for t in types]}

    @mcp.tool()
    async def list_entity_types_detailed(ctx: Context, exclude_unauthorized: bool = False) -> dict[str, Any]:
        """List all Data Hub entity types with full metadata (fields, relationships...). Read-only.

        Heavier than ``list_entity_types`` — includes each type's field
        definitions and relationship names. Prefer ``list_entity_types`` unless
        you need field/relationship detail for every type at once.

        Args:
            exclude_unauthorized: Drop types the caller isn't authorized to view.
                Only admin users can set this to True; non-admins get it ignored
                or an error from Viya.
        """
        url = f"{VIYA_ENDPOINT}/svi-datahub/admin/storedObjects/all"
        async with viya_session("list_entity_types_detailed", ctx) as client:
            resp = await client.get(
                url, headers={"Accept": "application/json"},
                params={"excludeUnauthorized": exclude_unauthorized})
            resp.raise_for_status()
            data = resp.json()
        types = data if isinstance(data, list) else data.get("items", [])
        return {"count": len(types), "entityTypes": [_entity_type_summary(t, detailed=True) for t in types]}

    @mcp.tool()
    async def get_entity_type(name: str, ctx: Context, exclude_unauthorized: bool = False) -> dict[str, Any]:
        """Get one Data Hub entity type's full metadata by name (fields, relationships...). Read-only.

        Args:
            name: The entity type's name (e.g. ``PTY``).
            exclude_unauthorized: Only admin users can set this to True.
        """
        url = f"{VIYA_ENDPOINT}/svi-datahub/admin/storedObjects"
        async with viya_session("get_entity_type", ctx) as client:
            resp = await client.get(
                url, headers={"Accept": "application/json"},
                params={"name": name, "excludeUnauthorized": exclude_unauthorized})
            resp.raise_for_status()
            data = resp.json()
        return _entity_type_summary(data, detailed=True)

    @mcp.tool()
    async def get_entity_type_by_id(entity_type_id: str, ctx: Context,
                                    exclude_unauthorized: bool = False) -> dict[str, Any]:
        """Get one Data Hub entity type's full metadata by ID (fields, relationships...). Read-only.

        Args:
            entity_type_id: The entity type's numeric ID (``id`` field, from
                ``list_entity_types``/``get_entity_type``).
            exclude_unauthorized: Only admin users can set this to True.
        """
        url = f"{VIYA_ENDPOINT}/svi-datahub/admin/storedObjects/{entity_type_id}"
        async with viya_session("get_entity_type_by_id", ctx) as client:
            resp = await client.get(
                url, headers={"Accept": "application/json"},
                params={"excludeUnauthorized": exclude_unauthorized})
            resp.raise_for_status()
            data = resp.json()
        return _entity_type_summary(data, detailed=True)
