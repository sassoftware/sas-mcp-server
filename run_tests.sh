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
NO_LINT=false
REPORT=false
ENDPOINT=""
USERNAME=""
PASSWORD=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --integration)       INTEGRATION=true; shift ;;
        --integration-only)  INTEGRATION=true; INTEGRATION_ONLY=true; shift ;;
        --no-lint)           NO_LINT=true; shift ;;
        --report)            REPORT=true; shift ;;
        --endpoint)          ENDPOINT="$2"; shift 2 ;;
        --username)          USERNAME="$2"; shift 2 ;;
        --password)          PASSWORD="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 [--integration] [--integration-only] [--no-lint] [--report] [--endpoint URL] [--username USER] [--password PASS]"
            echo ""
            echo "  (no flags)          Run lint + type check + unit tests"
            echo "  --integration       Run lint + type check + unit + integration tests"
            echo "  --integration-only  Run integration tests only"
            echo "  --no-lint           Skip the ruff + pyright gates"
            echo "  --report            Write reports/integration.xml + reports/integration-summary.md"
            echo "                      (git-ignored) — for attaching results to a PR locally"
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

# Optional JUnit report (written to git-ignored reports/). The coverage floor in
# pytest.ini applies to the full unit suite, so integration-only runs disable it.
REPORT_ARG=""
if $REPORT; then
    mkdir -p reports
    REPORT_ARG="--junitxml=reports/integration.xml"
fi

# Lint + type-check gates (skip with --no-lint or for integration-only runs)
if ! $NO_LINT && ! $INTEGRATION_ONLY; then
    echo "=== Linting (ruff) ==="
    uv run ruff check .
    echo "=== Type checking (pyright) ==="
    uv run pyright src
fi

if $INTEGRATION_ONLY; then
    echo "=== Running integration tests ==="
    uv run python -m pytest -m integration --no-cov -v $REPORT_ARG "$@"
elif $INTEGRATION; then
    echo "=== Running all tests (unit + integration) ==="
    uv run python -m pytest -v $REPORT_ARG "$@"
else
    echo "=== Running unit tests ==="
    uv run python -m pytest -m "not integration" -v "$@"
fi

if $REPORT; then
    uv run python scripts/junit_to_summary.py reports/integration.xml reports/integration-summary.md >/dev/null
    echo ""
    echo "Wrote reports/integration.xml and reports/integration-summary.md (git-ignored)."
    echo "Attach results to a PR without committing, e.g.:"
    echo "  gh pr comment <PR> --body-file reports/integration-summary.md   # summary as a comment"
    echo "  gh gist create reports/integration.xml                          # full XML as a linkable gist"
fi
