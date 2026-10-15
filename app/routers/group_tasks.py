"""HTTP boundary for the persistent quality workflow."""
from fastapi import APIRouter, File, Form, Request, UploadFile

from app.schemas.group_tasks import (AnnouncementRequest, ConfirmAI, DuplicateDecision, FeedbackCorrection, MappingUpdate,
                                    PlayerADVDecision, QualityOverride, RunTask, TaskCreate, TaskRulesUpdate, VersionRequest)
from app.services.auth import _current_login
from app.services import group_tasks as service
from app.services.group_task_runner import prepare_run
from app.services.task_announcement import extract_announcement
from app.services.task_records import clipboard_records

router = APIRouter(prefix="/api/group-tasks")


@router.get("")
async def list_tasks(request: Request):
    return {"tasks": service.list_tasks(await _current_login(request))}


@router.post("")
async def create_task(body: TaskCreate, request: Request):
    return service.create_task(body.rules, await _current_login(request))


@router.post("/announcement")
async def announcement(body: AnnouncementRequest, request: Request):
    return await extract_announcement(body.announcement, await _current_login(request))


@router.get("/{task_id}")
async def detail(task_id: str, request: Request):
    login = await _current_login(request)
    return service.task_view(service.load_task(task_id, login), login)


@router.put("/{task_id}/rules")
async def rules(task_id: str, body: TaskRulesUpdate, request: Request):
    return service.update_rules(task_id, body.expected_version, body.rules, await _current_login(request))


@router.post("/{task_id}/upload")
async def upload(task_id: str, request: Request, expected_version: int = Form(...), file: UploadFile = File(...)):
    return service.upload_feedback(task_id, expected_version, file.filename or "feedback.csv", await file.read(), await _current_login(request))


@router.put("/{task_id}/mapping")
async def mapping(task_id: str, body: MappingUpdate, request: Request):
    return service.set_mapping(task_id, body.expected_version, body.mapping, await _current_login(request))


@router.post("/{task_id}/roster")
async def roster(task_id: str, body: VersionRequest, request: Request):
    return await service.refresh_roster(task_id, body.expected_version, await _current_login(request))


@router.put("/{task_id}/feedback/{response_id}")
async def correction(task_id: str, response_id: str, body: FeedbackCorrection, request: Request):
    return service.correct_feedback(task_id, response_id, body, await _current_login(request))


@router.post("/{task_id}/duplicates")
async def duplicates(task_id: str, body: DuplicateDecision, request: Request):
    return service.resolve_duplicate(task_id, body, await _current_login(request))


@router.post("/{task_id}/run")
async def run(task_id: str, body: RunTask, request: Request):
    return await prepare_run(task_id, body, await _current_login(request))


@router.put("/{task_id}/feedback/{response_id}/quality")
async def quality(task_id: str, response_id: str, body: QualityOverride, request: Request):
    return service.override_quality(task_id, response_id, body, await _current_login(request))


@router.post("/{task_id}/confirm-ai")
async def confirm_ai(task_id: str, body: ConfirmAI, request: Request):
    return service.confirm_ai(task_id, body, await _current_login(request))


@router.put("/{task_id}/player-adv")
async def adv(task_id: str, body: PlayerADVDecision, request: Request):
    return service.set_player_adv(task_id, body, await _current_login(request))


@router.post("/{task_id}/confirm")
async def confirm(task_id: str, body: VersionRequest, request: Request):
    return service.confirm_task(task_id, body.expected_version, await _current_login(request))


@router.get("/{task_id}/clipboard")
async def clipboard(task_id: str, version: int, request: Request, source_id: str = ""):
    return await clipboard_records(task_id, version, await _current_login(request), source_id)
