#!/usr/bin/env bash
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Test runner for sas-mcp-server.
#
# Usage:
#   ./run_tests.sh                          # unit tests only
#   ./run_tests.sh --integration            # unit + integration (reads .env)
#   ./run_tests.sh --integration --endpoint URL --username USER --password PASS
#   ./run_tests.sh --integration-only       # integration tests only
#
set -euo pipefail

INTEGRATION=false
INTEGRATION_ONLY=false
ENDPOINT=""
USERNAME=""
PASSWORD=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --integration)       INTEGRATION=true; shift ;;
        --integration-only)  INTEGRATION=true; INTEGRATION_ONLY=true; shift ;;
        --endpoint)          ENDPOINT="$2"; shift 2 ;;
        --username)          USERNAME="$2"; shift 2 ;;
        --password)          PASSWORD="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 [--integration] [--integration-only] [--endpoint URL] [--username USER] [--password PASS]"
            echo ""
            echo "  (no flags)          Run unit tests only"
            echo "  --integration       Run unit + integration tests"
            echo "  --integration-only  Run integration tests only"
            echo "  --endpoint URL      SAS Viya endpoint (overrides VIYA_ENDPOINT / .env)"
            echo "  --username USER     Viya username   (overrides VIYA_USERNAME / .env)"
            echo "  --password PASS     Viya password   (overrides VIYA_PASSWORD / .env)"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# Override env vars from CLI args if provided
[[ -n "$ENDPOINT" ]] && export VIYA_ENDPOINT="$ENDPOINT"
[[ -n "$USERNAME" ]] && export VIYA_USERNAME="$USERNAME"
[[ -n "$PASSWORD" ]] && export VIYA_PASSWORD="$PASSWORD"

if $INTEGRATION_ONLY; then
    echo "=== Running integration tests ==="
    uv run python -m pytest -m integration -v "$@"
elif $INTEGRATION; then
    echo "=== Running all tests (unit + integration) ==="
    uv run python -m pytest -v "$@"
else
    echo "=== Running unit tests ==="
    uv run python -m pytest -m "not integration" -v "$@"
fi
