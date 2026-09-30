"""Shared fixtures: a fake FLECS REST API behind ``httpx.MockTransport``."""

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastmcp import Client

from flecs_mcp.client import FlecsRestClient
from flecs_mcp.config import Config
from flecs_mcp.server import create_server

REST_URL = "http://flecs.test:27750"

type Responder = Callable[[httpx.Request], httpx.Response]


@dataclass
class FakeFlecs:
    """Routes requests by method and *raw* (still percent-encoded) path.

    Unknown routes reply 404 with an empty body, like FLECS does.
    """

    routes: dict[tuple[str, str], Responder] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def on(self, method: str, path: str, responder: Responder) -> None:
        self.routes[(method, path)] = responder

    def reply(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        status: int = 200,
        text: str | None = None,
    ) -> None:
        """Reply with ``body`` as JSON, or with ``text`` verbatim."""
        content = (
            text if text is not None else ("" if body is None else json.dumps(body))
        )

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status,
                content=content.encode(),
                headers={"Content-Type": "application/json"},
            )

        self.on(method, path, responder)

    def fail(
        self, method: str, path: str, error: Callable[[httpx.Request], Exception]
    ) -> None:
        def responder(request: httpx.Request) -> httpx.Response:
            raise error(request)

        self.on(method, path, responder)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.raw_path.decode().split("?", 1)[0]
        responder = self.routes.get((request.method, path))
        if responder is None:
            return httpx.Response(404)
        return responder(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def paths(self) -> list[str]:
        return [r.url.raw_path.decode().split("?", 1)[0] for r in self.requests]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def params_of(request: httpx.Request) -> dict[str, str]:
    """Decoded query parameters of ``request`` (single-valued)."""
    query = request.url.query.decode()
    return {k: v[0] for k, v in parse_qs(query, keep_blank_values=True).items()}


@pytest.fixture
def fake_flecs() -> FakeFlecs:
    return FakeFlecs()


@pytest.fixture
def config() -> Config:
    return Config(rest_url=REST_URL)


@pytest.fixture
async def client(
    config: Config, fake_flecs: FakeFlecs
) -> AsyncIterator[FlecsRestClient]:
    async with FlecsRestClient(config, transport=fake_flecs.transport()) as rest:
        yield rest


@pytest.fixture
async def mcp_client(config: Config, fake_flecs: FakeFlecs) -> AsyncIterator[Client]:
    server = create_server(config, transport=fake_flecs.transport())
    async with Client(server) as mcp:
        yield mcp


@pytest.fixture
async def mutating_mcp_client(fake_flecs: FakeFlecs) -> AsyncIterator[Client]:
    config = Config(rest_url=REST_URL, allow_mutations=True)
    server = create_server(config, transport=fake_flecs.transport())
    async with Client(server) as mcp:
        yield mcp
