"""Model administration A/B: versioned configuration only, without LLM calls."""
from copy import deepcopy
import asyncio
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import ValidationError

from app.core import config
from app.core.llm_model_registry import check_configuration, environment_defaults, fingerprint, task_catalog
from app.core.runtime_logging import runtime_log
from app.schemas.llm_model_settings import ModelConfiguration, ModelSettingsDocument
from app.storage import llm_model_settings as storage


def _validated_document(raw):
    if raw is None:
        return None
    try:
        document = ModelSettingsDocument.model_validate(raw).model_dump()
        versions = [document["current"]["version"], *[h["version"] for h in document["history"]]]
        if versions != sorted(set(versions), reverse=True):
            raise ValueError("invalid versions")
        current = document["current"]["configuration"]
        if not check_configuration(current, current)["valid"]:
            raise ValueError("invalid current configuration")
        return document
    except (ValidationError, ValueError) as exc:
        raise storage.ModelSettingsStorageError("模型配置或历史结构异常，请联系管理员核对备份；未覆盖原文件") from exc


def _state(raw):
    document = _validated_document(raw)
    if document:
        current = document["current"]
        configuration, version = current["configuration"], current["version"]
    else:
        try:
            configuration = ModelConfiguration.model_validate(environment_defaults()).model_dump()
        except ValidationError as exc:
            raise storage.ModelSettingsStorageError("环境模型配置格式不符合管理页面要求，请联系管理员核对；未改动环境配置") from exc
        version = 0
    return document, configuration, version, fingerprint(document or {"environment": configuration})


def _storage_failure(exc):
    runtime_log("模型管理", "error", str(exc), http_status=503)
    raise HTTPException(status_code=503, detail=str(exc)) from exc


async def get_model_settings() -> dict:
    try:
        document, configuration, version, revision = _state(await asyncio.to_thread(storage.read_document))
    except storage.ModelSettingsStorageError as exc:
        _storage_failure(exc)
    return {
        "version": version, "revision": revision, "configuration": deepcopy(configuration),
        "tasks": task_catalog(), "history": deepcopy(([document["current"], *document["history"]]) if document else []),
        "runtime_active": False, "source": "saved" if document else "environment",
        "message": "当前完成配置管理；实际业务尚未接入，保存版本不会切换现有业务模型。",
        "check": check_configuration(configuration, configuration),
    }


def _require_revision(actual, expected):
    if actual != expected:
        raise HTTPException(status_code=409, detail="配置已被其他管理员更新；你的草稿仍已保留，请加载最新版本后重新核对")


async def check_model_settings(request) -> dict:
    state = await get_model_settings()
    _require_revision(state["revision"], request.expected_revision)
    configuration, baseline = request.configuration.model_dump(), state["configuration"]
    if request.restored_from is not None:
        target = next((h for h in state["history"] if h["version"] == request.restored_from), None)
        if not target or fingerprint(target["configuration"]) != fingerprint(configuration):
            raise HTTPException(status_code=409, detail="恢复草稿与所选历史版本不一致，请重新选择历史版本")
        baseline = target["configuration"]
    result = check_configuration(configuration, baseline)
    return {**result, "revision": state["revision"], "runtime_active": False}


async def publish_model_settings(request, actor_name: str) -> dict:
    configuration = request.configuration.model_dump()

    def update(raw):
        document, previous, version, revision = _state(raw)
        _require_revision(revision, request.expected_revision)
        baseline = previous
        if request.restored_from is not None:
            candidates = [document["current"], *document["history"]] if document else []
            target = next((v for v in candidates if v["version"] == request.restored_from), None)
            if not target or fingerprint(target["configuration"]) != fingerprint(configuration):
                raise HTTPException(status_code=409, detail="恢复草稿与所选历史版本不一致，请重新选择历史版本")
            baseline = target["configuration"]
        check = check_configuration(configuration, baseline)
        if not check["valid"]:
            raise HTTPException(status_code=422, detail={"message": "配置检查未通过，未保存任何修改", **check})
        if fingerprint(configuration) == fingerprint(previous):
            raise HTTPException(status_code=409, detail="配置没有变化，无需保存新版本")
        entry = {"version": version + 1, "configuration": configuration,
                 "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "actor_name": str(actor_name or "管理员")[:100], "note": request.note,
                 "restored_from": request.restored_from}
        history = [document["current"], *document["history"]] if document else []
        return {"schema_version": 1, "current": entry, "history": history[:config.LLM_MODEL_SETTINGS_HISTORY_LIMIT - 1]}

    try:
        document = await asyncio.to_thread(storage.update_document, update)
    except storage.ModelSettingsStorageError as exc:
        _storage_failure(exc)
    except HTTPException as exc:
        runtime_log("模型管理", "warning", "模型配置未保存，请根据页面提示核对后重试", http_status=exc.status_code)
        raise
    runtime_log("模型管理", "success", f"模型配置版本 v{document['current']['version']} 已保存；实际业务尚未接入")
    # Return the exact version written, rather than rereading a concurrent publication.
    _, saved, version, revision = _state(document)
    return {"version": version, "revision": revision, "configuration": deepcopy(saved),
            "tasks": task_catalog(), "history": [document["current"], *document["history"]],
            "runtime_active": False, "source": "saved", "message": "配置版本已保存；实际业务尚未接入。",
            "check": check_configuration(saved, saved)}


async def restore_model_settings(version: int, request) -> dict:
    state = await get_model_settings()
    _require_revision(state["revision"], request.expected_revision)
    target = next((h for h in state["history"] if h["version"] == version), None)
    if not target:
        raise HTTPException(status_code=404, detail="所选配置版本不存在或已超过保留范围")
    return {"configuration": deepcopy(target["configuration"]), "restored_from": version,
            "revision": state["revision"], "runtime_active": False,
            "message": "已恢复为草稿，尚未保存新版本；实际业务配置没有变化。"}


async def build_model_snapshot() -> dict:
    """Detached infrastructure for C; no caller or runtime behavior is changed in A/B."""
    state = await get_model_settings()
    return {"version": state["version"], "revision": state["revision"], "configuration": deepcopy(state["configuration"])}
