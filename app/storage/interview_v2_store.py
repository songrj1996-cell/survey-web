"""访谈报告 V2 的本地文件存储。

本模块只负责隔离上传、原子文件写入和正式导入快照的持久化。业务状态机、
鉴权判断和错误文案由 service 层负责。正式项目的三个首批实体放在同一个项目
目录中，并通过一次目录重命名发布，避免只创建其中一部分。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.core import config


_STORE_LOCK = threading.RLock()
_ID_RE = re.compile(
    r"^(?:upload|job|project|import|workbook)_[0-9a-f]{32}$"
)


def _root() -> Path:
    return Path(config.INTERVIEW_V2_DATA_DIR).expanduser().resolve()


def validate_resource_id(value: str, expected_prefix: str | None = None) -> str:
    resource_id = str(value or "").strip()
    if not _ID_RE.fullmatch(resource_id):
        raise ValueError("invalid interview V2 resource id")
    if expected_prefix and not resource_id.startswith(f"{expected_prefix}_"):
        raise ValueError("unexpected interview V2 resource id type")
    return resource_id


def _safe_child(*parts: str) -> Path:
    root = _root()
    candidate = root.joinpath(*parts).resolve()
    root_text = os.path.normcase(os.path.abspath(os.fspath(root)))
    candidate_text = os.path.normcase(os.path.abspath(os.fspath(candidate)))
    for prefix in ("\\\\?\\", "\\??\\"):
        if root_text.startswith(prefix):
            root_text = root_text[len(prefix) :]
        if candidate_text.startswith(prefix):
            candidate_text = candidate_text[len(prefix) :]
    try:
        common = os.path.commonpath((root_text, candidate_text))
    except ValueError as exc:
        raise ValueError("interview V2 path escaped its storage root") from exc
    if common != root_text:
        raise ValueError("interview V2 path escaped its storage root")
    return candidate


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    _atomic_write_bytes(path, encoded)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"invalid JSON object at {path.name}")
    return value


def _attempt_dir(upload_attempt_id: str) -> Path:
    upload_attempt_id = validate_resource_id(upload_attempt_id, "upload")
    return _safe_child("upload_attempts", upload_attempt_id)


def create_upload_attempt(metadata: dict[str, Any], content: bytes) -> None:
    upload_attempt_id = validate_resource_id(
        str(metadata.get("upload_attempt_id") or ""), "upload"
    )
    attempt_dir = _attempt_dir(upload_attempt_id)
    with _STORE_LOCK:
        if attempt_dir.exists():
            raise FileExistsError(upload_attempt_id)
        attempt_dir.mkdir(parents=True, exist_ok=False)
        try:
            _atomic_write_bytes(attempt_dir / "source.xlsx", content)
            _atomic_write_json(attempt_dir / "metadata.json", metadata)
        except Exception:
            shutil.rmtree(attempt_dir, ignore_errors=True)
            raise


def claim_upload_attempt(
    *,
    owner_key: str,
    idempotency_key: str,
    metadata: dict[str, Any],
    content: bytes,
    idempotency_record: dict[str, Any],
) -> dict[str, Any] | None:
    """Atomically claim one owner/idempotency-key pair within this process.

    Returns the existing idempotency record when another request already owns
    the pair. A new attempt and its idempotency record are otherwise committed
    under the same re-entrant store lock; failures remove the unpublished
    attempt directory.
    """

    upload_attempt_id = validate_resource_id(
        str(metadata.get("upload_attempt_id") or ""), "upload"
    )
    attempt_dir = _attempt_dir(upload_attempt_id)
    idempotency_path = _idempotency_path(owner_key, idempotency_key)
    with _STORE_LOCK:
        existing = _read_json(idempotency_path)
        if existing is not None:
            return existing
        if attempt_dir.exists():
            raise FileExistsError(upload_attempt_id)
        _atomic_write_json(idempotency_path, idempotency_record)
        try:
            attempt_dir.mkdir(parents=True, exist_ok=False)
            _atomic_write_bytes(attempt_dir / "source.xlsx", content)
            _atomic_write_json(attempt_dir / "metadata.json", metadata)
        except Exception:
            shutil.rmtree(attempt_dir, ignore_errors=True)
            current = _read_json(idempotency_path)
            if current == idempotency_record:
                idempotency_path.unlink(missing_ok=True)
            raise
    return None


def recover_claimed_upload_attempt(
    *,
    owner_key: str,
    idempotency_key: str,
    request_fingerprint: str,
    metadata: dict[str, Any],
    content: bytes,
) -> dict[str, Any]:
    """Recover a claim left between durable key claim and attempt publication."""

    idempotency_path = _idempotency_path(owner_key, idempotency_key)
    with _STORE_LOCK:
        record = _read_json(idempotency_path)
        if (
            record is None
            or record.get("request_fingerprint") != request_fingerprint
        ):
            raise ValueError("idempotency claim no longer matches request")
        upload_attempt_id = validate_resource_id(
            str(record.get("upload_attempt_id") or ""), "upload"
        )
        attempt_dir = _attempt_dir(upload_attempt_id)
        existing = _read_json(attempt_dir / "metadata.json")
        if existing is not None:
            return existing
        recovered = dict(metadata)
        recovered["upload_attempt_id"] = upload_attempt_id
        recovered["job_id"] = str(record.get("job_id") or recovered.get("job_id") or "")
        recovered["created_at"] = str(
            record.get("created_at") or recovered.get("created_at") or ""
        )
        shutil.rmtree(attempt_dir, ignore_errors=True)
        attempt_dir.mkdir(parents=True, exist_ok=False)
        try:
            _atomic_write_bytes(attempt_dir / "source.xlsx", content)
            _atomic_write_json(attempt_dir / "metadata.json", recovered)
        except Exception:
            shutil.rmtree(attempt_dir, ignore_errors=True)
            raise
        return recovered


def load_upload_attempt(upload_attempt_id: str) -> dict[str, Any] | None:
    with _STORE_LOCK:
        return _read_json(_attempt_dir(upload_attempt_id) / "metadata.json")


def save_upload_attempt(metadata: dict[str, Any]) -> None:
    upload_attempt_id = validate_resource_id(
        str(metadata.get("upload_attempt_id") or ""), "upload"
    )
    with _STORE_LOCK:
        attempt_dir = _attempt_dir(upload_attempt_id)
        if not attempt_dir.is_dir():
            raise FileNotFoundError(upload_attempt_id)
        _atomic_write_json(attempt_dir / "metadata.json", metadata)


def claim_upload_precheck(
    upload_attempt_id: str,
    *,
    claim_token: str,
    lease_expires_at: float,
    project_id: str,
    import_id: str,
    workbook_revision_id: str,
    updated_at: str,
) -> tuple[dict[str, Any], bool]:
    """CAS ``QUARANTINED`` (or a stale lease) into ``PRECHECKING``."""

    validate_resource_id(upload_attempt_id, "upload")
    validate_resource_id(project_id, "project")
    validate_resource_id(import_id, "import")
    validate_resource_id(workbook_revision_id, "workbook")
    if not re.fullmatch(r"claim_[0-9a-f]{32}", str(claim_token or "")):
        raise ValueError("invalid precheck claim token")
    now_epoch = time.time()
    with _STORE_LOCK:
        path = _attempt_dir(upload_attempt_id) / "metadata.json"
        metadata = _read_json(path)
        if metadata is None:
            raise FileNotFoundError(upload_attempt_id)
        status = str(metadata.get("status") or "")
        if status in {"ACCEPTED", "REJECTED"}:
            return metadata, False
        current_expiry = float(metadata.get("precheck_lease_expires_at") or 0)
        can_claim = status == "QUARANTINED" or (
            status == "PRECHECKING" and current_expiry <= now_epoch
        )
        if not can_claim:
            return metadata, False
        metadata.update(
            {
                "status": "PRECHECKING",
                "updated_at": updated_at,
                "precheck_claim_token": claim_token,
                "precheck_lease_expires_at": float(lease_expires_at),
                "project_id": metadata.get("project_id") or project_id,
                "import_id": metadata.get("import_id") or import_id,
                "workbook_revision_id": (
                    metadata.get("workbook_revision_id") or workbook_revision_id
                ),
            }
        )
        validate_resource_id(str(metadata["project_id"]), "project")
        validate_resource_id(str(metadata["import_id"]), "import")
        validate_resource_id(
            str(metadata["workbook_revision_id"]), "workbook"
        )
        _atomic_write_json(path, metadata)
        return metadata, True


def renew_upload_precheck_claim(
    upload_attempt_id: str,
    *,
    claim_token: str,
    lease_expires_at: float,
    updated_at: str,
) -> dict[str, Any] | None:
    """Extend only the currently owning precheck claim."""

    validate_resource_id(upload_attempt_id, "upload")
    with _STORE_LOCK:
        path = _attempt_dir(upload_attempt_id) / "metadata.json"
        metadata = _read_json(path)
        if metadata is None:
            raise FileNotFoundError(upload_attempt_id)
        if (
            metadata.get("status") != "PRECHECKING"
            or metadata.get("precheck_claim_token") != claim_token
        ):
            return None
        metadata["precheck_lease_expires_at"] = float(lease_expires_at)
        metadata["updated_at"] = updated_at
        _atomic_write_json(path, metadata)
        return metadata


def finalize_upload_attempt_rejected(
    upload_attempt_id: str,
    *,
    claim_token: str,
    error: dict[str, Any],
    updated_at: str,
) -> tuple[dict[str, Any], bool]:
    """Reject only when the caller still owns the active precheck claim."""

    validate_resource_id(upload_attempt_id, "upload")
    with _STORE_LOCK:
        path = _attempt_dir(upload_attempt_id) / "metadata.json"
        metadata = _read_json(path)
        if metadata is None:
            raise FileNotFoundError(upload_attempt_id)
        if metadata.get("status") in {"ACCEPTED", "REJECTED"}:
            return metadata, False
        if (
            metadata.get("status") != "PRECHECKING"
            or metadata.get("precheck_claim_token") != claim_token
        ):
            return metadata, False
        metadata.update(
            {
                "status": "REJECTED",
                "updated_at": updated_at,
                "project_id": None,
                "import_id": None,
                "workbook_revision_id": None,
                "precheck_summary": None,
                "error": error,
                "precheck_claim_token": None,
                "precheck_lease_expires_at": None,
            }
        )
        metadata.pop("filename", None)
        metadata.pop("research_focus", None)
        _atomic_write_json(path, metadata)
        return metadata, True


def read_quarantined_source(upload_attempt_id: str) -> bytes:
    source = _attempt_dir(upload_attempt_id) / "source.xlsx"
    with _STORE_LOCK:
        return source.read_bytes()


def delete_quarantined_source(upload_attempt_id: str) -> None:
    source = _attempt_dir(upload_attempt_id) / "source.xlsx"
    with _STORE_LOCK:
        source.unlink(missing_ok=True)


def quarantined_source_exists(upload_attempt_id: str) -> bool:
    with _STORE_LOCK:
        return (_attempt_dir(upload_attempt_id) / "source.xlsx").is_file()


def _idempotency_path(owner_key: str, idempotency_key: str) -> Path:
    digest = hashlib.sha256(
        f"{owner_key}\0{idempotency_key}".encode("utf-8")
    ).hexdigest()
    return _safe_child("idempotency", f"{digest}.json")


def load_idempotency(
    owner_key: str, idempotency_key: str
) -> dict[str, Any] | None:
    with _STORE_LOCK:
        return _read_json(_idempotency_path(owner_key, idempotency_key))


def save_idempotency(
    owner_key: str,
    idempotency_key: str,
    record: dict[str, Any],
) -> None:
    with _STORE_LOCK:
        _atomic_write_json(
            _idempotency_path(owner_key, idempotency_key),
            record,
        )


def publish_accepted_bundle(
    *,
    project: dict[str, Any],
    workbook_revision: dict[str, Any],
    interview_import: dict[str, Any],
    source_content: bytes,
    physical_snapshot: dict[str, Any],
) -> None:
    """原子发布一个新项目及其首个工作簿版本和导入记录。"""

    project_id = validate_resource_id(str(project.get("project_id") or ""), "project")
    workbook_id = validate_resource_id(
        str(workbook_revision.get("workbook_revision_id") or ""), "workbook"
    )
    import_id = validate_resource_id(
        str(interview_import.get("import_id") or ""), "import"
    )
    projects_dir = _safe_child("projects")
    target = _safe_child("projects", project_id)
    staging_root = _safe_child(".staging")

    with _STORE_LOCK:
        projects_dir.mkdir(parents=True, exist_ok=True)
        staging_root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(project_id)
        stage = staging_root / f"{project_id}.{uuid.uuid4().hex}"
        stage.mkdir(parents=False, exist_ok=False)
        try:
            _atomic_write_json(stage / "project.json", project)
            workbook_dir = stage / "workbook_revisions" / workbook_id
            _atomic_write_json(workbook_dir / "metadata.json", workbook_revision)
            _atomic_write_bytes(workbook_dir / "source.xlsx", source_content)
            _atomic_write_json(
                workbook_dir / "physical_snapshot.json", physical_snapshot
            )
            _atomic_write_json(
                stage / "imports" / f"{import_id}.json", interview_import
            )
            os.replace(stage, target)
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def accepted_bundle_exists(
    project_id: str,
    workbook_revision_id: str,
    import_id: str,
) -> bool:
    project_id = validate_resource_id(project_id, "project")
    workbook_revision_id = validate_resource_id(
        workbook_revision_id, "workbook"
    )
    import_id = validate_resource_id(import_id, "import")
    project_dir = _safe_child("projects", project_id)
    required = (
        project_dir / "project.json",
        project_dir / "workbook_revisions" / workbook_revision_id / "metadata.json",
        project_dir / "workbook_revisions" / workbook_revision_id / "source.xlsx",
        project_dir
        / "workbook_revisions"
        / workbook_revision_id
        / "physical_snapshot.json",
        project_dir / "imports" / f"{import_id}.json",
    )
    with _STORE_LOCK:
        return all(path.is_file() for path in required)


def finalize_upload_attempt_accepted(
    upload_attempt_id: str,
    *,
    claim_token: str,
    project_id: str,
    workbook_revision_id: str,
    import_id: str,
    precheck_summary: dict[str, Any],
    updated_at: str,
) -> dict[str, Any]:
    """Idempotently finish an attempt after its complete bundle is published."""

    validate_resource_id(upload_attempt_id, "upload")
    if not accepted_bundle_exists(project_id, workbook_revision_id, import_id):
        raise FileNotFoundError("accepted bundle is incomplete")
    with _STORE_LOCK:
        path = _attempt_dir(upload_attempt_id) / "metadata.json"
        metadata = _read_json(path)
        if metadata is None:
            raise FileNotFoundError(upload_attempt_id)
        if metadata.get("status") == "ACCEPTED":
            return metadata
        if (
            metadata.get("status") != "PRECHECKING"
            or metadata.get("precheck_claim_token") != claim_token
        ):
            raise ValueError("upload attempt cannot be accepted from current state")
        expected_ids = {
            "project_id": validate_resource_id(project_id, "project"),
            "workbook_revision_id": validate_resource_id(
                workbook_revision_id, "workbook"
            ),
            "import_id": validate_resource_id(import_id, "import"),
        }
        if any(metadata.get(key) != value for key, value in expected_ids.items()):
            raise ValueError("accepted bundle IDs do not match upload attempt")
        metadata.update(
            {
                "status": "ACCEPTED",
                "updated_at": updated_at,
                "precheck_summary": precheck_summary,
                "error": None,
                "precheck_claim_token": None,
                "precheck_lease_expires_at": None,
            }
        )
        _atomic_write_json(path, metadata)
        return metadata


def cleanup_stale_staging(*, older_than_epoch: float) -> int:
    """Remove old unpublished staging directories, preserving active claims."""

    staging_root = _safe_child(".staging")
    removed = 0
    pattern = re.compile(r"^(project_[0-9a-f]{32})\.[0-9a-f]{32}$")
    with _STORE_LOCK:
        if not staging_root.is_dir():
            return 0
        active_project_ids: set[str] = set()
        attempts_root = _safe_child("upload_attempts")
        now_epoch = time.time()
        if attempts_root.is_dir():
            for attempt_dir in attempts_root.iterdir():
                if (
                    not attempt_dir.is_dir()
                    or not re.fullmatch(r"upload_[0-9a-f]{32}", attempt_dir.name)
                ):
                    continue
                try:
                    metadata = _read_json(attempt_dir / "metadata.json")
                except (OSError, ValueError):
                    continue
                if (
                    metadata
                    and metadata.get("status") == "PRECHECKING"
                    and str(metadata.get("precheck_claim_token") or "")
                    and float(metadata.get("precheck_lease_expires_at") or 0)
                    > now_epoch
                ):
                    project_id = str(metadata.get("project_id") or "")
                    if re.fullmatch(r"project_[0-9a-f]{32}", project_id):
                        active_project_ids.add(project_id)
        for candidate in staging_root.iterdir():
            match = pattern.fullmatch(candidate.name)
            if (
                not candidate.is_dir()
                or match is None
                or match.group(1) in active_project_ids
                or candidate.stat().st_mtime > float(older_than_epoch)
            ):
                continue
            shutil.rmtree(candidate)
            removed += 1
    return removed


def cleanup_orphaned_upload_attempts(*, older_than_epoch: float) -> int:
    """Delete only stale upload dirs that never obtained durable metadata."""

    attempts_root = _safe_child("upload_attempts")
    pattern = re.compile(r"^upload_[0-9a-f]{32}$")
    removed = 0
    with _STORE_LOCK:
        if not attempts_root.is_dir():
            return 0
        for candidate in attempts_root.iterdir():
            if (
                not candidate.is_dir()
                or not pattern.fullmatch(candidate.name)
                or (candidate / "metadata.json").exists()
                or candidate.stat().st_mtime > float(older_than_epoch)
            ):
                continue
            shutil.rmtree(candidate)
            removed += 1
    return removed


def load_interview_import(import_id: str) -> dict[str, Any] | None:
    import_id = validate_resource_id(import_id, "import")
    projects_dir = _safe_child("projects")
    with _STORE_LOCK:
        if not projects_dir.is_dir():
            return None
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir() or not _ID_RE.fullmatch(project_dir.name):
                continue
            value = _read_json(project_dir / "imports" / f"{import_id}.json")
            if value is not None:
                return value
    return None


def load_physical_snapshot(
    project_id: str, workbook_revision_id: str
) -> dict[str, Any] | None:
    project_id = validate_resource_id(project_id, "project")
    workbook_revision_id = validate_resource_id(
        workbook_revision_id, "workbook"
    )
    path = _safe_child(
        "projects",
        project_id,
        "workbook_revisions",
        workbook_revision_id,
        "physical_snapshot.json",
    )
    with _STORE_LOCK:
        return _read_json(path)
