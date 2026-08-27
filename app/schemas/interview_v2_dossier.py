"""HTTP contracts for V2 participant dossier checkpoint."""

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InterviewV2DossierGenerateRequest(_Strict):
    project_id: str = Field(min_length=1, max_length=80)
    base_dossier_version_id: str | None = Field(default=None, max_length=80)


class InterviewV2DossierReviewRequest(_Strict):
    project_id: str = Field(min_length=1, max_length=80)
    base_dossier_version_id: str = Field(min_length=1, max_length=80)
    decision: Literal["approved", "needs_changes"]
    note: str = Field(default="", max_length=2000)


class InterviewV2ParticipantListResponse(BaseModel):
    project_id: str
    import_id: str
    status: str
    participants: list[dict[str, Any]]


class InterviewV2DossierResponse(BaseModel):
    project_id: str
    import_id: str
    participant_id: str
    status: str
    dossier_version_id: str | None = None
    dossier_version_number: int = 0
    attributes: dict[str, Any] = Field(default_factory=dict)
    dossier: dict[str, Any] = Field(default_factory=dict)
    source: dict[str, Any] = Field(default_factory=dict)
    review: dict[str, Any] = Field(default_factory=dict)
    model_usage: dict[str, Any] = Field(default_factory=dict)
