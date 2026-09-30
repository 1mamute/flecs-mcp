"""MCP resources for stable, read-only FLECS data.

Only data that does not change while the application runs is exposed as a
resource: the FLECS build information and component type schemas. Everything
else is live world state and is served by tools.
"""

from typing import Any

from fastmcp import FastMCP

from flecs_mcp.client import FlecsRestClient


def register_resources(mcp: FastMCP[Any], client: FlecsRestClient) -> None:
    """Register the FLECS resources on ``mcp``."""

    @mcp.resource(
        "flecs://build-info",
        name="flecs_build_info",
        mime_type="application/json",
        tags={"flecs", "read"},
    )
    async def build_info() -> Any:
        """FLECS build of the connected application: version, compiler, enabled
        addons and debug/sanitize/perf_trace flags (flecs.core.BuildInfo)."""
        return await client.get_build_info()

    @mcp.resource(
        "flecs://type-info/{component}",
        name="flecs_type_info",
        mime_type="application/json",
        tags={"flecs", "read"},
    )
    async def type_info(component: str) -> dict[str, Any]:
        """Reflection schema of a component type, addressed by its dotted path
        (e.g. flecs://type-info/planets.Mass). Same content as flecs_get_type_info."""
        schema = await client.get_type_info(component)
        return {
            "component": component,
            "has_reflection": schema is not None,
            "schema": schema,
        }
