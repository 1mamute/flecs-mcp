"""Argument types and annotations shared by the MCP tools."""

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from flecs_mcp.client import MAX_EXPRESSION_LENGTH, MAX_QUERY_LIMIT, StatsPeriod

_MAX_PATH_LENGTH = 1024

EntityPath = Annotated[
    str,
    Field(
        min_length=1,
        max_length=_MAX_PATH_LENGTH,
        description=(
            "Entity path in FLECS dotted notation, e.g. 'Sun.Earth' or "
            "'flecs.core.World' (the 'parent' and 'name' fields of a query result "
            "joined with '.'), or a numeric entity id such as '#523'. Escape a "
            "literal '.' inside a name as '\\.'."
        ),
    ),
]

ComponentId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=_MAX_PATH_LENGTH,
        description=(
            "Component, tag or pair, written as FLECS prints it: a full path "
            "such as 'planets.Mass', or a pair such as '(flecs.core.ChildOf, Sun)'."
        ),
    ),
]

QueryExpression = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_EXPRESSION_LENGTH,
        description="Query in the FLECS query language, e.g. 'Position, Velocity'.",
    ),
]

Limit = Annotated[
    int,
    Field(ge=1, le=MAX_QUERY_LIMIT, description="Maximum number of items to return."),
]

Offset = Annotated[
    int,
    Field(ge=0, description="Number of items to skip (for paging)."),
]

Period = Annotated[
    StatsPeriod,
    Field(
        description=(
            "Sampling window: '1s', '1m', '1h', '1d' or '1w'. Each window holds "
            "60 samples."
        )
    ),
]

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
