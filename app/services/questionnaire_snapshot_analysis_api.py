"""从完整问卷快照创建旧版标准分析 session 的业务门面。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import PurePosixPath

from fastapi import HTTPException
from pydantic import ValidationError

from app.core.security import _owner_from_login
from app.schemas.questionnaire_source_api import (
    QuestionnaireSnapshotAnalysisSessionResponse,
)
from app.services.questionnaire_snapshot_binding import (
    SnapshotSurveyBinding,
    bind_snapshot_to_survey_responses,
)
from app.services.survey_service import handle_survey_upload
from app.storage.research_assets import (
    ResearchAssetStorageError,
    ResearchSnapshotStorage,
    SnapshotPackage,
    SnapshotPackageError,
)


MAX_QUESTIONNAIRE_RESPONSE_UPLOAD_BYTES = 50 * 1024 * 1024
_SUPPORTED_RESPONSE_SUFFIXES = frozenset({".csv", ".xlsx"})


class QuestionnaireSnapshotAnalysisApiError(RuntimeError):
    """创建快照分析会话时可由 HTTP 层安全映射的错误基类。"""


class QuestionnaireSnapshotAnalysisInvalidError(
    QuestionnaireSnapshotAnalysisApiError
):
    """回答文件无效、不受支持或无法与问卷快照确定性匹配。"""


class QuestionnaireSnapshotAnalysisNotFoundError(
    QuestionnaireSnapshotAnalysisApiError
):
    """当前 owner 范围内不存在目标问卷快照。"""


class QuestionnaireSnapshotAnalysisInternalError(
    QuestionnaireSnapshotAnalysisApiError
):
    """快照损坏、注入依赖异常或会话创建失败。"""


def _require_owner(owner_ref: str) -> str:
    if not isinstance(owner_ref, str) or not owner_ref.strip():
        raise ValueError("owner_ref 不能为空")
    return owner_ref.strip()


def _require_snapshot_id(snapshot_id: str) -> str:
    if (
        not isinstance(snapshot_id, str)
        or not snapshot_id.strip()
        or snapshot_id != snapshot_id.strip()
        or len(snapshot_id) > 1024
    ):
        raise QuestionnaireSnapshotAnalysisNotFoundError()
    return snapshot_id


def _require_response_upload(filename: str, content: bytes) -> str:
    if (
        not isinstance(filename, str)
        or not filename.strip()
        or filename != filename.strip()
        or len(filename) > 255
        or PurePosixPath(filename.replace("\\", "/")).name != filename
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        raise QuestionnaireSnapshotAnalysisInvalidError()
    suffix = PurePosixPath(filename).suffix.casefold()
    if suffix not in _SUPPORTED_RESPONSE_SUFFIXES:
        raise QuestionnaireSnapshotAnalysisInvalidError()
    if (
        not isinstance(content, bytes)
        or not content
        or len(content) > MAX_QUESTIONNAIRE_RESPONSE_UPLOAD_BYTES
    ):
        raise QuestionnaireSnapshotAnalysisInvalidError()
    return filename


def _load_owner_snapshot(
    storage: ResearchSnapshotStorage,
    owner_ref: str,
    snapshot_id: str,
) -> SnapshotPackage | None:
    package = storage.load_snapshot_package(owner_ref, snapshot_id)
    if package is not None and not isinstance(package, SnapshotPackage):
        raise QuestionnaireSnapshotAnalysisInternalError()
    if package is not None:
        try:
            loaded_snapshot_id = package.bundle.snapshot.snapshot_id
        except Exception as error:
            raise QuestionnaireSnapshotAnalysisInternalError() from error
        if loaded_snapshot_id != snapshot_id:
            raise QuestionnaireSnapshotAnalysisInternalError()
    return package


def _bind_snapshot(
    package: SnapshotPackage,
    *,
    owner_ref: str,
    filename: str,
    content: bytes,
) -> SnapshotSurveyBinding:
    binding = bind_snapshot_to_survey_responses(
        package,
        owner_ref=owner_ref,
        response_filename=filename,
        response_content=content,
    )
    if not isinstance(binding, SnapshotSurveyBinding):
        raise QuestionnaireSnapshotAnalysisInternalError()
    return binding


def _safe_response(
    result: object,
    *,
    snapshot_id: str,
    filename: str,
) -> QuestionnaireSnapshotAnalysisSessionResponse:
    if not isinstance(result, dict):
        raise QuestionnaireSnapshotAnalysisInternalError()
    if (
        result.get("questionnaire_snapshot_id") != snapshot_id
        or result.get("filename") != filename
        or result.get("questionnaire_used") is not True
    ):
        raise QuestionnaireSnapshotAnalysisInternalError()
    try:
        return QuestionnaireSnapshotAnalysisSessionResponse(
            session_id=result.get("session_id"),
            filename=result.get("filename"),
            total_rows=result.get("total_rows"),
            headers=result.get("headers"),
            preview=result.get("preview"),
            source_type=result.get("source_type"),
            questionnaire_used=result.get("questionnaire_used"),
            matched_questions=result.get("matched_questions"),
            questionnaire_snapshot_id=result.get(
                "questionnaire_snapshot_id"
            ),
        )
    except (ValidationError, TypeError, ValueError) as error:
        raise QuestionnaireSnapshotAnalysisInternalError() from error


@dataclass(frozen=True, slots=True)
class QuestionnaireSnapshotAnalysisApi:
    """加载 owner-scoped 快照并桥接到现有问卷分析会话。"""

    storage: ResearchSnapshotStorage

    def __post_init__(self) -> None:
        if not isinstance(self.storage, ResearchSnapshotStorage):
            raise TypeError("storage 必须实现 ResearchSnapshotStorage")

    async def create_session(
        self,
        owner_ref: str,
        snapshot_id: str,
        filename: str,
        content: bytes,
        login: dict | None,
    ) -> QuestionnaireSnapshotAnalysisSessionResponse:
        owner = _require_owner(owner_ref)
        target_snapshot_id = _require_snapshot_id(snapshot_id)
        response_filename = _require_response_upload(filename, content)

        try:
            login_owner = str(
                _owner_from_login(login)["owner_key"]
            ).strip()
        except Exception as error:
            raise QuestionnaireSnapshotAnalysisInternalError() from error
        if not login_owner or login_owner != owner:
            raise QuestionnaireSnapshotAnalysisInternalError()

        try:
            package = await asyncio.to_thread(
                _load_owner_snapshot,
                self.storage,
                owner,
                target_snapshot_id,
            )
        except QuestionnaireSnapshotAnalysisApiError:
            raise
        except ResearchAssetStorageError as error:
            raise QuestionnaireSnapshotAnalysisInternalError() from error
        except Exception as error:
            raise QuestionnaireSnapshotAnalysisInternalError() from error
        if package is None:
            raise QuestionnaireSnapshotAnalysisNotFoundError()

        try:
            binding = await asyncio.to_thread(
                _bind_snapshot,
                package,
                owner_ref=owner,
                filename=response_filename,
                content=content,
            )
        except QuestionnaireSnapshotAnalysisApiError:
            raise
        except ValueError as error:
            raise QuestionnaireSnapshotAnalysisInvalidError() from error
        except SnapshotPackageError as error:
            raise QuestionnaireSnapshotAnalysisInternalError() from error
        except Exception as error:
            raise QuestionnaireSnapshotAnalysisInternalError() from error

        try:
            result = await handle_survey_upload(
                response_filename,
                content,
                login,
                bound_questionnaire=binding,
            )
        except HTTPException as error:
            if error.status_code in {400, 413, 415, 422}:
                raise QuestionnaireSnapshotAnalysisInvalidError() from error
            raise QuestionnaireSnapshotAnalysisInternalError() from error
        except QuestionnaireSnapshotAnalysisApiError:
            raise
        except Exception as error:
            raise QuestionnaireSnapshotAnalysisInternalError() from error

        return _safe_response(
            result,
            snapshot_id=target_snapshot_id,
            filename=response_filename,
        )
