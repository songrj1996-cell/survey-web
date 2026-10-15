from fastapi import APIRouter, Request
from app.schemas.roster_sources import RosterSourceInput, RosterSourceUpdate, SourceInspect
from app.services.auth import _current_login
from app.services import roster_sources as service

router = APIRouter(prefix="/api/roster-sources")


@router.get("")
async def sources(request: Request):
    return {"sources": service.list_sources(await _current_login(request))}


@router.post("/inspect")
async def inspect(body: SourceInspect, request: Request):
    return await service.inspect_source(body, await _current_login(request))


@router.post("")
async def create(body: RosterSourceInput, request: Request):
    return await service.save_source(body, await _current_login(request))


@router.put("/{source_id}")
async def update(source_id: str, body: RosterSourceUpdate, request: Request):
    return await service.save_source(body, await _current_login(request), source_id)
