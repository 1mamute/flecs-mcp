"""Asynchronous client for the FLECS REST API.

This module is the only place that knows about the FLECS REST protocol: URL
layout, parameter names, response shapes and error conventions. It targets the
REST API of flecs v4 and was verified against ``src/addons/rest.c`` and
``docs/FlecsRemoteApi.md`` at flecs commit ``9e874bc``.

Only explicitly implemented operations are available; there is intentionally no
public method for issuing arbitrary HTTP requests.
"""

import json
import logging
import re
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Self, cast
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import httpx

from flecs_mcp.config import Config
from flecs_mcp.errors import (
    FlecsConnectionError,
    FlecsError,
    FlecsRequestError,
    FlecsResponseError,
    FlecsTimeoutError,
)

logger = logging.getLogger(__name__)

type JsonObject = dict[str, Any]
type Method = Literal["GET", "PUT", "DELETE"]

#: Upper bound for query expressions and JSON component values. FLECS reads a
#: request into a single 64 KiB buffer, and both travel in the request line.
MAX_EXPRESSION_LENGTH = 16 * 1024

#: Default and maximum page size for query results (FLECS' own default is 1000).
DEFAULT_QUERY_LIMIT = 100
MAX_QUERY_LIMIT = 1000

#: Sampling periods supported by the FLECS stats addon.
type StatsPeriod = Literal["1s", "1m", "1h", "1d", "1w"]

WORLD_ENTITY = "flecs.core.World"
BUILD_INFO_COMPONENT = "flecs.core.BuildInfo"
WORLD_SUMMARY_COMPONENT = "flecs.stats.WorldSummary"
STATS_MODULE_URL_PATH = "flecs/stats"

# Terminal color codes. FLECS' JSON string escaping replaces the ESC character
# with '[', so "\x1b[0;49m" arrives as "[[0;49m".
_ANSI_ESCAPE_RE = re.compile(r"(?:\x1b\[|\[\[)[0-9;]*m")
# FLECS does not JSON-escape error messages, so an error body can be invalid
# JSON (for example when the message quotes a name containing '"').
_RAW_ERROR_RE = re.compile(r'\{\s*"error"\s*:\s*"(.*)"\s*\}', re.DOTALL)
_MAX_ERROR_TEXT = 1000


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryOptions:
    """Paging and serialization options for query results.

    Field names match the FLECS REST parameters of ``GET /query``. ``limit``
    must be at least 1 because FLECS treats a limit of 0 as "no limit".
    """

    limit: int = DEFAULT_QUERY_LIMIT
    offset: int = 0
    table: bool = False
    values: bool = True
    fields: bool = True
    entity_ids: bool = False
    inherited: bool = False
    type_info: bool = False
    doc: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_QUERY_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_QUERY_LIMIT}")
        if self.offset < 0:
            raise ValueError("offset must not be negative")

    def to_params(self) -> dict[str, str]:
        return {
            "limit": str(self.limit),
            "offset": str(self.offset),
            "table": _flag(self.table),
            "values": _flag(self.values),
            "fields": _flag(self.fields),
            "entity_ids": _flag(self.entity_ids),
            "inherited": _flag(self.inherited),
            "type_info": _flag(self.type_info),
            "doc": _flag(self.doc),
            "full_paths": "true",
        }


def entity_url_path(path: str) -> str:
    """Convert an entity path in FLECS dotted notation to its REST URL form.

    FLECS prints entity paths with ``.`` separators (``Sun.Earth``) and escapes
    a literal separator inside a name with a backslash (``main\\.flecs``). The
    REST API expects ``/`` separators instead. Elements are percent-encoded;
    FLECS decodes them before resolving the path, and still honours the
    backslash escapes. Numeric ids (``#123``) are passed through.

    Examples: ``Sun.Earth`` -> ``Sun/Earth``, ``foo/bar`` -> ``foo%5C%2Fbar``.

    Raises:
        FlecsError: If the path is empty or contains an empty element.
    """
    elements: list[str] = []
    current: list[str] = []
    template_depth = 0  # FLECS does not split on separators inside <...>
    chars = iter(path)
    for ch in chars:
        if ch == "\\":
            current.append(ch + next(chars, ""))
        elif ch == "." and template_depth == 0:
            elements.append("".join(current))
            current = []
        elif ch == "/":
            current.append("\\/")
        else:
            if ch == "<":
                template_depth += 1
            elif ch == ">" and template_depth > 0:
                template_depth -= 1
            current.append(ch)
    elements.append("".join(current))
    if not all(elements):
        raise FlecsError(
            f"Invalid entity path {path!r}: use FLECS dotted notation such as "
            "'Sun.Earth' or a numeric id such as '#123'."
        )
    return "/".join(quote(element, safe="") for element in elements)


def strip_ansi(text: str) -> str:
    """Remove terminal color codes (FLECS colors query plans)."""
    return _ANSI_ESCAPE_RE.sub("", text)


class FlecsRestClient:
    """Async client for the FLECS REST API.

    A single :class:`httpx.AsyncClient` is created on first use and reused for
    all requests until :meth:`aclose` is called. The client can be used again
    after closing; a new connection pool is then created.
    """

    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    @property
    def display_url(self) -> str:
        """The configured REST URL without credentials."""
        return self._config.display_url

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        if self._http is not None:
            http, self._http = self._http, None
            await http.aclose()
            logger.debug("Closed HTTP client for %s", self.display_url)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ READ

    async def get_entity(
        self,
        entity: str,
        *,
        values: bool = True,
        inherited: bool = False,
        type_info: bool = False,
        entity_id: bool = False,
        doc: bool = False,
        matches: bool = False,
    ) -> JsonObject:
        """``GET /entity/<path>``: tags, pairs and components of an entity."""
        params = {
            "values": _flag(values),
            "inherited": _flag(inherited),
            "type_info": _flag(type_info),
            "entity_id": _flag(entity_id),
            "doc": _flag(doc),
            "matches": _flag(matches),
            "full_paths": "true",
        }
        data = await self._entity_request("GET", "entity", entity, params)
        return _expect_object(data, "entity")

    async def get_component(self, entity: str, component: str) -> Any:
        """``GET /component/<path>?component=<id>``: one component value."""
        params = {"component": component}
        return await self._entity_request("GET", "component", entity, params)

    async def get_type_info(self, component: str) -> Any | None:
        """``GET /type_info/<path>``: reflection schema of a component type.

        Returns ``None`` when the type has no reflection data. The FLECS docs
        state that the endpoint replies 204 in that case; the implementation
        replies 200 with the body ``0``. Both are handled.
        """
        data = await self._entity_request("GET", "type_info", component)
        if data is None or (type(data) is int and data == 0):
            return None
        return data

    async def query(self, expr: str, options: QueryOptions) -> JsonObject:
        """``GET /query?expr=...``: evaluate an ad-hoc query expression."""
        params = {"expr": expr, "try": "true", **options.to_params()}
        data = await self._request("GET", "query", params, error_in_body=True)
        return _expect_query_result(data)

    async def named_query(
        self, name: str, options: QueryOptions, variables: str | None = None
    ) -> JsonObject:
        """``GET /query?name=...``: evaluate an existing query, system or observer."""
        params = {"name": name, "try": "true", **options.to_params()}
        if variables:
            params["vars"] = variables
        data = await self._request("GET", "query", params, error_in_body=True)
        return _expect_query_result(data)

    async def explain_query(self, expr: str, *, profile: bool = False) -> JsonObject:
        """Query metadata (terms, fields, plan) without evaluating results."""
        params = {
            "expr": expr,
            "try": "true",
            "results": "false",
            "query_info": "true",
            "field_info": "true",
            "query_plan": "true",
            "query_profile": _flag(profile),
            "full_paths": "true",
        }
        data = await self._request("GET", "query", params, error_in_body=True)
        result = _expect_object(data, "query")
        plan = result.get("query_plan")
        if isinstance(plan, str):
            result["query_plan"] = strip_ansi(plan)
        return result

    async def list_components(self) -> list[JsonObject]:
        """``GET /components``: component records with storage and trait info."""
        return _expect_list(await self._request("GET", "components"), "components")

    async def list_queries(self) -> list[JsonObject]:
        """``GET /queries``: named queries, systems and observers.

        Note that FLECS evaluates every query to report its match counts.
        """
        queries = _expect_list(await self._request("GET", "queries"), "queries")
        for entry in queries:
            for key in ("plan", "cache_plan"):
                value = entry.get(key)
                if isinstance(value, str):
                    entry[key] = strip_ansi(value)
        return queries

    async def get_build_info(self) -> Any:
        """The ``flecs.core.BuildInfo`` component of the world entity."""
        return await self.get_component(WORLD_ENTITY, BUILD_INFO_COMPONENT)

    async def get_world_summary(self) -> Any:
        """The ``flecs.stats.WorldSummary`` component (requires FlecsStats)."""
        return await self.get_component(WORLD_ENTITY, WORLD_SUMMARY_COMPONENT)

    async def get_world_stats(self, period: StatsPeriod) -> JsonObject:
        """``GET /stats/world``: world metrics over a sampling window."""
        await self._ensure_stats_module()
        data = await self._request("GET", "stats/world", {"period": period})
        return _expect_object(data, "stats/world")

    async def get_pipeline_stats(
        self, period: StatsPeriod, pipeline: str | None = None
    ) -> list[JsonObject]:
        """``GET /stats/pipeline``: per-system (and sync point) metrics."""
        await self._ensure_stats_module()
        params = {"period": period}
        if pipeline:
            params["name"] = pipeline
        data = await self._request("GET", "stats/pipeline", params)
        return _expect_list(data, "stats/pipeline")

    # -------------------------------------------------------------- MUTATION

    async def create_entity(self, entity: str) -> int:
        """``PUT /entity/<path>``: create an entity (returns the existing one if
        the path already exists). Returns the entity id reported by FLECS."""
        data = await self._entity_request("PUT", "entity", entity)
        entity_id = _expect_object(data, "entity").get("id")
        if not isinstance(entity_id, str | int) or not str(entity_id).isdigit():
            raise FlecsResponseError(
                "Unexpected response from PUT /entity: missing entity id."
            )
        logger.info("Created entity %s", entity)
        return int(entity_id)

    async def set_component(
        self, entity: str, component: str, value: Any | None = None
    ) -> None:
        """``PUT /component/<path>``: add a component, and set its value when
        ``value`` is given. FLECS starts from the current value, so members
        that are not present in ``value`` keep their current value."""
        params = {"component": component}
        if value is not None:
            try:
                encoded = json.dumps(value, separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise FlecsError(f"Component value is not valid JSON: {exc}") from exc
            if len(encoded) > MAX_EXPRESSION_LENGTH:
                raise FlecsError(
                    f"Component value is too large ({len(encoded)} characters of "
                    f"JSON, maximum {MAX_EXPRESSION_LENGTH})."
                )
            params["value"] = encoded
        await self._entity_request("PUT", "component", entity, params)
        logger.info("Set component %s on entity %s", component, entity)

    async def remove_component(self, entity: str, component: str) -> None:
        """``DELETE /component/<path>``: remove a component, tag or pair."""
        params = {"component": component}
        await self._entity_request("DELETE", "component", entity, params)
        logger.info("Removed component %s from entity %s", component, entity)

    async def set_enabled(
        self, entity: str, *, enabled: bool, component: str | None = None
    ) -> None:
        """``PUT /toggle/<path>``: enable/disable an entity or a component."""
        params = {"enable": _flag(enabled)}
        if component:
            params["component"] = component
        await self._entity_request("PUT", "toggle", entity, params)
        target = f"component {component} of entity {entity}" if component else entity
        logger.info("%s %s", "Enabled" if enabled else "Disabled", target)

    # -------------------------------------------------------------- internals

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            base_url, auth = _split_credentials(self._config.rest_url)
            self._http = httpx.AsyncClient(
                base_url=f"{base_url}/",
                auth=auth,
                timeout=self._config.timeout,
                verify=self._config.verify_tls,
                transport=self._transport,
                headers={"Accept": "application/json"},
            )
            logger.debug("Created HTTP client for %s", self.display_url)
        return self._http

    async def _entity_request(
        self,
        method: Method,
        endpoint: str,
        entity: str,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        """Request ``<endpoint>/<entity path>``.

        FLECS reports unknown entities with the URL form of the path
        ('Sun/Earth'); the error is rephrased with the dotted path the caller
        used so that it is not mistaken for the expected notation.
        """
        path = f"{endpoint}/{entity_url_path(entity)}"
        try:
            return await self._request(method, path, params)
        except FlecsRequestError as exc:
            if exc.status_code != 404 or not exc.flecs_message:
                raise
            raise FlecsRequestError(
                f"Entity {entity!r} not found (FLECS: {exc.flecs_message}).",
                status_code=exc.status_code,
                flecs_message=exc.flecs_message,
            ) from exc

    async def _ensure_stats_module(self) -> None:
        """Fail cleanly when the FlecsStats module is not imported.

        In flecs 9e874bc, ``GET /stats/*`` dereferences the world's stats
        component without a NULL check, so calling it on a world without the
        FlecsStats module crashes the FLECS application. Never do that.
        """
        try:
            await self._request(
                "GET", f"entity/{STATS_MODULE_URL_PATH}", {"values": "false"}
            )
        except FlecsRequestError as exc:
            if exc.status_code != 404:
                raise
            raise FlecsError(
                "Statistics are not available: the FLECS application has not "
                "imported the stats module (flecs.stats). Import it with "
                "ECS_IMPORT(world, FlecsStats) in C, world.import<flecs::stats>() "
                "in C++, or enable_stats in the app addon."
            ) from exc

    async def _request(
        self,
        method: Method,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        error_in_body: bool = False,
    ) -> Any:
        """Send a request and return the decoded JSON body (``None`` if empty).

        ``error_in_body`` treats a JSON object with an ``error`` member as a
        failure even on HTTP 200, which FLECS uses for queries sent with
        ``try=true``. It must not be used for component values, which may
        legitimately contain an ``error`` member.
        """
        endpoint = f"{method} /{path}"
        started = time.perf_counter()
        try:
            response = await self._client().request(method, path, params=params)
        except httpx.ConnectTimeout as exc:
            raise FlecsTimeoutError(
                f"Timed out after {self._config.timeout:g} s while connecting to the "
                f"FLECS REST API at {self.display_url}. Verify that the FLECS "
                "application is running with the REST API enabled, that "
                "FLECS_REST_URL (host and port, default port 27750) is correct and "
                "that no firewall blocks the connection."
            ) from exc
        except httpx.TimeoutException as exc:
            raise FlecsTimeoutError(
                f"The FLECS REST API at {self.display_url} did not respond within "
                f"{self._config.timeout:g} s ({endpoint}). FLECS only answers REST "
                "requests while the application runs its main loop "
                "(ecs_progress / world.progress()); check that it is not paused, "
                "blocked in a debugger or stuck in a long frame. For large "
                "requests, increase FLECS_REST_TIMEOUT."
            ) from exc
        except httpx.RequestError as exc:
            raise FlecsConnectionError(self._describe_request_error(exc)) from exc

        logger.debug(
            "%s params=%s -> %d (%.1f ms)",
            endpoint,
            sorted(params or {}),
            response.status_code,
            (time.perf_counter() - started) * 1000,
        )
        return _decode_response(response, endpoint, error_in_body=error_in_body)

    def _describe_request_error(self, exc: httpx.RequestError) -> str:
        detail = str(exc) or type(exc).__name__
        if _is_tls_error(exc):
            return (
                f"TLS error while connecting to the FLECS REST API at "
                f"{self.display_url}: {detail}. If the server uses a self-signed "
                "certificate, set FLECS_REST_VERIFY_TLS=false (trusted networks only)."
            )
        if isinstance(exc, httpx.ConnectError):
            return (
                f"Unable to connect to the FLECS REST API at {self.display_url}. "
                "Verify that the FLECS application is running with the REST API "
                "enabled and that FLECS_REST_URL (host and port, default port "
                f"27750) is correct. Details: {detail}"
            )
        return (
            f"Network error while communicating with the FLECS REST API at "
            f"{self.display_url}: {type(exc).__name__}: {detail}"
        )


def _decode_response(
    response: httpx.Response, endpoint: str, *, error_in_body: bool
) -> Any:
    status = response.status_code
    text = response.text
    if not response.is_success:
        flecs_message = _extract_error_message(text)
        if flecs_message is not None:
            detail = flecs_message
        elif status == 404:
            detail = (
                "endpoint not found. The FLECS build may not include the addon "
                "that serves it."
            )
        elif status == 503:
            detail = "the FLECS HTTP server is busy (send queue full); retry shortly."
        else:
            detail = response.reason_phrase or "no details provided"
        raise FlecsRequestError(
            f"FLECS REST API returned HTTP {status} for {endpoint}: {detail}",
            status_code=status,
            flecs_message=flecs_message,
        )

    if not text.strip():
        return None
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FlecsResponseError(
            f"The FLECS REST API returned malformed JSON for {endpoint}: {exc}"
        ) from exc

    if error_in_body:
        message = _error_member(data)
        if message is not None:
            raise FlecsRequestError(
                f"FLECS rejected {endpoint}: {message}",
                status_code=status,
                flecs_message=message,
            )
    return data


def _extract_error_message(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        data: Any = json.loads(stripped)
    except json.JSONDecodeError:
        match = _RAW_ERROR_RE.fullmatch(stripped)
        return match.group(1) if match else _truncate(stripped)
    return _error_member(data) or _truncate(stripped)


def _error_member(data: Any) -> str | None:
    """The ``error`` member FLECS uses to report a failure, if ``data`` has one.

    Query replies that carry ``results`` are never errors.
    """
    if not isinstance(data, dict):
        return None
    obj = cast(JsonObject, data)
    error = obj.get("error")
    if "results" in obj or not isinstance(error, str):
        return None
    return error


def _expect_object(data: Any, path: str) -> JsonObject:
    if not isinstance(data, dict):
        raise FlecsResponseError(
            f"Unexpected response from /{path}: expected a JSON object, "
            f"got {_json_type(data)}."
        )
    return cast(JsonObject, data)


def _expect_list(data: Any, path: str) -> list[JsonObject]:
    if isinstance(data, list):
        items = cast(list[Any], data)
        if all(isinstance(item, dict) for item in items):
            return cast(list[JsonObject], items)
    raise FlecsResponseError(
        f"Unexpected response from /{path}: expected a JSON array of objects, "
        f"got {_json_type(data)}."
    )


def _expect_query_result(data: Any) -> JsonObject:
    """Validate a ``GET /query`` reply: an object whose ``results`` (present
    because ``results`` serialization is enabled) is an array of objects."""
    result = _expect_object(data, "query")
    result["results"] = _expect_list(result.get("results"), "query (results)")
    return result


def _json_type(data: Any) -> str:
    match data:
        case None:
            return "an empty response"
        case dict():
            return "an object"
        case list():
            return "an array"
        case _:
            return type(data).__name__


def _split_credentials(url: str) -> tuple[str, httpx.BasicAuth | None]:
    """Move ``user:password@`` out of ``url`` into an explicit basic auth.

    FLECS itself has no authentication, but it may sit behind a reverse proxy
    that does. Keeping credentials out of the URL keeps them out of every URL
    that httpx or this module logs or reports.
    """
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url, None
    auth = httpx.BasicAuth(unquote(parts.username or ""), unquote(parts.password or ""))
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=host)), auth


def _is_tls_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _truncate(text: str) -> str:
    if len(text) <= _MAX_ERROR_TEXT:
        return text
    return text[:_MAX_ERROR_TEXT] + "..."


def _flag(value: bool) -> str:
    return "true" if value else "false"
