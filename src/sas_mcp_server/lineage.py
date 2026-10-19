# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Data-lineage recorder writing to the Viya Relationships service.

Records input → activity → output edges for artifacts the agent creates, so
the chain (source tables → SAS program → feature table → ML project → model →
detection flow → deployment) is queryable via ``GET /relationships`` and
explorable in SAS Information Catalog, which renders objects held in the
Relationship Service.

Conventions mirror what the platform itself writes (harvested live by
``scripts/probe_lineage.py``):

* relationship ``type``:  ``Dependent`` — *resourceUri* depends on
  *relatedResourceUri* (e.g. a report is ``Dependent`` on its source table).
* CAS table URIs use the dataTables form
  ``/dataTables/dataSources/cas~fs~{server}~fs~{caslib}/tables/{TABLE}`` —
  NOT the casManagement form — so edges attach to the same asset Information
  Catalog displays.

Behavior is gated by ``LINEAGE_BACKEND`` ("relationships" | "off"). All hook
call sites treat failures as warnings — lineage must never fail the tool that
created the artifact.
"""

from typing import Any

import httpx
from fastmcp import Context, FastMCP

from .config import LINEAGE_BACKEND, VIYA_ENDPOINT
from .viya_client import logger

_REL_CT = "application/vnd.sas.relationship+json"
_REL_TYPE = "Dependent"
_DEFAULT_CAS_SERVER = "cas-shared-default"


# ---------------------------------------------------------------------------
# URI builders — one per artifact kind the demo creates.
# ---------------------------------------------------------------------------

def cas_table_uri(caslib: str, table: str,
                  server: str = _DEFAULT_CAS_SERVER) -> str:
    return (f"/dataTables/dataSources/cas~fs~{server}~fs~{caslib}"
            f"/tables/{table.upper()}")


def ml_project_uri(project_id: str) -> str:
    return f"/mlPipelineAutomation/projects/{project_id}"


def model_uri(model_id: str) -> str:
    return f"/modelRepository/models/{model_id}"


def flow_uri(flow_id: str) -> str:
    return f"/svi-vsd-service/flows/{flow_id}"


def deployment_uri(deployment_id: str) -> str:
    return f"/svi-vsd-service/deployments/{deployment_id}"


def file_uri(file_id: str) -> str:
    return f"/files/files/{file_id}"


def parse_table_ref(ref: str, server: str = _DEFAULT_CAS_SERVER) -> str:
    """Accept a raw URI (passed through), ``CASLIB.TABLE``, or a bare table
    name (assumed caslib Public)."""
    if ref.startswith("/"):
        return ref
    caslib, dot, table = ref.partition(".")
    if not dot:
        caslib, table = "Public", ref
    return cas_table_uri(caslib, table, server)


# ---------------------------------------------------------------------------
# Edge writer
# ---------------------------------------------------------------------------

async def add_edges(
    client: httpx.AsyncClient, edges: list[tuple[str, str]]
) -> dict[str, Any]:
    """POST ``Dependent`` relationships for *edges* [(resource, related), ...].

    Returns a summary with created relationship ids and a verification URL.
    Honors ``LINEAGE_BACKEND``: anything but "relationships" is a no-op.
    """
    if LINEAGE_BACKEND != "relationships":
        return {"backend": LINEAGE_BACKEND, "skipped": True}
    ids: list[str] = []
    existing = 0
    for resource, related in edges:
        resp = await client.post(
            f"{VIYA_ENDPOINT}/relationships/relationships",
            json={"resourceUri": resource, "relatedResourceUri": related,
                  "type": _REL_TYPE},
            headers={"Content-Type": _REL_CT, "Accept": _REL_CT},
        )
        if resp.status_code == 409:
            # Idempotent: the identical edge is already recorded (e.g. a
            # demo re-run). That is the desired end state, not an error.
            existing += 1
            logger.info("Lineage edge already recorded: %s -> %s",
                        resource, related)
            continue
        resp.raise_for_status()
        ids.append(resp.json().get("id", "?"))
        logger.info("Lineage edge: %s -[%s]-> %s", resource, _REL_TYPE, related)
    return {
        "backend": "relationships",
        "edges_created": len(ids),
        "edges_already_existing": existing,
        "relationship_ids": ids,
    }


def lineage_edges(
    inputs: list[str], outputs: list[str], activity_uri: str | None
) -> list[tuple[str, str]]:
    """Shape edges: activity depends on inputs; outputs depend on activity.
    Without an activity node, outputs depend directly on inputs."""
    in_uris = [parse_table_ref(r) for r in inputs]
    out_uris = [parse_table_ref(r) for r in outputs]
    edges: list[tuple[str, str]] = []
    if activity_uri:
        edges += [(activity_uri, i) for i in in_uris]
        edges += [(o, activity_uri) for o in out_uris]
    else:
        edges += [(o, i) for o in out_uris for i in in_uris]
    return edges


async def record_failsoft(
    client: httpx.AsyncClient,
    result: dict[str, Any],
    edges: list[tuple[str, str]],
) -> None:
    """Write *edges* and merge a ``lineage`` summary into *result*; a failure
    becomes ``lineage_error`` instead of an exception (artifact creation has
    already succeeded — lineage must not undo that)."""
    if not edges:
        return
    try:
        result["lineage"] = await add_edges(client, edges)
    except Exception as exc:  # noqa: BLE001 — fail-soft by contract
        logger.warning("Lineage recording failed", exc_info=True)
        result["lineage_error"] = f"{type(exc).__name__}: {exc}"[:300]


def register_lineage_tools(mcp: FastMCP, get_token) -> None:
    """Register the explicit lineage tool on *mcp*."""
    from .viya_client import make_client

    @mcp.tool()
    async def record_data_lineage(
        ctx: Context,
        inputs: list[str],
        outputs: list[str],
        activity_uri: str | None = None,
    ) -> dict[str, Any]:
        """Record data-lineage edges (input → activity → output) in the Viya
        Relationships service so the chain shows in Information Catalog.

        Most artifact-creating tools record lineage automatically
        (``execute_sas_code`` with declared inputs/outputs, ``create_ml_project``,
        ``register_champion_model``, ``create_flow_from_template``,
        ``deploy_flow``). Use this tool for steps those hooks don't cover.

        Args:
            inputs: Source tables as "CASLIB.TABLE" (or raw Viya URIs).
            outputs: Produced artifacts as "CASLIB.TABLE" (or raw Viya URIs,
                e.g. "/modelRepository/models/{id}").
            activity_uri: Optional URI of the producing activity (e.g. the
                saved .sas program's "/files/files/{id}"). Without it, outputs
                link directly to inputs.
        """
        logger.info("--- TOOL USED: record_data_lineage ---")
        edges = lineage_edges(inputs, outputs, activity_uri)
        if not edges:
            return {"status": "no_edges", "note": "inputs/outputs were empty"}
        token = await get_token(ctx)
        async with make_client(token) as client:
            summary = await add_edges(client, edges)
        return {
            "status": "ok",
            **summary,
            "verify_api": (f"{VIYA_ENDPOINT}/relationships/relationships"
                           f"?resourceUri={edges[0][0]}"),
        }
