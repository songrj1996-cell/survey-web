"""Deterministic multi-language questionnaire-family mapping.

LLM translations may supply semantic labels, but they never alter provider
IDs, question types, option/row counts, or one-to-one mapping constraints.
Ambiguous structural matches fail closed as ``needs_review``.
"""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.research_assets import structured_sha256
from app.schemas.questionnaire import (
    CanonicalQuestion,
    CanonicalQuestionType,
    QuestionnaireSnapshot,
)
from app.schemas.questionnaire_families import (
    FamilyCanonicalOption,
    FamilyCanonicalQuestion,
    FamilyCanonicalRow,
    FamilyDiagnosticSeverity,
    FamilyOptionMapping,
    FamilyQuestionRole,
    FamilyRowMapping,
    FamilyVariantQuestionMapping,
    LanguageCode,
    QuestionnaireFamily,
    QuestionnaireFamilyDiagnostic,
    QuestionnaireFamilyStatus,
    QuestionnaireFamilyVariant,
)


_NON_SUBSTANTIVE_TYPES = {
    CanonicalQuestionType.SECTION,
    CanonicalQuestionType.STATIC_TEXT,
}
_METADATA_PATTERNS = (
    re.compile(r"\bdiscord(?:\s*id)?\b", re.IGNORECASE),
    re.compile(r"\be-?mail(?:\s*address)?\b", re.IGNORECASE),
    re.compile(r"\b(?:whatsapp|telegram|line)\s*(?:id|number)?\b", re.IGNORECASE),
    re.compile(r"\bcontact\s*(?:id|detail|information|method)?\b", re.IGNORECASE),
    re.compile(r"(?:联系方式|联系信息|联系账号|邮箱|电邮|电子邮件)", re.IGNORECASE),
)


@dataclass(frozen=True, slots=True)
class FamilyVariantSnapshot:
    language: LanguageCode
    snapshot: QuestionnaireSnapshot


@dataclass(frozen=True, slots=True)
class SemanticQuestionText:
    title: str
    options: tuple[str, ...] = ()
    rows: tuple[str, ...] = ()


SemanticQuestionMap = dict[tuple[str, str], SemanticQuestionText]


def _norm(value: str) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


def questionnaire_family_id(
    owner_ref: str,
    title: str,
    provider_form_ids: list[str],
) -> str:
    return _stable_id(
        "fam",
        owner_ref.strip(),
        title.strip(),
        *sorted(str(value) for value in provider_form_ids),
    )


def questionnaire_family_variant_id(
    family_id: str,
    language: str,
    provider_form_id: str,
) -> str:
    return _stable_id("var", family_id, str(language), provider_form_id)


def _substantive(snapshot: QuestionnaireSnapshot) -> list[CanonicalQuestion]:
    return [
        question
        for question in snapshot.canonical_questions
        if question.canonical_type not in _NON_SUBSTANTIVE_TYPES
    ]


def _is_metadata(question: CanonicalQuestion, semantic_title: str = "") -> bool:
    text = f"{question.title}\n{semantic_title}"
    return any(pattern.search(text) for pattern in _METADATA_PATTERNS)


def _semantic(
    semantic_questions: SemanticQuestionMap,
    variant_id: str,
    question: CanonicalQuestion,
) -> SemanticQuestionText:
    return semantic_questions.get(
        (variant_id, question.question_id),
        SemanticQuestionText(
            title=question.title,
            options=tuple(option.label or option.value for option in question.options),
            rows=tuple(row.label for row in question.rows),
        ),
    )


def _compatible(left: CanonicalQuestion, right: CanonicalQuestion) -> bool:
    return (
        left.canonical_type == right.canonical_type
        and len(left.options) == len(right.options)
        and len(left.rows) == len(right.rows)
    )


def _structural_signature(
    question: CanonicalQuestion,
) -> tuple[CanonicalQuestionType, int, int]:
    return (
        question.canonical_type,
        len(question.options),
        len(question.rows),
    )


def _structural_position_offset(
    base_questions: list[CanonicalQuestion],
    provider_questions: list[CanonicalQuestion],
) -> int | None:
    base_positions: dict[tuple[CanonicalQuestionType, int, int], list[int]] = {}
    provider_positions: dict[tuple[CanonicalQuestionType, int, int], list[int]] = {}
    for index, question in enumerate(base_questions):
        base_positions.setdefault(_structural_signature(question), []).append(index)
    for index, question in enumerate(provider_questions):
        provider_positions.setdefault(
            _structural_signature(question), []
        ).append(index)
    offsets: dict[int, int] = {}
    for signature, base_indexes in base_positions.items():
        provider_indexes = provider_positions.get(signature, [])
        if len(base_indexes) != 1 or len(provider_indexes) != 1:
            continue
        offset = base_indexes[0] - provider_indexes[0]
        offsets[offset] = offsets.get(offset, 0) + 1
    if not offsets:
        return None
    ranked = sorted(offsets.items(), key=lambda item: item[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def _labels_match(left: str, right: str) -> bool:
    return bool(_norm(left) and _norm(left) == _norm(right))


def _text_similarity(left: str, right: str) -> float:
    left_key = _norm(left)
    right_key = _norm(right)
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key:
        return 1.0
    return SequenceMatcher(None, left_key, right_key, autojunk=False).ratio()


def _list_similarity(left: list[str], right: list[str]) -> float:
    if not left and not right:
        return 1.0
    if len(left) != len(right) or not left:
        return 0.0
    remaining = list(right)
    scores: list[float] = []
    for value in left:
        ranked = sorted(
            ((_text_similarity(value, candidate), index) for index, candidate in enumerate(remaining)),
            reverse=True,
        )
        score, index = ranked[0]
        scores.append(score)
        remaining.pop(index)
    return sum(scores) / len(scores)


def _semantic_match_score(
    canonical: FamilyCanonicalQuestion,
    semantic: SemanticQuestionText,
) -> float:
    title_score = _text_similarity(canonical.title, semantic.title)
    option_score = _list_similarity(
        [item.label for item in canonical.options],
        list(semantic.options),
    )
    row_score = _list_similarity(
        [item.label for item in canonical.rows],
        list(semantic.rows),
    )
    structured_weight = (0.2 if canonical.options else 0.0) + (
        0.1 if canonical.rows else 0.0
    )
    title_weight = 1.0 - structured_weight
    return (
        title_score * title_weight
        + option_score * (0.2 if canonical.options else 0.0)
        + row_score * (0.1 if canonical.rows else 0.0)
    )


def _provider_question_id(question: CanonicalQuestion) -> str | None:
    if question.provider_question_id:
        return question.provider_question_id
    if question.rows:
        return None
    return None


def _canonical_options(
    family_id: str,
    question: CanonicalQuestion,
    semantic: SemanticQuestionText,
) -> list[FamilyCanonicalOption]:
    values = list(semantic.options)
    if len(values) != len(question.options):
        values = [option.label or option.value for option in question.options]
    return [
        FamilyCanonicalOption(
            canonical_option_key=_stable_id(
                "opt", family_id, question.question_id, str(index)
            ),
            label=str(values[index] or question.options[index].value),
        )
        for index in range(len(question.options))
    ]


def _canonical_rows(
    family_id: str,
    question: CanonicalQuestion,
    semantic: SemanticQuestionText,
) -> list[FamilyCanonicalRow]:
    values = list(semantic.rows)
    if len(values) != len(question.rows):
        values = [row.label for row in question.rows]
    return [
        FamilyCanonicalRow(
            canonical_row_key=_stable_id(
                "row", family_id, question.question_id, str(index)
            ),
            label=str(values[index] or question.rows[index].label),
        )
        for index in range(len(question.rows))
    ]


def _ordered_indexes(
    canonical_labels: list[str],
    provider_labels: list[str],
) -> tuple[list[int], bool]:
    """Return unique semantic mapping, else deterministic positional mapping."""

    if len(canonical_labels) != len(provider_labels):
        raise ValueError("结构数量不一致")
    provider_by_label: dict[str, list[int]] = {}
    for index, label in enumerate(provider_labels):
        provider_by_label.setdefault(_norm(label), []).append(index)
    resolved: list[int] = []
    semantic = True
    for canonical in canonical_labels:
        matches = provider_by_label.get(_norm(canonical), [])
        if len(matches) != 1 or matches[0] in resolved:
            semantic = False
            break
        resolved.append(matches[0])
    if semantic and len(resolved) == len(canonical_labels):
        return resolved, True
    return list(range(len(canonical_labels))), False


def _variant_mapping(
    canonical: FamilyCanonicalQuestion,
    provider_question: CanonicalQuestion,
    semantic: SemanticQuestionText,
    *,
    confidence: float,
) -> tuple[FamilyVariantQuestionMapping, bool]:
    provider_option_labels = list(semantic.options)
    if len(provider_option_labels) != len(provider_question.options):
        provider_option_labels = [
            option.label or option.value for option in provider_question.options
        ]
    option_indexes, options_semantic = _ordered_indexes(
        [item.label for item in canonical.options],
        provider_option_labels,
    )
    option_mappings = [
        FamilyOptionMapping(
            canonical_option_key=canonical.options[index].canonical_option_key,
            provider_value=provider_question.options[provider_index].value,
        )
        for index, provider_index in enumerate(option_indexes)
    ]

    provider_row_labels = list(semantic.rows)
    if len(provider_row_labels) != len(provider_question.rows):
        provider_row_labels = [row.label for row in provider_question.rows]
    row_indexes, rows_semantic = _ordered_indexes(
        [item.label for item in canonical.rows],
        provider_row_labels,
    )
    row_mappings: list[FamilyRowMapping] = []
    for index, provider_index in enumerate(row_indexes):
        provider_row = provider_question.rows[provider_index]
        if not provider_row.provider_question_id:
            raise ValueError("矩阵行缺少 provider questionId")
        row_mappings.append(FamilyRowMapping(
            canonical_row_key=canonical.rows[index].canonical_row_key,
            provider_question_id=provider_row.provider_question_id,
            provider_label=provider_row.label,
        ))
    mapping = FamilyVariantQuestionMapping(
        variant_id="placeholder",
        provider_question_id=_provider_question_id(provider_question),
        mapping_confidence=confidence,
        option_mappings=option_mappings,
        row_mappings=row_mappings,
    )
    return mapping, options_semantic and rows_semantic


def _new_canonical(
    family_id: str,
    variant_id: str,
    question: CanonicalQuestion,
    semantic: SemanticQuestionText,
    *,
    role: FamilyQuestionRole,
) -> FamilyCanonicalQuestion:
    canonical = FamilyCanonicalQuestion(
        canonical_question_key=_stable_id(
            "cq", family_id, variant_id, question.question_id
        ),
        canonical_type=question.canonical_type,
        role=role,
        title=semantic.title or question.title,
        required=question.required,
        options=_canonical_options(family_id, question, semantic),
        rows=_canonical_rows(family_id, question, semantic),
        variant_mappings=[],
    )
    mapping, _ = _variant_mapping(
        canonical,
        question,
        semantic,
        confidence=1.0,
    )
    mapping = mapping.model_copy(update={"variant_id": variant_id})
    return canonical.model_copy(update={"variant_mappings": [mapping]})


def build_questionnaire_family(
    *,
    owner_ref: str,
    title: str,
    variants: list[FamilyVariantSnapshot],
    semantic_questions: SemanticQuestionMap | None = None,
    now: datetime | None = None,
) -> QuestionnaireFamily:
    """Build one stable family; ambiguous core mappings fail closed."""

    if not isinstance(owner_ref, str) or not owner_ref.strip():
        raise ValueError("owner_ref 不能为空")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title 不能为空")
    if not variants:
        raise ValueError("至少需要一个 questionnaire variant")
    semantic_map = semantic_questions or {}
    form_ids = [item.snapshot.provider_form_id for item in variants]
    if any(not value for value in form_ids) or len(form_ids) != len(set(form_ids)):
        raise ValueError("variant 必须引用唯一 Google Form")
    languages = [str(item.language) for item in variants]
    if len(languages) != len(set(languages)):
        raise ValueError("variant language 不能重复")

    family_id = questionnaire_family_id(
        owner_ref,
        title,
        [str(value) for value in form_ids],
    )
    family_variants: list[QuestionnaireFamilyVariant] = []
    variant_ids: list[str] = []
    for item in variants:
        form_id = str(item.snapshot.provider_form_id)
        variant_id = questionnaire_family_variant_id(
            family_id,
            str(item.language),
            form_id,
        )
        variant_ids.append(variant_id)
        family_variants.append(QuestionnaireFamilyVariant(
            variant_id=variant_id,
            language=item.language,
            snapshot_id=item.snapshot.snapshot_id,
            provider_form_id=form_id,
            question_count=item.snapshot.question_count,
        ))

    base_questions = _substantive(variants[0].snapshot)
    canonical_questions: list[FamilyCanonicalQuestion] = []
    diagnostics: list[QuestionnaireFamilyDiagnostic] = []
    for question in base_questions:
        semantic = _semantic(semantic_map, variant_ids[0], question)
        role = (
            FamilyQuestionRole.OPTIONAL_RESPONDENT_METADATA
            if _is_metadata(question, semantic.title)
            else FamilyQuestionRole.CORE
        )
        canonical_questions.append(_new_canonical(
            family_id,
            variant_ids[0],
            question,
            semantic,
            role=role,
        ))

    for variant_index, variant_source in enumerate(variants[1:], start=1):
        variant_id = variant_ids[variant_index]
        language = family_variants[variant_index].language
        provider_questions = _substantive(variant_source.snapshot)
        position_offset = _structural_position_offset(
            base_questions,
            provider_questions,
        )
        base_positions_by_key = {
            canonical.canonical_question_key: index
            for index, canonical in enumerate(canonical_questions)
        }
        used_provider_ids: set[str] = set()
        unresolved_questions: list[tuple[CanonicalQuestion, SemanticQuestionText]] = []
        type_conflict_canonical_keys: set[str] = set()

        # Metadata is optional and may legitimately exist in only one variant.
        for metadata_provider_position, question in enumerate(provider_questions):
            semantic = _semantic(semantic_map, variant_id, question)
            if not _is_metadata(question, semantic.title):
                continue
            available = [
                canonical
                for canonical in canonical_questions
                if canonical.role == FamilyQuestionRole.OPTIONAL_RESPONDENT_METADATA
                and canonical.canonical_type == question.canonical_type
                and len(canonical.options) == len(question.options)
                and len(canonical.rows) == len(question.rows)
                and not any(m.variant_id == variant_id for m in canonical.variant_mappings)
            ]
            exact = [
                canonical for canonical in available
                if _labels_match(canonical.title, semantic.title)
            ]
            chosen_metadata: FamilyCanonicalQuestion | None = None
            metadata_confidence = 0.0
            metadata_position_fallback = False
            if len(exact) == 1:
                chosen_metadata = exact[0]
                metadata_confidence = 0.98
            elif len(available) == 1:
                chosen_metadata = available[0]
                metadata_confidence = 0.88
            elif available:
                ranked_metadata = sorted(
                    (
                        (_semantic_match_score(canonical, semantic), canonical)
                        for canonical in available
                    ),
                    key=lambda item: item[0],
                    reverse=True,
                )
                best_score = ranked_metadata[0][0]
                next_score = (
                    ranked_metadata[1][0]
                    if len(ranked_metadata) > 1
                    else 0.0
                )
                if best_score >= 0.72 and best_score - next_score >= 0.10:
                    chosen_metadata = ranked_metadata[0][1]
                    metadata_confidence = round(min(0.96, best_score), 4)
                elif best_score >= 0.80:
                    semantic_ties = [
                        canonical
                        for score, canonical in ranked_metadata
                        if best_score - score <= 0.01
                    ]
                    positioned_metadata = sorted(
                        (
                            (
                                abs(
                                    canonical_questions.index(canonical)
                                    - metadata_provider_position
                                ),
                                canonical,
                            )
                            for canonical in semantic_ties
                        ),
                        key=lambda item: item[0],
                    )
                    if (
                        positioned_metadata
                        and (
                            len(positioned_metadata) == 1
                            or positioned_metadata[0][0]
                            < positioned_metadata[1][0]
                        )
                    ):
                        chosen_metadata = positioned_metadata[0][1]
                        metadata_confidence = 0.90
                        metadata_position_fallback = True
            if chosen_metadata is None and position_offset is not None:
                expected_position = metadata_provider_position + position_offset
                positioned_metadata = sorted(
                    (
                        (
                            abs(
                                base_positions_by_key[
                                    canonical.canonical_question_key
                                ] - expected_position
                            ),
                            canonical,
                        )
                        for canonical in available
                        if canonical.canonical_question_key
                        in base_positions_by_key
                    ),
                    key=lambda item: item[0],
                )
                if (
                    positioned_metadata
                    and positioned_metadata[0][0] == 0
                    and (
                        len(positioned_metadata) == 1
                        or positioned_metadata[0][0]
                        < positioned_metadata[1][0]
                    )
                ):
                    chosen_metadata = positioned_metadata[0][1]
                    metadata_confidence = 0.84
                    metadata_position_fallback = True
            if chosen_metadata is not None:
                mapping, semantic_structure = _variant_mapping(
                    chosen_metadata,
                    question,
                    semantic,
                    confidence=metadata_confidence,
                )
                mapping = mapping.model_copy(update={"variant_id": variant_id})
                chosen_metadata.variant_mappings.append(mapping)
                used_provider_ids.add(question.question_id)
                if metadata_position_fallback:
                    diagnostics.append(QuestionnaireFamilyDiagnostic(
                        code="metadata_semantic_position_fallback",
                        message=(
                            "多项同义受访者信息按相对顺序完成唯一映射；"
                            "缺失值仍按语言版本留空"
                        ),
                        severity=FamilyDiagnosticSeverity.INFO,
                        blocking=False,
                        variant_id=variant_id,
                        language=language,
                        canonical_question_key=(
                            chosen_metadata.canonical_question_key
                        ),
                        question_title=chosen_metadata.title,
                    ))
                if not semantic_structure and (
                    chosen_metadata.options or chosen_metadata.rows
                ):
                    diagnostics.append(QuestionnaireFamilyDiagnostic(
                        code="metadata_structure_position_fallback",
                        message="可选受访者元数据按结构顺序完成映射",
                        severity=FamilyDiagnosticSeverity.INFO,
                        blocking=False,
                        variant_id=variant_id,
                        language=language,
                        canonical_question_key=(
                            chosen_metadata.canonical_question_key
                        ),
                        question_title=chosen_metadata.title,
                    ))
            else:
                canonical_questions.append(_new_canonical(
                    family_id,
                    variant_id,
                    question,
                    semantic,
                    role=FamilyQuestionRole.OPTIONAL_RESPONDENT_METADATA,
                ))
                used_provider_ids.add(question.question_id)

        core_canonicals = [
            item for item in canonical_questions
            if item.role == FamilyQuestionRole.CORE
        ]
        canonical_positions = {
            item.canonical_question_key: index
            for index, item in enumerate(core_canonicals)
        }
        core_provider_position = -1
        duplicate_position_fallback_count = 0
        for provider_position, question in enumerate(provider_questions):
            if question.question_id in used_provider_ids:
                continue
            semantic = _semantic(semantic_map, variant_id, question)
            if _is_metadata(question, semantic.title):
                continue
            core_provider_position += 1
            compatible = [
                canonical
                for canonical in core_canonicals
                if not any(m.variant_id == variant_id for m in canonical.variant_mappings)
                and canonical.canonical_type == question.canonical_type
                and len(canonical.options) == len(question.options)
                and len(canonical.rows) == len(question.rows)
            ]
            semantic_matches = [
                canonical for canonical in compatible
                if _labels_match(canonical.title, semantic.title)
            ]
            chosen: FamilyCanonicalQuestion | None = None
            confidence = 0.0
            duplicate_position_fallback = False
            if len(semantic_matches) == 1:
                chosen = semantic_matches[0]
                confidence = 0.98
            elif len(semantic_matches) > 1:
                positioned = sorted(
                    (
                        (
                            abs(
                                canonical_positions[
                                    canonical.canonical_question_key
                                ] - core_provider_position
                            ),
                            canonical,
                        )
                        for canonical in semantic_matches
                    ),
                    key=lambda item: item[0],
                )
                if (
                    len(positioned) == 1
                    or positioned[0][0] < positioned[1][0]
                ):
                    chosen = positioned[0][1]
                    confidence = 0.92
                    duplicate_position_fallback = True
            elif len(compatible) == 1:
                chosen = compatible[0]
                confidence = 0.88
            elif compatible:
                ranked = sorted(
                    (
                        (_semantic_match_score(canonical, semantic), canonical)
                        for canonical in compatible
                    ),
                    key=lambda item: item[0],
                    reverse=True,
                )
                best_score = ranked[0][0]
                next_score = ranked[1][0] if len(ranked) > 1 else 0.0
                if best_score >= 0.80 and best_score - next_score >= 0.10:
                    chosen = ranked[0][1]
                    confidence = round(min(0.96, best_score), 4)
            if chosen is None:
                type_conflicts = [
                    canonical
                    for canonical in core_canonicals
                    if _labels_match(canonical.title, semantic.title)
                    and canonical.canonical_type != question.canonical_type
                ]
                if len(type_conflicts) == 1:
                    conflict = type_conflicts[0]
                    type_conflict_canonical_keys.add(
                        conflict.canonical_question_key
                    )
                    diagnostics.append(QuestionnaireFamilyDiagnostic(
                        code="core_question_type_conflict",
                        message="同一核心题在两个语言版本中的题型不一致",
                        severity=FamilyDiagnosticSeverity.ERROR,
                        blocking=True,
                        variant_id=variant_id,
                        language=language,
                        canonical_question_key=conflict.canonical_question_key,
                        question_title=semantic.title or question.title,
                        related_question_title=conflict.title,
                    ))
                else:
                    unresolved_questions.append((question, semantic))
                continue
            mapping, semantic_structure = _variant_mapping(
                chosen,
                question,
                semantic,
                confidence=confidence,
            )
            mapping = mapping.model_copy(update={"variant_id": variant_id})
            chosen.variant_mappings.append(mapping)
            used_provider_ids.add(question.question_id)
            if duplicate_position_fallback:
                duplicate_position_fallback_count += 1
            if not semantic_structure and (chosen.options or chosen.rows):
                diagnostics.append(QuestionnaireFamilyDiagnostic(
                    code="structured_values_position_fallback",
                    message="选项或矩阵行按结构顺序映射，原始值仍完整保留",
                    severity=FamilyDiagnosticSeverity.WARNING,
                    blocking=False,
                    variant_id=variant_id,
                    language=language,
                    canonical_question_key=chosen.canonical_question_key,
                    question_title=chosen.title,
                ))

        if duplicate_position_fallback_count:
            diagnostics.append(QuestionnaireFamilyDiagnostic(
                code="duplicate_semantic_position_fallback",
                message=(
                    "多道同名同结构题按核心题相对顺序完成唯一映射；"
                    "题目原始来源仍完整保留"
                ),
                severity=FamilyDiagnosticSeverity.INFO,
                blocking=False,
                variant_id=variant_id,
                language=language,
                affected_count=duplicate_position_fallback_count,
            ))

        missing_core = [
            canonical
            for canonical in canonical_questions
            if canonical.role == FamilyQuestionRole.CORE
            and canonical.canonical_question_key not in type_conflict_canonical_keys
            and not any(
                mapping.variant_id == variant_id
                for mapping in canonical.variant_mappings
            )
        ]
        paired_gap_count = min(len(unresolved_questions), len(missing_core))
        for question, semantic in unresolved_questions:
            diagnostics.append(QuestionnaireFamilyDiagnostic(
                code="core_question_ambiguous",
                message=(
                    "该语言版本的核心题无法唯一对应规范题；"
                    "请核对两个 Form 是否为同一问卷版本"
                ),
                severity=FamilyDiagnosticSeverity.ERROR,
                blocking=True,
                variant_id=variant_id,
                language=language,
                question_title=semantic.title or question.title,
            ))
        for canonical in missing_core[paired_gap_count:]:
            diagnostics.append(QuestionnaireFamilyDiagnostic(
                code="core_question_missing",
                message="该语言版本缺少核心规范题",
                severity=FamilyDiagnosticSeverity.ERROR,
                blocking=True,
                variant_id=variant_id,
                language=language,
                canonical_question_key=canonical.canonical_question_key,
                question_title=canonical.title,
            ))

        for canonical in canonical_questions:
            if (
                canonical.role == FamilyQuestionRole.OPTIONAL_RESPONDENT_METADATA
                and not any(
                    mapping.variant_id == variant_id
                    for mapping in canonical.variant_mappings
                )
            ):
                diagnostics.append(QuestionnaireFamilyDiagnostic(
                    code="optional_metadata_missing",
                    message="该语言版本未收集一项可选受访者元数据，合并时留空",
                    severity=FamilyDiagnosticSeverity.INFO,
                    blocking=False,
                    variant_id=variant_id,
                    language=language,
                    canonical_question_key=canonical.canonical_question_key,
                    question_title=canonical.title,
                ))

    current_time = now or datetime.now(timezone.utc)
    status = (
        QuestionnaireFamilyStatus.NEEDS_REVIEW
        if any(item.blocking for item in diagnostics)
        else QuestionnaireFamilyStatus.READY
    )
    fingerprint_payload = {
        "family_id": family_id,
        "variants": [item.model_dump(mode="json") for item in family_variants],
        "canonical_questions": [
            item.model_dump(mode="json") for item in canonical_questions
        ],
        "diagnostics": [item.model_dump(mode="json") for item in diagnostics],
    }
    return QuestionnaireFamily(
        family_id=family_id,
        owner_ref=owner_ref.strip(),
        title=title.strip(),
        created_at=current_time,
        updated_at=current_time,
        status=status,
        variants=family_variants,
        canonical_questions=canonical_questions,
        diagnostics=diagnostics,
        mapping_fingerprint=structured_sha256(fingerprint_payload),
    )


__all__ = [
    "FamilyVariantSnapshot",
    "SemanticQuestionMap",
    "SemanticQuestionText",
    "build_questionnaire_family",
    "questionnaire_family_id",
    "questionnaire_family_variant_id",
]
