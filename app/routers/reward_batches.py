from fastapi import APIRouter, Request
from app.core.responses import _make_download_response
from app.schemas.reward_batches import BatchGenerate, BatchResolution, OtherRewardCreate, RewardSelection
from app.services.auth import _current_login
from app.services import reward_batches as service

router = APIRouter(prefix="/api/reward-batches")


@router.get("/choices")
async def choices(request: Request):
    return service.choices(await _current_login(request))


@router.post("/other-rewards")
async def other_rewards(body: OtherRewardCreate, request: Request):
    return await service.create_other_reward(body, await _current_login(request))


@router.post("/previews")
async def create_preview(body: RewardSelection, request: Request):
    return await service.create_preview(body, await _current_login(request))


@router.get("/previews/{preview_id}")
async def preview(preview_id: str, request: Request):
    return service.get_preview(preview_id, await _current_login(request))


@router.put("/previews/{preview_id}")
async def resolve(preview_id: str, body: BatchResolution, request: Request):
    return service.resolve_preview(preview_id, body, await _current_login(request))


@router.post("/previews/{preview_id}/generate")
async def generate(preview_id: str, body: BatchGenerate, request: Request):
    return await service.generate_batch(preview_id, body, await _current_login(request))


@router.get("/{batch_id}")
async def batch(batch_id: str, request: Request):
    return service.get_batch(batch_id, await _current_login(request))


@router.get("/{batch_id}/download")
async def download(batch_id: str, request: Request):
    content, filename = service.download_batch(batch_id, await _current_login(request))
    return _make_download_response(content, "text/plain; charset=utf-8", filename)
