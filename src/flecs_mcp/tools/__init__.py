"""MCP tools exposing the FLECS REST API."""

from typing import Any

from fastmcp import FastMCP

from flecs_mcp.client import FlecsRestClient
from flecs_mcp.tools.mutations import register_mutation_tools
from flecs_mcp.tools.read import register_read_tools


def register_tools(
    mcp: FastMCP[Any], client: FlecsRestClient, *, allow_mutations: bool
) -> None:
    """Register the READ tools, plus the MUTATION tools if ``allow_mutations``."""
    register_read_tools(mcp, client)
    if allow_mutations:
        register_mutation_tools(mcp, client)


__all__ = ["register_tools"]
