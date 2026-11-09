"""Human-only, cookie/CSRF protected hierarchy administration routes.

No worker tool, device connection or Runtime activation is registered here.
Every write delegates to the transactional store and its current human check.
"""
from typing import Annotated

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from hierarchy_store import HierarchyStore


class HierarchyBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class HierarchyLimits(HierarchyBody):
    expected_version: Annotated[int, Field(ge=0)]
    policy: dict
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]


class HierarchyRoot(HierarchyBody):
    beneficiary_id: Annotated[str, Field(min_length=1, max_length=200)]
    policy: dict
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]


class HierarchyChange(HierarchyBody):
    expected_version: Annotated[int, Field(ge=1)]
    policy: dict | None = None
    revoke: bool = False
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]


class HierarchyProjectArchive(HierarchyBody):
    project_version: Annotated[int, Field(ge=1)]
    profile_version: Annotated[int, Field(ge=1)]
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]


def mount_hierarchy_routes(app, board, human):
    store = HierarchyStore(board)

    @app.get('/api/hierarchy')
    async def overview(limit: int = Query(50, ge=1, le=100), after: str | None = None, actor=Depends(human)):
        return store.list_for_owner(actor, limit=limit, after=after)

    @app.put('/api/hierarchy/limits')
    async def limits(values: HierarchyLimits, actor=Depends(human)):
        return store.configure(actor, values.expected_version, values.policy, values.idempotency_key)

    @app.post('/api/hierarchy/roots')
    async def root(values: HierarchyRoot, actor=Depends(human)):
        return store.create_root(actor, values.beneficiary_id, values.policy, values.idempotency_key)

    @app.get('/api/hierarchy/delegations/{identifier}')
    async def inspect(identifier: str, actor=Depends(human)):
        return store.inspect(actor, identifier)

    @app.post('/api/hierarchy/delegations/{identifier}/change')
    async def change(identifier: str, values: HierarchyChange, actor=Depends(human)):
        return store.change(actor, identifier, values.expected_version, values.idempotency_key,
                            policy=values.policy, revoke=values.revoke)

    @app.post('/api/hierarchy/projects/{project_id}/archive')
    async def archive_project(project_id: str, values: HierarchyProjectArchive, actor=Depends(human)):
        return store.archive_project_lead(actor, project_id, values.project_version,
                                          values.profile_version, values.idempotency_key)

    @app.get('/api/hierarchy/projects/{project_id}/archive-preview')
    async def archive_preview(project_id: str, actor=Depends(human)):
        return store.project_archive_preview(actor, project_id)
