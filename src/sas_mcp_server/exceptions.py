# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared exception types for the SAS MCP server."""

from fastmcp.exceptions import FastMCPError


class AuthenticationError(FastMCPError):
    """Raised when a request cannot be authenticated against SAS Viya."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return f"AuthenticationError: {self.message}"


class ConfigError(Exception):
    """Raised when required server configuration is missing or invalid."""
