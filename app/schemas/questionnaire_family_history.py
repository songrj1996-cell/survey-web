"""Safe report-history contracts for unified Google Forms families."""

from typing import Literal

from pydantic import Field

from app.schemas.questionnaire_families import LanguageCode
from app.schemas.research_assets import ContractModel


class QuestionnaireFamilyHistoryRef(ContractModel):
    schema_version: Literal[1] = 1
    family_id: str = Field(min_length=1, max_length=128)
    mapping_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    languages: list[LanguageCode] = Field(min_length=1, max_length=10)
    variant_count: int = Field(ge=1, le=10)
    canonical_question_count: int = Field(ge=1)
    duplicate_response_count: int = Field(ge=0)
    unmatched_answer_count: int = Field(ge=0)
    file_upload_answer_count: int = Field(ge=0)


class QuestionnaireFamilyHistoryPayload(ContractModel):
    questionnaire_family_input_kind: Literal["google_forms_family"]
    questionnaire_family_ref: QuestionnaireFamilyHistoryRef


class QuestionnaireFamilyHistorySummary(ContractModel):
    languages: list[LanguageCode] = Field(min_length=1, max_length=10)
    variant_count: int = Field(ge=1, le=10)
    canonical_question_count: int = Field(ge=1)
    duplicate_response_count: int = Field(ge=0)
    unmatched_answer_count: int = Field(ge=0)
    file_upload_answer_count: int = Field(ge=0)


__all__ = [
    "QuestionnaireFamilyHistoryPayload",
    "QuestionnaireFamilyHistoryRef",
    "QuestionnaireFamilyHistorySummary",
]
