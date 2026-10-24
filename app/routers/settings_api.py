"""routers/settings_api:上传说明 / 提示词 / 页面文案 / 系统设置接口（HTTP 壳）。

读写逻辑全部在 services/settings_service。
"""
from datetime import datetime

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.core.responses import _make_download_response
from app.core.text import _short_text
from app.schemas.requests import (
    AppSettingsPatch,
    GroupManagementPreferencesPatch,
    GlossaryCreateRequest,
    GlossaryUpdateRequest,
    PromptUpdateRequest,
    UiTextUpdateRequest,
)
from app.services.audit import audit_log
from app.services.auth import _require_feature, _require_admin
from app.schemas.llm_model_settings import (
    CheckModelSettingsRequest, PublishModelSettingsRequest, RestoreModelSettingsRequest,
)
from app.services.llm_model_settings import (
    get_model_settings, check_model_settings, publish_model_settings, restore_model_settings,
)
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
    get_app_settings_for_display as get_app_settings,
    get_upload_guide,
    update_app_settings,
    update_group_management_preferences,
    update_prompt,
    update_ui_text,
)

async def _require_settings_access(request: Request):
    path = request.url.path
    feature = ('glossary' if path.startswith('/api/glossary') else 'prompts' if path.startswith('/api/prompts')
               else 'ui_texts' if path.startswith('/api/ui-texts') else 'platform')
    return await _require_feature(request, feature, action='read' if request.method == 'GET' else 'use')


router = APIRouter()
_MAX_GLOSSARY_UPLOAD_BYTES = 10 * 1024 * 1024


@router.get("/api/model-settings")
async def get_model_settings_endpoint(request: Request):
    await _require_admin(request)
    return await get_model_settings()


@router.post("/api/model-settings/check")
async def check_model_settings_endpoint(req: CheckModelSettingsRequest, request: Request):
    await _require_admin(request)
    return await check_model_settings(req)


@router.post("/api/model-settings/publish")
async def publish_model_settings_endpoint(req: PublishModelSettingsRequest, request: Request):
    login = await _require_admin(request)
    result = await publish_model_settings(req, (login or {}).get("name") or "管理员")
    await audit_log(request, "settings", "保存模型配置版本", f"v{result['version']}；配置已保存，实际业务尚未接入")
    return result


@router.post("/api/model-settings/versions/{version}/restore")
async def restore_model_settings_endpoint(version: int, req: RestoreModelSettingsRequest, request: Request):
    await _require_admin(request)
    return await restore_model_settings(version, req)


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
    await _require_settings_access(request)
    return get_all_prompts()


@router.put("/api/prompts/{key}")
async def update_prompt_endpoint(
    key: str,
    req: PromptUpdateRequest,
    request: Request,
    expected_revision: str,
):
    await _require_settings_access(request)
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
    await _require_settings_access(request)
    update_ui_text(key, req.content)
    await audit_log(request, "settings", "修改页面文案", f"{key}；内容：{_short_text(req.content)}")
    return {"ok": True, "key": key}


@router.get("/api/app-settings")
async def get_app_settings_endpoint(request: Request):
    login = await _require_settings_access(request)
    return get_app_settings(login)


@router.patch("/api/group-management/preferences")
async def update_group_management_preferences_endpoint(req: GroupManagementPreferencesPatch, request: Request):
    login = await _require_settings_access(request)
    preferences, detail = update_group_management_preferences(login, req)
    await audit_log(request, "settings", "修改群组管理个人查看范围", detail)
    return preferences


@router.patch("/api/app-settings")
async def update_app_settings_endpoint(req: AppSettingsPatch, request: Request):
    await _require_settings_access(request)
    settings, detail = update_app_settings(req)
    await audit_log(request, "settings", "修改平台设置", detail)
    return settings


@router.get("/api/glossary")
async def get_glossary_endpoint(request: Request):
    await _require_settings_access(request)
    return get_glossary_catalog()


@router.post("/api/glossary")
async def create_glossary_endpoint(
    req: GlossaryCreateRequest,
    request: Request,
    expected_revision: int | None = None,
):
    await _require_settings_access(request)
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
    await _require_settings_access(request)
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
    await _require_settings_access(request)
    revision = resolve_expected_revision(expected_revision, None)
    result = delete_glossary_item(item_id, revision)
    await audit_log(request, "settings", "删除术语", item_id)
    return result


@router.get("/api/glossary/template")
async def download_glossary_template(request: Request):
    await _require_settings_access(request)
    content = build_glossary_template_xlsx()
    await audit_log(request, "settings", "下载术语模板", "glossary-template.xlsx")
    return _make_download_response(
        content,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "glossary-template.xlsx",
    )


@router.get("/api/glossary/export")
async def export_glossary_endpoint(request: Request):
    await _require_settings_access(request)
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
    await _require_settings_access(request)
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
    await _require_settings_access(request)
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
