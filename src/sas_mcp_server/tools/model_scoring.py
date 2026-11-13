# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 6 — Model Management & Scoring tools."""

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..helpers import model_helpers as mh
from ..viya_client import (
    contains_filter,
    filter_literal,
    get_json,
    get_paged_items,
    post_json,
    return_items,
)
from ._common import make_session_helpers

_PUBLISHED_FIELDS = (
    "id", "name", "publishName", "destinationName", "publishType", "codeType", "state", "verified",
    "sourceUri", "createdBy", "creationTimeStamp", "modifiedTimeStamp",
)


def published_summary(item: dict[str, Any]) -> dict[str, Any]:
    """A published model as list_published_models reports it, with the source model's ID when there is one."""
    out = {key: item.get(key) for key in _PUBLISHED_FIELDS if item.get(key) not in (None, "")}
    source = item.get("sourceUri") or ""
    if source.startswith("/modelRepository/models/"):
        out["modelId"] = source.rsplit("/", 1)[-1]
    if item.get("errorResponse"):
        out["error"] = item["errorResponse"]
    return out


def match_input_names(input_data: dict[str, Any], step_inputs: list[str]) -> dict[str, str]:
    """The names in *input_data* the step does not know but knows with a trailing underscore.

    Modules published from SAS Intelligent Decisioning name their inputs
    ``credit_score_``; MAS compares names without case. Returns
    ``{given name: step input name}``.
    """
    known = {name.lower(): name for name in step_inputs}
    return {
        name: known[f"{name}_".lower()]
        for name in input_data
        if name.lower() not in known and f"{name}_".lower() in known and f"{name}_" not in input_data
    }


def _inputs(input_data: dict[str, Any]) -> dict[str, Any]:
    return {"inputs": [{"name": k, "value": v} for k, v in input_data.items()]}


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 6 (Model Management & Scoring) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

    @mcp.tool()
    async def list_registered_models(ctx: Context, limit: int = 50) -> list[dict[str, Any]]:
        """List models in the Model Repository.

        Args:
            limit: Maximum models to return (default 50).
        """
        async with viya_session("list_registered_models", ctx) as client:
            items, _ = await get_paged_items("/modelRepository/models", client, limit=limit)
            return return_items(items, ["id", "name", "description", "modelVersionName"])

    @mcp.tool()
    async def list_publishing_destinations(
        ctx: Context, limit: int = 50, start: int = 0, filter_name: str | None = None
    ) -> list[dict[str, Any]]:
        """List available publishing destinations.

        Args:
            ctx: FastMCP context.
            limit: Maximum destinations to return (default 50).
            start: Row offset (default 0).
            filter_name: Optional filter for destination names.
        """
        async with viya_session("list_publishing_destinations", ctx) as client:
            items, _ = await get_paged_items(
                "/modelPublish/destinations",
                client,
                limit=limit,
                start=start,
                filters=contains_filter(filter_name),
            )
            return return_items(items, ["id", "name", "description", "destinationType"])

    @mcp.tool()
    async def list_mas_modules(ctx: Context, limit: int = 50) -> list[dict[str, Any]]:
        """List published scoring models and decisions (MAS modules).

        Args:
            limit: Maximum modules to return (default 50).
        """
        async with viya_session("list_mas_modules", ctx) as client:
            items, _ = await get_paged_items("/microanalyticScore/modules", client, limit=limit)
            return return_items(items, ["id", "name", "description"])

    @mcp.tool()
    async def get_mas_module_step_signature(module_id: str, ctx: Context, step_id: str = "execute") -> dict[str, Any]:
        """Fetch a MAS module step's input/output variable signature.

        Call before ``score_data`` to know the exact variable names, types,
        and order to pass as inputs, and what outputs to expect.

        Args:
            module_id: The MAS module ID (see ``list_mas_modules``).
            step_id: The step within the module to inspect (default "execute").
        """
        async with viya_session("get_mas_module_step_signature", ctx) as client:
            return await get_json(f"/microanalyticScore/modules/{module_id}/steps/{step_id}", client)

    @mcp.tool()
    async def score_data(module_id: str, step_id: str, input_data: dict, ctx: Context) -> dict[str, Any]:
        """Score data against a published model or decision (MAS module).

        A decision published from the SAS Intelligent Decisioning UI names its
        inputs with a trailing underscore (``credit_score_``); the plain names
        are accepted and mapped, and the result's ``inputs_renamed`` says so.
        (``publish_decision_flow`` keeps the plain names.)

        Args:
            module_id: MAS module ID.
            step_id: Step ID within the module (usually 'score' or 'execute').
            input_data: Dictionary of input variable name-value pairs.
        """
        path = f"/microanalyticScore/modules/{module_id}/steps/{step_id}"
        async with viya_session("score_data", ctx) as client:
            try:
                return await post_json(path, client, body=_inputs(input_data))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 400:
                    raise
                step = await get_json(path, client)
                renames = match_input_names(input_data, [i.get("name", "") for i in step.get("inputs") or []])
                if not renames:
                    raise
            given: dict[str, Any] = input_data
            renamed = {renames.get(name, name): value for name, value in given.items()}
            result = await post_json(path, client, body=_inputs(renamed))
            result["inputs_renamed"] = renames
            return result

    @mcp.tool()
    async def get_model(model_id: str, ctx: Context) -> dict[str, Any]:
        """Describe a registered model: project, version, target, variables and files.

        Use it before scoring, publishing or monitoring a model, to see its
        input variables (what a scoring table must hold), its target and event
        probability variable, and whether it is the project's champion
        (``role``).

        Args:
            model_id: The model's ID (see ``list_registered_models``).
        """
        async with viya_session("get_model", ctx) as client:
            try:
                model = await mh.fetch_model(client, model_id)
            except mh.ModelError as exc:
                return {"status": "not_found", "message": str(exc)}
            summary = mh.model_summary(model)
            files, _ = await get_paged_items(f"/modelRepository/models/{model_id}/contents", client, limit=200)
            summary["files"] = [{"name": f.get("name"), "role": f.get("role")} for f in files]
            return summary

    @mcp.tool()
    async def list_model_versions(model_id: str, ctx: Context) -> dict[str, Any]:
        """List a registered model's versions, the current one first.

        Args:
            model_id: The model's ID (see ``list_registered_models``).
        """
        async with viya_session("list_model_versions", ctx) as client:
            try:
                model = await mh.fetch_model(client, model_id)
            except mh.ModelError as exc:
                return {"status": "not_found", "message": str(exc)}
            history, _ = await get_paged_items(f"/modelRepository/models/{model_id}/modelVersions", client, limit=200)
            fields = ("id", "modelVersionName", "createdBy", "creationTimeStamp", "comment")
            current = {key: model.get(key) for key in fields}
            versions = [{**current, "current": True}]
            versions += [{key: v.get(key) for key in fields} for v in history if v.get("id") != model_id]
            return {"model": model.get("name"), "id": model_id, "versions": versions}

    @mcp.tool()
    async def test_score_model(
        model_id: str,
        caslib_name: str,
        table_name: str,
        ctx: Context,
        rows: int = 10,
        timeout_seconds: int = 180,
    ) -> dict[str, Any]:
        """Score a few rows with a registered model and return the scored rows.

        Model Manager's own score test: it takes the first ``rows`` rows of
        the table, scores them with the model in the "SAS Model Manager
        compute context", waits, and returns them with the model's output
        columns (for example ``P_BAD1`` and ``I_BAD``). Use it to check that a
        model runs before publishing or monitoring it. Nothing is left behind:
        the scratch tables and the score definition are removed. It takes
        about 15 seconds plus a compute session start.

        The table needs the model's input variables (``get_model`` lists
        them). For a whole table use ``score_table``.

        Args:
            model_id: The model's ID.
            caslib_name: Caslib of the input table.
            table_name: A loaded CAS table holding the model's inputs.
            rows: How many rows to score (default 10, at most 100).
            timeout_seconds: How long to wait for the score test (default 180).
        """
        async with viya_session("test_score_model", ctx) as client:
            token = await get_token(ctx)
            try:
                model = await mh.fetch_model(client, model_id)
            except mh.ModelError as exc:
                return {"status": "not_found", "message": str(exc)}
            extra = {"modelOrigUri": f"/modelRepository/models/{model_id}"}
            if model.get("projectId"):
                extra["projectUri"] = f"/modelRepository/projects/{model['projectId']}"
                if model.get("projectVersionId"):
                    extra["projectVersionUri"] = (
                        f"/modelRepository/projects/{model['projectId']}/projectVersions/{model['projectVersionId']}"
                    )
            result = await mh.score_test(
                client,
                token,
                object_type="sas.models.model",
                object_uri=f"/modelManagement/models/{model_id}",
                object_name=model.get("name", model_id),
                input_variables=[v["name"] for v in model.get("inputVariables") or [] if v.get("name")],
                caslib_name=caslib_name,
                table_name=table_name,
                rows=rows,
                timeout_seconds=timeout_seconds,
                extra_properties=extra,
            )
            return {"model": model.get("name"), "model_id": model_id, **result}

    @mcp.tool()
    async def score_table(
        model_id: str,
        caslib_name: str,
        table_name: str,
        ctx: Context,
        output_caslib: str | None = None,
        output_table: str | None = None,
    ) -> dict[str, Any]:
        """Score a whole CAS table with a registered model; the result is a new CAS table.

        Runs the model straight from the Model Repository, so it need not be
        published first. Scoring runs in the background: this returns the
        score execution at once, and ``get_score_execution`` reports when it
        has finished and where the scored table is. The score definition
        stays in the model's project, as one made in Model Manager would.

        Args:
            model_id: The model's ID.
            caslib_name: Caslib of the input table.
            table_name: A CAS table holding the model's input variables.
            output_caslib: Caslib for the scored table (default: the input's).
            output_table: Name of the scored table (default:
                ``<table_name>_SCORED``).
        """
        async with viya_session("score_table", ctx) as client:
            try:
                model = await mh.fetch_model(client, model_id)
            except mh.ModelError as exc:
                return {"status": "not_found", "message": str(exc)}
            out_lib = output_caslib or caslib_name
            out_table = output_table or f"{table_name}_SCORED"
            extra = {"modelOrigUri": f"/modelRepository/models/{model_id}"}
            if model.get("projectId"):
                extra["projectUri"] = f"/modelRepository/projects/{model['projectId']}"
            definition = mh.score_definition(
                name=f"{model.get('name', model_id)} on {table_name}",
                object_type="sas.models.model",
                object_uri=f"/modelManagement/models/{model_id}",
                object_name=model.get("name", model_id),
                input_caslib=caslib_name,
                input_table=table_name,
                output_caslib=out_lib,
                output_table=out_table,
                input_variables=[v["name"] for v in model.get("inputVariables") or [] if v.get("name")],
                extra_properties=extra,
            )
            definition_id, execution = await mh.start_score(client, definition, f"/modelManagement/models/{model_id}")
            return {
                "status": execution.get("state") or "pending",
                "execution_id": execution.get("id"),
                "score_definition_id": definition_id,
                "model": model.get("name"),
                "input": f"{caslib_name}.{table_name}",
                "output": f"{out_lib}.{out_table}",
                "next": "Call get_score_execution with execution_id to see when it has finished.",
            }

    @mcp.tool()
    async def get_score_execution(execution_id: str, ctx: Context, rows: int = 0) -> dict[str, Any]:
        """Report a score execution: its state, its output table, and why it failed if it did.

        Args:
            execution_id: From ``score_table`` (or ``test_score_model`` when it
                ran out of time).
            rows: When it has completed, also return this many scored rows
                (default 0, at most 100).
        """
        async with viya_session("get_score_execution", ctx) as client:
            try:
                report = await mh.execution_report(client, execution_id)
            except mh.ModelError as exc:
                return {"status": "not_found", "message": str(exc)}
            output = report.get("outputTable") or {}
            if rows > 0 and report.get("state") == "completed" and output.get("table"):
                limit = min(int(rows), mh.MAX_TEST_ROWS)
                report.update(await mh.table_rows(client, output["caslib"], output["table"], limit))
            return report

    @mcp.tool()
    async def list_published_models(
        ctx: Context,
        destination_name: str | None = None,
        name_filter: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List models published to destinations (MAS, CAS, containers, Git, ...).

        Each entry names its destination, its published name, its state and
        the registered model it came from (``modelId``). Use it to find the
        published copy that ``validate_published_model`` checks, or to see
        where a model is already deployed.

        Args:
            destination_name: Only this destination (see
                ``list_publishing_destinations``).
            name_filter: Only published names containing this text.
            limit: Maximum entries (default 50).
        """
        parts = []
        if destination_name:
            parts.append(f"eq(destinationName,'{filter_literal(destination_name)}')")
        if name_filter:
            parts.append(contains_filter(name_filter))
        filters = parts[0] if len(parts) == 1 else (f"and({','.join(parts)})" if parts else None)
        async with viya_session("list_published_models", ctx) as client:
            items, _ = await get_paged_items(
                "/modelPublish/models",
                client,
                limit=limit,
                filters=filters,
                extra_params={"sortBy": "modifiedTimeStamp:descending"},
            )
            return [published_summary(item) for item in items]
