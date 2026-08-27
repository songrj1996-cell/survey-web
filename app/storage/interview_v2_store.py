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
import unicodedata
import uuid
from copy import deepcopy
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
    r"^(?:upload|job|project|import|workbook|mapping|structure|evidence|boundary|coverage|dossier)_[0-9a-f]{32}$"
)
_EVIDENCE_ID_RE = re.compile(r"^(?:ev|evidence)_[0-9a-f]{32}$")
_REVIEW_ISSUE_ID_RE = re.compile(r"^(?:issue|review)_[0-9a-f]{32}$")
_MANUAL_OVERRIDE_ID_RE = re.compile(r"^(?:override)_[0-9a-f]{32}$")
_GROUP_ID_RE = re.compile(r"^group_[0-9a-f]{32}$")
_PARTICIPANT_ID_RE = re.compile(r"^participant_[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STRUCTURE_STATUSES = {"STRUCTURE_REVIEW_REQUIRED", "READY_FOR_DOSSIERS"}
_STRUCTURE_STATUS_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


class StructureInputConflictError(RuntimeError):
    """The confirmed mapping head moved after structure work started."""

    def __init__(
        self,
        *,
        current_mapping_revision_id: str,
        current_mapping_sha256: str,
        current_mapping_status: str,
    ) -> None:
        super().__init__("confirmed structure input is no longer current")
        self.current_mapping_revision_id = current_mapping_revision_id
        self.current_mapping_sha256 = current_mapping_sha256
        self.current_mapping_status = current_mapping_status


class AnalysisBoundaryInputConflictError(RuntimeError):
    """The structure/evidence head moved before boundary publication."""

    def __init__(
        self,
        *,
        current_structure_revision_id: str | None,
        current_evidence_revision_id: str | None,
        current_structure_status: str,
    ) -> None:
        super().__init__("confirmed analysis-boundary input is no longer current")
        self.current_structure_revision_id = current_structure_revision_id
        self.current_evidence_revision_id = current_evidence_revision_id
        self.current_structure_status = current_structure_status


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


# ---------------------------------------------------------------------------
# Batch 3A: immutable structure/evidence revisions and review checkpoints.
#
# These functions deliberately share the per-project mapping lock.  A
# structure publication therefore cannot race a mapping confirmation/edit and
# accidentally publish against a mapping head that has already moved.


def _canonical_payload_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def structure_revision_payload_sha256(revision: dict[str, Any]) -> str:
    """Digest every immutable structure revision field except its digest."""

    return _canonical_payload_sha256(
        {
            key: value
            for key, value in revision.items()
            if key != "revision_payload_sha256"
        }
    )


def evidence_revision_payload_sha256(revision: dict[str, Any]) -> str:
    """Digest every immutable evidence revision field except its digest."""

    return _canonical_payload_sha256(
        {
            key: value
            for key, value in revision.items()
            if key != "revision_payload_sha256"
        }
    )


def structure_state_payload_sha256(state: dict[str, Any]) -> str:
    """Digest the durable structure head, excluding read-time derived fields."""

    return _canonical_payload_sha256(
        {
            key: value
            for key, value in state.items()
            if key
            not in {
                "state_payload_sha256",
                "is_stale",
                "artifact_status",
            }
        }
    )


def _review_issues_payload_sha256(bundle: dict[str, Any]) -> str:
    return _canonical_payload_sha256(
        {
            key: value
            for key, value in bundle.items()
            if key != "review_issues_payload_sha256"
        }
    )


def _locator_payload_sha256(locator: dict[str, Any]) -> str:
    return _canonical_payload_sha256(
        {
            key: value
            for key, value in locator.items()
            if key != "locator_payload_sha256"
        }
    )


def _manual_override_payload_sha256(override: dict[str, Any]) -> str:
    return _canonical_payload_sha256(
        {
            key: value
            for key, value in override.items()
            if key != "override_payload_sha256"
        }
    )


def _structure_state_path(project_id: str) -> Path:
    project_id = validate_resource_id(project_id, "project")
    return _safe_child("projects", project_id, "structure_state.json")


def _structure_revision_path(
    project_id: str, structure_revision_id: str
) -> Path:
    project_id = validate_resource_id(project_id, "project")
    structure_revision_id = validate_resource_id(
        structure_revision_id, "structure"
    )
    return _safe_child(
        "projects",
        project_id,
        "structure_revisions",
        f"{structure_revision_id}.json",
    )


def _evidence_revision_path(
    project_id: str, evidence_revision_id: str
) -> Path:
    project_id = validate_resource_id(project_id, "project")
    evidence_revision_id = validate_resource_id(
        evidence_revision_id, "evidence"
    )
    return _safe_child(
        "projects",
        project_id,
        "evidence_revisions",
        f"{evidence_revision_id}.json",
    )


def _review_issues_path(
    project_id: str, evidence_revision_id: str
) -> Path:
    project_id = validate_resource_id(project_id, "project")
    evidence_revision_id = validate_resource_id(
        evidence_revision_id, "evidence"
    )
    return _safe_child(
        "projects",
        project_id,
        "review_issue_revisions",
        f"{evidence_revision_id}.json",
    )


def _manual_override_path(project_id: str, override_id: str) -> Path:
    project_id = validate_resource_id(project_id, "project")
    override_id = _validate_entity_id(
        override_id,
        _MANUAL_OVERRIDE_ID_RE,
        "manual override",
    )
    return _safe_child(
        "projects",
        project_id,
        "manual_overrides",
        f"{override_id}.json",
    )


def _evidence_locator_path(
    evidence_id: str,
) -> Path:
    evidence_id = _validate_entity_id(
        evidence_id, _EVIDENCE_ID_RE, "evidence"
    )
    return _safe_child(
        "indexes",
        "evidence",
        f"{evidence_id}.json",
    )


def _review_issue_locator_path(
    issue_id: str,
) -> Path:
    issue_id = _validate_entity_id(
        issue_id, _REVIEW_ISSUE_ID_RE, "review issue"
    )
    return _safe_child(
        "indexes",
        "review_issues",
        f"{issue_id}.json",
    )


def _validate_entity_id(
    value: str,
    pattern: re.Pattern[str],
    label: str,
) -> str:
    entity_id = str(value or "").strip()
    if not pattern.fullmatch(entity_id):
        raise ValueError(f"invalid interview V2 {label} id")
    return entity_id


def _validated_revision_number(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"invalid {label} revision number")
    return value


def _frozen_source(revision: dict[str, Any]) -> dict[str, str]:
    """Read immutable upstream references from a revision or its source block."""

    source = revision.get("source")
    if source is None:
        source = {}
    if not isinstance(source, dict):
        raise ValueError("revision source must be an object")
    source_containers: list[dict[str, Any]] = [revision, source]
    for content_key in ("structure", "evidence"):
        content = revision.get(content_key)
        if content is None:
            continue
        if not isinstance(content, dict):
            raise ValueError(f"revision {content_key} must be an object")
        nested_source = content.get("source")
        if nested_source is not None:
            if not isinstance(nested_source, dict):
                raise ValueError(
                    f"revision {content_key} source must be an object"
                )
            source_containers.append(nested_source)

    aliases = {
        "project_id": ("project_id",),
        "import_id": ("import_id",),
        "workbook_revision_id": ("workbook_revision_id",),
        "snapshot_sha256": ("snapshot_sha256", "base_snapshot_sha256"),
        "mapping_revision_id": ("mapping_revision_id",),
        "mapping_sha256": ("mapping_sha256",),
    }
    result: dict[str, str] = {}
    for target, names in aliases.items():
        values: list[str] = []
        for container in source_containers:
            for name in names:
                if name in container and container.get(name) is not None:
                    values.append(str(container.get(name) or ""))
        if not values or any(value != values[0] for value in values[1:]):
            raise ValueError(f"revision {target} reference mismatch")
        result[target] = values[0]

    validate_resource_id(result["project_id"], "project")
    validate_resource_id(result["import_id"], "import")
    validate_resource_id(result["workbook_revision_id"], "workbook")
    validate_resource_id(result["mapping_revision_id"], "mapping")
    if not _SHA256_RE.fullmatch(result["snapshot_sha256"]):
        raise ValueError("invalid revision snapshot digest")
    if not _SHA256_RE.fullmatch(result["mapping_sha256"]):
        raise ValueError("invalid revision mapping digest")
    return result


def _confirmed_input_descriptor(
    *,
    project_id: str,
    import_id: str,
    workbook_revision_id: str,
    snapshot_sha256: str,
    mapping_revision_id: str,
    mapping_sha256: str,
) -> dict[str, str]:
    descriptor = {
        "project_id": validate_resource_id(project_id, "project"),
        "import_id": validate_resource_id(import_id, "import"),
        "workbook_revision_id": validate_resource_id(
            workbook_revision_id, "workbook"
        ),
        "snapshot_sha256": str(snapshot_sha256 or ""),
        "mapping_revision_id": validate_resource_id(
            mapping_revision_id, "mapping"
        ),
        "mapping_sha256": str(mapping_sha256 or ""),
    }
    if not _SHA256_RE.fullmatch(descriptor["snapshot_sha256"]):
        raise ValueError("invalid confirmed snapshot digest")
    if not _SHA256_RE.fullmatch(descriptor["mapping_sha256"]):
        raise ValueError("invalid confirmed mapping digest")
    return descriptor


def confirmed_structure_input_sha256(descriptor: dict[str, Any]) -> str:
    """Create the canonical fingerprint for the six frozen structure inputs."""

    normalized = _confirmed_input_descriptor(
        project_id=str(descriptor.get("project_id") or ""),
        import_id=str(descriptor.get("import_id") or ""),
        workbook_revision_id=str(
            descriptor.get("workbook_revision_id") or ""
        ),
        snapshot_sha256=str(descriptor.get("snapshot_sha256") or ""),
        mapping_revision_id=str(
            descriptor.get("mapping_revision_id") or ""
        ),
        mapping_sha256=str(descriptor.get("mapping_sha256") or ""),
    )
    return _canonical_payload_sha256(normalized)


def _load_accepted_bundle_for_project_locked(
    project_id: str, import_id: str
) -> dict[str, Any] | None:
    project_id = validate_resource_id(project_id, "project")
    import_id = validate_resource_id(import_id, "import")
    project_dir = _safe_child("projects", project_id)
    interview_import = _read_json(
        project_dir / "imports" / f"{import_id}.json"
    )
    if interview_import is None:
        return None
    project = _read_json(project_dir / "project.json")
    workbook_revision_id = validate_resource_id(
        str(interview_import.get("workbook_revision_id") or ""), "workbook"
    )
    workbook_dir = project_dir / "workbook_revisions" / workbook_revision_id
    workbook_revision = _read_json(workbook_dir / "metadata.json")
    physical_snapshot = _read_json(workbook_dir / "physical_snapshot.json")
    if project is None or workbook_revision is None or physical_snapshot is None:
        raise ValueError("structure input bundle is incomplete")

    snapshot_sha256 = str(physical_snapshot.get("snapshot_sha256") or "")
    content_sha256 = str(physical_snapshot.get("content_sha256") or "")
    snapshot_version = str(physical_snapshot.get("schema_version") or "")
    if (
        project.get("project_id") != project_id
        or project.get("current_import_id") != import_id
        or project.get("current_workbook_revision_id") != workbook_revision_id
        or interview_import.get("import_id") != import_id
        or interview_import.get("project_id") != project_id
        or workbook_revision.get("project_id") != project_id
        or workbook_revision.get("workbook_revision_id")
        != workbook_revision_id
        or str(workbook_revision.get("snapshot_sha256") or "")
        != snapshot_sha256
        or str(workbook_revision.get("content_sha256") or "")
        != content_sha256
        or str(workbook_revision.get("physical_snapshot_version") or "")
        != snapshot_version
        or str(interview_import.get("physical_snapshot_version") or "")
        != snapshot_version
        or not _SHA256_RE.fullmatch(snapshot_sha256)
        or not _SHA256_RE.fullmatch(content_sha256)
        or physical_snapshot_sha256(physical_snapshot) != snapshot_sha256
    ):
        raise ValueError("structure input bundle integrity check failed")
    return {
        "interview_import": interview_import,
        "project": project,
        "workbook_revision": workbook_revision,
        "physical_snapshot": physical_snapshot,
    }


def _load_confirmed_mapping_head_locked(
    project_id: str, import_id: str
) -> dict[str, Any]:
    """Verify only the current confirmed mapping head, without snapshot IO."""

    mapping_state = _read_json(_mapping_state_path(project_id))
    if mapping_state is None:
        raise ValueError("confirmed mapping state does not exist")
    _require_mapping_state_integrity(mapping_state)
    mapping_revision_id = validate_resource_id(
        str(mapping_state.get("confirmed_mapping_revision_id") or ""),
        "mapping",
    )
    mapping_sha256 = str(mapping_state.get("confirmed_mapping_sha256") or "")
    if (
        mapping_state.get("project_id") != project_id
        or mapping_state.get("import_id") != import_id
        or mapping_state.get("effective_status") != "GROUP_MAPPING_CONFIRMED"
        or mapping_revision_id
        != str(mapping_state.get("current_mapping_revision_id") or "")
        or mapping_sha256
        != str(mapping_state.get("current_mapping_sha256") or "")
        or not _SHA256_RE.fullmatch(mapping_sha256)
    ):
        raise ValueError("mapping head is not currently confirmed")
    mapping_revision = _read_json(
        _mapping_revision_path(project_id, mapping_revision_id)
    )
    if mapping_revision is None:
        raise ValueError("confirmed mapping revision does not exist")
    revision_payload_sha256 = str(
        mapping_revision.get("revision_payload_sha256") or ""
    )
    mapping = mapping_revision.get("mapping")
    if not isinstance(mapping, dict):
        raise ValueError("confirmed mapping payload is invalid")
    if (
        mapping_revision.get("project_id") != project_id
        or mapping_revision.get("import_id") != import_id
        or mapping_revision.get("mapping_revision_id") != mapping_revision_id
        or mapping_revision.get("mapping_sha256") != mapping_sha256
        or _canonical_payload_sha256(mapping) != mapping_sha256
        or not _SHA256_RE.fullmatch(revision_payload_sha256)
        or mapping_revision_payload_sha256(mapping_revision)
        != revision_payload_sha256
        or mapping_state.get("current_revision_payload_sha256")
        != revision_payload_sha256
    ):
        raise ValueError("confirmed mapping revision integrity check failed")
    return {
        "mapping_state": mapping_state,
        "mapping_revision": mapping_revision,
        "mapping_revision_id": mapping_revision_id,
        "mapping_sha256": mapping_sha256,
    }


def _require_current_confirmed_mapping_input_locked(
    *,
    project_id: str,
    import_id: str,
    expected_mapping_revision_id: str,
    expected_mapping_sha256: str,
) -> dict[str, Any]:
    """Fail with a typed CAS conflict when the mapping head has moved.

    This check runs under the shared mapping lock immediately before any
    structure artifacts are published.  Integrity failures remain ordinary
    validation errors; only a valid draft or a valid replacement head is a
    recoverable input conflict.
    """

    mapping_state = _read_json(_mapping_state_path(project_id))
    if mapping_state is None:
        raise ValueError("confirmed mapping state does not exist")
    _require_mapping_state_integrity(mapping_state)
    if (
        mapping_state.get("project_id") != project_id
        or mapping_state.get("import_id") != import_id
    ):
        raise ValueError("mapping state resource mismatch")

    current_mapping_revision_id = validate_resource_id(
        str(mapping_state.get("current_mapping_revision_id") or ""),
        "mapping",
    )
    current_mapping_sha256 = str(
        mapping_state.get("current_mapping_sha256") or ""
    )
    if not _SHA256_RE.fullmatch(current_mapping_sha256):
        raise ValueError("invalid current mapping digest")
    current_mapping_status = str(mapping_state.get("effective_status") or "")
    if current_mapping_status not in {
        "GROUP_CONFIRMATION_REQUIRED",
        "GROUP_MAPPING_CONFIRMED",
    }:
        raise ValueError("invalid current mapping status")

    if current_mapping_status == "GROUP_MAPPING_CONFIRMED":
        confirmed_mapping_revision_id = validate_resource_id(
            str(mapping_state.get("confirmed_mapping_revision_id") or ""),
            "mapping",
        )
        confirmed_mapping_sha256 = str(
            mapping_state.get("confirmed_mapping_sha256") or ""
        )
        if (
            not _SHA256_RE.fullmatch(confirmed_mapping_sha256)
            or confirmed_mapping_revision_id != current_mapping_revision_id
            or confirmed_mapping_sha256 != current_mapping_sha256
        ):
            raise ValueError("confirmed mapping state head is inconsistent")
    elif any(
        mapping_state.get(key) is not None
        for key in (
            "confirmed_mapping_revision_id",
            "confirmed_mapping_sha256",
            "confirmed_mapping_revision_number",
        )
    ):
        raise ValueError("draft mapping state retains confirmed references")

    if (
        current_mapping_status != "GROUP_MAPPING_CONFIRMED"
        or current_mapping_revision_id != expected_mapping_revision_id
        or current_mapping_sha256 != expected_mapping_sha256
    ):
        raise StructureInputConflictError(
            current_mapping_revision_id=current_mapping_revision_id,
            current_mapping_sha256=current_mapping_sha256,
            current_mapping_status=current_mapping_status,
        )

    mapping_head = _load_confirmed_mapping_head_locked(project_id, import_id)
    if (
        mapping_head["mapping_revision_id"] != expected_mapping_revision_id
        or mapping_head["mapping_sha256"] != expected_mapping_sha256
    ):
        raise StructureInputConflictError(
            current_mapping_revision_id=mapping_head["mapping_revision_id"],
            current_mapping_sha256=mapping_head["mapping_sha256"],
            current_mapping_status=current_mapping_status,
        )
    return mapping_head


def _load_confirmed_structure_input_locked(
    project_id: str, import_id: str
) -> dict[str, Any] | None:
    """Load a confirmed mapping and all immutable inputs under mapping lock."""

    accepted = _load_accepted_bundle_for_project_locked(project_id, import_id)
    if accepted is None:
        return None
    mapping_head = _load_confirmed_mapping_head_locked(project_id, import_id)
    mapping_state = mapping_head["mapping_state"]
    mapping_revision = mapping_head["mapping_revision"]
    mapping_revision_id = mapping_head["mapping_revision_id"]
    mapping_sha256 = mapping_head["mapping_sha256"]
    if (
        mapping_revision.get("workbook_revision_id")
        != accepted["interview_import"].get("workbook_revision_id")
    ):
        raise ValueError("confirmed mapping workbook reference mismatch")

    descriptor = _confirmed_input_descriptor(
        project_id=project_id,
        import_id=import_id,
        workbook_revision_id=str(
            accepted["interview_import"].get("workbook_revision_id") or ""
        ),
        snapshot_sha256=str(
            accepted["physical_snapshot"].get("snapshot_sha256") or ""
        ),
        mapping_revision_id=mapping_revision_id,
        mapping_sha256=mapping_sha256,
    )
    return {
        **accepted,
        "mapping_state": mapping_state,
        "mapping_revision": mapping_revision,
        "confirmed_input": descriptor,
        "input_fingerprint": confirmed_structure_input_sha256(descriptor),
    }


def load_confirmed_structure_input_bundle(
    import_id: str,
) -> dict[str, Any] | None:
    """Read and verify the complete frozen input for a Batch 3A build."""

    import_id = validate_resource_id(import_id, "import")
    projects_dir = _safe_child("projects")
    with _STORE_LOCK:
        if not projects_dir.is_dir():
            return None
        matches: list[str] = []
        for project_dir in projects_dir.iterdir():
            if (
                project_dir.is_dir()
                and re.fullmatch(r"project_[0-9a-f]{32}", project_dir.name)
                and (project_dir / "imports" / f"{import_id}.json").is_file()
            ):
                matches.append(project_dir.name)
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("interview import is not globally unique")
        project_id = matches[0]
        with _mapping_process_lock(project_id):
            return _load_confirmed_structure_input_locked(project_id, import_id)


def _require_revision_digest(
    revision: dict[str, Any],
    digest_function: Any,
    label: str,
) -> str:
    declared = str(revision.get("revision_payload_sha256") or "")
    if (
        not _SHA256_RE.fullmatch(declared)
        or digest_function(revision) != declared
    ):
        raise ValueError(f"{label} revision payload digest mismatch")
    return declared


def _require_structure_revision(
    revision: dict[str, Any],
    *,
    project_id: str | None = None,
    import_id: str | None = None,
) -> tuple[str, int, dict[str, str], str]:
    revision_id = validate_resource_id(
        str(revision.get("structure_revision_id") or ""), "structure"
    )
    revision_number = _validated_revision_number(
        revision.get("revision_number"), "structure"
    )
    source = _frozen_source(revision)
    if (
        (project_id is not None and source["project_id"] != project_id)
        or (import_id is not None and source["import_id"] != import_id)
    ):
        raise ValueError("structure revision resource mismatch")
    digest = _require_revision_digest(
        revision,
        structure_revision_payload_sha256,
        "structure",
    )
    return revision_id, revision_number, source, digest


def _evidence_entries(revision: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = [
        revision.get("entries"),
        revision.get("evidence_entries"),
    ]
    evidence_payload = revision.get("evidence")
    if isinstance(evidence_payload, dict):
        candidates.extend(
            [
                evidence_payload.get("entries"),
                evidence_payload.get("evidence_entries"),
            ]
        )
    entries = next((item for item in candidates if item is not None), None)
    if not isinstance(entries, list) or any(
        not isinstance(item, dict) for item in entries
    ):
        raise ValueError("evidence revision entries must be a list of objects")
    return entries


def _expected_participants(revision: dict[str, Any]) -> list[dict[str, str]]:
    candidates: list[Any] = []
    if "expected_participants" in revision:
        candidates.append(revision.get("expected_participants"))
    evidence_payload = revision.get("evidence")
    if isinstance(evidence_payload, dict) and "expected_participants" in evidence_payload:
        candidates.append(evidence_payload.get("expected_participants"))
    if not candidates or any(candidate != candidates[0] for candidate in candidates[1:]):
        raise ValueError("evidence expected participant manifest is missing or inconsistent")
    manifest = candidates[0]
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("evidence expected participant manifest must be non-empty")

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in manifest:
        if not isinstance(item, dict) or set(item) != {"participant_id", "group_id"}:
            raise ValueError("evidence expected participant manifest is invalid")
        participant_id = _validate_entity_id(
            str(item.get("participant_id") or ""),
            _PARTICIPANT_ID_RE,
            "participant",
        )
        group_id = _validate_entity_id(
            str(item.get("group_id") or ""), _GROUP_ID_RE, "group"
        )
        identity = (group_id, participant_id)
        if identity in seen:
            raise ValueError("duplicate expected participant identity")
        seen.add(identity)
        normalized.append(
            {"participant_id": participant_id, "group_id": group_id}
        )
    expected_order = sorted(
        normalized, key=lambda item: (item["group_id"], item["participant_id"])
    )
    if manifest != expected_order:
        raise ValueError("evidence expected participant manifest is not canonical")
    return normalized


def _confirmed_mapping_expected_participants(
    mapping_revision: dict[str, Any],
) -> list[dict[str, str]]:
    mapping = mapping_revision.get("mapping")
    if not isinstance(mapping, dict):
        raise ValueError("confirmed mapping payload is invalid")
    participants: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    groups = mapping.get("groups")
    if not isinstance(groups, list):
        raise ValueError("confirmed mapping groups are invalid")
    for group in groups:
        if not isinstance(group, dict) or group.get("decision_status") != "confirmed":
            raise ValueError("confirmed mapping group is invalid")
        group_id = _validate_entity_id(
            str(group.get("group_id") or ""), _GROUP_ID_RE, "group"
        )
        group_participants = group.get("participants")
        if not isinstance(group_participants, list):
            raise ValueError("confirmed mapping participants are invalid")
        for participant in group_participants:
            if (
                not isinstance(participant, dict)
                or participant.get("decision_status") != "confirmed"
            ):
                raise ValueError("confirmed mapping participant is invalid")
            participant_id = _validate_entity_id(
                str(participant.get("participant_id") or ""),
                _PARTICIPANT_ID_RE,
                "participant",
            )
            identity = (group_id, participant_id)
            if identity in seen:
                raise ValueError("duplicate confirmed participant identity")
            seen.add(identity)
            participants.append(
                {"participant_id": participant_id, "group_id": group_id}
            )
    participants.sort(key=lambda item: (item["group_id"], item["participant_id"]))
    if not participants:
        raise ValueError("confirmed mapping has no expected participants")
    return participants


def _entry_evidence_id(entry: dict[str, Any]) -> str:
    return _validate_entity_id(
        str(entry.get("evidence_id") or ""),
        _EVIDENCE_ID_RE,
        "evidence",
    )


def _require_evidence_revision(
    revision: dict[str, Any],
    *,
    project_id: str | None = None,
    import_id: str | None = None,
) -> tuple[str, int, dict[str, str], str, list[dict[str, Any]]]:
    revision_id = validate_resource_id(
        str(revision.get("evidence_revision_id") or ""), "evidence"
    )
    revision_number = _validated_revision_number(
        revision.get("revision_number"), "evidence"
    )
    source = _frozen_source(revision)
    if (
        (project_id is not None and source["project_id"] != project_id)
        or (import_id is not None and source["import_id"] != import_id)
    ):
        raise ValueError("evidence revision resource mismatch")
    validate_resource_id(
        str(revision.get("structure_revision_id") or ""), "structure"
    )
    expected_participants = _expected_participants(revision)
    expected_identities = {
        (item["group_id"], item["participant_id"])
        for item in expected_participants
    }
    entries = _evidence_entries(revision)
    evidence_ids = [_entry_evidence_id(entry) for entry in entries]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("duplicate evidence id in revision")
    for entry in entries:
        participant_id = _validate_entity_id(
            str(entry.get("participant_id") or ""),
            _PARTICIPANT_ID_RE,
            "participant",
        )
        group_id = _validate_entity_id(
            str(entry.get("group_id") or ""), _GROUP_ID_RE, "group"
        )
        if (group_id, participant_id) not in expected_identities:
            raise ValueError("evidence participant is outside the frozen manifest")
    digest = _require_revision_digest(
        revision,
        evidence_revision_payload_sha256,
        "evidence",
    )
    return revision_id, revision_number, source, digest, entries


def _review_issue_id(issue: dict[str, Any]) -> str:
    first = issue.get("review_issue_id")
    second = issue.get("issue_id")
    if first is not None and second is not None and first != second:
        raise ValueError("review issue id aliases disagree")
    return _validate_entity_id(
        str(first or second or ""),
        _REVIEW_ISSUE_ID_RE,
        "review issue",
    )


def _normalized_review_issues(
    review_issues: list[dict[str, Any]],
    *,
    project_id: str,
    import_id: str,
    structure_revision_id: str,
    evidence_revision_id: str,
) -> list[dict[str, Any]]:
    if not isinstance(review_issues, list) or any(
        not isinstance(item, dict) for item in review_issues
    ):
        raise ValueError("review issues must be a list of objects")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    references = {
        "project_id": project_id,
        "import_id": import_id,
        "structure_revision_id": structure_revision_id,
        "evidence_revision_id": evidence_revision_id,
    }
    for original in review_issues:
        item = dict(original)
        issue_id = _review_issue_id(item)
        if issue_id in seen:
            raise ValueError("duplicate review issue id")
        seen.add(issue_id)
        item.setdefault("issue_id", issue_id)
        for key, expected in references.items():
            if key in item and item.get(key) != expected:
                raise ValueError("review issue revision reference mismatch")
            item[key] = expected
        result.append(item)
    return result


def _derived_structure_status(
    review_issues: list[dict[str, Any]],
    evidence_entries: list[dict[str, Any]],
    expected_participants: list[dict[str, str]],
) -> str:
    """Derive the only publishable status from persisted review semantics."""

    for issue in review_issues:
        if issue.get("status") not in {"open", "resolved"}:
            raise ValueError("review issue status is invalid")
        if issue.get("severity") not in {"blocking", "recommended"}:
            raise ValueError("review issue severity is invalid")

    allowed_evidence_types = {
        "participant_self_report",
        "researcher_observation",
    }
    allowed_identity_statuses = {
        "needs_review",
        "system_verified",
        "human_confirmed",
    }
    allowed_formula_cache_statuses = {
        "not_applicable",
        "available",
        "unavailable",
    }
    for entry in evidence_entries:
        inclusion_status = entry.get("inclusion_status")
        if inclusion_status not in {
            "included",
            "excluded_by_user",
        }:
            raise ValueError("evidence inclusion status is invalid")
        identity_status = entry.get("identity_decision_status")
        if identity_status not in allowed_identity_statuses:
            raise ValueError("evidence identity decision status is invalid")
        if entry.get("formula_cache_status") not in allowed_formula_cache_statuses:
            raise ValueError("evidence formula cache status is invalid")
        if inclusion_status == "included":
            normalized_content = entry.get("normalized_content")
            if not isinstance(normalized_content, str):
                raise ValueError("included evidence normalized content is invalid")
            canonical_content = unicodedata.normalize(
                "NFC",
                normalized_content.replace("\x00", "")
                .replace("\r\n", "\n")
                .replace("\r", "\n"),
            ).strip()
            if not canonical_content or normalized_content != canonical_content:
                raise ValueError("included evidence normalized content is invalid")
        if (
            inclusion_status == "included"
            and entry.get("formula_cache_status") == "available"
        ):
            display_value = entry.get("display_content")
            if display_value is None:
                normalized_display = ""
            elif isinstance(display_value, bool):
                normalized_display = "true" if display_value else "false"
            else:
                normalized_display = str(display_value)
            normalized_display = unicodedata.normalize(
                "NFC",
                normalized_display.replace("\x00", "")
                .replace("\r\n", "\n")
                .replace("\r", "\n"),
            ).strip()
            if (
                not normalized_display
                or entry.get("normalized_content") != normalized_display
            ):
                raise ValueError("available formula evidence display is invalid")
        evidence_type = entry.get("evidence_type")
        if evidence_type is not None and evidence_type not in allowed_evidence_types:
            raise ValueError("evidence type is invalid")
        if (
            identity_status in {"system_verified", "human_confirmed"}
            and evidence_type not in allowed_evidence_types
        ):
            raise ValueError("verified evidence type is missing")

    has_open_blocking_issue = any(
        issue.get("status") == "open"
        and issue.get("severity") == "blocking"
        for issue in review_issues
    )
    has_unsafe_included_evidence = any(
        entry.get("inclusion_status") == "included"
        and (
            entry.get("identity_decision_status")
            not in {"system_verified", "human_confirmed"}
            or not entry.get("module_id")
            or not entry.get("main_question_id")
            or entry.get("evidence_type") not in allowed_evidence_types
            or entry.get("formula_cache_status") == "unavailable"
        )
        for entry in evidence_entries
    )
    reportable_participants = {
        (str(entry.get("group_id") or ""), str(entry.get("participant_id") or ""))
        for entry in evidence_entries
        if entry.get("inclusion_status") == "included"
        and entry.get("identity_decision_status")
        in {"system_verified", "human_confirmed"}
        and entry.get("module_id")
        and entry.get("main_question_id")
        and entry.get("evidence_type") in allowed_evidence_types
        and entry.get("formula_cache_status") != "unavailable"
    }
    expected_identities = {
        (item["group_id"], item["participant_id"])
        for item in expected_participants
    }
    return (
        "STRUCTURE_REVIEW_REQUIRED"
        if (
            not expected_identities.issubset(reportable_participants)
            or has_open_blocking_issue
            or has_unsafe_included_evidence
        )
        else "READY_FOR_DOSSIERS"
    )


def _review_issues_bundle(
    *,
    project_id: str,
    import_id: str,
    structure_revision_id: str,
    evidence_revision_id: str,
    review_issues: list[dict[str, Any]],
    request_fingerprint: str,
    effective_status: str,
) -> dict[str, Any]:
    bundle = {
        "schema_version": "interview-review-issues/1.0",
        "project_id": project_id,
        "import_id": import_id,
        "structure_revision_id": structure_revision_id,
        "evidence_revision_id": evidence_revision_id,
        "request_fingerprint": request_fingerprint,
        "effective_status": effective_status,
        "issues": _normalized_review_issues(
            review_issues,
            project_id=project_id,
            import_id=import_id,
            structure_revision_id=structure_revision_id,
            evidence_revision_id=evidence_revision_id,
        ),
    }
    bundle["review_issues_payload_sha256"] = _review_issues_payload_sha256(
        bundle
    )
    return bundle


def _require_review_issues_bundle(
    bundle: dict[str, Any],
    *,
    project_id: str,
    import_id: str,
    structure_revision_id: str,
    evidence_revision_id: str,
    request_fingerprint: str | None = None,
) -> list[dict[str, Any]]:
    declared = str(bundle.get("review_issues_payload_sha256") or "")
    if (
        bundle.get("project_id") != project_id
        or bundle.get("import_id") != import_id
        or bundle.get("structure_revision_id") != structure_revision_id
        or bundle.get("evidence_revision_id") != evidence_revision_id
        or bundle.get("effective_status") not in _STRUCTURE_STATUSES
        or (
            request_fingerprint is not None
            and bundle.get("request_fingerprint") != request_fingerprint
        )
        or not _SHA256_RE.fullmatch(declared)
        or _review_issues_payload_sha256(bundle) != declared
    ):
        raise ValueError("review issues payload integrity check failed")
    issues = bundle.get("issues")
    if not isinstance(issues, list):
        raise ValueError("review issues payload is invalid")
    normalized = _normalized_review_issues(
        issues,
        project_id=project_id,
        import_id=import_id,
        structure_revision_id=structure_revision_id,
        evidence_revision_id=evidence_revision_id,
    )
    if normalized != issues:
        raise ValueError("review issues payload is not canonical")
    return issues


def _manual_override_id(override: dict[str, Any]) -> str:
    first = override.get("manual_override_id")
    second = override.get("override_id")
    if first is not None and second is not None and first != second:
        raise ValueError("manual override id aliases disagree")
    return _validate_entity_id(
        str(first or second or ""),
        _MANUAL_OVERRIDE_ID_RE,
        "manual override",
    )


def _normalized_manual_overrides(
    manual_overrides: list[dict[str, Any]],
    *,
    project_id: str,
    import_id: str,
    base_structure_revision_id: str | None,
    base_evidence_revision_id: str | None,
    structure_revision_id: str,
    evidence_revision_id: str,
    first_revision_number: int,
    request_fingerprint: str,
) -> list[dict[str, Any]]:
    if not isinstance(manual_overrides, list) or any(
        not isinstance(item, dict) for item in manual_overrides
    ):
        raise ValueError("manual overrides must be a list of objects")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for offset, original in enumerate(manual_overrides):
        item = dict(original)
        override_id = _manual_override_id(item)
        if override_id in seen:
            raise ValueError("duplicate manual override id")
        seen.add(override_id)
        item.pop("override_id", None)
        item["manual_override_id"] = override_id
        declared_request_fingerprint = item.get("request_fingerprint")
        if (
            declared_request_fingerprint is not None
            and declared_request_fingerprint != request_fingerprint
        ):
            raise ValueError("manual override request fingerprint mismatch")
        item["request_fingerprint"] = request_fingerprint
        expected_revision = first_revision_number + offset
        declared_revision = item.get("manual_override_revision")
        if declared_revision is not None and (
            isinstance(declared_revision, bool)
            or declared_revision != expected_revision
        ):
            raise ValueError("manual override revision sequence mismatch")
        item["manual_override_revision"] = expected_revision
        references = {
            "project_id": project_id,
            "import_id": import_id,
            "base_structure_revision_id": base_structure_revision_id,
            "base_evidence_revision_id": base_evidence_revision_id,
            "structure_revision_id": structure_revision_id,
            "evidence_revision_id": evidence_revision_id,
        }
        for key, expected in references.items():
            if key in item and item.get(key) != expected:
                raise ValueError("manual override revision reference mismatch")
            item[key] = expected
        if not str(item.get("reason") or "").strip():
            raise ValueError("manual override reason is required")
        actor = str(
            item.get("created_by")
            or item.get("actor")
            or item.get("changed_by")
            or ""
        ).strip()
        if not actor:
            raise ValueError("manual override actor is required")
        for alias in ("actor", "changed_by"):
            alias_value = item.get(alias)
            if alias_value is not None and str(alias_value).strip() != actor:
                raise ValueError("manual override actor aliases disagree")
            item.pop(alias, None)
        item["created_by"] = actor
        created_at = str(
            item.get("created_at") or item.get("changed_at") or ""
        ).strip()
        if not created_at:
            raise ValueError("manual override timestamp is required")
        if (
            "changed_at" in item
            and str(item.get("changed_at") or "").strip() != created_at
        ):
            raise ValueError("manual override timestamp aliases disagree")
        item.pop("changed_at", None)
        item["created_at"] = created_at
        changes = item.get("changes")
        if (
            not isinstance(changes, list)
            or not changes
            or any(
                not isinstance(change, dict)
                or "before" not in change
                or "after" not in change
                for change in changes
            )
        ):
            raise ValueError(
                "manual override changes must contain before/after values"
            )
        declared_digest = item.get("override_payload_sha256")
        calculated_digest = _manual_override_payload_sha256(item)
        if declared_digest is not None and declared_digest != calculated_digest:
            raise ValueError("manual override payload digest mismatch")
        item["override_payload_sha256"] = calculated_digest
        result.append(item)
    return result


def _require_manual_override(
    override: dict[str, Any],
    *,
    project_id: str,
    import_id: str,
    expected_revision: int,
) -> str:
    override_id = _manual_override_id(override)
    declared = str(override.get("override_payload_sha256") or "")
    if (
        override.get("project_id") != project_id
        or override.get("import_id") != import_id
        or override.get("manual_override_revision") != expected_revision
        or not _SHA256_RE.fullmatch(declared)
        or _manual_override_payload_sha256(override) != declared
    ):
        raise ValueError("manual override payload integrity check failed")
    return override_id


def _require_locator(
    locator: dict[str, Any],
    *,
    kind: str,
    entity_id: str,
    project_id: str,
    import_id: str,
) -> None:
    declared = str(locator.get("locator_payload_sha256") or "")
    if (
        locator.get("kind") != kind
        or locator.get("entity_id") != entity_id
        or locator.get("project_id") != project_id
        or locator.get("import_id") != import_id
        or not _SHA256_RE.fullmatch(declared)
        or _locator_payload_sha256(locator) != declared
    ):
        raise ValueError(f"{kind} locator integrity check failed")


def _require_structure_state_digest(state: dict[str, Any]) -> str:
    declared_state_digest = str(state.get("state_payload_sha256") or "")
    if (
        not _SHA256_RE.fullmatch(declared_state_digest)
        or structure_state_payload_sha256(state) != declared_state_digest
    ):
        raise ValueError("structure state payload digest mismatch")
    return declared_state_digest


def _load_structure_head_locked(
    state: dict[str, Any],
) -> dict[str, Any]:
    _require_structure_state_digest(state)

    project_id = validate_resource_id(
        str(state.get("project_id") or ""), "project"
    )
    import_id = validate_resource_id(
        str(state.get("import_id") or ""), "import"
    )
    structure_revision_id = validate_resource_id(
        str(state.get("current_structure_revision_id") or ""), "structure"
    )
    evidence_revision_id = validate_resource_id(
        str(state.get("current_evidence_revision_id") or ""), "evidence"
    )
    structure_number = _validated_revision_number(
        state.get("current_structure_revision_number"), "structure state"
    )
    evidence_number = _validated_revision_number(
        state.get("current_evidence_revision_number"), "evidence state"
    )
    if structure_number != evidence_number:
        raise ValueError("structure and evidence heads are not aligned")
    if state.get("effective_status") not in _STRUCTURE_STATUSES:
        raise ValueError("invalid structure state status")

    structure_revision = _read_json(
        _structure_revision_path(project_id, structure_revision_id)
    )
    evidence_revision = _read_json(
        _evidence_revision_path(project_id, evidence_revision_id)
    )
    if structure_revision is None or evidence_revision is None:
        raise ValueError("current structure artifact is missing")
    (
        loaded_structure_id,
        loaded_structure_number,
        structure_source,
        structure_digest,
    ) = _require_structure_revision(
        structure_revision,
        project_id=project_id,
        import_id=import_id,
    )
    (
        loaded_evidence_id,
        loaded_evidence_number,
        evidence_source,
        evidence_digest,
        evidence_entries,
    ) = _require_evidence_revision(
        evidence_revision,
        project_id=project_id,
        import_id=import_id,
    )
    evidence_expected_participants = _expected_participants(evidence_revision)
    if (
        loaded_structure_id != structure_revision_id
        or loaded_evidence_id != evidence_revision_id
        or loaded_structure_number != structure_number
        or loaded_evidence_number != evidence_number
        or structure_source != evidence_source
        or evidence_revision.get("structure_revision_id")
        != structure_revision_id
        or state.get("current_structure_payload_sha256")
        != structure_digest
        or state.get("current_evidence_payload_sha256") != evidence_digest
    ):
        raise ValueError("structure state head reference mismatch")

    frozen = _confirmed_input_descriptor(
        project_id=project_id,
        import_id=import_id,
        workbook_revision_id=str(state.get("workbook_revision_id") or ""),
        snapshot_sha256=str(state.get("snapshot_sha256") or ""),
        mapping_revision_id=str(
            state.get("current_mapping_revision_id") or ""
        ),
        mapping_sha256=str(state.get("current_mapping_sha256") or ""),
    )
    if structure_source != frozen:
        raise ValueError("structure state frozen input mismatch")
    input_fingerprint = confirmed_structure_input_sha256(frozen)
    if state.get("current_input_fingerprint") != input_fingerprint:
        raise ValueError("structure state input fingerprint mismatch")
    for revision in (structure_revision, evidence_revision):
        declared_input = revision.get("input_fingerprint")
        if declared_input is not None and declared_input != input_fingerprint:
            raise ValueError("artifact input fingerprint mismatch")

    issues_bundle = _read_json(
        _review_issues_path(project_id, evidence_revision_id)
    )
    if issues_bundle is None:
        raise ValueError("current review issue revision is missing")
    review_issues = _require_review_issues_bundle(
        issues_bundle,
        project_id=project_id,
        import_id=import_id,
        structure_revision_id=structure_revision_id,
        evidence_revision_id=evidence_revision_id,
    )
    derived_status = _derived_structure_status(
        review_issues,
        evidence_entries,
        evidence_expected_participants,
    )
    if (
        issues_bundle.get("effective_status") != derived_status
        or state.get("effective_status") != derived_status
    ):
        raise ValueError("structure checkpoint status contradicts persisted evidence")
    issue_ids = [_review_issue_id(issue) for issue in review_issues]
    if (
        state.get("current_review_issue_ids") != issue_ids
        or state.get("current_review_issues_payload_sha256")
        != issues_bundle.get("review_issues_payload_sha256")
        or state.get("effective_status")
        != issues_bundle.get("effective_status")
    ):
        raise ValueError("structure state review issue reference mismatch")

    override_ids = state.get("manual_override_ids")
    manual_override_revision = state.get("manual_override_revision")
    if (
        not isinstance(override_ids, list)
        or isinstance(manual_override_revision, bool)
        or not isinstance(manual_override_revision, int)
        or manual_override_revision < 0
        or len(override_ids) != manual_override_revision
    ):
        raise ValueError("manual override state is invalid")
    for override_id_value in override_ids:
        _validate_entity_id(
            str(override_id_value or ""),
            _MANUAL_OVERRIDE_ID_RE,
            "manual override",
        )

    history = state.get("revision_history")
    if not isinstance(history, list) or len(history) != structure_number:
        raise ValueError("structure revision history is invalid")
    for expected_number, entry in enumerate(history, start=1):
        if (
            not isinstance(entry, dict)
            or entry.get("revision_number") != expected_number
        ):
            raise ValueError("structure revision history sequence mismatch")
    head_history = history[-1]
    if (
        head_history.get("structure_revision_id") != structure_revision_id
        or head_history.get("evidence_revision_id") != evidence_revision_id
        or head_history.get("structure_payload_sha256") != structure_digest
        or head_history.get("evidence_payload_sha256") != evidence_digest
        or head_history.get("review_issues_payload_sha256")
        != issues_bundle.get("review_issues_payload_sha256")
        or head_history.get("input_fingerprint") != input_fingerprint
        or head_history.get("manual_override_revision")
        != manual_override_revision
        or head_history.get("effective_status") != derived_status
    ):
        raise ValueError("structure revision history head mismatch")

    mapping_state = _read_json(_mapping_state_path(project_id))
    if mapping_state is None:
        raise ValueError("mapping state referenced by structure is missing")
    _require_mapping_state_integrity(mapping_state)
    mapping_matches = (
        mapping_state.get("project_id") == project_id
        and mapping_state.get("import_id") == import_id
        and mapping_state.get("effective_status") == "GROUP_MAPPING_CONFIRMED"
        and mapping_state.get("current_mapping_revision_id")
        == frozen["mapping_revision_id"]
        and mapping_state.get("current_mapping_sha256")
        == frozen["mapping_sha256"]
        and mapping_state.get("confirmed_mapping_revision_id")
        == frozen["mapping_revision_id"]
        and mapping_state.get("confirmed_mapping_sha256")
        == frozen["mapping_sha256"]
    )
    if mapping_matches:
        mapping_head = _load_confirmed_mapping_head_locked(project_id, import_id)
        if (
            mapping_head["mapping_revision_id"]
            != frozen["mapping_revision_id"]
            or mapping_head["mapping_sha256"] != frozen["mapping_sha256"]
            or _confirmed_mapping_expected_participants(
                mapping_head["mapping_revision"]
            )
            != evidence_expected_participants
        ):
            raise ValueError("current confirmed structure input mismatch")

    public_state = dict(state)
    public_state["is_stale"] = not mapping_matches
    public_state["artifact_status"] = "STALE" if not mapping_matches else "CURRENT"
    return {
        "state": public_state,
        "structure_revision": structure_revision,
        "evidence_revision": evidence_revision,
        "review_issues": review_issues,
        "manual_overrides": [],
    }


def _structure_status_cache_key(project_id: str) -> tuple[str, str]:
    root = os.path.normcase(os.path.abspath(os.fspath(_root())))
    return root, project_id


def _file_validation_signature(path: Path) -> tuple[str, int, int, int, int]:
    stat_result = path.stat()
    return (
        os.path.normcase(os.path.abspath(os.fspath(path))),
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
        stat_result.st_ino,
    )


def _structure_status_dependency_signatures(
    state: dict[str, Any],
) -> tuple[tuple[str, int, int, int, int], ...] | None:
    project_id = validate_resource_id(
        str(state.get("project_id") or ""), "project"
    )
    structure_revision_id = validate_resource_id(
        str(state.get("current_structure_revision_id") or ""), "structure"
    )
    evidence_revision_id = validate_resource_id(
        str(state.get("current_evidence_revision_id") or ""), "evidence"
    )
    mapping_revision_id = validate_resource_id(
        str(state.get("current_mapping_revision_id") or ""), "mapping"
    )
    paths = (
        _structure_revision_path(project_id, structure_revision_id),
        _evidence_revision_path(project_id, evidence_revision_id),
        _review_issues_path(project_id, evidence_revision_id),
        _mapping_state_path(project_id),
        _mapping_revision_path(project_id, mapping_revision_id),
    )
    try:
        return tuple(_file_validation_signature(path) for path in paths)
    except OSError:
        return None


def load_structure_state(project_id: str) -> dict[str, Any] | None:
    """Load the verified head and cache stable status-only validations."""

    project_id = validate_resource_id(project_id, "project")
    state_path = _structure_state_path(project_id)
    cache_key = _structure_status_cache_key(project_id)
    with _STORE_LOCK:
        if not state_path.is_file():
            _STRUCTURE_STATUS_CACHE.pop(cache_key, None)
            return None
        with _mapping_process_lock(project_id):
            state = _read_json(state_path)
            if state is None:
                _STRUCTURE_STATUS_CACHE.pop(cache_key, None)
                return None
            state_digest = _require_structure_state_digest(state)
            signatures = _structure_status_dependency_signatures(state)
            cached = _STRUCTURE_STATUS_CACHE.get(cache_key)
            if (
                signatures is not None
                and cached is not None
                and cached.get("state_payload_sha256") == state_digest
                and cached.get("dependency_signatures") == signatures
            ):
                return deepcopy(cached["public_state"])

            _STRUCTURE_STATUS_CACHE.pop(cache_key, None)
            public_state = _load_structure_head_locked(state)["state"]
            verified_signatures = _structure_status_dependency_signatures(state)
            if verified_signatures is None or verified_signatures != signatures:
                raise ValueError(
                    "structure status dependencies changed during validation"
                )
            _STRUCTURE_STATUS_CACHE[cache_key] = {
                "state_payload_sha256": state_digest,
                "dependency_signatures": verified_signatures,
                "public_state": deepcopy(public_state),
            }
            return deepcopy(public_state)


def load_structure_revision(
    project_id: str, structure_revision_id: str
) -> dict[str, Any] | None:
    project_id = validate_resource_id(project_id, "project")
    structure_revision_id = validate_resource_id(
        structure_revision_id, "structure"
    )
    with _STORE_LOCK:
        if not _safe_child("projects", project_id).is_dir():
            return None
        with _mapping_process_lock(project_id):
            revision = _read_json(
                _structure_revision_path(project_id, structure_revision_id)
            )
            if revision is not None:
                loaded_id, _, _, _ = _require_structure_revision(
                    revision, project_id=project_id
                )
                if loaded_id != structure_revision_id:
                    raise ValueError("structure revision path mismatch")
            return revision


def load_evidence_revision(
    project_id: str, evidence_revision_id: str
) -> dict[str, Any] | None:
    project_id = validate_resource_id(project_id, "project")
    evidence_revision_id = validate_resource_id(
        evidence_revision_id, "evidence"
    )
    with _STORE_LOCK:
        if not _safe_child("projects", project_id).is_dir():
            return None
        with _mapping_process_lock(project_id):
            revision = _read_json(
                _evidence_revision_path(project_id, evidence_revision_id)
            )
            if revision is not None:
                loaded_id, _, _, _, _ = _require_evidence_revision(
                    revision, project_id=project_id
                )
                if loaded_id != evidence_revision_id:
                    raise ValueError("evidence revision path mismatch")
            return revision


def load_current_structure_bundle(
    project_id: str, import_id: str
) -> dict[str, Any] | None:
    """Load current artifacts after the service has completed owner checks."""

    project_id = validate_resource_id(project_id, "project")
    import_id = validate_resource_id(import_id, "import")
    state_path = _structure_state_path(project_id)
    with _STORE_LOCK:
        if not state_path.is_file():
            return None
        with _mapping_process_lock(project_id):
            state = _read_json(state_path)
            if state is None:
                return None
            if state.get("import_id") != import_id:
                raise ValueError("structure state import mismatch")
            bundle = _load_structure_head_locked(state)
            manual_overrides: list[dict[str, Any]] = []
            for expected_revision, override_id_value in enumerate(
                state.get("manual_override_ids") or [], start=1
            ):
                override_id = _validate_entity_id(
                    str(override_id_value or ""),
                    _MANUAL_OVERRIDE_ID_RE,
                    "manual override",
                )
                override = _read_json(
                    _manual_override_path(project_id, override_id)
                )
                if override is None:
                    raise ValueError(
                        "manual override referenced by state is missing"
                    )
                if (
                    _require_manual_override(
                        override,
                        project_id=project_id,
                        import_id=import_id,
                        expected_revision=expected_revision,
                    )
                    != override_id
                ):
                    raise ValueError("manual override id mismatch")
                manual_overrides.append(override)
            bundle["manual_overrides"] = manual_overrides
            return bundle


def _write_or_reuse_structure_revision(
    path: Path,
    incoming: dict[str, Any],
    *,
    request_fingerprint: str,
    project_id: str,
    import_id: str,
) -> dict[str, Any]:
    existing = _read_json(path)
    if existing is None:
        _atomic_write_json(path, incoming)
        return incoming
    _require_structure_revision(
        existing, project_id=project_id, import_id=import_id
    )
    if existing.get("request_fingerprint") != request_fingerprint:
        raise FileExistsError("structure revision identity collision")
    return existing


def _write_or_reuse_evidence_revision(
    path: Path,
    incoming: dict[str, Any],
    *,
    request_fingerprint: str,
    project_id: str,
    import_id: str,
) -> dict[str, Any]:
    existing = _read_json(path)
    if existing is None:
        _atomic_write_json(path, incoming)
        return incoming
    _require_evidence_revision(
        existing, project_id=project_id, import_id=import_id
    )
    if existing.get("request_fingerprint") != request_fingerprint:
        raise FileExistsError("evidence revision identity collision")
    return existing


def _write_or_reuse_review_issues(
    path: Path,
    incoming: dict[str, Any],
    *,
    project_id: str,
    import_id: str,
    structure_revision_id: str,
    evidence_revision_id: str,
    request_fingerprint: str,
) -> dict[str, Any]:
    existing = _read_json(path)
    if existing is None:
        _atomic_write_json(path, incoming)
        return incoming
    _require_review_issues_bundle(
        existing,
        project_id=project_id,
        import_id=import_id,
        structure_revision_id=structure_revision_id,
        evidence_revision_id=evidence_revision_id,
    )
    if existing.get("request_fingerprint") != request_fingerprint:
        raise FileExistsError("review issue revision identity collision")
    return existing


def _write_or_reuse_manual_override(
    path: Path,
    incoming: dict[str, Any],
    *,
    project_id: str,
    import_id: str,
    expected_revision: int,
    request_fingerprint: str,
) -> dict[str, Any]:
    existing = _read_json(path)
    if existing is None:
        _atomic_write_json(path, incoming)
        return incoming
    declared_digest = str(existing.get("override_payload_sha256") or "")
    if (
        not _SHA256_RE.fullmatch(declared_digest)
        or _manual_override_payload_sha256(existing) != declared_digest
    ):
        raise ValueError("manual override payload integrity check failed")
    if existing.get("request_fingerprint") != request_fingerprint:
        raise FileExistsError("manual override identity collision")
    _require_manual_override(
        existing,
        project_id=project_id,
        import_id=import_id,
        expected_revision=expected_revision,
    )
    return existing


def _write_or_reuse_locator(
    path: Path,
    *,
    kind: str,
    entity_id: str,
    project_id: str,
    import_id: str,
) -> dict[str, Any]:
    locator = {
        "schema_version": "interview-safe-locator/1.0",
        "kind": kind,
        "entity_id": entity_id,
        "project_id": project_id,
        "import_id": import_id,
    }
    locator["locator_payload_sha256"] = _locator_payload_sha256(locator)
    existing = _read_json(path)
    if existing is None:
        _atomic_write_json(path, locator)
        return locator
    declared_digest = str(existing.get("locator_payload_sha256") or "")
    if (
        not _SHA256_RE.fullmatch(declared_digest)
        or _locator_payload_sha256(existing) != declared_digest
    ):
        raise ValueError(f"{kind} locator integrity check failed")
    if (
        existing.get("kind") != kind
        or existing.get("entity_id") != entity_id
        or existing.get("project_id") != project_id
        or existing.get("import_id") != import_id
    ):
        raise FileExistsError(f"{kind} locator identity collision")
    return existing


def save_structure_bundle_cas(
    *,
    project_id: str,
    import_id: str,
    base_structure_revision_id: str | None,
    base_evidence_revision_id: str | None,
    structure_revision: dict[str, Any],
    evidence_revision: dict[str, Any],
    review_issues: list[dict[str, Any]],
    manual_overrides: list[dict[str, Any]],
    request_fingerprint: str,
    effective_status: str,
    updated_at: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Atomically advance the structure/evidence heads under mapping CAS.

    Immutable files are published before the mutable state.  If a process is
    interrupted in between, a retry carrying the same request fingerprint and
    deterministic revision IDs reuses those durable payloads.  A different
    request claiming any existing immutable ID fails closed.
    """

    project_id = validate_resource_id(project_id, "project")
    import_id = validate_resource_id(import_id, "import")
    if base_structure_revision_id is not None:
        base_structure_revision_id = validate_resource_id(
            base_structure_revision_id, "structure"
        )
    if base_evidence_revision_id is not None:
        base_evidence_revision_id = validate_resource_id(
            base_evidence_revision_id, "evidence"
        )
    if (base_structure_revision_id is None) != (
        base_evidence_revision_id is None
    ):
        raise ValueError("structure and evidence bases must be supplied together")
    request_fingerprint = str(request_fingerprint or "")
    if not _SHA256_RE.fullmatch(request_fingerprint):
        raise ValueError("invalid structure request fingerprint")
    if effective_status not in _STRUCTURE_STATUSES:
        raise ValueError("invalid structure checkpoint status")
    if not str(updated_at or "").strip():
        raise ValueError("structure checkpoint timestamp is required")

    incoming_structure = dict(structure_revision)
    incoming_evidence = dict(evidence_revision)
    if (
        incoming_structure.get("request_fingerprint") != request_fingerprint
        or incoming_evidence.get("request_fingerprint") != request_fingerprint
    ):
        raise ValueError("revision request fingerprint mismatch")
    (
        structure_revision_id,
        incoming_structure_number,
        incoming_structure_source,
        _,
    ) = _require_structure_revision(
        incoming_structure,
        project_id=project_id,
        import_id=import_id,
    )
    (
        evidence_revision_id,
        incoming_evidence_number,
        incoming_evidence_source,
        _,
        incoming_entries,
    ) = _require_evidence_revision(
        incoming_evidence,
        project_id=project_id,
        import_id=import_id,
    )
    incoming_expected_participants = _expected_participants(incoming_evidence)
    if (
        incoming_structure_source != incoming_evidence_source
        or incoming_evidence.get("structure_revision_id")
        != structure_revision_id
    ):
        raise ValueError("structure and evidence revisions are not aligned")

    incoming_issue_bundle = _review_issues_bundle(
        project_id=project_id,
        import_id=import_id,
        structure_revision_id=structure_revision_id,
        evidence_revision_id=evidence_revision_id,
        review_issues=review_issues,
        request_fingerprint=request_fingerprint,
        effective_status=effective_status,
    )
    incoming_issues = _require_review_issues_bundle(
        incoming_issue_bundle,
        project_id=project_id,
        import_id=import_id,
        structure_revision_id=structure_revision_id,
        evidence_revision_id=evidence_revision_id,
        request_fingerprint=request_fingerprint,
    )
    if (
        _derived_structure_status(
            incoming_issues,
            incoming_entries,
            incoming_expected_participants,
        )
        != effective_status
    ):
        raise ValueError("structure checkpoint status contradicts evidence")

    state_path = _structure_state_path(project_id)
    with _STORE_LOCK:
        with _mapping_process_lock(project_id):
            _require_current_confirmed_mapping_input_locked(
                project_id=project_id,
                import_id=import_id,
                expected_mapping_revision_id=incoming_structure_source[
                    "mapping_revision_id"
                ],
                expected_mapping_sha256=incoming_structure_source[
                    "mapping_sha256"
                ],
            )
            state = _read_json(state_path)
            current_bundle: dict[str, Any] | None = None
            if state is not None:
                current_bundle = _load_structure_head_locked(state)
                durable_state = current_bundle["state"]
                current_structure_id = str(
                    durable_state.get("current_structure_revision_id") or ""
                )
                current_evidence_id = str(
                    durable_state.get("current_evidence_revision_id") or ""
                )
                if durable_state.get("is_stale"):
                    confirmed = _load_confirmed_structure_input_locked(
                        project_id, import_id
                    )
                    if confirmed is None:
                        raise FileNotFoundError(import_id)
                    frozen = confirmed["confirmed_input"]
                    input_fingerprint = confirmed["input_fingerprint"]
                    publish_expected_participants = (
                        _confirmed_mapping_expected_participants(
                            confirmed["mapping_revision"]
                        )
                    )
                else:
                    frozen = _confirmed_input_descriptor(
                        project_id=project_id,
                        import_id=import_id,
                        workbook_revision_id=str(
                            durable_state.get("workbook_revision_id") or ""
                        ),
                        snapshot_sha256=str(
                            durable_state.get("snapshot_sha256") or ""
                        ),
                        mapping_revision_id=str(
                            durable_state.get("current_mapping_revision_id") or ""
                        ),
                        mapping_sha256=str(
                            durable_state.get("current_mapping_sha256") or ""
                        ),
                    )
                    input_fingerprint = str(
                        durable_state.get("current_input_fingerprint") or ""
                    )
                    publish_expected_participants = _expected_participants(
                        current_bundle["evidence_revision"]
                    )
            else:
                confirmed = _load_confirmed_structure_input_locked(
                    project_id, import_id
                )
                if confirmed is None:
                    raise FileNotFoundError(import_id)
                frozen = confirmed["confirmed_input"]
                input_fingerprint = confirmed["input_fingerprint"]
                publish_expected_participants = (
                    _confirmed_mapping_expected_participants(
                        confirmed["mapping_revision"]
                    )
                )
                current_structure_id = None
                current_evidence_id = None

            if incoming_structure_source != frozen:
                raise ValueError("artifact inputs do not match confirmed mapping")
            if incoming_expected_participants != publish_expected_participants:
                raise ValueError(
                    "evidence participant manifest does not match confirmed mapping"
                )
            for revision in (incoming_structure, incoming_evidence):
                declared_input = revision.get("input_fingerprint")
                if (
                    declared_input is not None
                    and declared_input != input_fingerprint
                ):
                    raise ValueError("artifact input fingerprint mismatch")
            if (
                current_bundle is not None
                and not current_bundle["state"].get("is_stale")
                and current_bundle["state"].get("current_request_fingerprint")
                == request_fingerprint
                and current_structure_id == structure_revision_id
                and current_evidence_id == evidence_revision_id
            ):
                return (
                    current_bundle["structure_revision"],
                    current_bundle["evidence_revision"],
                    current_bundle["state"],
                )

            current_evidence_ids = (
                {
                    _entry_evidence_id(item)
                    for item in _evidence_entries(
                        current_bundle["evidence_revision"]
                    )
                }
                if current_bundle is not None
                else set()
            )
            current_issue_ids = (
                {
                    _review_issue_id(item)
                    for item in current_bundle["review_issues"]
                }
                if current_bundle is not None
                else set()
            )

            if (
                current_structure_id != base_structure_revision_id
                or current_evidence_id != base_evidence_revision_id
            ):
                raise FileExistsError("structure revision conflict")
            current_number = int(
                (state or {}).get("current_structure_revision_number") or 0
            )
            expected_number = current_number + 1
            if (
                incoming_structure_number != expected_number
                or incoming_evidence_number != expected_number
            ):
                raise ValueError("invalid structure/evidence revision sequence")

            current_override_revision = int(
                (state or {}).get("manual_override_revision") or 0
            )
            normalized_overrides = _normalized_manual_overrides(
                manual_overrides,
                project_id=project_id,
                import_id=import_id,
                base_structure_revision_id=base_structure_revision_id,
                base_evidence_revision_id=base_evidence_revision_id,
                structure_revision_id=structure_revision_id,
                evidence_revision_id=evidence_revision_id,
                first_revision_number=current_override_revision + 1,
                request_fingerprint=request_fingerprint,
            )

            durable_structure = _write_or_reuse_structure_revision(
                _structure_revision_path(project_id, structure_revision_id),
                incoming_structure,
                request_fingerprint=request_fingerprint,
                project_id=project_id,
                import_id=import_id,
            )
            (
                _,
                durable_structure_number,
                durable_structure_source,
                durable_structure_digest,
            ) = _require_structure_revision(
                durable_structure,
                project_id=project_id,
                import_id=import_id,
            )
            durable_evidence = _write_or_reuse_evidence_revision(
                _evidence_revision_path(project_id, evidence_revision_id),
                incoming_evidence,
                request_fingerprint=request_fingerprint,
                project_id=project_id,
                import_id=import_id,
            )
            (
                _,
                durable_evidence_number,
                durable_evidence_source,
                durable_evidence_digest,
                durable_entries,
            ) = _require_evidence_revision(
                durable_evidence,
                project_id=project_id,
                import_id=import_id,
            )
            durable_expected_participants = _expected_participants(
                durable_evidence
            )
            if (
                durable_structure_number != expected_number
                or durable_evidence_number != expected_number
                or durable_structure_source != frozen
                or durable_evidence_source != frozen
                or durable_evidence.get("structure_revision_id")
                != structure_revision_id
                or durable_expected_participants
                != publish_expected_participants
            ):
                raise FileExistsError("durable artifact identity collision")

            durable_issue_bundle = _write_or_reuse_review_issues(
                _review_issues_path(project_id, evidence_revision_id),
                incoming_issue_bundle,
                project_id=project_id,
                import_id=import_id,
                structure_revision_id=structure_revision_id,
                evidence_revision_id=evidence_revision_id,
                request_fingerprint=request_fingerprint,
            )
            durable_issues = _require_review_issues_bundle(
                durable_issue_bundle,
                project_id=project_id,
                import_id=import_id,
                structure_revision_id=structure_revision_id,
                evidence_revision_id=evidence_revision_id,
                request_fingerprint=request_fingerprint,
            )
            durable_effective_status = str(
                durable_issue_bundle.get("effective_status") or ""
            )
            if (
                _derived_structure_status(
                    durable_issues,
                    durable_entries,
                    durable_expected_participants,
                )
                != durable_effective_status
            ):
                raise ValueError(
                    "durable structure checkpoint status contradicts evidence"
                )

            durable_new_overrides: list[dict[str, Any]] = []
            for offset, override in enumerate(normalized_overrides):
                override_id = _manual_override_id(override)
                durable_override = _write_or_reuse_manual_override(
                    _manual_override_path(project_id, override_id),
                    override,
                    project_id=project_id,
                    import_id=import_id,
                    expected_revision=current_override_revision + offset + 1,
                    request_fingerprint=request_fingerprint,
                )
                durable_new_overrides.append(durable_override)

            for entry in durable_entries:
                evidence_id = _entry_evidence_id(entry)
                if evidence_id in current_evidence_ids:
                    continue
                _write_or_reuse_locator(
                    _evidence_locator_path(evidence_id),
                    kind="evidence",
                    entity_id=evidence_id,
                    project_id=project_id,
                    import_id=import_id,
                )
            durable_issue_ids = [
                _review_issue_id(issue) for issue in durable_issues
            ]
            for issue_id in durable_issue_ids:
                if issue_id in current_issue_ids:
                    continue
                _write_or_reuse_locator(
                    _review_issue_locator_path(issue_id),
                    kind="review_issue",
                    entity_id=issue_id,
                    project_id=project_id,
                    import_id=import_id,
                )

            override_ids = list((state or {}).get("manual_override_ids") or [])
            override_ids.extend(
                _manual_override_id(item) for item in durable_new_overrides
            )
            manual_override_revision = len(override_ids)
            history = list((state or {}).get("revision_history") or [])
            history_entry = {
                "revision_number": expected_number,
                "structure_revision_id": structure_revision_id,
                "evidence_revision_id": evidence_revision_id,
                "structure_payload_sha256": durable_structure_digest,
                "evidence_payload_sha256": durable_evidence_digest,
                "review_issues_payload_sha256": durable_issue_bundle[
                    "review_issues_payload_sha256"
                ],
                "input_fingerprint": input_fingerprint,
                "request_fingerprint": request_fingerprint,
                "manual_override_revision": manual_override_revision,
                "effective_status": durable_effective_status,
                "created_at": str(
                    durable_structure.get("created_at")
                    or durable_evidence.get("created_at")
                    or updated_at
                ),
            }
            history.append(history_entry)
            next_state = {
                "schema_version": "interview-structure-state/1.0",
                **frozen,
                "current_mapping_revision_id": frozen["mapping_revision_id"],
                "current_mapping_sha256": frozen["mapping_sha256"],
                "current_structure_revision_number": expected_number,
                "current_structure_revision_id": structure_revision_id,
                "current_structure_payload_sha256": durable_structure_digest,
                "current_evidence_revision_number": expected_number,
                "current_evidence_revision_id": evidence_revision_id,
                "current_evidence_payload_sha256": durable_evidence_digest,
                "current_review_issue_ids": durable_issue_ids,
                "current_review_issues_payload_sha256": durable_issue_bundle[
                    "review_issues_payload_sha256"
                ],
                "manual_override_revision": manual_override_revision,
                "manual_override_ids": override_ids,
                "current_input_fingerprint": input_fingerprint,
                "current_request_fingerprint": request_fingerprint,
                "effective_status": durable_effective_status,
                "revision_history": history,
                "updated_at": updated_at,
            }
            next_state["state_payload_sha256"] = structure_state_payload_sha256(
                next_state
            )
            _atomic_write_json(state_path, next_state)
            verified = _load_structure_head_locked(next_state)
            return durable_structure, durable_evidence, verified["state"]


def _safe_locator_result(locator: dict[str, Any]) -> dict[str, Any]:
    """Return ownership IDs only, without opening any current artifact."""

    return {
        "kind": locator.get("kind"),
        "entity_id": locator.get("entity_id"),
        "project_id": locator.get("project_id"),
        "import_id": locator.get("import_id"),
    }


def locate_evidence(evidence_id: str) -> dict[str, Any] | None:
    """Locate ownership IDs only; this function never returns evidence text."""

    evidence_id = _validate_entity_id(
        evidence_id, _EVIDENCE_ID_RE, "evidence"
    )
    with _STORE_LOCK:
        locator = _read_json(_evidence_locator_path(evidence_id))
        if locator is None:
            return None
        project_id = validate_resource_id(
            str(locator.get("project_id") or ""), "project"
        )
        import_id = validate_resource_id(
            str(locator.get("import_id") or ""), "import"
        )
        _require_locator(
            locator,
            kind="evidence",
            entity_id=evidence_id,
            project_id=project_id,
            import_id=import_id,
        )
        return _safe_locator_result(locator)


def locate_review_issue(issue_id: str) -> dict[str, Any] | None:
    """Locate ownership IDs only; this function never returns issue context."""

    issue_id = _validate_entity_id(
        issue_id, _REVIEW_ISSUE_ID_RE, "review issue"
    )
    with _STORE_LOCK:
        locator = _read_json(_review_issue_locator_path(issue_id))
        if locator is None:
            return None
        project_id = validate_resource_id(
            str(locator.get("project_id") or ""), "project"
        )
        import_id = validate_resource_id(
            str(locator.get("import_id") or ""), "import"
        )
        _require_locator(
            locator,
            kind="review_issue",
            entity_id=issue_id,
            project_id=project_id,
            import_id=import_id,
        )
        return _safe_locator_result(locator)


def load_review_issues(
    project_id: str, import_id: str
) -> list[dict[str, Any]] | None:
    """Load current issue content after the service has verified ownership."""

    bundle = load_current_structure_bundle(project_id, import_id)
    if bundle is None:
        return None
    return bundle["review_issues"]


def load_review_issue(
    project_id: str,
    import_id: str,
    evidence_revision_id: str,
    issue_id: str,
) -> dict[str, Any] | None:
    """Load one current issue after a preceding safe owner lookup."""

    project_id = validate_resource_id(project_id, "project")
    import_id = validate_resource_id(import_id, "import")
    evidence_revision_id = validate_resource_id(
        evidence_revision_id, "evidence"
    )
    issue_id = _validate_entity_id(
        issue_id, _REVIEW_ISSUE_ID_RE, "review issue"
    )
    bundle = load_current_structure_bundle(project_id, import_id)
    if bundle is None:
        return None
    if (
        bundle["state"].get("current_evidence_revision_id")
        != evidence_revision_id
    ):
        raise FileExistsError("evidence revision is no longer current")
    matches = [
        issue
        for issue in bundle["review_issues"]
        if _review_issue_id(issue) == issue_id
    ]
    if len(matches) > 1:
        raise ValueError("duplicate review issue in current revision")
    return matches[0] if matches else None


def load_evidence_with_context(
    project_id: str,
    import_id: str,
    evidence_revision_id: str,
    evidence_id: str,
) -> dict[str, Any] | None:
    """Load trusted context inputs after a preceding safe owner lookup.

    The returned physical snapshot is for service-side, explicit allow-list
    assembly only.  It must never be forwarded directly to an API response:
    neighboring participant columns can contain unrelated private answers.
    """

    project_id = validate_resource_id(project_id, "project")
    import_id = validate_resource_id(import_id, "import")
    evidence_revision_id = validate_resource_id(
        evidence_revision_id, "evidence"
    )
    evidence_id = _validate_entity_id(
        evidence_id, _EVIDENCE_ID_RE, "evidence"
    )
    state_path = _structure_state_path(project_id)
    with _STORE_LOCK:
        if not state_path.is_file():
            return None
        with _mapping_process_lock(project_id):
            state = _read_json(state_path)
            if state is None:
                return None
            if state.get("import_id") != import_id:
                raise ValueError("structure state import mismatch")
            bundle = _load_structure_head_locked(state)
            if (
                bundle["state"].get("current_evidence_revision_id")
                != evidence_revision_id
            ):
                raise FileExistsError("evidence revision is no longer current")
            entries = _evidence_entries(bundle["evidence_revision"])
            matches = [
                entry
                for entry in entries
                if _entry_evidence_id(entry) == evidence_id
            ]
            if len(matches) > 1:
                raise ValueError("duplicate evidence in current revision")
            if not matches:
                return None
            accepted = _load_accepted_bundle_for_project_locked(
                project_id, import_id
            )
            if accepted is None:
                raise ValueError("evidence source snapshot is missing")
            if (
                accepted["physical_snapshot"].get("snapshot_sha256")
                != bundle["state"].get("snapshot_sha256")
            ):
                raise ValueError("evidence source snapshot reference mismatch")
            return {
                "project_id": project_id,
                "import_id": import_id,
                "workbook_revision_id": bundle["state"].get(
                    "workbook_revision_id"
                ),
                "structure_revision_id": bundle["state"].get(
                    "current_structure_revision_id"
                ),
                "evidence_revision_id": evidence_revision_id,
                "evidence": matches[0],
                "structure_revision": bundle["structure_revision"],
                "physical_snapshot": accepted["physical_snapshot"],
                "artifact_status": bundle["state"].get("artifact_status"),
                "is_stale": bundle["state"].get("is_stale"),
            }


# ---------------------------------------------------------------------------
# Batch 3B: immutable analysis-boundary / coverage revision pairs.
#
# Boundary persistence deliberately reuses the project-wide mapping lock.  It
# therefore cannot publish against a structure/evidence head while that head
# is being advanced in another process.  Proposal generation remains a pure
# service/core concern and never calls these writers.


_ANALYSIS_BOUNDARY_STATUSES = {
    "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
    "READY_FOR_DOSSIERS",
}


def analysis_boundary_revision_payload_sha256(
    revision: dict[str, Any],
) -> str:
    """Digest every immutable boundary revision field except its digest."""

    return _canonical_payload_sha256(
        {
            key: value
            for key, value in revision.items()
            if key != "revision_payload_sha256"
        }
    )


def coverage_revision_payload_sha256(revision: dict[str, Any]) -> str:
    """Digest every immutable coverage revision field except its digest."""

    return _canonical_payload_sha256(
        {
            key: value
            for key, value in revision.items()
            if key != "revision_payload_sha256"
        }
    )


def analysis_boundary_state_payload_sha256(state: dict[str, Any]) -> str:
    """Digest all durable state, excluding only read-time derived fields."""

    return _canonical_payload_sha256(
        {
            key: value
            for key, value in state.items()
            if key
            not in {
                "state_payload_sha256",
                "is_stale",
                "artifact_status",
                "derived_status",
            }
        }
    )


def _analysis_boundary_state_path(project_id: str) -> Path:
    project_id = validate_resource_id(project_id, "project")
    return _safe_child(
        "projects", project_id, "analysis_boundary_state.json"
    )


def _analysis_boundary_revision_path(
    project_id: str, boundary_revision_id: str
) -> Path:
    project_id = validate_resource_id(project_id, "project")
    boundary_revision_id = validate_resource_id(
        boundary_revision_id, "boundary"
    )
    return _safe_child(
        "projects",
        project_id,
        "analysis_boundary_revisions",
        f"{boundary_revision_id}.json",
    )


def _coverage_revision_path(
    project_id: str, coverage_revision_id: str
) -> Path:
    project_id = validate_resource_id(project_id, "project")
    coverage_revision_id = validate_resource_id(
        coverage_revision_id, "coverage"
    )
    return _safe_child(
        "projects",
        project_id,
        "coverage_revisions",
        f"{coverage_revision_id}.json",
    )


def _analysis_boundary_source(revision: dict[str, Any]) -> dict[str, str]:
    source = revision.get("source")
    if not isinstance(source, dict):
        raise ValueError("analysis boundary source must be an object")
    normalized = {
        "structure_revision_id": validate_resource_id(
            str(source.get("structure_revision_id") or ""), "structure"
        ),
        "structure_payload_sha256": str(
            source.get("structure_payload_sha256") or ""
        ),
        "evidence_revision_id": validate_resource_id(
            str(source.get("evidence_revision_id") or ""), "evidence"
        ),
        "evidence_payload_sha256": str(
            source.get("evidence_payload_sha256") or ""
        ),
    }
    for field in (
        "structure_payload_sha256",
        "evidence_payload_sha256",
    ):
        if not _SHA256_RE.fullmatch(normalized[field]):
            raise ValueError(f"invalid analysis boundary {field}")
    if source != normalized:
        raise ValueError("analysis boundary source is not canonical")
    return normalized


def _require_analysis_boundary_revision(
    revision: dict[str, Any],
    *,
    project_id: str | None = None,
    import_id: str | None = None,
) -> tuple[str, int, dict[str, str], str]:
    if not isinstance(revision, dict):
        raise ValueError("analysis boundary revision must be an object")
    revision_project_id = validate_resource_id(
        str(revision.get("project_id") or ""), "project"
    )
    revision_import_id = validate_resource_id(
        str(revision.get("import_id") or ""), "import"
    )
    if project_id is not None and revision_project_id != project_id:
        raise ValueError("analysis boundary project mismatch")
    if import_id is not None and revision_import_id != import_id:
        raise ValueError("analysis boundary import mismatch")
    revision_id = validate_resource_id(
        str(revision.get("boundary_revision_id") or ""), "boundary"
    )
    revision_number = _validated_revision_number(
        revision.get("revision_number"), "analysis boundary"
    )
    request_fingerprint = str(revision.get("request_fingerprint") or "")
    if not _SHA256_RE.fullmatch(request_fingerprint):
        raise ValueError("invalid analysis boundary request fingerprint")
    if not isinstance(revision.get("analysis_boundary"), dict):
        raise ValueError("analysis boundary payload must be an object")
    if not str(revision.get("created_at") or "").strip():
        raise ValueError("analysis boundary timestamp is required")
    source = _analysis_boundary_source(revision)
    declared = str(revision.get("revision_payload_sha256") or "")
    if (
        not _SHA256_RE.fullmatch(declared)
        or analysis_boundary_revision_payload_sha256(revision) != declared
    ):
        raise ValueError("analysis boundary revision payload digest mismatch")
    return revision_id, revision_number, source, declared


def _require_coverage_revision(
    revision: dict[str, Any],
    *,
    project_id: str | None = None,
    import_id: str | None = None,
) -> tuple[str, int, dict[str, str], str, str, str]:
    if not isinstance(revision, dict):
        raise ValueError("coverage revision must be an object")
    revision_project_id = validate_resource_id(
        str(revision.get("project_id") or ""), "project"
    )
    revision_import_id = validate_resource_id(
        str(revision.get("import_id") or ""), "import"
    )
    if project_id is not None and revision_project_id != project_id:
        raise ValueError("coverage project mismatch")
    if import_id is not None and revision_import_id != import_id:
        raise ValueError("coverage import mismatch")
    revision_id = validate_resource_id(
        str(revision.get("coverage_revision_id") or ""), "coverage"
    )
    boundary_revision_id = validate_resource_id(
        str(revision.get("boundary_revision_id") or ""), "boundary"
    )
    boundary_payload_sha256 = str(
        revision.get("boundary_payload_sha256") or ""
    )
    if not _SHA256_RE.fullmatch(boundary_payload_sha256):
        raise ValueError("invalid coverage boundary digest")
    revision_number = _validated_revision_number(
        revision.get("revision_number"), "coverage"
    )
    request_fingerprint = str(revision.get("request_fingerprint") or "")
    if not _SHA256_RE.fullmatch(request_fingerprint):
        raise ValueError("invalid coverage request fingerprint")
    if not isinstance(revision.get("coverage_preview"), dict):
        raise ValueError("coverage preview must be an object")
    if not str(revision.get("created_at") or "").strip():
        raise ValueError("coverage timestamp is required")
    source = _analysis_boundary_source(revision)
    declared = str(revision.get("revision_payload_sha256") or "")
    if (
        not _SHA256_RE.fullmatch(declared)
        or coverage_revision_payload_sha256(revision) != declared
    ):
        raise ValueError("coverage revision payload digest mismatch")
    return (
        revision_id,
        revision_number,
        source,
        declared,
        boundary_revision_id,
        boundary_payload_sha256,
    )


def _require_analysis_boundary_state_digest(state: dict[str, Any]) -> str:
    declared = str(state.get("state_payload_sha256") or "")
    if (
        not _SHA256_RE.fullmatch(declared)
        or analysis_boundary_state_payload_sha256(state) != declared
    ):
        raise ValueError("analysis boundary state payload digest mismatch")
    return declared


def _current_analysis_boundary_source_locked(
    project_id: str, import_id: str
) -> dict[str, Any]:
    state = _read_json(_structure_state_path(project_id))
    if state is None:
        return {
            "source": None,
            "status": "STRUCTURE_REQUIRED",
            "is_stale": True,
        }
    if state.get("import_id") != import_id:
        raise ValueError("structure state import mismatch")
    bundle = _load_structure_head_locked(state)
    public_state = bundle["state"]
    return {
        "source": {
            "structure_revision_id": str(
                public_state.get("current_structure_revision_id") or ""
            ),
            "structure_payload_sha256": str(
                public_state.get("current_structure_payload_sha256") or ""
            ),
            "evidence_revision_id": str(
                public_state.get("current_evidence_revision_id") or ""
            ),
            "evidence_payload_sha256": str(
                public_state.get("current_evidence_payload_sha256") or ""
            ),
        },
        "status": str(public_state.get("effective_status") or ""),
        "is_stale": bool(public_state.get("is_stale")),
    }


def _raise_analysis_boundary_input_conflict(
    current: dict[str, Any],
) -> None:
    source = current.get("source")
    if not isinstance(source, dict):
        source = {}
    raise AnalysisBoundaryInputConflictError(
        current_structure_revision_id=(
            str(source.get("structure_revision_id") or "") or None
        ),
        current_evidence_revision_id=(
            str(source.get("evidence_revision_id") or "") or None
        ),
        current_structure_status=str(current.get("status") or ""),
    )


def _require_current_analysis_boundary_input_locked(
    *,
    project_id: str,
    import_id: str,
    expected_source: dict[str, str],
) -> dict[str, Any]:
    current = _current_analysis_boundary_source_locked(project_id, import_id)
    if (
        current.get("source") != expected_source
        or current.get("status") != "READY_FOR_DOSSIERS"
        or current.get("is_stale")
    ):
        _raise_analysis_boundary_input_conflict(current)
    return current


def _durable_analysis_boundary_source(state: dict[str, Any]) -> dict[str, str]:
    return {
        "structure_revision_id": validate_resource_id(
            str(state.get("current_structure_revision_id") or ""),
            "structure",
        ),
        "structure_payload_sha256": str(
            state.get("current_structure_payload_sha256") or ""
        ),
        "evidence_revision_id": validate_resource_id(
            str(state.get("current_evidence_revision_id") or ""),
            "evidence",
        ),
        "evidence_payload_sha256": str(
            state.get("current_evidence_payload_sha256") or ""
        ),
    }


def _load_analysis_boundary_head_locked(
    state: dict[str, Any],
) -> dict[str, Any]:
    _require_analysis_boundary_state_digest(state)
    project_id = validate_resource_id(
        str(state.get("project_id") or ""), "project"
    )
    import_id = validate_resource_id(
        str(state.get("import_id") or ""), "import"
    )
    boundary_revision_id = validate_resource_id(
        str(state.get("current_boundary_revision_id") or ""), "boundary"
    )
    coverage_revision_id = validate_resource_id(
        str(state.get("current_coverage_revision_id") or ""), "coverage"
    )
    boundary_number = _validated_revision_number(
        state.get("current_boundary_revision_number"),
        "analysis boundary state",
    )
    coverage_number = _validated_revision_number(
        state.get("current_coverage_revision_number"), "coverage state"
    )
    if boundary_number != coverage_number:
        raise ValueError("analysis boundary and coverage heads are not aligned")
    effective_status = str(state.get("effective_status") or "")
    if effective_status not in _ANALYSIS_BOUNDARY_STATUSES:
        raise ValueError("invalid analysis boundary state status")

    boundary_revision = _read_json(
        _analysis_boundary_revision_path(project_id, boundary_revision_id)
    )
    coverage_revision = _read_json(
        _coverage_revision_path(project_id, coverage_revision_id)
    )
    if boundary_revision is None or coverage_revision is None:
        raise ValueError("current analysis boundary artifact is missing")
    (
        loaded_boundary_id,
        loaded_boundary_number,
        boundary_source,
        boundary_digest,
    ) = _require_analysis_boundary_revision(
        boundary_revision, project_id=project_id, import_id=import_id
    )
    (
        loaded_coverage_id,
        loaded_coverage_number,
        coverage_source,
        coverage_digest,
        coverage_boundary_id,
        coverage_boundary_digest,
    ) = _require_coverage_revision(
        coverage_revision, project_id=project_id, import_id=import_id
    )
    durable_source = _durable_analysis_boundary_source(state)
    for digest in (
        durable_source["structure_payload_sha256"],
        durable_source["evidence_payload_sha256"],
    ):
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError("invalid analysis boundary state source digest")
    if (
        loaded_boundary_id != boundary_revision_id
        or loaded_coverage_id != coverage_revision_id
        or loaded_boundary_number != boundary_number
        or loaded_coverage_number != coverage_number
        or boundary_source != coverage_source
        or boundary_source != durable_source
        or coverage_boundary_id != boundary_revision_id
        or coverage_boundary_digest != boundary_digest
        or state.get("current_boundary_payload_sha256") != boundary_digest
        or state.get("current_coverage_payload_sha256") != coverage_digest
    ):
        raise ValueError("analysis boundary state head reference mismatch")

    history = state.get("revision_history")
    if not isinstance(history, list) or len(history) != boundary_number:
        raise ValueError("analysis boundary revision history is invalid")
    for expected_number, entry in enumerate(history, start=1):
        if (
            not isinstance(entry, dict)
            or entry.get("revision_number") != expected_number
        ):
            raise ValueError("analysis boundary history sequence mismatch")
    head_history = history[-1]
    if (
        head_history.get("boundary_revision_id") != boundary_revision_id
        or head_history.get("coverage_revision_id") != coverage_revision_id
        or head_history.get("boundary_payload_sha256") != boundary_digest
        or head_history.get("coverage_payload_sha256") != coverage_digest
        or head_history.get("source") != durable_source
        or head_history.get("request_fingerprint")
        != state.get("current_request_fingerprint")
    ):
        raise ValueError("analysis boundary history head mismatch")
    events = state.get("confirmation_events")
    if not isinstance(events, list):
        raise ValueError("analysis boundary confirmation history is invalid")
    confirmed_values = (
        state.get("confirmed_boundary_revision_id"),
        state.get("confirmed_boundary_payload_sha256"),
        state.get("confirmed_coverage_revision_id"),
        state.get("confirmed_coverage_payload_sha256"),
        state.get("confirmed_revision_number"),
    )
    if effective_status == "READY_FOR_DOSSIERS":
        if confirmed_values != (
            boundary_revision_id,
            boundary_digest,
            coverage_revision_id,
            coverage_digest,
            boundary_number,
        ):
            raise ValueError("analysis boundary confirmation head mismatch")
        if not any(
            isinstance(event, dict)
            and event.get("boundary_revision_id") == boundary_revision_id
            and event.get("coverage_revision_id") == coverage_revision_id
            and event.get("boundary_payload_sha256") == boundary_digest
            and event.get("coverage_payload_sha256") == coverage_digest
            for event in events
        ):
            raise ValueError("analysis boundary confirmation event is missing")
    elif any(value is not None for value in confirmed_values):
        raise ValueError("unconfirmed analysis boundary has confirmed head")

    current = _current_analysis_boundary_source_locked(project_id, import_id)
    is_stale = not (
        current.get("source") == durable_source
        and current.get("status") == "READY_FOR_DOSSIERS"
        and not current.get("is_stale")
    )
    public_state = dict(state)
    public_state["is_stale"] = is_stale
    public_state["artifact_status"] = "STALE" if is_stale else "CURRENT"
    public_state["derived_status"] = (
        "ANALYSIS_BOUNDARY_REQUIRED" if is_stale else effective_status
    )
    return {
        "state": public_state,
        "boundary_revision": boundary_revision,
        "coverage_revision": coverage_revision,
    }


def load_analysis_boundary_state(project_id: str) -> dict[str, Any] | None:
    """Load the verified boundary head with read-time staleness fields."""

    project_id = validate_resource_id(project_id, "project")
    state_path = _analysis_boundary_state_path(project_id)
    with _STORE_LOCK:
        if not state_path.is_file():
            return None
        with _mapping_process_lock(project_id):
            state = _read_json(state_path)
            if state is None:
                return None
            return _load_analysis_boundary_head_locked(state)["state"]


def load_analysis_boundary_revision(
    project_id: str, boundary_revision_id: str
) -> dict[str, Any] | None:
    project_id = validate_resource_id(project_id, "project")
    boundary_revision_id = validate_resource_id(
        boundary_revision_id, "boundary"
    )
    with _STORE_LOCK:
        if not _safe_child("projects", project_id).is_dir():
            return None
        with _mapping_process_lock(project_id):
            revision = _read_json(
                _analysis_boundary_revision_path(
                    project_id, boundary_revision_id
                )
            )
            if revision is not None:
                loaded_id, _, _, _ = _require_analysis_boundary_revision(
                    revision, project_id=project_id
                )
                if loaded_id != boundary_revision_id:
                    raise ValueError("analysis boundary revision path mismatch")
            return revision


def load_coverage_revision(
    project_id: str, coverage_revision_id: str
) -> dict[str, Any] | None:
    project_id = validate_resource_id(project_id, "project")
    coverage_revision_id = validate_resource_id(
        coverage_revision_id, "coverage"
    )
    with _STORE_LOCK:
        if not _safe_child("projects", project_id).is_dir():
            return None
        with _mapping_process_lock(project_id):
            revision = _read_json(
                _coverage_revision_path(project_id, coverage_revision_id)
            )
            if revision is not None:
                loaded_id, _, _, _, _, _ = _require_coverage_revision(
                    revision, project_id=project_id
                )
                if loaded_id != coverage_revision_id:
                    raise ValueError("coverage revision path mismatch")
            return revision


def load_current_analysis_boundary_bundle(
    project_id: str, import_id: str
) -> dict[str, Any] | None:
    """Load the current pair after the service has checked ownership."""

    project_id = validate_resource_id(project_id, "project")
    import_id = validate_resource_id(import_id, "import")
    state_path = _analysis_boundary_state_path(project_id)
    with _STORE_LOCK:
        if not state_path.is_file():
            return None
        with _mapping_process_lock(project_id):
            state = _read_json(state_path)
            if state is None:
                return None
            if state.get("import_id") != import_id:
                raise ValueError("analysis boundary state import mismatch")
            return _load_analysis_boundary_head_locked(state)


def _write_or_reuse_analysis_boundary_revision(
    path: Path,
    incoming: dict[str, Any],
    *,
    project_id: str,
    import_id: str,
    request_fingerprint: str,
) -> dict[str, Any]:
    existing = _read_json(path)
    if existing is None:
        _atomic_write_json(path, incoming)
        return incoming
    _require_analysis_boundary_revision(
        existing, project_id=project_id, import_id=import_id
    )
    if existing.get("request_fingerprint") != request_fingerprint:
        raise FileExistsError("analysis boundary revision identity collision")
    return existing


def _write_or_reuse_coverage_revision(
    path: Path,
    incoming: dict[str, Any],
    *,
    project_id: str,
    import_id: str,
    request_fingerprint: str,
) -> dict[str, Any]:
    existing = _read_json(path)
    if existing is None:
        _atomic_write_json(path, incoming)
        return incoming
    _require_coverage_revision(
        existing, project_id=project_id, import_id=import_id
    )
    if existing.get("request_fingerprint") != request_fingerprint:
        raise FileExistsError("coverage revision identity collision")
    return existing


def _require_confirmable_analysis_boundary_pair(
    boundary_revision: dict[str, Any],
    coverage_revision: dict[str, Any],
) -> None:
    boundary = boundary_revision.get("analysis_boundary")
    if not isinstance(boundary, dict) or boundary.get("status") != "confirmed":
        raise ValueError("analysis boundary payload is not confirmed")
    coverage = coverage_revision.get("coverage_preview")
    if not isinstance(coverage, dict):
        raise ValueError("coverage preview must be an object")
    coverage_source = coverage.get("source")
    if not isinstance(coverage_source, dict):
        raise ValueError("coverage source must be an object")
    if coverage_source.get("analysis_boundary_sha256") != (
        _canonical_payload_sha256(boundary)
    ):
        raise ValueError("coverage is not bound to the confirmed boundary")
    rows = coverage.get("rows")
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) for row in rows
    ):
        raise ValueError("coverage rows must be a list of objects")
    if any(row.get("review_status") == "proposed" for row in rows):
        raise ValueError("confirmed coverage still contains proposed rows")


def save_analysis_boundary_bundle_cas(
    *,
    project_id: str,
    import_id: str,
    base_boundary_revision_id: str | None,
    base_coverage_revision_id: str | None,
    boundary_revision: dict[str, Any],
    coverage_revision: dict[str, Any],
    request_fingerprint: str,
    updated_at: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Atomically advance the immutable boundary/coverage revision pair."""

    project_id = validate_resource_id(project_id, "project")
    import_id = validate_resource_id(import_id, "import")
    if base_boundary_revision_id is not None:
        base_boundary_revision_id = validate_resource_id(
            base_boundary_revision_id, "boundary"
        )
    if base_coverage_revision_id is not None:
        base_coverage_revision_id = validate_resource_id(
            base_coverage_revision_id, "coverage"
        )
    if (base_boundary_revision_id is None) != (
        base_coverage_revision_id is None
    ):
        raise ValueError("boundary and coverage bases must be supplied together")
    request_fingerprint = str(request_fingerprint or "")
    if not _SHA256_RE.fullmatch(request_fingerprint):
        raise ValueError("invalid analysis boundary request fingerprint")
    if not str(updated_at or "").strip():
        raise ValueError("analysis boundary checkpoint timestamp is required")

    incoming_boundary = dict(boundary_revision)
    incoming_coverage = dict(coverage_revision)
    if (
        incoming_boundary.get("request_fingerprint") != request_fingerprint
        or incoming_coverage.get("request_fingerprint") != request_fingerprint
    ):
        raise ValueError("analysis boundary request fingerprint mismatch")
    (
        boundary_revision_id,
        incoming_boundary_number,
        boundary_source,
        boundary_digest,
    ) = _require_analysis_boundary_revision(
        incoming_boundary, project_id=project_id, import_id=import_id
    )
    (
        coverage_revision_id,
        incoming_coverage_number,
        coverage_source,
        _,
        coverage_boundary_id,
        coverage_boundary_digest,
    ) = _require_coverage_revision(
        incoming_coverage, project_id=project_id, import_id=import_id
    )
    if (
        boundary_source != coverage_source
        or coverage_boundary_id != boundary_revision_id
        or coverage_boundary_digest != boundary_digest
    ):
        raise ValueError("analysis boundary and coverage revisions are not aligned")

    state_path = _analysis_boundary_state_path(project_id)
    with _STORE_LOCK:
        with _mapping_process_lock(project_id):
            _require_current_analysis_boundary_input_locked(
                project_id=project_id,
                import_id=import_id,
                expected_source=boundary_source,
            )
            state = _read_json(state_path)
            current_bundle: dict[str, Any] | None = None
            if state is not None:
                if state.get("import_id") != import_id:
                    raise ValueError("analysis boundary state import mismatch")
                current_bundle = _load_analysis_boundary_head_locked(state)
                durable_state = current_bundle["state"]
                current_boundary_id = str(
                    durable_state.get("current_boundary_revision_id") or ""
                )
                current_coverage_id = str(
                    durable_state.get("current_coverage_revision_id") or ""
                )
                if (
                    not durable_state.get("is_stale")
                    and durable_state.get("current_request_fingerprint")
                    == request_fingerprint
                    and current_boundary_id == boundary_revision_id
                    and current_coverage_id == coverage_revision_id
                ):
                    return (
                        current_bundle["boundary_revision"],
                        current_bundle["coverage_revision"],
                        durable_state,
                    )
            else:
                current_boundary_id = None
                current_coverage_id = None

            if (
                current_boundary_id != base_boundary_revision_id
                or current_coverage_id != base_coverage_revision_id
            ):
                raise FileExistsError("analysis boundary revision conflict")
            current_number = int(
                (state or {}).get("current_boundary_revision_number") or 0
            )
            expected_number = current_number + 1
            if (
                incoming_boundary_number != expected_number
                or incoming_coverage_number != expected_number
            ):
                raise ValueError("invalid boundary/coverage revision sequence")

            durable_boundary = _write_or_reuse_analysis_boundary_revision(
                _analysis_boundary_revision_path(
                    project_id, boundary_revision_id
                ),
                incoming_boundary,
                project_id=project_id,
                import_id=import_id,
                request_fingerprint=request_fingerprint,
            )
            (
                _,
                durable_boundary_number,
                durable_source,
                durable_boundary_digest,
            ) = _require_analysis_boundary_revision(
                durable_boundary, project_id=project_id, import_id=import_id
            )
            coverage_to_publish = incoming_coverage
            if (
                coverage_to_publish.get("boundary_payload_sha256")
                != durable_boundary_digest
            ):
                coverage_to_publish = dict(coverage_to_publish)
                coverage_to_publish["boundary_payload_sha256"] = (
                    durable_boundary_digest
                )
                coverage_to_publish["revision_payload_sha256"] = (
                    coverage_revision_payload_sha256(coverage_to_publish)
                )
                (
                    rebound_coverage_id,
                    rebound_coverage_number,
                    rebound_coverage_source,
                    _,
                    rebound_boundary_id,
                    rebound_boundary_digest,
                ) = _require_coverage_revision(
                    coverage_to_publish,
                    project_id=project_id,
                    import_id=import_id,
                )
                if (
                    rebound_coverage_id != coverage_revision_id
                    or rebound_coverage_number != incoming_coverage_number
                    or rebound_coverage_source != boundary_source
                    or rebound_boundary_id != boundary_revision_id
                    or rebound_boundary_digest != durable_boundary_digest
                ):
                    raise FileExistsError(
                        "coverage retry could not bind durable boundary"
                    )
            durable_coverage = _write_or_reuse_coverage_revision(
                _coverage_revision_path(project_id, coverage_revision_id),
                coverage_to_publish,
                project_id=project_id,
                import_id=import_id,
                request_fingerprint=request_fingerprint,
            )
            (
                _,
                durable_coverage_number,
                durable_coverage_source,
                durable_coverage_digest,
                durable_coverage_boundary_id,
                durable_coverage_boundary_digest,
            ) = _require_coverage_revision(
                durable_coverage, project_id=project_id, import_id=import_id
            )
            if (
                durable_boundary_number != expected_number
                or durable_coverage_number != expected_number
                or durable_source != boundary_source
                or durable_coverage_source != boundary_source
                or durable_coverage_boundary_id != boundary_revision_id
                or durable_coverage_boundary_digest != durable_boundary_digest
            ):
                raise FileExistsError("durable boundary artifact identity collision")

            history = list((state or {}).get("revision_history") or [])
            history.append(
                {
                    "revision_number": expected_number,
                    "boundary_revision_id": boundary_revision_id,
                    "coverage_revision_id": coverage_revision_id,
                    "boundary_payload_sha256": durable_boundary_digest,
                    "coverage_payload_sha256": durable_coverage_digest,
                    "source": durable_source,
                    "request_fingerprint": request_fingerprint,
                    "created_at": str(
                        durable_boundary.get("created_at")
                        or durable_coverage.get("created_at")
                        or updated_at
                    ),
                }
            )
            next_state = {
                "schema_version": "interview-analysis-boundary-state/1.0",
                "project_id": project_id,
                "import_id": import_id,
                "current_structure_revision_id": durable_source[
                    "structure_revision_id"
                ],
                "current_structure_payload_sha256": durable_source[
                    "structure_payload_sha256"
                ],
                "current_evidence_revision_id": durable_source[
                    "evidence_revision_id"
                ],
                "current_evidence_payload_sha256": durable_source[
                    "evidence_payload_sha256"
                ],
                "current_boundary_revision_number": expected_number,
                "current_boundary_revision_id": boundary_revision_id,
                "current_boundary_payload_sha256": durable_boundary_digest,
                "current_coverage_revision_number": expected_number,
                "current_coverage_revision_id": coverage_revision_id,
                "current_coverage_payload_sha256": durable_coverage_digest,
                "current_request_fingerprint": request_fingerprint,
                "effective_status": "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
                "confirmed_boundary_revision_id": None,
                "confirmed_boundary_payload_sha256": None,
                "confirmed_coverage_revision_id": None,
                "confirmed_coverage_payload_sha256": None,
                "confirmed_revision_number": None,
                "revision_history": history,
                "confirmation_events": list(
                    (state or {}).get("confirmation_events") or []
                ),
                "updated_at": updated_at,
            }
            next_state["state_payload_sha256"] = (
                analysis_boundary_state_payload_sha256(next_state)
            )
            _atomic_write_json(state_path, next_state)
            verified = _load_analysis_boundary_head_locked(next_state)
            return (
                durable_boundary,
                durable_coverage,
                verified["state"],
            )


def confirm_analysis_boundary_cas(
    *,
    project_id: str,
    import_id: str,
    boundary_revision_id: str,
    coverage_revision_id: str,
    boundary_payload_sha256: str,
    coverage_payload_sha256: str,
    confirmed_by: str,
    confirmed_at: str,
) -> dict[str, Any]:
    """Confirm exactly the current, non-stale boundary/coverage head pair."""

    project_id = validate_resource_id(project_id, "project")
    import_id = validate_resource_id(import_id, "import")
    boundary_revision_id = validate_resource_id(
        boundary_revision_id, "boundary"
    )
    coverage_revision_id = validate_resource_id(
        coverage_revision_id, "coverage"
    )
    boundary_payload_sha256 = str(boundary_payload_sha256 or "")
    coverage_payload_sha256 = str(coverage_payload_sha256 or "")
    if not _SHA256_RE.fullmatch(boundary_payload_sha256):
        raise ValueError("invalid boundary confirmation digest")
    if not _SHA256_RE.fullmatch(coverage_payload_sha256):
        raise ValueError("invalid coverage confirmation digest")
    if not str(confirmed_by or "").strip():
        raise ValueError("analysis boundary confirmer is required")
    if not str(confirmed_at or "").strip():
        raise ValueError("analysis boundary confirmation timestamp is required")

    state_path = _analysis_boundary_state_path(project_id)
    with _STORE_LOCK:
        with _mapping_process_lock(project_id):
            state = _read_json(state_path)
            if state is None:
                raise FileNotFoundError("analysis boundary state does not exist")
            if state.get("import_id") != import_id:
                raise ValueError("analysis boundary state import mismatch")
            bundle = _load_analysis_boundary_head_locked(state)
            public_state = bundle["state"]
            _require_confirmable_analysis_boundary_pair(
                bundle["boundary_revision"], bundle["coverage_revision"]
            )
            expected_source = _durable_analysis_boundary_source(state)
            _require_current_analysis_boundary_input_locked(
                project_id=project_id,
                import_id=import_id,
                expected_source=expected_source,
            )
            if (
                public_state.get("current_boundary_revision_id")
                != boundary_revision_id
                or public_state.get("current_coverage_revision_id")
                != coverage_revision_id
                or public_state.get("current_boundary_payload_sha256")
                != boundary_payload_sha256
                or public_state.get("current_coverage_payload_sha256")
                != coverage_payload_sha256
            ):
                raise FileExistsError("analysis boundary revision conflict")
            if (
                public_state.get("effective_status") == "READY_FOR_DOSSIERS"
                and public_state.get("confirmed_boundary_revision_id")
                == boundary_revision_id
                and public_state.get("confirmed_coverage_revision_id")
                == coverage_revision_id
            ):
                return public_state

            events = list(state.get("confirmation_events") or [])
            events.append(
                {
                    "revision_number": int(
                        state.get("current_boundary_revision_number") or 0
                    ),
                    "boundary_revision_id": boundary_revision_id,
                    "boundary_payload_sha256": boundary_payload_sha256,
                    "coverage_revision_id": coverage_revision_id,
                    "coverage_payload_sha256": coverage_payload_sha256,
                    "confirmed_by": confirmed_by,
                    "confirmed_at": confirmed_at,
                }
            )
            next_state = dict(state)
            next_state.update(
                {
                    "effective_status": "READY_FOR_DOSSIERS",
                    "confirmed_boundary_revision_id": boundary_revision_id,
                    "confirmed_boundary_payload_sha256": (
                        boundary_payload_sha256
                    ),
                    "confirmed_coverage_revision_id": coverage_revision_id,
                    "confirmed_coverage_payload_sha256": (
                        coverage_payload_sha256
                    ),
                    "confirmed_revision_number": int(
                        state.get("current_boundary_revision_number") or 0
                    ),
                    "confirmation_events": events,
                    "updated_at": confirmed_at,
                }
            )
            next_state["state_payload_sha256"] = (
                analysis_boundary_state_payload_sha256(next_state)
            )
            _atomic_write_json(state_path, next_state)
            return _load_analysis_boundary_head_locked(next_state)["state"]


# Participant dossier checkpoint -------------------------------------------------

def _dossier_participant_dir(project_id: str, participant_id: str) -> Path:
    project_id = validate_resource_id(project_id, "project")
    participant_id = _validate_entity_id(
        participant_id, _PARTICIPANT_ID_RE, "participant"
    )
    return _safe_child("projects", project_id, "participant_dossiers", participant_id)


def _dossier_digest(revision: dict[str, Any]) -> str:
    payload = {key: value for key, value in revision.items() if key != "revision_payload_sha256"}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_current_participant_dossier(
    project_id: str, participant_id: str
) -> dict[str, Any] | None:
    directory = _dossier_participant_dir(project_id, participant_id)
    with _STORE_LOCK:
        with _mapping_process_lock(project_id):
            state = _read_json(directory / "state.json")
            if state is None:
                return None
            revision_id = validate_resource_id(
                str(state.get("current_dossier_version_id") or ""), "dossier"
            )
            revision = _read_json(directory / "versions" / f"{revision_id}.json")
            if revision is None:
                raise ValueError("current participant dossier revision is missing")
            declared = str(revision.get("revision_payload_sha256") or "")
            if not _SHA256_RE.fullmatch(declared) or _dossier_digest(revision) != declared:
                raise ValueError("participant dossier revision digest mismatch")
            return {"state": state, "revision": revision}


def save_participant_dossier_cas(
    *,
    project_id: str,
    participant_id: str,
    base_dossier_version_id: str | None,
    revision: dict[str, Any],
) -> dict[str, Any]:
    directory = _dossier_participant_dir(project_id, participant_id)
    revision_id = validate_resource_id(
        str(revision.get("dossier_version_id") or ""), "dossier"
    )
    with _STORE_LOCK:
        with _mapping_process_lock(project_id):
            state_path = directory / "state.json"
            current = _read_json(state_path)
            current_id = (
                str(current.get("current_dossier_version_id") or "")
                if current else None
            )
            if current_id != base_dossier_version_id:
                raise ValueError("participant dossier version conflict")
            next_number = int((current or {}).get("current_version_number") or 0) + 1
            durable = deepcopy(revision)
            durable["project_id"] = project_id
            durable["participant_id"] = participant_id
            durable["version_number"] = next_number
            durable["revision_payload_sha256"] = _dossier_digest(durable)
            version_path = directory / "versions" / f"{revision_id}.json"
            if version_path.exists():
                raise ValueError("participant dossier revision already exists")
            _atomic_write_json(version_path, durable)
            history = list((current or {}).get("history") or [])
            history.append({
                "dossier_version_id": revision_id,
                "version_number": next_number,
                "revision_payload_sha256": durable["revision_payload_sha256"],
                "created_at": durable.get("created_at"),
                "status": durable.get("status"),
            })
            next_state = {
                "project_id": project_id,
                "participant_id": participant_id,
                "current_dossier_version_id": revision_id,
                "current_version_number": next_number,
                "status": durable.get("status"),
                "source": deepcopy(durable.get("source") or {}),
                "history": history,
            }
            _atomic_write_json(state_path, next_state)
            return {"state": next_state, "revision": durable}


def review_participant_dossier_cas(
    *,
    project_id: str,
    participant_id: str,
    base_dossier_version_id: str,
    decision: str,
    note: str,
    actor: str,
    reviewed_at: str,
) -> dict[str, Any]:
    current = load_current_participant_dossier(project_id, participant_id)
    if current is None or (
        current["state"].get("current_dossier_version_id") != base_dossier_version_id
    ):
        raise ValueError("participant dossier version conflict")
    revision = deepcopy(current["revision"])
    revision["dossier_version_id"] = f"dossier_{uuid.uuid4().hex}"
    revision.pop("revision_payload_sha256", None)
    revision.pop("version_number", None)
    revision["status"] = "approved" if decision == "approved" else "needs_changes"
    revision["review"] = {
        "decision": decision,
        "note": note,
        "reviewed_by": actor,
        "reviewed_at": reviewed_at,
    }
    revision["created_at"] = reviewed_at
    return save_participant_dossier_cas(
        project_id=project_id,
        participant_id=participant_id,
        base_dossier_version_id=base_dossier_version_id,
        revision=revision,
    )
