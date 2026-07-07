# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 1 — Data Discovery tools (Information Catalog, compute & CAS metadata)."""

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..config import VIYA_ENDPOINT
from ..viya_client import get_json, get_paged_items, post_json, return_items
from ._common import make_session_helpers


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 1 (Data Discovery) tools on *mcp*."""

    viya_session, compute_tool_session = make_session_helpers(get_token)

    catalog = "/catalog"
    search_collection_type = "application/vnd.sas.metadata.search.collection+json"
    # The adhoc job's status lives only in the full representation; the summary
    # (application/json) omits it.
    adhoc_media = "application/vnd.sas.metadata.bot.adhoc+json"
    profile_levels = ("dataDictionary", "dataDictionaryAndProfile", "detailedMetrics")

    def resource_uri_of(item: dict[str, Any]) -> str:
        """Pull the source-asset URI (rel='resource') from a catalog item's links."""
        for link in item.get("links", []) or []:
            if link.get("rel") == "resource":
                return link.get("href", "") or link.get("uri", "")
        return ""

    async def instance_for_resource_uri(client: httpx.AsyncClient, resource_uri: str) -> dict[str, Any] | None:
        """Resolve the catalog instance for a source-asset URI, or None if absent.

        Filters ``/catalog/instances`` by ``resourceId`` — the reliable way to
        locate the instance that profiling writes its results onto, rather than
        assuming the instance id equals a search hit's id.
        """
        data = await get_json(
            f"{catalog}/instances",
            client,
            params={"filter": f'eq(resourceId,"{resource_uri}")'},
        )
        items = data.get("items", []) or []
        return items[0] if items else None

    @mcp.tool()
    async def catalog_search(
        query: str,
        ctx: Context,
        indices: str = "catalog",
        limit: int = 20,
        start: int = 0,
    ) -> dict[str, Any]:
        """Search the SAS Information Catalog for assets (tables, columns, reports, ...).

        The catalog is a metadata index across the whole Viya environment, so this
        finds assets without needing to know their server/library first. Each hit
        includes the asset's ``resource_uri`` — the URI you can hand to the matching
        tool (e.g. get_report, get_castable_data) to act on the live asset — and an
        ``attributes`` map with whatever metadata the catalog holds for it (commonly
        ``library``, ``rowCount``, ``columnCount``, ``completenessPercent``,
        ``reviewStatus``, ``informationPrivacy``, and ``analysisTimeStamp``).

        The ``query`` uses the SAS catalog search grammar:
          * Free text matches names, with wildcards ``*`` (0+ chars) and ``?`` (1 char): ``cust*``.
          * Facets constrain fields, e.g. ``AssetType:Report``, ``Name:sales``,
            ``Library.name:PUBLIC``, ``Column.informationPrivacy:Sensitive``.
          * Ranges ``DateModified:[2024-01-01 TO 2024-12-31]`` and ``+`` to require a term.
            Combine freely: ``AssetType:"CAS Table" +Name:cust*``.
        Use ``catalog_search_helper`` to discover valid facet names and values.

        Args:
            query: The catalog search query (see grammar above). Use ``*`` to match all names.
            indices: Comma-separated index name(s) to search (default 'catalog').
            limit: Maximum hits to return (default 20).
            start: Offset of the first hit (default 0).
        """
        async with viya_session("catalog_search", ctx) as client:
            data = await get_json(
                f"{catalog}/search",
                client,
                params={"q": query, "indices": indices, "start": start, "limit": limit},
                accept=search_collection_type,
            )
            raw_items = data.get("items", [])
            items = return_items(
                raw_items,
                [
                    "id",
                    "type",
                    "typeLabel",
                    "label",
                    "name",
                    "description",
                    "score",
                    "attributes",
                ],
            )
            # resource_uri is derived from the item's links, not a flat field.
            for out, src in zip(items, raw_items, strict=True):
                out["resource_uri"] = resource_uri_of(src)
            return {
                "count": data.get("count", len(items)),
                "start": data.get("start", start),
                "limit": data.get("limit", limit),
                "items": items,
            }

    @mcp.tool()
    async def catalog_search_helper(
        ctx: Context, facet: str | None = None, query: str = "", limit: int = 50
    ) -> dict[str, Any]:
        """Discover how to search the catalog: list facets, or values for one facet.

        Call with no ``facet`` to list the available facets — the fields you can
        constrain in a ``catalog_search`` query. Call with a ``facet`` name to get the
        suggested/valid values for that facet (e.g. the asset types or review
        statuses that actually exist). Use the results to build precise
        ``catalog_search`` queries.

        Args:
            facet: Facet name to get suggested values for (e.g. 'AssetType'). If
                omitted, returns the list of available facets instead.
            query: Optional filter — when listing facets, matches facet names; when
                listing values, matches value prefixes.
            limit: Maximum entries to return (default 50).
        """
        async with viya_session("catalog_search_helper", ctx) as client:
            if facet:
                data = await get_json(
                    f"{catalog}/search/suggestions",
                    client,
                    params={"facet": facet, "q": query, "limit": limit},
                )
                return {"facet": facet, "values": data.get("items", [])}
            data = await get_json(
                f"{catalog}/search/facets",
                client,
                params={"q": query, "start": 0, "limit": limit},
            )
            facets = return_items(data.get("items", []), ["name", "type", "indices"])
            return {"facets": facets}

    @mcp.tool()
    async def catalog_find_instance(resource_uri: str, ctx: Context) -> dict[str, Any]:
        """Resolve the catalog instance for a source-asset URI.

        ``catalog_search`` finds assets by free text and facets, but the
        profiling and download tools key off a catalog *instance id*. When you
        already hold a resource URI — the ``resource_uri`` from a search hit, or
        a CAS table path — this looks the instance up directly by ``resourceId``
        (the same filter the profiling workflow uses) and returns its id plus
        the key profile attributes. Use it to tell at a glance whether the asset
        has been profiled (``analysisTimeStamp``) and what semantic metadata it
        carries (``informationPrivacy``, ``nlpTerms``, ``nlpTags``,
        ``mostImportantFields``) before calling ``catalog_download_table_profile``.

        Args:
            resource_uri: Source URI of the asset (e.g.
                '/dataTables/dataSources/cas~fs~.../tables/MYTABLE').
        """
        async with viya_session("catalog_find_instance", ctx) as client:
            inst = await instance_for_resource_uri(client, resource_uri)
            if inst is None:
                return {
                    "status": "not_found",
                    "resource_uri": resource_uri,
                    "message": (
                        "No catalog instance indexes that URI yet. Confirm the URI "
                        "with catalog_search, or run a discovery agent "
                        "(catalog_run_agent) to populate it."
                    ),
                }
            attrs = inst.get("attributes", {}) or {}
            return {
                "status": "ok",
                "instance_id": inst.get("id"),
                "name": inst.get("name", ""),
                "type": inst.get("type", ""),
                "resource_uri": inst.get("resourceId", resource_uri),
                "profiled": bool(attrs.get("analysisTimeStamp")),
                "attributes": attrs,
            }

    @mcp.tool()
    async def catalog_list_agents(
        ctx: Context, limit: int = 50, start: int = 0, filter_name: str | None = None
    ) -> list[dict[str, Any]]:
        """List SAS Information Catalog discovery agents.

        Agents crawl a data source (server/library) to discover assets and collect
        their metadata into the catalog. Use ``catalog_run_agent`` to start one and
        ``catalog_get_agent_history`` to see what a run produced.

        Args:
            limit: Maximum agents to return (default 50).
            start: Offset of the first agent (default 0).
            filter_name: Optional name filter (substring match).
        """
        params: dict[str, Any] = {"start": start, "limit": limit}
        if filter_name:
            params["filter"] = f"contains(name,'{filter_name}')"
        async with viya_session("catalog_list_agents", ctx) as client:
            data = await get_json(f"{catalog}/bots", client, params=params)
            return return_items(
                data.get("items", []),
                ["id", "name", "description", "agentType", "provider"],
            )

    @mcp.tool()
    async def catalog_run_agent(agent_id: str, ctx: Context) -> dict[str, str]:
        """Start a catalog discovery agent run (asynchronous).

        Triggers the agent to crawl its data source and populate/refresh catalog
        metadata. The run is asynchronous — results are applied to the catalog in
        the background; poll ``catalog_get_agent_history`` to track completion.
        Note: the Catalog API can only *start* an agent, not stop one already running.

        Args:
            agent_id: ID of the agent to run (see catalog_list_agents).
        """
        async with viya_session("catalog_run_agent", ctx) as client:
            resp = await client.put(
                f"{VIYA_ENDPOINT}{catalog}/bots/{agent_id}/state",
                params={"value": "running"},
                headers={"Accept": "text/plain"},
            )
            resp.raise_for_status()
            return {
                "status": resp.text.strip() or "running",
                "agent_id": agent_id,
                "message": (
                    "Agent started; metadata is applied to the catalog asynchronously. "
                    "Poll catalog_get_agent_history for completion."
                ),
            }

    @mcp.tool()
    async def catalog_get_agent_history(
        agent_id: str, ctx: Context, limit: int = 20, start: int = 0
    ) -> list[dict[str, Any]]:
        """Get the execution history of a catalog agent's runs.

        Each record reports a run's status and how much metadata it populated
        (tables enumerated/added/updated/removed), so you can confirm a run started
        by ``catalog_run_agent`` finished and what it changed.

        Args:
            agent_id: ID of the agent (see catalog_list_agents).
            limit: Maximum run records to return (default 20).
            start: Offset of the first record (default 0).
        """
        async with viya_session("catalog_get_agent_history", ctx) as client:
            data = await get_json(
                f"{catalog}/bots/{agent_id}/history",
                client,
                params={"start": start, "limit": limit},
            )
            return return_items(
                data.get("items", []),
                [
                    "id",
                    "status",
                    "creationTimeStamp",
                    "endTimeStamp",
                    "nEnumerated",
                    "nAdded",
                    "nUpdated",
                    "nRemoved",
                ],
            )

    @mcp.tool()
    async def catalog_run_adhoc_analysis(
        resource_uri: str,
        name: str,
        ctx: Context,
        resource_type: str = "",
        description: str = "",
        provider: str = "TABLE-BOT",
        identify_language: bool = True,
        analyze_sentiment: bool = True,
        get_nlp_semantic_id: bool = True,
    ) -> dict[str, Any]:
        """Submit an ad-hoc analysis (profiling) job for a table in the catalog.

        Profiles the table — computing the data dictionary, column statistics, and
        data-quality metrics that ``catalog_download_table_profile`` returns. The job
        runs asynchronously and may take a while; poll ``catalog_get_adhoc_analysis``
        with the returned job id until the profile is ready.

        The three NLP job parameters are enabled by default — they drive the
        semantic enrichment that populates an asset's ``informationPrivacy``,
        ``nlpTerms``, ``nlpTags``, and ``mostImportantFields`` (the privacy and
        keyword signals the catalog is most useful for). Leave them on unless you
        only need a plain column profile and want the job to finish faster.

        Args:
            resource_uri: Source URI of the table to analyze (the ``resource_uri`` from
                a catalog_search hit, e.g. '/dataTables/dataSources/cas~fs~.../tables/MYTABLE').
            name: A name for the analysis job.
            resource_type: Catalog entity type of the resource. Defaults to
                'CASMEMTable' when the URI is a CAS table (contains 'cas~fs~');
                pass it explicitly for other asset types.
            description: Optional description for the job.
            provider: Job provider (default 'TABLE-BOT').
            identify_language: Detect each text column's language (default True).
            analyze_sentiment: Score sentiment on text columns (default True).
            get_nlp_semantic_id: Derive semantic types / privacy classification
                (informationPrivacy, nlpTerms, nlpTags) (default True).
        """
        rtype = resource_type or ("CASMEMTable" if "cas~fs~" in resource_uri else "")
        if not rtype:
            return {
                "status": "missing_resource_type",
                "resource_uri": resource_uri,
                "message": (
                    "Could not infer resource_type from the URI. Pass resource_type "
                    "explicitly (e.g. 'CASMEMTable' for a CAS table)."
                ),
            }
        job_parameters: dict[str, str] = {}
        if identify_language:
            job_parameters["identifyLanguage"] = "1"
        if analyze_sentiment:
            job_parameters["analyzeSentiment"] = "1"
        if get_nlp_semantic_id:
            job_parameters["getNLPSemanticID"] = "1"
        body = {
            "provider": provider,
            "name": name,
            "description": description,
            "resources": [{"uri": resource_uri, "type": rtype}],
            "jobParameters": job_parameters,
        }
        async with viya_session("catalog_run_adhoc_analysis", ctx) as client:
            job = await post_json(
                f"{catalog}/bots/adhocAnalysisJobs",
                client,
                body=body,
                accept=adhoc_media,
            )
            return {
                "id": job.get("id"),
                "status": job.get("status", ""),
                "name": job.get("name", name),
                "message": (
                    "Analysis submitted. Poll catalog_get_adhoc_analysis until status "
                    "is 'completed', then catalog_download_table_profile."
                ),
            }

    @mcp.tool()
    async def catalog_get_adhoc_analysis(job_id: str, ctx: Context) -> dict[str, Any]:
        """Get the status of an ad-hoc analysis job, and whether its profile is ready.

        The job reaching a terminal ``status`` is *not* sufficient: the profile
        attributes are written onto the asset a little later, so a download fired
        the instant the job completes can come back empty. To close that gap, when
        the job carries a resource this also resolves the target catalog instance
        and reports ``profile_ready`` (the asset's ``analysisTimeStamp`` is
        populated — the same gate ``catalog_download_table_profile`` uses) and
        ``information_privacy`` (non-empty once the NLP semantic enrichment has
        landed). Poll until ``profile_ready`` is true, then download.

        Args:
            job_id: The analysis job id returned by catalog_run_adhoc_analysis.
        """
        async with viya_session("catalog_get_adhoc_analysis", ctx) as client:
            try:
                job = await get_json(
                    f"{catalog}/bots/adhocAnalysisJobs/{job_id}",
                    client,
                    accept=adhoc_media,
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    # Adhoc jobs can be purged once terminal — report it as such
                    # rather than raising, so polling loops can stop cleanly.
                    return {
                        "id": job_id,
                        "status": "not_found",
                        "message": ("No such analysis job — it may have finished and been purged, or the id is wrong."),
                    }
                raise
            resources = job.get("resources", []) or []
            resource_uri = ""
            if resources:
                resource_uri = resources[0].get("uri", "") or resources[0].get("resourceId", "")
            # Cross-check the asset itself: the job can be terminal while the
            # profile is still being written onto the instance.
            instance_id = ""
            profile_ready = False
            information_privacy = ""
            if resource_uri:
                inst = await instance_for_resource_uri(client, resource_uri)
                if inst:
                    instance_id = inst.get("id", "")
                    inst_attrs = inst.get("attributes", {}) or {}
                    profile_ready = bool(inst_attrs.get("analysisTimeStamp"))
                    information_privacy = inst_attrs.get("informationPrivacy", "") or ""
            return {
                "id": job.get("id", job_id),
                "status": job.get("status", ""),
                "name": job.get("name", ""),
                "creationTimeStamp": job.get("creationTimeStamp", ""),
                "endTimeStamp": job.get("endTimeStamp", ""),
                "resources": resources,
                "instance_id": instance_id,
                "profile_ready": profile_ready,
                "information_privacy": information_privacy,
                "message": (
                    f"Profile ready — download with catalog_download_table_profile (instance_id='{instance_id}')."
                    if profile_ready
                    else "Profile not written to the asset yet — poll again before downloading."
                ),
            }

    @mcp.tool()
    async def catalog_download_table_profile(
        ctx: Context,
        instance_id: str = "",
        resource_uri: str = "",
        level: str = "dataDictionaryAndProfile",
    ) -> dict[str, Any]:
        """Download a catalog table's data dictionary and profile as CSV.

        Returns the table's column metadata plus, by default, its profile (column
        statistics and data-quality metrics). If the table has not been profiled yet,
        this returns a recommendation to run ``catalog_run_adhoc_analysis`` (pre-filled
        with the table's URI and type) instead of an empty profile.

        Identify the table by either ``instance_id`` or ``resource_uri`` (give one).
        Passing ``resource_uri`` lets you run search → profile → download without ever
        handling an instance id: the asset is resolved by ``resourceId`` the same way
        ``catalog_find_instance`` does. ``instance_id`` takes precedence if both are given.

        Args:
            instance_id: Catalog instance id of the table (the ``id`` from a catalog_search hit).
            resource_uri: Source URI of the table (the ``resource_uri`` from a search hit,
                e.g. '/dataTables/dataSources/cas~fs~.../tables/MYTABLE'). Used when
                ``instance_id`` is omitted.
            level: Detail level — 'dataDictionaryAndProfile' (default; columns + profile),
                'detailedMetrics' (full per-column metrics), or 'dataDictionary'
                (column metadata only).
        """
        if level not in profile_levels:
            return {
                "status": "invalid_level",
                "message": f"level must be one of {', '.join(profile_levels)}.",
            }
        if not instance_id and not resource_uri:
            return {
                "status": "missing_identifier",
                "message": "Pass either instance_id or resource_uri.",
            }
        async with viya_session("catalog_download_table_profile", ctx) as client:
            # Resolve the instance first to identify the asset and whether it is profiled.
            if instance_id:
                try:
                    inst = await get_json(
                        f"{catalog}/instances/{instance_id}",
                        client,
                        accept="application/vnd.sas.metadata.instance.entity+json",
                    )
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        return {
                            "status": "not_found",
                            "instance_id": instance_id,
                            "message": (f"No catalog instance '{instance_id}'. Use catalog_search to find one."),
                        }
                    raise
            else:
                inst = await instance_for_resource_uri(client, resource_uri)
                if inst is None:
                    return {
                        "status": "not_found",
                        "resource_uri": resource_uri,
                        "message": (
                            f"No catalog instance indexes '{resource_uri}'. "
                            "Use catalog_search or catalog_find_instance to confirm it."
                        ),
                    }
                instance_id = inst.get("id", "")
            attrs = inst.get("attributes", {}) or {}
            resource_uri = inst.get("resourceId", "") or resource_uri
            resource_type = inst.get("type", "")
            wants_profile = level in ("dataDictionaryAndProfile", "detailedMetrics")
            if wants_profile and not attrs.get("analysisTimeStamp"):
                return {
                    "status": "not_profiled",
                    "instance_id": instance_id,
                    "resource_uri": resource_uri,
                    "resource_type": resource_type,
                    "message": (
                        "This table has no profile yet. Run catalog_run_adhoc_analysis "
                        f"with resource_uri='{resource_uri}' and "
                        f"resource_type='{resource_type}', poll catalog_get_adhoc_analysis "
                        "until completed, then retry."
                    ),
                }
            resp = await client.get(
                f"{VIYA_ENDPOINT}{catalog}/instances",
                params={"level": level, "filter": f"eq(id,'{instance_id}')"},
                headers={"Accept": "text/csv"},
                follow_redirects=True,
            )
            resp.raise_for_status()
            return {
                "status": "ok",
                "instance_id": instance_id,
                "resource_uri": resource_uri,
                "level": level,
                "csv": resp.text,
            }

    @mcp.tool()
    async def list_compute_libraries(
        compute_context_name: str,
        ctx: Context,
        limit: int = 50,
        start: int = 0,
        filter_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """List the SAS libraries (librefs) assigned in a compute context.

        Runs in the reusable per-user compute session for the context, so it
        also sees libraries created by prior ``execute_sas_code`` calls.

        Args:
            compute_context_name: Name of the compute context (see list_compute_contexts).
            limit: Maximum number of libraries to return (default 50).
            start: Offset of the first library to return (default 0).
            filter_name: Optional name filter (substring match).
        """
        async with compute_tool_session("list_compute_libraries", ctx, compute_context_name) as (client, session_id):
            filters = f"contains(name,'{filter_name}')" if filter_name else None
            items, _ = await get_paged_items(
                f"/compute/sessions/{session_id}/data",
                client,
                limit=limit,
                start=start,
                filters=filters,
            )
            return return_items(items, ["name", "description"])

    @mcp.tool()
    async def list_compute_tables(
        compute_context_name: str,
        library_name: str,
        ctx: Context,
        limit: int = 50,
        start: int = 0,
        filter_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """List the tables in a SAS library within a compute context.

        These are SAS/Compute tables (e.g. WORK or an assigned libref), distinct
        from in-memory CAS tables (see list_castables). Runs in the reusable
        per-user compute session for the context.

        Args:
            compute_context_name: Name of the compute context (see list_compute_contexts).
            library_name: Name of the SAS library/libref (e.g. 'WORK', 'SASHELP').
            limit: Maximum number of tables to return (default 50).
            start: Offset of the first table to return (default 0).
            filter_name: Optional name filter (substring match).
        """
        async with compute_tool_session("list_compute_tables", ctx, compute_context_name) as (client, session_id):
            filters = f"contains(name,'{filter_name}')" if filter_name else None
            items, _ = await get_paged_items(
                f"/compute/sessions/{session_id}/data/{library_name}",
                client,
                limit=limit,
                start=start,
                filters=filters,
            )
            return return_items(items, ["name", "description"])

    @mcp.tool()
    async def list_compute_columns(
        compute_context_name: str,
        library_name: str,
        table_name: str,
        ctx: Context,
        limit: int = 50,
        start: int = 0,
        filter_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """List the columns of a table in a SAS library within a compute context.

        Runs in the reusable per-user compute session for the context.

        Args:
            compute_context_name: Name of the compute context (see list_compute_contexts).
            library_name: Name of the SAS library/libref (e.g. 'WORK', 'SASHELP').
            table_name: Name of the table within the library.
            limit: Maximum number of columns to return (default 50).
            start: Offset of the first column to return (default 0).
            filter_name: Optional name filter (substring match).
        """
        async with compute_tool_session("list_compute_columns", ctx, compute_context_name) as (client, session_id):
            filters = f"contains(name,'{filter_name}')" if filter_name else None
            items, _ = await get_paged_items(
                f"/compute/sessions/{session_id}/data/{library_name}/{table_name}/columns",
                client,
                limit=limit,
                start=start,
                filters=filters,
            )
            return return_items(items, ["id", "name", "label", "type", "length"])

    @mcp.tool()
    async def list_cas_servers(ctx: Context) -> list[dict[str, Any]]:
        """List available CAS servers on the Viya environment."""
        async with viya_session("list_cas_servers", ctx) as client:
            items, _ = await get_paged_items("/casManagement/servers", client)
            return return_items(items, ["name", "id", "description"])

    @mcp.tool()
    async def list_caslibs(server_id: str, ctx: Context, limit: int = 50) -> list[dict[str, Any]]:
        """List CAS libraries (caslibs) available on a CAS server.

        Args:
            server_id: CAS server name or ID (e.g. 'cas-shared-default').
            limit: Maximum number of caslibs to return (default 50).
        """
        async with viya_session("list_caslibs", ctx) as client:
            items, _ = await get_paged_items(f"/casManagement/servers/{server_id}/caslibs", client, limit=limit)
            return return_items(items, ["name", "type", "description"])

    @mcp.tool()
    async def list_castables(server_id: str, caslib_name: str, ctx: Context, limit: int = 50) -> list[dict[str, Any]]:
        """List tables in a CAS library.

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            limit: Maximum number of tables to return (default 50).
        """
        async with viya_session("list_castables", ctx) as client:
            items, _ = await get_paged_items(
                f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables",
                client,
                limit=limit,
            )
            return return_items(items, ["name", "rowCount", "columnCount"])

    @mcp.tool()
    async def list_source_tables(
        server_id: str, caslib_name: str, ctx: Context, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List source tables that are NOT yet loaded into memory in a CAS library.

        These are the candidates for ``promote_table_to_memory`` — tables that
        exist on the caslib's data source but are not in CAS memory yet.

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            limit: Maximum number of tables to return (default 50).
        """
        async with viya_session("list_source_tables", ctx) as client:
            items, _ = await get_paged_items(
                f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables",
                client,
                limit=limit,
                extra_params={"state": "unloaded"},
            )
            return return_items(items, ["name", "sourceTableName", "scope", "state"])

    @mcp.tool()
    async def get_castable_info(server_id: str, caslib_name: str, table_name: str, ctx: Context) -> dict[str, Any]:
        """Get metadata for a CAS table (row count, column count, size, etc.).

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            table_name: Name of the table.
        """
        async with viya_session("get_castable_info", ctx) as client:
            return await get_json(
                f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables/{table_name}",
                client,
            )

    @mcp.tool()
    async def get_castable_columns(
        server_id: str,
        caslib_name: str,
        table_name: str,
        ctx: Context,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Get column metadata for a CAS table (names, types, labels, formats).

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            table_name: Name of the table.
            limit: Maximum columns to return (default 200).
        """
        async with viya_session("get_castable_columns", ctx) as client:
            items, _ = await get_paged_items(
                f"/casManagement/servers/{server_id}/caslibs/{caslib_name}/tables/{table_name}/columns",
                client,
                limit=limit,
            )
            return return_items(items, ["name", "type", "rawLength", "label", "format"])

    @mcp.tool()
    async def get_castable_data(
        server_id: str,
        caslib_name: str,
        table_name: str,
        ctx: Context,
        limit: int = 100,
        start: int = 0,
    ) -> dict[str, Any]:
        """Fetch rows from a CAS table with column names.

        Args:
            server_id: CAS server name or ID.
            caslib_name: Name of the caslib.
            table_name: Name of the table.
            limit: Maximum rows to return (default 100).
            start: Row offset (default 0).
        """
        data_source_id = f"cas~fs~{server_id}~fs~{caslib_name}"
        table_id = f"cas~fs~{server_id}~fs~{caslib_name}~fs~{table_name}"
        async with viya_session("get_castable_data", ctx) as client:
            columns: list[dict[str, Any]] = []
            col_start = 0
            col_limit = 100
            while True:
                col_resp = await client.get(
                    f"{VIYA_ENDPOINT}/dataTables/dataSources/{data_source_id}/tables/{table_name}/columns",
                    params={"start": col_start, "limit": col_limit},
                    follow_redirects=True,
                )
                col_resp.raise_for_status()
                col_data = col_resp.json()
                for item in col_data.get("items", []):
                    columns.append(
                        {
                            "name": item.get("name"),
                            "type": item.get("type"),
                            "index": item.get("index"),
                        }
                    )
                total = col_data.get("count", 0)
                col_start += col_limit
                if col_start >= total:
                    break

            row_resp = await client.get(
                f"{VIYA_ENDPOINT}/rowSets/tables/{table_id}/rows",
                params={"start": start, "limit": limit},
                follow_redirects=True,
            )
            row_resp.raise_for_status()
            row_data = row_resp.json()

            col_names = [c["name"] for c in columns]
            rows = []
            for item in row_data.get("items", []):
                cells = item.get("cells", [])
                rows.append(dict(zip(col_names, cells, strict=False)))

            return {
                "columns": col_names,
                "rows": rows,
                "count": row_data.get("count", len(rows)),
                "start": start,
                "limit": limit,
            }
