# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                   # install runtime + dev dependencies
uv run python scripts/check.py            # full quality gate: ruff format --check, ruff check, pyright, pytest
uv run pytest                             # all tests (coverage is on by default via addopts)
uv run pytest tests/test_client.py::test_query_encodes_expression_and_paging --no-cov   # single test
uv run ruff format . && uv run ruff check .
uv run pyright                            # strict for src/, standard for tests/ and scripts/
FLECS_REST_URL=http://localhost:27750 uv run flecs-mcp   # run the server (stdio); also: python -m flecs_mcp
```

Run the quality gate before calling a change done. Tests never need a live FLECS instance.

## Architecture

An MCP server (FastMCP 4) that adapts the FLECS ECS REST API (`EcsRest`, port 27750) into MCP tools and resources:
`server.py` → `tools/` + `resources.py` → `client.py` (`FlecsRestClient`) → HTTP → FLECS.

- **`client.py` is the only module that knows the FLECS REST protocol**: URL layout, parameter names and encoding, response shapes, error conventions and version quirks. Keep that knowledge out of the tool layer. The tools shape results for agents (paging envelopes, stats reduction, filtering) and pass FLECS JSON through unchanged.
- **Tools are closures registered by `register_*` functions** that capture a single `FlecsRestClient`. `create_server()` builds the client and a FastMCP `lifespan` that closes it on shutdown. The client creates its `httpx.AsyncClient` lazily and can reopen it after `aclose()`, because FastMCP may re-enter the lifespan.
- **Read vs. mutation is structural.** `tools/read.py` is always registered. `tools/mutations.py` is only registered when `FLECS_REST_ALLOW_MUTATIONS=true`. Tool docstrings start with `[READ]` or `[MUTATION]` and carry `ToolAnnotations`. Never add a generic HTTP passthrough tool. The endpoints that are deliberately not exposed (delete entity, script, call, command capture, `/world`) are listed with reasons at the top of `tools/mutations.py`.
- **Errors:** everything raised for the agent is a `FlecsError`, which subclasses FastMCP's `FastMCPError`. FastMCP then returns the message verbatim and logs it without a traceback. Any other exception type is masked and logged with a full traceback.
- **Configuration** comes only from environment variables (`config.py`, frozen `Config`). `FLECS_REST_URL` is required. Logs go to stderr, because stdout carries the MCP stdio protocol.

### FLECS REST behaviors the client depends on (verified against the flecs source)

- **Entity paths:** tool arguments use FLECS dotted notation (`Sun.Earth`, `#123`, `main\.flecs`). `entity_url_path()` converts them to the `/`-separated, percent-encoded URL form. It escapes a literal `/` as `\/` and doesn't split on dots inside `<...>`.
- **Parameter encoding:** FLECS splits query parameters on `?&=` *before* percent-decoding them. Released FLECS (≤ v4.1.6) decodes only `%XX`, so a `+` stays a literal `+`. Only FLECS after v4.1.6 decodes `+` as a space. `_request()` builds every query string with `encode_query()`, which percent-encodes everything and writes spaces as `%20`. Never pass httpx `params=`, because it encodes spaces as `+`. `params_of()` in the tests decodes the way v4.1.6 does, so a stray `+` fails the tests.
- **Query errors in 200 responses:** queries are sent with `try=true`, so FLECS reports parse errors as HTTP 200 with a `{"error": ...}` body. `_request(..., error_in_body=True)` handles this, and must only be used for `/query`, because component values can legitimately contain an `error` member.
- **Stats crash guard:** `GET /stats/*` crashes the FLECS app if the `FlecsStats` module isn't imported. `_ensure_stats_module()` checks for `flecs.stats` first; keep that guard.
- **Paging:** `limit=0` means "unlimited" to FLECS, so `QueryOptions` enforces a limit of 1–1000.
- **Other quirks:** `type_info` returns HTTP 200 with body `0` when there's no reflection data. FLECS error bodies aren't JSON-escaped. Query plans contain colour codes with ESC written as `[`, which `strip_ansi` removes.

When touching endpoints, parameters or response shapes, check them against the flecs source with the project skill `.claude/skills/flecs-source` (a local clone of flecs at `~/.cache/flecs`), not from memory. The README section "FLECS REST API assumptions" documents what the server relies on; update it when that changes.

## Tests

- `tests/conftest.py` provides `FakeFlecs`, a fake FLECS REST API behind `httpx.MockTransport`. It routes on method plus the **raw, still-encoded path**, so tests assert the exact URL encoding (e.g. `/entity/foo%5C%2Fbar`). Unknown routes reply 404 with an empty body, as FLECS does.
- Use `params_of(request)` to check the decoded query parameters.
- The `mcp_client` / `mutating_mcp_client` fixtures drive the tools through FastMCP's in-memory `Client`. `test_server.py` also starts the real `python -m flecs_mcp` subprocess over stdio.
- FastMCP 4 details that tests rely on:
  - Use the era-neutral `client.server_info`; `initialize_result` is `None` on modern-protocol connections.
  - Tool annotations use snake_case fields (`read_only_hint`) from `mcp.types`.
