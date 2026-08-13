"""访谈报告 V2 批次 1：隔离上传与确定性预检编排。"""

from __future__ import annotations

import hashlib
import re
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.core import config
from app.core.interview_v2_workbook import (
    InterviewV2WorkbookError,
    parse_interview_v2_workbook,
)
from app.core.security import _owner_from_login, _visible_to_owner
from app.storage import interview_v2_store as store


_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PUBLIC_WARNING_KEYS = {
    "code",
    "message",
    "level",
    "retryable",
    "suggested_action",
    "context",
}
_SENSITIVE_CONTEXT_KEY_PARTS = (
    "raw",
    "value",
    "text",
    "content",
    "cell",
    "quote",
    "formula",
    "filename",
    "path",
)
_PRECHECK_LEASE_SECONDS = 300.0
_ORPHAN_CLEANUP_SECONDS = 24 * 60 * 60
_STAGING_CLEANUP_SECONDS = 24 * 60 * 60


class InterviewV2ImportError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        suggested_action: str | None = None,
        context: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code)
        self.message = str(message)
        self.retryable = bool(retryable)
        self.suggested_action = str(suggested_action or "")
        self.context = _safe_context(context)
        self.trace_id = str(trace_id or f"trace_{uuid4().hex}")

    def to_error_body(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "suggested_action": self.suggested_action,
            "context": deepcopy(self.context),
            "trace_id": self.trace_id,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _safe_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).strip()
        lowered = normalized.lower()
        if not normalized or any(part in lowered for part in _SENSITIVE_CONTEXT_KEY_PARTS):
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[normalized] = item
        elif isinstance(item, list) and all(
            isinstance(child, (str, int, float, bool)) or child is None
            for child in item
        ):
            result[normalized] = item[:100]
    return result


def _owner_record(login: dict[str, Any] | None) -> dict[str, str]:
    return _owner_from_login(login)


def _resource_error(code: str, message: str) -> InterviewV2ImportError:
    return InterviewV2ImportError(
        status_code=404,
        code=code,
        message=message,
        suggested_action="restart_upload",
    )


def _load_attempt(upload_attempt_id: str) -> dict[str, Any]:
    try:
        attempt = store.load_upload_attempt(upload_attempt_id)
    except ValueError as exc:
        raise InterviewV2ImportError(
            status_code=400,
            code="RESOURCE_ID_INVALID",
            message="上传记录 ID 格式无效。",
            suggested_action="check_resource_id",
        ) from exc
    if attempt is None:
        raise _resource_error("UPLOAD_ATTEMPT_NOT_FOUND", "上传记录不存在。")
    return attempt


def _owned(item: dict[str, Any], login: dict[str, Any] | None) -> bool:
    return _visible_to_owner(item, login)


def _public_warning(
    value: Any, *, default_level: str = "warning"
) -> dict[str, Any] | None:
    if isinstance(value, str):
        code = value.strip()
        if not code:
            return None
        return {
            "code": code,
            "message": "工作簿存在需要确认的物理结构，请在下一步检查。",
            "level": default_level,
            "suggested_action": "review_workbook_structure",
            "context": {},
        }
    if not isinstance(value, dict):
        return None
    result = {key: value.get(key) for key in _PUBLIC_WARNING_KEYS if key in value}
    code = str(result.get("code") or "WORKBOOK_STRUCTURE_WARNING").strip()
    message = str(
        result.get("message")
        or "工作簿存在需要确认的物理结构，请在下一步检查。"
    ).strip()
    return {
        "code": code,
        "message": message,
        "level": str(result.get("level") or default_level),
        "retryable": bool(result.get("retryable", False)),
        "suggested_action": str(result.get("suggested_action") or ""),
        "context": _safe_context(result.get("context")),
    }


def _warnings(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    public: list[dict[str, Any]] = []
    for item in list(snapshot.get("warnings") or []):
        warning = _public_warning(item, default_level="warning")
        if warning is not None:
            public.append(warning)
    for item in list(snapshot.get("confirmation_required") or []):
        warning = _public_warning(item, default_level="confirmation_required")
        if warning is not None:
            public.append(warning)
    return public


def _precheck_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
    return {
        "file_size_bytes": int(snapshot.get("file_size") or 0),
        "sheet_count": int(summary.get("sheet_count") or len(snapshot.get("sheets") or [])),
        "non_empty_cell_count": int(summary.get("non_empty_cell_count") or 0),
        "text_char_count": int(summary.get("total_text_chars") or 0),
        "formula_count": int(summary.get("formula_count") or 0),
        "warnings": _warnings(snapshot),
    }


def _candidate_region(sheet: dict[str, Any]) -> Any:
    candidate = sheet.get("candidate_participant_region")
    if isinstance(candidate, dict):
        allowed = {
            "status",
            "range",
            "start_column",
            "end_column",
            "candidate_columns",
            "candidate_count",
            "header_row",
            "basis",
            "confidence",
        }
        return {key: candidate[key] for key in allowed if key in candidate}
    if isinstance(candidate, str):
        return candidate
    return None


def _import_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    public = _precheck_summary(snapshot)
    sheets: list[dict[str, Any]] = []
    for sheet in snapshot.get("sheets") or []:
        if not isinstance(sheet, dict):
            continue
        sheets.append(
            {
                "sheet_id": str(sheet.get("sheet_id") or ""),
                "index": sheet.get("index"),
                "name": str(sheet.get("name") or ""),
                "state": str(sheet.get("state") or "visible"),
                "declared_range": sheet.get("declared_range"),
                "content_range": sheet.get("content_range"),
                "dimensions": deepcopy(sheet.get("dimensions") or {}),
                "hidden_row_count": len(sheet.get("hidden_rows") or []),
                "hidden_column_count": len(sheet.get("hidden_columns") or []),
                "merged_range_count": len(sheet.get("merged_ranges") or []),
                "candidate_participant_region": _candidate_region(sheet),
            }
        )
    public["sheets"] = sheets
    return public


def _public_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "upload_attempt_id",
        "job_id",
        "status",
        "filename",
        "file_size",
        "content_sha256",
        "file_contract_version",
        "created_at",
        "updated_at",
        "project_id",
        "import_id",
        "workbook_revision_id",
        "precheck_summary",
        "error",
    )
    result = {key: deepcopy(attempt.get(key)) for key in allowed}
    if result.get("status") != "ACCEPTED":
        result["project_id"] = None
        result["import_id"] = None
        result["workbook_revision_id"] = None
    if result.get("error") is not None:
        error = result["error"] if isinstance(result["error"], dict) else {}
        result["error"] = {
            "code": str(error.get("code") or "WORKBOOK_INVALID"),
            "message": str(error.get("message") or "工作簿预检失败。"),
            "retryable": bool(error.get("retryable", False)),
            "suggested_action": str(error.get("suggested_action") or ""),
            "context": _safe_context(error.get("context")),
            "trace_id": str(error.get("trace_id") or f"trace_{uuid4().hex}"),
        }
    return result


def _published_ids(attempt: dict[str, Any]) -> tuple[str, str, str] | None:
    project_id = str(attempt.get("project_id") or "")
    workbook_revision_id = str(attempt.get("workbook_revision_id") or "")
    import_id = str(attempt.get("import_id") or "")
    if not project_id or not workbook_revision_id or not import_id:
        return None
    try:
        store.validate_resource_id(project_id, "project")
        store.validate_resource_id(workbook_revision_id, "workbook")
        store.validate_resource_id(import_id, "import")
    except ValueError:
        return None
    return project_id, workbook_revision_id, import_id


def _recover_published_attempt(
    attempt: dict[str, Any],
) -> dict[str, Any] | None:
    ids = _published_ids(attempt)
    if attempt.get("status") != "PRECHECKING" or ids is None:
        return None
    project_id, workbook_revision_id, import_id = ids
    if not store.accepted_bundle_exists(
        project_id, workbook_revision_id, import_id
    ):
        return None
    snapshot = store.load_physical_snapshot(project_id, workbook_revision_id)
    if snapshot is None:
        return None
    try:
        finalized = store.finalize_upload_attempt_accepted(
            attempt["upload_attempt_id"],
            claim_token=str(attempt.get("precheck_claim_token") or ""),
            project_id=project_id,
            workbook_revision_id=workbook_revision_id,
            import_id=import_id,
            precheck_summary=_precheck_summary(snapshot),
            updated_at=_now(),
        )
    except Exception:
        return _public_attempt(_load_attempt(attempt["upload_attempt_id"]))
    _best_effort_delete_quarantined_source(attempt["upload_attempt_id"])
    return _public_attempt(finalized)


def _best_effort_delete_quarantined_source(upload_attempt_id: str) -> None:
    """Delete a terminal attempt's quarantine copy without changing its state."""

    try:
        store.delete_quarantined_source(upload_attempt_id)
    except Exception:
        # Terminal metadata is the source of truth. Cleanup is retried by both
        # status reads and precheck re-entry; it must never downgrade or leak.
        pass


def _reject_claimed_attempt(
    attempt: dict[str, Any], error: InterviewV2ImportError
) -> dict[str, Any]:
    metadata, rejected = store.finalize_upload_attempt_rejected(
        attempt["upload_attempt_id"],
        claim_token=str(attempt.get("precheck_claim_token") or ""),
        error=error.to_error_body(),
        updated_at=_now(),
    )
    if rejected:
        _best_effort_delete_quarantined_source(attempt["upload_attempt_id"])
    recovered = _recover_published_attempt(metadata)
    return recovered if recovered is not None else _public_attempt(metadata)


def create_upload_attempt(
    *,
    filename: str,
    content: bytes,
    login: dict[str, Any] | None,
    research_focus: str,
    file_contract_version: str,
    contract_acknowledged: bool,
    idempotency_key: str,
) -> tuple[dict[str, Any], bool]:
    if not contract_acknowledged:
        raise InterviewV2ImportError(
            status_code=422,
            code="FILE_CONTRACT_NOT_ACKNOWLEDGED",
            message="请先确认访谈文件要求。",
            suggested_action="acknowledge_file_contract",
        )
    if str(file_contract_version or "").strip() != config.INTERVIEW_V2_FILE_CONTRACT_VERSION:
        raise InterviewV2ImportError(
            status_code=409,
            code="FILE_CONTRACT_VERSION_MISMATCH",
            message="文件要求已更新，请刷新页面后重新确认。",
            suggested_action="refresh_file_contract",
            context={"expected_version": config.INTERVIEW_V2_FILE_CONTRACT_VERSION},
        )
    key = str(idempotency_key or "").strip()
    if not _IDEMPOTENCY_RE.fullmatch(key):
        raise InterviewV2ImportError(
            status_code=400,
            code="IDEMPOTENCY_KEY_INVALID",
            message="幂等键格式无效。",
            suggested_action="use_new_idempotency_key",
        )
    if not content:
        raise InterviewV2ImportError(
            status_code=400,
            code="UPLOAD_EMPTY",
            message="上传文件为空。",
            suggested_action="select_workbook",
        )
    if len(content) > config.INTERVIEW_V2_MAX_FILE_BYTES:
        raise InterviewV2ImportError(
            status_code=413,
            code="WORKBOOK_LIMIT_EXCEEDED",
            message="上传文件超过系统上限，请拆分或精简后重试。",
            suggested_action="reduce_workbook_size",
            context={"limit_bytes": config.INTERVIEW_V2_MAX_FILE_BYTES},
        )

    normalized_focus = str(research_focus or "").strip()
    if len(normalized_focus) > 4000:
        raise InterviewV2ImportError(
            status_code=422,
            code="RESEARCH_FOCUS_TOO_LONG",
            message="研究重点不能超过 4000 个字符。",
            suggested_action="shorten_research_focus",
            context={"limit_chars": 4000},
        )

    store.cleanup_orphaned_upload_attempts(
        older_than_epoch=time.time() - _ORPHAN_CLEANUP_SECONDS
    )

    owner = _owner_record(login)
    owner_key = owner.get("owner_key", "")
    digest = hashlib.sha256(content).hexdigest()
    fingerprint = hashlib.sha256(
        f"{digest}\0{file_contract_version}\0{normalized_focus}".encode(
            "utf-8"
        )
    ).hexdigest()
    now = _now()
    upload_attempt_id = _new_id("upload")
    attempt: dict[str, Any] = {
        "upload_attempt_id": upload_attempt_id,
        "job_id": _new_id("job"),
        "status": "QUARANTINED",
        "filename": str(filename or "interview.xlsx"),
        "file_size": len(content),
        "content_sha256": digest,
        "file_contract_version": str(file_contract_version),
        "contract_acknowledged": True,
        "research_focus": normalized_focus,
        "idempotency_key_sha256": hashlib.sha256(key.encode("utf-8")).hexdigest(),
        "request_fingerprint": fingerprint,
        "created_at": now,
        "updated_at": now,
        "project_id": None,
        "import_id": None,
        "workbook_revision_id": None,
        "precheck_summary": None,
        "error": None,
        **owner,
    }
    try:
        existing = store.claim_upload_attempt(
            owner_key=owner_key,
            idempotency_key=key,
            metadata=attempt,
            content=content,
            idempotency_record={
                "upload_attempt_id": upload_attempt_id,
                "job_id": attempt["job_id"],
                "request_fingerprint": fingerprint,
                "created_at": now,
            },
        )
    except Exception:
        raise InterviewV2ImportError(
            status_code=500,
            code="UPLOAD_PERSISTENCE_FAILED",
            message="上传记录保存失败，请重试。",
            retryable=True,
            suggested_action="retry_upload",
        )

    if existing is not None:
        if existing.get("request_fingerprint") != fingerprint:
            raise InterviewV2ImportError(
                status_code=409,
                code="IDEMPOTENCY_KEY_CONFLICT",
                message="同一幂等键已用于其他上传内容。",
                suggested_action="use_new_idempotency_key",
                context={"upload_attempt_id": existing.get("upload_attempt_id")},
            )
        existing_id = str(existing.get("upload_attempt_id") or "")
        try:
            existing_attempt = store.load_upload_attempt(existing_id)
        except ValueError as exc:
            raise InterviewV2ImportError(
                status_code=500,
                code="UPLOAD_PERSISTENCE_FAILED",
                message="上传记录保存失败，请重试。",
                retryable=True,
                suggested_action="retry_upload",
            ) from exc
        if existing_attempt is None:
            try:
                existing_attempt = store.recover_claimed_upload_attempt(
                    owner_key=owner_key,
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                    metadata=attempt,
                    content=content,
                )
            except Exception as exc:
                raise InterviewV2ImportError(
                    status_code=500,
                    code="UPLOAD_PERSISTENCE_FAILED",
                    message="上传记录保存失败，请重试。",
                    retryable=True,
                    suggested_action="retry_upload",
                ) from exc
        if not _owned(existing_attempt, login):
            raise _resource_error("UPLOAD_ATTEMPT_NOT_FOUND", "上传记录不存在。")
        status = str(existing_attempt.get("status") or "")
        if status in {"ACCEPTED", "REJECTED"}:
            _best_effort_delete_quarantined_source(existing_id)
        lease_expiry = float(
            existing_attempt.get("precheck_lease_expires_at") or 0
        )
        should_schedule = status == "QUARANTINED" or (
            status == "PRECHECKING" and lease_expiry <= time.time()
        )
        return _public_attempt(existing_attempt), should_schedule

    return _public_attempt(attempt), True


def run_upload_precheck(upload_attempt_id: str) -> dict[str, Any]:
    store.cleanup_orphaned_upload_attempts(
        older_than_epoch=time.time() - _ORPHAN_CLEANUP_SECONDS
    )
    attempt = _load_attempt(upload_attempt_id)
    if attempt.get("status") in {"ACCEPTED", "REJECTED"}:
        _best_effort_delete_quarantined_source(upload_attempt_id)
        return _public_attempt(attempt)
    recovered = _recover_published_attempt(attempt)
    if recovered is not None:
        return recovered
    if attempt.get("status") not in {"QUARANTINED", "PRECHECKING"}:
        raise InterviewV2ImportError(
            status_code=409,
            code="UPLOAD_STATE_CONFLICT",
            message="上传记录当前状态不能执行预检。",
            suggested_action="refresh_upload_status",
            context={"status": attempt.get("status")},
        )

    now_epoch = time.time()
    attempt, claimed = store.claim_upload_precheck(
        upload_attempt_id,
        claim_token=_new_id("claim"),
        lease_expires_at=now_epoch + _PRECHECK_LEASE_SECONDS,
        project_id=_new_id("project"),
        import_id=_new_id("import"),
        workbook_revision_id=_new_id("workbook"),
        updated_at=_now(),
    )
    if not claimed:
        recovered = _recover_published_attempt(attempt)
        return recovered if recovered is not None else _public_attempt(attempt)

    published = False
    try:
        content = store.read_quarantined_source(upload_attempt_id)
        snapshot = parse_interview_v2_workbook(attempt["filename"], content)
        if hashlib.sha256(content).hexdigest() != attempt.get("content_sha256"):
            raise InterviewV2ImportError(
                status_code=409,
                code="UPLOAD_CONTENT_CHANGED",
                message="隔离文件内容校验失败，请重新上传。",
                suggested_action="restart_upload",
            )

        now = _now()
        project_id = attempt["project_id"]
        import_id = attempt["import_id"]
        workbook_revision_id = attempt["workbook_revision_id"]
        project = {
            "project_id": project_id,
            "status": "GROUP_CONFIRMATION_REQUIRED",
            "research_focus": attempt.get("research_focus", ""),
            "current_workbook_revision_id": workbook_revision_id,
            "current_import_id": import_id,
            "created_at": now,
            "updated_at": now,
            "source_upload_attempt_id": upload_attempt_id,
            **{key: attempt.get(key, "") for key in ("owner_key", "owner_email", "owner_open_id", "owner_name")},
        }
        workbook_revision = {
            "workbook_revision_id": workbook_revision_id,
            "project_id": project_id,
            "content_sha256": attempt.get("content_sha256"),
            "original_filename": attempt.get("filename"),
            "file_size": attempt.get("file_size"),
            "physical_snapshot_version": snapshot.get("schema_version"),
            "snapshot_sha256": snapshot.get("snapshot_sha256"),
            "created_at": now,
            "source_upload_attempt_id": upload_attempt_id,
            **{key: attempt.get(key, "") for key in ("owner_key", "owner_email", "owner_open_id", "owner_name")},
        }
        import_summary = _import_summary(snapshot)
        interview_import = {
            "import_id": import_id,
            "project_id": project_id,
            "workbook_revision_id": workbook_revision_id,
            "status": "GROUP_CONFIRMATION_REQUIRED",
            "created_at": now,
            "updated_at": now,
            "physical_snapshot_version": snapshot.get("schema_version"),
            "summary": import_summary,
            "warnings": _warnings(snapshot),
            "source_upload_attempt_id": upload_attempt_id,
            **{key: attempt.get(key, "") for key in ("owner_key", "owner_email", "owner_open_id", "owner_name")},
        }
        renewed = store.renew_upload_precheck_claim(
            upload_attempt_id,
            claim_token=str(attempt.get("precheck_claim_token") or ""),
            lease_expires_at=time.time() + _PRECHECK_LEASE_SECONDS,
            updated_at=_now(),
        )
        if renewed is None:
            current = _load_attempt(upload_attempt_id)
            recovered = _recover_published_attempt(current)
            return recovered if recovered is not None else _public_attempt(current)
        attempt = renewed
        store.cleanup_stale_staging(
            older_than_epoch=time.time() - _STAGING_CLEANUP_SECONDS
        )
        store.publish_accepted_bundle(
            project=project,
            workbook_revision=workbook_revision,
            interview_import=interview_import,
            source_content=content,
            physical_snapshot=snapshot,
        )
        published = True
        try:
            finalized = store.finalize_upload_attempt_accepted(
                upload_attempt_id,
                claim_token=str(attempt.get("precheck_claim_token") or ""),
                project_id=project_id,
                workbook_revision_id=workbook_revision_id,
                import_id=import_id,
                precheck_summary=_precheck_summary(snapshot),
                updated_at=now,
            )
        except Exception:
            return _public_attempt(_load_attempt(upload_attempt_id))
        _best_effort_delete_quarantined_source(upload_attempt_id)
        return _public_attempt(finalized)
    except InterviewV2WorkbookError as exc:
        error = InterviewV2ImportError(
            status_code=422,
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
            suggested_action=exc.suggested_action,
            context=exc.context,
        )
        return _reject_claimed_attempt(attempt, error)
    except Exception as exc:
        recovered = _recover_published_attempt(_load_attempt(upload_attempt_id))
        if published or recovered is not None:
            return recovered or _public_attempt(_load_attempt(upload_attempt_id))
        if isinstance(exc, InterviewV2ImportError):
            error = exc
        else:
            error = InterviewV2ImportError(
                status_code=500,
                code="UPLOAD_PRECHECK_FAILED",
                message="工作簿预检未完成，请重新上传。",
                retryable=True,
                suggested_action="retry_upload",
            )
        return _reject_claimed_attempt(attempt, error)


def get_upload_attempt(
    upload_attempt_id: str, login: dict[str, Any] | None
) -> dict[str, Any]:
    attempt = _load_attempt(upload_attempt_id)
    if not _owned(attempt, login):
        raise _resource_error("UPLOAD_ATTEMPT_NOT_FOUND", "上传记录不存在。")
    if attempt.get("status") in {"ACCEPTED", "REJECTED"}:
        _best_effort_delete_quarantined_source(upload_attempt_id)
    return _public_attempt(attempt)


def upload_attempt_needs_precheck(
    upload_attempt_id: str, login: dict[str, Any] | None
) -> bool:
    """Return whether an owned attempt needs a background precheck scheduled."""

    attempt = _load_attempt(upload_attempt_id)
    if not _owned(attempt, login):
        raise _resource_error("UPLOAD_ATTEMPT_NOT_FOUND", "上传记录不存在。")
    status = str(attempt.get("status") or "")
    if status == "QUARANTINED":
        return True
    if status != "PRECHECKING":
        return False
    return float(attempt.get("precheck_lease_expires_at") or 0) <= time.time()


def get_interview_import(
    import_id: str, login: dict[str, Any] | None
) -> dict[str, Any]:
    try:
        item = store.load_interview_import(import_id)
    except ValueError as exc:
        raise InterviewV2ImportError(
            status_code=400,
            code="RESOURCE_ID_INVALID",
            message="导入记录 ID 格式无效。",
            suggested_action="check_resource_id",
        ) from exc
    if item is None or not _owned(item, login):
        raise _resource_error("INTERVIEW_IMPORT_NOT_FOUND", "导入记录不存在。")
    allowed = (
        "import_id",
        "project_id",
        "workbook_revision_id",
        "status",
        "created_at",
        "updated_at",
        "physical_snapshot_version",
        "summary",
        "warnings",
    )
    return {key: deepcopy(item.get(key)) for key in allowed}
