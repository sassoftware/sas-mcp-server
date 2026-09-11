# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import logging
import os

from dotenv import load_dotenv
from fastmcp.server.auth.providers.jwt import JWTVerifier

from .auth import PermissiveOAuthProxy
from .env import env_bool, parse_log_results
from .exceptions import ConfigError
from .ssl_patch import disable_tls_verification

load_dotenv()

SSL_VERIFY = env_bool("SSL_VERIFY", True)
ALLOW_RAW_BEARER = env_bool("ALLOW_RAW_BEARER", False)
AUTH_ENABLED = env_bool("VIYA_AUTH", True)

# --- Opt-in collection mode (telemetry) -------------------------------------
# Master switch. Default OFF; must be explicitly true/1/yes/on. When false,
# install_telemetry() returns immediately: no middleware, no schema change,
# zero overhead.
COLLECTION_MODE = env_bool("COLLECTION_MODE", False)
# DOTTED default matches the existing credentials dir ~/.sas-mcp-server/ (see
# .env.sample note re: the user-stated dot-less path) and sits outside the tree
# mcp.http_app() serves. Expanded with os.path.expanduser at install time.
COLLECTION_LOG_PATH = os.getenv(
    "COLLECTION_LOG_PATH", "~/.sas-mcp-server/tool-usage.log"
)
# Per-field cap (bytes) for arguments/goal/error. Raised from 4 KiB so the
# submitted SAS code / queries — the core signal — are actually captured.
COLLECTION_MAX_FIELD_BYTES = int(os.getenv("COLLECTION_MAX_FIELD_BYTES", "16384"))
# Separate (smaller) cap for tool RESULTS. Results tend to be large and less
# central to the analysis than the arguments, so they get a tighter bound.
COLLECTION_MAX_RESULT_BYTES = int(os.getenv("COLLECTION_MAX_RESULT_BYTES", "8192"))
# RotatingFileHandler rollover size (bytes); default 10 MiB.
COLLECTION_MAX_LOG_BYTES = int(
    os.getenv("COLLECTION_MAX_LOG_BYTES", str(10 * 1024 * 1024))
)
# RotatingFileHandler backupCount; rotated files glob as tool-usage.log*.
COLLECTION_LOG_BACKUPS = int(os.getenv("COLLECTION_LOG_BACKUPS", "3"))
# Whether 'goal' is appended to each schema's required[]. Escape hatch = false.
COLLECTION_REQUIRE_GOAL = env_bool("COLLECTION_REQUIRE_GOAL", True)
# Privacy dial for tool RESULTS — tri-state:
#   never    — results recorded as a content-free shape summary
#            ({"_type":"object","_keys":[...names]}), NOT their contents, so
#            data-sensitive shops contribute usage signal without exfiltrating
#            table rows or SAS listings. Note this makes a success and a
#            tool-declared failure indistinguishable in the log.
#   failures (DEFAULT) — full (capped + redacted) result contents ONLY when the call
#            errored at the MCP layer or the tool itself declared a failure
#            (status apply_failed / invalid_* / not_found / ...). Successes
#            stay shape-only. The middle ground: failure diagnostics are the
#            highest-value trace data and rarely carry table rows.
#   always   — full (capped + redacted) result contents on every call.
# Back-compat: true/1/yes/on -> always; false/0/no/off -> never.
# Parsing lives in env.py alongside env_bool, which owns the boolean spellings.
COLLECTION_LOG_RESULTS = parse_log_results(os.getenv("COLLECTION_LOG_RESULTS"))
# Free-text experiment label stamped into each run_start record — tag A/B runs
# (skill on/off, server build) so traces are self-describing.
COLLECTION_RUN_TAG = os.getenv("COLLECTION_RUN_TAG", "") or None
# Accept the former name so an existing .env keeps labelling its runs. Dropping
# it silently would only surface AFTER an A/B arm finished, as an untagged
# header — i.e. the arm has to be re-run. Warn like parse_log_results does.
_legacy_tag = os.getenv("COLLECTION_SESSION_TAG", "") or None
if _legacy_tag and not COLLECTION_RUN_TAG:
    logging.getLogger(__name__).warning(
        "COLLECTION_SESSION_TAG is deprecated; using it as COLLECTION_RUN_TAG. "
        "Rename it — telemetry groups by run, not by MCP session."
    )
    COLLECTION_RUN_TAG = _legacy_tag

_logger = logging.getLogger(__name__)

if not SSL_VERIFY:
    # Self-signed Viya certificates. Patches httpx AND httpx2 (FastMCP 4's
    # client), so the exemption keeps covering FastMCP's own JWKS/OAuth calls
    # across a major upgrade. See sas_mcp_server.ssl_patch.
    disable_tls_verification()

VIYA_ENDPOINT = os.getenv("VIYA_ENDPOINT", "").rstrip("/")
CLIENT_ID = os.getenv("CLIENT_ID", "sas-mcp")
HOST_PORT = int(os.getenv("HOST_PORT", "8134"))
MCP_SIGNING_KEY = os.getenv("MCP_SIGNING_KEY", "default")
CONTEXT_NAME = os.getenv("COMPUTE_CONTEXT_NAME", "SAS Job Execution compute context")
# Name advertised to MCP clients as serverInfo.name during the initialize
# handshake. Clients label the server with this rather than the key in their own
# config, so one server per Viya environment can be told apart.
DEFAULT_SERVER_NAME = "SAS Viya Execution MCP Server"
SERVER_NAME = os.getenv("MCP_SERVER_NAME", DEFAULT_SERVER_NAME)
COMPUTE_SESSION_ID = os.getenv("COMPUTE_SESSION_ID", "").strip()
# Optional tool-tier selection, e.g. "0-4" or "0,1,7". Empty means all tiers.
# Parsed by sas_mcp_server.tools.resolve_enabled_tiers.
MCP_TIERS = os.getenv("MCP_TIERS", "")
# Read-only mode: register only tools that neither change server-side state nor
# cause server-side work. Composes with (does not replace) MCP_TIERS — the split
# cuts across tiers, so this filters whichever tiers are enabled. Default OFF.
# Classification lives in sas_mcp_server.tools._access.READ_ONLY_TOOLS.
MCP_READ_ONLY = env_bool("MCP_READ_ONLY", False)
# Browser landing page on the MCP endpoint (HTTP mode). A plain browser GET of
# /mcp (Accept: text/html) gets an explanatory page with the tool catalogue and
# client-config snippets instead of FastMCP's bare 401. Unauthenticated by
# design and shows only deployment shape (name, version, Viya host, tiers,
# tool names + one-line summaries) — set false to hide it and get the 401 back.
# Rendering lives in sas_mcp_server.landing.
MCP_LANDING_PAGE = env_bool("MCP_LANDING_PAGE", True)

_mcp_base_url = os.getenv("MCP_BASE_URL", "").strip()
# An empty value is the documented .env.sample default. Also ignore values
# that are plainly placeholder comments instead of URLs.
MCP_BASE_URL = _mcp_base_url if _mcp_base_url and not _mcp_base_url.startswith("#") else f"http://localhost:{HOST_PORT}"

# Upper bound on binary export bytes returned inline as an embedded resource by
# ``export_report``. Larger exports are refused with guidance rather than
# streamed through the model context (default 25 MiB).
MAX_EXPORT_INLINE_BYTES = int(os.getenv("MAX_EXPORT_INLINE_BYTES", str(25 * 1024 * 1024)))

# Upper bound on bytes accepted from the server-side upload sources
# (``upload_data`` / ``upload_file`` with ``file_path`` or ``url``). The whole
# source is buffered in memory while it is re-posted to Viya, so without a cap
# one oversized URL fetch can OOM-kill the process. Default 100 MiB — SAS
# Viya's own default file-upload limit — because a bigger payload would be
# refused upstream anyway; if your Viya administrator raises the Viya-side
# limit, raise this with it (and revisit the pod memory limit, see
# deploy/SCALING.md).
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))

if not VIYA_ENDPOINT:
    raise ConfigError(
        "VIYA_ENDPOINT is not set. Please set it in the environment variables."
    )

AUTHORIZATION_ENDPOINT = f"{VIYA_ENDPOINT}/SASLogon/oauth/authorize"
TOKEN_ENDPOINT = f"{VIYA_ENDPOINT}/SASLogon/oauth/token"
JWKS_URI = f"{VIYA_ENDPOINT}/SASLogon/token_keys"


token_verifier = JWTVerifier(jwks_uri=JWKS_URI, audience=[])

viya_auth = PermissiveOAuthProxy(
    upstream_authorization_endpoint=AUTHORIZATION_ENDPOINT,
    upstream_token_endpoint=TOKEN_ENDPOINT,
    upstream_client_id=CLIENT_ID,
    upstream_client_secret=None,
    # Keep in step with upstream_client_secret above: sas-mcp is registered as a
    # public client (allowpublic, no secret — see examples/register_mcp_client.py),
    # so the token exchange must present no password at all. Say so explicitly
    # rather than leave it inferred. FastMCP 4.0 replaced authlib's OAuth2 client
    # with its own, and the replacement defaults to client_secret_basic whether or
    # not a secret exists, where authlib chose "none" when there was none. The
    # None above was f-string interpolated into Basic base64("sas-mcp:None") and
    # SAS Logon rejected every browser sign-in with "invalid_client: Missing
    # credentials" (#54). If a client secret ever becomes configurable, this has
    # to become conditional on it.
    token_endpoint_auth_method="none",
    jwt_signing_key=MCP_SIGNING_KEY,
    base_url=MCP_BASE_URL,
    # An MCP client opens a fresh loopback port for every sign-in. FastMCP 4.0
    # turns CIMD on by default, and a client whose client_id is an HTTPS URL
    # (GitHub Copilot CLI is one) then has its callback checked against the
    # redirect URIs in its own published metadata document — which lists fixed
    # ports and gets no loopback exemption, so the check can never pass. It
    # cannot be widened from here either: the document check runs first, and
    # allowed_client_redirect_uris only narrows what the document already
    # permits. Off, those client IDs take the ordinary dynamic-registration
    # path, which does let loopback ports vary (RFC 8252 §7.3) and is what every
    # other MCP client already uses. The trade is that this server no longer
    # advertises client_id_metadata_document_supported or private_key_jwt —
    # correct, since with CIMD off it supports neither (#58). The underlying
    # asymmetry is FastMCP's: its DCR path implements the loopback rule and its
    # CIMD path does not; drop this line if that is ever reconciled.
    enable_cimd=False,
    forward_pkce=True,
    token_verifier=token_verifier,
    valid_scopes=["openid"],
    allow_raw_bearer=ALLOW_RAW_BEARER,
)
