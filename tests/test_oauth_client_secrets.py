# Copyright © 2026, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Client secrets for MCP clients that register asking for one
(MCP_OAUTH_CLIENT_SECRETS).

The shapes were taken from a live Microsoft 365 Copilot provisioning run.
Its registration service first reads the authorization-server metadata and
refuses to go on unless ``token_endpoint_auth_methods_supported`` lists
``client_secret_basic`` or ``client_secret_post``; it then registers with
``token_endpoint_auth_method: client_secret_basic`` and refuses a response
that has no ``client_secret``. FastMCP 4 answers every registration as a
public client, which fails both checks.
"""

from __future__ import annotations

import base64

import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier
from key_value.aio.stores.memory import MemoryStore
from starlette.applications import Starlette
from starlette.testclient import TestClient

from sas_mcp_server.auth import (
    SECRET_AUTH_METHODS,
    PermissiveOAuthProxy,
    _basic_client_id,
    _ClientIdFromBasic,
)

# What Microsoft's registration service sent, verbatim.
M365_REGISTRATION = {
    "redirect_uris": ["https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect"],
    "client_name": "9bf78vcf81",
    "token_endpoint_auth_method": "client_secret_basic",
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
}
PUBLIC_REGISTRATION = {
    **M365_REGISTRATION,
    "redirect_uris": ["http://127.0.0.1:53211/callback"],
    "client_name": "an MCP client",
    "token_endpoint_auth_method": "none",
}
METADATA = "/.well-known/oauth-authorization-server"


def _client(client_secrets: bool) -> TestClient:
    proxy = PermissiveOAuthProxy(
        upstream_authorization_endpoint="https://viya.example.com/SASLogon/oauth/authorize",
        upstream_token_endpoint="https://viya.example.com/SASLogon/oauth/token",
        upstream_client_id="sas-mcp",
        upstream_client_secret=None,
        token_endpoint_auth_method="none",
        jwt_signing_key="test-signing-key",
        base_url="https://mcp.example.com",
        enable_cimd=False,
        token_verifier=JWTVerifier(jwks_uri="https://viya.example.com/SASLogon/token_keys", audience=[]),
        valid_scopes=["openid"],
        client_storage=MemoryStore(),
        client_secrets=client_secrets,
    )
    return TestClient(Starlette(routes=proxy.get_routes("/mcp")))


def _basic(client_id: str, secret: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()}


# --- off (the default): FastMCP's behaviour, unchanged -------------------------


def test_off_every_client_is_registered_as_public():
    http = _client(client_secrets=False)
    assert http.get(METADATA).json()["token_endpoint_auth_methods_supported"] == ["none"]
    registered = http.post("/register", json=M365_REGISTRATION).json()
    assert registered["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in registered


# --- on -------------------------------------------------------------------------


def test_metadata_lists_the_secret_methods_next_to_none():
    http = _client(client_secrets=True)
    response = http.get(METADATA)
    assert response.status_code == 200
    document = response.json()
    assert document["token_endpoint_auth_methods_supported"] == ["none", *SECRET_AUTH_METHODS]
    # The rest of the document is FastMCP's, untouched.
    assert document["registration_endpoint"] == "https://mcp.example.com/register"
    assert document["code_challenge_methods_supported"] == ["S256"]
    assert int(response.headers["content-length"]) == len(response.content)


def test_metadata_preflight_still_answers():
    http = _client(client_secrets=True)
    response = http.options(
        METADATA,
        headers={"Origin": "https://example.com", "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 200


def test_a_client_that_asks_for_a_secret_gets_one():
    http = _client(client_secrets=True)
    response = http.post("/register", json=M365_REGISTRATION)
    assert response.status_code == 201
    registered = response.json()
    assert registered["token_endpoint_auth_method"] == "client_secret_basic"
    assert len(registered["client_secret"]) >= 32
    # RFC 7591: required whenever a secret is issued; 0 means it never expires.
    assert registered["client_secret_expires_at"] == 0


def test_a_public_client_stays_public():
    http = _client(client_secrets=True)
    registered = http.post("/register", json=PUBLIC_REGISTRATION).json()
    assert registered["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in registered


@pytest.mark.parametrize("method", SECRET_AUTH_METHODS)
def test_the_token_endpoint_enforces_the_secret(method):
    """Issuing a secret that is never checked would be worse than issuing none."""
    http = _client(client_secrets=True)
    registered = http.post(
        "/register", json={**M365_REGISTRATION, "token_endpoint_auth_method": method}
    ).json()
    client_id, secret = registered["client_id"], registered["client_secret"]
    form = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": "not-a-real-code",
        "redirect_uri": M365_REGISTRATION["redirect_uris"][0],
        "code_verifier": "v" * 43,
    }

    def exchange(with_secret: str | None):
        if method == "client_secret_basic":
            headers = _basic(client_id, with_secret) if with_secret else {}
            return http.post("/token", data=form, headers=headers)
        data = {**form, "client_secret": with_secret} if with_secret else form
        return http.post("/token", data=data)

    for wrong in (None, "wrong-secret"):
        refused = exchange(wrong)
        assert refused.status_code == 401, refused.text
        assert refused.json()["error"] in ("invalid_client", "unauthorized_client")

    # The right secret gets past client authentication; the made-up code is
    # then what fails, which is a different error.
    accepted = exchange(secret)
    assert accepted.json()["error"] == "invalid_grant", accepted.text


# --- the Basic header alone names the client ---------------------------------------
#
# Since November 2026 Microsoft 365 Copilot's token request carries the client id in
# the Basic header only, as RFC 6749 section 2.3.1 allows. The SDK reads it from the
# form alone and answered "Missing client_id", so sign-in failed at the exchange.

_EXCHANGE = {
    "grant_type": "authorization_code",
    "code": "not-a-real-code",
    "redirect_uri": M365_REGISTRATION["redirect_uris"][0],
    "code_verifier": "v" * 43,
}
_REFRESH = {"grant_type": "refresh_token", "refresh_token": "not-a-real-token"}


def _register(http: TestClient) -> tuple[str, str]:
    registered = http.post("/register", json=M365_REGISTRATION).json()
    return registered["client_id"], registered["client_secret"]


@pytest.mark.parametrize("form", [_EXCHANGE, _REFRESH], ids=["code", "refresh"])
def test_the_basic_header_alone_names_the_client(form):
    http = _client(client_secrets=True)
    client_id, secret = _register(http)
    # Past client authentication: the made-up code or token is what fails now.
    accepted = http.post("/token", data=form, headers=_basic(client_id, secret))
    assert accepted.json()["error"] == "invalid_grant", accepted.text
    # The secret is still checked.
    refused = http.post("/token", data=form, headers=_basic(client_id, "wrong-secret"))
    assert refused.status_code == 401
    assert refused.json()["error"] == "invalid_client"


def test_a_client_id_in_the_form_must_still_match_the_header():
    http = _client(client_secrets=True)
    client_id, secret = _register(http)
    other, _ = _register(http)
    refused = http.post("/token", data={**_EXCHANGE, "client_id": other}, headers=_basic(client_id, secret))
    assert refused.status_code == 401
    assert refused.json()["error_description"] == "Client ID mismatch in Basic auth"


def test_off_the_token_endpoint_is_left_as_it_is():
    http = _client(client_secrets=False)
    client_id = http.post("/register", json=M365_REGISTRATION).json()["client_id"]
    response = http.post("/token", data=_EXCHANGE, headers=_basic(client_id, "anything"))
    assert response.json()["error_description"] == "Missing client_id"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (b"Basic " + base64.b64encode(b"abc:secret"), "abc"),
        (b"basic " + base64.b64encode(b"a%3Ab:secret"), "a:b"),
        (b"Bearer " + base64.b64encode(b"abc:secret"), None),
        (b"Basic " + base64.b64encode(b"no-colon"), None),
        (b"Basic " + base64.b64encode(b":secret"), None),
        (b"Basic not base64!", None),
        (b"", None),
    ],
)
def test_basic_client_id(header, expected):
    assert _basic_client_id(header) == expected


async def test_a_body_in_several_chunks_is_joined_before_the_id_is_added():
    seen: dict = {}

    async def app(scope, receive, send):
        seen["headers"] = dict(scope["headers"])
        seen["body"] = (await receive())["body"]

    chunks = [
        {"type": "http.request", "body": b"grant_type=refresh_token&", "more_body": True},
        {"type": "http.request", "body": b"refresh_token=t", "more_body": False},
    ]

    async def receive():
        return chunks.pop(0)

    scope = {
        "type": "http",
        "method": "POST",
        "headers": [
            (b"authorization", b"Basic " + base64.b64encode(b"abc:secret")),
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", b"40"),
        ],
    }
    await _ClientIdFromBasic(app)(scope, receive, None)
    assert seen["body"] == b"grant_type=refresh_token&refresh_token=t&client_id=abc"
    assert seen["headers"][b"content-length"] == str(len(seen["body"])).encode()
