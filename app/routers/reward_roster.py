"""奖励整理 HTTP 接口。"""

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from app.core.responses import _make_download_response
from app.schemas.reward_roster import RewardAnalyzeRequest, RewardResolveRequest
from app.services.audit import audit_log
from app.services.auth import _current_login, _require_feature
from app.services.reward_roster import (
    analyze_reward_session,
    create_reward_session,
    get_reward_session,
    resolve_reward_conflicts,
    reward_download,
)
from app.services.group_access import require_record


router = APIRouter()


async def _require_reward_session(request: Request, session_id: str) -> dict | None:
    login = await _require_feature(request, "reward")
    require_record(get_reward_session(session_id), login, write=request.method != "GET")
    return login


@router.post("/api/reward-roster/upload")
async def upload_reward_workbook(request: Request, file: UploadFile = File(...)):
    login = await _require_feature(request, "reward")
    content = await file.read()
    result = create_reward_session(file.filename or "reward.xlsx", content, login)
    await audit_log(
        request,
        "reward",
        "上传奖励 Excel",
        f"{result['filename']}，{len(result['sheets'])} 个 sheet",
    )
    return result


@router.post("/api/reward-roster/{session_id}/analyze")
async def analyze_reward_workbook(
    session_id: str,
    body: RewardAnalyzeRequest,
    request: Request,
):
    await _require_reward_session(request, session_id)
    result = analyze_reward_session(session_id, body, latest_roster=True)
    await audit_log(
        request,
        "reward",
        "分析奖励数据",
        f"{len(result['selected_sheets'])} 个 sheet，{result['eligible_row_count']} 条有效奖励来源",
    )
    return result


@router.post("/api/reward-roster/{session_id}/resolve")
async def resolve_reward_workbook(
    session_id: str,
    body: RewardResolveRequest,
    request: Request,
):
    await _require_reward_session(request, session_id)
    result = resolve_reward_conflicts(session_id, body)
    await audit_log(
        request,
        "reward",
        "确认奖励冲突",
        f"{result['result']['unique_accounts']} 个账号，{result['result']['total_diamonds']} 钻",
    )
    return result


@router.get("/api/reward-roster/{session_id}/download")
async def download_reward_txt(session_id: str, request: Request):
    await _require_reward_session(request, session_id)
    raise HTTPException(409, "请将已导入的 Excel 纳入奖励整理，核对最新名单并生成批次后下载；此旧地址不再绕过批次防重")
