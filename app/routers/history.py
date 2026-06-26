"""routers/history:历史记录列表 / 详情 / 改名。"""
from fastapi import APIRouter, HTTPException, Request

from app.core.audit import audit_log
from app.core.security import _current_login, _find_history_for_login, _visible_to_owner
from app.schemas.requests import HistoryTitleUpdateByIdRequest, HistoryTitleUpdateRequest
from app.services.report_history import (
    _history_effective_row_count,
    _qa_user_count,
    _update_history_title_by_id,
)
from app.storage.history import _ensure_history_report_numbers, _load_history

router = APIRouter()


@router.get("/api/history")
async def get_history(request: Request, mode: str = ""):
    login = await _current_login(request)
    history = _load_history()
    history = _ensure_history_report_numbers(history)
    visible_history = [h for h in history if _visible_to_owner(h, login)]
    if mode:
        visible_history = [h for h in visible_history if (h.get("mode") or "") == mode]
    await audit_log(request, "report", "查看历史记录", f"历史报告数：{len(visible_history)}")
    # 列表视图不返回完整 report_md（节省带宽）
    return [
        {
            "id": h["id"],
            "report_no": h.get("report_no", ""),
            "filename": h["filename"],
            "title": h["title"],
            "created_at": h["created_at"],
            "has_qa": bool(h.get("analyst_conv_id")),
            "qa_count": _qa_user_count(h),
            "mode": h.get("mode", ""),
            "row_count": _history_effective_row_count(h),
            "comment_valid_count": h.get("comment_valid_count", 0),
            "comment_sample_count": h.get("comment_sample_count", 0),
            "annotate_ai_count": h.get("annotate_ai_count", 0),
            "annotate_confirmed_ai_count": h.get("annotate_confirmed_ai_count", 0),
            "annotate_quality_count": h.get("annotate_quality_count", 0),
            "annotate_has_download": bool(h.get("annotate_result_path")),
        }
        for h in visible_history
    ]


@router.get("/api/history/{hist_id}")
async def get_history_item(hist_id: str, request: Request):
    login = await _current_login(request)
    history = _load_history()
    history = _ensure_history_report_numbers(history)
    entry = _find_history_for_login(history, hist_id, login)
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
