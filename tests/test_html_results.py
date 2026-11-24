# Copyright © 2026, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""HTML results for ``execute_sas_code``.

The compute context opens no HTML destination, so the server wraps the code in
ODS HTML5, strips the wrapper from the log, reads the page back from the job's
results and saves it to the Files service for a link. The shapes asserted here
were probed against a live Viya: result items named after the body file with a
``self`` link of type text/html, an empty ``<body>`` when nothing was printed,
and the note ODS logs when it opens the file.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import patch
from urllib.parse import urlparse

import httpx
import pytest

from sas_mcp_server.viya_utils import (
    VIYA_ENDPOINT,
    clear_session_cache,
    fetch_html_result,
    new_html_body_file,
    run_one_snippet,
    store_html_result,
    strip_html_wrapper,
    wrap_for_html,
)

BODY = "sasmcp-0a1b2c3d.htm"
PAGE = b"<!DOCTYPE html><html><head><style>.c{}</style></head><body class=\"c body\"><table></table></body></html>"
EMPTY_PAGE = b"<!DOCTYPE html><html><head><style>.c{}</style></head><body class=\"c body\">\n</body>\n</html>"


@pytest.fixture(autouse=True)
def _dynamic_sessions():
    with patch("sas_mcp_server.viya_utils.COMPUTE_SESSION_ID", ""):
        clear_session_cache()
        yield
        clear_session_cache()


# --- the wrapper -----------------------------------------------------------------


def test_the_code_runs_between_the_open_and_a_guarded_close():
    wrapped = wrap_for_html("proc print data=sashelp.class; run;", BODY).split("\n")
    assert wrapped[0] == f'ods html5 (id=sasmcp) file="{BODY}";'
    assert wrapped[1] == "proc print data=sashelp.class; run;"
    # The magic string first: it ends an unterminated statement, quote or
    # comment the code left open, which would otherwise swallow the close.
    assert wrapped[2] == ";*';*\";*/;run;quit;ods html5 (id=sasmcp) close;"


def test_wrapper_lines_never_wrap_in_the_log():
    """The log echoes a source line after its number; past LINESIZE (64 at the
    least) it wraps, and a wrapped echo could no longer be stripped exactly."""
    for line in (wrap_for_html("x", new_html_body_file()).split("\n")[i] for i in (0, 2)):
        assert len(f"123456    {line}") < 64, line


def test_each_call_gets_its_own_body_file():
    """Two calls sharing a session must never read each other's page."""
    assert len({new_html_body_file() for _ in range(50)}) == 50


# The log of a wrapped job, as the compute service returned it (line numbers
# continue across the jobs of a reused session).
WRAPPED_LOG = f"""13   ods html5 (id=sasmcp) file="{BODY}";
NOTE: Writing HTML5(SASMCP) Body file: {BODY}
14   proc print data=sashelp.class(obs=2); run;

NOTE: There were 2 observations read from the data set SASHELP.CLASS.
NOTE: PROCEDURE PRINT used (Total process time):
      real time           0.03 seconds

15   ;*';*";*/;run;quit;ods html5 (id=sasmcp) close;"""


def test_the_log_shows_only_the_code_that_was_given():
    stripped = strip_html_wrapper(WRAPPED_LOG, BODY)
    assert "sasmcp" not in stripped.lower()
    assert stripped.split("\n")[0] == "14   proc print data=sashelp.class(obs=2); run;"
    assert "NOTE: There were 2 observations read" in stripped
    assert stripped.split("\n")[-1] == ""  # the step's own trailing blank line


def test_a_line_of_the_persons_own_is_kept_even_when_it_looks_alike():
    """Only this call's lines go: another body file's statement, or the magic
    string written by the person themselves, stay in the log."""
    log = "\n".join([
        '20   ods html5 (id=sasmcp) file="sasmcp-ffffffff.htm";',
        "21   ;*';*\";*/;quit;run;",
        "NOTE: Writing HTML5(SASMCP) Body file: sasmcp-ffffffff.htm",
    ])
    assert strip_html_wrapper(log, BODY) == log
    ours = "22   ;*';*\";*/;run;quit;ods html5 (id=sasmcp) close;"
    assert strip_html_wrapper(f"{log}\n{ours}", BODY) == log


# A step the code left open, ended by the guard's run; — the log as returned
# for `proc print data=sashelp.class(obs=1);` with no run. The echo of the
# closing line breaks around the step's notes and resumes after "3  !".
SPLIT_LOG = f"""1    ods html5 (id=sasmcp) file="{BODY}";
NOTE: Writing HTML5(SASMCP) Body file: {BODY}
2    proc print data=sashelp.class(obs=1);
3    ;*';*";*/;run;

NOTE: There were 1 observations read from the data set SASHELP.CLASS.
NOTE: PROCEDURE PRINT used (Total process time):
      real time           0.01 seconds
3                                                          The SAS System          Tuesday, September 22, 2026
      cpu time            0.01 seconds

3  !               quit;ods html5 (id=sasmcp) close;"""


def test_a_closing_line_split_around_the_steps_notes_goes_entirely():
    stripped = strip_html_wrapper(SPLIT_LOG, BODY).split("\n")
    assert stripped[0] == "2    proc print data=sashelp.class(obs=1);"
    assert not [line for line in stripped if "*/;" in line or "sasmcp" in line.lower()]
    # The step's own notes stay, and so does a page header that merely shares
    # the closing line's number.
    assert "NOTE: There were 1 observations read from the data set SASHELP.CLASS." in stripped
    assert any("The SAS System" in line for line in stripped)
    assert "      cpu time            0.01 seconds" in stripped


# --- reading the page back -----------------------------------------------------


def _response(method: str, url: str, status: int = 200, **kwargs) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request(method, url), **kwargs)


class FakeViya:
    """Routes a client's calls by URL; records what was sent."""

    def __init__(self, *, results=None, page=PAGE, files_status=201, job_log=WRAPPED_LOG):
        self.results = results if results is not None else [_result_item(BODY)]
        self.page = page
        self.files_status = files_status
        self.job_log = job_log
        self.posts: list[tuple[str, dict]] = []

    async def get(self, url, params=None, headers=None):
        path = urlparse(url).path
        if path.endswith("/compute/contexts"):
            return _response("GET", url, json={"items": [{"id": "ctx", "name": "SAS Job Execution compute context"}]})
        if path.endswith("/state"):
            return _response("GET", url, text="completed")
        if path.endswith("/log"):
            items = [{"line": line} for line in self.job_log.split("\n")]
            return _response("GET", url, json={"items": items})
        if path.endswith("/listing"):
            return _response("GET", url, json={"items": [{"line": "Obs  Name"}]})
        if path.endswith("/jobs/job-1/results"):
            return _response("GET", url, json={"items": self.results})
        if "/results/" in path:
            return _response("GET", url, content=self.page, headers={"Content-Type": "text/html"})
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, url, json=None, params=None, content=None, headers=None):
        path = urlparse(url).path
        self.posts.append((path, {"json": json, "params": params, "content": content, "headers": headers}))
        if path.endswith("/sessions") and "/contexts/" in path:
            return _response("POST", url, 201, json={"id": "sess-1"})
        if path.endswith("/jobs"):
            return _response("POST", url, 201, json={"id": "job-1"})
        if path == "/files/files":
            if self.files_status >= 400:
                return _response("POST", url, self.files_status, json={"message": "Access denied."})
            return _response("POST", url, 201, json={"id": "file-1"})
        raise AssertionError(f"unexpected POST {url}")

    def submitted_code(self) -> str:
        return "\n".join(next(p["json"]["code"] for path, p in self.posts if path.endswith("/jobs")))


def _result_item(name: str, kind: str = "ODS") -> dict:
    return {
        "id": name,
        "name": name,
        "type": kind,
        "links": [{"rel": "self", "method": "GET", "type": "text/html",
                   "href": f"/compute/sessions/sess-1/results/abc123/{name}"}],
    }


async def test_the_page_is_the_one_this_call_named():
    viya = FakeViya(results=[_result_item("sashtml.htm"), _result_item(BODY), _result_item("X", "TABLE")])
    assert await fetch_html_result(viya, "sess-1", "job-1", BODY) == PAGE


async def test_no_page_when_the_code_printed_nothing():
    """ODS writes a complete page for a DATA step too; its body is empty."""
    assert await fetch_html_result(FakeViya(page=EMPTY_PAGE), "sess-1", "job-1", BODY) is None


async def test_no_page_when_the_job_wrote_none():
    assert await fetch_html_result(FakeViya(results=[]), "sess-1", "job-1", BODY) is None


# --- saving it for a link --------------------------------------------------------


async def test_the_page_is_saved_inline_without_an_expiry():
    viya = FakeViya()
    assert await store_html_result(viya, PAGE, "job-1") == "file-1"
    path, sent = viya.posts[0]
    assert path == "/files/files"
    assert sent["content"] == PAGE
    assert sent["headers"]["Content-Type"] == "text/html"
    # inline, or a browser downloads the page instead of showing it
    assert sent["headers"]["Content-Disposition"].startswith("inline;")
    # The Files service's default: no expirationTimeStamp, kept until deleted.
    assert not sent["params"]


# --- end to end ------------------------------------------------------------------


@asynccontextmanager
async def _client(viya):
    yield viya


async def _run(viya, **kwargs):
    with patch("sas_mcp_server.viya_utils.make_client", lambda token: _client(viya)), \
         patch("sas_mcp_server.viya_utils.new_html_body_file", lambda: BODY):
        return await run_one_snippet("proc print data=sashelp.class(obs=2); run;", "1", "t", **kwargs)


async def test_html_results_add_a_link_and_leave_the_rest_as_it_was():
    viya = FakeViya()
    result = await _run(viya, html_results=True)
    assert viya.submitted_code().startswith('ods html5 (id=sasmcp)')
    assert result["state"] == "completed"
    assert result["listing"] == "Obs  Name"
    assert "sasmcp" not in result["log"].lower()
    assert result["html_results_url"] == f"{VIYA_ENDPOINT}/files/files/file-1/content"
    assert result["html_results_file_id"] == "file-1"
    json.dumps(result)  # still a flat dict of strings


async def test_without_html_results_the_code_is_submitted_exactly_as_given():
    viya = FakeViya(job_log="1    proc print data=sashelp.class(obs=2); run;")
    result = await _run(viya)
    assert viya.submitted_code() == "proc print data=sashelp.class(obs=2); run;"
    assert not [key for key in result if key.startswith("html_")]
    assert not [path for path, _ in viya.posts if path == "/files/files"]


async def test_nothing_printed_means_no_link_and_nothing_saved():
    viya = FakeViya(page=EMPTY_PAGE)
    result = await _run(viya, html_results=True)
    assert not [key for key in result if key.startswith("html_")]
    assert not [path for path, _ in viya.posts if path == "/files/files"]


async def test_a_page_that_cannot_be_saved_is_reported_not_raised():
    """The log and listing are what was asked for; they must survive a
    Files-service refusal, with the reason alongside."""
    result = await _run(FakeViya(files_status=403), html_results=True)
    assert result["state"] == "completed"
    assert result["listing"] == "Obs  Name"
    assert "html_results_url" not in result
    assert "Access denied." in result["html_results_error"]
