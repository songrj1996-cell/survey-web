"""Public capability contract for the Google Forms qualitative entry."""

from typing import Literal

from pydantic import ConfigDict, field_validator

from app.schemas.research_assets import ContractModel


class QuestionnaireSourceCapabilities(ContractModel):
    """Only advertises the two Google capabilities actually mounted."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    google_forms_connection: bool = False
    google_forms_unified_analysis: bool = False

    @field_validator(
        "google_forms_connection",
        "google_forms_unified_analysis",
        mode="before",
    )
    @classmethod
    def validate_strict_boolean_capability(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("capability 必须是布尔值")
        return value
