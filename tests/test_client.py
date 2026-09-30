import logging
import ssl
from typing import Any

import httpx
import pytest

from flecs_mcp import client as client_module
from flecs_mcp.client import (
    FlecsRestClient,
    QueryOptions,
    entity_url_path,
    strip_ansi,
)
from flecs_mcp.config import Config
from flecs_mcp.errors import (
    FlecsConnectionError,
    FlecsError,
    FlecsRequestError,
    FlecsResponseError,
    FlecsTimeoutError,
)
from tests.conftest import FakeFlecs, params_of

EARTH = {"parent": "Sun", "name": "Earth", "tags": ["Planet"], "components": {}}


# ------------------------------------------------------------------ entity paths


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Sun", "Sun"),
        ("Sun.Earth", "Sun/Earth"),
        ("flecs.core.World", "flecs/core/World"),
        ("Earth.USS Enterprise", "Earth/USS%20Enterprise"),
        ("main\\.flecs", "main%5C.flecs"),  # escaped '.' stays inside the name
        ("foo/bar", "foo%5C%2Fbar"),  # literal '/' is escaped for the URL form
        ("#523", "%23523"),
        ("#523.child", "%23523/child"),
        ("vector<flecs.core.i32>", "vector%3Cflecs.core.i32%3E"),  # no split in <>
        ("a+b&c=d?", "a%2Bb%26c%3Dd%3F"),
    ],
)
def test_entity_url_path(path: str, expected: str) -> None:
    assert entity_url_path(path) == expected


@pytest.mark.parametrize("path", ["", ".", "Sun.", ".Sun", "Sun..Earth"])
def test_entity_url_path_rejects_empty_elements(path: str) -> None:
    with pytest.raises(FlecsError, match="Invalid entity path"):
        entity_url_path(path)


def test_strip_ansi_handles_flecs_escaped_color_codes() -> None:
    # FLECS' JSON escaping turns ESC into '['.
    assert strip_ansi("[[0;49m 0. [[[0;37m-1[[0;49m]") == " 0. [-1]"
    assert strip_ansi("\x1b[0;32mok\x1b[0m [x]") == "ok [x]"


@pytest.mark.parametrize(
    ("limit", "offset"), [(0, 0), (1001, 0), (10, -1)], ids=["zero", "big", "neg"]
)
def test_query_options_reject_unbounded_or_invalid_paging(
    limit: int, offset: int
) -> None:
    # A limit of 0 means "no limit" to FLECS, so it must never be sent.
    with pytest.raises(ValueError, match=r"limit|offset"):
        QueryOptions(limit=limit, offset=offset)


# ---------------------------------------------------------------- successes


async def test_get_entity_sends_path_and_serialization_params(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply("GET", "/entity/Sun/Earth", EARTH)

    result = await client.get_entity("Sun.Earth", inherited=True, matches=True)

    assert result == EARTH
    assert str(fake_flecs.last.url).startswith("http://flecs.test:27750/entity/")
    assert params_of(fake_flecs.last) == {
        "values": "true",
        "inherited": "true",
        "type_info": "false",
        "entity_id": "false",
        "doc": "false",
        "matches": "true",
        "full_paths": "true",
    }


async def test_get_component_returns_value_containing_error_member(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    # A component may legitimately have a member called "error".
    fake_flecs.reply("GET", "/component/Sun/Earth", {"error": "none"})

    value = await client.get_component("Sun.Earth", "game.Status")

    assert value == {"error": "none"}
    assert params_of(fake_flecs.last) == {"component": "game.Status"}


async def test_query_encodes_expression_and_paging(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply("GET", "/query", {"results": [EARTH]})
    expr = 'Position, (ChildOf, $p), $this ~= "a+b & c"'

    result = await client.query(expr, QueryOptions(limit=5, offset=10, table=True))

    assert result == {"results": [EARTH]}
    raw_query = fake_flecs.last.url.query.decode()
    assert "%2B" in raw_query  # '+' would otherwise be decoded as a space by FLECS
    assert "%26" in raw_query  # '&' must not split the parameter
    params = params_of(fake_flecs.last)
    assert params["expr"] == expr
    assert params["try"] == "true"
    assert params["limit"] == "5"
    assert params["offset"] == "10"
    assert params["table"] == "true"
    assert params["full_paths"] == "true"


async def test_query_with_empty_result(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply("GET", "/query", {"results": []})

    assert await client.query("Position", QueryOptions()) == {"results": []}


async def test_named_query_sends_name_and_variables(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply("GET", "/query", {"results": []})

    await client.named_query("game.PlanetsQuery", QueryOptions(), "parent:Sun")

    params = params_of(fake_flecs.last)
    assert params["name"] == "game.PlanetsQuery"
    assert params["vars"] == "parent:Sun"
    assert "expr" not in params


async def test_type_info_without_reflection(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    # Documented as 204, implemented as 200 with body "0": handle both.
    fake_flecs.reply("GET", "/type_info/game/Planet", text="0")
    fake_flecs.reply("GET", "/type_info/game/Tag", status=204)
    fake_flecs.reply("GET", "/type_info/game/Position", {"x": ["float"]})

    assert await client.get_type_info("game.Planet") is None
    assert await client.get_type_info("game.Tag") is None
    assert await client.get_type_info("game.Position") == {"x": ["float"]}


async def test_list_queries_strips_color_codes_from_plans(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply("GET", "/queries", [{"name": "Q", "plan": "[[0;49m 0. setids"}])

    assert await client.list_queries() == [{"name": "Q", "plan": " 0. setids"}]


async def test_set_component_encodes_value_as_compact_json(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply("PUT", "/component/Sun/Earth")

    await client.set_component("Sun.Earth", "Position", {"x": 10, "y": 2.5})

    assert fake_flecs.last.method == "PUT"
    assert params_of(fake_flecs.last) == {
        "component": "Position",
        "value": '{"x":10,"y":2.5}',
    }


async def test_set_component_rejects_values_flecs_cannot_receive(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    with pytest.raises(FlecsError, match="not valid JSON"):
        await client.set_component("Sun", "Position", {"x": float("nan")})
    with pytest.raises(FlecsError, match="too large"):
        await client.set_component("Sun", "Position", {"x": "a" * 20_000})
    assert fake_flecs.requests == []


async def test_create_entity_returns_id(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply("PUT", "/entity/Sun/Venus", {"id": "535"})

    assert await client.create_entity("Sun.Venus") == 535


async def test_create_entity_rejects_reply_without_id(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply("PUT", "/entity/Sun", {})

    with pytest.raises(FlecsResponseError, match="missing entity id"):
        await client.create_entity("Sun")


# ------------------------------------------------------------ FLECS errors


async def test_entity_not_found_is_reported_with_dotted_path(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply(
        "GET", "/entity/Sun/Nope", {"error": "entity 'Sun/Nope' not found"}, status=404
    )

    with pytest.raises(FlecsRequestError) as excinfo:
        await client.get_entity("Sun.Nope")

    assert excinfo.value.status_code == 404
    assert str(excinfo.value).startswith("Entity 'Sun.Nope' not found")
    assert excinfo.value.flecs_message == "entity 'Sun/Nope' not found"


async def test_http_4xx_preserves_flecs_error(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply(
        "GET",
        "/component/Sun",
        {"error": "component 'Planet' is not a type"},
        status=400,
    )

    with pytest.raises(FlecsRequestError, match=r"HTTP 400.*'Planet' is not a type"):
        await client.get_component("Sun", "Planet")


async def test_http_5xx(client: FlecsRestClient, fake_flecs: FakeFlecs) -> None:
    fake_flecs.reply("GET", "/entity/Sun", status=500)

    with pytest.raises(FlecsRequestError, match=r"HTTP 500.*Internal Server Error"):
        await client.get_entity("Sun")


async def test_http_503_busy(client: FlecsRestClient, fake_flecs: FakeFlecs) -> None:
    fake_flecs.reply("GET", "/components", status=503)

    with pytest.raises(FlecsRequestError, match="busy"):
        await client.list_components()


async def test_unknown_endpoint_hints_at_missing_addon(
    client: FlecsRestClient,
) -> None:
    with pytest.raises(FlecsRequestError, match=r"HTTP 404.*addon"):
        await client.list_queries()


async def test_error_body_that_is_not_valid_json(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    # FLECS does not escape quotes inside error messages.
    fake_flecs.reply(
        "GET",
        "/component/Sun",
        text='{"error":"unresolved component \'a"b\'"}',
        status=400,
    )
    fake_flecs.reply("GET", "/query", text="Missing parameter 'expr'", status=400)

    with pytest.raises(FlecsRequestError, match="unresolved component 'a\"b'"):
        await client.get_component("Sun", 'a"b')
    with pytest.raises(FlecsRequestError, match="Missing parameter 'expr'"):
        await client.query("x", QueryOptions())


async def test_unusual_error_bodies_are_reported_but_bounded(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    # e.g. an HTML error page from a reverse proxy, or a JSON array
    fake_flecs.reply("GET", "/entity/Big", text="<html>" + "x" * 5000, status=502)
    fake_flecs.reply("GET", "/entity/List", ["unexpected"], status=500)

    with pytest.raises(FlecsRequestError) as excinfo:
        await client.get_entity("Big")
    assert str(excinfo.value).endswith("...")
    assert len(str(excinfo.value)) < 1100
    with pytest.raises(FlecsRequestError, match=r'HTTP 500.*\["unexpected"\]'):
        await client.get_entity("List")


async def test_query_error_reported_in_http_200_body(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    # With try=true FLECS replies 200 and puts the parser error in the body.
    parser_error = "1: expected ',' in pair\nPosition, (ChildOf\n       ^"
    fake_flecs.reply("GET", "/query", {"error": parser_error})

    with pytest.raises(FlecsRequestError) as excinfo:
        await client.query("Position, (ChildOf", QueryOptions())

    assert excinfo.value.flecs_message == parser_error
    assert "expected ',' in pair" in str(excinfo.value)


# ------------------------------------------------------- transport failures


async def test_connection_refused(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.fail(
        "GET",
        "/entity/Sun",
        lambda request: httpx.ConnectError("Connection refused", request=request),
    )

    with pytest.raises(FlecsConnectionError) as excinfo:
        await client.get_entity("Sun")

    message = str(excinfo.value)
    assert (
        "Unable to connect to the FLECS REST API at http://flecs.test:27750" in message
    )
    assert "FLECS_REST_URL" in message
    assert "Connection refused" in message


async def test_dns_failure(client: FlecsRestClient, fake_flecs: FakeFlecs) -> None:
    fake_flecs.fail(
        "GET",
        "/entity/Sun",
        lambda request: httpx.ConnectError("getaddrinfo failed", request=request),
    )

    with pytest.raises(FlecsConnectionError, match="getaddrinfo failed"):
        await client.get_entity("Sun")


async def test_connect_timeout(client: FlecsRestClient, fake_flecs: FakeFlecs) -> None:
    fake_flecs.fail(
        "GET", "/entity/Sun", lambda r: httpx.ConnectTimeout("timed out", request=r)
    )

    with pytest.raises(FlecsTimeoutError, match="while connecting"):
        await client.get_entity("Sun")


async def test_read_timeout_explains_the_main_loop_requirement(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.fail(
        "GET", "/entity/Sun", lambda r: httpx.ReadTimeout("timed out", request=r)
    )

    with pytest.raises(FlecsTimeoutError) as excinfo:
        await client.get_entity("Sun")

    message = str(excinfo.value)
    assert "did not respond within 5 s" in message
    assert "main loop" in message
    assert "FLECS_REST_TIMEOUT" in message


async def test_tls_verification_failure(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    def tls_error(request: httpx.Request) -> Exception:
        try:
            raise ssl.SSLCertVerificationError(1, "certificate verify failed")
        except ssl.SSLError as exc:
            error = httpx.ConnectError("certificate verify failed", request=request)
            error.__cause__ = exc
            return error

    fake_flecs.fail("GET", "/entity/Sun", tls_error)

    with pytest.raises(FlecsConnectionError, match="FLECS_REST_VERIFY_TLS=false"):
        await client.get_entity("Sun")


async def test_other_network_errors(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.fail(
        "GET",
        "/entity/Sun",
        lambda r: httpx.RemoteProtocolError("peer closed connection", request=r),
    )

    with pytest.raises(FlecsConnectionError, match="RemoteProtocolError: peer closed"):
        await client.get_entity("Sun")


# ------------------------------------------------------ unexpected responses


async def test_malformed_json(client: FlecsRestClient, fake_flecs: FakeFlecs) -> None:
    fake_flecs.reply("GET", "/entity/Sun", text='{"name": "Sun"')

    with pytest.raises(FlecsResponseError, match="malformed JSON"):
        await client.get_entity("Sun")


@pytest.mark.parametrize(
    ("body", "description"),
    [([1, 2], "an array"), ("text", "str"), (None, "an empty response")],
)
async def test_unexpected_entity_response(
    client: FlecsRestClient, fake_flecs: FakeFlecs, body: object, description: str
) -> None:
    fake_flecs.reply("GET", "/entity/Sun", body)

    with pytest.raises(
        FlecsResponseError, match=f"expected a JSON object, got {description}"
    ):
        await client.get_entity("Sun")


@pytest.mark.parametrize("body", [{"results": {}}, {"results": [1]}, {}])
async def test_unexpected_query_response(
    client: FlecsRestClient, fake_flecs: FakeFlecs, body: object
) -> None:
    fake_flecs.reply("GET", "/query", body)

    with pytest.raises(FlecsResponseError, match="expected a JSON array"):
        await client.query("Position", QueryOptions())


async def test_unexpected_list_response(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply("GET", "/components", {"name": "Position"})

    with pytest.raises(FlecsResponseError, match="got an object"):
        await client.list_components()


# ------------------------------------------------------------------ stats guard


async def test_stats_are_not_requested_without_the_stats_module(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    # /stats/world would crash a FLECS app that did not import FlecsStats.
    fake_flecs.reply(
        "GET", "/entity/flecs/stats", {"error": "entity not found"}, status=404
    )
    fake_flecs.reply("GET", "/stats/world", {})

    with pytest.raises(FlecsError, match=r"import.*FlecsStats"):
        await client.get_world_stats("1s")

    assert "/stats/world" not in fake_flecs.paths()


async def test_stats_guard_propagates_other_failures(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply("GET", "/entity/flecs/stats", status=500)

    with pytest.raises(FlecsRequestError, match="HTTP 500"):
        await client.get_pipeline_stats("1s")


async def test_stats_with_module(
    client: FlecsRestClient, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply("GET", "/entity/flecs/stats", {"name": "stats"})
    fake_flecs.reply("GET", "/stats/pipeline", [{"name": "Move"}])

    assert await client.get_pipeline_stats("1m", "flecs.pipeline.BuiltinPipeline") == [
        {"name": "Move"}
    ]
    assert params_of(fake_flecs.last) == {
        "period": "1m",
        "name": "flecs.pipeline.BuiltinPipeline",
    }


# ---------------------------------------------------- lifecycle and secrets


async def test_http_client_is_reused_and_closed(
    config: Config, fake_flecs: FakeFlecs, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[httpx.AsyncClient] = []
    real_async_client = httpx.AsyncClient

    def tracking_async_client(**kwargs: Any) -> httpx.AsyncClient:
        instance = real_async_client(**kwargs)
        created.append(instance)
        return instance

    monkeypatch.setattr(client_module.httpx, "AsyncClient", tracking_async_client)
    fake_flecs.reply("GET", "/entity/Sun", {"name": "Sun"})
    rest = FlecsRestClient(config, transport=fake_flecs.transport())

    await rest.get_entity("Sun")
    await rest.get_entity("Sun")
    assert len(created) == 1

    await rest.aclose()
    assert created[0].is_closed

    await rest.get_entity("Sun")  # usable again after closing, with a new pool
    assert len(created) == 2
    await rest.aclose()


async def test_credentials_never_appear_in_errors_or_logs(
    fake_flecs: FakeFlecs, caplog: pytest.LogCaptureFixture
) -> None:
    config = Config(rest_url="http://admin:s3cret@flecs.test:27750")
    fake_flecs.fail(
        "GET", "/entity/Sun", lambda r: httpx.ConnectError("refused", request=r)
    )
    fake_flecs.reply("PUT", "/component/Sun")
    caplog.set_level(logging.DEBUG)

    async with FlecsRestClient(config, transport=fake_flecs.transport()) as rest:
        with pytest.raises(FlecsConnectionError) as excinfo:
            await rest.get_entity("Sun")
        await rest.set_component("Sun", "Secret", {"token": "hunter2"})

    assert "s3cret" not in str(excinfo.value)
    assert "http://***@flecs.test:27750" in str(excinfo.value)
    # Credentials travel as a basic auth header, never inside a URL...
    assert fake_flecs.last.headers["Authorization"].startswith("Basic ")
    assert "s3cret" not in str(fake_flecs.last.url)
    assert "s3cret" not in caplog.text  # ...so no logger can print them.
    own_logs = " | ".join(
        r.getMessage() for r in caplog.records if r.name.startswith("flecs_mcp")
    )
    assert "PUT /component/Sun" in own_logs
    assert "hunter2" not in own_logs  # request values are never logged
