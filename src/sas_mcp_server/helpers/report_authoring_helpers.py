# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Helpers for the Visual Analytics report-authoring tools.

Backs ``create_report``, ``apply_report_operations``, ``describe_report_objects``,
and ``get_report_outline`` (all Tier 3, in ``tools/reports.py``), keeping each
``@mcp.tool`` a thin wrapper — matching the ``helpers/`` pattern used by
``report_export_helpers.py``.

The SAS Visual Analytics REST API has a single authoring shape: you POST a new
report or PUT an existing one with an ordered ``operations`` array, and the
service applies every operation atomically (all succeed or nothing persists).
Rather than one tool per chart type, the authoring tools pass that native array
straight through — so a new VA object type needs no new tool — and this module
supplies the *validation* and *discovery* that make the generic surface safe:

* :data:`REPORT_OBJECT_TYPES` — one frozen registry of every API-addable object
  and its data roles, the single source of truth consumed by all three tools.
* :func:`validate_operations` — structured, pre-flight checks (known object
  type, addable/updatable gating, data-role names + arity) returning an
  actionable error dict *before* any HTTP call, so the LLM self-corrects.
* :func:`describe` — progressive-disclosure discovery over the registry.
* :func:`execute_operations` — the GET-etag → ``If-Match`` → PUT round-trip
  (with a transparent 412 retry) that the caller never has to manage, plus an
  optional *save-as* mode (``resultReportName``/``resultFolder``) that applies
  the batch to a new report and leaves the source untouched.
* :func:`execute_outline` — the read-back path: reduces the stored report
  definition (BIRD content) to the page → object names/labels that placement,
  updateObject, and export_report consume.

The ETag is read from the generic Reports service resource
(``/reports/reports/{id}``) while the operations PUT targets the Visual
Analytics service (``/visualAnalytics/reports/{id}``) — the two-path handshake
the SAS sample notebooks use.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from sas_mcp_server.config import VIYA_ENDPOINT

# --- endpoints ------------------------------------------------------------

CREATE_PATH = "/visualAnalytics/reports"
OPERATIONS_PATH = "/visualAnalytics/reports/{report_id}"
COPY_PATH = "/visualAnalytics/reports/{report_id}/copy"
DELETE_PATH = "/visualAnalytics/reports/{report_id}"
# The current ETag comes from the Reports service view of the report, not the
# Visual Analytics operations endpoint (mirrors the SAS sample notebooks).
ETAG_PATH = "/reports/reports/{report_id}"
# The stored report definition (BIRD document) — the only read-back path for a
# report's page/object structure. Requires the vnd Accept type (plain
# application/json is answered with 415).
CONTENT_PATH = "/reports/reports/{report_id}/content"
CONTENT_ACCEPT = "application/vnd.sas.report.content+json"

CONFLICT_VALUES = frozenset({"abort", "rename", "replace"})

# The eight report operations the API accepts in an operations array. Meta keys
# may sit alongside the single operation key on an element.
OPERATION_KEYS = frozenset(
    {
        "addData",
        "updateData",
        "changeData",
        "applyDataView",
        "addPage",
        "addObject",
        "updateObject",
        "setParameterValue",
    }
)
_META_OP_KEYS = frozenset({"operationId", "includeObjectInResponse"})

# How an addObject may be placed. Each variant carries a target and/or a
# context/position from a fixed enum. VA has no absolute width/height/x/y in the
# request — objects are auto-sized — so layout is built from placement (relative
# positioning + containers + page assignment), not geometry.
# NOTE: the live report-context enum is snake_case "new_page" (the published
# OpenAPI spec says "newPage", but current Viya rejects that spelling with a
# 400); normalize_operations translates "newPage" for callers that follow the
# spec. Page/report "header" bands accept ONLY control objects — never text.
PLACEMENT_VARIANTS: tuple[str, ...] = ("page", "relativeToObject", "container", "report")
_PLACEMENT_TARGET_REQUIRED = frozenset({"page", "relativeToObject", "container"})
_PLACEMENT_ENUMS: dict[str, dict[str, frozenset[str]]] = {
    "page": {"context": frozenset({"header", "body"}), "position": frozenset({"start", "end"})},
    "relativeToObject": {"position": frozenset({"before", "after", "left", "right", "top", "bottom"})},
    "container": {"position": frozenset({"start", "end"})},
    "report": {"context": frozenset({"new_page", "header"}), "position": frozenset({"start", "end"})},
}
# VA enforces additionalProperties:false, so unknown placement keys fail the
# whole atomic batch server-side — reject them pre-flight instead.
_PLACEMENT_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "page": frozenset({"target", "context", "position"}),
    "relativeToObject": frozenset({"target", "position"}),
    "container": frozenset({"target", "position"}),
    "report": frozenset({"context", "position", "pageName", "pagePosition"}),
}

# The placement vocabulary and layout recipes surfaced by describe() so an agent
# can build structured pages rather than a single auto-flow stack.
PLACEMENT_GUIDE: tuple[dict[str, Any], ...] = (
    {
        "variant": "page",
        "shape": {"page": {"target": "<pageName>", "context": "header | body", "position": "start | end"}},
        "purpose": (
            "Put the object on a page (defaults: body/end). context 'header' is the control band "
            "across the top — it accepts ONLY control objects (dropdownList, buttonBar, ...), never "
            "text or visuals; a title text belongs in the body with position 'start'."
        ),
    },
    {
        "variant": "relativeToObject",
        "shape": {"relativeToObject": {"target": "<objectName>", "position": "left|right|top|bottom|before|after"}},
        "purpose": (
            "Anchor next to an EXISTING object by name — build columns, rows, and grids. "
            "left/right/top/bottom are geometric (VA auto-wraps both objects in a layout container "
            "when the direction crosses the parent's flow); before/after insert in flow order. The "
            "target must already exist when the operation applies — same-batch forward references fail."
        ),
    },
    {
        "variant": "container",
        "shape": {"container": {"target": "<containerName>", "position": "start | end"}},
        "purpose": "Place inside a standardContainer added earlier, to group objects together.",
    },
    {
        "variant": "report",
        "shape": {
            "report": {
                "context": "new_page | header",
                "position": "start | end",
                "pageName": "<name for the new page>",
                "pagePosition": 0,
            }
        },
        "purpose": (
            "Place at the report level. context 'new_page' creates a page as it places the object; "
            "give it a pageName (becomes the page label, targetable by later ops in the SAME batch) "
            "and an optional numeric pagePosition (0 = first — a NUMBER here, unlike the string "
            "addPage.pagePosition). context 'header' is the report-wide control band (controls only)."
        ),
    },
)

LAYOUT_RECIPES: tuple[str, ...] = (
    "Default page skeleton — the page body auto-flows VERTICALLY, so N page-placed objects render "
    "as one tall ugly stack. Structure every page instead: KPI tiles side by side in a "
    "standardContainer at the top, then each chart row built with relativeToObject left/right. "
    "Page placement alone is only right for a page's FIRST object per row.",
    'Page title: give addPage a "title", e.g. {"addPage": {"pageName": "Overview", "title": "Sales '
    'Overview"}} — it expands into a text band at the TOP OF THE PAGE BODY. Page and report headers '
    "accept only control objects, so titles never go in a header.",
    'Chart titles: pass {"options": {"object": {"title": "Revenue by Region"}}} inside the object '
    "spec at add time (every addable type except standardContainer — title a container via a "
    "follow-up updateObject using its returned name). Untitled charts fall back to auto-labels "
    'like "Frequency of Origin".',
    "One-batch multi-page report: create each page with its first object via placement "
    '{"report": {"context": "new_page", "pageName": "Trends", "pagePosition": 1}}, then target that '
    'pageName from later operations in the SAME batch with {"page": {"target": "Trends"}}. Page '
    "names are caller-chosen; object names are not.",
    "Two columns: add chart A in one call, read its returned object name, then add chart B with "
    '{"relativeToObject": {"target": "<A\'s name>", "position": "right"}}. Targets must already '
    "exist — a same-batch forward reference fails the whole atomic batch. before/after insert in "
    "flow order; left/right/top/bottom are geometric side-by-side.",
    "2x2 grid: place obj2 right of obj1, obj3 bottom of obj1, obj4 right of obj3 — chaining the "
    "object names each apply_report_operations call returns.",
    "Grouped strip (e.g. a KPI row of keyValue tiles): add a standardContainer (bare {} — it "
    "accepts no options at add time), then in a follow-up apply add each tile with placement "
    '{"container": {"target": "<container\'s returned name>"}}.',
    "Placement and dataRoles are WRITE-ONCE: updateObject changes only options (title, etc.) and "
    "there is no move/resize/remove operation — get placement right at add time, or rebuild via "
    "save-as/copy.",
    'Creating a report with inline operations leaves VA\'s empty default "Page 1" as the first '
    "page, so whole-report exports can render blank — verify page-by-page with "
    "export_report(..., report_objects=['<page label>']) and inspect structure with "
    "get_report_outline.",
)


# --- object registry ------------------------------------------------------


@dataclass(frozen=True)
class RoleSpec:
    """A single data role a VA object accepts.

    ``multi`` marks an array-valued role (e.g. ``measures``, ``columns``,
    ``variables``) versus a single-column role (e.g. ``category``, ``xAxis``).
    """

    name: str
    multi: bool = False


@dataclass(frozen=True)
class VaObject:
    """One VA report object and the data roles it exposes.

    ``commonly_required`` is a curated heuristic from the SAS ``vaobj``
    documentation (the OpenAPI spec marks *every* role optional), used only for
    non-blocking warnings — never to reject a payload. ``purpose`` is a one-line
    picking hint surfaced by describe() so an agent can choose the right object
    for an analytical intent instead of defaulting to barChart for everything.
    """

    schema_key: str
    category: str
    addable: bool
    updatable: bool
    roles: tuple[RoleSpec, ...]
    commonly_required: tuple[str, ...] = ()
    purpose: str = ""
    # Role groups where at least ONE member must be filled or the object is
    # accepted by VA but RENDERS EMPTY ("required roles not assigned") — the
    # API does not auto-apply Frequency the way the VA UI does. Live-observed.
    render_required: tuple[tuple[str, ...], ...] = ()

    @property
    def role_names(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.roles)


# Registry generated from the SAS Visual Analytics v8 OpenAPI spec (every object
# in the ``addObjectRequest`` union) and role semantics from the ``vaobj`` docs.
# Adding or retiring an object when VA changes is a one-line edit here that
# describe, validation, and the example builder all pick up automatically.
_R = RoleSpec
_O = VaObject
_OBJECTS: tuple[VaObject, ...] = (
    _O(
        "crosstab",
        "Tables",
        True,
        True,
        (_R("rows", multi=True), _R("columns", multi=True), _R("measures", multi=True)),
        (),
        purpose="Pivot table — measures at row x column category intersections.",
    ),
    _O(
        "listTable",
        "Tables",
        True,
        True,
        (_R("columns", multi=True),),
        ("columns",),
        purpose="Detail rows, spreadsheet-style — one row per record.",
    ),
    _O(
        "buttonBar",
        "Controls",
        True,
        True,
        (_R("category"), _R("measure")),
        (),
        purpose="Row of buttons picking one category value (prompt control; not auto-wired to filter).",
    ),
    _O(
        "dropdownList",
        "Controls",
        True,
        True,
        (_R("category"), _R("measure")),
        (),
        purpose="Compact dropdown picking a category value (prompt control; not auto-wired to filter).",
    ),
    _O(
        "list",
        "Controls",
        True,
        True,
        (_R("category"), _R("measure")),
        (),
        purpose="Scrollable multi-select list of category values (prompt control).",
    ),
    _O(
        "slider",
        "Controls",
        True,
        True,
        (_R("measure"),),
        (),
        purpose="Numeric range slider (prompt control).",
    ),
    _O(
        "textInput",
        "Controls",
        True,
        True,
        (_R("category"), _R("measure")),
        (),
        purpose="Free-text search box (prompt control).",
    ),
    _O(
        "standardContainer",
        "Containers",
        True,
        True,
        (),
        (),
        purpose="Groups objects into one auto-arranged block — the KPI-row / panel building block.",
    ),
    _O(
        "dataDrivenContent",
        "Content",
        True,
        True,
        (_R("variables", multi=True),),
        (),
        purpose="Embeds a custom third-party visualization fed by report data (its URL is NOT settable here).",
    ),
    _O(
        "image",
        "Content",
        True,
        True,
        (),
        (),
        purpose="Static image from a URL or a Viya folder — logos and branding.",
    ),
    _O(
        "text",
        "Content",
        True,
        True,
        (),
        (),
        purpose="Static narrative text — title bands, section intros, footnotes.",
    ),
    _O(
        "geoBubble",
        "Geo Maps",
        True,
        True,
        (_R("geography"), _R("size"), _R("color")),
        ("geography",),
        purpose="Map with bubbles sized/colored by measures at locations.",
    ),
    _O(
        "geoCluster",
        "Geo Maps",
        True,
        False,
        (_R("geography"), _R("size"), _R("color")),
        ("geography",),
        purpose="Map clustering dense point locations.",
    ),
    _O(
        "geoContour",
        "Geo Maps",
        True,
        True,
        (_R("geography"), _R("color")),
        ("geography",),
        purpose="Map with density contours over locations.",
    ),
    _O(
        "geoCoordinate",
        "Geo Maps",
        True,
        True,
        (_R("geography"), _R("size"), _R("color")),
        ("geography",),
        purpose="Map plotting individual coordinate points.",
    ),
    _O(
        "geoLine",
        "Geo Maps",
        True,
        True,
        (_R("geography"), _R("width"), _R("color"), _R("pattern")),
        ("geography",),
        purpose="Map drawing lines/routes between geo points.",
    ),
    _O(
        "geoLineCoordinate",
        "Geo Maps",
        True,
        True,
        (
            _R("geographyLine"),
            _R("widthLine"),
            _R("colorLine"),
            _R("patternLine"),
            _R("geographyScatter"),
            _R("sizeScatter"),
            _R("colorScatter"),
        ),
        (),
        purpose="Map combining a line layer with a coordinate-point layer.",
    ),
    _O(
        "geoNetwork",
        "Geo Maps",
        True,
        True,
        (_R("source"), _R("target"), _R("size"), _R("color"), _R("dataLabel")),
        (),
        purpose="Map of source-to-target links (flows) between locations.",
    ),
    _O(
        "geoPie",
        "Geo Maps",
        True,
        True,
        (_R("geography"), _R("size"), _R("response"), _R("group")),
        ("geography",),
        purpose="Map with pie markers at locations.",
    ),
    _O(
        "geoRegion",
        "Geo Maps",
        True,
        True,
        (_R("geography"), _R("color")),
        ("geography",),
        purpose="Choropleth — regions filled by a measure.",
    ),
    _O(
        "geoRegionCoordinate",
        "Geo Maps",
        True,
        True,
        (_R("geographyRegion"), _R("colorRegion"), _R("geographyScatter"), _R("sizeScatter"), _R("colorScatter")),
        (),
        purpose="Choropleth plus a coordinate-point overlay.",
    ),
    _O(
        "automatedExplanation",
        "Analytics",
        True,
        True,
        (_R("response"), _R("underlyingFactors", multi=True)),
        (),
        purpose="Auto-generated narrative explaining what drives a measure.",
    ),
    _O(
        "forecasting",
        "Analytics",
        True,
        True,
        (_R("timeAxis"), _R("measures", multi=True), _R("underlyingFactors", multi=True)),
        ("timeAxis",),
        purpose="Time-series forecast with confidence bands.",
    ),
    _O(
        "networkAnalysis",
        "Analytics",
        True,
        True,
        (_R("source"), _R("target"), _R("size"), _R("color"), _R("linkWidth"), _R("linkColor")),
        (),
        purpose="Node-link network diagram of relationships.",
    ),
    _O(
        "pathAnalysis",
        "Analytics",
        True,
        True,
        (_R("event"), _R("sequenceOrder"), _R("transactionId"), _R("weight")),
        (),
        purpose="Sankey-style flow of event sequences (journeys, funnels).",
    ),
    _O(
        "cluster",
        "Statistics",
        True,
        True,
        (_R("variables", multi=True),),
        (),
        purpose="Cluster analysis grouping observations across variables.",
    ),
    _O(
        "linearRegression",
        "Statistics",
        False,
        True,
        (_R("response"), _R("continuousEffects", multi=True), _R("classificationEffects", multi=True)),
        (),
        purpose="Linear regression fit summary (update-only via the API).",
    ),
    _O(
        "logisticRegression",
        "Statistics",
        True,
        False,
        (_R("response"), _R("continuousEffects", multi=True)),
        (),
        purpose="Logistic regression fit summary.",
    ),
    _O(
        "nonparametricLogisticRegression",
        "Statistics",
        True,
        True,
        (_R("response"), _R("splineEffects", multi=True)),
        (),
        purpose="Spline-based (GAM-like) logistic regression.",
    ),
    _O(
        "bayesianNetwork",
        "Machine Learning",
        True,
        True,
        (_R("response"), _R("predictors", multi=True)),
        (),
        purpose="Bayesian network model of a response and predictors.",
    ),
    _O(
        "factorizationMachine",
        "Machine Learning",
        True,
        True,
        (_R("response"), _R("predictors", multi=True)),
        (),
        purpose="Factorization machine model (sparse interactions).",
    ),
    _O(
        "forest",
        "Machine Learning",
        True,
        True,
        (_R("response"), _R("predictors", multi=True)),
        (),
        purpose="Random-forest model assessment.",
    ),
    _O(
        "gradientBoosting",
        "Machine Learning",
        True,
        True,
        (_R("response"), _R("predictors", multi=True)),
        (),
        purpose="Gradient-boosting model assessment (nearest to a decision tree).",
    ),
    _O(
        "neuralNetwork",
        "Machine Learning",
        True,
        True,
        (_R("response"), _R("predictors", multi=True)),
        (),
        purpose="Neural network model assessment.",
    ),
    _O(
        "supportVectorMachine",
        "Machine Learning",
        True,
        True,
        (_R("response"), _R("predictors", multi=True)),
        (),
        purpose="Support vector machine model assessment.",
    ),
    _O(
        "barChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measures", multi=True), _R("frequency")),
        ("category",),
        purpose="Bars comparing measures across categories — the general workhorse.",
        render_required=(("measures", "frequency"),),
    ),
    _O(
        "boxPlot",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measures", multi=True)),
        ("measures",),
        purpose="Distribution quartiles and outliers per category.",
    ),
    _O(
        "bubbleChangePlot",
        "Graphs",
        True,
        True,
        (_R("xStart"), _R("xEnd"), _R("yStart"), _R("yEnd"), _R("sizeStart"), _R("sizeEnd"), _R("group")),
        (),
        purpose="Bubbles showing movement between a start and an end state.",
    ),
    _O(
        "bubblePlot",
        "Graphs",
        True,
        True,
        (_R("xAxis"), _R("yAxis"), _R("size"), _R("group")),
        ("xAxis", "yAxis", "size"),
        purpose="Scatter with a third measure encoded as bubble size.",
    ),
    _O(
        "butterflyChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measureBar"), _R("measureBar2")),
        (),
        purpose="Two measures diverging left/right from a shared category axis.",
    ),
    _O(
        "comparativeTimeSeriesPlot",
        "Graphs",
        True,
        True,
        (_R("timeAxis"), _R("measureTimeSeries1"), _R("measureTimeSeries2")),
        (),
        purpose="Two stacked time-series panels over the same time axis.",
    ),
    _O(
        "correlationMatrix",
        "Graphs",
        True,
        True,
        (_R("measures", multi=True),),
        ("measures",),
        purpose="Heat-colored pairwise correlations between measures.",
    ),
    _O(
        "dotPlot",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measure")),
        ("category",),
        purpose="Dots marking a measure per category — lighter than bars.",
    ),
    _O(
        "dualAxisBarChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measureBar"), _R("measureBar2")),
        (),
        purpose="Bars for two measures on independent Y axes.",
    ),
    _O(
        "dualAxisBarLineChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measureBar"), _R("measureLine")),
        (),
        purpose="Bars plus a line on independent Y axes — volume vs rate.",
    ),
    _O(
        "dualAxisLineChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measureLine"), _R("measureLine2")),
        (),
        purpose="Two lines on independent Y axes.",
    ),
    _O(
        "dualAxisTimeSeriesPlot",
        "Graphs",
        True,
        True,
        (_R("timeAxis"), _R("measureLine"), _R("measureLine2")),
        (),
        purpose="Two time series on independent Y axes.",
    ),
    _O(
        "gauge",
        "Graphs",
        True,
        True,
        (_R("measure"), _R("target"), _R("group")),
        ("measure",),
        purpose="KPI dial of a measure against a target.",
    ),
    _O(
        "heatMap",
        "Graphs",
        True,
        True,
        (_R("axisItems", multi=True), _R("color")),
        ("axisItems",),
        purpose="Grid cells colored by a measure.",
    ),
    _O(
        "histogram",
        "Graphs",
        True,
        True,
        (_R("measure"), _R("frequency")),
        ("measure",),
        purpose="Distribution of a single measure.",
    ),
    _O(
        "keyValue",
        "Graphs",
        True,
        True,
        (_R("measure"), _R("latticeCategory")),
        ("measure",),
        purpose="Big-number KPI tile — one headline value.",
    ),
    _O(
        "lineChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measures", multi=True), _R("frequency")),
        ("category",),
        purpose="Lines across ordered categories — trends over a non-date axis.",
        render_required=(("measures", "frequency"),),
    ),
    _O(
        "needlePlot",
        "Graphs",
        True,
        True,
        (_R("xAxis"), _R("yAxis"), _R("group")),
        (),
        purpose="Vertical needles from a baseline — sparse event values.",
    ),
    _O(
        "numericSeriesPlot",
        "Graphs",
        True,
        True,
        (_R("xAxis"), _R("yAxis"), _R("group")),
        (),
        purpose="Line over a numeric (non-date) X axis.",
    ),
    _O(
        "parallelCoordinatePlot",
        "Graphs",
        True,
        True,
        (_R("variables", multi=True),),
        (),
        purpose="Profile lines across many variables at once.",
    ),
    _O(
        "pieChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measures", multi=True), _R("frequency")),
        ("category",),
        purpose="Part-to-whole share per category (keep to a few slices).",
        render_required=(("measures", "frequency"),),
    ),
    _O(
        "scatterPlot",
        "Graphs",
        True,
        True,
        (_R("measures", multi=True), _R("color")),
        ("measures",),
        purpose="Point cloud relating two or more measures.",
    ),
    _O(
        "scheduleChart",
        "Graphs",
        True,
        True,
        (_R("task"), _R("start"), _R("finish"), _R("group")),
        (),
        purpose="Gantt-style bars of tasks over start/finish times.",
    ),
    _O(
        "stepPlot",
        "Graphs",
        True,
        True,
        (_R("xAxis"), _R("yAxis"), _R("group")),
        (),
        purpose="Stepped line — values holding constant between changes.",
    ),
    _O(
        "targetedBarChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measure"), _R("target")),
        (),
        purpose="Bars with target markers — actual vs goal.",
    ),
    _O(
        "timeSeriesPlot",
        "Graphs",
        True,
        True,
        (_R("timeAxis"), _R("measure"), _R("group")),
        ("timeAxis",),
        purpose="Trend of a measure over a date/time axis.",
    ),
    _O(
        "treeMap",
        "Graphs",
        True,
        True,
        (_R("category"), _R("measure")),
        ("category",),
        purpose="Nested rectangles sized by a measure — hierarchical part-to-whole.",
    ),
    _O(
        "vectorPlot",
        "Graphs",
        True,
        True,
        (_R("xAxis"), _R("yAxis"), _R("xOrigin"), _R("yOrigin"), _R("color")),
        (),
        purpose="Arrows from origin to point — direction and magnitude of change.",
    ),
    _O(
        "waterfallChart",
        "Graphs",
        True,
        True,
        (_R("category"), _R("response")),
        (),
        purpose="Cumulative running total across categories.",
    ),
    _O(
        "wordCloud",
        "Graphs",
        True,
        True,
        (_R("word"), _R("size"), _R("color")),
        (),
        purpose="Words sized by frequency or a measure.",
        render_required=(("size",),),
    ),
)
del _R, _O

REPORT_OBJECT_TYPES: dict[str, VaObject] = {o.schema_key: o for o in _OBJECTS}

# Objects that appear in the VA UI but have NO schema in the report API. Mapping
# each to the nearest addable alternative lets the tools redirect an agent
# instead of letting it fail with an opaque VA error.
NOT_ADDABLE: dict[str, str] = {
    "textTopics": "wordCloud",
    "decisionTree": "gradientBoosting",
    "generalizedAdditiveModel": "nonparametricLogisticRegression",
    "generalizedLinearModel": "logisticRegression",
}

CATEGORIES: tuple[str, ...] = (
    "Tables",
    "Controls",
    "Containers",
    "Content",
    "Graphs",
    "Geo Maps",
    "Analytics",
    "Statistics",
    "Machine Learning",
)

# Colloquial names an agent is likely to try, mapped to real schema keys —
# consulted before difflib so describe('kpi') lands on keyValue instead of
# nothing (difflib alone scores kpi->keyValue below its cutoff).
ALIASES: dict[str, str] = {
    "kpi": "keyValue",
    "tile": "keyValue",
    "bignumber": "keyValue",
    "indicator": "keyValue",
    "map": "geoRegion",
    "choropleth": "geoRegion",
    "table": "listTable",
    "datatable": "listTable",
    "pivot": "crosstab",
    "pivottable": "crosstab",
    "filter": "dropdownList",
    "dropdown": "dropdownList",
    "donut": "pieChart",
    "gantt": "scheduleChart",
    "sankey": "pathAnalysis",
    "trend": "timeSeriesPlot",
    "sparkline": "timeSeriesPlot",
    "container": "standardContainer",
    "textbox": "text",
    "label": "text",
    "logo": "image",
    "funnel": "pathAnalysis",
}

def _normalize_key(value: str) -> str:
    """Case/spacing-insensitive lookup form: 'Decision Tree' -> 'decisiontree'."""
    return value.strip().lower().replace(" ", "").replace("-", "").replace("_", "")


_NORMALIZED_TYPES: dict[str, str] = {_normalize_key(k): k for k in REPORT_OBJECT_TYPES}
_NORMALIZED_NOT_ADDABLE: dict[str, str] = {_normalize_key(k): k for k in NOT_ADDABLE}


# Analytical intent -> the object types that serve it, surfaced in the describe
# index so an agent picks variety deliberately instead of barChart-for-everything.
INTENT_MAP: dict[str, list[str]] = {
    "single KPI number": ["keyValue", "gauge"],
    "KPI row of tiles": ["standardContainer", "keyValue"],
    "trend over time": ["timeSeriesPlot", "lineChart"],
    "compare categories": ["barChart", "dotPlot"],
    "actual vs target": ["targetedBarChart", "gauge"],
    "part-to-whole": ["pieChart", "treeMap"],
    "distribution": ["histogram", "boxPlot"],
    "relationship between measures": ["scatterPlot", "bubblePlot", "heatMap"],
    "geographic distribution": ["geoRegion", "geoBubble"],
    "detail rows": ["listTable"],
    "pivot / cross-tab": ["crosstab"],
    "filter control": ["dropdownList", "buttonBar", "slider"],
    "narrative text / title band": ["text"],
    "logo / branding": ["image"],
    "forecast": ["forecasting"],
    "what drives a measure": ["automatedExplanation"],
    "flows / sequences": ["pathAnalysis", "networkAnalysis"],
}

# Honest boundaries of the operations API, surfaced by describe() so an agent
# does not burn round-trips hunting for capabilities that do not exist.
API_LIMITS: tuple[str, ...] = (
    "Controls are added UNWIRED: no operation creates filters, actions, or links between objects "
    "— interactive wiring needs the VA UI (or raw report-content editing).",
    "setParameterValue only sets EXISTING report parameters; parameters cannot be created here.",
    "Calculated items, hierarchies, and custom sorts cannot be created via operations — save a "
    "data view once in the VA UI, then import it with applyDataView.",
    "No theme/page-numbering/footer operation exists; retheming lives in the SAS Report "
    "Transforms API.",
    "Placement and dataRoles are write-once: updateObject changes only options, and there is no "
    "move/resize/remove operation (delete_report or save-as/copy and rebuild instead).",
    "Objects are auto-named and auto-sized: no width/height/x/y anywhere, and VA rejects a "
    "caller-supplied object 'name' at add time — chain layouts on the names each apply returns.",
    "Page and report headers accept ONLY control objects — titles are text objects placed at the "
    "top of the page body.",
)

# dataItems vocabularies from the VA v8 OpenAPI spec (dataItemProperties),
# validated pre-flight because VA rejects unknown values with a whole-batch 400.
AGGREGATIONS: frozenset[str] = frozenset(
    {
        "sum",
        "average",
        "min",
        "max",
        "count",
        "median",
        "variance",
        "numberMissing",
        "standardDeviation",
        "standardError",
        "firstQuartile",
        "thirdQuartile",
        "skewness",
        "kurtosis",
        "coefficientOfVariation",
        "correctedSumOfSquares",
        "uncorrectedSumOfSquares",
        "tStatistic",
        "pValue",
    }
)
CLASSIFICATIONS: frozenset[str] = frozenset({"category", "measure", "geography"})
# The geographyDataSource union from the spec: named-region contexts OR raw
# lat/long coordinates (geographyCoordinates) — exactly one of the two.
GEO_SOURCE_KEYS: frozenset[str] = frozenset(
    {
        "geographyNameCodeContext",
        "geographyCountryRegion",
        "geographyDataProvider",
        "geographyCoordinates",
    }
)
# Named SAS format: optional $, leading letter, optional width, dot, optional
# decimals (DOLLAR12.2, COMMA10., PERCENT8.1, DATE9., $CHAR20.). VA rejects
# bare numeric w.d forms like "8.1" — and the whole atomic batch with them.
_SAS_FORMAT_RE = re.compile(r"^\$?[A-Za-z][A-Za-z0-9_]*\.\d*$")
GEO_NAME_CODE_CONTEXTS: frozenset[str] = frozenset(
    {
        "CountryRegionNames",
        "CountryRegionISO2LetterCodes",
        "CountryRegionISO3LetterCodes",
        "CountryRegionISONumericCodes",
        "CountryRegionSASMapIdValues",
        "SubdivisionNames",
        "SubdivisionSASMapIdValues",
        "USStateNames",
        "USStateAbbreviations",
        "USZipCodes",
    }
)

# The eight operations, with a worked example each, for describe(operation=...)
# and the tool docstrings. The index lists key+purpose; the full entry (example,
# notes) is returned by describe_report_objects(operation='addData') etc.
OPERATIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "addData",
        "purpose": (
            "Bind a CAS table as a data source; dataItems is where reports get polish — "
            "readable labels, formats, aggregations, geography."
        ),
        "required": ["cas.{server, library, table}"],
        "example": {
            "addData": {
                "cas": {"server": "cas-shared-default", "library": "Public", "table": "SALES"},
                "dataItems": [
                    {
                        "dataItem": "Revenue",
                        "properties": {"name": "Revenue (USD)", "format": "DOLLAR12.2", "aggregation": "average"},
                    },
                    {
                        "dataItem": "State",
                        "properties": {
                            "classification": "geography",
                            "geographyDataSource": {"geographyNameCodeContext": "USStateNames"},
                        },
                    },
                ],
            }
        },
        "notes": [
            "addObject.dataSource references the addData 'name', defaulting to cas.table.",
            "After a rename, dataRoles must use the NEW name (e.g. 'Revenue (USD)').",
            f"aggregation: one of {sorted(AGGREGATIONS)}.",
            "format must be a NAMED SAS format (DOLLAR12.2, PERCENT8.1, COMMA10., DATE9.) — "
            "bare numeric forms like '8.1' are rejected.",
            "classification: category | measure | geography — geography classification is the "
            "precondition for every Geo Map object.",
            f"geographyNameCodeContext: one of {sorted(GEO_NAME_CODE_CONTEXTS)}.",
            "Raw lat/long point data: {'classification': 'geography', 'geographyDataSource': "
            "{'geographyCoordinates': {'latitudeDataItem': 'LATITUDE', 'longitudeDataItem': "
            "'LONGITUDE'}}} — coordinates OR a name-code context, never both.",
        ],
    },
    {
        "key": "addPage",
        "purpose": (
            "Add a page; a 'title' becomes a text band at the top of the page body; reference the "
            "page by pageName when placing objects."
        ),
        "required": [],
        "example": {"addPage": {"pageName": "Overview", "title": "Sales Overview", "pagePosition": "0"}},
        "notes": [
            "pagePosition is a STRING here ('0' = first) — unlike the numeric "
            "report-placement pagePosition.",
            "The title text lands in the page body (page headers accept only controls).",
        ],
    },
    {
        "key": "addObject",
        "purpose": "Add a visual/control/content object, titled and placed.",
        "required": ["object.<type>  (or reportObject)"],
        "example": {
            "addObject": {
                "object": {
                    "barChart": {
                        "dataSource": "SALES",
                        "dataRoles": {"category": "Region", "measures": ["Revenue (USD)"]},
                        "options": {"object": {"title": "Revenue by Region"}},
                    }
                },
                "placement": {"page": {"target": "Overview"}},
            }
        },
        "notes": [
            "Allowed object-spec keys: dataSource, dataRoles, options (VA rejects anything else, "
            "including a caller-supplied 'name').",
            "options.object.title/alternativeText work at add time on every type except "
            "standardContainer.",
        ],
    },
    {
        "key": "updateObject",
        "purpose": "Change an existing object's options (title, etc.) — never its placement or data roles.",
        "required": ["object.<type>.name"],
        "example": {
            "updateObject": {
                "object": {"barChart": {"name": "ve15", "options": {"object": {"title": "MSRP by Region"}}}}
            }
        },
        "notes": ["'name' is the object name (ve*) or label returned by a previous apply or get_report_outline."],
    },
    {
        "key": "setParameterValue",
        "purpose": "Set an EXISTING report parameter's value (parameters cannot be created here).",
        "required": ["name", "value"],
        "example": {"setParameterValue": {"name": "originFilter", "value": "Asia"}},
    },
    {
        "key": "updateData",
        "purpose": "Update an existing data source's items in place.",
        "required": ["data"],
        "example": {"updateData": {"data": {"name": "SALES"}}},
    },
    {
        "key": "changeData",
        "purpose": "Swap a data source for a different CAS table (copy-and-replace).",
        "required": ["originalData", "replacementData"],
        "example": {
            "changeData": {
                "originalData": {"cas": {"server": "cas-shared-default", "library": "Public", "table": "SALES"}},
                "replacementData": {
                    "cas": {"server": "cas-shared-default", "library": "Public", "table": "SALES_2025"}
                },
            }
        },
    },
    {
        "key": "applyDataView",
        "purpose": (
            "Import a data view saved in the VA UI — the sanctioned route to calculated items, "
            "hierarchies, and custom sorts."
        ),
        "required": ["targetData", "dataView"],
        "example": {
            "applyDataView": {
                "dataItemConflictResolution": "createDuplicate",
                "targetData": {"name": "SALES"},
                "dataView": {"name": "SALES View 1"},
            }
        },
        "notes": [
            "dataItemConflictResolution: abort (default) | createDuplicate | replaceExisting | "
            "keepExisting | dataMapping.",
            "dataView takes {'name': ...} or {'uri': ...}; there is no API to create or list data "
            "views — save one in the VA UI first.",
        ],
    },
)


# --- discovery ------------------------------------------------------------


def _example_for(obj: VaObject) -> dict[str, Any]:
    """Build a minimal, copy-paste ``addObject`` payload for *obj*.

    The role-less content objects get their real payloads (probing showed the
    generic dataSource template is rejected or useless for them), and every
    other object carries an inline ``options.object.title`` — titled-at-add-time
    is the single cheapest polish an authoring agent can apply.
    """
    if obj.schema_key == "text":
        body: dict[str, Any] = {"options": {"content": "Your narrative text here."}}
    elif obj.schema_key == "image":
        body = {"options": {"url": "https://example.com/logo.png"}}
    elif obj.schema_key == "standardContainer":
        body = {}
    else:
        seed = obj.commonly_required or (obj.role_names[:1] if obj.role_names else ())
        roles: dict[str, Any] = {}
        for name in seed:
            spec = next((r for r in obj.roles if r.name == name), None)
            placeholder = f"<{name}Column>"
            roles[name] = [placeholder] if (spec and spec.multi) else placeholder
        body = {"dataSource": "<dataSourceName>"}
        if roles:
            body["dataRoles"] = roles
        # keyValue tiles render their measure's label prominently — a title
        # just duplicates it; name the measure well via dataItems instead.
        if obj.schema_key != "keyValue":
            body["options"] = {"object": {"title": "<Meaningful chart title>"}}
    return {"addObject": {"object": {obj.schema_key: body}, "placement": {"page": {"target": "<pageName>"}}}}


def describe(
    object_type: str | None = None,
    category: str | None = None,
    operation: str | None = None,
) -> dict[str, Any]:
    """Return the object/operation catalog, one object's contract, or one operation's shape.

    * No args → an index: the eight operations, every object (schema key,
      purpose, category, addable/updatable), the placement guide, layout
      recipes, an intent→object map, and the API's honest limits.
    * ``category`` → the index filtered to that category.
    * ``object_type`` → one object's roles (name + whether it takes a list),
      the commonly-required roles, common options, and a ready-to-send example
      payload; or a ``not_addable`` / ``unknown_object_type`` redirect.
      Colloquial aliases (``kpi``, ``choropleth``, ...) resolve to the nearest
      schema key.
    * ``operation`` → that operation's full entry (purpose, required keys,
      worked example, notes).
    """
    if object_type:
        return _describe_one(object_type)
    if operation:
        return _describe_operation(operation)

    objects = [
        {
            "schema_key": o.schema_key,
            "purpose": o.purpose,
            "category": o.category,
            "addable": o.addable,
            "updatable": o.updatable,
        }
        for o in _OBJECTS
        if category is None or o.category == category
    ]
    result: dict[str, Any] = {
        "operations": [{"key": op["key"], "purpose": op["purpose"]} for op in OPERATIONS],
        "categories": list(CATEGORIES),
        "objects": objects,
        "intent_map": dict(INTENT_MAP),
        "placement": list(PLACEMENT_GUIDE),
        "layout_recipes": list(LAYOUT_RECIPES),
        "limits": list(API_LIMITS),
        "hint": (
            "Call describe_report_objects(object_type='barChart') for one object's data roles and "
            "an example payload, describe_report_objects(operation='addData') for an operation's "
            "full shape (addData's dataItems is where formats/aggregations/geography live), and "
            "get_castable_columns to map columns to roles. Use intent_map to pick the right object "
            "and the placement variants + layout_recipes to arrange objects instead of stacking them."
        ),
    }
    if category is not None and not objects:
        result["note"] = f"No objects in category '{category}'. Valid categories: {list(CATEGORIES)}."
    return result


def _describe_operation(operation: str) -> dict[str, Any]:
    for op in OPERATIONS:
        if op["key"] == operation:
            return dict(op)
    return {
        "status": "unknown_operation",
        "operation": operation,
        "valid_operations": sorted(OPERATION_KEYS),
        "did_you_mean": difflib.get_close_matches(operation, OPERATION_KEYS, n=3, cutoff=0.5),
    }


# Extra guidance for objects whose real-world contract probing showed to be
# surprising; merged into the describe() detail.
_OBJECT_NOTES: dict[str, dict[str, str]] = {
    "text": {
        "content_note": (
            "Set the text via options.content (a plain string — no markup). Caveat: some Viya "
            "builds misroute options.content to the report's FIRST text object on both add and "
            "update, so keep one content-bearing text per report, or write additional texts via "
            "the report content endpoint (PUT /reports/reports/{id}/content)."
        ),
    },
    "image": {
        "options_note": (
            "options is a oneOf directly under it (no wrapper): {'url': 'https://.../logo.png'} "
            "for a web image, OR {'imageName': 'logo.png', 'imageFolder': '/folders/folders/"
            "{folderId}'} for a repository image (imageFolder is the folders-service URI, not a "
            "display path). URL extension must look like an image (.png/.jpg); reachability is "
            "NOT checked at add time, repository existence IS."
        ),
    },
    "standardContainer": {
        "options_note": (
            "Accepts NO properties at add time — VA rejects even 'options'. Add it bare ({}), "
            "then title it via a follow-up updateObject using the name the apply returned. "
            "Containers auto-arrange their children; there is no layout/direction knob."
        ),
    },
    "dataDrivenContent": {
        "options_note": (
            "The content URL is NOT settable via the operations API — a dataDrivenContent added "
            "here renders empty until the URL is set in the VA UI."
        ),
    },
    "histogram": {
        "data_note": (
            "A dataItem carrying a preset aggregation breaks histogram binning (the rendered "
            "object shows 'missing data item'). Point the histogram's measure at the raw, "
            "unaggregated column — or use a boxPlot for aggregated comparisons."
        ),
    },
    "keyValue": {
        "title_note": (
            "Skip options.object.title on KPI tiles — the tile already renders its measure's "
            "label prominently, so a title duplicates it. Give the measure a good display name "
            "via addData dataItems (e.g. 'Avg Loan (USD)') instead, and place tiles side by side "
            "in a standardContainer, never stacked on the page."
        ),
    },
}


def _describe_one(object_type: str) -> dict[str, Any]:
    resolved_from: str | None = None
    normalized = _normalize_key(object_type)
    obj = REPORT_OBJECT_TYPES.get(object_type)
    if obj is None:
        target = _NORMALIZED_TYPES.get(normalized) or ALIASES.get(normalized)
        if target:
            resolved_from = object_type
            obj = REPORT_OBJECT_TYPES[target]
    if obj is not None:
        detail: dict[str, Any] = {
            "schema_key": obj.schema_key,
            "purpose": obj.purpose,
            "category": obj.category,
            "addable": obj.addable,
            "updatable": obj.updatable,
            "data_roles": [
                {
                    "name": r.name,
                    "takes": "list" if r.multi else "single",
                    "commonly_required": r.name in obj.commonly_required,
                }
                for r in obj.roles
            ],
            "commonly_required": list(obj.commonly_required),
        }
        if resolved_from:
            detail["resolved_from_alias"] = resolved_from
        if obj.addable:
            detail["example"] = _example_for(obj)
            detail["placement_hint"] = (
                "The example uses page placement. To arrange it precisely, swap placement for a "
                "relativeToObject / container variant — see describe_report_objects() placement + layout_recipes."
            )
        else:
            detail["note"] = f"'{obj.schema_key}' can be updated (updateObject) but not added via the API."
        if obj.schema_key == "standardContainer":
            detail["common_options"] = {
                "note": _OBJECT_NOTES["standardContainer"]["options_note"],
            }
        else:
            detail["common_options"] = {
                "shape": {"options": {"object": {"title": "<title>", "alternativeText": "<alt text>"}}},
                "note": (
                    "Accepted inline at add time — title every visual meaningfully instead of "
                    "relying on VA's auto-labels."
                ),
            }
        detail.update(_OBJECT_NOTES.get(obj.schema_key, {}))
        if obj.render_required:
            detail["render_required"] = [list(group) for group in obj.render_required]
        if obj.category == "Geo Maps":
            detail["precondition"] = (
                "Geo objects need their column classified as geography first — in the same batch is "
                "fine: addData with dataItems [{'dataItem': 'State', 'properties': {'classification': "
                "'geography', 'geographyDataSource': {'geographyNameCodeContext': 'USStateNames'}}}]. "
                "For raw lat/long point data use geographyCoordinates instead: {'geographyDataSource': "
                "{'geographyCoordinates': {'latitudeDataItem': 'LATITUDE', 'longitudeDataItem': "
                "'LONGITUDE'}}}. See describe_report_objects(operation='addData') for the full shape."
            )
        return detail

    not_addable_key = object_type if object_type in NOT_ADDABLE else _NORMALIZED_NOT_ADDABLE.get(normalized)
    if not_addable_key:
        alt = NOT_ADDABLE[not_addable_key]
        return {
            "status": "not_addable",
            "object_type": object_type,
            "nearest": alt,
            "message": f"'{object_type}' is a VA UI object with no report-API support. Use '{alt}' instead.",
        }

    candidates = list(REPORT_OBJECT_TYPES) + list(ALIASES) + list(NOT_ADDABLE)
    matches = difflib.get_close_matches(object_type, candidates, n=3, cutoff=0.5)
    suggestions = list(dict.fromkeys(ALIASES.get(m, m) for m in matches))
    return {
        "status": "unknown_object_type",
        "object_type": object_type,
        "did_you_mean": suggestions,
        "hint": "Call describe_report_objects() with no arguments to list every object type.",
    }


# --- normalisation --------------------------------------------------------


def normalize_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of *operations* with tolerant, non-semantic coercions.

    * Coerces an integer ``addPage.pagePosition`` to the string index that
      operation expects (report-placement ``pagePosition`` is the opposite: a
      number — a digit string there is coerced to int).
    * Translates the spec spelling ``newPage`` to the ``new_page`` the live
      report-placement enum actually accepts.
    * Wraps a bare column string in a list for array-valued (``multi``) data
      roles, so ``measures="MSRP"`` works as well as ``measures=["MSRP"]``.
    * Expands an ``addPage`` convenience ``title`` into a text object placed at
      the top of that page's body (VA rejects text in page headers) — the
      addPage keeps its ``pageName`` and the title text is appended as a
      following ``addObject``.

    Never raises — malformed input is caught by :func:`validate_operations`.
    """
    try:
        ops = json.loads(json.dumps(operations))
    except (TypeError, ValueError):
        return operations
    if not isinstance(ops, list):
        return operations
    out: list[dict[str, Any]] = []
    title_ops: list[dict[str, Any]] = []
    for op in ops:
        if not isinstance(op, dict):
            out.append(op)
            continue
        page = op.get("addPage")
        if isinstance(page, dict):
            position = page.get("pagePosition")
            if isinstance(position, int) and not isinstance(position, bool):
                page["pagePosition"] = str(position)
            # Expand a page title into a body text band, but only when the
            # page is named (the text object must target the page by name).
            # Always strip the tool-only 'title' key — VA rejects unknown
            # properties — and skip the text op for an empty title.
            if "title" in page and page.get("pageName"):
                title = page.pop("title")
                if title:
                    title_ops.append(_page_title_object(page["pageName"], str(title)))
        add = op.get("addObject")
        if isinstance(add, dict):
            _normalize_roles(add.get("object"))
            _normalize_placement(add.get("placement"))
        out.append(op)
    # Synthesized title ops go at the END of the batch so the caller's
    # operation indices survive normalization — op_index in validation errors
    # and VA's failed-at-index messages keep pointing at the caller's array.
    # position "start" still renders the text at the top of the page body
    # regardless of when in the batch it is applied.
    out.extend(title_ops)
    return out


def _page_title_object(page_name: str, title: str) -> dict[str, Any]:
    """Build an addObject op putting *title* text at the top of *page_name*'s body.

    Page (and report) headers accept only control objects — a text placed with
    ``context: "header"`` fails the whole atomic batch — so a page title is a
    text band at the start of the body.
    """
    return {
        "addObject": {
            "object": {"text": {"options": {"content": title}}},
            "placement": {"page": {"target": page_name, "context": "body", "position": "start"}},
        }
    }


def _normalize_placement(placement: Any) -> None:
    """In-place report-placement coercions: enum spelling and pagePosition typing."""
    if not isinstance(placement, dict):
        return
    report = placement.get("report")
    if not isinstance(report, dict):
        return
    if report.get("context") == "newPage":  # published-spec spelling; live enum is snake_case
        report["context"] = "new_page"
    position = report.get("pagePosition")
    if isinstance(position, str):
        # A non-numeric string is left for validation to reject with the typed message.
        with contextlib.suppress(ValueError):
            report["pagePosition"] = int(position)


def _normalize_roles(obj: Any) -> None:
    if not isinstance(obj, dict) or len(obj) != 1:
        return
    (type_key,) = obj
    vo = REPORT_OBJECT_TYPES.get(type_key)
    spec = obj[type_key]
    if vo is None or not isinstance(spec, dict):
        return
    roles = spec.get("dataRoles")
    if not isinstance(roles, dict):
        return
    multi = {r.name for r in vo.roles if r.multi}
    for name, value in list(roles.items()):
        if name in multi and isinstance(value, str):
            roles[name] = [value]


# --- validation -----------------------------------------------------------


def _err(status: str, index: int, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "op_index": index, "message": message, **extra}


def validate_operations(operations: Any) -> dict[str, Any] | None:
    """Return a structured error dict for the invalid operations, else None.

    Enforces the rules the VA endpoints impose *before* any HTTP call: exactly
    one known operation per array element; for ``addObject`` a known + addable
    object type with only the spec-allowed keys, whose ``dataRoles`` are a
    subset of that type's roles with the right list/single arity, and a valid
    placement; for ``updateObject`` a known + updatable type with a ``name``;
    and the required blocks for the data operations. Every invalid operation is
    reported at once (the batch is atomic, so serial fix-one-resend loops are
    expensive): the returned dict is the first error, carrying ``all_errors``
    when more than one operation failed.
    """
    if not isinstance(operations, list) or not operations:
        return {"status": "invalid_operation", "message": "operations must be a non-empty list of operation objects."}
    errors = [err for i, op in enumerate(operations) for err in [_validate_one(op, i)] if err is not None]
    if not errors:
        return None
    if len(errors) == 1:
        return errors[0]
    return {**errors[0], "error_count": len(errors), "all_errors": errors}


def _validate_one(op: Any, i: int) -> dict[str, Any] | None:
    if not isinstance(op, dict):
        return _err("invalid_operation", i, "each operation must be an object.")
    keys = [k for k in op if k in OPERATION_KEYS]
    if not keys:
        unknown = [k for k in op if k not in _META_OP_KEYS]
        return _err(
            "invalid_operation",
            i,
            f"no known operation key. Expected one of {sorted(OPERATION_KEYS)}.",
            unknown_keys=unknown,
        )
    if len(keys) > 1:
        return _err("invalid_operation", i, f"exactly one operation per array element; got {sorted(keys)}.")
    stray = [k for k in op if k not in OPERATION_KEYS and k not in _META_OP_KEYS]
    if stray:
        return _err(
            "invalid_operation",
            i,
            f"unknown key(s) {sorted(stray)} alongside '{keys[0]}' — VA rejects unrecognised "
            f"properties; allowed meta keys: {sorted(_META_OP_KEYS)}.",
            unknown_keys=sorted(stray),
        )
    key = keys[0]
    val = op[key]
    if key == "addObject":
        return _validate_add_object(val, i)
    if key == "updateObject":
        return _validate_update_object(val, i)
    if key == "addData":
        err = _require_keys(val, i, "addData", ("cas",), nested={"cas": ("library", "table")})
        # The OpenAPI spec marks cas.server optional, but live Viya rejects the
        # whole batch without it ("Missing the required property: server").
        if err is None and isinstance(val.get("cas"), dict) and not val["cas"].get("server"):
            err = _err(
                "invalid_operation",
                i,
                "'addData.cas' requires 'server' (the CAS server name — list_cas_servers returns "
                "it; typically 'cas-shared-default').",
            )
        return err if err is not None else _validate_data_items(val, i, "addData")
    if key == "changeData":
        return _require_keys(val, i, "changeData", ("originalData", "replacementData"))
    if key == "applyDataView":
        return _require_keys(val, i, "applyDataView", ("targetData", "dataView"))
    if key == "setParameterValue":
        return _require_keys(val, i, "setParameterValue", ("name", "value"))
    if key == "addPage":
        return _validate_add_page(val, i)
    if key == "updateData":
        err = _require_keys(val, i, "updateData", ("data",))
        if err is not None:
            return err
        data = val.get("data")
        if not isinstance(data, dict):
            return _err("invalid_operation", i, "'updateData.data' must be an object.")
        return _validate_data_items(data, i, "updateData")
    return None


def _validate_add_page(val: Any, i: int) -> dict[str, Any] | None:
    if not isinstance(val, dict):
        return _err("invalid_operation", i, "'addPage' must be an object.")
    # A title becomes a body text object targeting the page by name, so the
    # page must be named. (When pageName is present, normalize strips the title
    # before validation, so this only fires on the unnamed case.)
    if "title" in val and not val.get("pageName"):
        return _err(
            "invalid_operation",
            i,
            "'addPage.title' requires 'pageName' — a page title is a text object placed at the "
            "top of the named page's body.",
        )
    position = val.get("pagePosition")
    if position is not None and (isinstance(position, bool) or not isinstance(position, (str, int))):
        return _err(
            "invalid_operation",
            i,
            "'addPage.pagePosition' must be a string index like '0' (an int is coerced for you) — "
            "unlike the numeric report-placement pagePosition.",
        )
    return None


def _validate_data_items(val: Any, i: int, op: str) -> dict[str, Any] | None:
    """Validate the optional ``dataItems`` polish block against the spec enums."""
    items = val.get("dataItems")
    if items is None:
        return None
    if not isinstance(items, list):
        return _err("invalid_operation", i, f"'{op}.dataItems' must be a list of {{dataItem, properties}} objects.")
    for item in items:
        if not isinstance(item, dict) or not item.get("dataItem"):
            return _err(
                "invalid_operation",
                i,
                f"each '{op}.dataItems' entry needs 'dataItem' (the column name) plus 'properties'.",
            )
        props = item.get("properties")
        if props is None:
            continue
        if not isinstance(props, dict):
            return _err("invalid_operation", i, f"'{op}.dataItems[].properties' must be an object.")
        aggregation = props.get("aggregation")
        if aggregation is not None and aggregation not in AGGREGATIONS:
            return _err(
                "invalid_operation",
                i,
                f"unknown aggregation '{aggregation}' for data item '{item['dataItem']}'.",
                valid_values=sorted(AGGREGATIONS),
            )
        fmt = props.get("format")
        if fmt is not None and (not isinstance(fmt, str) or not _SAS_FORMAT_RE.match(fmt)):
            return _err(
                "invalid_operation",
                i,
                f"format '{fmt}' is not a named SAS format — VA rejects bare numeric w.d forms "
                f"(and the whole atomic batch with them). Use e.g. DOLLAR12.2, COMMA10., "
                f"PERCENT8.1, DATE9.",
            )
        classification = props.get("classification")
        if classification is not None and classification not in CLASSIFICATIONS:
            return _err(
                "invalid_operation",
                i,
                f"unknown classification '{classification}' for data item '{item['dataItem']}'.",
                valid_values=sorted(CLASSIFICATIONS),
            )
        geo = props.get("geographyDataSource")
        if geo is not None:
            if classification != "geography":
                return _err(
                    "invalid_operation",
                    i,
                    "'geographyDataSource' is only allowed together with classification 'geography'.",
                )
            if not isinstance(geo, dict):
                return _err(
                    "invalid_operation",
                    i,
                    "'geographyDataSource' must be an object, e.g. "
                    "{'geographyNameCodeContext': 'USStateNames'}.",
                )
            if isinstance(geo, dict):
                unknown = sorted(set(geo) - GEO_SOURCE_KEYS)
                if unknown:
                    return _err(
                        "invalid_operation",
                        i,
                        f"unknown geographyDataSource key(s) {unknown} — for lat/long point data "
                        f"use geographyCoordinates: {{'latitudeDataItem': '<col>', "
                        f"'longitudeDataItem': '<col>'}}.",
                        valid_keys=sorted(GEO_SOURCE_KEYS),
                    )
                context = geo.get("geographyNameCodeContext")
                if context is not None and context not in GEO_NAME_CODE_CONTEXTS:
                    return _err(
                        "invalid_operation",
                        i,
                        f"unknown geographyNameCodeContext '{context}'.",
                        valid_values=sorted(GEO_NAME_CODE_CONTEXTS),
                    )
                coordinates = geo.get("geographyCoordinates")
                if coordinates is not None:
                    if context is not None:
                        return _err(
                            "invalid_operation",
                            i,
                            "geographyDataSource takes geographyNameCodeContext OR "
                            "geographyCoordinates, not both.",
                        )
                    if not isinstance(coordinates, dict) or not (
                        coordinates.get("latitudeDataItem") and coordinates.get("longitudeDataItem")
                    ):
                        return _err(
                            "invalid_operation",
                            i,
                            "'geographyCoordinates' requires 'latitudeDataItem' and "
                            "'longitudeDataItem' (column names or labels).",
                        )
    return None


def _require_keys(
    val: Any, i: int, op: str, keys: tuple[str, ...], nested: dict[str, tuple[str, ...]] | None = None
) -> dict[str, Any] | None:
    if not isinstance(val, dict):
        return _err("invalid_operation", i, f"'{op}' must be an object.")
    for k in keys:
        if k not in val:
            return _err("invalid_operation", i, f"'{op}' requires '{k}'.")
    for parent, children in (nested or {}).items():
        block = val.get(parent)
        if not isinstance(block, dict):
            return _err("invalid_operation", i, f"'{op}.{parent}' must be an object.")
        for c in children:
            if c not in block:
                return _err("invalid_operation", i, f"'{op}.{parent}' requires '{c}'.")
    return None


def _single_type(container: Any, i: int, op: str) -> tuple[str, Any] | dict[str, Any]:
    """Extract the single ``{<type>: spec}`` pair, or an error dict."""
    if not isinstance(container, dict) or len(container) != 1:
        return _err(
            "invalid_operation", i, f"'{op}.object' must name exactly one object type, e.g. {{'barChart': {{...}}}}."
        )
    (type_key,) = container
    return type_key, container[type_key]


def _resolve_type(type_key: str, i: int, *, for_update: bool) -> dict[str, Any] | None:
    """Return an error dict if *type_key* can't be added/updated, else None."""
    obj = REPORT_OBJECT_TYPES.get(type_key)
    if obj is None:
        if type_key in NOT_ADDABLE:
            alt = NOT_ADDABLE[type_key]
            return _err(
                "not_addable",
                i,
                f"'{type_key}' is a VA UI object with no report-API support. Use '{alt}'.",
                object_type=type_key,
                nearest=alt,
            )
        return _err(
            "unknown_object_type",
            i,
            f"unknown object type '{type_key}'.",
            object_type=type_key,
            did_you_mean=difflib.get_close_matches(type_key, REPORT_OBJECT_TYPES, n=3, cutoff=0.5),
        )
    if for_update and not obj.updatable:
        return _err(
            "not_updatable", i, f"'{type_key}' cannot be updated via the API (it is add-only).", object_type=type_key
        )
    if not for_update and not obj.addable:
        return _err(
            "not_addable", i, f"'{type_key}' cannot be added via the API (it is update-only).", object_type=type_key
        )
    return None


# VA enforces additionalProperties:false on every object spec: an unknown key
# (including a caller-supplied 'name') fails the whole atomic batch with an HTTP
# 400 — catch it pre-flight instead. standardContainer is stricter still: it
# accepts NO properties at add time, not even 'options'.
_OBJECT_SPEC_ALLOWED_KEYS = frozenset({"dataSource", "dataRoles", "options"})


def _validate_object_spec_keys(type_key: str, spec: Any, i: int) -> dict[str, Any] | None:
    if not isinstance(spec, dict):
        return None  # shape errors are reported by _validate_roles
    if type_key == "standardContainer":
        if spec:
            return _err(
                "invalid_object_spec",
                i,
                "standardContainer accepts no properties at add time (VA rejects even 'options'); "
                "add it bare ({}) and set its title via a follow-up updateObject using the name "
                "the apply returns.",
                object_type=type_key,
            )
        return None
    extras = sorted(set(spec) - _OBJECT_SPEC_ALLOWED_KEYS)
    if extras:
        if "name" in extras:
            return _err(
                "invalid_object_spec",
                i,
                "objects are auto-named by VA — 'name' is not allowed at add time; use the name "
                "the apply result returns (or get_report_outline) to reference the object later.",
                object_type=type_key,
                unknown_keys=extras,
            )
        return _err(
            "invalid_object_spec",
            i,
            f"'{type_key}' does not accept {extras}; allowed keys: "
            f"{sorted(_OBJECT_SPEC_ALLOWED_KEYS)}.",
            object_type=type_key,
            unknown_keys=extras,
        )
    return None


def _validate_add_object(val: Any, i: int) -> dict[str, Any] | None:
    if not isinstance(val, dict):
        return _err("invalid_operation", i, "'addObject' must be an object.")
    has_object = "object" in val
    has_report_object = "reportObject" in val
    if has_object and has_report_object:
        return _err("invalid_operation", i, "'addObject' takes object OR reportObject, not both.")
    if not has_object and not has_report_object:
        return _err("invalid_operation", i, "'addObject' requires 'object' or 'reportObject'.")
    if has_report_object:
        # Adding a pre-existing object by reference — the object itself needs
        # no validation, but its placement still does.
        return _validate_placement(val.get("placement"), i)

    extracted = _single_type(val["object"], i, "addObject")
    if isinstance(extracted, dict):
        return extracted
    type_key, spec = extracted
    resolved = _resolve_type(type_key, i, for_update=False)
    if resolved is not None:
        return resolved
    keys_err = _validate_object_spec_keys(type_key, spec, i)
    if keys_err is not None:
        return keys_err
    roles_err = _validate_roles(REPORT_OBJECT_TYPES[type_key], spec, i)
    if roles_err is not None:
        return roles_err
    placement_err = _validate_placement(val.get("placement"), i)
    if placement_err is not None:
        return placement_err
    return _validate_header_placement(type_key, val.get("placement"), i)


def _validate_header_placement(type_key: str, placement: Any, i: int) -> dict[str, Any] | None:
    """Page/report headers accept ONLY control objects — enforce it pre-flight.

    VA rejects anything else with "Only control objects are supported in the
    page header" and rolls back the whole atomic batch.
    """
    if not isinstance(placement, dict):
        return None
    for variant in ("page", "report"):
        inner = placement.get(variant)
        if isinstance(inner, dict) and inner.get("context") == "header":
            obj = REPORT_OBJECT_TYPES.get(type_key)
            if obj is not None and obj.category != "Controls":
                return _err(
                    "invalid_placement",
                    i,
                    f"the {variant} header accepts ONLY control objects (dropdownList, buttonBar, "
                    f"slider, ...); '{type_key}' ({obj.category}) belongs in the body — a title is "
                    "a text object placed with context 'body', position 'start'.",
                    object_type=type_key,
                )
    return None


def _validate_placement(placement: Any, i: int) -> dict[str, Any] | None:
    """Validate an addObject ``placement`` block, or None if valid/absent."""
    if placement is None:
        return None
    if not isinstance(placement, dict) or len(placement) != 1:
        return _err(
            "invalid_placement",
            i,
            f"placement must name exactly one of {list(PLACEMENT_VARIANTS)}.",
            valid_variants=list(PLACEMENT_VARIANTS),
        )
    (variant,) = placement
    if variant not in PLACEMENT_VARIANTS:
        return _err(
            "invalid_placement",
            i,
            f"unknown placement variant '{variant}'.",
            valid_variants=list(PLACEMENT_VARIANTS),
        )
    inner = placement[variant]
    if not isinstance(inner, dict):
        return _err("invalid_placement", i, f"placement.{variant} must be an object.")
    extras = sorted(set(inner) - _PLACEMENT_ALLOWED_KEYS[variant])
    if extras:
        return _err(
            "invalid_placement",
            i,
            f"placement.{variant} does not accept {extras}; allowed keys: "
            f"{sorted(_PLACEMENT_ALLOWED_KEYS[variant])}.",
            unknown_keys=extras,
        )
    if variant in _PLACEMENT_TARGET_REQUIRED and not inner.get("target"):
        return _err(
            "invalid_placement",
            i,
            f"placement.{variant} requires 'target' (the name of the {variant} to place against).",
        )
    for field_name, allowed in _PLACEMENT_ENUMS.get(variant, {}).items():
        value = inner.get(field_name)
        if value is not None and value not in allowed:
            return _err(
                "invalid_placement",
                i,
                f"placement.{variant}.{field_name} must be one of {sorted(allowed)}; got '{value}'.",
                valid_values=sorted(allowed),
            )
    if variant == "report":
        page_name = inner.get("pageName")
        if page_name is not None and (not isinstance(page_name, str) or not page_name.strip()):
            return _err("invalid_placement", i, "placement.report.pageName must be a non-empty string.")
        page_position = inner.get("pagePosition")
        if page_position is not None and (
            isinstance(page_position, bool) or not isinstance(page_position, (int, float))
        ):
            return _err(
                "invalid_placement",
                i,
                "placement.report.pagePosition must be a NUMBER (0 puts the new page first) — "
                "unlike addPage.pagePosition, which is a string.",
            )
    return None


def _validate_update_object(val: Any, i: int) -> dict[str, Any] | None:
    if not isinstance(val, dict) or "object" not in val:
        return _err("invalid_operation", i, "'updateObject' requires 'object'.")
    extracted = _single_type(val["object"], i, "updateObject")
    if isinstance(extracted, dict):
        return extracted
    type_key, spec = extracted
    resolved = _resolve_type(type_key, i, for_update=True)
    if resolved is not None:
        return resolved
    if not isinstance(spec, dict) or not spec.get("name"):
        return _err(
            "invalid_operation",
            i,
            f"'updateObject.object.{type_key}' requires 'name' (the existing object's name or label).",
        )
    extras = sorted(set(spec) - {"name", "options"})
    if extras:
        return _err(
            "invalid_object_spec",
            i,
            f"'updateObject.object.{type_key}' does not accept {extras} — updates can change only "
            "'options' (placement and dataRoles are write-once; there is no move/re-role).",
            object_type=type_key,
            unknown_keys=extras,
        )
    return None


def _validate_roles(obj: VaObject, spec: Any, i: int) -> dict[str, Any] | None:
    if not isinstance(spec, dict):
        return _err("invalid_operation", i, f"'{obj.schema_key}' must be an object.")
    roles = spec.get("dataRoles")
    if roles is None:
        return None
    if not isinstance(roles, dict):
        return _err(
            "invalid_roles",
            i,
            f"'{obj.schema_key}.dataRoles' must be an object of role -> column.",
            object_type=obj.schema_key,
            valid_roles=list(obj.role_names),
        )
    by_name = {r.name: r for r in obj.roles}
    for name, value in roles.items():
        spec_role = by_name.get(name)
        if spec_role is None:
            return _err(
                "invalid_roles",
                i,
                f"'{name}' is not a role of {obj.schema_key}.",
                object_type=obj.schema_key,
                valid_roles=list(obj.role_names),
            )
        if not spec_role.multi and isinstance(value, list):
            return _err(
                "invalid_roles",
                i,
                f"role '{name}' on {obj.schema_key} takes a single column, not a list.",
                object_type=obj.schema_key,
            )
    return None


def warn_operations(operations: list[dict[str, Any]]) -> list[str]:
    """Return non-blocking warnings for render risks the API accepts silently.

    The OpenAPI spec marks every data role optional, so these are advisory: an
    object can pass validation (and the VA PUT) yet render blank without its
    usual roles, and multiple content-bearing texts can collapse onto one
    element on affected Viya builds.
    """
    warnings: list[str] = []
    if not isinstance(operations, list):
        return warnings
    content_texts = 0
    page_stacked: dict[str, int] = {}
    for op in operations:
        if not isinstance(op, dict):
            continue
        add = op.get("addObject")
        if not isinstance(add, dict) or "object" not in add:
            continue
        placement = add.get("placement")
        if isinstance(placement, dict):
            page = placement.get("page")
            report = placement.get("report")
            if isinstance(page, dict) and page.get("context") != "header":
                target = str(page.get("target"))
                page_stacked[target] = page_stacked.get(target, 0) + 1
            elif isinstance(report, dict) and report.get("pageName"):
                target = str(report["pageName"])
                page_stacked[target] = page_stacked.get(target, 0) + 1
        container = add["object"]
        if not isinstance(container, dict) or len(container) != 1:
            continue
        (type_key,) = container
        spec = container[type_key]
        if type_key == "text" and isinstance(spec, dict):
            options = spec.get("options")
            if isinstance(options, dict) and options.get("content"):
                content_texts += 1
        obj = REPORT_OBJECT_TYPES.get(type_key)
        if obj is None:
            continue
        provided = set()
        if isinstance(spec, dict) and isinstance(spec.get("dataRoles"), dict):
            provided = set(spec["dataRoles"])
        missing = [r for r in obj.commonly_required if r not in provided]
        if missing:
            warnings.append(f"{type_key} usually needs role(s) {missing}; none provided — it may render empty.")
        for group in obj.render_required:
            if not any(role in provided for role in group):
                warnings.append(
                    f"{type_key} renders EMPTY without one of {list(group)} — the API does not "
                    f"auto-apply Frequency the way the VA UI does; add a measure (a count/flag "
                    f"column works)."
                )
    if content_texts >= 2:
        warnings.append(
            f"This batch creates {content_texts} content-bearing text objects; some Viya builds "
            f"misroute text options.content onto the report's FIRST text element (add and update "
            f"alike). Prefer one content-bearing text per report — e.g. title at most one page — "
            f"and check the result's text_content_warning / get_report_outline after applying."
        )
    for target, count in page_stacked.items():
        if count >= 4:
            warnings.append(
                f"{count} objects are page-placed onto '{target}' — the page body auto-flows "
                f"VERTICALLY, so they will render as one tall stack. Put KPI tiles side by side "
                f"in a standardContainer and arrange charts in rows with relativeToObject "
                f"left/right (see describe_report_objects layout_recipes)."
            )
    return warnings


# --- request shaping ------------------------------------------------------


@dataclass
class CreateReportRequest:
    """A normalised ``create_report`` invocation."""

    name: str
    folder: str | None = None
    on_conflict: str = "rename"
    operations: list[dict[str, Any]] | None = field(default=None)


def validate_create(req: CreateReportRequest) -> dict[str, Any] | None:
    """Validate a create request (name, conflict policy, any inline operations)."""
    if not req.name or not str(req.name).strip():
        return {"status": "invalid_request", "message": "create_report requires a non-empty name."}
    if req.on_conflict not in CONFLICT_VALUES:
        return {
            "status": "invalid_request",
            "message": f"on_conflict must be one of {sorted(CONFLICT_VALUES)}.",
            "on_conflict": req.on_conflict,
        }
    if req.operations:
        return validate_operations(req.operations)
    return None


def build_create_body(req: CreateReportRequest) -> dict[str, Any]:
    """Build the POST /visualAnalytics/reports request body."""
    body: dict[str, Any] = {"resultReportName": req.name, "resultNameConflict": req.on_conflict}
    if req.folder:
        body["resultFolder"] = req.folder
    if req.operations:
        body["operations"] = req.operations
    return body


def summarize_created(operations: list[dict[str, Any]], response: dict[str, Any]) -> dict[str, Any]:
    """Summarise what an operations batch created, from the request (+ response).

    Reads the requested pages/objects/data sources from *operations* so the
    result is deterministic regardless of the PUT response shape, then merges
    the per-operation response entries **by index** — the VA response
    ``operations`` array is a strict 1:1 mapping of the request array (failed
    entries keep their slot). Each entry carries ``name`` (the handle placement
    targets and updateObject consume, e.g. ``ve15``), ``label`` (the display
    label ``export_report`` consumes, e.g. ``Text 2``), and ``status``.
    """
    pages: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    data_sources: list[dict[str, Any]] = []
    slots: list[dict[str, Any] | None] = []
    for op in operations or []:
        record: dict[str, Any] | None = None
        if isinstance(op, dict):
            if "addPage" in op:
                page = op["addPage"] if isinstance(op["addPage"], dict) else {}
                # The response name is the internal section name (vi*); the
                # label is the requested pageName — what page placement targets.
                record = {"label": page.get("pageName")}
                pages.append(record)
            elif "addData" in op and isinstance(op["addData"], dict):
                add = op["addData"]
                cas = add.get("cas", {}) if isinstance(add.get("cas"), dict) else {}
                record = {"name": add.get("name") or cas.get("table")}
                data_sources.append(record)
            elif "addObject" in op and isinstance(op["addObject"], dict):
                container = op["addObject"].get("object")
                if isinstance(container, dict) and len(container) == 1:
                    (type_key,) = container
                    record = {
                        "type": type_key,
                        "page": _placement_page(op["addObject"].get("placement")),
                        "placement": op["addObject"].get("placement"),
                    }
                    objects.append(record)
        slots.append(record)
    _merge_response_results(slots, response)
    return {"pages": pages, "objects": objects, "dataSources": data_sources}


def _placement_page(placement: Any) -> str | None:
    if not isinstance(placement, dict):
        return None
    page = placement.get("page")
    if isinstance(page, dict):
        return page.get("target")
    report = placement.get("report")
    if isinstance(report, dict):
        return report.get("pageName")
    return None


def _merge_response_results(slots: list[dict[str, Any] | None], response: dict[str, Any]) -> None:
    """Merge per-operation response entries onto the request records by index."""
    if not isinstance(response, dict):
        return
    results = response.get("operations") or response.get("operationResponses")
    if not isinstance(results, list):
        return
    # Index-aligned 1:1 with the request array; strict=False tolerates a
    # response the server truncated.
    for record, entry in zip(slots, results, strict=False):
        if record is None or not isinstance(entry, dict):
            continue
        for key in ("name", "label", "status"):
            # Request-provided values win, but a pre-seeded None (e.g. an
            # unnamed addPage's label) must not block the response value.
            if entry.get(key) is not None and record.get(key) is None:
                record[key] = entry[key]
        if entry.get("messages"):
            record["messages"] = entry["messages"]


def parse_failure(response_text: str) -> dict[str, Any]:
    """Extract structured per-operation failure details from a VA error body.

    A rejected batch echoes the ``operations`` array with the failing entry at
    its request index carrying ``status: Failed/Invalid`` plus ``messages``;
    top-level ``messages`` name the failing index. Returns ``{}`` when the body
    is not parseable JSON in that shape.
    """
    try:
        data = json.loads(response_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    results = data.get("operations") or data.get("operationResponses")
    if isinstance(results, list):
        failed = [
            {"op_index": i, "status": entry.get("status"), "messages": entry.get("messages")}
            for i, entry in enumerate(results)
            if isinstance(entry, dict) and entry.get("status") not in (None, "Success")
        ]
        if failed:
            out["failed_operations"] = failed
            out["failed_operation_index"] = failed[0]["op_index"]
    if isinstance(data.get("messages"), list):
        out["viya_messages"] = data["messages"]
    return out


# --- execution ------------------------------------------------------------


def viewer_url(report_id: Any) -> str | None:
    """A ready-to-open VA viewer deep link — the deliverable of every build."""
    if not report_id:
        return None
    return f"{VIYA_ENDPOINT}/SASVisualAnalytics/?reportUri=/reports/reports/{report_id}"


def _expected_text_pairs(
    operations: list[dict[str, Any]], created_objects: list[dict[str, Any]]
) -> list[tuple[str, str]]:
    """(created ve-name, intended content) for each content-bearing text op.

    Paired op-by-op: every text op consumes one created text record (matching
    summarize_created's selection), whether or not it carries content — two
    independently filtered lists would shift against each other whenever a
    content-less text precedes a content-bearing one, producing false
    misroute warnings.
    """
    text_records = iter([o for o in created_objects if o.get("type") == "text"])
    pairs: list[tuple[str, str]] = []
    for op in operations or []:
        if not isinstance(op, dict) or not isinstance(op.get("addObject"), dict):
            continue
        container = op["addObject"].get("object")
        if not isinstance(container, dict) or len(container) != 1 or "text" not in container:
            continue
        record = next(text_records, None)
        if record is None:
            break
        spec = container["text"]
        options = spec.get("options") if isinstance(spec, dict) else None
        content = options.get("content") if isinstance(options, dict) else None
        if isinstance(content, str) and record.get("name"):
            pairs.append((record["name"], content))
    return pairs


async def check_text_contents(
    report_id: Any,
    operations: list[dict[str, Any]],
    created_objects: list[dict[str, Any]],
    client: httpx.AsyncClient,
) -> str | None:
    """Best-effort post-apply check that text contents landed on their elements.

    Some Viya builds misroute text ``options.content`` onto the report's FIRST
    text element (on add AND update), silently shipping wrong titles. One cheap
    content GET catches it; returns a warning string on mismatch, else None.
    Never raises.
    """
    try:
        pairs = _expected_text_pairs(operations, created_objects)
        if not pairs or not report_id:
            return None
        resp = await client.get(
            f"{VIYA_ENDPOINT}{CONTENT_PATH.format(report_id=report_id)}",
            headers={"Accept": CONTENT_ACCEPT},
        )
        if resp.status_code >= 400:
            return None
        outline = reduce_content_outline(_response_json_dict(resp))
        actual = {
            obj["name"]: obj.get("text")
            for page in outline.get("pages", [])
            for obj in page.get("objects", [])
            if obj.get("name")
        }
        mismatched = [
            f"'{content}' was meant for {name}, which holds {actual.get(name)!r}"
            for name, content in pairs
            if (actual.get(name) or "").strip() != content.strip()
        ]
        if mismatched:
            return (
                "Text content check FAILED — this Viya build misroutes text options.content onto "
                "the report's first text element: " + "; ".join(mismatched) + ". Keep one "
                "content-bearing text per report (title at most one page), or set contents via "
                "the report content endpoint."
            )
        return None
    except Exception:  # noqa: BLE001 - verification must never break the apply
        return None


def _response_json_dict(resp: httpx.Response) -> dict[str, Any]:
    """The response body as a dict — {} for empty, non-JSON, or non-object bodies."""
    if not resp.content:
        return {}
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _verify_hint(report_id: Any, created: dict[str, Any]) -> str | None:
    """A next-step suggestion so authoring agents close the see-what-you-built loop."""
    if not created.get("pages") and not created.get("objects"):
        return None
    labels = [p.get("label") or p.get("name") for p in created.get("pages", [])]
    page = next((label for label in labels if label), "<page label>")
    return (
        f"To see the result: export_report('{report_id}', 'png', report_objects=['{page}'], "
        "image_size='1200px,800px') — export page-by-page (whole-report png can render blank); "
        "get_report_outline returns the structure with the names/labels to target in follow-ups."
    )


async def execute_create(req: CreateReportRequest, client: httpx.AsyncClient) -> dict[str, Any]:
    """POST a create request and return ``{status, id, name}`` (or a failure dict)."""
    body = build_create_body(req)
    resp = await client.post(
        f"{VIYA_ENDPOINT}{CREATE_PATH}",
        content=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    if resp.status_code >= 400:
        return {
            "status": "create_failed",
            "name": req.name,
            "http_status": resp.status_code,
            "message": (
                f"Viya rejected the report creation (HTTP {resp.status_code}). A name "
                f"conflict policy of '{req.on_conflict}' with an existing name can cause this. "
                f"Viya said: {resp.text[:400] or '(no response body)'}"
            ),
            **parse_failure(resp.text),
        }
    data = _response_json_dict(resp)
    report_id = data.get("resultReportId") or data.get("id")
    result: dict[str, Any] = {
        "status": "created",
        "id": report_id,
        "name": data.get("resultReportName") or req.name,
    }
    url = viewer_url(report_id)
    if url:
        result["open_url"] = url
    if req.operations:
        created = summarize_created(req.operations, data)
        result["created"] = created
        if created.get("pages"):
            result["note"] = (
                'VA prepends an empty default "Page 1" before pages added at creation, so '
                "whole-report exports can render blank — verify page-by-page."
            )
        hint = _verify_hint(report_id, created)
        if hint:
            result["verify_hint"] = hint
        text_warning = await check_text_contents(report_id, req.operations, created.get("objects", []), client)
        if text_warning:
            result["text_content_warning"] = text_warning
    return result


async def _read_etag(report_id: str, client: httpx.AsyncClient) -> str:
    resp = await client.get(
        f"{VIYA_ENDPOINT}{ETAG_PATH.format(report_id=report_id)}",
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.headers.get("etag", "")


async def execute_operations(
    report_id: str,
    operations: list[dict[str, Any]],
    client: httpx.AsyncClient,
    response_format: str = "concise",
    retries: int = 1,
    result_report_name: str | None = None,
    result_folder: str | None = None,
    result_name_conflict: str = "rename",
) -> dict[str, Any]:
    """Apply a *validated* operations array with the ETag round-trip + 412 retry.

    Reads the report's current ETag, PUTs the operations with ``If-Match``, and
    on a 412 (a concurrent edit moved the ETag) transparently re-reads and
    retries once. HTTP >= 400 is surfaced as a structured ``apply_failed`` dict
    (with the per-operation failure details parsed out of the VA body) rather
    than raised; a missing report as ``not_found``.

    Passing ``result_report_name`` and/or ``result_folder`` switches the PUT to
    *save-as* mode: the operations are applied to a NEW report (HTTP 201) and
    the source report is left untouched — atomic template instantiation.
    """
    body: dict[str, Any] = {"operations": operations}
    save_as = bool(result_report_name or result_folder)
    if save_as:
        if result_report_name:
            body["resultReportName"] = result_report_name
        if result_folder:
            body["resultFolder"] = result_folder
        body["resultNameConflict"] = result_name_conflict
    payload = json.dumps(body).encode()
    url = f"{VIYA_ENDPOINT}{OPERATIONS_PATH.format(report_id=report_id)}"
    attempt = 0
    while True:
        try:
            etag = await _read_etag(report_id, client)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return {"status": "not_found", "report_id": report_id, "message": f"No report with id '{report_id}'."}
            raise
        resp = await client.put(
            url,
            content=payload,
            headers={
                "If-Match": etag,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        if resp.status_code == 412 and attempt < retries:
            attempt += 1
            continue
        break

    if resp.status_code >= 400:
        return {
            "status": "apply_failed",
            "report_id": report_id,
            "http_status": resp.status_code,
            "message": (
                f"Viya rejected the operations (HTTP {resp.status_code}). The whole batch is "
                f"atomic, so the report is unchanged. See failed_operations for the failing "
                f"index and Viya's reasons; fix those operations and resend the batch. "
                f"Viya said: {resp.text[:400] or '(no response body)'}"
            ),
            **parse_failure(resp.text),
        }

    data = _response_json_dict(resp)
    created = summarize_created(operations, data)
    result: dict[str, Any] = {
        "status": "applied",
        "report_id": report_id,
        "created": created,
    }
    verify_target: Any = report_id
    if save_as:
        result["saved_as"] = {
            "id": data.get("resultReportId"),
            "name": data.get("resultReportName") or result_report_name,
        }
        result["message"] = "Save-as: operations were applied to a NEW report; the source report is unchanged."
        verify_target = result["saved_as"]["id"] or report_id
        saved_url = viewer_url(result["saved_as"]["id"])
        if saved_url:
            result["saved_as"]["open_url"] = saved_url
    url = viewer_url(verify_target)
    if url:
        result["open_url"] = url
    hint = _verify_hint(verify_target, created)
    if hint:
        result["verify_hint"] = hint
    text_warning = await check_text_contents(verify_target, operations, created.get("objects", []), client)
    if text_warning:
        result["text_content_warning"] = text_warning
    if response_format == "detailed":
        result["response"] = data
    return result


# --- report outline (read-back) --------------------------------------------


def reduce_content_outline(content: dict[str, Any]) -> dict[str, Any]:
    """Reduce a BIRD report-content document to ``pages -> objects`` handles.

    Returns exactly what the authoring tools consume: per page the internal
    section name (``vi*``) and its label, and per object the visual-element
    name (``ve*`` — the relativeToObject/container/updateObject target), its
    label (the ``export_report`` handle), its type, and any text content.
    """
    elements: dict[str, dict[str, Any]] = {}
    for element in content.get("visualElements") or []:
        if isinstance(element, dict) and element.get("name"):
            elements[element["name"]] = element
    pages: list[dict[str, Any]] = []
    view = content.get("view")
    sections = view.get("sections") if isinstance(view, dict) else None
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        page: dict[str, Any] = {"name": section.get("name"), "label": section.get("label"), "objects": []}
        seen: set[str] = set()
        queue: list[tuple[str, str | None]] = []
        # Walk the whole section (header band + body) so header controls
        # appear in the outline too.
        _collect_refs(section, queue)
        while queue:
            ref, parent = queue.pop(0)
            if ref in seen:
                continue
            seen.add(ref)
            obj: dict[str, Any] = {"name": ref}
            element = elements.get(ref)
            if isinstance(element, dict):
                obj["type"] = element.get("@element")
                obj["label"] = element.get("labelAttribute")
                text = _text_content(element)
                if text:
                    obj["text"] = text
                # Containers hold their children in their own element entry.
                _collect_refs(element, queue, parent=ref)
            if parent:
                obj["container"] = parent
            page["objects"].append(obj)
        pages.append(page)
    return {"pages": pages}


def _collect_refs(node: Any, out: list[tuple[str, str | None]], parent: str | None = None) -> None:
    """Collect (ref, enclosing-container-ref) pairs from a layout tree, in order.

    Children of an implicit layout container (VA auto-creates one for geometric
    relativeToObject placements) are nested INSIDE the Container entry of the
    section body — the walk must descend through every entry, not just the top
    ``containedElementList`` — and carrying the enclosing ref lets the outline
    show which container each object sits in.
    """
    if isinstance(node, dict):
        ref = node.get("ref") if node.get("@element") in ("Visual", "Container") else None
        if ref:
            out.append((ref, parent))
        for value in node.values():
            _collect_refs(value, out, parent=ref or parent)
    elif isinstance(node, list):
        for value in node:
            _collect_refs(value, out, parent=parent)


def _text_content(element: dict[str, Any]) -> str | None:
    """Join the TextString fragments of a Text element's paragraphList."""
    texts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str):
                texts.append(text)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(element.get("paragraphList"))
    return " ".join(texts) if texts else None


async def execute_outline(report_id: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """GET the stored report definition and return the compact page/object outline."""
    resp = await client.get(
        f"{VIYA_ENDPOINT}{CONTENT_PATH.format(report_id=report_id)}",
        headers={"Accept": CONTENT_ACCEPT},
    )
    if resp.status_code == 404:
        return {"status": "not_found", "report_id": report_id, "message": f"No report with id '{report_id}'."}
    if resp.status_code >= 400:
        return {
            "status": "outline_failed",
            "report_id": report_id,
            "http_status": resp.status_code,
            "message": (
                f"Viya rejected the content read (HTTP {resp.status_code}). "
                f"Viya said: {resp.text[:400] or '(no response body)'}"
            ),
        }
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return {"status": "outline_failed", "report_id": report_id, "message": "content endpoint returned non-JSON."}
    outline = reduce_content_outline(data if isinstance(data, dict) else {})
    result = {
        "status": "ok",
        "report_id": report_id,
        **outline,
        "hint": (
            "Object 'name' (ve*) is the handle for relativeToObject/container placement and "
            "updateObject; 'label' is what export_report report_objects takes; a page's 'label' "
            "is the page-placement target; 'container' names the enclosing container element."
        ),
    }
    url = viewer_url(report_id)
    if url:
        result["open_url"] = url
    return result


def validate_copy(name: str | None, on_conflict: str) -> dict[str, Any] | None:
    """Validate a copy request (optional new name, conflict policy)."""
    if name is not None and not str(name).strip():
        return {"status": "invalid_request", "message": "copy_report name, if given, must be non-empty."}
    if on_conflict not in CONFLICT_VALUES:
        return {
            "status": "invalid_request",
            "message": f"on_conflict must be one of {sorted(CONFLICT_VALUES)}.",
            "on_conflict": on_conflict,
        }
    return None


def build_copy_body(name: str | None, folder: str | None, on_conflict: str) -> dict[str, Any]:
    """Build the PUT /visualAnalytics/reports/{id}/copy request body."""
    body: dict[str, Any] = {"resultNameConflict": on_conflict}
    if name:
        body["resultReportName"] = name
    if folder:
        body["resultFolder"] = folder
    return body


async def execute_copy(
    report_id: str,
    name: str | None,
    folder: str | None,
    on_conflict: str,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Copy a report and return ``{status, id, name, source_report_id}``.

    The copy endpoint mints a new report, so no ETag/If-Match is needed. HTTP
    errors surface as structured ``not_found`` / ``copy_failed`` dicts.
    """
    body = build_copy_body(name, folder, on_conflict)
    resp = await client.put(
        f"{VIYA_ENDPOINT}{COPY_PATH.format(report_id=report_id)}",
        content=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    if resp.status_code == 404:
        return {"status": "not_found", "report_id": report_id,
                "message": f"No report with id '{report_id}' to copy."}
    if resp.status_code >= 400:
        return {
            "status": "copy_failed",
            "source_report_id": report_id,
            "http_status": resp.status_code,
            "message": (
                f"Viya rejected the copy (HTTP {resp.status_code}). A name conflict policy "
                f"of '{on_conflict}' with an existing name can cause this. "
                f"Viya said: {resp.text[:400] or '(no response body)'}"
            ),
        }
    data = _response_json_dict(resp)
    copy_id = data.get("resultReportId") or data.get("id")
    result = {
        "status": "copied",
        "source_report_id": report_id,
        "id": copy_id,
        "name": data.get("resultReportName") or name,
    }
    url = viewer_url(copy_id)
    if url:
        result["open_url"] = url
    return result


async def execute_delete(report_id: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """Delete a report and its content, returning a structured result."""
    resp = await client.delete(f"{VIYA_ENDPOINT}{DELETE_PATH.format(report_id=report_id)}")
    if resp.status_code == 404:
        return {"status": "not_found", "report_id": report_id,
                "message": f"No report with id '{report_id}'."}
    if resp.status_code >= 400:
        return {
            "status": "delete_failed",
            "report_id": report_id,
            "http_status": resp.status_code,
            "message": (
                f"Viya rejected the delete (HTTP {resp.status_code}). "
                f"Viya said: {resp.text[:400] or '(no response body)'}"
            ),
        }
    return {"status": "deleted", "report_id": report_id}
