---
name: flecs-source
description: Read and review the flecs ECS source and its bundled docs (github.com/SanderMertens/flecs, docs/ folder) from a local clone, to answer questions accurately instead of from memory. Use this whenever working on the flecs-mcp server or anything touching the flecs REST API / Remote API, explorer protocol, JSON serialization (ecs_iter_to_json, ecs_entity_to_json, ecs_world_to_json), query DSL strings sent to /query, endpoint parameters, response shapes, or error messages — and whenever you're unsure how a flecs behavior or API works. Prefer this over web docs; the docs site is generated from the same docs/ folder.
---

# flecs source & docs

This project (flecs-mcp) wraps the REST API built into flecs. Endpoint names, query parameters and JSON shapes change between flecs versions, and memory of them is often stale, so check them against the source before you rely on them.

## 1. Get or refresh the local clone

The clone is kept at `~/.cache/flecs`, a shallow clone of the default branch:

```bash
D="$HOME/.cache/flecs"
if [ -d "$D/.git" ]; then git -C "$D" pull --depth 1 -q --ff-only || true
else git clone --depth 1 -q https://github.com/SanderMertens/flecs "$D"; fi
git -C "$D" log -1 --format='flecs @ %h (%cd)'
```

Refresh it at most once per session. When you report findings, include the commit hash so the reader knows which version they came from. If the user targets a specific flecs release, such as one pinned in a build file, check out that tag instead (`git -C "$D" fetch --depth 1 origin tag vX.Y.Z && git -C "$D" checkout -q vX.Y.Z`), because behavior differs between releases.

Do not use the flecs.dev website. Everything on it comes from `docs/*.md` in the clone.

## 2. Where things live

| Topic | Look at |
|---|---|
| REST API reference: every endpoint, option and example response | `docs/FlecsRemoteApi.md` (its "Reference" section) |
| REST request routing and handlers | `src/addons/rest.c`: dispatch is the `ecs_os_strncmp(req->path, ...)` chain; handlers are named `flecs_rest_get_*`, `flecs_rest_put_*` and `flecs_rest_delete_*` |
| Query-string parameters for each endpoint | `flecs_rest_bool_param` / `flecs_rest_string_param` / `ecs_http_get_param` calls in `rest.c` |
| REST public API (port 27750, `EcsRest`, `ecs_rest_server_init`) | `include/flecs/addons/rest.h` |
| JSON output shapes | `src/addons/json/serialize_*.c`; the option structs (`ecs_iter_to_json_desc_t`, `ecs_entity_to_json_desc_t`) are in `include/flecs/addons/json.h` |
| JSON input (PUT component `value`) | `src/addons/json/deserialize*.c` |
| HTTP server (CORS, caching, threads) | `src/addons/http/` |
| Query DSL syntax (the `expr` for `/query`) | `docs/FlecsQueryLanguage.md`, parser in `src/addons/query_dsl/` |
| Stats endpoints | `src/addons/stats/`, `flecs_rest_get_stats` |
| Flecs Script (`PUT script`) | `docs/FlecsScript.md`, `src/addons/script/` |
| Core concepts | `docs/Manual.md`, `Queries.md`, `Relationships.md`, `EntitiesComponents.md` |
| Reference client behavior | the "JavaScript library" section of `docs/FlecsRemoteApi.md` |
| Real usage and expected outputs | `test/` (grep for the endpoint or function name) and `examples/` |

Use Grep/Read on these paths directly. Grep with the path limited to one of these directories rather than across the whole repo; the amalgamated `distr/flecs.c` duplicates every source file and doubles the hits.

## 3. How to answer

1. Start with the docs for the intended behavior, then confirm it in the source. When they disagree, the source is what actually runs, so trust it and point out the mismatch.
2. For an endpoint, give the method and path, the parameters with their defaults, a response example (quoted from the docs or derived from the serializer), and the error cases (search for `flecs_reply_error` and the HTTP status codes in the handler).
3. Cite `path:symbol` plus the flecs commit, for example `src/addons/rest.c:flecs_rest_get_query @ 9e874bc`. Line numbers change between versions, so use them only alongside the commit.
4. Say plainly when something isn't in the source (an endpoint or option doesn't exist) instead of guessing.

## 4. Reviewing flecs-mcp code against flecs

When asked to review this project's use of the REST API, check each call site against the handler it hits:

- Is the path correct, including the trailing segment (`entity/<path>` vs `component/<path>?component=`)? Entity paths use `/` separators in URLs, not `.`.
- Are the parameter names spelled correctly? An unknown parameter is ignored without any error.
- Are the boolean parameters the literal strings `true`/`false`? Check this in `flecs_rest_bool_param`.
- Are values URL-encoded, especially query `expr` strings and JSON `value` bodies?
- Does the code handle the documented error shape, `{"error": "..."}`, together with its HTTP status?
- Does the code assume a JSON field that only appears when an option is enabled (for example `values`, `type_info` or `entity_ids`)?

Report each mismatch with the flecs source line that proves it.
