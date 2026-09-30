"""MCP server exposing the FLECS ECS REST API."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("flecs-mcp")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.0.0"
