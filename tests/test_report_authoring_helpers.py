# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the report_authoring helpers — pure logic, no MCP or network.

Cover the object registry, the pre-flight operation validation, the discovery
describe(), tolerant normalisation, common-role warnings, and the request
shaping that back the create_report / apply_report_operations tools.
"""

from sas_mcp_server.helpers.report_authoring_helpers import (
    AGGREGATIONS,
    NOT_ADDABLE,
    OPERATION_KEYS,
    REPORT_OBJECT_TYPES,
    CreateReportRequest,
    build_copy_body,
    build_create_body,
    describe,
    normalize_operations,
    parse_failure,
    reduce_content_outline,
    summarize_created,
    validate_copy,
    validate_create,
    validate_operations,
    warn_operations,
)


def _add_object(type_key, data_roles=None, page="P1", data_source="CARS"):
    obj = {"dataSource": data_source}
    if data_roles is not None:
        obj["dataRoles"] = data_roles
    op = {"addObject": {"object": {type_key: obj}}}
    if page:
        op["addObject"]["placement"] = {"page": {"target": page}}
    return op


# --- registry integrity ---------------------------------------------------


def test_registry_has_all_addable_objects():
    # 64 objects reachable via addObject; linearRegression is update-only.
    addable = [o for o in REPORT_OBJECT_TYPES.values() if o.addable]
    assert len(addable) == 64
    assert REPORT_OBJECT_TYPES["linearRegression"].addable is False
    assert REPORT_OBJECT_TYPES["linearRegression"].updatable is True


def test_registry_add_update_asymmetries():
    assert REPORT_OBJECT_TYPES["geoCluster"].addable is True
    assert REPORT_OBJECT_TYPES["geoCluster"].updatable is False
    assert REPORT_OBJECT_TYPES["logisticRegression"].updatable is False


def test_not_addable_ui_objects_mapped():
    assert set(NOT_ADDABLE) == {
        "textTopics",
        "decisionTree",
        "generalizedAdditiveModel",
        "generalizedLinearModel",
    }
    assert NOT_ADDABLE["decisionTree"] == "gradientBoosting"


def test_multi_roles_flagged():
    bar = REPORT_OBJECT_TYPES["barChart"]
    measures = next(r for r in bar.roles if r.name == "measures")
    category = next(r for r in bar.roles if r.name == "category")
    assert measures.multi is True
    assert category.multi is False


# --- describe -------------------------------------------------------------


def test_describe_index_lists_operations_and_objects():
    idx = describe()
    assert {op["key"] for op in idx["operations"]} == set(OPERATION_KEYS)
    assert any(o["schema_key"] == "barChart" for o in idx["objects"])


def test_describe_category_filter():
    idx = describe(category="Geo Maps")
    cats = {o["category"] for o in idx["objects"]}
    assert cats == {"Geo Maps"}


def test_describe_object_detail_has_example_and_roles():
    detail = describe(object_type="scatterPlot")
    assert detail["schema_key"] == "scatterPlot"
    assert detail["commonly_required"] == ["measures"]
    assert "addObject" in detail["example"]


def test_describe_geo_precondition():
    detail = describe(object_type="geoRegion")
    assert "precondition" in detail


def test_describe_update_only_object_notes_it():
    detail = describe(object_type="linearRegression")
    assert detail["addable"] is False
    assert "note" in detail


def test_describe_not_addable_redirects():
    detail = describe(object_type="decisionTree")
    assert detail["status"] == "not_addable"
    assert detail["nearest"] == "gradientBoosting"


def test_describe_wrong_case_resolves():
    detail = describe(object_type="barchart")  # wrong case resolves via normalized lookup
    assert detail["schema_key"] == "barChart"
    assert detail["resolved_from_alias"] == "barchart"


def test_describe_unknown_suggests():
    detail = describe(object_type="barChrat")  # typo
    assert detail["status"] == "unknown_object_type"
    assert "barChart" in detail["did_you_mean"]


def test_describe_not_addable_ui_names_resolve():
    # The VA UI spellings ("Decision Tree", "Text Topics") must hit the
    # redirect too, not fall through to unknown_object_type.
    assert describe(object_type="decision tree")["status"] == "not_addable"
    assert describe(object_type="Text Topics")["nearest"] == "wordCloud"


def test_describe_index_has_purposes_intents_and_limits():
    idx = describe()
    key_value = next(o for o in idx["objects"] if o["schema_key"] == "keyValue")
    assert "KPI" in key_value["purpose"]
    assert "keyValue" in idx["intent_map"]["single KPI number"]
    assert any("header" in limit for limit in idx["limits"])


def test_describe_alias_resolves_to_object():
    detail = describe(object_type="kpi")
    assert detail["schema_key"] == "keyValue"
    assert detail["resolved_from_alias"] == "kpi"


def test_describe_operation_mode_returns_full_entry():
    entry = describe(operation="addData")
    assert entry["key"] == "addData"
    assert "dataItems" in entry["example"]["addData"]
    assert any("geography" in n for n in entry["notes"])
    unknown = describe(operation="removeObject")
    assert unknown["status"] == "unknown_operation"
    assert "addObject" in unknown["valid_operations"]


def test_describe_common_options_and_titled_example():
    detail = describe(object_type="barChart")
    assert "title" in detail["common_options"]["shape"]["options"]["object"]
    example_body = detail["example"]["addObject"]["object"]["barChart"]
    assert example_body["options"]["object"]["title"]


def test_describe_content_object_examples_are_real():
    text = describe(object_type="text")
    text_body = text["example"]["addObject"]["object"]["text"]
    assert "dataSource" not in text_body
    assert text_body["options"]["content"]
    assert "content_note" in text

    image = describe(object_type="image")
    image_body = image["example"]["addObject"]["object"]["image"]
    assert image_body["options"]["url"].startswith("https://")
    assert "options_note" in image

    container = describe(object_type="standardContainer")
    assert container["example"]["addObject"]["object"]["standardContainer"] == {}
    assert "no properties" in container["common_options"]["note"].lower() or (
        "no properties" in container["options_note"].lower()
    )


# --- validate_operations --------------------------------------------------


def test_validate_rejects_empty():
    err = validate_operations([])
    assert err["status"] == "invalid_operation"


def test_validate_rejects_unknown_operation_key():
    err = validate_operations([{"deleteObject": {}}])
    assert err["status"] == "invalid_operation"
    assert "deleteObject" in err["unknown_keys"]


def test_validate_rejects_two_ops_in_one_element():
    err = validate_operations([{"addPage": {}, "addData": {"cas": {"library": "L", "table": "T"}}}])
    assert err["status"] == "invalid_operation"


def test_validate_accepts_minimal_valid_batch():
    ops = [
        {"addData": {"cas": {"server": "cas-shared-default", "library": "Public", "table": "CARS"}}},
        {"addPage": {"pageName": "P1"}},
        _add_object("barChart", {"category": "Origin", "measures": ["MSRP"]}),
    ]
    assert validate_operations(ops) is None


def test_validate_adddata_requires_server():
    # Live Viya rejects addData without cas.server even though the published
    # spec marks it optional — catch it pre-flight with a pointer.
    err = validate_operations([{"addData": {"cas": {"library": "Public", "table": "CARS"}}}])
    assert err["status"] == "invalid_operation"
    assert "cas-shared-default" in err["message"]


def test_validate_unknown_object_type():
    err = validate_operations([_add_object("bubbleplot", {"xAxis": "a"})])
    assert err["status"] == "unknown_object_type"
    assert "bubblePlot" in err["did_you_mean"]


def test_validate_not_addable_object():
    err = validate_operations([_add_object("decisionTree")])
    assert err["status"] == "not_addable"
    assert err["nearest"] == "gradientBoosting"


def test_validate_add_only_object_rejected_for_add():
    err = validate_operations([_add_object("linearRegression", {"response": "y"})])
    assert err["status"] == "not_addable"


def test_validate_bad_role_name():
    err = validate_operations([_add_object("scatterPlot", {"category": "Make"})])
    assert err["status"] == "invalid_roles"
    assert "measures" in err["valid_roles"]


def test_validate_single_role_given_list():
    err = validate_operations([_add_object("barChart", {"category": ["a", "b"]})])
    assert err["status"] == "invalid_roles"


def test_validate_addobject_requires_object_or_reportobject():
    err = validate_operations([{"addObject": {}}])
    assert err["status"] == "invalid_operation"


def test_validate_addobject_rejects_both_object_and_reportobject():
    op = {"addObject": {"object": {"text": {}}, "reportObject": {"name": "x"}}}
    err = validate_operations([op])
    assert err["status"] == "invalid_operation"


def test_validate_reportobject_passthrough_ok():
    op = {"addObject": {"reportObject": {"name": "existingObj"}}}
    assert validate_operations([op]) is None


def test_validate_updateobject_needs_updatable_and_name():
    # geoCluster is add-only.
    err = validate_operations([{"updateObject": {"object": {"geoCluster": {"name": "g"}}}}])
    assert err["status"] == "not_updatable"
    # missing name
    err2 = validate_operations([{"updateObject": {"object": {"barChart": {}}}}])
    assert err2["status"] == "invalid_operation"
    # valid
    assert validate_operations([{"updateObject": {"object": {"barChart": {"name": "b1"}}}}]) is None


def test_validate_adddata_requires_library_and_table():
    err = validate_operations([{"addData": {"cas": {"library": "Public"}}}])
    assert err["status"] == "invalid_operation"


def test_validate_setparameter_requires_name_value():
    err = validate_operations([{"setParameterValue": {"name": "p"}}])
    assert err["status"] == "invalid_operation"
    assert validate_operations([{"setParameterValue": {"name": "p", "value": "v"}}]) is None


def test_validate_changedata_requires_both_sides():
    err = validate_operations([{"changeData": {"originalData": {"cas": {}}}}])
    assert err["status"] == "invalid_operation"


def test_validate_reports_op_index():
    ops = [{"addPage": {"pageName": "P"}}, _add_object("nope")]
    err = validate_operations(ops)
    assert err["op_index"] == 1


def test_validate_collects_all_errors():
    # The batch is atomic server-side, so all invalid operations are reported
    # at once instead of one per round-trip.
    ops = [_add_object("nope"), {"addPage": {"pageName": "P"}}, _add_object("scatterPlot", {"category": "x"})]
    err = validate_operations(ops)
    assert err["status"] == "unknown_object_type"  # first error keeps its shape
    assert err["error_count"] == 2
    assert [e["op_index"] for e in err["all_errors"]] == [0, 2]


def test_validate_rejects_object_name_at_add_time():
    op = _add_object("barChart", {"category": "Origin"})
    op["addObject"]["object"]["barChart"]["name"] = "myBar"
    err = validate_operations([op])
    assert err["status"] == "invalid_object_spec"
    assert "auto-named" in err["message"]


def test_validate_rejects_unknown_object_spec_key():
    op = _add_object("barChart", {"category": "Origin"})
    op["addObject"]["object"]["barChart"]["width"] = 400
    err = validate_operations([op])
    assert err["status"] == "invalid_object_spec"
    assert err["unknown_keys"] == ["width"]


def test_validate_standard_container_must_be_bare():
    # VA rejects ANY property on standardContainer at add time — even options.
    op = {"addObject": {"object": {"standardContainer": {"options": {"object": {"title": "KPIs"}}}}}}
    err = validate_operations([op])
    assert err["status"] == "invalid_object_spec"
    assert "updateObject" in err["message"]
    assert validate_operations([{"addObject": {"object": {"standardContainer": {}}}}]) is None


def test_validate_object_options_allowed_at_add_time():
    op = _add_object("barChart", {"category": "Origin"})
    op["addObject"]["object"]["barChart"]["options"] = {"object": {"title": "MSRP by Origin"}}
    assert validate_operations([op]) is None


def test_validate_updateobject_rejects_placement_and_dataroles():
    op = {"updateObject": {"object": {"barChart": {"name": "ve15", "dataRoles": {"category": "Origin"}}}}}
    err = validate_operations([op])
    assert err["status"] == "invalid_object_spec"
    assert err["unknown_keys"] == ["dataRoles"]


def test_validate_data_items_enums():
    good = {
        "addData": {
            "cas": {"server": "cas-shared-default", "library": "Public", "table": "SALES"},
            "dataItems": [
                {"dataItem": "Revenue", "properties": {"name": "Revenue (USD)", "format": "DOLLAR12.2",
                                                       "aggregation": "average"}},
                {"dataItem": "State", "properties": {"classification": "geography",
                                                     "geographyDataSource": {
                                                         "geographyNameCodeContext": "USStateNames"}}},
            ],
        }
    }
    assert validate_operations([good]) is None
    assert "average" in AGGREGATIONS

    bad_agg = {"addData": {"cas": {"server": "s", "library": "L", "table": "T"},
                           "dataItems": [{"dataItem": "x", "properties": {"aggregation": "mean"}}]}}
    err = validate_operations([bad_agg])
    assert err["status"] == "invalid_operation"
    assert "average" in err["valid_values"]

    orphan_geo = {"addData": {"cas": {"server": "s", "library": "L", "table": "T"},
                              "dataItems": [{"dataItem": "x", "properties": {
                                  "geographyDataSource": {"geographyNameCodeContext": "USStateNames"}}}]}}
    err = validate_operations([orphan_geo])
    assert "geography" in err["message"]

    bad_context = {"addData": {"cas": {"server": "s", "library": "L", "table": "T"},
                               "dataItems": [{"dataItem": "x", "properties": {
                                   "classification": "geography",
                                   "geographyDataSource": {"geographyNameCodeContext": "USStates"}}}]}}
    err = validate_operations([bad_context])
    assert "USStateNames" in err["valid_values"]


def _geo_item(geo_source):
    return {"addData": {"cas": {"server": "s", "library": "L", "table": "T"},
                        "dataItems": [{"dataItem": "x", "properties": {
                            "classification": "geography", "geographyDataSource": geo_source}}]}}


def test_validate_geography_coordinates():
    # Raw lat/long point data — the shape the spec calls geographyCoordinates.
    good = _geo_item({"geographyCoordinates": {"latitudeDataItem": "LAT", "longitudeDataItem": "LON"}})
    assert validate_operations([good]) is None
    # The wrong key an agent actually guessed live must fail fast, with the fix.
    guessed = _geo_item({"customCoordinates": {"latitudeDataItem": "LAT", "longitudeDataItem": "LON"}})
    err = validate_operations([guessed])
    assert err["status"] == "invalid_operation"
    assert "geographyCoordinates" in err["message"]
    assert "geographyCoordinates" in err["valid_keys"]
    # coordinates need both columns
    half = _geo_item({"geographyCoordinates": {"latitudeDataItem": "LAT"}})
    assert "longitudeDataItem" in validate_operations([half])["message"]
    # context and coordinates are mutually exclusive
    both = _geo_item({"geographyNameCodeContext": "USStateNames",
                      "geographyCoordinates": {"latitudeDataItem": "LAT", "longitudeDataItem": "LON"}})
    assert "not both" in validate_operations([both])["message"]


def test_validate_rejects_bare_numeric_format():
    # "8.1" passed pre-flight live and killed the whole atomic batch server-side.
    bad = {"addData": {"cas": {"server": "s", "library": "L", "table": "T"},
                       "dataItems": [{"dataItem": "x", "properties": {"format": "8.1"}}]}}
    err = validate_operations([bad])
    assert err["status"] == "invalid_operation"
    assert "DOLLAR12.2" in err["message"]
    for fmt in ("DOLLAR12.2", "COMMA10.", "PERCENT8.1", "DATE9.", "$CHAR20."):
        good = {"addData": {"cas": {"server": "s", "library": "L", "table": "T"},
                            "dataItems": [{"dataItem": "x", "properties": {"format": fmt}}]}}
        assert validate_operations([good]) is None, fmt


def test_warn_render_required_roles():
    # Category-only charts are ACCEPTED but render empty — warn pre-flight.
    warnings = warn_operations([_add_object("barChart", {"category": "Origin"})])
    assert any("renders EMPTY" in w for w in warnings)
    # frequency satisfies the measures group
    assert warn_operations([_add_object("barChart", {"category": "Origin", "frequency": "N"})]) == []
    assert warn_operations([_add_object("barChart", {"category": "Origin", "measures": ["MSRP"]})]) == []
    word_only = {"addObject": {"object": {"wordCloud": {"dataSource": "T", "dataRoles": {"word": "EVENT"}}}}}
    assert any("renders EMPTY" in w for w in warn_operations([word_only]))


def test_warn_multiple_content_texts():
    text = {"addObject": {"object": {"text": {"options": {"content": "A"}}}}}
    text2 = {"addObject": {"object": {"text": {"options": {"content": "B"}}}}}
    warnings = warn_operations([text, text2])
    assert any("misroute" in w for w in warnings)
    assert warn_operations([text]) == []


def test_warn_page_placed_stack():
    # N merely page-placed objects auto-flow into one vertical stack — the
    # single ugliest failure mode observed; warn with the structural fix.
    stacked = [
        _add_object("barChart", {"category": "C", "measures": ["M"]}, page="Overview") for _ in range(4)
    ]
    warnings = warn_operations(stacked)
    assert any("VERTICALLY" in w and "standardContainer" in w for w in warnings)
    # Structured placements (container / relativeToObject) do not count.
    tiles = [
        {"addObject": {"object": {"keyValue": {"dataSource": "T", "dataRoles": {"measure": "M"}}},
                       "placement": {"container": {"target": "c1"}}}}
        for _ in range(4)
    ]
    assert not any("VERTICALLY" in w for w in warn_operations(tiles))
    # Three on one page is fine.
    assert not any("VERTICALLY" in w for w in warn_operations(stacked[:3]))


def test_expected_text_pairs_skip_contentless_texts():
    # A content-less text op must consume its created record WITHOUT shifting
    # the pairing — two independently filtered lists produced false misroute
    # warnings when an empty text preceded a content-bearing one.
    from sas_mcp_server.helpers.report_authoring_helpers import _expected_text_pairs

    ops = [
        {"addObject": {"object": {"text": {"options": {}}}}},
        {"addObject": {"object": {"text": {"options": {"content": "Intended Title"}}}}},
    ]
    created = [{"type": "text", "name": "ve1"}, {"type": "text", "name": "ve2"}]
    assert _expected_text_pairs(ops, created) == [("ve2", "Intended Title")]
    # Non-string content is skipped the same way.
    ops[0]["addObject"]["object"]["text"]["options"]["content"] = 123
    assert _expected_text_pairs(ops, created) == [("ve2", "Intended Title")]


def test_validate_geography_source_must_be_object():
    err = validate_operations([_geo_item("USStateNames")])
    assert err["status"] == "invalid_operation"
    assert "must be an object" in err["message"]


def test_describe_key_value_has_no_title_in_example():
    detail = describe(object_type="keyValue")
    body = detail["example"]["addObject"]["object"]["keyValue"]
    assert "options" not in body  # the measure label IS the tile's headline
    assert "title_note" in detail
    bar = describe(object_type="barChart")["example"]["addObject"]["object"]["barChart"]
    assert bar["options"]["object"]["title"]  # other visuals stay titled


# --- normalize ------------------------------------------------------------


def test_normalize_coerces_pageposition_int_to_str():
    ops = normalize_operations([{"addPage": {"pageName": "P", "pagePosition": 0}}])
    assert ops[0]["addPage"]["pagePosition"] == "0"


def test_normalize_wraps_scalar_multi_role():
    ops = normalize_operations([_add_object("barChart", {"category": "Origin", "measures": "MSRP"})])
    roles = ops[0]["addObject"]["object"]["barChart"]["dataRoles"]
    assert roles["measures"] == ["MSRP"]
    assert roles["category"] == "Origin"  # single role left as-is


def test_normalize_does_not_mutate_input():
    original = [{"addPage": {"pageName": "P", "pagePosition": 3}}]
    normalize_operations(original)
    assert original[0]["addPage"]["pagePosition"] == 3


# --- page titles ----------------------------------------------------------


def test_page_title_expands_to_body_text_band():
    # VA rejects text in page headers ("Only control objects are supported in
    # the page header"), so the title must land at the top of the page body.
    ops = normalize_operations([{"addPage": {"pageName": "Overview", "title": "Sales Overview"}}])
    assert len(ops) == 2
    assert "title" not in ops[0]["addPage"]  # stripped from the page op
    assert ops[0]["addPage"]["pageName"] == "Overview"
    text = ops[1]["addObject"]
    assert text["object"]["text"]["options"]["content"] == "Sales Overview"
    assert text["placement"]["page"] == {"target": "Overview", "context": "body", "position": "start"}


def test_page_title_survives_when_no_pagename_then_fails_validation():
    ops = normalize_operations([{"addPage": {"title": "Orphan"}}])
    assert len(ops) == 1  # not expanded — no page to target
    err = validate_operations(ops)
    assert err["status"] == "invalid_operation"
    assert "pageName" in err["message"]


def test_page_title_batch_is_valid_end_to_end():
    ops = normalize_operations(
        [
            {"addPage": {"pageName": "Overview", "title": "Sales Overview"}},
            _add_object("barChart", {"category": "Origin", "measures": ["MSRP"]}, page="Overview"),
        ]
    )
    assert validate_operations(ops) is None
    assert len(ops) == 3  # addPage + barChart + synthesized title text


def test_page_title_expansion_preserves_caller_indices():
    # Synthesized title ops go at the END so op_index in errors (and VA's
    # failed-at-index messages) keep matching the caller's operations array.
    ops = normalize_operations(
        [
            {"addPage": {"pageName": "A", "title": "T"}},
            _add_object("nope"),
        ]
    )
    assert ops[2]["addObject"]["object"]["text"]["options"]["content"] == "T"
    err = validate_operations(ops)
    assert err["op_index"] == 1  # the caller's failing element, unshifted


def test_falsy_page_title_is_stripped_not_expanded():
    # An empty title must not leak the tool-only 'title' key to VA (which
    # rejects unknown properties), and must not create an empty text band.
    ops = normalize_operations([{"addPage": {"pageName": "P", "title": ""}}])
    assert len(ops) == 1
    assert "title" not in ops[0]["addPage"]
    assert validate_operations(ops) is None


def test_bool_pageposition_rejected_not_stringified():
    ops = normalize_operations([{"addPage": {"pageName": "P", "pagePosition": True}}])
    assert ops[0]["addPage"]["pagePosition"] is True  # bool must not become "True"
    assert validate_operations(ops)["status"] == "invalid_operation"


# --- placement ------------------------------------------------------------


def _placed(placement):
    op = _add_object("barChart", {"category": "Origin"}, page=None)
    op["addObject"]["placement"] = placement
    return op


def test_placement_relative_positions_valid():
    for pos in ("left", "right", "top", "bottom", "before", "after"):
        op = _placed({"relativeToObject": {"target": "bar1", "position": pos}})
        assert validate_operations([op]) is None, pos


def test_placement_container_and_report_valid():
    assert validate_operations([_placed({"container": {"target": "c1", "position": "end"}})]) is None
    assert validate_operations([_placed({"report": {"context": "new_page"}})]) is None


def test_placement_header_is_controls_only():
    # VA: "Only control objects are supported in the page header" — enforce it
    # pre-flight for both the page and report header bands.
    control = {
        "addObject": {
            "object": {"dropdownList": {"dataSource": "CARS", "dataRoles": {"category": "Origin"}}},
            "placement": {"page": {"target": "P", "context": "header"}},
        }
    }
    assert validate_operations([control]) is None
    err = validate_operations([_placed({"page": {"target": "P", "context": "header"}})])
    assert err["status"] == "invalid_placement"
    assert "control objects" in err["message"]
    err2 = validate_operations([_placed({"report": {"context": "header"}})])
    assert err2["status"] == "invalid_placement"


def test_placement_report_new_page_with_name_and_position():
    ok = _placed({"report": {"context": "new_page", "pageName": "Trends", "pagePosition": 0}})
    assert validate_operations([ok]) is None
    # pagePosition is a NUMBER in report placement (string only on addPage).
    bad_pos = _placed({"report": {"context": "new_page", "pagePosition": "0"}})
    err = validate_operations([bad_pos])
    assert err["status"] == "invalid_placement"
    assert "NUMBER" in err["message"]
    bad_name = _placed({"report": {"context": "new_page", "pageName": "  "}})
    assert validate_operations([bad_name])["status"] == "invalid_placement"


def test_normalize_translates_report_placement_spellings():
    # The published spec says "newPage" but the live enum is snake_case; a
    # digit-string pagePosition is coerced to the number the API expects.
    op = _placed({"report": {"context": "newPage", "pagePosition": "1"}})
    ops = normalize_operations([op])
    report = ops[0]["addObject"]["placement"]["report"]
    assert report["context"] == "new_page"
    assert report["pagePosition"] == 1
    assert validate_operations(ops) is None


def test_placement_unknown_inner_key_rejected():
    err = validate_operations([_placed({"page": {"target": "P", "size": "50%"}})])
    assert err["status"] == "invalid_placement"
    assert err["unknown_keys"] == ["size"]


def test_normalize_malformed_report_pageposition_no_crash():
    # normalize must never raise; the typed validation message handles it.
    op = _placed({"report": {"context": "new_page", "pagePosition": "--1"}})
    ops = normalize_operations([op])
    assert ops[0]["addObject"]["placement"]["report"]["pagePosition"] == "--1"
    assert validate_operations(ops)["status"] == "invalid_placement"


def test_validate_rejects_stray_sibling_keys():
    err = validate_operations([{"addPage": {"pageName": "P"}, "comment": "x"}])
    assert err["status"] == "invalid_operation"
    assert err["unknown_keys"] == ["comment"]
    # The documented meta keys stay allowed.
    assert validate_operations([{"addPage": {"pageName": "P"}, "operationId": "op1"}]) is None


def test_validate_reportobject_placement_checked():
    op = {"addObject": {"reportObject": {"name": "x"}, "placement": {"grid": {}}}}
    assert validate_operations([op])["status"] == "invalid_placement"


def test_validate_updatedata_requires_data():
    assert validate_operations([{"updateData": {}}])["status"] == "invalid_operation"
    assert validate_operations([{"updateData": {"data": {"name": "CARS"}}}]) is None


def test_placement_unknown_variant():
    err = validate_operations([_placed({"grid": {"row": 1}})])
    assert err["status"] == "invalid_placement"
    assert "page" in err["valid_variants"]


def test_placement_missing_target():
    err = validate_operations([_placed({"relativeToObject": {"position": "right"}})])
    assert err["status"] == "invalid_placement"
    assert "target" in err["message"]


def test_placement_bad_position_enum():
    err = validate_operations([_placed({"relativeToObject": {"target": "bar1", "position": "below"}})])
    assert err["status"] == "invalid_placement"
    assert "bottom" in err["valid_values"]


def test_placement_two_variants_rejected():
    err = validate_operations([_placed({"page": {"target": "P"}, "container": {"target": "c"}})])
    assert err["status"] == "invalid_placement"


def test_describe_index_exposes_placement_and_recipes():
    idx = describe()
    variants = {p["variant"] for p in idx["placement"]}
    assert variants == {"page", "relativeToObject", "container", "report"}
    assert any("title" in r for r in idx["layout_recipes"])


# --- warnings -------------------------------------------------------------


def test_warn_missing_common_role():
    warnings = warn_operations([_add_object("keyValue", {"latticeCategory": "Origin"})])
    assert warnings and "measure" in warnings[0]


def test_no_warning_when_common_roles_present():
    warnings = warn_operations([_add_object("keyValue", {"measure": "MSRP"})])
    assert warnings == []


# --- create request shaping ----------------------------------------------


def test_validate_create_rejects_blank_name():
    assert validate_create(CreateReportRequest(name="  "))["status"] == "invalid_request"


def test_validate_create_rejects_bad_conflict():
    err = validate_create(CreateReportRequest(name="R", on_conflict="merge"))
    assert err["status"] == "invalid_request"


def test_validate_create_checks_inline_operations():
    err = validate_create(CreateReportRequest(name="R", operations=[_add_object("decisionTree")]))
    assert err["status"] == "not_addable"


def test_build_create_body_defaults_and_folder():
    body = build_create_body(CreateReportRequest(name="Cars", folder="/f/abc"))
    assert body["resultReportName"] == "Cars"
    assert body["resultNameConflict"] == "rename"
    assert body["resultFolder"] == "/f/abc"
    assert "operations" not in body


# --- copy request shaping -------------------------------------------------


def test_validate_copy():
    assert validate_copy("Copy", "rename") is None
    assert validate_copy(None, "abort") is None  # name optional for a copy
    assert validate_copy("  ", "rename")["status"] == "invalid_request"
    assert validate_copy("Copy", "merge")["status"] == "invalid_request"


def test_build_copy_body_omits_unset():
    assert build_copy_body(None, None, "abort") == {"resultNameConflict": "abort"}
    body = build_copy_body("New", "/f/x", "rename")
    assert body == {"resultNameConflict": "rename", "resultReportName": "New", "resultFolder": "/f/x"}


# --- summarize ------------------------------------------------------------


def test_summarize_created_reads_operations():
    ops = [
        {"addData": {"cas": {"library": "Public", "table": "CARS"}}},
        {"addPage": {"pageName": "Overview"}},
        _add_object("barChart", {"category": "Origin", "measures": ["MSRP"]}, page="Overview"),
    ]
    summary = summarize_created(ops, {})
    assert summary["pages"][0]["label"] == "Overview"
    assert summary["dataSources"] == [{"name": "CARS"}]
    assert summary["objects"][0]["type"] == "barChart"
    assert summary["objects"][0]["page"] == "Overview"


def test_summarize_merges_response_names():
    ops = [_add_object("barChart", {"category": "Origin"}, page="P")]
    response = {"operationResponses": [{"name": "bar1"}]}
    summary = summarize_created(ops, response)
    assert summary["objects"][0]["name"] == "bar1"


def test_summarize_merges_by_index_in_mixed_batches():
    # The VA response `operations` array is 1:1 index-aligned with the request;
    # a page/data name must never be attributed to an object (the old zip bug).
    ops = [
        {"addData": {"cas": {"library": "Public", "table": "CARS"}}},
        {"addPage": {"pageName": "Overview"}},
        _add_object("barChart", {"category": "Origin"}, page="Overview"),
    ]
    response = {
        "operations": [
            {"name": "ds7", "status": "Success"},
            {"name": "vi70", "label": "Overview", "status": "Success"},
            {"name": "ve15", "label": "Bar - Origin 1", "status": "Success"},
        ]
    }
    summary = summarize_created(ops, response)
    assert summary["objects"][0]["name"] == "ve15"
    assert summary["objects"][0]["label"] == "Bar - Origin 1"
    assert summary["pages"][0]["name"] == "vi70"
    assert summary["pages"][0]["label"] == "Overview"
    # The requested data-source handle (what addObject.dataSource references)
    # is preserved rather than replaced by the internal ds* name.
    assert summary["dataSources"][0]["name"] == "CARS"


def test_summarize_keeps_report_placement_page():
    op = {
        "addObject": {
            "object": {"text": {"options": {"content": "hi"}}},
            "placement": {"report": {"context": "new_page", "pageName": "Trends"}},
        }
    }
    summary = summarize_created([op], {})
    assert summary["objects"][0]["page"] == "Trends"
    assert summary["objects"][0]["placement"]["report"]["pageName"] == "Trends"


def test_summarize_fills_unnamed_page_from_response():
    # A pre-seeded None (unnamed addPage) must not block the response label —
    # the verify hint depends on a real, export-usable label.
    summary = summarize_created(
        [{"addPage": {}}], {"operations": [{"name": "vi6", "label": "Page 2", "status": "Success"}]}
    )
    assert summary["pages"][0]["label"] == "Page 2"
    assert summary["pages"][0]["name"] == "vi6"


def test_parse_failure_extracts_failing_index():
    body = (
        '{"operations": [{"name": "vi7", "label": "P", "status": "Success"},'
        '{"status": "Failed", "messages": ["The target was not found in the report: veX",'
        '"Unable to add the object to the report"]}],'
        '"status": "Failed", "messages": ["Operation addObject failed at index 1"]}'
    )
    parsed = parse_failure(body)
    assert parsed["failed_operation_index"] == 1
    assert parsed["failed_operations"][0]["messages"][0].startswith("The target was not found")
    assert parsed["viya_messages"] == ["Operation addObject failed at index 1"]
    assert parse_failure("not json") == {}


# --- outline reduction ------------------------------------------------------


def test_reduce_content_outline_pages_objects_and_text():
    content = {
        "visualElements": [
            {"@element": "Text", "name": "ve9", "labelAttribute": "Text 1",
             "paragraphList": [{"@element": "P", "elements": [
                 {"@element": "Span", "elements": [{"@element": "TextString", "text": "Sales Overview"}]}]}]},
            {"@element": "Graph", "name": "ve15", "labelAttribute": "Bar - Origin 1"},
            {"@element": "VisualContainer", "name": "ve20", "labelAttribute": "Standard container 1",
             "layout": {"containedElementList": [{"@element": "Visual", "ref": "ve21"}]}},
            {"@element": "Graph", "name": "ve21", "labelAttribute": "Key value 1"},
            {"@element": "DropDown", "name": "ve30", "labelAttribute": "Drop-down list 1"},
        ],
        "view": {
            "sections": [
                {
                    "name": "vi7",
                    "label": "Overview",
                    "header": {"containedElementList": [{"@element": "Visual", "ref": "ve30"}]},
                    "body": {"containedElementList": [
                        {"@element": "Visual", "ref": "ve9"},
                        {"@element": "Visual", "ref": "ve15"},
                        {"@element": "Container", "ref": "ve20"},
                    ]},
                },
                {"name": "vi30", "label": "Empty"},
            ]
        },
    }
    outline = reduce_content_outline(content)
    page = outline["pages"][0]
    assert (page["name"], page["label"]) == ("vi7", "Overview")
    names = [o["name"] for o in page["objects"]]
    # Header controls and container children are both included, in order.
    assert names == ["ve30", "ve9", "ve15", "ve20", "ve21"]
    text = page["objects"][1]
    assert text["type"] == "Text"
    assert text["text"] == "Sales Overview"
    assert page["objects"][2]["label"] == "Bar - Origin 1"
    # Container membership is exposed so layout is verifiable without a render.
    kv = next(o for o in page["objects"] if o["name"] == "ve21")
    assert kv["container"] == "ve20"
    assert "container" not in page["objects"][0]  # header control sits at top level
    assert outline["pages"][1] == {"name": "vi30", "label": "Empty", "objects": []}


def test_reduce_content_outline_tolerates_malformed_content():
    assert reduce_content_outline({}) == {"pages": []}
    assert reduce_content_outline({"view": "bogus"}) == {"pages": []}
    assert reduce_content_outline({"view": {"sections": ["junk", None]}}) == {"pages": []}
