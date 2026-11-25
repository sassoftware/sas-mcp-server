#!/usr/bin/env python3
"""Register (or re-register) the sas-mcp OAuth client on a SAS Viya instance.

Reads VIYA_ENDPOINT, CLIENT_ID, HOST_PORT and MCP_BASE_URL from the
environment (or .env). Set VIYA_USERNAME and VIYA_PASSWORD to run without
prompts. Use --dry-run to see exactly what would be registered first.

With --target posit-connect, posit-workbench or posit (both), it registers the
SAS Logon clients for Posit instead: the confidential client behind Posit
Connect's OAuth integration (deploy/POSIT-CONNECT.md) and the one behind Posit
Workbench's. Those targets read the variable names of the sas-posit-env
.env file (--env-file), so one file configures Viya and the Posit products.

This deletes and re-creates the client, which is privileged and briefly leaves
the deployment without one, so it is a deliberate manual step -- the running
server never does it.
"""

import argparse
import getpass
import json
import os
import secrets
import ssl
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from textwrap import indent
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv

load_dotenv()


def int_env(name: str, default: str) -> int:
    """A whole-number setting, with a readable failure instead of a traceback."""
    raw = os.getenv(name, default).strip() or default
    try:
        return int(raw)
    except ValueError:
        print(f"Error: {name} must be a whole number, got {raw!r}.")
        raise SystemExit(1) from None


VIYA_ENDPOINT = os.getenv("VIYA_ENDPOINT", "").rstrip("/")
CLIENT_ID = os.getenv("CLIENT_ID", "sas-mcp")
HOST_PORT = int_env("HOST_PORT", "8134")
# Mirrors the rule in src/sas_mcp_server/config.py: an empty value is the
# documented .env.sample default, and a value left as a commented-out
# placeholder is not a URL -- both fall back to localhost. Keep the two in
# sync. If they disagree, the running server presents Viya a redirect_uri that
# this script never registered, and SASLogon rejects the sign-in with
# "Invalid redirect ... did not match one of the registered values".
_mcp_base_url = os.getenv("MCP_BASE_URL", "").strip()
if not _mcp_base_url or _mcp_base_url.startswith("#"):
    _mcp_base_url = f"http://localhost:{HOST_PORT}"
MCP_BASE_URL = _mcp_base_url.rstrip("/")
# Deployments reachable at more than one URL (a tunnel beside a shared host,
# blue/green hostnames) can name the extra callbacks here, comma separated.
# Full redirect URIs, not base URLs.
EXTRA_REDIRECT_URIS = [
    uri.strip()
    for uri in os.getenv("MCP_EXTRA_REDIRECT_URIS", "").split(",")
    if uri.strip() and not uri.strip().startswith("#")
]
_ssl_verify_env = os.getenv("SSL_VERIFY", "true").lower() not in ("false", "0", "no")

if _ssl_verify_env:
    ssl_context = True
else:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

# The path the server actually serves its OAuth callback on.
CALLBACK_PATH = "/auth/callback"

# --- Posit targets ------------------------------------------------------------
# Connect and Workbench are confidential OAuth clients of SAS Logon (they hold
# a client secret and keep each user's grant), unlike this server's own public
# PKCE client. The variable names and defaults match sas-posit-env's .env, so
# that file can be passed with --env-file.
POSIT_TARGETS = ("posit-connect", "posit-workbench")
TARGET_CHOICES = ("mcp", "posit", *POSIT_TARGETS)
# Workbench requests these by default (comma-separated); Connect requests the
# same set space-separated. The client must allow every scope requested.
DEFAULT_POSIT_SCOPES = "openid,sas-compute.user_impersonation"


@dataclass(frozen=True)
class PositProduct:
    """Where one Posit product's settings live and what its callback is."""

    target: str
    name: str
    id_var: str
    default_id: str
    secret_var: str
    url_var: str
    default_url: str
    callback_path: str


POSIT_PRODUCTS = {
    "posit-connect": PositProduct(
        target="posit-connect",
        name="Posit Connect",
        id_var="CONNECT_VIYA_CLIENT_ID",
        default_id="posit-connect",
        secret_var="CONNECT_VIYA_CLIENT_SECRET",
        url_var="POSIT_CONNECT_URL",
        default_url="http://localhost:3939",
        callback_path="/__oauth__/integrations/callback",
    ),
    "posit-workbench": PositProduct(
        target="posit-workbench",
        name="Posit Workbench",
        id_var="WORKBENCH_VIYA_CLIENT_ID",
        default_id="posit-workbench",
        secret_var="WORKBENCH_VIYA_CLIENT_SECRET",
        url_var="POSIT_WORKBENCH_URL",
        default_url="http://localhost:8787",
        callback_path="/oauth_redirect_callback",
    ),
}


@dataclass(frozen=True)
class PositClient:
    """One confidential client to register, resolved from the environment."""

    product: PositProduct
    client_id: str
    secret: str
    secret_generated: bool
    redirect_uri: list[str]


def env_value(env: Mapping[str, str], name: str) -> str:
    """A setting, treating a commented-out placeholder like an empty one."""
    value = env.get(name, "").strip()
    return "" if value.startswith("#") else value


def posit_scopes(env: Mapping[str, str]) -> list[str]:
    """The scopes to allow, from VIYA_SCOPES (comma-separated) or the default."""
    raw = env_value(env, "VIYA_SCOPES") or DEFAULT_POSIT_SCOPES
    scopes: list[str] = []
    for scope in raw.replace(" ", ",").split(","):
        if scope and scope not in scopes:
            scopes.append(scope)
    return scopes


def posit_endpoint(env: Mapping[str, str]) -> str:
    """The Viya URL: https://VIYA_HOSTNAME (sas-posit-env), else VIYA_ENDPOINT."""
    hostname = env_value(env, "VIYA_HOSTNAME")
    if hostname:
        if "://" not in hostname:
            hostname = f"https://{hostname}"
        return hostname.rstrip("/")
    return env_value(env, "VIYA_ENDPOINT").rstrip("/")


def resolve_posit_clients(
    targets: list[str], env: Mapping[str, str], extra_redirect_uris: list[str] | None = None
) -> list[PositClient]:
    """The clients to register. A secret that is not set is generated."""
    clients = []
    for target in targets:
        product = POSIT_PRODUCTS[target]
        base_url = (env_value(env, product.url_var) or product.default_url).rstrip("/")
        uris: list[str] = []
        for uri in (f"{base_url}{product.callback_path}", *(extra_redirect_uris or [])):
            if uri.strip() and uri.strip() not in uris:
                uris.append(uri.strip())
        secret = env_value(env, product.secret_var)
        clients.append(
            PositClient(
                product=product,
                client_id=env_value(env, product.id_var) or product.default_id,
                secret=secret or secrets.token_urlsafe(32),
                secret_generated=not secret,
                redirect_uri=uris,
            )
        )
    return clients


def posit_client_payload(client: PositClient, scopes: list[str], *, show_secret: bool = True) -> dict:
    """The body sent to POST /oauth/clients for a confidential Posit client."""
    if show_secret:
        secret = client.secret
    elif client.secret_generated:
        secret = "<generated at registration>"
    else:
        secret = f"<from {client.product.secret_var}>"
    return {
        "client_id": client.client_id,
        "client_secret": secret,
        "scope": scopes,
        "authorized_grant_types": ["authorization_code", "refresh_token"],
        "redirect_uri": client.redirect_uri,
        "autoapprove": True,
    }


def callback_origins(uris: list[str]) -> list[str]:
    """The scheme://host[:port] of each URI, for SAS Logon's CSP form-action."""
    origins: list[str] = []
    for uri in uris:
        parts = urlsplit(uri)
        origin = f"{parts.scheme}://{parts.netloc}"
        if parts.netloc and origin not in origins:
            origins.append(origin)
    return origins


# Fields SASLogon accepts on POST /oauth/clients that are worth carrying over
# when rebuilding a client from its previous definition. GET never returns
# client_secret, so only a public client can be restored this way -- which is
# what this script registers (allowpublic, PKCE).
RESTORE_FIELDS = (
    "client_id",
    "scope",
    "authorized_grant_types",
    "redirect_uri",
    "autoapprove",
    "allowpublic",
    "access_token_validity",
    "refresh_token_validity",
    "authorities",
)

# Fields worth showing when reporting what Viya actually stored.
REPORTED_FIELDS = (
    "client_id",
    "scope",
    "authorized_grant_types",
    "redirect_uri",
    "autoapprove",
    "allowpublic",
    "access_token_validity",
)

# What each status means for *this* endpoint, so a failure reads as a cause
# rather than a stack trace.
HTTP_HINTS = {
    400: "SASLogon rejected the request; the body below usually names the field.",
    401: "Credentials were rejected, or the token is no longer valid.",
    403: "Authenticated, but this user may not manage OAuth clients (the clients.admin scope is required).",
    409: "A client with this ID already exists.",
}


def http_detail(exc: httpx.HTTPError) -> str:
    """A one-line explanation of an httpx failure, with Viya's own message."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return f"{exc} (no response -- check VIYA_ENDPOINT, network access and SSL_VERIFY)"
    parts = [f"HTTP {resp.status_code}"]
    hint = HTTP_HINTS.get(resp.status_code)
    if hint:
        parts.append(hint)
    body = (resp.text or "").strip()
    if body:
        parts.append(f"Viya said: {body[:500]}")
    return " -- ".join(parts)


def get_bearer_token(base_url: str, username: str, password: str) -> str:
    """Authenticate with SASLogon using the sas.cli client and return an access token."""
    resp = httpx.post(
        f"{base_url}/SASLogon/oauth/token",
        auth=("sas.cli", ""),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "password", "username": username, "password": password},
        verify=ssl_context,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_credentials() -> tuple[str, str] | None:
    """Viya admin credentials from the environment, else prompted for.

    Returns None when neither is possible, so an unattended run fails with a
    message instead of an EOFError from input().
    """
    username = os.getenv("VIYA_USERNAME", "").strip()
    password = os.getenv("VIYA_PASSWORD", "")

    if username and password:
        print(f"Using VIYA_USERNAME/VIYA_PASSWORD from the environment ({username}).")
        return username, password

    missing = " and ".join(
        name for name, value in (("VIYA_USERNAME", username), ("VIYA_PASSWORD", password)) if not value
    )

    if not sys.stdin.isatty():
        print(f"Error: no terminal to prompt on, and {missing} is not set.")
        print("Set VIYA_USERNAME and VIYA_PASSWORD to run this unattended.")
        return None

    # isatty() is not a reliable guard on its own -- a redirected or wrapped
    # stdin can claim to be a terminal and still have nothing to read -- so the
    # prompts themselves have to fail into the same message.
    try:
        if not username:
            username = input("Viya admin username: ")
        if not password:
            password = getpass.getpass("Viya admin password: ")
    except EOFError:
        print(f"\nError: no input available at the prompt, and {missing} is not set.")
        print("Set VIYA_USERNAME and VIYA_PASSWORD to run this unattended.")
        return None
    except KeyboardInterrupt:
        print("\nCancelled.")
        return None

    return username, password


def fetch_client(base_url: str, token: str, client_id: str) -> dict | None:
    """The client's current definition in Viya, or None if it isn't registered."""
    resp = httpx.get(
        f"{base_url}/SASLogon/oauth/clients/{client_id}",
        headers={"Authorization": f"Bearer {token}"},
        verify=ssl_context,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def back_up_client(existing: dict) -> Path | None:
    """Write the pre-change definition to a file so it can be restored.

    The definition is printed to the console as well; this file is the copy
    that survives a closed terminal.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = Path(tempfile.gettempdir()) / f"{existing.get('client_id', 'oauth-client')}-{stamp}.json"
    try:
        path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"  (could not write a backup file: {exc})")
        return None
    return path


def print_restore_hint(base_url: str, existing: dict, backup: Path | None) -> None:
    """Print exactly how to put the previous definition back."""
    payload = {field: existing[field] for field in RESTORE_FIELDS if field in existing}
    confidential = not existing.get("allowpublic")
    if confidential:
        # GET never returns the secret, so a confidential client can only be
        # put back with the secret its consumers (e.g. Posit) already hold.
        payload["client_secret"] = "<the previous client secret>"
    print("\nTo restore the previous registration, get an admin token and run:\n")
    print(f'  curl -X POST "{base_url}/SASLogon/oauth/clients" \\')
    print('    -H "Content-Type: application/json" \\')
    print('    -H "Authorization: Bearer $BEARER_TOKEN" \\')
    print(f"    -d '{json.dumps(payload)}'")
    if confidential:
        print("\nViya never returns a client secret, so replace the placeholder with the")
        print("secret the client's users are configured with.")
    if backup:
        print(f"\nThe same definition is saved at {backup}")


def delete_client(base_url: str, token: str, client_id: str) -> bool:
    """Delete an existing OAuth client. Returns True if deleted, False if not found."""
    resp = httpx.delete(
        f"{base_url}/SASLogon/oauth/clients/{client_id}",
        headers={"Authorization": f"Bearer {token}"},
        verify=ssl_context,
    )
    if resp.status_code == 200:
        print(f"Deleted existing client '{client_id}'.")
        return True
    elif resp.status_code == 404:
        return False
    else:
        resp.raise_for_status()
        return False


def redirect_uris(extra: list[str] | None = None) -> list[str]:
    """Every OAuth callback the server may present to Viya.

    Localhost is always registered so the same client still works for a
    developer running the server locally, and MCP_BASE_URL is added when the
    server is also reachable at an external URL (reverse proxy, tunnel, k8s).
    SASLogon rejects any redirect_uri it was not registered with, so the list
    has to cover every URL in play -- hence MCP_EXTRA_REDIRECT_URIS and
    --redirect-uri for deployments answering on more than one.
    """
    candidates = [
        f"http://localhost:{HOST_PORT}{CALLBACK_PATH}",
        f"{MCP_BASE_URL}{CALLBACK_PATH}",
        *EXTRA_REDIRECT_URIS,
        *(extra or []),
    ]
    uris: list[str] = []
    for uri in candidates:
        cleaned = uri.strip()
        if cleaned and cleaned not in uris:
            uris.append(cleaned)
    return uris


def warn_unexpected_callbacks(uris: list[str]) -> None:
    """Flag a URI that does not end in the path the server actually serves."""
    for uri in uris:
        if not uri.endswith(CALLBACK_PATH):
            print(f"  Warning: {uri} does not end with {CALLBACK_PATH}, which is the path")
            print("           the server's callback is served on. Registering it anyway.")


def client_payload(client_id: str, redirect_uri: list[str]) -> dict:
    """The body sent to POST /oauth/clients.

    Shared with --dry-run so what is previewed is what would be sent. The
    client is public (allowpublic) because the MCP spec requires
    authorization-code flow with PKCE.
    """
    return {
        "client_id": client_id,
        "scope": ["openid"],
        "authorized_grant_types": ["authorization_code", "refresh_token"],
        "redirect_uri": redirect_uri,
        "autoapprove": True,
        "allowpublic": True,
        "access_token_validity": 36000,
    }


def register_client(base_url: str, token: str, payload: dict):
    """Register a new OAuth client with SASLogon."""
    resp = httpx.post(
        f"{base_url}/SASLogon/oauth/clients",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        verify=ssl_context,
    )
    resp.raise_for_status()
    print(f"Client '{payload['client_id']}' registered successfully.")
    for uri in payload["redirect_uri"]:
        print(f"  Redirect URI: {uri}")
    print(f"  Scopes: {', '.join(payload['scope'])}")
    print(f"  Grant types: {', '.join(payload['authorized_grant_types'])}")
    print(f"  Type: {'public (PKCE)' if payload.get('allowpublic') else 'confidential (client secret)'}")


def verify_registration(
    base_url: str, token: str, client_id: str, expected: list[str], scopes: list[str] | None = None
) -> bool:
    """Read the client back and check Viya stored what was asked for.

    Printing the fields is not enough. The failure this guards against is Viya
    holding a redirect_uri the server never presents, which looks unremarkable
    in a console dump but rejects every sign-in.
    """
    print("\nVerifying registration...")
    try:
        client_data = fetch_client(base_url, token, client_id)
    except httpx.HTTPError as exc:
        print(f"  FAIL: could not read the client back: {http_detail(exc)}")
        return False

    if client_data is None:
        print(f"  FAIL: client '{client_id}' is not registered.")
        return False

    for field in REPORTED_FIELDS:
        print(f"  {field}: {client_data.get(field)}")

    registered = client_data.get("redirect_uri") or []
    if isinstance(registered, str):
        registered = [registered]

    missing = [uri for uri in expected if uri not in registered]
    if missing:
        print("\n  FAIL: Viya did not store these redirect URIs:")
        for uri in missing:
            print(f"    {uri}")
        print("  Sign-in will fail with 'Invalid redirect ... did not match one of the")
        print("  registered values' until they are registered.")
        return False

    stored_scopes = client_data.get("scope") or []
    if isinstance(stored_scopes, str):
        stored_scopes = [stored_scopes]
    missing_scopes = [scope for scope in (scopes or []) if scope not in stored_scopes]
    if missing_scopes:
        print(f"\n  FAIL: Viya did not store these scopes: {', '.join(missing_scopes)}")
        print("  Sign-in fails with 'invalid_scope' while a client requests them.")
        return False

    if client_data.get("access_token_validity") is None:
        print("\n  Note: Viya reported no access_token_validity, so its default applies.")

    print("\n  OK: every expected redirect URI is registered.")
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register (or re-register) the MCP server's OAuth client in SAS Viya, "
        "or the SAS Logon clients for Posit Connect and Posit Workbench.",
        epilog="Set VIYA_USERNAME and VIYA_PASSWORD to run without prompts. If Viya uses a "
        "private certificate authority, set SSL_CERT_FILE to a PEM file with its certificate.",
    )
    parser.add_argument(
        "--target",
        choices=TARGET_CHOICES,
        default="mcp",
        help="What to register: mcp (default) is this server's own public client (CLIENT_ID). "
        "posit-connect and posit-workbench are the confidential clients behind the Posit OAuth "
        "integrations, posit registers both. The posit targets read CONNECT_/WORKBENCH_VIYA_CLIENT_ID "
        "and _SECRET (a missing secret is generated), POSIT_CONNECT_URL / POSIT_WORKBENCH_URL "
        "(default: localhost:3939 / :8787), VIYA_SCOPES and VIYA_HOSTNAME or VIYA_ENDPOINT.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        help="Also read settings from this file, e.g. sas-posit-env's .env. Variables already set "
        "in the environment win. Posit targets only.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the redirect URIs and request body that would be sent, then exit "
        "without contacting Viya or changing anything.",
    )
    parser.add_argument(
        "--redirect-uri",
        action="append",
        default=[],
        metavar="URI",
        help="An additional full redirect URI to register. Repeatable. Adds to MCP_EXTRA_REDIRECT_URIS. "
        "With a posit target, only together with posit-connect or posit-workbench.",
    )
    args = parser.parse_args(argv)
    if args.env_file and args.target == "mcp":
        parser.error("--env-file applies to the posit targets only; the mcp target reads .env.")
    if args.redirect_uri and args.target == "posit":
        parser.error(
            "--redirect-uri is ambiguous with --target posit; register posit-connect and posit-workbench separately."
        )
    if args.env_file and not args.env_file.is_file():
        parser.error(f"--env-file {args.env_file} does not exist.")
    return args


def authenticate(base_url: str) -> str | None:
    """Prompt for (or read) admin credentials and return a bearer token."""
    credentials = get_credentials()
    if credentials is None:
        return None
    username, password = credentials

    print("\nAuthenticating...")
    try:
        token = get_bearer_token(base_url, username, password)
    except httpx.HTTPError as exc:
        print(f"Authentication failed: {http_detail(exc)}")
        return None
    print("Authenticated successfully.\n")
    return token


def replace_client(
    base_url: str, token: str, payload: dict, expected: list[str], scopes: list[str] | None = None
) -> bool:
    """Back up, delete, re-create and verify one client. True on success."""
    client_id = payload["client_id"]
    # Registering means deleting and re-creating the client, so a failure part
    # way through leaves the deployment with no client at all and nobody able
    # to sign in. This read is the only chance to record what to put back.
    try:
        existing = fetch_client(base_url, token, client_id)
    except httpx.HTTPError as exc:
        print(f"Could not read the existing client: {http_detail(exc)}")
        return False

    backup = None
    if existing is None:
        print(f"No existing client '{client_id}'; registering a new one.\n")
    else:
        print(f"Current registration for '{client_id}' (about to be replaced):")
        print(indent(json.dumps(existing, indent=2), "  "))
        backup = back_up_client(existing)
        if backup:
            print(f"  Backed up to {backup}")
        print()

    try:
        delete_client(base_url, token, client_id)
    except httpx.HTTPError as exc:
        print(f"Could not delete the existing client: {http_detail(exc)}")
        print("Nothing was changed.")
        return False

    try:
        register_client(base_url, token, payload)
    except httpx.HTTPError as exc:
        print(f"\nRegistration FAILED: {http_detail(exc)}")
        if existing is not None:
            print(
                f"\nThe old client was already deleted, so '{client_id}' does not exist "
                "right now\nand nobody can sign in until it is registered again."
            )
            print_restore_hint(base_url, existing, backup)
        return False

    return verify_registration(base_url, token, client_id, expected, scopes)


def main_posit(args: argparse.Namespace) -> int:
    """Register the confidential SAS Logon clients for Posit Connect / Workbench."""
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    env = os.environ
    targets = list(POSIT_TARGETS) if args.target == "posit" else [args.target]

    base_url = posit_endpoint(env)
    if not base_url:
        print("Error: set VIYA_HOSTNAME (as in sas-posit-env's .env) or VIYA_ENDPOINT.")
        return 1
    hostname, endpoint = env_value(env, "VIYA_HOSTNAME"), env_value(env, "VIYA_ENDPOINT").rstrip("/")
    if hostname and endpoint and endpoint != base_url:
        print(f"Note: VIYA_HOSTNAME wins over VIYA_ENDPOINT ({endpoint}).")

    scopes = posit_scopes(env)
    clients = resolve_posit_clients(targets, env, args.redirect_uri)
    print(f"Viya endpoint: {base_url}")
    print(f"Scopes: {', '.join(scopes)}")
    for client in clients:
        print(f"\n{client.product.name}: client '{client.client_id}' (confidential)")
        for uri in client.redirect_uri:
            print(f"  Redirect URI: {uri}")
        if client.secret_generated:
            print(f"  {client.product.secret_var} is not set; a secret will be generated.")
    print()

    if args.dry_run:
        print("--dry-run: nothing was contacted and nothing was changed.")
        print("These are the request bodies that would be sent to POST /SASLogon/oauth/clients:\n")
        for client in clients:
            print(indent(json.dumps(posit_client_payload(client, scopes, show_secret=False), indent=2), "  "))
            print()
        return 0

    token = authenticate(base_url)
    if token is None:
        return 1

    failed = []
    for client in clients:
        print(f"--- {client.product.name} ---")
        if not replace_client(base_url, token, posit_client_payload(client, scopes), client.redirect_uri, scopes):
            failed.append(client)
        print()

    registered = [client for client in clients if client not in failed]
    generated = [client for client in registered if client.secret_generated]
    if generated:
        print("Generated client secrets. Put them in the .env of sas-posit-env (shown only now):\n")
        for client in generated:
            print(f"  {client.product.id_var}={client.client_id}")
            print(f"  {client.product.secret_var}={client.secret}")
        print()
    if registered:
        origins = callback_origins([uri for client in registered for uri in client.redirect_uri])
        print("Next: SAS Logon must allow the redirect back to Posit after sign-in. In SAS Environment")
        print("Manager, Configuration > Definitions > sas.commons.web.security, add these origins to")
        print(f"form-action in SAS Logon Manager's content-security-policy: {' '.join(origins)}")
    if failed:
        print(f"\nFAILED: {', '.join(client.product.name for client in failed)}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not _ssl_verify_env:
        print("WARNING: SSL_VERIFY is false, so TLS certificate verification is disabled.\n")

    if args.target != "mcp":
        return main_posit(args)

    if not VIYA_ENDPOINT:
        print("Error: VIYA_ENDPOINT is not set. Check your .env file.")
        return 1

    expected = redirect_uris(args.redirect_uri)
    print(f"Viya endpoint: {VIYA_ENDPOINT}")
    print(f"Client ID: {CLIENT_ID}")
    for uri in expected:
        print(f"Redirect URI: {uri}")
    warn_unexpected_callbacks(expected)
    print()

    if args.dry_run:
        print("--dry-run: nothing was contacted and nothing was changed.")
        print("This is the request body that would be sent to POST /SASLogon/oauth/clients:\n")
        print(indent(json.dumps(client_payload(CLIENT_ID, expected), indent=2), "  "))
        return 0

    token = authenticate(VIYA_ENDPOINT)
    if token is None:
        return 1

    return 0 if replace_client(VIYA_ENDPOINT, token, client_payload(CLIENT_ID, expected), expected) else 1


if __name__ == "__main__":
    sys.exit(main())
