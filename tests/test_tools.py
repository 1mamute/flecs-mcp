"""MCP tools and resources, exercised through an in-memory FastMCP client."""

import json
from typing import Any

import httpx
import pytest
from fastmcp import Client
from mcp.types import TextContent, TextResourceContents

from tests.conftest import FakeFlecs, params_of

READ_TOOLS = {
    "flecs_get_world_info",
    "flecs_get_entity",
    "flecs_get_component",
    "flecs_query",
    "flecs_run_named_query",
    "flecs_explain_query",
    "flecs_get_type_info",
    "flecs_list_components",
    "flecs_list_queries",
    "flecs_get_world_stats",
    "flecs_get_pipeline_stats",
}
MUTATION_TOOLS = {
    "flecs_create_entity",
    "flecs_set_component",
    "flecs_remove_component",
    "flecs_set_enabled",
}

EARTH = {
    "parent": "Sun",
    "name": "Earth",
    "tags": ["game.Planet"],
    "components": {"game.Position": {"x": 10, "y": 20}},
}


async def call(mcp: Client, tool: str, **arguments: Any) -> Any:
    """Call ``tool`` and return its structured result; fail on tool errors."""
    result = await mcp.call_tool(tool, arguments)
    return result.structured_content


async def call_error(mcp: Client, tool: str, **arguments: Any) -> str:
    """Call ``tool``, assert that it failed, and return the error text."""
    result = await mcp.call_tool(tool, arguments, raise_on_error=False)
    assert result.is_error
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


async def read_json(mcp: Client, uri: str) -> Any:
    """Read a resource and decode its JSON text."""
    content = (await mcp.read_resource(uri))[0]
    assert isinstance(content, TextResourceContents)
    return json.loads(content.text)


def stats_module_present(fake_flecs: FakeFlecs) -> None:
    fake_flecs.reply("GET", "/entity/flecs/stats", {"name": "stats"})


# ---------------------------------------------------------------- registration


async def test_only_read_tools_are_registered_by_default(mcp_client: Client) -> None:
    tools = await mcp_client.list_tools()

    assert {tool.name for tool in tools} == READ_TOOLS
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.description is not None
        assert tool.description.startswith("[READ]")


async def test_mutation_tools_are_registered_when_enabled(
    mutating_mcp_client: Client,
) -> None:
    tools = {tool.name: tool for tool in await mutating_mcp_client.list_tools()}

    assert set(tools) == READ_TOOLS | MUTATION_TOOLS
    destructive: set[str] = set()
    for name in MUTATION_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert (tools[name].description or "").startswith("[MUTATION]")
        if annotations.destructive_hint:
            destructive.add(name)
    assert destructive == {"flecs_set_component", "flecs_remove_component"}


async def test_tool_schemas(mcp_client: Client) -> None:
    tools = {tool.name: tool for tool in await mcp_client.list_tools()}

    query = tools["flecs_query"].input_schema
    assert query["required"] == ["query"]
    assert query["additionalProperties"] is False
    assert query["properties"]["query"]["minLength"] == 1
    assert query["properties"]["limit"] == {
        "default": 100,
        "description": "Maximum number of items to return.",
        "maximum": 1000,
        "minimum": 1,
        "type": "integer",
    }
    assert query["properties"]["offset"]["minimum"] == 0

    entity = tools["flecs_get_entity"].input_schema
    assert entity["required"] == ["entity"]
    assert "dotted notation" in entity["properties"]["entity"]["description"]

    stats = tools["flecs_get_world_stats"].input_schema
    assert stats["properties"]["period"]["enum"] == ["1s", "1m", "1h", "1d", "1w"]

    output = tools["flecs_query"].output_schema
    assert output is not None
    assert set(output["required"]) == {"page", "results"}


# ----------------------------------------------------------------- READ tools


async def test_get_entity(mcp_client: Client, fake_flecs: FakeFlecs) -> None:
    fake_flecs.reply("GET", "/entity/Sun/Earth", EARTH)

    result = await call(mcp_client, "flecs_get_entity", entity="Sun.Earth", doc=True)

    assert result == EARTH
    assert params_of(fake_flecs.last)["doc"] == "true"


async def test_get_entity_not_found(mcp_client: Client, fake_flecs: FakeFlecs) -> None:
    fake_flecs.reply(
        "GET", "/entity/Sun/Nope", {"error": "entity 'Sun/Nope' not found"}, status=404
    )

    error = await call_error(mcp_client, "flecs_get_entity", entity="Sun.Nope")

    assert error.startswith("Entity 'Sun.Nope' not found")


async def test_get_component(mcp_client: Client, fake_flecs: FakeFlecs) -> None:
    fake_flecs.reply("GET", "/component/Sun/Earth", {"x": 10, "y": 20})

    result = await call(
        mcp_client, "flecs_get_component", entity="Sun.Earth", component="Position"
    )

    assert result == {
        "entity": "Sun.Earth",
        "component": "Position",
        "value": {"x": 10, "y": 20},
    }


async def test_query(mcp_client: Client, fake_flecs: FakeFlecs) -> None:
    fake_flecs.reply(
        "GET",
        "/query",
        {"results": [EARTH, EARTH], "type_info": {"game.Position": {"x": ["float"]}}},
    )

    result = await call(
        mcp_client,
        "flecs_query",
        query="game.Position, ?game.Mass",
        limit=2,
        offset=4,
        type_info=True,
    )

    assert result == {
        "page": {"offset": 4, "limit": 2, "returned": 2, "may_have_more": True},
        "results": [EARTH, EARTH],
        "type_info": {"game.Position": {"x": ["float"]}},
    }
    params = params_of(fake_flecs.last)
    assert params["expr"] == "game.Position, ?game.Mass"
    assert (params["limit"], params["offset"], params["type_info"]) == (
        "2",
        "4",
        "true",
    )


async def test_query_with_empty_result(
    mcp_client: Client, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply("GET", "/query", {"results": []})

    result = await call(
        mcp_client, "flecs_query", query="game.Position, !game.Position"
    )

    assert result == {
        "page": {"offset": 0, "limit": 100, "returned": 0, "may_have_more": False},
        "results": [],
    }


async def test_invalid_query_returns_flecs_parser_error(
    mcp_client: Client, fake_flecs: FakeFlecs
) -> None:
    parser_error = "1: expected ',' in pair\ngame.Position, (ChildOf\n       ^"
    fake_flecs.reply("GET", "/query", {"error": parser_error})

    error = await call_error(mcp_client, "flecs_query", query="game.Position, (ChildOf")

    assert parser_error in error


async def test_network_failure(mcp_client: Client, fake_flecs: FakeFlecs) -> None:
    fake_flecs.fail(
        "GET", "/query", lambda r: httpx.ConnectError("Connection refused", request=r)
    )

    error = await call_error(mcp_client, "flecs_query", query="Position")

    assert "Unable to connect to the FLECS REST API at http://flecs.test:27750" in error


@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("flecs_query", {"query": ""}, "at least 1 character"),
        (
            "flecs_query",
            {"query": "Position", "limit": 0},
            "greater than or equal to 1",
        ),
        (
            "flecs_query",
            {"query": "Position", "limit": 5000},
            "less than or equal to 1000",
        ),
        (
            "flecs_query",
            {"query": "Position", "offset": -1},
            "greater than or equal to 0",
        ),
        ("flecs_query", {"query": "x" * 20_000}, "at most 16384 characters"),
        ("flecs_get_entity", {}, "Missing required argument"),
        (
            "flecs_get_entity",
            {"entity": "Sun", "bogus": 1},
            "Unexpected keyword argument",
        ),
        ("flecs_get_world_stats", {"period": "2s"}, "Input should be"),
        ("flecs_get_entity", {"entity": "Sun..Earth"}, "Invalid entity path"),
    ],
)
async def test_malformed_input_is_rejected_before_calling_flecs(
    mcp_client: Client,
    fake_flecs: FakeFlecs,
    tool: str,
    arguments: dict[str, Any],
    message: str,
) -> None:
    error = await call_error(mcp_client, tool, **arguments)

    assert message in error
    assert fake_flecs.requests == []


async def test_run_named_query(mcp_client: Client, fake_flecs: FakeFlecs) -> None:
    fake_flecs.reply("GET", "/query", {"results": [EARTH]})

    result = await call(
        mcp_client,
        "flecs_run_named_query",
        name="game.PlanetsQuery",
        variables="parent:Sun",
        table=True,
    )

    assert result["results"] == [EARTH]
    params = params_of(fake_flecs.last)
    assert (params["name"], params["vars"], params["table"]) == (
        "game.PlanetsQuery",
        "parent:Sun",
        "true",
    )


async def test_explain_query(mcp_client: Client, fake_flecs: FakeFlecs) -> None:
    fake_flecs.reply(
        "GET",
        "/query",
        {
            "query_info": {"vars": ["this"], "terms": []},
            "field_info": [],
            "query_plan": "[[0;49m 0. [[[0;37m-1[[0;49m]  setids\n",
        },
    )

    result = await call(mcp_client, "flecs_explain_query", query="Position")

    assert result["query_plan"] == " 0. [-1]  setids\n"
    params = params_of(fake_flecs.last)
    assert params["results"] == "false"
    assert (
        params["query_plan"] == params["query_info"] == params["field_info"] == "true"
    )
    assert params["query_profile"] == "false"


async def test_get_type_info(mcp_client: Client, fake_flecs: FakeFlecs) -> None:
    fake_flecs.reply("GET", "/type_info/game/Mass", {"value": ["float"]})
    fake_flecs.reply("GET", "/type_info/game/Planet", text="0")

    mass = await call(mcp_client, "flecs_get_type_info", component="game.Mass")
    planet = await call(mcp_client, "flecs_get_type_info", component="game.Planet")

    assert mass == {
        "component": "game.Mass",
        "has_reflection": True,
        "schema": {"value": ["float"]},
    }
    assert planet == {
        "component": "game.Planet",
        "has_reflection": False,
        "schema": None,
    }


async def test_list_components_filters_and_pages(
    mcp_client: Client, fake_flecs: FakeFlecs
) -> None:
    components = [
        {"name": n} for n in ["game.Position", "game.Mass", "flecs.core.Name"]
    ]
    fake_flecs.reply("GET", "/components", components)

    result = await call(
        mcp_client, "flecs_list_components", name_contains="GAME", limit=1, offset=1
    )

    assert result == {
        "total": 2,
        "offset": 1,
        "limit": 1,
        "components": [{"name": "game.Mass"}],
    }


async def test_list_queries_filters_kind_and_omits_plans(
    mcp_client: Client, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply(
        "GET",
        "/queries",
        [
            {"name": "game.Move", "kind": "System", "plan": "p", "cache_plan": "c"},
            {"name": "game.Q", "kind": "Query", "plan": "p"},
        ],
    )

    compact = await call(mcp_client, "flecs_list_queries", kind="System")
    full = await call(mcp_client, "flecs_list_queries", include_plans=True)

    assert compact["queries"] == [{"name": "game.Move", "kind": "System"}]
    assert compact["total"] == 1
    assert full["queries"][0]["plan"] == "p"


async def test_world_info(mcp_client: Client, fake_flecs: FakeFlecs) -> None:
    def component(request: httpx.Request) -> httpx.Response:
        if params_of(request)["component"] == "flecs.core.BuildInfo":
            return httpx.Response(200, json={"version": "4.1.6"})
        return httpx.Response(400, json={"error": "unresolved component"})

    fake_flecs.on("GET", "/component/flecs/core/World", component)

    result = await call(mcp_client, "flecs_get_world_info")

    assert result["rest_url"] == "http://flecs.test:27750"
    assert result["build_info"] == {"version": "4.1.6"}
    assert result["world_summary"] is None
    assert len(result["notes"]) == 1
    assert "world_summary unavailable: unresolved component" in result["notes"][0]
    assert "FlecsStats" in result["notes"][0]


async def test_world_info_fails_when_flecs_is_unreachable(
    mcp_client: Client, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.fail(
        "GET",
        "/component/flecs/core/World",
        lambda r: httpx.ConnectError("refused", request=r),
    )

    error = await call_error(mcp_client, "flecs_get_world_info")

    assert "Unable to connect" in error


async def test_world_stats_latest_sample_and_history(
    mcp_client: Client, fake_flecs: FakeFlecs
) -> None:
    stats_module_present(fake_flecs)
    metric = {"avg": [1.0, 2.0, 3.0], "min": [0.5, 1.5, 2.5], "max": [], "brief": "FPS"}
    fake_flecs.reply("GET", "/stats/world", {"performance.fps": metric})

    latest = await call(mcp_client, "flecs_get_world_stats", period="1m")
    history = await call(mcp_client, "flecs_get_world_stats", history=True)

    assert latest == {
        "period": "1m",
        "history": False,
        "metrics": {
            "performance.fps": {"avg": 3.0, "min": 2.5, "max": None, "brief": "FPS"}
        },
    }
    assert history["metrics"] == {"performance.fps": metric}
    assert params_of(fake_flecs.last)["period"] == "1s"


async def test_world_stats_without_stats_module(
    mcp_client: Client, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply("GET", "/entity/flecs/stats", {"error": "not found"}, status=404)

    error = await call_error(mcp_client, "flecs_get_world_stats")

    assert "stats module" in error
    assert "/stats/world" not in fake_flecs.paths()


async def test_pipeline_stats(mcp_client: Client, fake_flecs: FakeFlecs) -> None:
    stats_module_present(fake_flecs)
    fake_flecs.reply(
        "GET",
        "/stats/pipeline",
        [
            {"name": "game.Move", "disabled": False, "time_spent": {"avg": [1, 2]}},
            {"multi_threaded": False, "commands_enqueued": {"avg": [0, 4]}},
        ],
    )

    result = await call(
        mcp_client,
        "flecs_get_pipeline_stats",
        pipeline="flecs.pipeline.BuiltinPipeline",
    )

    assert result["entries"] == [
        {"name": "game.Move", "disabled": False, "time_spent": {"avg": 2}},
        {"multi_threaded": False, "commands_enqueued": {"avg": 4}},
    ]
    assert params_of(fake_flecs.last)["name"] == "flecs.pipeline.BuiltinPipeline"


# -------------------------------------------------------------- MUTATION tools


async def test_mutation_tools_are_not_callable_by_default(
    mcp_client: Client, fake_flecs: FakeFlecs
) -> None:
    error = await call_error(
        mcp_client, "flecs_set_component", entity="Sun", component="Position"
    )

    assert "Unknown tool" in error
    assert fake_flecs.requests == []


async def test_create_entity(
    mutating_mcp_client: Client, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply("PUT", "/entity/Sun/Venus", {"id": "535"})

    result = await call(mutating_mcp_client, "flecs_create_entity", entity="Sun.Venus")

    assert result == {"entity": "Sun.Venus", "id": 535}


async def test_set_component(
    mutating_mcp_client: Client, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply("PUT", "/component/Sun/Venus")

    set_result = await call(
        mutating_mcp_client,
        "flecs_set_component",
        entity="Sun.Venus",
        component="game.Position",
        value={"x": 5},
    )
    set_params = params_of(fake_flecs.last)
    add_result = await call(
        mutating_mcp_client,
        "flecs_set_component",
        entity="Sun.Venus",
        component="game.Planet",
    )

    assert set_result["status"] == "set"
    assert set_params == {"component": "game.Position", "value": '{"x":5}'}
    assert add_result["status"] == "added"
    assert params_of(fake_flecs.last) == {"component": "game.Planet"}


async def test_set_component_invalid_value(
    mutating_mcp_client: Client, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply(
        "PUT",
        "/component/Sun",
        {"error": "invalid value for component 'game.Position'"},
        status=400,
    )

    error = await call_error(
        mutating_mcp_client,
        "flecs_set_component",
        entity="Sun",
        component="game.Position",
        value={"x": "oops"},
    )

    assert "invalid value for component 'game.Position'" in error


async def test_remove_component(
    mutating_mcp_client: Client, fake_flecs: FakeFlecs
) -> None:
    fake_flecs.reply("DELETE", "/component/Sun/Venus")

    result = await call(
        mutating_mcp_client,
        "flecs_remove_component",
        entity="Sun.Venus",
        component="game.Planet",
    )

    assert result["status"] == "removed"
    assert fake_flecs.last.method == "DELETE"


async def test_set_enabled(mutating_mcp_client: Client, fake_flecs: FakeFlecs) -> None:
    fake_flecs.reply("PUT", "/toggle/game/Move")

    result = await call(
        mutating_mcp_client, "flecs_set_enabled", entity="game.Move", enabled=False
    )
    params = params_of(fake_flecs.last)
    await call(
        mutating_mcp_client,
        "flecs_set_enabled",
        entity="game.Move",
        enabled=True,
        component="game.Velocity",
    )

    assert result == {"entity": "game.Move", "component": None, "status": "disabled"}
    assert params == {"enable": "false"}
    assert params_of(fake_flecs.last) == {
        "enable": "true",
        "component": "game.Velocity",
    }


# ------------------------------------------------------------------ resources


async def test_resources(mcp_client: Client, fake_flecs: FakeFlecs) -> None:
    fake_flecs.reply("GET", "/component/flecs/core/World", {"version": "4.1.6"})
    fake_flecs.reply("GET", "/type_info/game/Position", {"x": ["float"]})

    resources = await mcp_client.list_resources()
    templates = await mcp_client.list_resource_templates()
    build_info = await read_json(mcp_client, "flecs://build-info")
    type_info = await read_json(mcp_client, "flecs://type-info/game.Position")

    assert [str(r.uri) for r in resources] == ["flecs://build-info"]
    assert [t.uri_template for t in templates] == ["flecs://type-info/{component}"]
    assert build_info == {"version": "4.1.6"}
    assert type_info == {
        "component": "game.Position",
        "has_reflection": True,
        "schema": {"x": ["float"]},
    }
