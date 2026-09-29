# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SAS Studio Flow (.flw) generator.

Builds a Studio flow — source table → SAS-program step(s) → output table,
wired left-to-right — as a .flw JSON file saved into SAS Content, so the flow
appears in SAS Studio's Explorer and opens as a node-and-edge diagram. The
diagram doubles as a human-readable lineage picture of the pipeline the agent
just built.

The .flw format is not publicly documented; this generator mirrors the schema
harvested from real flows on the target environment (see
``scripts/probe_lineage.py`` → ``flw_schema_sample.json``): a flow object with
``nodes`` (``table`` / ``step`` / ``outputTable``), ``connections``
(sourcePort → targetPort), and table-structure ``parameters``. SAS-program
steps embed their code via ``codeOptions`` and reference the environment's
generic "SAS Program" step definition.
"""

import json
import time
from datetime import UTC
from typing import Any

from fastmcp import Context, FastMCP

from ..config import DEMO_FOLDER_PATH, VIYA_ENDPOINT
from ..viya_client import logger
from ._vi_links import content_file_url

# The generic "SAS Program" step definition on this environment (harvested).
_SAS_PROGRAM_STEP_URI = "/dataFlows/steps/a7190700-f59c-4a94-afe2-214ce639fcde"
_DEFAULT_CAS_SERVER = "cas-shared-default"

_X_SPACING = 240
_Y = 120.0


def _next_id(counter: list[int]) -> str:
    counter[0] += 1
    return f"id-{int(time.time() * 1000)}-{counter[0]:05d}"


def _table_parameter(param_id: str, caslib: str, table: str, server: str,
                     usage: str) -> dict[str, Any]:
    """Table-structure parameter in the FULL shape SAS Studio requires.

    Studio's flow editor refuses to open ("Cannot open flow") a flow whose
    table parameters carry only a minimal ``defaultValue.table``: the
    ``attributes``, ``source``, and ``columns`` blocks must be present —
    their VALUES need not be accurate (verified live: a flow with borrowed
    attribute/column values renders fine; Studio refreshes them from the
    actual table), but the structure is mandatory.
    """
    return {
        "id": param_id,
        "name": table.upper(),
        "version": 2,
        "parameterUsage": usage,
        "parameterType": "tableStructure",
        "defaultValue": {
            "table": {
                "creationTimeStamp": "2024-01-01T00:00:00.000Z",
                "modifiedTimeStamp": "2024-01-01T00:00:00.000Z",
                "version": 2,
                "name": table.upper(),
                "providerId": "cas",
                "dataSourceId": f"cas~fs~{server}~fs~{caslib}",
                "type": "dataTable",
                "attributes": {
                    "bookmarkLength": 12, "columnCount": 1,
                    "compressionRoutine": "NO", "dataTableType": "table",
                    "encoding": "utf-8  Unicode (UTF-8)", "engine": "V9",
                    "logicalRecordCount": 0, "physicalRecordCount": 0,
                    "recordLength": 8, "rowCount": 0, "version": 3,
                },
            },
            "source": {
                "creationTimeStamp": "0001-01-01T00:00:00Z",
                "modifiedTimeStamp": "0001-01-01T00:00:00Z",
                "version": 1,
                "id": caslib, "name": table.upper(), "providerId": "cas",
                "parentId": server,
                "hasTables": True, "hasEngines": False,
                "attributes": {
                    "concatenationCount": 1, "engineName": "V9",
                    "fileFormat": "7", "flags": 34849, "options": "",
                    "physicalName": "", "readOnly": False, "version": 2,
                },
            },
        },
        "columns": [
            {
                "version": 2, "name": "_placeholder_", "label": "_placeholder_",
                "index": 0, "type": "Char", "rawLength": 8,
                "formattedLength": 0, "indexed": False,
                "format": {"name": "$", "length": 8, "decimals": 0},
                "informat": {"name": "$", "length": 8, "decimals": 0},
            },
        ],
    }


def _table_node(node_id: str, name: str, x: int) -> dict[str, Any]:
    return {
        "description": "", "id": node_id, "name": name.upper(),
        "nodeType": "table", "note": None, "priority": 0,
        "properties": {
            "UI_PROP_LOCATION": f"{x} {_Y}",
            "UI_PROP_INPUT_PORT|inTable|0": "|inTable|",
            "UI_PROP_OUTPUT_PORT|outTable|0": "|outTable|",
            "usePersistedTableStructure": "false",
        },
        "version": 1,
        "tableReference": {"referenceType": "parameter", "parameterId": node_id},
    }


def _step_node(node_id: str, name: str, code: str, x: int,
               priority: int) -> dict[str, Any]:
    return {
        "description": "", "id": node_id, "name": name,
        "nodeType": "step", "note": None, "priority": priority,
        "properties": {
            "UI_PROP_LOCATION": f"{x} {_Y}",
            "UI_PROP_INPUT_PORT|inTables|0": "|Input table 1|Input tables",
            "UI_PROP_OUTPUT_PORT|outTables|0": "|Output table 1|Output tables",
            "UI_PROP_IS_INPUT_EXPANDED": "false",
            "UI_PROP_IS_OUTPUT_EXPANDED": "false",
        },
        "version": 1,
        "stepReference": {"type": "uri", "path": _SAS_PROGRAM_STEP_URI},
        "arguments": {
            "codeOptions": {
                "code": code,
                "contentType": "embedded",
                "variables": [
                    {"name": "_input1",
                     "value": {"portIndex": 0, "portName": "inTables",
                               "referenceType": "inputPort"}},
                    {"name": "_output1",
                     "value": {"arguments": {}, "portIndex": 0,
                               "portName": "outTables",
                               "referenceType": "outputPort"}},
                ],
            },
        },
        "portMappings": None,
    }


def _output_table_node(node_id: str, name: str, x: int,
                       priority: int) -> dict[str, Any]:
    return {
        "description": "", "id": node_id, "name": name,
        "nodeType": "outputTable", "note": None, "priority": priority,
        "properties": {
            "UI_PROP_LOCATION": f"{x} {_Y}",
            "UI_PROP_INPUT_PORT|inTable|0": "|inTable|",
            "UI_PROP_OUTPUT_PORT|outTable|0": "||",
            "usePersistedTableStructure": "false",
        },
        "version": 1,
        "tableReference": {"referenceType": "parameter", "parameterId": node_id},
        "outputTableArguments": {"advancedOptions": [], "arguments": {}},
    }


def _connection(src_node: str, src_port: str, dst_node: str,
                dst_port: str) -> dict[str, Any]:
    return {
        "sourcePort": {"node": src_node, "portName": src_port, "index": 0},
        "targetPort": {"node": dst_node, "portName": dst_port, "index": 0},
    }


def _inner_flow(nodes: dict[str, Any], parameters: dict[str, Any],
                connections: list[dict[str, Any]]) -> dict[str, Any]:
    """The dataFlow object embedded inside a canvas box (dataFlow node)."""
    from datetime import datetime
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "id": None, "eTag": None, "name": "", "description": "",
        "createdBy": None, "creationTimeStamp": ts,
        "modifiedBy": None, "modifiedTimeStamp": ts,
        "connections": connections,
        "nodes": nodes,
        "parameters": parameters,
        "properties": {"UI_PROP_DF_OPTIMIZE": "false",
                       "UI_PROP_DF_EXECUTION_ORDERED": "true"},
        "statusHandling": [],
        "stickyNotes": [],
        "extendedProperties": {},
        "sourceVersion": 2,
        "version": 4,
    }


def _stage_box(counter: list[int], stage_name: str, code: str,
               in_caslib: str, in_table: str, out_caslib: str, out_table: str,
               server: str, y: float, priority: int) -> dict[str, Any]:
    """One canvas box: a dataFlow node whose inner flow is table → step → output.

    This wrapper shape mirrors the .flw files that verifiably render on this
    environment (flat top-level table/step nodes parse but do NOT render —
    SAS Studio fails to open the flow with "Cannot open flow")."""
    nodes: dict[str, Any] = {}
    parameters: dict[str, Any] = {}
    connections: list[dict[str, Any]] = []

    src_id = _next_id(counter)
    nodes[src_id] = _table_node(src_id, in_table, 0)
    parameters[src_id] = _table_parameter(src_id, in_caslib, in_table,
                                          server, "INPUT")
    step_id = _next_id(counter)
    nodes[step_id] = _step_node(step_id, stage_name, code, _X_SPACING, 1)
    connections.append(_connection(src_id, "outTable", step_id, "inTables"))
    out_id = _next_id(counter)
    nodes[out_id] = _output_table_node(out_id, out_table.upper(),
                                       _X_SPACING * 2, 2)
    parameters[out_id] = _table_parameter(out_id, out_caslib, out_table,
                                          server, "OUTPUT")
    connections.append(_connection(step_id, "outTables", out_id, "inTable"))

    box_id = _next_id(counter)
    return {
        "description": "", "id": box_id, "name": stage_name,
        "nodeType": "dataFlow", "note": None, "priority": priority,
        # SWIMLANE_STEP marks this as a canvas swimlane box — without it SAS
        # Studio fails to open the flow.
        "stepId": "SWIMLANE_STEP",
        "properties": {
            "UI_PROP_IS_EXPANDED": "true",
            "UI_PROP_LOCATION": f"0 {y}",
        },
        "version": 1,
        "dataFlowAndBindings": {
            "dataFlow": _inner_flow(nodes, parameters, connections),
            "dataFlowReference": None,
            "executionBindings": {
                "environmentId": "Compute", "contextId": "",
                "arguments": {"__NO_OPTIMIZE": {"argumentType": "string",
                                                "value": "true"}},
                "tempTablePrefix": "", "sources": None, "interactive": True,
            },
        },
    }


_BOX_Y_START = 36.0
_BOX_Y_SPACING = 136.0


def build_flow(
    name: str,
    source_caslib: str,
    source_table: str,
    steps: list[dict[str, str]],
    output_caslib: str,
    output_table: str,
    server: str = _DEFAULT_CAS_SERVER,
    description: str = "",
) -> dict[str, Any]:
    """Assemble the .flw JSON.

    Each step becomes one canvas box (dataFlow node) chained through
    intermediate tables: the first box reads the source table, the last box
    writes the output table, boxes in between hand over via
    ``{output_table}_S{k}`` staging names. One box per step means each demo
    update adds a visible box to the canvas.
    """
    counter = [0]
    boxes: dict[str, Any] = {}
    in_lib, in_tbl = source_caslib, source_table
    for i, step in enumerate(steps):
        last = i == len(steps) - 1
        out_lib = output_caslib if last else output_caslib
        out_tbl = output_table if last else f"{output_table}_S{i + 1}"
        box = _stage_box(counter, step["name"], step["code"],
                         in_lib, in_tbl, out_lib, out_tbl, server,
                         _BOX_Y_START + _BOX_Y_SPACING * i, i)
        boxes[box["id"]] = box
        in_lib, in_tbl = out_lib, out_tbl

    from datetime import datetime
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "name": name,
        "description": description,
        "connections": [],
        "nodes": boxes,
        "parameters": {},
        "properties": {"UI_PROP_DF_OPTIMIZE": "false",
                       "UI_PROP_DF_EXECUTION_ORDERED": "true"},
        "statusHandling": [],
        "stickyNotes": [],
        "extendedProperties": {},
        "sourceVersion": 2,
        "version": 4,
        "creationTimeStamp": ts,
        "modifiedTimeStamp": ts,
    }


_FLW_CONTENT_TYPE = "application/vnd.sas.data.flow+json"


async def find_flow_id(client, folder_path: str, name: str) -> str | None:
    """Return the dataFlows id of the flow member *name* in *folder_path*."""
    from ..content import ensure_folder

    folder_id = await ensure_folder(client, folder_path)
    resp = await client.get(
        f"{VIYA_ENDPOINT}/folders/folders/{folder_id}/members",
        params={"filter": f'eq(name,"{name}")', "limit": 5},
        headers={"Accept": "application/vnd.sas.collection+json"},
    )
    resp.raise_for_status()
    items = list(resp.json().get("items", []))
    # Tolerate a legacy ".flw"-suffixed member name for the same flow.
    resp2 = await client.get(
        f"{VIYA_ENDPOINT}/folders/folders/{folder_id}/members",
        params={"filter": f'eq(name,"{name}.flw")', "limit": 5},
        headers={"Accept": "application/vnd.sas.collection+json"},
    )
    if resp2.status_code == 200:
        items += resp2.json().get("items", [])
    for member in items:
        uri = member.get("uri", "")
        if uri.startswith("/dataFlows/dataFlows/"):
            return uri.rsplit("/", 1)[-1]
    return None


async def save_or_replace_flow(client, folder_path: str, name: str,
                               flow: dict[str, Any]) -> dict[str, str]:
    """Create or update the flow as a **dataFlows service object** registered
    as a folder member — the form SAS Studio actually opens. (A raw .flw file
    in the Files service shows in the tree but Studio refuses to open it.)

    Updating in place keeps the object URI stable while the flow grows during
    a live demo. Uses ETag-checked PUT; retries once on 412 (last writer wins).
    """
    from ..content import ensure_folder

    existing = await find_flow_id(client, folder_path, name)
    if existing:
        for _ in range(2):
            g = await client.get(f"{VIYA_ENDPOINT}/dataFlows/dataFlows/{existing}")
            g.raise_for_status()
            etag = g.headers.get("etag", "")
            body = dict(flow)
            body["id"] = existing
            resp = await client.put(
                f"{VIYA_ENDPOINT}/dataFlows/dataFlows/{existing}",
                content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": _FLW_CONTENT_TYPE,
                         "Accept": _FLW_CONTENT_TYPE, "If-Match": etag},
            )
            if resp.status_code == 412:
                continue
            resp.raise_for_status()
            return {"id": existing, "uri": f"/dataFlows/dataFlows/{existing}",
                    "updated": "true"}
        raise RuntimeError(f"Flow update kept conflicting (412): {name}")

    resp = await client.post(
        f"{VIYA_ENDPOINT}/dataFlows/dataFlows",
        content=json.dumps(flow, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": _FLW_CONTENT_TYPE, "Accept": _FLW_CONTENT_TYPE},
    )
    resp.raise_for_status()
    flow_id = resp.json()["id"]
    folder_id = await ensure_folder(client, folder_path)
    member = await client.post(
        f"{VIYA_ENDPOINT}/folders/folders/{folder_id}/members",
        json={"name": name, "uri": f"/dataFlows/dataFlows/{flow_id}",
              "type": "child", "contentType": "dataFlow"},
        headers={
            "Content-Type": "application/vnd.sas.content.folder.member+json",
            "Accept": "application/vnd.sas.content.folder.member+json",
        },
    )
    member.raise_for_status()
    return {"id": flow_id, "uri": f"/dataFlows/dataFlows/{flow_id}",
            "updated": "false"}


def register_flw_tools(mcp: FastMCP, get_token) -> None:
    """Register the SAS Studio Flow generator tool on *mcp*."""
    from ..viya_client import make_client

    @mcp.tool()
    async def create_studio_flow(
        ctx: Context,
        name: str,
        source_table: str,
        output_table: str,
        steps: list[dict[str, str]],
        folder_path: str | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        """Create a SAS Studio Flow (.flw) in SAS Content: source table →
        SAS-program step(s) → output table, wired as a left-to-right diagram.

        The flow appears in SAS Studio's Explorer and opens as an editable
        node diagram — a visual lineage of the pipeline. Calling this again
        with the SAME name updates the file IN PLACE (stable URL), so to show
        a flow "growing" during a live demo, call repeatedly with one more
        step each time and have the viewer reopen/refresh the flow tab.

        Args:
            name: Flow name (file is saved as "{name}.flw").
            source_table: Source as "CASLIB.TABLE" (e.g. "Public.PARTY_SUMMARY2").
            output_table: Output as "CASLIB.TABLE".
            steps: Ordered SAS-program steps: [{"name": "...", "code": "..."}].
                In step code, read from &_input1. and write to &_output1..
            folder_path: SAS Content folder (defaults to DEMO_FOLDER_PATH).
            description: Optional flow description.
        """
        logger.info("--- TOOL USED: create_studio_flow ---")
        src_lib, _, src_tbl = source_table.partition(".")
        out_lib, _, out_tbl = output_table.partition(".")
        if not src_tbl or not out_tbl:
            return {"error": "source_table / output_table must be 'CASLIB.TABLE'"}
        flow = build_flow(name, src_lib, src_tbl, steps, out_lib, out_tbl,
                          description=description)
        target = folder_path or DEMO_FOLDER_PATH
        token = await get_token(ctx)
        async with make_client(token) as client:
            saved = await save_or_replace_flow(client, target, name, flow)
            result: dict[str, Any] = {
                "status": "ok",
                "flow": f"{target}/{name}",
                "flow_id": saved["id"],
                "replaced_existing": saved.get("updated") == "true",
                "stage_boxes": len(flow["nodes"]),
                "steps": [s["name"] for s in steps],
                **content_file_url(target, name),
                "api_url": f"{VIYA_ENDPOINT}{saved['uri']}",
            }
            # Lineage: the flow transforms source into output.
            # Wrapped in try/except — lineage.py is wired separately in mcp_server.py.
            try:
                from ..lineage import lineage_edges, record_failsoft
                await record_failsoft(
                    client, result,
                    lineage_edges([source_table], [output_table], saved["uri"]),
                )
            except ImportError:
                pass
        return result
