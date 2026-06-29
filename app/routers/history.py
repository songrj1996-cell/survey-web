"""routers/history:历史记录列表 / 详情 / 改名（HTTP 壳）。

列表查询与详情在 services/history_service，改名在 services/report_history。
"""
from fastapi import APIRouter, HTTPException, Request

from app.schemas.requests import HistoryTitleUpdateByIdRequest, HistoryTitleUpdateRequest
from app.services.audit import audit_log
from app.services.auth import _current_login
from app.services.history_service import get_history_entry, get_history_list
from app.services.report_history import _update_history_title_by_id

router = APIRouter()


@router.get("/api/history")
async def get_history(request: Request, mode: str = ""):
    login = await _current_login(request)
    result = get_history_list(login, mode)
    await audit_log(request, "report", "查看历史记录", f"历史报告数：{len(result)}")
    return result


@router.get("/api/history/{hist_id}")
async def get_history_item(hist_id: str, request: Request):
    login = await _current_login(request)
    entry = get_history_entry(hist_id, login)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    await audit_log(request, "report", "打开历史报告", f"报告：{entry.get('title', hist_id)}", metadata={"history_id": hist_id})
    return entry


@router.patch("/api/history-title")
async def update_history_title_by_body(req: HistoryTitleUpdateByIdRequest, request: Request):
    login = await _current_login(request)
    result = _update_history_title_by_id(req.id, req.title, login)
    await audit_log(request, "report", "修改报告名称", f"{req.id} → {result.get('title', req.title)}", metadata={"history_id": req.id})
    return result


@router.post("/api/history-title")
async def update_history_title_by_body_post(req: HistoryTitleUpdateByIdRequest, request: Request):
    login = await _current_login(request)
    result = _update_history_title_by_id(req.id, req.title, login)
    await audit_log(request, "report", "修改报告名称", f"{req.id} → {result.get('title', req.title)}", metadata={"history_id": req.id})
    return result


@router.patch("/api/history/{hist_id}/title")
async def update_history_title(hist_id: str, req: HistoryTitleUpdateRequest, request: Request):
    login = await _current_login(request)
    result = _update_history_title_by_id(hist_id, req.title, login)
    await audit_log(request, "report", "修改报告名称", f"{hist_id} → {result.get('title', req.title)}", metadata={"history_id": hist_id})
    return result


@router.post("/api/history/{hist_id}/title")
async def update_history_title_post(hist_id: str, req: HistoryTitleUpdateRequest, request: Request):
    login = await _current_login(request)
    result = _update_history_title_by_id(hist_id, req.title, login)
    await audit_log(request, "report", "修改报告名称", f"{hist_id} → {result.get('title', req.title)}", metadata={"history_id": hist_id})
    return result
