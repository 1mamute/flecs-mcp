"""Application-level errors raised while talking to the FLECS REST API.

All errors derive from :class:`fastmcp.exceptions.FastMCPError`. FastMCP
forwards the message of such errors to the MCP client verbatim (it never masks
them) and logs them at the error's ``log_level`` without a traceback. That is
exactly what we want for expected failures such as "entity not found" or an
invalid query: the agent gets an actionable message and the server log stays
readable.
"""

import logging

from fastmcp.exceptions import FastMCPError


class FlecsError(FastMCPError):
    """Base class for all errors related to the FLECS REST API."""

    def __init__(self, message: str, *, log_level: int = logging.WARNING) -> None:
        super().__init__(message, log_level=log_level)


class FlecsConnectionError(FlecsError):
    """The FLECS REST API could not be reached (refused, DNS, TLS, network)."""


class FlecsTimeoutError(FlecsConnectionError):
    """The FLECS REST API did not answer within the configured timeout."""


class FlecsRequestError(FlecsError):
    """FLECS processed the request and rejected it.

    ``flecs_message`` holds the error text reported by FLECS (for example a
    query parse error), or ``None`` when FLECS did not send one.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        flecs_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.flecs_message = flecs_message


class FlecsResponseError(FlecsError):
    """FLECS returned a response that could not be interpreted."""

    def __init__(self, message: str) -> None:
        super().__init__(message, log_level=logging.ERROR)
