"""问卷快照来源在 session 与 report history 之间的安全边界。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from app.schemas.questionnaire_snapshot_history import (
    QuestionnaireSnapshotHistoryPayload,
    QuestionnaireSnapshotHistorySummary,
)


_HISTORY_FIELDS = (
    "questionnaire_input_kind",
    "questionnaire_snapshot_ref",
    "questionnaire_response_bindings",
)
_INVALID_HISTORY_MESSAGE = "问卷快照历史来源无效"


class QuestionnaireSnapshotHistoryError(ValueError):
    """显式快照来源无法通过历史安全合同。"""


def _has_explicit_snapshot_fields(source: object) -> bool:
    return isinstance(source, dict) and any(
        field in source for field in _HISTORY_FIELDS
    )


def _validated_snapshot_payload(
    source: object,
) -> QuestionnaireSnapshotHistoryPayload | None:
    if not _has_explicit_snapshot_fields(source):
        return None
    if not isinstance(source, dict):
        raise QuestionnaireSnapshotHistoryError(_INVALID_HISTORY_MESSAGE)

    try:
        questionnaire_used = source.get("questionnaire_used")
        source_type = source.get("source_type")
        questionnaire_sha256 = source.get("questionnaire_sha256")
        if questionnaire_used is not True:
            raise ValueError
        if not isinstance(questionnaire_sha256, str):
            raise ValueError

        raw_payload = {field: source[field] for field in _HISTORY_FIELDS}
        payload = QuestionnaireSnapshotHistoryPayload.model_validate(raw_payload)
        snapshot_ref = payload.questionnaire_snapshot_ref
        expected_source_type = {
            "google_forms": "google",
            "bested": "bested",
        }[snapshot_ref.provider]
        if source_type != expected_source_type:
            raise ValueError
        if questionnaire_sha256 != snapshot_ref.package_sha256:
            raise ValueError
        return payload
    except (KeyError, TypeError, ValidationError, ValueError) as exc:
        raise QuestionnaireSnapshotHistoryError(
            _INVALID_HISTORY_MESSAGE
        ) from exc


def snapshot_history_fields(
    source: dict[str, Any] | None,
    *,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回严格三字段副本；显式坏来源抛固定安全错误。"""
    primary = _validated_snapshot_payload(source)
    if primary is not None:
        return deepcopy(primary.model_dump(mode="json"))

    secondary = _validated_snapshot_payload(fallback)
    if secondary is None:
        return {}
    if not isinstance(source, dict):
        return {}
    secondary_ref = secondary.questionnaire_snapshot_ref
    if (
        source.get("questionnaire_used") is not True
        or source.get("source_type")
        != {
            "google_forms": "google",
            "bested": "bested",
        }[secondary_ref.provider]
        or source.get("questionnaire_sha256")
        != secondary_ref.package_sha256
    ):
        return {}
    return deepcopy(secondary.model_dump(mode="json"))


def snapshot_history_summary(source: dict[str, Any] | None) -> dict[str, Any]:
    """为列表生成不含 ID、哈希、绑定或定位信息的 fail-closed 摘要。"""
    try:
        payload = _validated_snapshot_payload(source)
    except QuestionnaireSnapshotHistoryError:
        payload = None
    if payload is None:
        return {"questionnaire_snapshot_summary": None}

    ref = payload.questionnaire_snapshot_ref
    summary = QuestionnaireSnapshotHistorySummary(
        provider=ref.provider,
        question_count=ref.question_count,
        asset_count=ref.asset_count,
    )
    return {
        "questionnaire_snapshot_summary": summary.model_dump(mode="json"),
    }


def has_matching_snapshot_provenance(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> bool:
    """判断两份重复报告指纹是否具有对称且一致的快照来源。"""
    try:
        left_payload = _validated_snapshot_payload(left)
        right_payload = _validated_snapshot_payload(right)
    except QuestionnaireSnapshotHistoryError:
        return False
    if left_payload is None or right_payload is None:
        return left_payload is None and right_payload is None
    return left_payload.model_dump(mode="json") == right_payload.model_dump(
        mode="json"
    )
