"""Service orchestration for multi-language Google Forms analysis."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
from typing import Protocol

from fastapi import HTTPException

from app.core.config import (
    LLM_COLUMN_FALLBACK_MODELS,
    LLM_COLUMN_MAX_TOKENS,
    LLM_COLUMN_MODEL,
    LLM_COLUMN_REASONING,
)
from app.integrations.google_forms_responses_client import (
    GoogleFormResponsesCapture,
    GoogleFormsResponsesClientError,
    GoogleFormsResponsesErrorCode,
)
from app.integrations.llm_client import collect_chat_completion
from app.schemas.questionnaire import CanonicalQuestionType, QuestionnaireSnapshot
from app.schemas.questionnaire_families import (
    LanguageCode,
    QuestionnaireFamily,
    QuestionnaireFamilyAnalysisSessionResponse,
    QuestionnaireFamilyStatus,
    QuestionnaireFamilySummary,
    family_summary,
)
from app.services.glossary_service import prepare_glossary_messages
from app.services.google_forms_family_binding import (
    bind_google_forms_family_responses,
)
from app.services.google_forms_snapshot_api import (
    GoogleFormsQuestionnaireAuthRequiredError,
    GoogleFormsQuestionnaireNotFoundError,
    GoogleFormsQuestionnairePermissionError,
    GoogleFormsQuestionnaireRetryableError,
    GoogleFormsQuestionnaireSnapshotApi,
    GoogleFormsQuestionnaireSnapshotApiError,
)
from app.services.questionnaire_family_mapping import (
    FamilyVariantSnapshot,
    SemanticQuestionMap,
    SemanticQuestionText,
    build_questionnaire_family,
    questionnaire_family_id,
    questionnaire_family_variant_id,
)
from app.services.questionnaire_import import (
    build_questionnaire_translation_query,
    parse_questionnaire_translations,
)
from app.services.survey_service import handle_survey_upload
from app.storage.prompts import _get_questionnaire_translation_system_prompt
from app.storage.questionnaire_families import (
    FileQuestionnaireFamilyStorage,
    QuestionnaireFamilyStorageError,
)
from app.storage.research_assets import (
    ResearchAssetStorageError,
    ResearchSnapshotStorage,
    SnapshotPackage,
)


class GoogleFormsFamilyClient(Protocol):
    async def fetch_responses(
        self,
        owner_ref: str,
        form_id: str,
    ) -> GoogleFormResponsesCapture: ...


FamilySemanticTranslator = Callable[
    [str, str, list[FamilyVariantSnapshot]],
    Awaitable[SemanticQuestionMap],
]


class GoogleFormsFamilyApiError(RuntimeError):
    pass


class GoogleFormsFamilyInvalidError(GoogleFormsFamilyApiError):
    pass


class GoogleFormsFamilyNotFoundError(GoogleFormsFamilyApiError):
    pass


class GoogleFormsFamilyNeedsReviewError(GoogleFormsFamilyApiError):
    def __init__(self, family: QuestionnaireFamily, message: str) -> None:
        super().__init__(message)
        self.family = family


class GoogleFormsFamilyProviderError(GoogleFormsFamilyApiError):
    def __init__(self, *, status_code: int | None = None) -> None:
        super().__init__()
        self.status_code = status_code


class GoogleFormsFamilyMappingUnavailableError(GoogleFormsFamilyApiError):
    pass


class GoogleFormsFamilyInternalError(GoogleFormsFamilyApiError):
    pass


_NON_TRANSLATED_QUESTION_TYPES = {
    CanonicalQuestionType.SECTION,
    CanonicalQuestionType.STATIC_TEXT,
}


def _role(question_type) -> str:
    value = str(question_type.value)
    return {
        "single_choice": "single_choice",
        "dropdown": "single_choice",
        "multi_choice": "multi_choice",
        "open_text": "open_text",
        "scale": "scale",
        "rating": "scale",
        "matrix_single": "matrix_single",
        "matrix_multi": "matrix_multi",
        "matrix_scale": "matrix_scale",
        "date": "open_text",
        "time": "open_text",
        "file_upload": "ignore",
    }.get(value, "ignore")


async def translate_family_variants_with_llm(
    owner_ref: str,
    title: str,
    variants: list[FamilyVariantSnapshot],
) -> SemanticQuestionMap:
    """Translate display text only; failures leave deterministic mapping to fail closed."""

    family_id = questionnaire_family_id(
        owner_ref,
        title,
        [str(item.snapshot.provider_form_id or "") for item in variants],
    )
    semantic: SemanticQuestionMap = {}
    reference_translations: list[dict[str, object]] = []
    for variant_index, variant in enumerate(variants):
        form_id = str(variant.snapshot.provider_form_id or "")
        questions = []
        translatable_questions = [
            question
            for question in variant.snapshot.canonical_questions
            if question.canonical_type not in _NON_TRANSLATED_QUESTION_TYPES
        ]
        for index, question in enumerate(translatable_questions):
            questions.append({
                "source_question_id": question.question_id,
                "name_zh": question.title,
                "role": _role(question.canonical_type),
                "column_indexes": [index],
                "options": [item.label or item.value for item in question.options],
                "rows": [item.label for item in question.rows],
            })
        if not questions:
            continue
        system_prompt = _get_questionnaire_translation_system_prompt()
        query = build_questionnaire_translation_query(questions)
        if reference_translations:
            query += (
                "\n\n跨语言对齐参考：下面是同一问卷基准语言版本的规范中文。"
                "当前题目如与某项语义相同，必须复用其 name_zh、options_zh 和 rows_zh；"
                "若确实是新增或不同题目，则准确翻译，不要强行对应。\n"
                + json.dumps(reference_translations, ensure_ascii=False)
            )
        messages = prepare_glossary_messages([
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": query,
            },
        ])
        try:
            answer, _ = await collect_chat_completion(
                messages,
                models=(LLM_COLUMN_MODEL, *LLM_COLUMN_FALLBACK_MODELS),
                max_tokens=LLM_COLUMN_MAX_TOKENS,
                reasoning_effort=LLM_COLUMN_REASONING or None,
            )
            try:
                translated = parse_questionnaire_translations(answer, questions)
            except ValueError as parse_error:
                repair_messages = prepare_glossary_messages([
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {"role": "user", "content": query},
                    {"role": "assistant", "content": answer},
                    {
                        "role": "user",
                        "content": (
                            f"上一次输出无法通过校验：{parse_error}。\n"
                            "请只修复翻译 JSON，不得改变 question_id、数组长度或顺序，"
                            "并确保英文题干已翻译为简体中文。"
                        ),
                    },
                ])
                repaired, _ = await collect_chat_completion(
                    repair_messages,
                    models=(LLM_COLUMN_MODEL, *LLM_COLUMN_FALLBACK_MODELS),
                    max_tokens=LLM_COLUMN_MAX_TOKENS,
                    reasoning_effort=LLM_COLUMN_REASONING or None,
                )
                translated = parse_questionnaire_translations(
                    repaired,
                    questions,
                )
        except Exception as error:
            raise GoogleFormsFamilyMappingUnavailableError() from error
        semantic_variant_id = questionnaire_family_variant_id(
            family_id,
            str(variant.language),
            form_id,
        )
        for question in variant.snapshot.canonical_questions:
            item = translated.get(question.question_id)
            if item is None:
                continue
            semantic[(semantic_variant_id, question.question_id)] = SemanticQuestionText(
                title=item["name_zh"],
                options=tuple(item["options_zh"]),
                rows=tuple(item["rows_zh"]),
            )
        if variant_index == 0:
            reference_translations = [
                {
                    "reference_key": f"reference-{index + 1}",
                    "role": question["role"],
                    "name_zh": translated[question["source_question_id"]]["name_zh"],
                    "options_zh": translated[question["source_question_id"]]["options_zh"],
                    "rows_zh": translated[question["source_question_id"]]["rows_zh"],
                }
                for index, question in enumerate(questions)
            ]
    return semantic


def _semantic_translation_is_complete(
    owner_ref: str,
    title: str,
    variants: list[FamilyVariantSnapshot],
    semantic: SemanticQuestionMap,
) -> bool:
    family_id = questionnaire_family_id(
        owner_ref,
        title,
        [str(item.snapshot.provider_form_id or "") for item in variants],
    )
    expected = {
        (
            questionnaire_family_variant_id(
                family_id,
                str(variant.language),
                str(variant.snapshot.provider_form_id or ""),
            ),
            question.question_id,
        )
        for variant in variants
        for question in variant.snapshot.canonical_questions
        if question.canonical_type not in _NON_TRANSLATED_QUESTION_TYPES
    }
    return bool(expected) and expected.issubset(semantic)


@dataclass(frozen=True, slots=True)
class GoogleFormsFamilyApi:
    client: GoogleFormsFamilyClient
    snapshot_api: GoogleFormsQuestionnaireSnapshotApi
    snapshot_storage: ResearchSnapshotStorage
    family_storage: FileQuestionnaireFamilyStorage
    semantic_translator: FamilySemanticTranslator = translate_family_variants_with_llm

    def __post_init__(self) -> None:
        if not hasattr(self.client, "fetch_responses"):
            raise TypeError("client 必须支持 fetch_responses")
        if not isinstance(self.snapshot_api, GoogleFormsQuestionnaireSnapshotApi):
            raise TypeError("snapshot_api 类型无效")
        if not isinstance(self.snapshot_storage, ResearchSnapshotStorage):
            raise TypeError("snapshot_storage 类型无效")
        if not isinstance(self.family_storage, FileQuestionnaireFamilyStorage):
            raise TypeError("family_storage 类型无效")
        if not callable(self.semantic_translator):
            raise TypeError("semantic_translator 必须可调用")

    async def create_family(
        self,
        owner_ref: str,
        title: str,
        variants: list[tuple[LanguageCode, str]],
    ) -> QuestionnaireFamilySummary:
        if not owner_ref.strip() or not title.strip() or not variants:
            raise GoogleFormsFamilyInvalidError()
        if len({form_id for _, form_id in variants}) != len(variants):
            raise GoogleFormsFamilyInvalidError()
        if len({str(language) for language, _ in variants}) != len(variants):
            raise GoogleFormsFamilyInvalidError()

        loaded: list[FamilyVariantSnapshot] = []
        try:
            for language, form_id in variants:
                summary = await self.snapshot_api.import_questionnaire(
                    owner_ref,
                    form_id,
                )
                package = await asyncio.to_thread(
                    self.snapshot_storage.load_snapshot_package,
                    owner_ref,
                    summary.snapshot_id,
                )
                if not isinstance(package, SnapshotPackage):
                    raise GoogleFormsFamilyInternalError()
                loaded.append(FamilyVariantSnapshot(
                    language=language,
                    snapshot=package.bundle.snapshot,
                ))
            try:
                translated = await self.semantic_translator(owner_ref, title, loaded)
            except GoogleFormsFamilyMappingUnavailableError:
                if len(loaded) == 1:
                    translated = {}
                else:
                    raise
            except Exception as error:
                if len(loaded) == 1:
                    translated = {}
                else:
                    raise GoogleFormsFamilyMappingUnavailableError() from error
            if len(loaded) > 1 and not _semantic_translation_is_complete(
                owner_ref,
                title,
                loaded,
                translated,
            ):
                raise GoogleFormsFamilyMappingUnavailableError()
            family = build_questionnaire_family(
                owner_ref=owner_ref,
                title=title,
                variants=loaded,
                semantic_questions=translated,
            )
            existing = await asyncio.to_thread(
                self.family_storage.load_family,
                owner_ref,
                family.family_id,
            )
            if existing is not None:
                family = family.model_copy(update={
                    "created_at": existing.created_at,
                })
            await asyncio.to_thread(self.family_storage.save_family, family)
            return family_summary(family)
        except GoogleFormsFamilyApiError:
            raise
        except GoogleFormsQuestionnaireSnapshotApiError as error:
            status_code = (
                401 if isinstance(error, GoogleFormsQuestionnaireAuthRequiredError)
                else 403 if isinstance(error, GoogleFormsQuestionnairePermissionError)
                else 404 if isinstance(error, GoogleFormsQuestionnaireNotFoundError)
                else 503 if isinstance(error, GoogleFormsQuestionnaireRetryableError)
                else None
            )
            raise GoogleFormsFamilyProviderError(status_code=status_code) from error
        except (
            QuestionnaireFamilyStorageError,
            ResearchAssetStorageError,
            TypeError,
            ValueError,
        ) as error:
            raise GoogleFormsFamilyInternalError() from error

    async def get_family(
        self,
        owner_ref: str,
        family_id: str,
    ) -> QuestionnaireFamilySummary:
        try:
            family = await asyncio.to_thread(
                self.family_storage.load_family,
                owner_ref,
                family_id,
            )
        except QuestionnaireFamilyStorageError as error:
            raise GoogleFormsFamilyInternalError() from error
        if family is None:
            raise GoogleFormsFamilyNotFoundError()
        return family_summary(family)

    async def create_analysis_session(
        self,
        owner_ref: str,
        family_id: str,
        login: dict | None,
    ) -> QuestionnaireFamilyAnalysisSessionResponse:
        try:
            family = await asyncio.to_thread(
                self.family_storage.load_family,
                owner_ref,
                family_id,
            )
        except QuestionnaireFamilyStorageError as error:
            raise GoogleFormsFamilyInternalError() from error
        if family is None:
            raise GoogleFormsFamilyNotFoundError()
        if family.status != QuestionnaireFamilyStatus.READY:
            raise GoogleFormsFamilyNeedsReviewError(
                family,
                "问卷家族存在阻断映射问题",
            )
        try:
            captures = await asyncio.gather(*(
                self.client.fetch_responses(owner_ref, variant.provider_form_id)
                for variant in family.variants
            ))
            binding = await asyncio.to_thread(
                bind_google_forms_family_responses,
                family,
                list(captures),
            )
            if binding.blocking_issue_count:
                raise GoogleFormsFamilyNeedsReviewError(
                    family,
                    "回答包含无法映射的原始 questionId",
                )
            if len(binding.rows) <= 1:
                raise GoogleFormsFamilyInvalidError()
            result = await handle_survey_upload(
                binding.questionnaire_filename,
                binding.response_fingerprint.encode("ascii"),
                login,
                bound_questionnaire=binding,
            )
            return QuestionnaireFamilyAnalysisSessionResponse.model_validate(result)
        except GoogleFormsFamilyApiError:
            raise
        except GoogleFormsResponsesClientError as error:
            status_code = error.status_code
            if status_code is None:
                status_code = (
                    401 if error.code in {
                        GoogleFormsResponsesErrorCode.AUTHORIZATION_FAILED,
                        GoogleFormsResponsesErrorCode.AUTHENTICATION_REQUIRED,
                    }
                    else 403 if error.code == GoogleFormsResponsesErrorCode.PERMISSION_DENIED
                    else 404 if error.code == GoogleFormsResponsesErrorCode.FORM_NOT_FOUND
                    else 429 if error.code == GoogleFormsResponsesErrorCode.RATE_LIMITED
                    else 503 if error.retryable
                    else None
                )
            raise GoogleFormsFamilyProviderError(
                status_code=status_code,
            ) from error
        except HTTPException as error:
            if error.status_code in {400, 413, 415, 422}:
                raise GoogleFormsFamilyInvalidError() from error
            raise GoogleFormsFamilyInternalError() from error
        except (TypeError, ValueError) as error:
            raise GoogleFormsFamilyInternalError() from error


__all__ = [
    "GoogleFormsFamilyApi",
    "GoogleFormsFamilyApiError",
    "GoogleFormsFamilyInternalError",
    "GoogleFormsFamilyInvalidError",
    "GoogleFormsFamilyMappingUnavailableError",
    "GoogleFormsFamilyNeedsReviewError",
    "GoogleFormsFamilyNotFoundError",
    "GoogleFormsFamilyProviderError",
    "translate_family_variants_with_llm",
]
