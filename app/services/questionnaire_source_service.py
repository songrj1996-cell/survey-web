"""问卷来源优先级、降级选择、冲突提示与原子快照保存。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.core.research_assets import (
    ResearchContractError,
    structured_sha256,
    validate_research_contract,
)
from app.schemas.questionnaire_sources import (
    QuestionnaireMergeCandidate,
    QuestionnaireSourceAttempt,
    QuestionnaireSourceConflict,
    QuestionnaireSourceResult,
    QuestionnaireSourceValue,
)
from app.schemas.research_assets import (
    ImportErrorCode,
    ImportIssue,
    ProcessingStatus,
)
from app.storage.research_assets import ResearchAssetBundle, ResearchAssetStorage


_USABLE_STATUSES = {
    ProcessingStatus.COMPLETED,
    ProcessingStatus.PARTIAL,
    ProcessingStatus.NEEDS_REVIEW,
}


class QuestionnaireSourceUnavailableError(ValueError):
    """所有候选来源均未产生可验证的快照。"""

    def __init__(self, attempts: tuple[QuestionnaireSourceAttempt, ...]):
        super().__init__("没有可用的问卷定义来源，可上传原问卷或快照包后重试")
        self.attempts = attempts


class QuestionnaireSourceScopeError(ValueError):
    """来源候选不属于当前请求用户；错误文本不包含任何候选内容。"""

    def __init__(self) -> None:
        super().__init__("问卷来源候选不属于当前用户范围")


class QuestionnaireSourceSelectionRequiredError(ValueError):
    """同一可信级别有多个完整来源，必须由上层显式选择。"""

    def __init__(
        self,
        attempts: tuple[QuestionnaireSourceAttempt, ...],
        source_ids: tuple[str, ...],
        conflicts: tuple[QuestionnaireSourceConflict, ...],
    ) -> None:
        super().__init__("同一可信级别存在多个问卷来源，需要明确选择后继续")
        self.attempts = attempts
        self.source_ids = source_ids
        self.conflicts = conflicts


def _attempt(candidate: QuestionnaireMergeCandidate) -> QuestionnaireSourceAttempt:
    return QuestionnaireSourceAttempt(
        source_id=candidate.source_id,
        source_mode=candidate.source_mode,
        priority=candidate.priority,
        status=candidate.status,
        snapshot_id=(
            candidate.snapshot.snapshot_id if candidate.snapshot is not None else None
        ),
        warnings=candidate.warnings,
        issues=candidate.issues,
    )


def _field_value(candidate: QuestionnaireMergeCandidate, field: str) -> Any:
    assert candidate.snapshot is not None
    value = getattr(candidate.snapshot, field)
    return value.value if hasattr(value, "value") else value


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _question_identity(
    candidate: QuestionnaireMergeCandidate,
    question: Any,
) -> str:
    assert candidate.snapshot is not None
    provider_ids = sorted({
        *(
            [question.provider_question_id]
            if question.provider_question_id is not None
            else []
        ),
        *(
            row.provider_question_id
            for row in question.rows
            if row.provider_question_id is not None
        ),
    })
    provider = candidate.snapshot.provider.value
    if provider_ids:
        return f"{provider}:question:{','.join(provider_ids)}"
    if question.provider_item_id is not None:
        return f"{provider}:item:{question.provider_item_id}"
    return f"{provider}:canonical:{question.question_id}"


def _conflict_values(
    usable: list[QuestionnaireMergeCandidate],
) -> list[tuple[str, list[Any]]]:
    specs: list[tuple[str, list[Any]]] = []
    for field in (
        "title",
        "collection_state",
        "question_count",
        "item_count",
        "asset_count",
        "provider_form_id",
        "provider_revision",
    ):
        specs.append((
            f"snapshot.{field}",
            [_field_value(candidate, field) for candidate in usable],
        ))

    questions_by_candidate: list[dict[str, Any]] = []
    all_identities: set[str] = set()
    for candidate in usable:
        assert candidate.snapshot is not None
        mapped = {
            _question_identity(candidate, question): question
            for question in candidate.snapshot.canonical_questions
        }
        questions_by_candidate.append(mapped)
        all_identities.update(mapped)

    for identity in sorted(all_identities):
        available = [mapping.get(identity) for mapping in questions_by_candidate]
        presence_values = [question is not None for question in available]
        specs.append((
            f"snapshot.canonical_questions[{identity}].present",
            presence_values,
        ))
        for field in (
            "title",
            "description",
            "canonical_type",
            "required",
            "options",
            "rows",
            "branching",
            "asset_reference_ids",
        ):
            values: list[Any] = []
            for question in available:
                if question is None:
                    values.append(None)
                    continue
                values.append(_json_value(getattr(question, field)))
            specs.append((
                f"snapshot.canonical_questions[{identity}].{field}",
                values,
            ))
    return specs


def _conflicts(
    selected: QuestionnaireMergeCandidate,
    usable: list[QuestionnaireMergeCandidate],
) -> list[QuestionnaireSourceConflict]:
    conflicts: list[QuestionnaireSourceConflict] = []
    for field_path, values in _conflict_values(usable):
        candidates = [
            QuestionnaireSourceValue(
                source_id=item.source_id,
                source_mode=item.source_mode,
                priority=item.priority,
                value=value,
                confidence=(
                    1.0
                    if item.snapshot.mapping_status.value == "exact"
                    else 0.75
                ),
            )
            for item, value in zip(usable, values, strict=True)
        ]
        distinct = {
            structured_sha256(candidate.value) for candidate in candidates
        }
        if len(distinct) <= 1:
            continue
        selected_value = next(
            candidate
            for candidate in candidates
            if candidate.source_id == selected.source_id
        )
        if field_path.endswith(".present"):
            suggested = next(
                (candidate for candidate in candidates if candidate.value is True),
                selected_value,
            )
        else:
            suggested = next(
                (
                    candidate for candidate in candidates
                    if candidate.value is not None
                    and candidate.value != ""
                    and candidate.value != []
                    and candidate.value != {}
                ),
                selected_value,
            )
        conflicts.append(QuestionnaireSourceConflict(
            conflict_id=f"qconf_{structured_sha256({
                'field': field_path,
                'sources': [candidate.source_id for candidate in candidates],
                'values': [candidate.value for candidate in candidates],
            })[:24]}",
            field_path=field_path,
            candidates=candidates,
            suggested_source_id=suggested.source_id,
            suggested_value=suggested.value,
            reason=(
                "优先保留高可信来源；高可信来源缺失该字段时，建议采用"
                "下一可用来源的非空值"
            ),
            blocking=False,
        ))
    return conflicts


def resolve_questionnaire_sources(
    candidates: Iterable[QuestionnaireMergeCandidate],
    *,
    owner_ref: str,
) -> QuestionnaireSourceResult:
    """选择最高优先级完整来源，并把其他来源差异显式保留为冲突。

    此处不做整份结构的静默字段覆盖。真正的逐字段合并必须在用户确认冲突后
    由上层提交新的完整候选；因此选出的 Bundle 始终可独立复现和校验。
    """
    ordered = sorted(candidates, key=lambda item: (item.priority, item.source_id))
    if not ordered:
        raise ValueError("至少需要一个问卷来源候选")
    if not isinstance(owner_ref, str) or not owner_ref.strip():
        raise ValueError("owner_ref 不能为空")
    owner = owner_ref.strip()
    if len({candidate.source_id for candidate in ordered}) != len(ordered):
        raise ValueError("问卷来源候选不能包含重复 source_id")
    if any(
        candidate.collection is not None
        and candidate.collection.owner_ref != owner
        for candidate in ordered
    ):
        raise QuestionnaireSourceScopeError()
    attempts: list[QuestionnaireSourceAttempt] = []
    usable: list[QuestionnaireMergeCandidate] = []
    usable_indexes: list[int] = []
    for index, candidate in enumerate(ordered):
        if (
            candidate.snapshot is None
            or candidate.collection is None
            or candidate.status not in _USABLE_STATUSES
        ):
            attempts.append(_attempt(candidate))
            continue
        try:
            validate_research_contract(candidate.snapshot, candidate.collection)
        except ResearchContractError:
            attempts.append(QuestionnaireSourceAttempt(
                source_id=candidate.source_id,
                source_mode=candidate.source_mode,
                priority=candidate.priority,
                status=ProcessingStatus.FAILED,
                warnings=candidate.warnings,
                issues=[
                    *candidate.issues,
                    ImportIssue(
                        code=ImportErrorCode.INTEGRITY_ERROR,
                        message="候选问卷快照未通过完整性校验",
                        retryable=False,
                        suggested_action="重新连接来源或上传未修改的快照包",
                    ),
                ],
            ))
            continue
        attempts.append(_attempt(candidate))
        usable.append(candidate)
        usable_indexes.append(index)

    if not usable:
        raise QuestionnaireSourceUnavailableError(tuple(attempts))

    best_priority = usable[0].priority
    best_candidates = [
        candidate for candidate in usable
        if candidate.priority == best_priority
    ]
    if len(best_candidates) > 1:
        raise QuestionnaireSourceSelectionRequiredError(
            tuple(attempts),
            tuple(candidate.source_id for candidate in best_candidates),
            tuple(_conflicts(best_candidates[0], best_candidates)),
        )

    selected = best_candidates[0]
    assert selected.snapshot is not None
    assert selected.collection is not None
    selected_index = usable_indexes[0]
    failed_before_success = selected_index > 0
    conflicts = _conflicts(selected, usable)
    partial = (
        failed_before_success
        or selected.status != ProcessingStatus.COMPLETED
        or bool(selected.warnings)
        or bool(selected.issues)
        or bool(conflicts)
    )
    return QuestionnaireSourceResult(
        snapshot=selected.snapshot,
        collection=selected.collection,
        selected_source_ids=[selected.source_id],
        attempts=attempts,
        conflicts=conflicts,
        partial_success=partial,
    )


def save_questionnaire_source_result(
    result: QuestionnaireSourceResult,
    storage: ResearchAssetStorage,
) -> None:
    """通过原子存储端口保存已解析、可复现的统一结果。"""
    validate_research_contract(result.snapshot, result.collection)
    storage.save_bundle(
        result.collection.owner_ref,
        ResearchAssetBundle(result.snapshot, result.collection),
    )
