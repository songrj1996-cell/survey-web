"""访谈报告 V2 的本地文件存储。

本模块只负责隔离上传、原子文件写入和正式导入快照的持久化。业务状态机、
鉴权判断和错误文案由 service 层负责。正式项目的三个首批实体放在同一个项目
目录中，并通过一次目录重命名发布，避免只创建其中一部分。
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from app.core import config


_STORE_LOCK = threading.RLock()
_MAPPING_LOCK_TIMEOUT_SECONDS = 10.0
_MAPPING_LOCK_POLL_SECONDS = 0.025
_ID_RE = re.compile(
    r"^(?:upload|job|project|import|workbook|mapping)_[0-9a-f]{32}$"
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


def load_project(project_id: str) -> dict[str, Any] | None:
    project_id = validate_resource_id(project_id, "project")
    with _STORE_LOCK:
        return _read_json(
            _safe_child("projects", project_id, "project.json")
        )


def load_mapping_input_bundle(import_id: str) -> dict[str, Any] | None:
    """Load and cross-check one import's immutable Batch 2 inputs atomically."""

    import_id = validate_resource_id(import_id, "import")
    projects_dir = _safe_child("projects")
    with _STORE_LOCK:
        if not projects_dir.is_dir():
            return None
        for project_dir in projects_dir.iterdir():
            if (
                not project_dir.is_dir()
                or not re.fullmatch(r"project_[0-9a-f]{32}", project_dir.name)
            ):
                continue
            interview_import = _read_json(
                project_dir / "imports" / f"{import_id}.json"
            )
            if interview_import is None:
                continue
            project = _read_json(project_dir / "project.json")
            project_id = str(interview_import.get("project_id") or "")
            workbook_revision_id = str(
                interview_import.get("workbook_revision_id") or ""
            )
            validate_resource_id(project_id, "project")
            validate_resource_id(workbook_revision_id, "workbook")
            if project_id != project_dir.name or project is None:
                raise ValueError("mapping input project mismatch")
            workbook_dir = (
                project_dir / "workbook_revisions" / workbook_revision_id
            )
            workbook_revision = _read_json(workbook_dir / "metadata.json")
            physical_snapshot = _read_json(
                workbook_dir / "physical_snapshot.json"
            )
            if workbook_revision is None or physical_snapshot is None:
                raise ValueError("mapping input bundle is incomplete")
            workbook_content_sha256 = str(
                workbook_revision.get("content_sha256") or ""
            )
            snapshot_content_sha256 = str(
                physical_snapshot.get("content_sha256") or ""
            )
            workbook_snapshot_version = str(
                workbook_revision.get("physical_snapshot_version") or ""
            )
            snapshot_schema_version = str(
                physical_snapshot.get("schema_version") or ""
            )
            snapshot_sha256 = str(
                physical_snapshot.get("snapshot_sha256") or ""
            )
            calculated_snapshot_sha256 = physical_snapshot_sha256(
                physical_snapshot
            )
            import_snapshot_version = str(
                interview_import.get("physical_snapshot_version") or ""
            )
            if (
                interview_import.get("import_id") != import_id
                or project.get("project_id") != project_id
                or workbook_revision.get("project_id") != project_id
                or workbook_revision.get("workbook_revision_id")
                != workbook_revision_id
                or project.get("current_workbook_revision_id")
                != workbook_revision_id
                or project.get("current_import_id") != import_id
                or str(workbook_revision.get("snapshot_sha256") or "")
                != snapshot_sha256
                or calculated_snapshot_sha256 != snapshot_sha256
                or not re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256)
                or not re.fullmatch(r"[0-9a-f]{64}", workbook_content_sha256)
                or not re.fullmatch(r"[0-9a-f]{64}", snapshot_content_sha256)
                or workbook_content_sha256 != snapshot_content_sha256
                or not workbook_snapshot_version
                or not snapshot_schema_version
                or not import_snapshot_version
                or workbook_snapshot_version != snapshot_schema_version
                or import_snapshot_version != snapshot_schema_version
            ):
                raise ValueError("mapping input bundle integrity check failed")
            return {
                "interview_import": interview_import,
                "project": project,
                "workbook_revision": workbook_revision,
                "physical_snapshot": physical_snapshot,
            }
    return None


def physical_snapshot_sha256(snapshot: dict[str, Any]) -> str:
    """Recompute the canonical Batch 1 physical snapshot digest."""

    payload = {
        key: value
        for key, value in snapshot.items()
        if key not in {"original_filename", "snapshot_sha256"}
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _mapping_state_path(project_id: str) -> Path:
    project_id = validate_resource_id(project_id, "project")
    return _safe_child("projects", project_id, "mapping_state.json")


def _mapping_revision_path(project_id: str, mapping_revision_id: str) -> Path:
    project_id = validate_resource_id(project_id, "project")
    mapping_revision_id = validate_resource_id(mapping_revision_id, "mapping")
    return _safe_child(
        "projects",
        project_id,
        "mapping_revisions",
        f"{mapping_revision_id}.json",
    )


def mapping_revision_payload_sha256(revision: dict[str, Any]) -> str:
    """Digest every immutable revision field except the digest itself."""

    payload = {
        key: value
        for key, value in revision.items()
        if key != "revision_payload_sha256"
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def mapping_state_payload_sha256(state: dict[str, Any]) -> str:
    """Digest the complete mutable mapping state except the digest itself."""

    payload = {
        key: value
        for key, value in state.items()
        if key != "state_payload_sha256"
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _require_mapping_state_integrity(state: dict[str, Any]) -> None:
    declared = str(state.get("state_payload_sha256") or "")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", declared)
        or mapping_state_payload_sha256(state) != declared
    ):
        raise ValueError("mapping state payload digest mismatch")


def _mapping_lock_path(project_id: str) -> Path:
    project_id = validate_resource_id(project_id, "project")
    return _safe_child("projects", project_id, ".mapping.lock")


def _try_acquire_file_lock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_file_lock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _is_file_lock_contention(exc: OSError) -> bool:
    return exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(
        exc, "winerror", None
    ) in {33, 36}


@contextmanager
def _mapping_process_lock(
    project_id: str,
    *,
    timeout_seconds: float | None = None,
) -> Iterator[None]:
    """Serialize mapping state changes across application processes.

    The byte-range lock is kept in the existing V2 project directory. Any
    inability to create, acquire, or release it raises and prevents the
    caller from entering or silently completing the critical section.
    """

    project_id = validate_resource_id(project_id, "project")
    lock_path = _mapping_lock_path(project_id)
    project_dir = lock_path.parent
    if not project_dir.is_dir():
        raise FileNotFoundError(project_id)
    timeout = (
        _MAPPING_LOCK_TIMEOUT_SECONDS
        if timeout_seconds is None
        else float(timeout_seconds)
    )
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("invalid mapping lock timeout")
    deadline = time.monotonic() + timeout

    with lock_path.open("a+b", buffering=0) as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())

        acquired = False
        while not acquired:
            try:
                _try_acquire_file_lock(handle)
                acquired = True
            except OSError as exc:
                if not _is_file_lock_contention(exc):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timed out acquiring mapping lock for {project_id}"
                    ) from exc
                time.sleep(min(_MAPPING_LOCK_POLL_SECONDS, remaining))

        try:
            yield
        finally:
            _release_file_lock(handle)


def load_mapping_state(project_id: str) -> dict[str, Any] | None:
    """Load the authoritative mapping head for one project."""

    state_path = _mapping_state_path(project_id)
    with _STORE_LOCK:
        if not state_path.is_file():
            return None
        with _mapping_process_lock(project_id):
            state = _read_json(state_path)
            if state is not None:
                _require_mapping_state_integrity(state)
            return state


def load_mapping_revision(
    project_id: str, mapping_revision_id: str
) -> dict[str, Any] | None:
    """Load one immutable mapping revision by its deterministic ID."""

    revision_path = _mapping_revision_path(project_id, mapping_revision_id)
    with _STORE_LOCK:
        if not _safe_child("projects", project_id).is_dir():
            return None
        with _mapping_process_lock(project_id):
            return _read_json(revision_path)


def save_mapping_revision_cas(
    *,
    project_id: str,
    import_id: str,
    base_mapping_revision: int,
    revision: dict[str, Any],
    updated_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Publish an immutable revision, then advance the authoritative head.

    The revision file is written first. A crash before the state write leaves
    an unreferenced but deterministic revision that the same request can reuse.
    The integer base revision is compared while holding the store lock.
    """

    project_id = validate_resource_id(project_id, "project")
    import_id = validate_resource_id(import_id, "import")
    mapping_revision_id = validate_resource_id(
        str(revision.get("mapping_revision_id") or ""), "mapping"
    )
    revision_number = int(revision.get("revision_number", -1))
    base_mapping_revision = int(base_mapping_revision)
    if base_mapping_revision < 0 or revision_number != base_mapping_revision + 1:
        raise ValueError("invalid mapping revision sequence")
    if (
        revision.get("project_id") != project_id
        or revision.get("import_id") != import_id
    ):
        raise ValueError("mapping revision resource mismatch")
    revision_payload_sha256 = str(
        revision.get("revision_payload_sha256") or ""
    )
    if (
        len(revision_payload_sha256) != 64
        or mapping_revision_payload_sha256(revision) != revision_payload_sha256
    ):
        raise ValueError("mapping revision payload digest mismatch")

    state_path = _mapping_state_path(project_id)
    revision_path = _mapping_revision_path(project_id, mapping_revision_id)
    with _STORE_LOCK:
        with _mapping_process_lock(project_id):
            state = _read_json(state_path)
            if state is None:
                state = {
                    "project_id": project_id,
                    "import_id": import_id,
                    "current_revision_number": 0,
                    "current_mapping_revision_id": None,
                    "current_mapping_sha256": None,
                    "current_revision_payload_sha256": None,
                    "effective_status": "GROUP_CONFIRMATION_REQUIRED",
                    "confirmed_mapping_revision_id": None,
                    "confirmed_mapping_sha256": None,
                    "confirmed_mapping_revision_number": None,
                    "revision_history": [],
                    "confirmation_events": [],
                }
            else:
                _require_mapping_state_integrity(state)
            if state.get("import_id") != import_id:
                raise ValueError("mapping state import mismatch")
            current_number = int(state.get("current_revision_number") or 0)
            if current_number != base_mapping_revision:
                raise FileExistsError("mapping revision conflict")

            existing = _read_json(revision_path)
            if existing is not None and (
                str(existing.get("revision_payload_sha256") or "")
                != mapping_revision_payload_sha256(existing)
            ):
                raise ValueError("existing mapping revision payload digest mismatch")
            identity_keys = (
                "project_id",
                "import_id",
                "revision_number",
                "mapping_sha256",
                "change_kind",
                "change_reason",
                "created_by",
                "restored_from_mapping_revision_id",
                "restored_from_revision_number",
            )
            if existing is not None and any(
                existing.get(key) != revision.get(key) for key in identity_keys
            ):
                raise FileExistsError("mapping revision identity collision")
            durable_revision = existing or revision
            durable_revision_payload_sha256 = str(
                durable_revision.get("revision_payload_sha256") or ""
            )
            if existing is None:
                _atomic_write_json(revision_path, revision)

            history = list(state.get("revision_history") or [])
            history_entry = {
                "revision_number": revision_number,
                "mapping_revision_id": mapping_revision_id,
                "mapping_sha256": str(durable_revision.get("mapping_sha256") or ""),
                "revision_payload_sha256": durable_revision_payload_sha256,
                "change_kind": str(
                    durable_revision.get("change_kind") or "manual_edit"
                ),
                "change_reason": str(
                    durable_revision.get("change_reason") or ""
                ),
                "restored_from_mapping_revision_id": durable_revision.get(
                    "restored_from_mapping_revision_id"
                ),
                "restored_from_revision_number": durable_revision.get(
                    "restored_from_revision_number"
                ),
                "created_at": str(durable_revision.get("created_at") or updated_at),
            }
            if not any(
                item.get("mapping_revision_id") == mapping_revision_id
                for item in history
                if isinstance(item, dict)
            ):
                history.append(history_entry)
            state.update(
                {
                    "current_revision_number": revision_number,
                    "current_mapping_revision_id": mapping_revision_id,
                    "current_mapping_sha256": history_entry["mapping_sha256"],
                    "current_revision_payload_sha256": (
                        durable_revision_payload_sha256
                    ),
                    "effective_status": "GROUP_CONFIRMATION_REQUIRED",
                    "confirmed_mapping_revision_id": None,
                    "confirmed_mapping_sha256": None,
                    "confirmed_mapping_revision_number": None,
                    "revision_history": history,
                    "confirmation_events": list(
                        state.get("confirmation_events") or []
                    ),
                    "updated_at": updated_at,
                }
            )
            state["state_payload_sha256"] = mapping_state_payload_sha256(state)
            _atomic_write_json(state_path, state)
            return durable_revision, state


def confirm_mapping_revision_cas(
    *,
    project_id: str,
    import_id: str,
    base_mapping_revision: int,
    mapping_sha256: str,
    confirmed_by: str,
    confirmed_at: str,
) -> dict[str, Any]:
    """Confirm only the current mapping head, preserving confirmation history."""

    project_id = validate_resource_id(project_id, "project")
    import_id = validate_resource_id(import_id, "import")
    state_path = _mapping_state_path(project_id)
    with _STORE_LOCK:
        with _mapping_process_lock(project_id):
            state = _read_json(state_path)
            if state is None:
                raise FileNotFoundError("mapping state does not exist")
            _require_mapping_state_integrity(state)
            if state.get("import_id") != import_id:
                raise ValueError("mapping state import mismatch")
            if (
                int(state.get("current_revision_number") or 0)
                != int(base_mapping_revision)
                or str(state.get("current_mapping_sha256") or "")
                != str(mapping_sha256 or "")
            ):
                raise FileExistsError("mapping revision conflict")

            revision_id = validate_resource_id(
                str(state.get("current_mapping_revision_id") or ""), "mapping"
            )
            revision = _read_json(
                _mapping_revision_path(project_id, revision_id)
            )
            state_revision_payload_sha256 = str(
                state.get("current_revision_payload_sha256") or ""
            )
            if (
                revision is None
                or not state_revision_payload_sha256
                or str(revision.get("revision_payload_sha256") or "")
                != state_revision_payload_sha256
                or mapping_revision_payload_sha256(revision)
                != state_revision_payload_sha256
            ):
                raise ValueError("mapping revision payload integrity check failed")
            events = list(state.get("confirmation_events") or [])
            already_confirmed = any(
                isinstance(event, dict)
                and event.get("mapping_revision_id") == revision_id
                and event.get("mapping_sha256") == mapping_sha256
                for event in events
            )
            if already_confirmed:
                return state
            events.append(
                {
                    "mapping_revision_id": revision_id,
                    "revision_number": int(base_mapping_revision),
                    "mapping_sha256": mapping_sha256,
                    "confirmed_by": str(confirmed_by or ""),
                    "confirmed_at": confirmed_at,
                }
            )
            state.update(
                {
                    "effective_status": "GROUP_MAPPING_CONFIRMED",
                    "confirmed_mapping_revision_id": revision_id,
                    "confirmed_mapping_sha256": mapping_sha256,
                    "confirmed_mapping_revision_number": int(
                        base_mapping_revision
                    ),
                    "confirmation_events": events,
                    "updated_at": confirmed_at,
                }
            )
            state["state_payload_sha256"] = mapping_state_payload_sha256(state)
            _atomic_write_json(state_path, state)
            return state
