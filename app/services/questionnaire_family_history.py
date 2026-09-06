"""Fail-closed report-history boundary for Google Forms families."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from app.schemas.questionnaire_family_history import (
    QuestionnaireFamilyHistoryPayload,
    QuestionnaireFamilyHistorySummary,
)


_HISTORY_FIELDS = (
    "questionnaire_family_input_kind",
    "questionnaire_family_ref",
)


class QuestionnaireFamilyHistoryError(ValueError):
    pass


def _validated(source: object) -> QuestionnaireFamilyHistoryPayload | None:
    if not isinstance(source, dict) or not any(field in source for field in _HISTORY_FIELDS):
        return None
    try:
        if source.get("source_type") != "google":
            raise ValueError
        if source.get("questionnaire_used") is not True:
            raise ValueError
        payload = QuestionnaireFamilyHistoryPayload.model_validate({
            field: source[field] for field in _HISTORY_FIELDS
        })
        if source.get("questionnaire_sha256") != (
            payload.questionnaire_family_ref.mapping_fingerprint
        ):
            raise ValueError
        return payload
    except (KeyError, TypeError, ValidationError, ValueError) as error:
        raise QuestionnaireFamilyHistoryError("问卷家族历史来源无效") from error


def family_history_fields(
    source: dict[str, Any] | None,
    *,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    primary = _validated(source)
    if primary is not None:
        return deepcopy(primary.model_dump(mode="json"))
    secondary = _validated(fallback)
    if secondary is None or not isinstance(source, dict):
        return {}
    ref = secondary.questionnaire_family_ref
    if (
        source.get("source_type") != "google"
        or source.get("questionnaire_used") is not True
        or source.get("questionnaire_sha256") != ref.mapping_fingerprint
    ):
        return {}
    return deepcopy(secondary.model_dump(mode="json"))


def family_history_summary(source: dict[str, Any] | None) -> dict[str, Any]:
    try:
        payload = _validated(source)
    except QuestionnaireFamilyHistoryError:
        payload = None
    if payload is None:
        return {"questionnaire_family_summary": None}
    ref = payload.questionnaire_family_ref
    summary = QuestionnaireFamilyHistorySummary(
        languages=ref.languages,
        variant_count=ref.variant_count,
        canonical_question_count=ref.canonical_question_count,
        duplicate_response_count=ref.duplicate_response_count,
        unmatched_answer_count=ref.unmatched_answer_count,
        file_upload_answer_count=ref.file_upload_answer_count,
    )
    return {"questionnaire_family_summary": summary.model_dump(mode="json")}


def has_matching_family_provenance(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> bool:
    try:
        left_payload = _validated(left)
        right_payload = _validated(right)
    except QuestionnaireFamilyHistoryError:
        return False
    if left_payload is None or right_payload is None:
        return left_payload is None and right_payload is None
    return left_payload.model_dump(mode="json") == right_payload.model_dump(mode="json")


__all__ = [
    "QuestionnaireFamilyHistoryError",
    "family_history_fields",
    "family_history_summary",
    "has_matching_family_provenance",
]
