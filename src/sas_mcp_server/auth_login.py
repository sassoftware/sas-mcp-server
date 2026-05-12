#!/usr/bin/env python3
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Zero-prerequisite OAuth 2.0 login helper for SAS Viya stdio mode.

Runs the OAuth 2.0 Authorization Code + PKCE flow against the built-in
``vscode`` client shipped with SAS Viya 2022.11+. No admin client
registration and no external CLI install are required, which makes this
the lowest-friction bootstrap when the operator cannot use the ``sas-viya``
CLI path.

The flow is interactive: a browser opens to the SAS Logon page, the user
signs in, and SAS Logon displays the authorization code on a results
page. The user pastes the code back into the terminal; the helper
exchanges it for an access token and writes the result to
``~/.sas-mcp-server/credentials.json`` in the same shape as
``~/.sas/credentials.json`` so the stdio server picks it up
automatically.

Usage:

  uv run sas-mcp-login
      Prints the SAS Logon URL, opens it in a browser, then prompts for the
      authorization code (interactive terminal).

  uv run sas-mcp-login --code <CODE>
      Two-step variant for non-interactive contexts (e.g., shells without a
      TTY). The first invocation (no --code) persists PKCE state to
      ``~/.sas-mcp-server/login-state.json`` and prints instructions; the
      second invocation completes the exchange.
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import string
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv

load_dotenv()

DEFAULT_HELPER_CLIENT_ID = "vscode"
CACHE_PATH = Path.home() / ".sas-mcp-server" / "credentials.json"
STATE_PATH = Path.home() / ".sas-mcp-server" / "login-state.json"


def _generate_pkce() -> tuple[str, str]:
    allowed = string.ascii_letters + string.digits + "-._~"
    verifier = "".join(secrets.choice(allowed) for _ in range(128))
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    return verifier, challenge


def _authorize_url(
    endpoint: str, client_id: str, challenge: str, redirect_uri: str | None
) -> str:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": secrets.token_urlsafe(16),
    }
    if redirect_uri:
        params["redirect_uri"] = redirect_uri
    return f"{endpoint.rstrip('/')}/SASLogon/oauth/authorize?{urlencode(params)}"


def _exchange(
    endpoint: str,
    client_id: str,
    code: str,
    verifier: str,
    redirect_uri: str | None,
    verify_ssl: bool,
) -> dict:
    data = {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
    }
    if redirect_uri:
        data["redirect_uri"] = redirect_uri
    resp = httpx.post(
        f"{endpoint.rstrip('/')}/SASLogon/oauth/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        verify=verify_ssl,
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def _write_state(payload: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(payload))
    try:
        os.chmod(STATE_PATH, 0o600)
    except OSError:
        pass


def _read_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _clear_state() -> None:
    try:
        STATE_PATH.unlink()
    except OSError:
        pass


def _write_cache(tokens: dict) -> Path:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    expires_in = int(tokens.get("expires_in", 0))
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    payload = {
        "Default": {
            "access-token": tokens["access_token"],
            "refresh-token": tokens.get("refresh_token", ""),
            "expiry": expiry,
        }
    }
    CACHE_PATH.write_text(json.dumps(payload, indent=2))
    try:
        os.chmod(CACHE_PATH, 0o600)
    except OSError:
        pass
    return CACHE_PATH


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SAS Viya zero-prereq OAuth login helper for sas-mcp-server stdio mode.",
    )
    parser.add_argument(
        "--endpoint",
        default=os.getenv("VIYA_ENDPOINT", ""),
        help="SAS Viya endpoint (default: VIYA_ENDPOINT from environment)",
    )
    parser.add_argument(
        "--client-id",
        default=os.getenv("AUTH_HELPER_CLIENT_ID", DEFAULT_HELPER_CLIENT_ID),
        help="OAuth client_id to use (default: 'vscode' — built-in on Viya 2022.11+)",
    )
    parser.add_argument(
        "--redirect-uri",
        default="",
        help=(
            "OAuth redirect URI. Default is empty, which omits the parameter "
            "entirely; SAS Logon then shows the authorization code on a results "
            "page for the user to copy. Pass a value only if your client has a "
            "registered redirect URI."
        ),
    )
    parser.add_argument(
        "--code",
        default="",
        help=(
            "Authorization code returned by SAS Logon. Pass this in a second "
            "invocation after the first one has opened the browser. Without "
            "--code, the helper falls back to an interactive prompt if stdin "
            "is a terminal, or saves the PKCE state to "
            f"{STATE_PATH} and exits otherwise."
        ),
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorization URL without opening a browser",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip SSL certificate verification",
    )
    args = parser.parse_args()

    if not args.endpoint:
        parser.error(
            "VIYA_ENDPOINT is not set in the environment and --endpoint was not given"
        )

    env_ssl = os.getenv("SSL_VERIFY", "true").lower() not in ("false", "0", "no")
    verify_ssl = False if args.insecure else env_ssl

    # Phase 2: --code was given. Load saved state, exchange, write cache.
    if args.code:
        state = _read_state()
        if not state:
            print(
                f"No saved login state at {STATE_PATH}. Run `sas-mcp-login` "
                "first to get the authorization URL, then re-run with --code.",
                file=sys.stderr,
            )
            return 1
        try:
            tokens = _exchange(
                state["endpoint"],
                state["client_id"],
                args.code,
                state["verifier"],
                state.get("redirect_uri") or None,
                verify_ssl,
            )
        except httpx.HTTPStatusError as exc:
            print(f"\nToken exchange failed: {exc}", file=sys.stderr)
            if exc.response is not None:
                print(f"Response body: {exc.response.text[:500]}", file=sys.stderr)
            return 1
        path = _write_cache(tokens)
        _clear_state()
        print(f"Token cached at: {path}")
        print(
            "Stdio mode (`uv run app-stdio` or the container's `app-stdio` "
            "command) will read this file automatically. Re-run "
            "`sas-mcp-login` when the token expires."
        )
        return 0

    # Phase 1: no --code. Generate PKCE, print URL, persist verifier, and either
    # prompt interactively (terminal) or exit with instructions (non-tty).
    redirect_uri = args.redirect_uri or None
    verifier, challenge = _generate_pkce()
    auth_url = _authorize_url(args.endpoint, args.client_id, challenge, redirect_uri)
    _write_state({
        "endpoint": args.endpoint,
        "client_id": args.client_id,
        "redirect_uri": redirect_uri or "",
        "verifier": verifier,
    })

    print(f"Endpoint:    {args.endpoint}")
    print(f"Client ID:   {args.client_id}")
    print(f"Redirect:    {redirect_uri or '(none — SAS Logon will show the code on a page)'}")
    print(f"SSL verify:  {verify_ssl}")
    print()
    print("Step 1 — open this URL in a browser and sign in:")
    print()
    print(f"  {auth_url}")
    print()
    if not args.no_browser:
        webbrowser.open(auth_url)

    if redirect_uri:
        instruction = (
            f"Step 2 — after signing in, the browser is redirected to "
            f"{redirect_uri}?code=...\n"
            "         Copy the value of the 'code' query parameter."
        )
    else:
        instruction = (
            "Step 2 — after signing in, SAS Logon displays the authorization "
            "code on the results page.\n         Copy the code."
        )
    print(instruction)
    print()

    if not sys.stdin.isatty():
        # Non-interactive: persist state, tell the user how to finish.
        print(
            "Stdin is not a terminal, so this helper cannot prompt. Once you "
            "have the code, run:\n"
            f"\n    uv run sas-mcp-login --code <CODE>\n\n"
            f"PKCE state has been saved to {STATE_PATH}."
        )
        return 0

    try:
        code = input("Authorization code: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 1
    if not code:
        print("No code provided.", file=sys.stderr)
        return 1

    try:
        tokens = _exchange(
            args.endpoint,
            args.client_id,
            code,
            verifier,
            redirect_uri,
            verify_ssl,
        )
    except httpx.HTTPStatusError as exc:
        print(f"\nToken exchange failed: {exc}", file=sys.stderr)
        if exc.response is not None:
            print(f"Response body: {exc.response.text[:500]}", file=sys.stderr)
        return 1

    path = _write_cache(tokens)
    _clear_state()
    print()
    print(f"Token cached at: {path}")
    print(
        "Stdio mode (`uv run app-stdio` or the container's `app-stdio` command) "
        "will read this file automatically. Re-run `sas-mcp-login` when the "
        "token expires."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
