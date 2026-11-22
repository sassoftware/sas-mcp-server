# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Scoring a published decision: module naming, links back to the decision, the view."""

from __future__ import annotations

import importlib
import json
import re
import sys
from unittest.mock import patch

import httpx
import pytest
from fastmcp import Client, FastMCP

from conftest import _make_mock_response
from sas_mcp_server import tools, ui
from sas_mcp_server.config import VIYA_ENDPOINT
from sas_mcp_server.exceptions import ConfigError
from sas_mcp_server.helpers.viya_links import decision_from_uri, decision_url
from sas_mcp_server.tools.decisioning import module_name, name_module

# The shape of MAS code generated for a decision: one quoted package per rule
# set, then the decision's own package, named after the revision, whose name
# also appears in the generated log messages.
MAS_CODE = '''ds2_options scond=WARNING;
  package "Loanu32Preu45Approval_Loanu32Affordability_1_0"  / inline;
    method execute(double "annual_income", in_out double "debt_to_income");
    end;
  endpackage;
/* {PACKAGE_START} */
package Decision_48a29267_abb2_40d1_b1d  / inline;
    dcl package "Loanu32Preu45Approval_Loanu32Affordability_1_0" "aff_hdl"();
    method execute_internal(double "annual_income", in_out double "debt_to_income");
        logr_perf.log( 'd', 'Package Name: Decision_48a29267_abb2_40d1_b1d, TOTAL DURATION: ' .. t);
    end;
    method execute(double "annual_income", in_out double "debt_to_income");
        execute_internal("annual_income", "debt_to_income");
    end;
/* {PACKAGE_END} */
  endpackage;
'''


# --- module names ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("publish_name", "expected"),
    [
        ("Loan_Pre_Approval", "Loan_Pre_Approval"),
        ("Loan Pre-Approval", "Loan_Pre_Approval"),
        ("  Credit  risk (v2) ", "Credit_risk_v2"),
        ("_private", "private"),
        ("123_decision", None),
        ("---", None),
    ],
)
def test_module_name_follows_the_publish_name(publish_name, expected):
    assert module_name(publish_name) == expected


def test_name_module_renames_the_decision_package_and_its_log_lines():
    code, name = name_module(MAS_CODE, "Loan Pre-Approval")
    assert name == "Loan_Pre_Approval"
    assert "package Loan_Pre_Approval  / inline;" in code
    assert "Decision_48a29267_abb2_40d1_b1d" not in code
    assert "Package Name: Loan_Pre_Approval," in code
    # The rule set's package and the handle declared from it are untouched.
    assert code.count('"Loanu32Preu45Approval_Loanu32Affordability_1_0"') == 2


def test_name_module_leaves_the_code_alone_when_it_cannot_rename():
    assert name_module(MAS_CODE, "123") == (MAS_CODE, None)
    clash = "Loanu32Preu45Approval_Loanu32Affordability_1_0"
    assert name_module(MAS_CODE, clash) == (MAS_CODE, None)
    assert name_module("data _null_; run;", "Loan") == ("data _null_; run;", None)


def test_name_module_renames_a_quoted_decision_package_by_its_declaration():
    code = 'package "Old Name" / inline;\n  method execute(); end;\nendpackage;\n'
    renamed, name = name_module(code, "New")
    assert name == "New"
    assert renamed.startswith("package New / inline;")


@pytest.mark.parametrize(("replace", "params"), [(False, None), (True, {"reuseExistingModules": "true"})])
async def test_publish_replaces_a_module_only_when_asked(mcp_server_with_mock_client, replace, params):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.side_effect = [
        _make_mock_response(text=MAS_CODE),
        _make_mock_response({"state": "completed", "moduleId": "loan_pre_approval"}),
    ]
    mock_client.post.return_value = _make_mock_response({"items": [{
        "id": "pub-1", "properties": {"masModules": [{"jobUri": "/microanalyticScore/jobs/job-1"}]},
    }]})
    async with Client(mcp) as client:
        result = (await client.call_tool("publish_decision_flow", {
            "decision_id": "dec-1", "revision_id": "rev-1", "publish_name": "Loan_Pre_Approval",
            "replace_existing": replace,
        })).data
    assert mock_client.post.call_args.kwargs["params"] == params
    sent = json.loads(mock_client.post.call_args.kwargs["content"])
    assert "package Loan_Pre_Approval  / inline;" in sent["modelContents"][0]["code"]
    assert result["moduleId"] == "loan_pre_approval"


# --- links ------------------------------------------------------------------------


def test_decision_links():
    assert decision_url("dec-1") == f"{VIYA_ENDPOINT}/SASDecisionManager/decisions/dec-1"
    assert decision_url("") is None
    assert decision_from_uri("/decisions/flows/f8a2/revisions/48a2") == ("f8a2", "48a2")
    assert decision_from_uri("/decisions/flows/f8a2") == ("f8a2", None)
    assert decision_from_uri("/modelRepository/models/m-1") is None
    assert decision_from_uri(None) is None


async def test_decision_flow_tools_carry_the_link(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.return_value = _make_mock_response(
        {"items": [{"id": "dec-1", "name": "Loan Pre-Approval", "majorRevision": 1}], "count": 1}
    )
    async with Client(mcp) as client:
        flows = (await client.call_tool("list_decision_flows", {"filter_name": "Loan"})).data
    assert flows[0]["open_url"] == decision_url("dec-1")

    mock_client.get.return_value = _make_mock_response({"id": "dec-1", "name": "Loan Pre-Approval"})
    async with Client(mcp) as client:
        flow = (await client.call_tool("get_decision_flow", {"decision_id": "dec-1"})).data
    assert flow["open_url"] == decision_url("dec-1")
    assert flow["name"] == "Loan Pre-Approval"


# --- the signature names the module and the decision behind it ---------------------


_MODULE = {
    "id": "loan_pre_approval",
    "name": "Loan_Pre_Approval",
    "properties": [{"name": "sourceURI", "value": "/decisions/flows/dec-1/revisions/rev-1"}],
}
_STEP = {
    "id": "execute",
    "moduleId": "loan_pre_approval",
    "inputs": [{"name": "credit_score", "type": "decimal", "dim": 0, "size": 0}],
    "outputs": [{"name": "outcome", "type": "string", "dim": 0, "size": 0}],
}


async def test_signature_names_the_module_and_its_decision(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.side_effect = [
        _make_mock_response(_MODULE),
        _make_mock_response({"id": "dec-1", "name": "Loan Pre-Approval"}),
        _make_mock_response(_STEP),
    ]
    async with Client(mcp) as client:
        result = (await client.call_tool(
            "get_mas_module_step_signature", {"module_id": "loan_pre_approval"}
        )).data

    urls = [c.args[0] for c in mock_client.get.call_args_list]
    assert urls == [
        f"{VIYA_ENDPOINT}/microanalyticScore/modules/loan_pre_approval",
        f"{VIYA_ENDPOINT}/decisions/flows/dec-1",
        f"{VIYA_ENDPOINT}/microanalyticScore/modules/loan_pre_approval/steps/execute",
    ]
    assert result["inputs"] == _STEP["inputs"]
    assert result["module"] == {
        "id": "loan_pre_approval",
        "name": "Loan_Pre_Approval",
        "source_uri": "/decisions/flows/dec-1/revisions/rev-1",
    }
    assert result["decision"] == {
        "id": "dec-1",
        "revision_id": "rev-1",
        "name": "Loan Pre-Approval",
        "open_url": decision_url("dec-1"),
    }


async def test_signature_keeps_the_link_when_the_decision_cannot_be_read(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    missing = httpx.Response(
        404, request=httpx.Request("GET", f"{VIYA_ENDPOINT}/decisions/flows/dec-1"), json={"message": "gone"}
    )
    mock_client.get.side_effect = [_make_mock_response(_MODULE), missing, _make_mock_response(_STEP)]
    async with Client(mcp) as client:
        result = (await client.call_tool(
            "get_mas_module_step_signature", {"module_id": "loan_pre_approval"}
        )).data
    assert result["decision"] == {"id": "dec-1", "revision_id": "rev-1", "open_url": decision_url("dec-1")}


async def test_signature_of_a_model_module_has_no_decision(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    source = {"name": "sourceURI", "value": "/modelRepository/models/m-1"}
    module = {"id": "hmeq", "name": "HMEQ", "properties": [source]}
    mock_client.get.side_effect = [_make_mock_response(module), _make_mock_response(_STEP)]
    async with Client(mcp) as client:
        result = (await client.call_tool("get_mas_module_step_signature", {"module_id": "hmeq"})).data
    assert "decision" not in result
    assert result["module"]["source_uri"] == "/modelRepository/models/m-1"
    assert len(mock_client.get.call_args_list) == 2


# --- the view -----------------------------------------------------------------------


async def _stamp(**kwargs) -> tuple[set[str], set[str]]:
    mcp = FastMCP("ui-test")

    async def get_token(ctx):
        return "t"

    tools.register_tools(mcp, get_token, apps=True, **kwargs)
    async with Client(mcp) as client:
        uris = {str(r.uri) for r in await client.list_resources()}
        text = (await client.read_resource(ui.resource_uri("get_mas_module_step_signature")))[0].text
    stamp = re.search(r"globalThis\.SAS_VIEW = (\{.*?\});", text, re.S)
    assert stamp
    return uris, set(json.loads(stamp.group(1))["can"])


def test_both_scoring_tools_render_the_scoring_view():
    assert ui.VIEW_FOR_TOOL["get_mas_module_step_signature"].key == "decision-score"
    assert ui.VIEW_FOR_TOOL["score_data"].key == "decision-score"


async def test_read_only_keeps_the_form_but_not_the_score_button():
    """score_data runs the module, so read-only mode withholds it: the form
    still shows the inputs, and the view must not offer a Score that fails."""
    uris, can = await _stamp(read_only=True)
    assert ui.resource_uri("get_mas_module_step_signature") in uris
    assert ui.resource_uri("score_data") not in uris
    assert can == {"get_mas_module_step_signature"}
    _, can = await _stamp()
    assert can == {"get_mas_module_step_signature", "score_data"}


def test_the_email_draft_goes_to_outlook_and_never_sends():
    """The view opens a compose window and the person presses Send: no tool or
    host call sends anything on their behalf."""
    page = ui._read("views/decision-score.html")
    assert "https://outlook.office.com/mail/deeplink/compose" in page
    assert "sas.say(" not in page


# --- the email draft is a deployment choice (MCP_EMAIL_DRAFT) ----------------------


def _stamp_of(page: str) -> dict:
    found = re.search(r"globalThis\.SAS_VIEW = (\{.*?\});", page, re.S)
    assert found
    return json.loads(found.group(1))


@pytest.fixture
def email_setting(monkeypatch):
    def use(value: str) -> None:
        monkeypatch.setattr(ui, "MCP_EMAIL_DRAFT", value)
        ui.render_view.cache_clear()

    yield use
    ui.render_view.cache_clear()


@pytest.mark.parametrize("value", ["off", "outlook", "mail"])
def test_the_view_is_told_the_email_setting(email_setting, value):
    email_setting(value)
    page = ui.render_view("decision-score", "score_data", "1.0", ("score_data",))
    assert _stamp_of(page)["options"] == {"email": value}


def test_the_view_hides_every_email_control_unless_the_setting_turns_them_on():
    """Off is the default, so the page must treat anything else as off too."""
    page = ui._read("views/decision-score.html")
    assert '["outlook", "mail"].includes(globalThis.SAS_VIEW?.options?.email)' in page
    for control in ('$("to").hidden = !mail', '$("email").hidden = !mail', 'EMAIL === "outlook" && sas.canOpenLinks'):
        assert control in page
    assert 'draft(EMAIL === "mail" ? "mailto" : "outlook")' in page


def test_changing_the_setting_changes_the_view_uri(email_setting):
    """Hosts cache a view by its URI, so a new setting must not be served stale."""
    email_setting("off")
    off = ui.fingerprint("1.0", [6, 7], False)
    email_setting("outlook")
    assert ui.fingerprint("1.0", [6, 7], False) != off


def _reload_config(monkeypatch, value):
    monkeypatch.setenv("VIYA_ENDPOINT", "https://test.viya.com")
    if value is None:
        monkeypatch.delenv("MCP_EMAIL_DRAFT", raising=False)
    else:
        monkeypatch.setenv("MCP_EMAIL_DRAFT", value)
    with patch("dotenv.load_dotenv"):
        return importlib.reload(sys.modules["sas_mcp_server.config"])


@pytest.fixture
def _restore_config(monkeypatch):
    yield
    monkeypatch.delenv("MCP_EMAIL_DRAFT", raising=False)
    monkeypatch.setenv("VIYA_ENDPOINT", "https://test.viya.com")
    with patch("dotenv.load_dotenv"):
        importlib.reload(sys.modules["sas_mcp_server.config"])


@pytest.mark.usefixtures("_restore_config")
@pytest.mark.parametrize(("raw", "expected"), [(None, "off"), ("", "off"), (" Outlook ", "outlook"), ("MAIL", "mail")])
def test_email_setting_default_and_spellings(monkeypatch, raw, expected):
    setting = _reload_config(monkeypatch, raw).MCP_EMAIL_DRAFT
    assert setting == expected


@pytest.mark.usefixtures("_restore_config")
def test_an_unknown_email_setting_stops_the_server_with_a_reason(monkeypatch):
    with pytest.raises(ConfigError, match="MCP_EMAIL_DRAFT must be one of off, outlook, mail"):
        _reload_config(monkeypatch, "gmail")
