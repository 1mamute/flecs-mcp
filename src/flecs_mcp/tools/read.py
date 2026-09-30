"""READ tools: inspect the FLECS world without modifying it."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, NotRequired, TypedDict, cast

from fastmcp import FastMCP
from pydantic import Field

from flecs_mcp.client import (
    DEFAULT_QUERY_LIMIT,
    FlecsRestClient,
    JsonObject,
    QueryOptions,
)
from flecs_mcp.errors import FlecsRequestError
from flecs_mcp.tools.common import (
    READ_ONLY,
    ComponentId,
    EntityPath,
    Limit,
    Offset,
    Period,
    QueryExpression,
)

_TAGS = {"flecs", "read"}

Table = Annotated[
    bool,
    Field(
        description=(
            "Return every tag, pair and component of each matched entity (same "
            "format as flecs_get_entity) instead of only the query fields."
        )
    ),
]
Values = Annotated[bool, Field(description="Include component values.")]
Fields = Annotated[
    bool,
    Field(description="Include per-term field data ('fields') for each result."),
]
EntityIds = Annotated[bool, Field(description="Include numeric entity ids ('id').")]
Inherited = Annotated[
    bool,
    Field(description="With table=true: include components inherited from prefabs."),
]
TypeInfo = Annotated[
    bool,
    Field(description="Include the reflection schema of the returned components."),
]
Doc = Annotated[
    bool,
    Field(description="Include flecs.doc information (doc names, descriptions)."),
]
NameContains = Annotated[
    str | None,
    Field(
        max_length=256,
        description="Only return entries whose name contains this text (any case).",
    ),
]


class WorldInfo(TypedDict):
    rest_url: str
    build_info: Any
    world_summary: Any
    notes: list[str]


class ComponentValue(TypedDict):
    entity: str
    component: str
    value: Any


class TypeSchema(TypedDict):
    component: str
    has_reflection: bool
    schema: Any


class Page(TypedDict):
    offset: int
    limit: int
    returned: int
    may_have_more: bool


class QueryResult(TypedDict):
    page: Page
    results: list[JsonObject]
    type_info: NotRequired[JsonObject]


class ComponentList(TypedDict):
    total: int
    offset: int
    limit: int
    components: list[JsonObject]


class QueryList(TypedDict):
    total: int
    offset: int
    limit: int
    queries: list[JsonObject]


class WorldStats(TypedDict):
    period: str
    history: bool
    metrics: JsonObject


class PipelineStats(TypedDict):
    period: str
    pipeline: str | None
    history: bool
    entries: list[JsonObject]


def register_read_tools(mcp: FastMCP[Any], client: FlecsRestClient) -> None:
    """Register all read-only FLECS tools on ``mcp``."""

    @mcp.tool(annotations=READ_ONLY, tags=_TAGS)
    async def flecs_get_world_info() -> WorldInfo:
        """[READ] Describe the connected FLECS world; call it first to test the link.

        Returns:
        - rest_url: the FLECS REST API this server talks to.
        - build_info: FLECS version, compiler, enabled addons and build flags
          (flecs.core.BuildInfo), or null if unavailable.
        - world_summary: live counters such as entity, table, component and
          query counts, frame count, fps, target fps, time scale and uptime
          (flecs.stats.WorldSummary), or null if the stats module is not imported.
        - notes: why a section is null, if any.

        Fails if the FLECS REST API cannot be reached.
        """
        notes: list[str] = []
        build_info = await _optional(client.get_build_info, "build_info", notes)
        world_summary = await _optional(
            client.get_world_summary,
            "world_summary",
            notes,
            hint="The FLECS application must import the stats module (FlecsStats).",
        )
        return WorldInfo(
            rest_url=client.display_url,
            build_info=build_info,
            world_summary=world_summary,
            notes=notes,
        )

    @mcp.tool(annotations=READ_ONLY, tags=_TAGS)
    async def flecs_get_entity(
        entity: EntityPath,
        values: Values = True,
        inherited: Annotated[
            bool,
            Field(description="Include tags/components inherited from prefabs (IsA)."),
        ] = False,
        type_info: TypeInfo = False,
        entity_id: Annotated[
            bool, Field(description="Include the numeric entity id ('id').")
        ] = False,
        doc: Doc = False,
        matches: Annotated[
            bool,
            Field(
                description=(
                    "Include the queries, systems and observers that match this "
                    "entity ('matches'). Useful to find out why a system does or "
                    "does not process an entity."
                )
            ),
        ] = False,
    ) -> JsonObject:
        """[READ] Get one entity with its tags, relationship pairs and component values.

        Returns the FLECS entity JSON: 'parent', 'name', 'tags' (list),
        'pairs' (relationship -> target), 'components' (component -> value; null
        when the component has no reflection data) and, when requested,
        'id', 'type_info', 'inherited', 'matches' and doc info. Names use full paths.

        Errors when the entity does not exist. Use flecs_query to find entities.
        """
        return await client.get_entity(
            entity,
            values=values,
            inherited=inherited,
            type_info=type_info,
            entity_id=entity_id,
            doc=doc,
            matches=matches,
        )

    @mcp.tool(annotations=READ_ONLY, tags=_TAGS)
    async def flecs_get_component(
        entity: EntityPath, component: ComponentId
    ) -> ComponentValue:
        """[READ] Get the value of a single component of an entity.

        Returns {entity, component, value}; 'value' is the component serialized
        by FLECS reflection (usually an object of members, e.g. {"x": 10, "y": 20}).
        Errors when the entity does not have the component, when the id is a tag
        (no data) or when it cannot be resolved.
        """
        value = await client.get_component(entity, component)
        return ComponentValue(entity=entity, component=component, value=value)

    @mcp.tool(annotations=READ_ONLY, tags=_TAGS)
    async def flecs_query(
        query: QueryExpression,
        limit: Limit = DEFAULT_QUERY_LIMIT,
        offset: Offset = 0,
        table: Table = False,
        values: Values = True,
        fields: Fields = True,
        entity_ids: EntityIds = False,
        inherited: Inherited = False,
        type_info: TypeInfo = False,
        doc: Doc = False,
    ) -> QueryResult:
        """[READ] Find entities with a query written in the FLECS query language.

        Syntax cheat sheet (terms are comma separated and all must match):
        - 'Position, Velocity'           entities with both components
        - 'Position, !Velocity'          ... without Velocity
        - 'Position, ?Mass'              Mass is optional
        - 'Planet || Moon'               either tag
        - '(ChildOf, Sun)'               children of entity Sun
        - '!(flecs.core.ChildOf, *)'     root entities
        - '(Likes, *)' / '(Likes, $x)'   wildcard / variable pair target
        - 'Position, Mass(up)'           Mass on an ancestor (ChildOf)
        - 'SpaceShip, $this ~= "Uss"'    name contains "Uss"
        - 'IsA(_, *)'                    instances of any prefab
        Use full paths (e.g. 'transform.Position') when names are ambiguous.

        Returns {page, results, type_info?}. Each result has 'parent', 'name' and,
        depending on options, 'id', 'fields' ({values, ids, sources, is_set} per
        query term), 'vars' or, with table=true, 'tags'/'pairs'/'components'.
        page = {offset, limit, returned, may_have_more}; when may_have_more is
        true, call again with offset += limit. Invalid queries return the FLECS
        parser error (with position) so the query can be fixed.
        """
        options = QueryOptions(
            limit=limit,
            offset=offset,
            table=table,
            values=values,
            fields=fields,
            entity_ids=entity_ids,
            inherited=inherited,
            type_info=type_info,
            doc=doc,
        )
        return _query_result(await client.query(query, options), options)

    @mcp.tool(annotations=READ_ONLY, tags=_TAGS)
    async def flecs_run_named_query(
        name: Annotated[
            str,
            Field(
                min_length=1,
                max_length=1024,
                description=(
                    "Dotted path of an existing named query, system or observer, "
                    "as listed by flecs_list_queries (e.g. 'game.systems.Move')."
                ),
            ),
        ],
        variables: Annotated[
            str | None,
            Field(
                max_length=512,
                description=(
                    "Optional values for query variables as 'var:entity' pairs, "
                    "e.g. 'parent:Sun' or 'x:e1,y:e2'."
                ),
            ),
        ] = None,
        limit: Limit = DEFAULT_QUERY_LIMIT,
        offset: Offset = 0,
        table: Table = False,
        values: Values = True,
        fields: Fields = True,
        entity_ids: EntityIds = False,
        inherited: Inherited = False,
        type_info: TypeInfo = False,
        doc: Doc = False,
    ) -> QueryResult:
        """[READ] Return what an existing named query, system or observer matches now.

        Useful to check which entities a system processes. Same result format as
        flecs_query: {page, results, type_info?}.
        """
        options = QueryOptions(
            limit=limit,
            offset=offset,
            table=table,
            values=values,
            fields=fields,
            entity_ids=entity_ids,
            inherited=inherited,
            type_info=type_info,
            doc=doc,
        )
        result = await client.named_query(name, options, variables)
        return _query_result(result, options)

    @mcp.tool(annotations=READ_ONLY, tags=_TAGS)
    async def flecs_explain_query(
        query: QueryExpression,
        profile: Annotated[
            bool,
            Field(
                description=(
                    "Also measure evaluation time and result/entity counts "
                    "('query_profile'). Evaluates the query repeatedly for up to ~1 ms."
                )
            ),
        ] = False,
    ) -> JsonObject:
        """[READ] Explain how FLECS parses and plans a query, without returning results.

        Returns 'query_info' (resolved terms: operator, source, traversal
        flags), 'field_info' (id, type and member schema per field), 'query_plan'
        (the FLECS query plan as text) and optionally 'query_profile'. Use it to
        debug queries that match nothing or match too much. Invalid queries
        return the FLECS parser error.
        """
        return await client.explain_query(query, profile=profile)

    @mcp.tool(annotations=READ_ONLY, tags=_TAGS)
    async def flecs_get_type_info(
        component: Annotated[
            str,
            Field(
                min_length=1,
                max_length=1024,
                description="Dotted path of a component entity, e.g. 'planets.Mass'.",
            ),
        ],
    ) -> TypeSchema:
        """[READ] Get the reflection schema (members, types, units) of a component type.

        Returns {component, has_reflection, schema}. 'schema' maps member names
        to [type, {unit, ...}] descriptors, e.g. {"x": ["float"], "y": ["float"]}.
        has_reflection is false (and schema null) when the type has no reflection
        data, in which case FLECS cannot serialize or set its value.
        """
        schema = await client.get_type_info(component)
        return TypeSchema(
            component=component, has_reflection=schema is not None, schema=schema
        )

    @mcp.tool(annotations=READ_ONLY, tags=_TAGS)
    async def flecs_list_components(
        name_contains: NameContains = None,
        limit: Limit = DEFAULT_QUERY_LIMIT,
        offset: Offset = 0,
    ) -> ComponentList:
        """[READ] List the component, tag and pair ids in use, with storage statistics.

        Each entry has 'name', 'entity_count', 'entity_size', 'tables' (table
        ids), 'traits' (e.g. Exclusive, CanToggle, (OnDelete,Remove)),
        'type' (size, alignment, which lifecycle hooks are set; absent for tags),
        'sparse' (for sparse components) and 'memory' when the stats module is
        imported. Includes FLECS builtin ids and wildcard records.
        Returns {total, offset, limit, components}; total counts matches after
        filtering. Use flecs_get_type_info for a component's member schema.
        """
        components = _filter_by_name(await client.list_components(), name_contains)
        page = components[offset : offset + limit]
        return ComponentList(
            total=len(components), offset=offset, limit=limit, components=page
        )

    @mcp.tool(annotations=READ_ONLY, tags=_TAGS)
    async def flecs_list_queries(
        name_contains: NameContains = None,
        kind: Annotated[
            Literal["Query", "System", "Observer"] | None,
            Field(description="Only return entries of this kind."),
        ] = None,
        include_plans: Annotated[
            bool,
            Field(description="Include the query plan text ('plan', 'cache_plan')."),
        ] = False,
        limit: Limit = DEFAULT_QUERY_LIMIT,
        offset: Offset = 0,
    ) -> QueryList:
        """[READ] List named queries, systems and observers with evaluation statistics.

        Each entry has 'name' (usable with flecs_run_named_query), 'kind'
        (Query, System or Observer), 'expr' (the query expression), 'results' and
        'count' (current matches), 'eval_count', 'eval_time', 'eval_mode',
        'cache_kind', 'batched', 'empty_tables', 'plan_size' and, when the stats
        module is imported, 'memory'. FLECS evaluates every query to produce this
        list, which can take a while in very large worlds.
        Returns {total, offset, limit, queries}.
        """
        queries = _filter_by_name(await client.list_queries(), name_contains)
        if kind is not None:
            queries = [entry for entry in queries if entry.get("kind") == kind]
        if not include_plans:
            queries = [
                {k: v for k, v in entry.items() if k not in ("plan", "cache_plan")}
                for entry in queries
            ]
        page = queries[offset : offset + limit]
        return QueryList(total=len(queries), offset=offset, limit=limit, queries=page)

    @mcp.tool(annotations=READ_ONLY, tags=_TAGS)
    async def flecs_get_world_stats(
        period: Period = "1s",
        history: Annotated[
            bool,
            Field(
                description=(
                    "Return all 60 samples of the window (oldest first) instead of "
                    "only the latest sample."
                )
            ),
        ] = False,
    ) -> WorldStats:
        """[READ] Get world performance statistics from the FLECS stats module.

        'metrics' maps names such as 'performance.fps', 'performance.frame_time',
        'entities.count', 'tables.count', 'queries.system_count',
        'commands.add_count' and 'memory.alloc_count' to {avg, min, max, brief}.
        Times are in seconds. Requires the FLECS application to import the
        stats module (FlecsStats); returns an explanatory error otherwise.
        """
        metrics = await client.get_world_stats(period)
        return WorldStats(
            period=period,
            history=history,
            metrics=metrics if history else _latest_sample(metrics),
        )

    @mcp.tool(annotations=READ_ONLY, tags=_TAGS)
    async def flecs_get_pipeline_stats(
        period: Period = "1s",
        pipeline: Annotated[
            str | None,
            Field(
                max_length=1024,
                description=(
                    "Dotted path of a pipeline, e.g. 'flecs.pipeline.BuiltinPipeline', "
                    "to get its systems in execution order including sync points. "
                    "Omit to get all systems (unordered)."
                ),
            ),
        ] = None,
        history: Annotated[
            bool,
            Field(
                description=(
                    "Return all 60 samples of the window (oldest first) instead of "
                    "only the latest sample."
                )
            ),
        ] = False,
    ) -> PipelineStats:
        """[READ] Get per-system timing statistics from the FLECS stats module.

        'entries' contains one object per system: 'name', 'disabled',
        'time_spent' and (for non-task systems) 'matched_entity_count' and
        'matched_table_count', each metric as {avg, min, max}. With a pipeline,
        sync points ('multi_threaded', 'immediate', 'time_spent',
        'commands_enqueued') are interleaved in execution order. Times are in
        seconds. Requires the stats module (FlecsStats).
        """
        entries = await client.get_pipeline_stats(period, pipeline)
        return PipelineStats(
            period=period,
            pipeline=pipeline,
            history=history,
            entries=entries if history else [_latest_sample(e) for e in entries],
        )


async def _optional(
    fetch: Callable[[], Awaitable[Any]], label: str, notes: list[str], hint: str = ""
) -> Any:
    """Return ``await fetch()``, or ``None`` plus a note if FLECS rejects it."""
    try:
        return await fetch()
    except FlecsRequestError as exc:
        notes.append(f"{label} unavailable: {exc.flecs_message or exc} {hint}".strip())
        return None


def _query_result(result: JsonObject, options: QueryOptions) -> QueryResult:
    results: list[JsonObject] = result["results"]
    output = QueryResult(
        page=Page(
            offset=options.offset,
            limit=options.limit,
            returned=len(results),
            may_have_more=len(results) >= options.limit,
        ),
        results=results,
    )
    type_info = result.get("type_info")
    if isinstance(type_info, dict):
        output["type_info"] = type_info
    return output


def _filter_by_name(entries: list[JsonObject], text: str | None) -> list[JsonObject]:
    if not text:
        return entries
    needle = text.lower()
    return [e for e in entries if needle in str(e.get("name", "")).lower()]


def _latest_sample(entry: JsonObject) -> JsonObject:
    """Reduce every time series (list of samples) in ``entry`` to its last sample.

    FLECS sends each metric as {"avg": [...], "min": [...], "max": [...]} with
    the window ordered oldest to newest.
    """
    reduced: JsonObject = {}
    for key, value in entry.items():
        if isinstance(value, dict):
            reduced[key] = _latest_sample(cast(JsonObject, value))
        elif isinstance(value, list):
            reduced[key] = value[-1] if value else None
        else:
            reduced[key] = value
    return reduced
