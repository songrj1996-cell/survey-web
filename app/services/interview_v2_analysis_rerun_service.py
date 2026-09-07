"""Batch 6B2: one-module analysis replacement, never an automatic report rewrite."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any

from pydantic import ValidationError

from app.core.config import (
    INTERVIEW_V2_ANALYSIS_MAX_TOKENS,
    INTERVIEW_V2_ANALYSIS_MODEL,
    INTERVIEW_V2_ANALYSIS_REASONING,
    INTERVIEW_V2_MODEL_FALLBACKS,
)
from app.core.interview_v2_analysis import (
    InterviewV2AnalysisValidationError,
    analysis_module_rerun_fingerprint,
    build_analysis_input,
    build_analysis_module_rerun_input,
    merge_analysis_module_result,
    validate_module_findings,
)
from app.core.security import _owner_from_login, _visible_to_owner
from app.integrations.llm_client import collect_chat_completion
from app.schemas.interview_v2_rerun import InterviewV2AnalysisModuleRerunRequest
from app.services.interview_v2_analysis_service import _error, _frozen_dossiers, _now, _public
from app.services.interview_v2_dossier_service import _manifest, _ready_project
from app.services.interview_v2_import_service import InterviewV2ImportError
from app.services.interview_v2_report_rerun_service import validate_report_rerun_idempotency_key
from app.storage import interview_v2_store as store
from app.storage.prompts import _load_prompts


def _load_accessible_base(project_id: str, request: dict[str, Any], login: dict | None) -> dict[str, Any]:
    try:
        project = store.load_project(project_id)
        if project is None or not _visible_to_owner(project, login):
            raise _error("INTERVIEW_ANALYSIS_NOT_FOUND", "未找到该分析版本。", status=404)
        saved = store.load_analysis_run(project_id, request["base_analysis_run_id"])
    except (OSError, TypeError, ValueError) as exc:
        raise _error("ANALYSIS_PERSISTENCE_FAILED", "分析版本读取失败。", status=500, retryable=True) from exc
    if saved is None:
        raise _error("INTERVIEW_ANALYSIS_NOT_FOUND", "未找到该分析版本。", status=404)
    modules = (saved["revision"].get("model_usage") or {}).get("modules") or []
    if sum(item.get("module_id") == request["module_id"] for item in modules) != 1:
        raise _error("ANALYSIS_MODULE_NOT_FOUND", "该分析版本中没有此模块。", status=404)
    return saved


def validate_analysis_rerun_access(project_id: str, request: dict[str, Any], login: dict | None) -> None:
    """Read-only owner and scope authorization before requesting any LLM key."""
    _load_accessible_base(project_id, request, login)


def _prompt_bundle() -> tuple[str, dict[str, Any]]:
    key = "interview_v2_analysis_system"
    entry = _load_prompts().get(key)
    if not isinstance(entry, dict) or not isinstance(entry.get("current"), str) or not entry["current"].strip():
        raise ValueError("analysis system prompt is unavailable")
    text = entry["current"]
    return text, {key: {
        "version": int(entry.get("version") or 1),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }}


def _prepare_input(project_id: str, login: dict | None, base: dict, module_id: str, prompts: dict, models: dict) -> dict:
    if not store.is_analysis_run_current(
        project_id, analysis_run_id=base["analysis_run_id"],
        revision_payload_sha256=base["revision_payload_sha256"],
    ):
        raise _error("ANALYSIS_INPUT_CHANGED", "基础分析或上游输入已变化，请刷新后重试。")
    try:
        _, evidence, boundary, coverage, source = _ready_project(project_id, login)
        dossiers, unreviewed, versions = _frozen_dossiers(project_id, _manifest(evidence), source)
        analysis_input = build_analysis_input(
            project_id=project_id, source={**source, "dossier_versions": versions},
            evidence_revision=evidence, analysis_boundary=boundary,
            coverage_revision=coverage.get("coverage_preview") or coverage,
            dossier_revisions=dossiers, unreviewed_participant_ids=unreviewed,
        )
        return build_analysis_module_rerun_input(
            base_revision=base, analysis_input=analysis_input, module_id=module_id,
            prompt_snapshot=prompts, model_configuration=models,
        )
    except InterviewV2AnalysisValidationError as exc:
        raise _error("ANALYSIS_INPUT_CHANGED", "当前输入与基础分析不一致，不能复用旧模块。") from exc


def _rerun_public(saved: dict, operation: dict, *, reused: bool) -> dict[str, Any]:
    revision = saved["revision"]
    is_current = store.is_analysis_run_current(
        operation["project_id"], analysis_run_id=revision["analysis_run_id"],
        revision_payload_sha256=revision["revision_payload_sha256"],
    )
    # A replay may refer to an older completed result; do not label it as the head.
    historical = {**saved, "state": {**saved["state"], "current_version_number": revision["version_number"]}}
    return {
        **_public(operation["project_id"], historical, status_override=None if is_current else "stale"),
        "is_current_version": is_current,
        "rerun": {
            "from_stage": "analysis_module", "rerun_id": operation["rerun_id"],
            "base_analysis_run_id": operation["base_analysis_run_id"],
            "analysis_run_id": operation["analysis_run_id"], "module_id": operation["module_id"],
            "status": "completed", "reused": reused,
            "input_fingerprint": operation["request_fingerprint"],
            "reports_rewritten": False,
        },
    }


async def create_analysis_module_rerun(
    project_id: str, request: dict[str, Any], login: dict | None, idempotency_key: str,
) -> dict[str, Any]:
    try:
        request = InterviewV2AnalysisModuleRerunRequest.model_validate(request).model_dump(mode="json")
    except ValidationError as exc:
        raise _error("ANALYSIS_RERUN_REQUEST_INVALID", "模块重跑请求格式无效。", status=400) from exc
    key = validate_report_rerun_idempotency_key(idempotency_key)
    base = _load_accessible_base(project_id, request, login)["revision"]
    actor = str(_owner_from_login(login).get("owner_key") or "")
    owner = actor or "local:anonymous"
    models = {
        "models": [INTERVIEW_V2_ANALYSIS_MODEL, *INTERVIEW_V2_MODEL_FALLBACKS],
        "max_tokens": INTERVIEW_V2_ANALYSIS_MAX_TOKENS,
        "reasoning_effort": INTERVIEW_V2_ANALYSIS_REASONING,
    }
    try:
        prompt_text, prompts = _prompt_bundle()
        fingerprint = analysis_module_rerun_fingerprint(
            base_revision=base, module_id=request["module_id"],
            prompt_snapshot=prompts, model_configuration=models,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _error("ANALYSIS_RERUN_INPUT_INVALID", "基础分析或重跑配置未通过校验。", status=422) from exc
    operation_args = {
        "owner_key": owner, "project_id": project_id,
        "idempotency_key": key, "request_fingerprint": fingerprint,
    }
    try:
        operation = store.claim_analysis_module_rerun(
            **operation_args, base_analysis_run_id=base["analysis_run_id"],
            base_revision_payload_sha256=base["revision_payload_sha256"],
            module_id=request["module_id"], created_at=_now(),
        )
        if operation["status"] == "completed":
            saved = store.load_analysis_run(project_id, operation["analysis_run_id"])
            if saved is None:
                raise ValueError("completed analysis rerun is missing")
            return _rerun_public(saved, operation, reused=True)
    except store.AnalysisRerunIdempotencyConflictError as exc:
        raise _error("RERUN_IDEMPOTENCY_CONFLICT", "该 Idempotency-Key 已绑定其他重跑输入。") from exc
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise _error("ANALYSIS_PERSISTENCE_FAILED", "模块重跑状态读取或保存失败。", status=500, retryable=True) from exc
    if not operation.get("_claim_acquired"):
        raise _error("RERUN_IN_PROGRESS", "相同模块重跑正在处理中，请稍后使用同一请求重试。", retryable=True)

    committed = False
    try:
        rerun_input = _prepare_input(project_id, login, base, request["module_id"], prompts, models)
        module_input = rerun_input["module_input"]
        model_payload = {
            "analysis_schema_version": rerun_input["analysis_schema_version"],
            "source": rerun_input["source"],
            "unreviewed_participant_ids": rerun_input["unreviewed_participant_ids"],
            **module_input,
        }
        try:
            output, model = await collect_chat_completion(
                [{"role": "system", "content": prompt_text}, {
                    "role": "user", "content": "<untrusted_interview_data>\n"
                    + json.dumps(model_payload, ensure_ascii=False) + "\n</untrusted_interview_data>",
                }],
                models=tuple(models["models"]), max_tokens=models["max_tokens"],
                reasoning_effort=models["reasoning_effort"],
            )
        except Exception as exc:
            raise _error("ANALYSIS_RERUN_FAILED", "模块分析调用失败，未创建新版本。", status=502, retryable=True) from exc
        try:
            raw = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", str(output).strip()))
            if not isinstance(raw, dict) or set(raw) != {"module_id", "findings"}:
                raise ValueError("module output must contain exactly module_id and findings")
            if raw["module_id"] != request["module_id"]:
                raise ValueError("module output identity mismatch")
            validated = validate_module_findings(
                raw, module_input=module_input, analysis_run_id=operation["analysis_run_id"],
            )
            merged = merge_analysis_module_result(
                base_revision=base, module_id=request["module_id"], result=validated,
                analysis_run_id=operation["analysis_run_id"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _error("ANALYSIS_RERUN_MODEL_OUTPUT_INVALID", "模块分析输出未通过证据或统计校验，未创建新版本。", status=502, retryable=True) from exc
        revision = deepcopy(base)
        for field in ("version_number", "revision_payload_sha256"):
            revision.pop(field, None)
        revision.update(merged)
        revision.update({
            "analysis_run_id": operation["analysis_run_id"], "created_at": _now(),
            "created_by": actor, "status": "completed",
            "rerun": {
                "rerun_id": operation["rerun_id"], "from_stage": "analysis_module",
                "module_id": request["module_id"], "base_analysis_run_id": base["analysis_run_id"],
                "input_fingerprint": fingerprint, "prompt_snapshot": prompts,
                "model_configuration": models, "frozen_module_input": rerun_input,
            },
        })
        for item in revision["model_usage"]["modules"]:
            if item["module_id"] == request["module_id"]:
                item.update({"model": model, "rerun_id": operation["rerun_id"], "prompts": prompts})
        try:
            saved = store.save_analysis_module_rerun_cas(**operation_args, revision=revision)
        except ValueError as exc:
            if str(exc) in {"analysis version conflict", "analysis input changed"}:
                raise _error("ANALYSIS_INPUT_CHANGED", "分析或上游版本在运行期间发生变化，请刷新后重试。") from exc
            raise
        committed = True
        return _rerun_public(saved, saved["operation"], reused=False)
    except InterviewV2ImportError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise _error("ANALYSIS_PERSISTENCE_FAILED", "模块重跑未完成版本提交，请刷新后重试。", status=500, retryable=True) from exc
    finally:
        if not committed:
            try:
                store.release_analysis_module_rerun(**operation_args, analysis_run_id=operation["analysis_run_id"])
            except (OSError, KeyError, TypeError, ValueError) as exc:
                raise _error("ANALYSIS_PERSISTENCE_FAILED", "模块重跑的占位清理未完成，请稍后重试。", status=500, retryable=True) from exc
