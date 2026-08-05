"""routers/settings_api:上传说明 / 提示词 / 页面文案 / 系统设置接口（HTTP 壳）。

读写逻辑全部在 services/settings_service。
"""
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.core.responses import _make_download_response
from app.core.text import _short_text
from app.schemas.requests import (
    AppSettingsPatch,
    GlossaryCreateRequest,
    GlossaryUpdateRequest,
    PromptUpdateRequest,
    UiTextUpdateRequest,
)
from app.services.audit import audit_log
from app.services.auth import _require_admin
from app.services.glossary_service import (
    build_glossary_template_xlsx,
    commit_glossary_import,
    create_glossary_item,
    delete_glossary_item,
    export_glossary_xlsx,
    get_glossary_catalog,
    preview_glossary_import,
    resolve_expected_revision,
    update_glossary_item,
)
from app.services.settings_service import (
    get_all_prompts,
    get_all_ui_texts,
    get_app_settings,
    get_upload_guide,
    update_app_settings,
    update_prompt,
    update_ui_text,
)

router = APIRouter()
_MAX_GLOSSARY_UPLOAD_BYTES = 10 * 1024 * 1024


def _request_payload(request_model, *, exclude: set[str] | None = None) -> dict:
    excluded = exclude or set()
    if hasattr(request_model, "model_dump"):
        return request_model.model_dump(exclude_unset=True, exclude=excluded)
    return request_model.dict(exclude_unset=True, exclude=excluded)


async def _read_glossary_upload(file: UploadFile) -> bytes:
    filename = str(file.filename or "").strip().lower()
    if not filename.endswith(".xlsx"):
        raise HTTPException(status_code=422, detail="请上传 .xlsx 格式的术语库文件")
    content = await file.read(_MAX_GLOSSARY_UPLOAD_BYTES + 1)
    if len(content) > _MAX_GLOSSARY_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="术语库文件不能超过 10MB")
    return content


@router.get("/api/upload-guide")
async def get_upload_guide_endpoint():
    return {"content": get_upload_guide()}


@router.get("/api/prompts")
async def get_prompts(request: Request):
    await _require_admin(request)
    return get_all_prompts()


@router.put("/api/prompts/{key}")
async def update_prompt_endpoint(
    key: str,
    req: PromptUpdateRequest,
    request: Request,
    expected_revision: str,
):
    await _require_admin(request)
    update_prompt(
        key,
        req.content,
        req.note or "",
        expected_revision=expected_revision,
    )
    await audit_log(request, "settings", "修改 Prompt", f"{key}；备注：{_short_text(req.note or '')}")
    return {"ok": True, "key": key}


@router.get("/api/ui-texts")
async def get_ui_texts():
    return get_all_ui_texts()


@router.put("/api/ui-texts/{key}")
async def update_ui_text_endpoint(key: str, req: UiTextUpdateRequest, request: Request):
    await _require_admin(request)
    update_ui_text(key, req.content)
    await audit_log(request, "settings", "修改页面文案", f"{key}；内容：{_short_text(req.content)}")
    return {"ok": True, "key": key}


@router.get("/api/app-settings")
async def get_app_settings_endpoint(request: Request):
    await _require_admin(request)
    return get_app_settings()


@router.patch("/api/app-settings")
async def update_app_settings_endpoint(req: AppSettingsPatch, request: Request):
    await _require_admin(request)
    settings, detail = update_app_settings(req)
    await audit_log(request, "settings", "修改平台设置", detail)
    return settings


@router.get("/api/glossary")
async def get_glossary_endpoint(request: Request):
    await _require_admin(request)
    return get_glossary_catalog()


@router.post("/api/glossary")
async def create_glossary_endpoint(
    req: GlossaryCreateRequest,
    request: Request,
    expected_revision: int | None = None,
):
    await _require_admin(request)
    revision = resolve_expected_revision(expected_revision, req.expected_revision)
    result = create_glossary_item(
        _request_payload(req, exclude={"expected_revision"}),
        revision,
    )
    await audit_log(request, "settings", "新增术语", f"{result['item']['category']} / {result['item']['ch']}")
    return result


@router.patch("/api/glossary/{item_id}")
async def update_glossary_endpoint(
    item_id: str,
    req: GlossaryUpdateRequest,
    request: Request,
    expected_revision: int | None = None,
):
    await _require_admin(request)
    revision = resolve_expected_revision(expected_revision, req.expected_revision)
    result = update_glossary_item(
        item_id,
        _request_payload(req, exclude={"expected_revision"}),
        revision,
    )
    await audit_log(request, "settings", "更新术语", f"{result['item']['category']} / {result['item']['ch']}")
    return result


@router.delete("/api/glossary/{item_id}")
async def delete_glossary_endpoint(
    item_id: str,
    request: Request,
    expected_revision: int | None = None,
):
    await _require_admin(request)
    revision = resolve_expected_revision(expected_revision, None)
    result = delete_glossary_item(item_id, revision)
    await audit_log(request, "settings", "删除术语", item_id)
    return result


@router.get("/api/glossary/template")
async def download_glossary_template(request: Request):
    await _require_admin(request)
    content = build_glossary_template_xlsx()
    await audit_log(request, "settings", "下载术语模板", "glossary-template.xlsx")
    return _make_download_response(
        content,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "glossary-template.xlsx",
    )


@router.get("/api/glossary/export")
async def export_glossary_endpoint(request: Request):
    await _require_admin(request)
    content = export_glossary_xlsx()
    filename = f"glossary-export-{datetime.now():%Y%m%d-%H%M%S}.xlsx"
    await audit_log(request, "settings", "导出术语库", filename)
    return _make_download_response(
        content,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename,
    )


@router.post("/api/glossary/import/preview")
async def preview_glossary_import_endpoint(request: Request, file: UploadFile = File(...)):
    await _require_admin(request)
    content = await _read_glossary_upload(file)
    result = preview_glossary_import(content)
    await audit_log(
        request,
        "settings",
        "预览术语导入",
        f"文件：{_short_text(file.filename or '')}；行数：{result['preview']['stats']['total']}",
    )
    return result


@router.post("/api/glossary/import/commit")
async def commit_glossary_import_endpoint(
    request: Request,
    file: UploadFile = File(...),
    file_hash: str = Form(...),
    base_revision: int = Form(...),
):
    await _require_admin(request)
    content = await _read_glossary_upload(file)
    result = commit_glossary_import(content, file_hash, base_revision)
    stats = result["stats"]
    await audit_log(
        request,
        "settings",
        "导入术语库",
        f"新增 {stats['created']}；更新 {stats['updated']}；无变化 {stats['unchanged']}",
    )
    return result
