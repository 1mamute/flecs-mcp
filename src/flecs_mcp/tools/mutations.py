"""MUTATION tools: modify the FLECS world.

These tools are only registered when ``FLECS_REST_ALLOW_MUTATIONS=true``.

Deliberately not exposed, although the FLECS REST API supports them:

- ``DELETE /entity``: deletion cascades to children and can trigger
  ``(OnDelete, Panic)`` cleanup policies that abort the application.
- ``PUT /script`` and ``GET /call``: execute FLECS script code and can write
  files on the application host.
- ``GET /commands/capture``: replaces the world's command observer hook.
- ``PUT /action``: server maintenance actions, not useful to an agent.
"""

from typing import Annotated, Any, TypedDict

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from flecs_mcp.client import FlecsRestClient
from flecs_mcp.tools.common import ComponentId, EntityPath

_TAGS = {"flecs", "mutation"}

_ADDITIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)


class CreatedEntity(TypedDict):
    entity: str
    id: int


class MutationResult(TypedDict):
    entity: str
    component: str | None
    status: str


def register_mutation_tools(mcp: FastMCP[Any], client: FlecsRestClient) -> None:
    """Register the FLECS tools that modify the world on ``mcp``."""

    @mcp.tool(annotations=_ADDITIVE, tags=_TAGS)
    async def flecs_create_entity(entity: EntityPath) -> CreatedEntity:
        """[MUTATION] Create an entity at a path; missing parents are created too.

        If the entity already exists it is returned unchanged. Returns
        {entity, id}; 'id' is the entity index reported by FLECS, usable as '#<id>'.
        """
        entity_id = await client.create_entity(entity)
        return CreatedEntity(entity=entity, id=entity_id)

    @mcp.tool(annotations=_DESTRUCTIVE, tags=_TAGS)
    async def flecs_set_component(
        entity: EntityPath,
        component: ComponentId,
        value: Annotated[
            Any,
            Field(
                description=(
                    "New component value as JSON matching the component's "
                    "reflection schema (see flecs_get_type_info), e.g. "
                    '{"x": 10, "y": 20}. Members that are omitted keep their '
                    "current value. Omit or pass null to only add the component or "
                    "tag with its default value."
                )
            ),
        ] = None,
    ) -> MutationResult:
        """[MUTATION] Add a component, tag or pair to an entity and optionally set it.

        Overwrites the given members of the current value. The entity must exist
        (see flecs_create_entity). Errors when the component cannot be resolved,
        when a value is given for a type without reflection data, or when the
        value does not match the schema.
        """
        await client.set_component(entity, component, value)
        return MutationResult(
            entity=entity,
            component=component,
            status="set" if value is not None else "added",
        )

    @mcp.tool(annotations=_DESTRUCTIVE, tags=_TAGS)
    async def flecs_remove_component(
        entity: EntityPath, component: ComponentId
    ) -> MutationResult:
        """[MUTATION] Remove a component, tag or pair from an entity (value is lost).

        Removing an id the entity does not have is a no-op.
        """
        await client.remove_component(entity, component)
        return MutationResult(entity=entity, component=component, status="removed")

    @mcp.tool(annotations=_ADDITIVE, tags=_TAGS)
    async def flecs_set_enabled(
        entity: EntityPath,
        enabled: Annotated[
            bool, Field(description="true to enable, false to disable.")
        ],
        component: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=1024,
                description=(
                    "Optional component to toggle on the entity instead of the "
                    "entity itself. The component must have the CanToggle trait."
                ),
            ),
        ] = None,
    ) -> MutationResult:
        """[MUTATION] Enable or disable an entity (e.g. a system) or a component.

        Disabling an entity adds the flecs.core.Disabled tag, so queries and
        systems skip it; disabling a system entity stops the system from
        running. Disabling a component makes queries treat it as absent on that
        entity. Fully reversible by enabling again.
        """
        await client.set_enabled(entity, enabled=enabled, component=component)
        return MutationResult(
            entity=entity,
            component=component,
            status="enabled" if enabled else "disabled",
        )
