"""Typed runtime configuration, loaded exclusively from environment variables.

=============================  ========  ==========================================
Variable                       Default   Meaning
=============================  ========  ==========================================
``FLECS_REST_URL``             required  Base URL of the FLECS REST API.
``FLECS_REST_TIMEOUT``         ``5``     Request timeout in seconds.
``FLECS_REST_VERIFY_TLS``      ``true``  Verify TLS certificates for https URLs.
``FLECS_REST_ALLOW_MUTATIONS`` ``false`` Register the tools that modify the world.
``MCP_LOG_LEVEL``              ``INFO``  DEBUG, INFO, WARNING, ERROR or CRITICAL.
``MCP_TRANSPORT``              ``stdio`` ``stdio`` or ``http`` (streamable HTTP).
``MCP_HOST``                   127.0.0.1 Bind address for the ``http`` transport.
``MCP_PORT``                   ``8000``  Bind port for the ``http`` transport.
=============================  ========  ==========================================

Empty values are treated as unset.
"""

import logging
import math
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Self
from urllib.parse import urlsplit, urlunsplit

type Transport = Literal["stdio", "http"]

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ConfigError(ValueError):
    """Raised when the environment contains missing or invalid configuration."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """Validated configuration of the MCP server."""

    rest_url: str
    timeout: float = 5.0
    verify_tls: bool = True
    allow_mutations: bool = False
    log_level: str = "INFO"
    transport: Transport = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def display_url(self) -> str:
        """The REST URL with any embedded credentials removed, safe to log."""
        return redact_url(self.rest_url)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        """Build a configuration from ``env`` (defaults to ``os.environ``).

        Raises:
            ConfigError: If a variable is missing or has an invalid value.
        """
        source = os.environ if env is None else env

        def get(name: str) -> str | None:
            value = source.get(name, "").strip()
            return value or None

        return cls(
            rest_url=_parse_url("FLECS_REST_URL", get("FLECS_REST_URL")),
            timeout=_parse_timeout("FLECS_REST_TIMEOUT", get("FLECS_REST_TIMEOUT")),
            verify_tls=_parse_bool(
                "FLECS_REST_VERIFY_TLS", get("FLECS_REST_VERIFY_TLS"), default=True
            ),
            allow_mutations=_parse_bool(
                "FLECS_REST_ALLOW_MUTATIONS",
                get("FLECS_REST_ALLOW_MUTATIONS"),
                default=False,
            ),
            log_level=_parse_log_level("MCP_LOG_LEVEL", get("MCP_LOG_LEVEL")),
            transport=_parse_transport("MCP_TRANSPORT", get("MCP_TRANSPORT")),
            host=get("MCP_HOST") or "127.0.0.1",
            port=_parse_port("MCP_PORT", get("MCP_PORT")),
        )


def redact_url(url: str) -> str:
    """Replace the user-info part of ``url`` (``user:password@``) with ``***@``."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=f"***@{host}"))


def configure_logging(level: str) -> None:
    """Configure standard logging for the server process.

    Logs always go to stderr because stdout carries the MCP stdio protocol.
    ``httpx``/``httpcore`` are kept at WARNING: they log every request at INFO,
    including full URLs.
    """
    logging.basicConfig(
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger().setLevel(level)
    logging.getLogger("fastmcp").setLevel(level)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _parse_url(name: str, raw: str | None) -> str:
    if raw is None:
        raise ConfigError(
            f"{name} is not set. Set it to the base URL of the FLECS REST API, "
            f"for example {name}=http://localhost:27750 "
            "(27750 is the default FLECS REST port)."
        )
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ConfigError(
            f"{name} must start with http:// or https://, got {redact_url(raw)!r}."
        )
    if not parts.hostname:
        raise ConfigError(f"{name} must include a host name, got {redact_url(raw)!r}.")
    try:
        _ = parts.port
    except ValueError:
        raise ConfigError(
            f"{name} contains an invalid port, got {redact_url(raw)!r}."
        ) from None
    if parts.query or parts.fragment:
        raise ConfigError(
            f"{name} must not contain a query string or fragment, "
            f"got {redact_url(raw)!r}."
        )
    return raw.rstrip("/")


def _parse_timeout(name: str, raw: str | None) -> float:
    if raw is None:
        return 5.0
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number of seconds, got {raw!r}.") from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be greater than 0, got {raw!r}.")
    return value


def _parse_bool(name: str, raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    value = raw.lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ConfigError(
        f"{name} must be a boolean (true/false, 1/0, yes/no, on/off), got {raw!r}."
    )


def _parse_log_level(name: str, raw: str | None) -> str:
    if raw is None:
        return "INFO"
    value = raw.upper()
    if value not in LOG_LEVELS:
        raise ConfigError(
            f"{name} must be one of {', '.join(LOG_LEVELS)}, got {raw!r}."
        )
    return value


def _parse_transport(name: str, raw: str | None) -> Transport:
    match (raw or "stdio").lower():
        case "stdio":
            return "stdio"
        case "http":
            return "http"
        case _:
            raise ConfigError(f"{name} must be 'stdio' or 'http', got {raw!r}.")


def _parse_port(name: str, raw: str | None) -> int:
    if raw is None:
        return 8000
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from None
    if not 1 <= value <= 65535:
        raise ConfigError(f"{name} must be between 1 and 65535, got {raw!r}.")
    return value
