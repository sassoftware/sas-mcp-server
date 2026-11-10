#!/usr/bin/env bash
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Deploy the SAS MCP server to Posit Connect. See deploy/POSIT-CONNECT.md.
#
# Builds a bundle of the sas_mcp_server package plus a requirements.txt pinned
# from uv.lock, then runs `rsconnect deploy fastapi`. Everything after the
# options below is passed to rsconnect unchanged, e.g. -n <server-nickname>,
# --guid <content-guid> to redeploy, -t <title>, --visibility private.
#
# Usage:
#   VIYA_ENDPOINT=https://viya.example.com deploy/posit-connect/deploy.sh -n myconnect
#   deploy/posit-connect/deploy.sh --bundle-only /tmp/sas-mcp-bundle
#
# Environment variables forwarded to the content when set locally (others can
# be set later in the content's Vars pane on Connect):
#   VIYA_ENDPOINT (required), POSIT_CONNECT_INTEGRATION_GUID, MCP_BASE_URL,
#   COMPUTE_CONTEXT_NAME, MCP_SERVER_NAME, MCP_TIERS, MCP_READ_ONLY,
#   MCP_DEFAULT_FOLDER, MCP_APPS, MCP_TOOL_RENAME, SSL_VERIFY,
#   VIYA_CLIENT_TIMEOUT, MAX_UPLOAD_BYTES, MAX_EXPORT_INLINE_BYTES
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENTRYPOINT="sas_mcp_server.mcp_server:app"
FORWARDED_VARS=(
  POSIT_CONNECT_INTEGRATION_GUID MCP_BASE_URL COMPUTE_CONTEXT_NAME MCP_SERVER_NAME
  MCP_TIERS MCP_READ_ONLY MCP_DEFAULT_FOLDER MCP_APPS MCP_TOOL_RENAME SSL_VERIFY
  VIYA_CLIENT_TIMEOUT MAX_UPLOAD_BYTES MAX_EXPORT_INLINE_BYTES
)

bundle_only=""
if [[ "${1:-}" == "--bundle-only" ]]; then
  bundle_only="${2:?--bundle-only needs a target directory}"
  shift 2
fi

command -v uv >/dev/null || { echo "error: uv is required (https://docs.astral.sh/uv/)" >&2; exit 1; }

version="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$REPO_ROOT/pyproject.toml" | head -1)"
python_version="$(tr -d '[:space:]' < "$REPO_ROOT/.python-version")"

if [[ -n "$bundle_only" ]]; then
  mkdir -p "$bundle_only"
  bundle="$(cd "$bundle_only" && pwd)"
else
  bundle="$(mktemp -d "${TMPDIR:-/tmp}/sas-mcp-connect.XXXXXX")"
  trap 'rm -rf "$bundle"' EXIT
fi

# The package itself, importable from the bundle root (Connect runs the
# entrypoint from there; the code uses absolute sas_mcp_server.* imports).
rm -rf "$bundle/sas_mcp_server"
cp -R "$REPO_ROOT/src/sas_mcp_server" "$bundle/"
find "$bundle/sas_mcp_server" -name '__pycache__' -type d -prune -exec rm -rf {} +

# Runtime dependencies, pinned to the lock like the Docker image. No hashes:
# Connect's installer may add packages of its own to the environment.
(cd "$REPO_ROOT" && uv export --frozen --no-default-groups --no-emit-project --no-hashes \
  --format requirements.txt -o "$bundle/requirements.txt" >/dev/null)
echo "$python_version" > "$bundle/.python-version"

echo "Bundle for sas-mcp-server $version (Python $python_version): $bundle"
if [[ -n "$bundle_only" ]]; then
  exit 0
fi

: "${VIYA_ENDPOINT:?set VIYA_ENDPOINT to your SAS Viya URL, e.g. https://viya.example.com}"
command -v rsconnect >/dev/null || {
  echo "error: rsconnect not found. Install it with: uv tool install rsconnect-python" >&2
  exit 1
}

env_args=(
  -E "VIYA_ENDPOINT=$VIYA_ENDPOINT"
  -E "POSIT_CONNECT_OAUTH=true"
  -E "MCP_STATELESS_HTTP=true"
  -E "SAS_MCP_VERSION=$version"
)
for name in "${FORWARDED_VARS[@]}"; do
  if [[ -n "${!name:-}" ]]; then
    env_args+=(-E "$name=${!name}")
  fi
done

# Defaults first so the caller's own flags (later on the line) win.
rsconnect deploy fastapi \
  --entrypoint "$ENTRYPOINT" \
  --title "SAS MCP Server" \
  --override-python-version "$python_version" \
  --requirements-file "$bundle/requirements.txt" \
  "${env_args[@]}" \
  "$@" \
  "$bundle"
