"""Bind mapped Google Forms variants and responses into one survey table."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.integrations.google_forms_responses_client import (
    GoogleFormResponsesCapture,
    GoogleResponseAnswer,
)
from app.schemas.questionnaire import CanonicalQuestionType
from app.schemas.questionnaire_families import (
    FamilyCanonicalQuestion,
    FamilyQuestionRole,
    QuestionnaireFamily,
    QuestionnaireFamilyStatus,
    UnifiedAnswerEvidence,
    UnifiedResponseProvenance,
)
from app.services.questionnaire_snapshot_binding import SnapshotDetectedColumn


_ROLE_BY_TYPE = {
    CanonicalQuestionType.SINGLE_CHOICE: "single_choice",
    CanonicalQuestionType.MULTI_CHOICE: "multi_choice",
    CanonicalQuestionType.DROPDOWN: "single_choice",
    CanonicalQuestionType.OPEN_TEXT: "open_text",
    CanonicalQuestionType.SCALE: "scale",
    CanonicalQuestionType.RATING: "scale",
    CanonicalQuestionType.MATRIX_SINGLE: "matrix_single",
    CanonicalQuestionType.MATRIX_MULTI: "matrix_multi",
    CanonicalQuestionType.MATRIX_SCALE: "matrix_scale",
    CanonicalQuestionType.DATE: "open_text",
    CanonicalQuestionType.TIME: "open_text",
    CanonicalQuestionType.FILE_UPLOAD: "ignore",
    CanonicalQuestionType.UNKNOWN: "ignore",
}
_MATRIX_TYPES = {
    CanonicalQuestionType.MATRIX_SINGLE,
    CanonicalQuestionType.MATRIX_MULTI,
    CanonicalQuestionType.MATRIX_SCALE,
}


@dataclass(frozen=True, slots=True)
class QuestionnaireFamilySurveyBinding:
    family: QuestionnaireFamily
    rows: tuple[tuple[str, ...], ...]
    columns_detected: tuple[SnapshotDetectedColumn, ...]
    questionnaire_text: str
    response_fingerprint: str
    response_provenance: tuple[UnifiedResponseProvenance, ...]
    duplicate_response_count: int
    unmatched_answer_count: int
    file_upload_answer_count: int
    blocking_issue_count: int

    @property
    def matched_questions(self) -> int:
        return len(self.family.canonical_questions)

    @property
    def source_type(self) -> str:
        return "google"

    @property
    def questionnaire_filename(self) -> str:
        return f"google-forms-family-{self.family.family_id}.json"

    def session_rows(self) -> list[list[str]]:
        return [list(row) for row in self.rows]

    def session_columns(self) -> list[dict]:
        return [column.to_session_value() for column in self.columns_detected]

    def session_family_ref(self) -> dict:
        return {
            "schema_version": 1,
            "family_id": self.family.family_id,
            "mapping_fingerprint": self.family.mapping_fingerprint,
            "languages": [item.language for item in self.family.variants],
            "variant_count": len(self.family.variants),
            "canonical_question_count": len(self.family.canonical_questions),
            "duplicate_response_count": self.duplicate_response_count,
            "unmatched_answer_count": self.unmatched_answer_count,
            "file_upload_answer_count": self.file_upload_answer_count,
        }

    def session_response_provenance(self) -> list[dict]:
        return [item.model_dump(mode="json") for item in self.response_provenance]


def _question_columns(
    family: QuestionnaireFamily,
) -> tuple[list[str], list[SnapshotDetectedColumn], dict[tuple[str, str | None], int]]:
    headers: list[str] = []
    detected: list[SnapshotDetectedColumn] = []
    indexes: dict[tuple[str, str | None], int] = {}
    for question in family.canonical_questions:
        role = _ROLE_BY_TYPE.get(question.canonical_type, "ignore")
        if question.canonical_type in _MATRIX_TYPES:
            target_indexes: list[int] = []
            for row in question.rows:
                target = len(headers)
                headers.append(f"{question.title} [{row.label}]")
                indexes[(question.canonical_question_key, row.canonical_row_key)] = target
                target_indexes.append(target)
            detected.append(SnapshotDetectedColumn(
                name_zh=question.title,
                role=role,
                column_indexes=tuple(target_indexes),
                source_question_id=question.canonical_question_key,
                options=tuple(item.label for item in question.options),
                rows=tuple(item.label for item in question.rows),
            ))
            continue
        target = len(headers)
        headers.append(question.title)
        indexes[(question.canonical_question_key, None)] = target
        options = tuple(item.label for item in question.options)
        scale_min = scale_max = None
        if role == "scale" and options:
            numeric: list[int] = []
            for value in options:
                try:
                    numeric.append(int(value))
                except ValueError:
                    pass
            if numeric:
                scale_min, scale_max = min(numeric), max(numeric)
        detected.append(SnapshotDetectedColumn(
            name_zh=question.title,
            role=role,
            column_indexes=(target,),
            source_question_id=question.canonical_question_key,
            delimiter=("\n" if role == "multi_choice" else None),
            options=options,
            scale_min=scale_min,
            scale_max=scale_max,
        ))
    language_index = len(headers)
    headers.append("来源语言")
    detected.append(SnapshotDetectedColumn(
        name_zh="来源语言",
        role="profile_dim",
        column_indexes=(language_index,),
        source_question_id="system:source_language",
        options=tuple(item.language for item in family.variants),
    ))
    response_ref_index = len(headers)
    headers.append("Google 回答来源")
    detected.append(SnapshotDetectedColumn(
        name_zh="Google 回答来源",
        role="id",
        column_indexes=(response_ref_index,),
        source_question_id="system:google_response_ref",
    ))
    indexes[("system:source_language", None)] = language_index
    indexes[("system:google_response_ref", None)] = response_ref_index
    return headers, detected, indexes


def _questionnaire_text(family: QuestionnaireFamily) -> str:
    lines = [
        f"问卷家族：{family.title}",
        "语言版本：" + "、".join(item.language for item in family.variants),
    ]
    for question in family.canonical_questions:
        lines.append(
            f"{question.canonical_question_key} [{question.canonical_type.value}] "
            f"{question.title} ({question.role.value})"
        )
        if question.options:
            lines.append("选项：" + " | ".join(item.label for item in question.options))
        if question.rows:
            lines.append("矩阵行：" + " | ".join(item.label for item in question.rows))
    return "\n".join(lines)


def _answer_value(
    question: FamilyCanonicalQuestion,
    mapping,
    answer: GoogleResponseAnswer,
) -> str:
    if answer.file_uploads:
        return ""
    option_labels = {
        item.canonical_option_key: item.label for item in question.options
    }
    provider_to_canonical = {
        item.provider_value: item.canonical_option_key
        for item in mapping.option_mappings
    }
    values: list[str] = []
    for raw in answer.text_values:
        canonical_key = provider_to_canonical.get(raw)
        values.append(option_labels.get(canonical_key, raw))
    return "\n".join(values)


def bind_google_forms_family_responses(
    family: QuestionnaireFamily,
    captures: list[GoogleFormResponsesCapture],
) -> QuestionnaireFamilySurveyBinding:
    if not isinstance(family, QuestionnaireFamily):
        raise TypeError("family 类型无效")
    if family.status != QuestionnaireFamilyStatus.READY:
        raise ValueError("问卷家族映射尚未通过，不能创建分析 session")
    captures_by_form = {item.form_id: item for item in captures}
    if len(captures_by_form) != len(captures):
        raise ValueError("回答 capture 包含重复 Form")
    expected_forms = {item.provider_form_id for item in family.variants}
    if set(captures_by_form) != expected_forms:
        raise ValueError("回答 capture 未完整覆盖问卷家族")

    headers, detected, column_indexes = _question_columns(family)
    question_by_key = {
        item.canonical_question_key: item for item in family.canonical_questions
    }
    provider_lookup: dict[tuple[str, str], tuple[FamilyCanonicalQuestion, object, str | None]] = {}
    for question in family.canonical_questions:
        for mapping in question.variant_mappings:
            if mapping.provider_question_id:
                provider_lookup[(mapping.variant_id, mapping.provider_question_id)] = (
                    question, mapping, None
                )
            for row in mapping.row_mappings:
                provider_lookup[(mapping.variant_id, row.provider_question_id)] = (
                    question, mapping, row.canonical_row_key
                )

    rows: list[tuple[str, ...]] = [tuple(headers)]
    provenance: list[UnifiedResponseProvenance] = []
    seen: set[tuple[str, str]] = set()
    duplicates = 0
    unmatched = 0
    file_uploads = 0
    blocking = 0

    for variant in family.variants:
        capture = captures_by_form[variant.provider_form_id]
        for response in capture.responses:
            identity = (variant.variant_id, response.response_id)
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)
            row = [""] * len(headers)
            row[column_indexes[("system:source_language", None)]] = variant.language
            row[column_indexes[("system:google_response_ref", None)]] = (
                f"{variant.language}|{variant.variant_id}|{response.response_id}"
            )
            evidence: list[UnifiedAnswerEvidence] = []
            for answer in response.answers:
                target = provider_lookup.get((variant.variant_id, answer.question_id))
                if target is None:
                    unmatched += 1
                    blocking += 1
                    continue
                question, mapping, row_key = target
                target_index = column_indexes.get(
                    (question.canonical_question_key, row_key)
                )
                if target_index is None:
                    unmatched += 1
                    blocking += 1
                    continue
                row[target_index] = _answer_value(question, mapping, answer)
                file_ids = [item.file_id for item in answer.file_uploads]
                file_uploads += len(file_ids)
                evidence.append(UnifiedAnswerEvidence(
                    canonical_question_key=question.canonical_question_key,
                    original_question_id=answer.question_id,
                    original_values=list(answer.text_values),
                    file_ids=file_ids,
                ))
            row_index = len(rows)
            rows.append(tuple(row))
            provenance.append(UnifiedResponseProvenance(
                row_index=row_index,
                language=variant.language,
                variant_id=variant.variant_id,
                provider_form_id=variant.provider_form_id,
                response_id=response.response_id,
                create_time=response.create_time,
                last_submitted_time=response.last_submitted_time,
                respondent_email=response.respondent_email,
                answers=evidence,
            ))

    fingerprint_source = {
        "mapping_fingerprint": family.mapping_fingerprint,
        "responses": [
            {
                "variant_id": item.variant_id,
                "response_id": item.response_id,
                "last_submitted_time": item.last_submitted_time,
                "answers": [answer.model_dump(mode="json") for answer in item.answers],
            }
            for item in provenance
        ],
    }
    response_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_source,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return QuestionnaireFamilySurveyBinding(
        family=family,
        rows=tuple(rows),
        columns_detected=tuple(detected),
        questionnaire_text=_questionnaire_text(family),
        response_fingerprint=response_fingerprint,
        response_provenance=tuple(provenance),
        duplicate_response_count=duplicates,
        unmatched_answer_count=unmatched,
        file_upload_answer_count=file_uploads,
        blocking_issue_count=blocking,
    )


__all__ = [
    "QuestionnaireFamilySurveyBinding",
    "bind_google_forms_family_responses",
]
