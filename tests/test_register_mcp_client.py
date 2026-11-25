# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for examples/register_mcp_client.py, in particular the Posit targets."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "register_mcp_client.py"
_spec = importlib.util.spec_from_file_location("register_mcp_client", SCRIPT)
assert _spec and _spec.loader
reg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reg)

POSIT_VARS = (
    "VIYA_HOSTNAME",
    "VIYA_ENDPOINT",
    "VIYA_SCOPES",
    "CONNECT_VIYA_CLIENT_ID",
    "CONNECT_VIYA_CLIENT_SECRET",
    "WORKBENCH_VIYA_CLIENT_ID",
    "WORKBENCH_VIYA_CLIENT_SECRET",
    "POSIT_CONNECT_URL",
    "POSIT_WORKBENCH_URL",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Unset every Posit setting, and undo whatever --env-file loads later."""
    for name in POSIT_VARS:
        # setenv first so monkeypatch records the variable and removes a value
        # that load_dotenv() sets during the test.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    return monkeypatch


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("no network call expected")

    for name in ("get", "post", "delete"):
        monkeypatch.setattr(reg.httpx, name, refuse)


def test_posit_defaults_match_sas_posit_env():
    connect, workbench = reg.resolve_posit_clients(["posit-connect", "posit-workbench"], {})
    assert connect.client_id == "posit-connect"
    assert connect.redirect_uri == ["http://localhost:3939/__oauth__/integrations/callback"]
    assert workbench.client_id == "posit-workbench"
    assert workbench.redirect_uri == ["http://localhost:8787/oauth_redirect_callback"]
    assert connect.secret_generated and workbench.secret_generated
    assert len(connect.secret) >= 32 and connect.secret != workbench.secret
    assert reg.posit_scopes({}) == ["openid", "sas-compute.user_impersonation"]


def test_posit_settings_from_env():
    env = {
        "CONNECT_VIYA_CLIENT_ID": "my-connect",
        "CONNECT_VIYA_CLIENT_SECRET": "s3cret",
        "POSIT_CONNECT_URL": "https://connect.example.com/",
        "WORKBENCH_VIYA_CLIENT_SECRET": "#placeholder",
    }
    connect, workbench = reg.resolve_posit_clients(["posit-connect", "posit-workbench"], env)
    assert connect.client_id == "my-connect"
    assert connect.secret == "s3cret" and not connect.secret_generated
    assert connect.redirect_uri == ["https://connect.example.com/__oauth__/integrations/callback"]
    assert workbench.secret_generated, "a commented-out placeholder is not a secret"


def test_extra_redirect_uris_are_deduplicated():
    (client,) = reg.resolve_posit_clients(
        ["posit-connect"],
        {},
        ["http://localhost:3939/__oauth__/integrations/callback", "https://alt.example.com/cb"],
    )
    assert client.redirect_uri == [
        "http://localhost:3939/__oauth__/integrations/callback",
        "https://alt.example.com/cb",
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("openid", ["openid"]),
        ("openid,uaa.user", ["openid", "uaa.user"]),
        ("openid uaa.user openid", ["openid", "uaa.user"]),
    ],
)
def test_posit_scopes(raw, expected):
    assert reg.posit_scopes({"VIYA_SCOPES": raw}) == expected


def test_posit_endpoint_prefers_hostname():
    assert reg.posit_endpoint({"VIYA_HOSTNAME": "viya.example.com"}) == "https://viya.example.com"
    assert (
        reg.posit_endpoint({"VIYA_HOSTNAME": "viya.example.com", "VIYA_ENDPOINT": "https://other"})
        == "https://viya.example.com"
    )
    assert reg.posit_endpoint({"VIYA_ENDPOINT": "https://viya.example.com/"}) == "https://viya.example.com"
    assert reg.posit_endpoint({}) == ""


def test_posit_payload_is_confidential():
    (client,) = reg.resolve_posit_clients(["posit-connect"], {"CONNECT_VIYA_CLIENT_SECRET": "s3cret"})
    payload = reg.posit_client_payload(client, ["openid"])
    assert payload["client_secret"] == "s3cret"
    assert "allowpublic" not in payload
    assert payload["authorized_grant_types"] == ["authorization_code", "refresh_token"]
    masked = reg.posit_client_payload(client, ["openid"], show_secret=False)
    assert masked["client_secret"] == "<from CONNECT_VIYA_CLIENT_SECRET>"


def test_mcp_payload_unchanged():
    payload = reg.client_payload("sas-mcp", ["http://localhost:8134/auth/callback"])
    assert payload["allowpublic"] is True
    assert "client_secret" not in payload
    assert payload["scope"] == ["openid"]


def test_callback_origins():
    assert reg.callback_origins(
        [
            "http://localhost:3939/__oauth__/integrations/callback",
            "http://localhost:8787/oauth_redirect_callback",
            "http://localhost:3939/other",
        ]
    ) == ["http://localhost:3939", "http://localhost:8787"]


def test_dry_run_posit_reads_env_file_and_hides_secrets(tmp_path, clean_env, no_network, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "VIYA_HOSTNAME=viya.example.com\n"
        "CONNECT_VIYA_CLIENT_ID=posit-connect\n"
        "CONNECT_VIYA_CLIENT_SECRET=do-not-print-me\n"
        "WORKBENCH_VIYA_CLIENT_ID=posit-workbench\n",
        encoding="utf-8",
    )
    assert reg.main(["--target", "posit", "--env-file", str(env_file), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Viya endpoint: https://viya.example.com" in out
    assert "do-not-print-me" not in out
    assert "<from CONNECT_VIYA_CLIENT_SECRET>" in out
    assert "<generated at registration>" in out
    assert "http://localhost:8787/oauth_redirect_callback" in out


def test_posit_without_endpoint_fails(clean_env, no_network, capsys):
    assert reg.main(["--target", "posit-connect", "--dry-run"]) == 1
    assert "VIYA_HOSTNAME" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["--env-file", "x.env"],
        ["--target", "posit", "--redirect-uri", "https://x/cb"],
        ["--target", "posit", "--env-file", "does-not-exist.env"],
    ],
)
def test_invalid_argument_combinations(argv):
    with pytest.raises(SystemExit) as exc:
        reg.parse_args(argv)
    assert exc.value.code == 2


class FakeViya:
    """Just enough of /SASLogon/oauth/clients for replace_client()."""

    def __init__(self, existing=None, drop_scopes=False):
        self.clients = {existing["client_id"]: existing} if existing else {}
        self.drop_scopes = drop_scopes
        self.posted = []

    def install(self, monkeypatch):
        monkeypatch.setattr(reg.httpx, "get", self.get)
        monkeypatch.setattr(reg.httpx, "delete", self.delete)
        monkeypatch.setattr(reg.httpx, "post", self.post)

    @staticmethod
    def _resp(method, url, status, body=None):
        return httpx.Response(status, json=body, request=httpx.Request(method, url))

    def get(self, url, **kwargs):
        client = self.clients.get(url.rsplit("/", 1)[-1])
        return self._resp("GET", url, 200, client) if client else self._resp("GET", url, 404)

    def delete(self, url, **kwargs):
        found = self.clients.pop(url.rsplit("/", 1)[-1], None)
        return self._resp("DELETE", url, 200 if found else 404)

    def post(self, url, json=None, **kwargs):
        self.posted.append(json)
        stored = {k: v for k, v in json.items() if k != "client_secret"}
        if self.drop_scopes:
            stored["scope"] = ["openid"]
        self.clients[json["client_id"]] = stored
        return self._resp("POST", url, 201, stored)


def test_replace_client_registers_confidential_client(monkeypatch, capsys):
    viya = FakeViya(existing={"client_id": "posit-connect", "scope": ["openid"], "redirect_uri": ["old"]})
    viya.install(monkeypatch)
    (client,) = reg.resolve_posit_clients(["posit-connect"], {"CONNECT_VIYA_CLIENT_SECRET": "s3cret"})
    scopes = ["openid", "sas-compute.user_impersonation"]
    assert reg.replace_client(
        "https://viya", "tok", reg.posit_client_payload(client, scopes), client.redirect_uri, scopes
    )
    assert viya.posted[0]["client_secret"] == "s3cret"
    out = capsys.readouterr().out
    assert "confidential (client secret)" in out
    assert "OK: every expected redirect URI is registered." in out


def test_replace_client_fails_when_scopes_are_not_stored(monkeypatch, capsys):
    FakeViya(drop_scopes=True).install(monkeypatch)
    (client,) = reg.resolve_posit_clients(["posit-workbench"], {})
    scopes = ["openid", "sas-compute.user_impersonation"]
    payload = reg.posit_client_payload(client, scopes)
    assert not reg.replace_client("https://viya", "tok", payload, client.redirect_uri, scopes)
    assert "did not store these scopes: sas-compute.user_impersonation" in capsys.readouterr().out


def test_restore_hint_for_confidential_client(capsys):
    existing = {"client_id": "posit-connect", "scope": ["openid"], "redirect_uri": ["x"]}
    reg.print_restore_hint("https://viya", existing, None)
    out = capsys.readouterr().out
    body = json.loads(out.split("-d '", 1)[1].split("'", 1)[0])
    assert body["client_secret"] == "<the previous client secret>"
    assert "Viya never returns a client secret" in out


def test_restore_hint_for_public_client_has_no_secret(capsys):
    existing = {"client_id": "sas-mcp", "allowpublic": True, "redirect_uri": ["x"]}
    reg.print_restore_hint("https://viya", existing, None)
    assert "client_secret" not in capsys.readouterr().out
