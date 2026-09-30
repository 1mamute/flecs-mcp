"""FastMCP server wiring and the process entry point."""

import logging
import sys
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from flecs_mcp import __version__
from flecs_mcp.client import FlecsRestClient
from flecs_mcp.config import Config, ConfigError, configure_logging
from flecs_mcp.resources import register_resources
from flecs_mcp.tools import register_tools

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Inspect a running FLECS (Entity Component System) application through its REST API.

- Start with flecs_get_world_info to check the connection, FLECS version and world size.
- Find entities with flecs_query (FLECS query language) and inspect them with
  flecs_get_entity or flecs_get_component. flecs_list_components and
  flecs_list_queries show which components, systems and queries exist.
- Entity paths use FLECS dotted notation ('Sun.Earth') or '#<id>' for numeric ids.
  Component ids use full paths ('planets.Mass') or pairs ('(flecs.core.ChildOf, Sun)').
- Results are paged with limit/offset; prefer narrow queries and small limits.
- Tools marked [MUTATION] modify the running application. They are only available
  when the server runs with FLECS_REST_ALLOW_MUTATIONS=true.
"""

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def create_server(
    config: Config, *, transport: httpx.AsyncBaseTransport | None = None
) -> FastMCP[Any]:
    """Create the FastMCP server for ``config``.

    ``transport`` replaces the HTTP transport of the FLECS REST client; tests
    use it to substitute a mock for the FLECS application.
    """
    client = FlecsRestClient(config, transport=transport)

    @lifespan
    async def close_client_on_shutdown(
        _: FastMCP[Any],
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {}
        finally:
            await client.aclose()
            logger.info("FLECS REST client closed")

    mcp: FastMCP[Any] = FastMCP(
        name="flecs",
        instructions=INSTRUCTIONS,
        version=__version__,
        lifespan=close_client_on_shutdown,
        on_duplicate="error",
    )
    register_tools(mcp, client, allow_mutations=config.allow_mutations)
    register_resources(mcp, client)
    return mcp


def main() -> None:
    """Console entry point: load configuration and run the server."""
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"flecs-mcp: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    configure_logging(config.log_level)
    logger.info(
        "Starting flecs-mcp %s (transport=%s, FLECS REST API=%s, mutations=%s)",
        __version__,
        config.transport,
        config.display_url,
        "enabled" if config.allow_mutations else "disabled",
    )
    mcp = create_server(config)
    try:
        if config.transport == "http":
            if config.host not in _LOOPBACK_HOSTS:
                logger.warning(
                    "The MCP HTTP endpoint on %s:%d has no authentication; anyone "
                    "who can reach it can use the FLECS tools.",
                    config.host,
                    config.port,
                )
            mcp.run(
                transport="http",
                host=config.host,
                port=config.port,
                show_banner=False,
            )
        else:
            mcp.run(transport="stdio", show_banner=False)
    except KeyboardInterrupt:
        logger.info("Interrupted, shutting down")
