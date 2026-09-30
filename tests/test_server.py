import os
import sys
from typing import Any

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport

from flecs_mcp import __version__, server
from flecs_mcp.config import Config
from flecs_mcp.server import create_server
from tests.conftest import REST_URL, FakeFlecs


async def test_server_metadata() -> None:
    mcp = create_server(Config(rest_url=REST_URL))

    assert mcp.name == "flecs"
    assert mcp.instructions is not None
    assert "flecs_query" in mcp.instructions
    assert "FLECS_REST_ALLOW_MUTATIONS" in mcp.instructions


async def test_tool_call_reaches_the_rest_api(fake_flecs: FakeFlecs) -> None:
    fake_flecs.reply("GET", "/entity/Sun", {"name": "Sun"})
    mcp = create_server(Config(rest_url=REST_URL), transport=fake_flecs.transport())

    async with Client(mcp) as client:
        result = await client.call_tool("flecs_get_entity", {"entity": "Sun"})

    assert result.structured_content == {"name": "Sun"}
    assert [str(r.url).split("?")[0] for r in fake_flecs.requests] == [
        "http://flecs.test:27750/entity/Sun"
    ]


async def test_http_client_is_closed_on_shutdown(
    fake_flecs: FakeFlecs, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[httpx.AsyncClient] = []
    real_async_client = httpx.AsyncClient

    def tracking_async_client(**kwargs: Any) -> httpx.AsyncClient:
        created.append(real_async_client(**kwargs))
        return created[-1]

    monkeypatch.setattr("flecs_mcp.client.httpx.AsyncClient", tracking_async_client)
    fake_flecs.reply("GET", "/entity/Sun", {"name": "Sun"})
    mcp = create_server(Config(rest_url=REST_URL), transport=fake_flecs.transport())

    for _ in range(2):  # the server can be started again after a shutdown
        async with Client(mcp) as client:
            await client.call_tool("flecs_get_entity", {"entity": "Sun"})
            await client.call_tool("flecs_get_entity", {"entity": "Sun"})
            assert not created[-1].is_closed

    assert len(created) == 2  # one pool per server run, reused across calls
    assert all(http.is_closed for http in created)


def test_main_reports_configuration_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("FLECS_REST_URL", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        server.main()

    assert excinfo.value.code == 2
    assert "FLECS_REST_URL is not set" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, {"transport": "stdio", "show_banner": False}),
        (
            {"MCP_TRANSPORT": "http", "MCP_HOST": "0.0.0.0", "MCP_PORT": "9000"},
            {
                "transport": "http",
                "host": "0.0.0.0",
                "port": 9000,
                "show_banner": False,
            },
        ),
    ],
)
def test_main_runs_the_configured_transport(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: dict[str, Any]
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(self: FastMCP[Any], **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(FastMCP, "run", fake_run)
    monkeypatch.setattr(server, "configure_logging", lambda level: None)
    monkeypatch.setenv("FLECS_REST_URL", REST_URL)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    server.main()

    assert calls == [expected]


async def test_console_entry_point_serves_mcp_over_stdio() -> None:
    """Start ``python -m flecs_mcp`` as an MCP client would and list its tools."""
    env = {
        **os.environ,
        "FLECS_REST_URL": "http://127.0.0.1:9",
        "MCP_LOG_LEVEL": "DEBUG",  # logging must never corrupt the stdio channel
    }
    transport = StdioTransport(
        command=sys.executable, args=["-m", "flecs_mcp"], env=env, keep_alive=False
    )

    async with Client(transport) as client:
        tools = await client.list_tools()
        server_info = client.server_info  # era-neutral (initialize or discover)
        assert server_info is not None
        assert server_info.name == "flecs"
        assert server_info.version == __version__

    assert "flecs_query" in {tool.name for tool in tools}
