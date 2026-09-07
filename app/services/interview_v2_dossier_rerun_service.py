"""Batch 6B3: idempotent regeneration of one current participant dossier."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from pydantic import ValidationError

from app.core.config import (
    INTERVIEW_V2_ATTRIBUTE_MAX_TOKENS,
    INTERVIEW_V2_ATTRIBUTE_MODEL,
    INTERVIEW_V2_ATTRIBUTE_REASONING,
    INTERVIEW_V2_DOSSIER_MAX_TOKENS,
    INTERVIEW_V2_DOSSIER_MODEL,
    INTERVIEW_V2_DOSSIER_REASONING,
    INTERVIEW_V2_MODEL_FALLBACKS,
)
from app.core.interview_v2_dossier import (
    InterviewV2DossierValidationError,
    build_participant_input,
    payload_sha256,
    validate_attribute_output,
    validate_dossier_output,
)
from app.core.security import _owner_from_login, _visible_to_owner
from app.integrations.llm_client import collect_chat_completion
from app.schemas.interview_v2_rerun import InterviewV2ParticipantDossierRerunRequest
from app.services.interview_v2_dossier_service import _error, _parse_json, _public, _ready_project
from app.services.interview_v2_import_service import InterviewV2ImportError
from app.services.interview_v2_report_rerun_service import validate_report_rerun_idempotency_key
from app.storage import interview_v2_store as store
from app.storage.prompts import _load_prompts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _owner(login: dict[str, Any] | None) -> str:
    return str(_owner_from_login(login).get("owner_key") or "local:anonymous")


def _load_accessible_base(
    project_id: str, participant_id: str, base_dossier_version_id: str,
    login: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        project = store.load_project(project_id)
        if project is None or not _visible_to_owner(project, login):
            raise _error("INTERVIEW_DOSSIER_NOT_FOUND", "未找到该玩家档案。", status=404)
        saved = store.load_participant_dossier(
            project_id, participant_id, base_dossier_version_id
        )
    except InterviewV2ImportError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise _error(
            "DOSSIER_PERSISTENCE_FAILED", "玩家档案版本读取失败。",
            status=500, retryable=True,
        ) from exc
    if saved is None:
        raise _error("INTERVIEW_DOSSIER_NOT_FOUND", "未找到该玩家档案。", status=404)
    return saved


def validate_dossier_rerun_access(
    project_id: str, request: dict[str, Any], login: dict[str, Any] | None,
) -> None:
    """Authorize project, participant and historical base before API-key lookup."""
    _load_accessible_base(
        project_id, request["participant_id"], request["base_dossier_version_id"], login
    )


def _prompt_bundle() -> tuple[dict[str, str], dict[str, Any]]:
    catalog = _load_prompts()
    texts: dict[str, str] = {}
    snapshot: dict[str, Any] = {}
    for key in ("interview_v2_attribute_system", "interview_v2_dossier_system"):
        entry = catalog.get(key)
        if not isinstance(entry, dict):
            raise ValueError(f"dossier rerun prompt {key} is missing")
        content = str(entry.get("current") or "")
        if not content.strip():
            raise ValueError(f"dossier rerun prompt {key} is empty")
        texts[key] = content
        snapshot[key] = {
            "version": int(entry.get("version") or 1),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
    return texts, snapshot


def _model_configuration() -> dict[str, Any]:
    return {
        "attribute": {
            "models": [INTERVIEW_V2_ATTRIBUTE_MODEL, *INTERVIEW_V2_MODEL_FALLBACKS],
            "max_tokens": INTERVIEW_V2_ATTRIBUTE_MAX_TOKENS,
            "reasoning_effort": INTERVIEW_V2_ATTRIBUTE_REASONING,
        },
        "dossier": {
            "models": [INTERVIEW_V2_DOSSIER_MODEL, *INTERVIEW_V2_MODEL_FALLBACKS],
            "max_tokens": INTERVIEW_V2_DOSSIER_MAX_TOKENS,
            "reasoning_effort": INTERVIEW_V2_DOSSIER_REASONING,
        },
    }


def _fingerprint(
    *, request: dict[str, Any], base: dict[str, Any], source: dict[str, Any],
    participant_input: dict[str, Any], prompt_snapshot: dict[str, Any],
    model_configuration: dict[str, Any],
) -> str:
    return payload_sha256({
        "request": request,
        "base_dossier_version_id": base["dossier_version_id"],
        "base_revision_payload_sha256": base["revision_payload_sha256"],
        "source": source,
        "participant_input": participant_input,
        "prompt_snapshot": prompt_snapshot,
        "model_configuration": model_configuration,
    })


def _rerun_public(
    saved: dict[str, Any], operation: dict[str, Any], *, reused: bool,
) -> dict[str, Any]:
    revision = saved["revision"]
    current = store.load_current_participant_dossier(
        operation["project_id"], operation["participant_id"]
    )
    is_current = bool(
        current
        and current["state"].get("current_dossier_version_id")
        == revision.get("dossier_version_id")
        and current["revision"].get("revision_payload_sha256")
        == revision.get("revision_payload_sha256")
    )
    historical = {
        **saved,
        "state": {**saved["state"], "current_version_number": revision["version_number"]},
    }
    return {
        **_public(
            operation["project_id"], str(revision.get("import_id") or ""),
            operation["participant_id"], historical, operation["source"],
        ),
        "is_current_version": is_current,
        "rerun": {
            "from_stage": "participant_dossier",
            "rerun_id": operation["rerun_id"],
            "participant_id": operation["participant_id"],
            "base_dossier_version_id": operation["base_dossier_version_id"],
            "dossier_version_id": operation["dossier_version_id"],
            "status": "completed", "reused": reused,
            "input_fingerprint": operation["request_fingerprint"],
            "other_participants_rewritten": False,
            "reports_rewritten": False,
        },
    }


def _release(
    *, owner_key: str, project_id: str, idempotency_key: str,
    request_fingerprint: str, dossier_version_id: str,
) -> None:
    try:
        store.release_participant_dossier_rerun(
            owner_key=owner_key, project_id=project_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            dossier_version_id=dossier_version_id,
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise _error(
            "DOSSIER_PERSISTENCE_FAILED",
            "玩家档案重跑失败，且幂等占位清理未完成。",
            status=500, retryable=True,
        ) from exc


async def create_participant_dossier_rerun(
    project_id: str, request: dict[str, Any], login: dict[str, Any] | None,
    idempotency_key: str,
) -> dict[str, Any]:
    try:
        request = InterviewV2ParticipantDossierRerunRequest.model_validate(
            request
        ).model_dump(mode="json")
    except ValidationError as exc:
        raise _error(
            "DOSSIER_RERUN_REQUEST_INVALID", "玩家档案重跑请求格式无效。", status=400
        ) from exc
    key = validate_report_rerun_idempotency_key(idempotency_key)
    participant_id = request["participant_id"]
    base = _load_accessible_base(
        project_id, participant_id, request["base_dossier_version_id"], login
    )["revision"]
    owner_key = _owner(login)
    models = _model_configuration()
    try:
        prompt_texts, prompts = _prompt_bundle()
    except (KeyError, TypeError, ValueError) as exc:
        raise _error(
            "DOSSIER_RERUN_INPUT_INVALID", "玩家档案 Prompt 或模型配置无效。", status=422
        ) from exc

    try:
        existing = store.load_dossier_rerun_operation(
            owner_key=owner_key, project_id=project_id, idempotency_key=key
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise _error(
            "DOSSIER_PERSISTENCE_FAILED", "玩家档案重跑状态读取失败。",
            status=500, retryable=True,
        ) from exc
    if existing is not None:
        candidate = _fingerprint(
            request=request, base=base, source=existing["source"],
            participant_input=existing["frozen_participant_input"],
            prompt_snapshot=prompts, model_configuration=models,
        )
        if candidate != existing["request_fingerprint"]:
            raise _error(
                "RERUN_IDEMPOTENCY_CONFLICT",
                "该 Idempotency-Key 已绑定其他重跑输入。",
            )
        if existing["status"] != "completed":
            raise _error(
                "RERUN_IN_PROGRESS",
                "相同玩家档案重跑正在处理中，请稍后使用同一请求重试。",
                retryable=True,
            )
        saved = store.load_participant_dossier(
            project_id, participant_id, existing["dossier_version_id"]
        )
        if saved is None:
            raise _error(
                "DOSSIER_PERSISTENCE_FAILED", "已完成的玩家档案重跑结果不存在。",
                status=500, retryable=True,
            )
        return _rerun_public(saved, existing, reused=True)

    try:
        public, evidence, boundary, coverage, source = _ready_project(project_id, login)
        current = store.load_current_participant_dossier(project_id, participant_id)
        if (
            current is None
            or current["state"].get("current_dossier_version_id")
            != base["dossier_version_id"]
            or current["revision"].get("revision_payload_sha256")
            != base["revision_payload_sha256"]
        ):
            raise _error(
                "DOSSIER_VERSION_CONFLICT", "当前玩家档案版本已变化，请刷新后重试。"
            )
        if base.get("source") != source:
            raise _error(
                "DOSSIER_INPUT_CHANGED",
                "当前玩家档案已过期，请使用按最新证据重新生成。",
            )
        participant_input = build_participant_input(
            participant_id=participant_id, evidence_revision=evidence,
            analysis_boundary=boundary,
        )
        fingerprint = _fingerprint(
            request=request, base=base, source=source,
            participant_input=participant_input, prompt_snapshot=prompts,
            model_configuration=models,
        )
    except InterviewV2ImportError:
        raise
    except (InterviewV2DossierValidationError, KeyError, TypeError, ValueError) as exc:
        raise _error(
            "DOSSIER_RERUN_INPUT_INVALID", "玩家档案重跑输入未通过校验。", status=422
        ) from exc

    operation_args = {
        "owner_key": owner_key, "project_id": project_id,
        "idempotency_key": key, "request_fingerprint": fingerprint,
    }
    try:
        operation = store.claim_participant_dossier_rerun(
            **operation_args,
            base_dossier_version_id=base["dossier_version_id"],
            base_revision_payload_sha256=base["revision_payload_sha256"],
            participant_id=participant_id, source=source,
            frozen_participant_input=participant_input,
            prompt_snapshot=prompts, model_configuration=models,
            created_at=_now(),
        )
    except store.DossierRerunIdempotencyConflictError as exc:
        raise _error(
            "RERUN_IDEMPOTENCY_CONFLICT",
            "该 Idempotency-Key 已绑定其他类型或玩家档案重跑输入。",
        ) from exc
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise _error(
            "DOSSIER_PERSISTENCE_FAILED", "玩家档案重跑状态保存失败。",
            status=500, retryable=True,
        ) from exc
    if operation["status"] == "completed":
        saved = store.load_participant_dossier(
            project_id, participant_id, operation["dossier_version_id"]
        )
        if saved is None:
            raise _error("DOSSIER_PERSISTENCE_FAILED", "玩家档案重跑结果不存在。", status=500)
        return _rerun_public(saved, operation, reused=True)
    if not operation.get("_claim_acquired"):
        raise _error(
            "RERUN_IN_PROGRESS",
            "相同玩家档案重跑正在处理中，请稍后使用同一请求重试。",
            retryable=True,
        )

    committed = False
    try:
        model_input = {
            "participant_id": participant_id,
            "attribute_evidence": participant_input["attribute_evidence"],
            "dossier_evidence": participant_input["dossier_evidence"],
            "evidence_allowlist": participant_input["evidence_allowlist"],
        }
        try:
            attribute_text, attribute_model = await collect_chat_completion(
                [
                    {"role": "system", "content": prompt_texts["interview_v2_attribute_system"]},
                    {"role": "user", "content": "<untrusted_interview_data>\n"
                     + json.dumps(model_input, ensure_ascii=False)
                     + "\n</untrusted_interview_data>"},
                ],
                models=tuple(models["attribute"]["models"]),
                max_tokens=models["attribute"]["max_tokens"],
                reasoning_effort=models["attribute"]["reasoning_effort"],
            )
            attributes = validate_attribute_output(
                _parse_json(attribute_text, "属性抽取"),
                participant_input=participant_input,
            )
            dossier_payload = {**model_input, "validated_attributes": attributes}
            dossier_text, dossier_model = await collect_chat_completion(
                [
                    {"role": "system", "content": prompt_texts["interview_v2_dossier_system"]},
                    {"role": "user", "content": "<untrusted_interview_data>\n"
                     + json.dumps(dossier_payload, ensure_ascii=False)
                     + "\n</untrusted_interview_data>"},
                ],
                models=tuple(models["dossier"]["models"]),
                max_tokens=models["dossier"]["max_tokens"],
                reasoning_effort=models["dossier"]["reasoning_effort"],
            )
            dossier = validate_dossier_output(
                _parse_json(dossier_text, "玩家档案"),
                participant_input=participant_input,
            )
        except asyncio.CancelledError:
            raise
        except InterviewV2ImportError as exc:
            raise _error(
                "DOSSIER_RERUN_MODEL_OUTPUT_INVALID",
                "玩家档案模型没有返回有效 JSON，未创建新版本。",
                status=502, retryable=True,
            ) from exc
        except InterviewV2DossierValidationError as exc:
            raise _error(
                "DOSSIER_RERUN_MODEL_OUTPUT_INVALID",
                "玩家档案模型输出未通过结构或证据校验，未创建新版本。",
                status=502, retryable=True,
            ) from exc
        except Exception as exc:
            raise _error(
                "DOSSIER_RERUN_FAILED",
                "玩家档案模型调用失败，未创建新版本。",
                status=502, retryable=True,
            ) from exc

        created_at = _now()
        revision = {
            "dossier_version_id": operation["dossier_version_id"],
            "import_id": public["import_id"],
            "source": source,
            "input_fingerprint": payload_sha256(dossier_payload),
            "attributes": attributes, "dossier": dossier,
            "status": "generated", "review": {},
            "model_usage": {
                "attribute_model": attribute_model,
                "dossier_model": dossier_model,
                "prompts": prompts,
                "model_configuration": models,
            },
            "created_at": created_at,
            "created_by": str(_owner_from_login(login).get("owner_key") or ""),
            "rerun": {
                "rerun_id": operation["rerun_id"],
                "from_stage": "participant_dossier",
                "participant_id": participant_id,
                "base_dossier_version_id": base["dossier_version_id"],
                "base_revision_payload_sha256": base["revision_payload_sha256"],
                "input_fingerprint": fingerprint,
                "frozen_participant_input": participant_input,
                "source": source,
                "prompt_snapshot": prompts,
                "model_configuration": models,
            },
        }
        try:
            saved = store.save_participant_dossier_rerun_cas(
                **operation_args, revision=revision
            )
        except store.DossierRerunIdempotencyConflictError as exc:
            raise _error(
                "RERUN_IDEMPOTENCY_CONFLICT",
                "该 Idempotency-Key 已绑定其他类型或玩家档案重跑输入。",
            ) from exc
        except ValueError as exc:
            if str(exc) in {
                "participant dossier version conflict",
                "participant dossier digest changed",
            }:
                raise _error(
                    "DOSSIER_VERSION_CONFLICT",
                    "玩家档案版本在重跑期间发生变化，请刷新后重试。",
                ) from exc
            if str(exc) == "dossier input changed":
                raise _error(
                    "DOSSIER_INPUT_CHANGED",
                    "玩家档案上游输入在重跑期间发生变化，请刷新后重试。",
                ) from exc
            raise
        committed = True
        return _rerun_public(saved, saved["operation"], reused=False)
    except asyncio.CancelledError:
        raise
    except InterviewV2ImportError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise _error(
            "DOSSIER_PERSISTENCE_FAILED",
            "玩家档案重跑未完成版本提交，请刷新后重试。",
            status=500, retryable=True,
        ) from exc
    finally:
        if not committed:
            _release(
                **operation_args,
                dossier_version_id=operation["dossier_version_id"],
            )


__all__ = ["create_participant_dossier_rerun", "validate_dossier_rerun_access"]
