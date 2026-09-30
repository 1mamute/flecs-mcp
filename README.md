# flecs-mcp

An [MCP](https://modelcontextprotocol.io) server that lets MCP clients and AI agents inspect a
running [FLECS](https://github.com/SanderMertens/flecs) application through FLECS' built-in
REST API. Agents can query entities, read components, explain queries, and inspect systems
and performance statistics. With explicit opt-in, they can also make small, reversible
changes to the world.

- Built on [FastMCP](https://gofastmcp.com) 4 with async [httpx](https://www.python-httpx.org).
- Read-only by default. Mutation tools are only registered when you enable them.
- No generic HTTP passthrough: every tool maps to one verified FLECS REST operation.
- Configured entirely through environment variables. Runs over stdio (default) or streamable HTTP.

## Contents

- [Why it was created](#why-it-was-created)
- [What you can do with it](#what-you-can-do-with-it)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running](#running)
- [MCP client configuration](#mcp-client-configuration)
- [Available tools](#available-tools)
- [Resources](#resources)
- [FLECS requirements](#flecs-requirements)
- [FLECS REST API assumptions](#flecs-rest-api-assumptions)
- [Safety](#safety)
- [Architecture](#architecture)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

## Why it was created

Coding agents such as Claude Code can read your source code, but not the world that code
produces at runtime. In an ECS, bugs usually live in runtime state: which components an
entity actually has, what values they hold, and which systems match it. Without runtime
access, you end up pasting debugger output into the chat.

flecs-mcp gives the agent that runtime view, so it can reason over the code and the
running world together:

```text
               Claude Code
                    │
         ┌──────────┴──────────┐
    Source code           Running world
    (C/C++, build)        (flecs-mcp → FLECS REST)
         └──────────┬──────────┘
               AI reasoning
```

## What you can do with it

- **Inspect the live world.** "Show entities with Position and Velocity and their values"
  becomes the FLECS query `Position, Velocity`. "Health but no Position" becomes
  `Health, !Position`. The query language can't express things like "within 10 units of
  #421", so the agent queries the positions and computes those itself.
- **Debug systems.** Given "The player isn't moving, investigate", the agent inspects the
  player and the systems that match it (`flecs_get_entity` with `matches`). It then compares
  the player with an entity that does move, and forms a hypothesis from real state rather
  than from source code alone.
- **Close the development loop.** Given "Implement enemy movement and verify it", the agent
  edits and builds the code, runs the game, records enemy positions, queries again later,
  and fixes the code if nothing moved.
- **Check expected state.** For example, check that a spawned enemy has `Position`,
  `Health` and `AIState`. This helps with emergent behavior that is awkward to unit-test.
- **Explore the architecture.** See how many entities use each component
  (`flecs_list_components`), which systems exist and what they match (`flecs_list_queries`,
  `flecs_run_named_query`), and how FLECS plans a query (`flecs_explain_query`).
- **Investigate performance.** Ask "Which systems take the most frame time?"
  (`flecs_get_pipeline_stats`, `flecs_get_world_stats`), then cross-check against match
  counts, the source code, or other MCP servers such as a metrics backend.
- **Experiment** (requires `FLECS_REST_ALLOW_MUTATIONS=true`). Create entities, set component
  values or pause a system, then watch what happens. This makes the running world a
  sandbox for experiments, or an AI-driven editing console. Deleting entities and running
  scripts are not available, by design.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- A FLECS application (v4) with the REST API enabled (see [FLECS requirements](#flecs-requirements))

## Installation

```bash
git clone <this repository> flecs-mcp
cd flecs-mcp
uv sync
```

## Configuration

All configuration comes from environment variables. Empty values count as unset.

| Variable | Default | Description |
|---|---|---|
| `FLECS_REST_URL` | *(required)* | Base URL of the FLECS REST API, e.g. `http://localhost:27750`. A path prefix is allowed (for a reverse proxy); a query string is not. |
| `FLECS_REST_TIMEOUT` | `5` | Request timeout in seconds (> 0). |
| `FLECS_REST_VERIFY_TLS` | `true` | Verify TLS certificates for `https://` URLs. Accepts `true/false`, `1/0`, `yes/no`, `on/off`. |
| `FLECS_REST_ALLOW_MUTATIONS` | `false` | Register the [MUTATION tools](#mutation-tools-opt-in). |
| `MCP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. Logs go to stderr. |
| `MCP_TRANSPORT` | `stdio` | `stdio`, or `http` for streamable HTTP. |
| `MCP_HOST` | `127.0.0.1` | Bind address for `MCP_TRANSPORT=http`. |
| `MCP_PORT` | `8000` | Bind port for `MCP_TRANSPORT=http`. The endpoint is `http://MCP_HOST:MCP_PORT/mcp`. |

```bash
export FLECS_REST_URL=http://localhost:27750
```

[`.env.example`](.env.example) lists every variable. The server doesn't read `.env` files
itself, but uv can load one for you:

```bash
cp .env.example .env
uv run --env-file .env flecs-mcp
```

The server stops with exit code 2 and a clear message if a value is missing or invalid.

FLECS has no authentication. If you put it behind a reverse proxy that uses HTTP basic auth,
include the credentials in the URL (`https://user:password@host/flecs`). They are sent as an
`Authorization` header and are redacted from every log line and error message.

## Running

```bash
FLECS_REST_URL=http://localhost:27750 uv run flecs-mcp
```

`uv run python -m flecs_mcp` is equivalent. With the default stdio transport, the process
talks MCP over stdin/stdout, so you normally let your MCP client start it (see below). The
server starts even when FLECS isn't running yet: connectivity is only checked when a tool
is called.

To serve MCP over HTTP instead:

```bash
FLECS_REST_URL=http://localhost:27750 MCP_TRANSPORT=http uv run flecs-mcp
# MCP endpoint: http://127.0.0.1:8000/mcp
```

The HTTP endpoint has no authentication. Keep it on `127.0.0.1` unless it sits on a trusted network.

## MCP client configuration

Replace `/path/to/flecs-mcp` with the absolute path of this repository.

### Claude Code

```bash
claude mcp add --transport stdio --env FLECS_REST_URL=http://localhost:27750 \
  flecs -- uv --directory /path/to/flecs-mcp run flecs-mcp
```

Add `--scope project` to store the server in the project's `.mcp.json` so your team shares
it. The equivalent `.mcp.json` entry:

```json
{
  "mcpServers": {
    "flecs": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/path/to/flecs-mcp", "run", "flecs-mcp"],
      "env": {
        "FLECS_REST_URL": "http://localhost:27750"
      }
    }
  }
}
```

To connect to a server started with `MCP_TRANSPORT=http`:

```bash
claude mcp add --transport http flecs http://127.0.0.1:8000/mcp
```

### Other MCP clients (Claude Desktop, Cursor, VS Code, ...)

Most clients accept the same `mcpServers` shape (`command`, `args`, `env`) in their own
configuration file, for example `claude_desktop_config.json` for Claude Desktop. Use the
JSON above and add `"FLECS_REST_ALLOW_MUTATIONS": "true"` to `env` if you want the mutation
tools.

### Docker

The image is optional; `uv` remains the primary workflow.

```bash
docker build -t flecs-mcp .
```

```json
{
  "mcpServers": {
    "flecs": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "-e", "FLECS_REST_URL", "flecs-mcp"],
      "env": { "FLECS_REST_URL": "http://host.docker.internal:27750" }
    }
  }
}
```

Inside a container, `localhost` is the container itself. Use `host.docker.internal` (Docker
Desktop) or the host's address to reach a FLECS application running on the host.

## Available tools

Entity paths use FLECS dotted notation: `Sun.Earth`, `flecs.core.World`. That's the
`parent` and `name` fields of a query result joined with `.`. You can also use a numeric id
such as `#523`. Names containing a dot are escaped as FLECS prints them (`main\.flecs`).
Component ids use full paths (`planets.Mass`) or pairs (`(flecs.core.ChildOf, Sun)`).

Every tool returns structured JSON, with an output schema where the shape is known. FLECS
payloads are passed through unchanged. Tool descriptions start with `[READ]` or `[MUTATION]`
and carry MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`).

### READ tools (always available)

#### `flecs_get_world_info`
Checks the connection and describes the world. Call it first.

- **Arguments:** none.
- **Returns:** `{rest_url, build_info, world_summary, notes}`.
  - `build_info`: FLECS version, compiler, enabled addons and build flags.
  - `world_summary`: entity, table, component and query counts, fps, frame count, uptime.
    It is `null` when the stats module isn't imported, and `notes` says why.
- **Example:** "Which FLECS version is the game running, and how many entities does it have?"

#### `flecs_get_entity`
Returns one entity with its tags, relationship pairs and component values.

- **Arguments:**
  - `entity` (required)
  - `values` (default `true`)
  - `inherited`: prefab (IsA) components.
  - `type_info`: component schemas.
  - `entity_id`: include the numeric id.
  - `doc`: flecs.doc names and descriptions.
  - `matches`: queries, systems and observers that match this entity.
- **Returns:** FLECS entity JSON: `{parent, name, tags, pairs, components, id?, type_info?, inherited?, matches?}`.
  A component value is `null` when the component has no reflection data.
- **Example:** `{"entity": "Sun.Earth", "matches": true}` answers "which systems process Earth?"

#### `flecs_get_component`
Returns the value of one component.

- **Arguments:** `entity`, `component`.
- **Returns:** `{entity, component, value}`, e.g. `value = {"x": 10, "y": 20}`.
- **Example:** `{"entity": "Sun.Earth", "component": "planets.Mass"}`.

#### `flecs_query`
Runs a query in the [FLECS query language](https://github.com/SanderMertens/flecs/blob/master/docs/FlecsQueryLanguage.md).
FLECS parses and evaluates it; this server only passes it through.

- **Arguments:**
  - `query` (required)
  - `limit` (1–1000, default 100) and `offset`, for paging.
  - `table`: return all components of each match instead of only the query fields.
  - `values`, `fields`, `entity_ids`, `inherited`, `type_info`, `doc`.
- **Returns:** `{page: {offset, limit, returned, may_have_more}, results: [...], type_info?}`.
  - Each result has `parent`, `name`, and `fields` (`values`, `ids`, `sources`, `is_set` per term), or all of the entity's components with `table=true`.
  - If `may_have_more` is true, call again with `offset += limit`.
  - Invalid queries return the FLECS parser error, including the error position.
- **Examples:**
  - `Position, Velocity`: entities with both components.
  - `(ChildOf, Sun)`: children of `Sun`.
  - `!(flecs.core.ChildOf, *)`: root entities.
  - `SpaceShip, $this ~= "Uss"`: name contains "Uss".
  - `Position, ?Mass(up)`: `Mass` is optional and may come from an ancestor.

#### `flecs_run_named_query`
Evaluates an existing named query, system or observer query.

- **Arguments:**
  - `name` (required), e.g. `game.systems.Move`.
  - `variables`, e.g. `parent:Sun` or `x:e1,y:e2`.
  - The same paging and serialization options as `flecs_query`.
- **Returns:** same shape as `flecs_query`.
- **Example:** "Which entities does the `Move` system currently process?"

#### `flecs_explain_query`
Shows how FLECS parses and plans a query, without returning results.

- **Arguments:**
  - `query` (required)
  - `profile`: also measure evaluation time and match counts; evaluates the query repeatedly for about 1 ms.
- **Returns:**
  - `query_info`: resolved terms, operators, sources and traversal.
  - `field_info`: type and schema per field.
  - `query_plan`: plain text.
  - `query_profile`: only when `profile` is true.
- **Example:** debugging a query that unexpectedly matches nothing.

#### `flecs_get_type_info`
Returns the reflection schema of a component type.

- **Arguments:** `component`, the dotted path of the component entity.
- **Returns:** `{component, has_reflection, schema}`, e.g. `schema = {"x": ["float"], "y": ["float"]}`.
- **Example:** check the value format before calling `flecs_set_component`.

#### `flecs_list_components`
Lists component, tag and pair ids, with entity counts, storage size, lifecycle hooks and traits.

- **Arguments:** `name_contains` (case-insensitive filter), `limit`, `offset`.
- **Returns:** `{total, offset, limit, components: [{name, entity_count, entity_size, tables, type?, traits, sparse?, memory?}]}`.
- **Example:** `{"name_contains": "game."}`: which game components exist, and how many entities use each one?

#### `flecs_list_queries`
Lists named queries, systems and observers with their evaluation statistics. FLECS evaluates
every query to produce this list.

- **Arguments:** `name_contains`, `kind` (`Query`, `System`, `Observer`), `include_plans` (default `false`), `limit`, `offset`.
- **Returns:** `{total, offset, limit, queries: [{name, kind, expr, results, count, eval_count, eval_time, eval_mode, cache_kind, ...}]}`.
- **Example:** `{"kind": "System"}`: list every system and how many entities it matches.

#### `flecs_get_world_stats`
Returns world performance metrics. Requires the FLECS stats module.

- **Arguments:**
  - `period`: `1s`, `1m`, `1h`, `1d` or `1w`.
  - `history`: return all 60 samples (oldest first) instead of only the latest.
- **Returns:** `{period, history, metrics: {"performance.fps": {avg, min, max, brief}, "entities.count": {...}, ...}}`. Times are in seconds.
- **Example:** "What is the frame time over the last minute?" → `{"period": "1m"}`.

#### `flecs_get_pipeline_stats`
Returns per-system timing. Requires the FLECS stats module.

- **Arguments:**
  - `period`
  - `pipeline`, e.g. `flecs.pipeline.BuiltinPipeline`: systems in execution order, with sync points. Omit it for all systems.
  - `history`
- **Returns:** `{period, pipeline, history, entries: [{name, disabled, time_spent, matched_entity_count?, matched_table_count?} | sync point]}`.
- **Example:** "Which system takes the most time per frame?"

### MUTATION tools (opt-in)

These are only registered when `FLECS_REST_ALLOW_MUTATIONS=true`. They change the running
application.

#### `flecs_create_entity`
Creates an entity, including any missing parents. If the path already exists, the existing
entity is returned.

- **Arguments:** `entity`.
- **Returns:** `{entity, id}`.
- **Example:** `{"entity": "Sun.Venus"}`.

#### `flecs_set_component`
Adds a component, tag or pair and, optionally, sets its value. FLECS starts from the
current value, so members you leave out keep their values.

- **Arguments:** `entity`, `component`, `value` (any JSON; omit it to only add).
- **Returns:** `{entity, component, status}`, where `status` is `set` or `added`.
- **Example:** `{"entity": "Sun.Venus", "component": "game.Position", "value": {"x": 5}}`.

#### `flecs_remove_component`
Removes a component, tag or pair. Its value is lost.

- **Arguments:** `entity`, `component`.
- **Returns:** `{entity, component, status: "removed"}`.

#### `flecs_set_enabled`
Enables or disables an entity (which adds or removes `flecs.core.Disabled`; a disabled
system stops running), or one of its components. A component can only be toggled if it
has the `CanToggle` trait.

- **Arguments:** `entity`, `enabled`, `component` (optional).
- **Returns:** `{entity, component, status}`, where `status` is `enabled` or `disabled`.
- **Example:** `{"entity": "game.systems.Move", "enabled": false}` pauses the `Move` system.

## Resources

Resources are used only for data that doesn't change while the application runs. Live
world state is served by tools.

| URI | Content |
|---|---|
| `flecs://build-info` | FLECS build information: version, compiler, addons, flags. |
| `flecs://type-info/{component}` | Reflection schema of a component, e.g. `flecs://type-info/planets.Mass`. |

## FLECS requirements

Enable the REST API in the application. It listens on port 27750 by default:

```c
// C
ECS_IMPORT(world, FlecsStats);         // optional: statistics tools
ecs_singleton_set(world, EcsRest, {0}); // REST server on port 27750
while (ecs_progress(world, 0)) { }
```

```cpp
// C++
world.import<flecs::stats>();  // optional: statistics tools
world.set<flecs::Rest>({});
while (world.progress()) { }
// or: world.app().enable_stats().enable_rest().run();
```

C# (`world.Set<flecs.EcsRest>(default)`) and Rust (`world.set(flecs::rest::Rest::default())`)
work the same way. Keep in mind:

- **The main loop must be running.** FLECS answers REST requests from inside
  `ecs_progress()`. An application that is paused, stopped at a breakpoint or stuck in a long
  frame doesn't answer, and requests time out.
- **Reflection** (the `meta` addon, e.g. `ecs_struct` / `flecs::meta` registration) is needed
  to see component values and to set them. Components without reflection show `null` values.
- **Stats module:** `flecs_get_world_stats`, `flecs_get_pipeline_stats` and the
  `world_summary` part of `flecs_get_world_info` require the stats addon (`FlecsStats`).
- To use a different port or bind address, set `EcsRest.port` / `EcsRest.ipaddr`.

## FLECS REST API assumptions

The implementation targets the FLECS v4 REST API and was verified against the FLECS source
(`src/addons/rest.c`, `src/addons/http/http.c`, `docs/FlecsRemoteApi.md`) at commit
`9e874bc`. It was also exercised end to end against a live FLECS **4.1.6** application. All
protocol details are isolated in [`client.py`](src/flecs_mcp/client.py).

| Topic | Behavior relied on |
|---|---|
| Endpoints used | `GET /entity`, `/component`, `/type_info`, `/query`, `/components`, `/queries`, `/stats/world`, `/stats/pipeline`; `PUT /entity`, `/component`, `/toggle`; `DELETE /component`. |
| Entity paths | URLs use `/` separators, while FLECS prints paths with `.`. The client converts dotted paths, percent-encodes each element, and escapes a literal `/` in a name as `\/`. |
| Parameters | Booleans are sent as the literal strings `true`/`false`. Every value is percent-encoded: FLECS splits parameters before decoding and decodes `+` as a space. |
| Query errors | Queries are sent with `try=true`, so FLECS doesn't log agent mistakes to the application console. FLECS then reports parse errors as `{"error": ...}` with HTTP 200, and the client treats that as a failure. This check applies only to query endpoints, because component values may legitimately contain an `error` member. |
| Error bodies | FLECS doesn't JSON-escape error messages, so error bodies may be invalid JSON. The client extracts the message anyway. |
| Paging | `limit` must be ≥ 1, because FLECS treats `limit=0` as unlimited. The FLECS default is 1000; this server defaults to 100. |
| `type_info` | The docs say "204 if no reflection". The implementation returns HTTP 200 with body `0`. Both are handled. |
| Query plans | FLECS colors plans and writes the escape character as `[`. The color codes are stripped. |
| Stats | In `9e874bc`, `/stats/*` dereferences the stats component without a null check, and **crashes the application** if `FlecsStats` isn't imported (reproduced against 4.1.6). The client first checks that `flecs.stats` exists and refuses otherwise. |
| Caching | FLECS caches identical GET responses for 0.2 s, so data may be up to one frame old. |

Deliberately **not exposed**:

- `DELETE /entity`: cascades to children and can trigger `(OnDelete, Panic)` aborts.
- `PUT /script` and `GET /call`: execute FLECS script code and can write files on the host.
- `GET /commands/capture`: replaces the world's command hook.
- `PUT /action`: maintenance only.
- `GET /world`: an unbounded dump; use the paged `flecs_query` instead.
- `GET /tables`.

## Safety

- The FLECS REST API has **no authentication**. Anyone who can reach its port can read the
  world and, through the REST API itself, modify or delete it, whether or not this server
  is involved. Don't expose it on untrusted networks.
- This server exposes only explicitly implemented operations, with typed arguments. It
  never forwards arbitrary paths, methods or bodies.
- Mutation tools are off by default and are never registered unless enabled. Even then,
  destructive operations (entity deletion, script execution) aren't available.
- In debug builds, FLECS asserts on some invalid operations, for example changing builtin
  components. Only enable mutations against development builds.
- Credentials embedded in `FLECS_REST_URL` are redacted from logs and errors. Request values,
  such as component values, are never logged, and `httpx` request logging is silenced.

## Architecture

```text
MCP Client (Claude Code, Claude Desktop, ...)
    ↓  MCP over stdio or streamable HTTP
FastMCP Server              server.py, tools/, resources.py
    ↓  typed tool calls
FLECS REST Client           client.py (one reused httpx.AsyncClient)
    ↓  HTTP
FLECS REST API              EcsRest, port 27750
    ↓
FLECS ECS World
```

| Module | Responsibility |
|---|---|
| `config.py` | Typed `Config` loaded and validated from the environment; logging setup. |
| `errors.py` | `FlecsError` hierarchy. It derives from FastMCP's `FastMCPError`, so messages reach the client verbatim and are logged without tracebacks. |
| `client.py` | `FlecsRestClient`: URL building, encoding, error translation, response validation, FLECS version quirks. |
| `tools/read.py` | READ tools (thin adapters that shape results for agents). |
| `tools/mutations.py` | MUTATION tools, only registered when enabled. |
| `resources.py` | `flecs://` resources. |
| `server.py` | `create_server()` wiring, the lifespan that closes the HTTP client on shutdown, and `main()`. |

## Development

```bash
uv sync                         # install runtime and dev dependencies
uv run pytest                   # tests (with coverage); no FLECS instance needed
uv run ruff check .             # lint
uv run ruff format --check .    # formatting (uv run ruff format . to fix)
uv run pyright                  # type checking (strict for src/)
```

Run the complete quality gate (format, lint, types, tests) with:

```bash
uv run python scripts/check.py
```

It runs every step and exits non-zero if any of them fails.

- **Tests** replace the HTTP layer with `httpx.MockTransport` (see `tests/conftest.py`), drive
  the MCP tools through FastMCP's in-memory client, and start the real server over stdio once.
- **Ruff** enables pycodestyle, pyflakes, isort, bugbear, pyupgrade, simplify, comprehensions,
  pytest-style, async and Ruff's own rules.
- **Pyright** runs in `standard` mode, with `strict` mode for `src/`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Unable to connect to the FLECS REST API at ...` | The application isn't running, REST isn't enabled, or the host or port is wrong. Check `FLECS_REST_URL` (default port 27750) and open `http://<host>:27750/` in a browser: it should answer "You've reached the REST API for Flecs". |
| `Timed out ... while connecting` | Wrong host, a firewall, or the host is unreachable. From Docker, use `host.docker.internal` instead of `localhost`. |
| `did not respond within N s` | The connection works but FLECS isn't serving requests: the main loop is paused, stopped at a breakpoint or stuck in a long frame. Resume it, or raise `FLECS_REST_TIMEOUT` for large queries. |
| `Entity 'X' not found` | Use dotted paths (`Sun.Earth`) and full parent paths. Find the entity with `flecs_query`, e.g. `$this ~= "Earth"`. |
| `FLECS rejected GET /query: ...` | The FLECS query parser error, including the position. Check names (full paths), commas and parentheses; `flecs_explain_query` helps. |
| `HTTP 404 ... endpoint not found` | The FLECS build doesn't include the addon that serves this endpoint (e.g. stats). |
| `Statistics are not available` | Import the stats module (`FlecsStats`) in the application. |
| Component values are `null` | The component has no reflection data. Register it with the meta addon. |
| MCP client can't start the server | Check that `uv` is on the client's `PATH`, that `--directory` points to this repository, and that `FLECS_REST_URL` is set in the client's `env`. Configuration errors are printed to stderr with exit code 2. Run the same command in a terminal to see them. |
| TLS / certificate errors | For self-signed certificates on a trusted network, set `FLECS_REST_VERIFY_TLS=false`. |
| Too much or too little logging | Set `MCP_LOG_LEVEL` (`DEBUG` shows each REST call without its values). |

## License

[MIT](LICENSE)
