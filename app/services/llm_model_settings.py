"""Versioned model administration, task snapshots and stage routing."""
from copy import deepcopy
import asyncio
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
import inspect
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import ValidationError

from app.core import config
from app.core.llm_model_registry import check_configuration, environment_defaults, fingerprint, task_catalog
from app.core.runtime_logging import runtime_log
from app.core.llm_context import bind_llm_model_snapshot, current_llm_model_snapshot, bind_llm_model_budget, current_llm_model_budget
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
        "runtime_active": True, "source": "saved" if document else "environment",
        "message": "模型配置已接入业务；发布后用于新任务，进行中及恢复的任务沿用原配置。",
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
    return {**result, "revision": state["revision"], "runtime_active": True}


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
    runtime_log("模型管理", "success", f"模型配置版本 v{document['current']['version']} 已发布；新任务使用此版本，进行中及恢复的任务沿用原配置")
    # Return the exact version written, rather than rereading a concurrent publication.
    _, saved, version, revision = _state(document)
    return {"version": version, "revision": revision, "configuration": deepcopy(saved),
            "tasks": task_catalog(), "history": [document["current"], *document["history"]],
            "runtime_active": True, "source": "saved", "message": "配置版本已发布；新任务使用此版本，进行中及恢复的任务沿用原配置。",
            "check": check_configuration(saved, saved)}


async def restore_model_settings(version: int, request) -> dict:
    state = await get_model_settings()
    _require_revision(state["revision"], request.expected_revision)
    target = next((h for h in state["history"] if h["version"] == version), None)
    if not target:
        raise HTTPException(status_code=404, detail="所选配置版本不存在或已超过保留范围")
    return {"configuration": deepcopy(target["configuration"]), "restored_from": version,
            "revision": state["revision"], "runtime_active": True,
            "message": "已恢复为草稿，尚未保存新版本；实际业务配置没有变化。"}


async def build_model_snapshot() -> dict:
    """Build a detached, validated snapshot before starting an operation."""
    state = await get_model_settings()
    return {"version": state["version"], "revision": state["revision"],
            "configuration_sha256": fingerprint(state["configuration"]),
            "configuration": deepcopy(state["configuration"])}


def validate_model_snapshot(snapshot):
    if not isinstance(snapshot, dict) or type(snapshot.get("version")) is not int or snapshot["version"] < 0:
        raise ValueError("原任务模型配置快照不完整，请完整重新生成；未切换到当前配置")
    try:
        value = ModelConfiguration.model_validate(snapshot.get("configuration")).model_dump()
    except ValidationError as exc:
        raise ValueError("原任务模型配置快照无效，未切换到当前配置") from exc
    if snapshot.get("configuration_sha256") != fingerprint(value) or not check_configuration(value, value)["valid"]:
        raise ValueError("原任务模型配置快照校验未通过，未切换到当前配置")
    return deepcopy(snapshot)


@asynccontextmanager
async def model_operation_scope(snapshot=None, *, resume=False):
    inherited = current_llm_model_snapshot()
    if snapshot is not None:
        selected = validate_model_snapshot(snapshot)
        selected["use_saved_defaults"] = bool(selected.get("use_saved_defaults") or (resume and selected["version"] == 0 and selected["configuration_sha256"] != fingerprint(ModelConfiguration.model_validate(environment_defaults()).model_dump())))
    elif inherited is not None:
        selected = inherited
    else:
        selected = await build_model_snapshot()
    if snapshot is None and resume and selected["version"] > 0:
        raise ValueError("原任务没有保存模型配置，无法确认原设置；请完整重新生成，不会自动切换模型续跑")
    with bind_llm_model_snapshot(selected):
        yield selected


def model_operation(function=None, *, resume_from=None, persist_result=False, budget_key=None):
    """Freeze a root operation; checkpoints resume only their own saved settings."""
    if function is None:
        return lambda fn: model_operation(fn, resume_from=resume_from, persist_result=persist_result, budget_key=budget_key)
    signature = inspect.signature(function)

    def recovery(args, kwargs):
        bound = signature.bind(*args, **kwargs)
        source = bound.arguments.get(resume_from) if resume_from else None
        if resume_from == "checkpoint" and not bound.arguments.get("retry_failed", False):
            source = None
        return source

    @asynccontextmanager
    async def scope(args, kwargs):
        source = recovery(args, kwargs)
        if source is not None and source.get("llm_model_snapshot") is not None:
            async with model_operation_scope(source["llm_model_snapshot"], resume=True):
                yield
        else:
            async with model_operation_scope():
                if source is not None:
                    async with model_operation_scope(resume_model_snapshot(source), resume=True):
                        yield
                else:
                    yield

    if inspect.isasyncgenfunction(function):
        @wraps(function)
        async def stream(*args, **kwargs):
            # A streaming iterator may be advanced or closed by different tasks.
            # Never retain ContextVar reset tokens across a yielded SSE event.
            async with scope(args, kwargs):
                selected = current_llm_model_snapshot()
                key = budget_key(signature.bind(*args, **kwargs).arguments) if callable(budget_key) else budget_key
                with model_attempt_budget(key):
                    budget = current_llm_model_budget()
            iterator = function(*args, **kwargs)
            try:
                while True:
                    try:
                        async with model_operation_scope(selected):
                            with bind_llm_model_budget(budget):
                                item = await anext(iterator)
                    except StopAsyncIteration:
                        break
                    yield item
            finally:
                async with model_operation_scope(selected):
                    with bind_llm_model_budget(budget):
                        await iterator.aclose()
        return stream
    @wraps(function)
    async def call(*args, **kwargs):
        async with scope(args, kwargs):
            key = budget_key(signature.bind(*args, **kwargs).arguments) if callable(budget_key) else budget_key
            with model_attempt_budget(key):
                result = await function(*args, **kwargs)
            if persist_result and isinstance(result, dict):
                record_model_snapshot(result)
            return result
    return call


def task_model_settings(key: str):
    snapshot = current_llm_model_snapshot()
    if snapshot is None or (snapshot["version"] == 0 and not snapshot.get("use_saved_defaults")):
        return None
    settings = snapshot["configuration"]["tasks"].get(key)
    if settings is None:
        raise ValueError("此业务环节不在原模型配置中，请完整重新生成")
    return deepcopy(settings)


def task_model_chain(key: str, legacy):
    settings = task_model_settings(key)
    return tuple(c["model"] for c in [settings["primary"], *settings["fallbacks"]] if c["model"]) if settings else tuple(legacy)


def model_call_options(key: str, options: dict, *, selected_model=None, preferred_model=None):
    settings = task_model_settings(key)
    if settings is None:
        return dict(options)
    snapshot = current_llm_model_snapshot()
    candidates = [settings["primary"], *settings["fallbacks"]]
    if selected_model is not None:
        candidates = [c for c in candidates if c["model"] == selected_model]
        if not candidates:
            raise ValueError("本次选择的模型不在冻结配置中")
    if preferred_model is not None:
        candidates.sort(key=lambda c: c["model"] != preferred_model)
    models = {m["id"]: m for m in snapshot["configuration"]["models"]}
    candidates = [{**c, "capabilities": deepcopy(models.get(c["model"]))} for c in candidates if c["model"]]
    if not candidates:
        raise ValueError("当前业务环节尚未配置可用模型")
    result = {**options, "models": tuple(c["model"] for c in candidates),
              "max_tokens": candidates[0]["max_tokens"],
              "reasoning_effort": None if candidates[0]["reasoning"] == "default" else candidates[0]["reasoning"],
              "candidate_settings": candidates,
              "model_config_version": snapshot["version"], "model_task_key": key}
    if settings["timeout_seconds"] is not None:
        result["timeout_seconds"] = settings["timeout_seconds"]
    if settings["max_http_attempts"] is not None:
        original = options.get("max_http_attempts")
        result["max_http_attempts"] = min(original, settings["max_http_attempts"]) if original else settings["max_http_attempts"]
    return result


async def call_model(collector, key: str, messages, *, selected_model=None, preferred_model=None, **options):
    async with model_operation_scope():
        resolved = model_call_options(key, options, selected_model=selected_model, preferred_model=preferred_model)
        budget = current_llm_model_budget()
        limit = resolved.get("timeout_seconds")
        if budget and budget["key"] == key and budget.get("deadline") is not None:
            limit = max(0, budget["deadline"] - time.monotonic())
        if limit is not None:
            async with asyncio.timeout(limit):
                return await collector(messages, **resolved)
        return await collector(messages, **resolved)


def record_model_snapshot(target: dict):
    snapshot = current_llm_model_snapshot()
    if snapshot is not None:
        target["llm_model_snapshot"] = deepcopy(snapshot)


def model_configuration_fingerprint():
    snapshot = current_llm_model_snapshot()
    return snapshot.get("configuration_sha256") if snapshot and snapshot["version"] > 0 else None


def business_model_key(kind: str, source=None):
    snapshot = current_llm_model_snapshot() or {}
    business = (source or {}).get("mode") or snapshot.get("business")
    return ("quant." if business in {"quant", "crosstab"} else "survey.") + kind


def resume_model_snapshot(source: dict):
    saved = source.get("llm_model_snapshot")
    if saved is not None:
        saved = validate_model_snapshot(saved)
        return saved
    current = current_llm_model_snapshot()
    if current and current["version"] > 0:
        raise ValueError("原任务缺少模型配置快照，请完整重新生成；未使用当前配置补做旧任务")
    return current


def has_task_model(key: str, legacy):
    """Read-only admission check; execution still freezes and validates its own version."""
    snapshot = current_llm_model_snapshot()
    if snapshot is None:
        try:
            _, configuration, version, _ = _state(storage.read_document())
        except storage.ModelSettingsStorageError as exc:
            _storage_failure(exc)
        if version == 0:
            return bool(any(legacy))
    else:
        if snapshot["version"] == 0 and not snapshot.get("use_saved_defaults"):
            return bool(any(legacy))
        configuration = snapshot["configuration"]
    settings = configuration["tasks"][key]
    return bool(settings["primary"]["model"] or any(c["model"] for c in settings["fallbacks"]))


@contextmanager
def model_attempt_budget(key):
    settings = task_model_settings(key) if key is not None else None
    existing = current_llm_model_budget()
    if settings is None or existing and existing["key"] == key:
        yield
        return
    seconds = settings["timeout_seconds"]
    with bind_llm_model_budget({"key": key, "remaining": settings["max_http_attempts"],
                               "deadline": time.monotonic() + seconds if seconds else None}):
        yield
