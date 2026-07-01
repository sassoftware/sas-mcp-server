# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the report_export helpers — pure logic, no MCP or network.

These exercise the validation and query-param building that back the
``export_report`` tool, which is the reusability win of the helpers split.
"""

from sas_mcp_server.helpers.report_export_helpers import (
    REPORT_EXPORT_FORMATS,
    ReportExportRequest,
    build_export_params,
    validate_export_request,
)


def _req(**kw):
    kw.setdefault("report_id", "rep1")
    return ReportExportRequest(**kw)


# --- request normalisation ------------------------------------------------


def test_request_normalises_none_objects():
    assert _req(export_format="summary", report_objects=None).report_objects == []


def test_request_normalises_string_object():
    assert _req(export_format="csv", report_objects="ve1").report_objects == ["ve1"]


# --- validation -----------------------------------------------------------


def test_validate_unsupported_format():
    err = validate_export_request(_req(export_format="docx"))
    assert err["status"] == "unsupported_format"
    assert "package" in err["supported"]


def test_validate_summary_rejects_objects():
    err = validate_export_request(_req(export_format="summary", report_objects=["ve1"]))
    assert err["status"] == "invalid_request"


def test_validate_data_requires_object():
    err = validate_export_request(_req(export_format="csv"))
    assert err["status"] == "invalid_request"


def test_validate_data_rejects_multiple_objects():
    err = validate_export_request(_req(export_format="xlsx", report_objects=["a", "b"]))
    assert err["status"] == "invalid_request"


def test_validate_image_requires_size():
    err = validate_export_request(_req(export_format="png", report_objects=["ve1"]))
    assert err["status"] == "invalid_request"


def test_validate_accepts_valid_requests():
    assert validate_export_request(_req(export_format="package")) is None
    assert validate_export_request(
        _req(export_format="package", report_objects=["a", "b"])
    ) is None
    assert validate_export_request(
        _req(export_format="png", report_objects=["ve1"], image_size="800px,600px")
    ) is None
    assert validate_export_request(_req(export_format="csv", report_objects=["ve1"])) is None


# --- param building -------------------------------------------------------


def test_params_multi_object_comma_joined():
    fmt = REPORT_EXPORT_FORMATS["package"]
    params = build_export_params(fmt, _req(export_format="package", report_objects=["a", "b"]))
    assert params == {"reportObjects": "a,b"}


def test_params_single_object_uses_singular_key():
    fmt = REPORT_EXPORT_FORMATS["csv"]
    params = build_export_params(fmt, _req(export_format="csv", report_objects=["ve1"]))
    assert params == {"reportObject": "ve1"}


def test_params_image_includes_size():
    fmt = REPORT_EXPORT_FORMATS["png"]
    params = build_export_params(
        fmt, _req(export_format="png", report_objects=["ve1"], image_size="800px,600px")
    )
    assert params["reportObject"] == "ve1"
    assert params["size"] == "800px,600px"


def test_params_pdf_passes_options_as_strings():
    fmt = REPORT_EXPORT_FORMATS["pdf"]
    params = build_export_params(
        fmt, _req(export_format="pdf", options={"orientation": "landscape", "includeCoverPage": True})
    )
    assert params["orientation"] == "landscape"
    assert params["includeCoverPage"] == "True"


def test_params_summary_has_none():
    fmt = REPORT_EXPORT_FORMATS["summary"]
    assert build_export_params(fmt, _req(export_format="summary")) == {}
