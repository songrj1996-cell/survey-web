"""问卷来源运行时对外公开的安全能力契约。"""

from typing import Literal

from pydantic import ConfigDict, field_validator

from app.schemas.research_assets import ContractModel


class QuestionnaireSourceCapabilities(ContractModel):
    """仅声明当前已装配能力，不暴露存储路径或内部依赖。"""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    snapshot_package_upload: Literal[True] = True
    snapshot_catalog: Literal[True] = True
    snapshot_analysis_session: Literal[True] = True
    asset_review_projection: Literal[True] = True
    asset_review_decisions: Literal[True] = True
    bested_original_questionnaire_upload: Literal[True] = True
    screenshot_material_upload: Literal[True] = True
    pdf_material_upload: Literal[True] = True
    google_forms_connection: Literal[False] = False
    source_workflow: Literal[False] = False

    @field_validator("asset_review_decisions", mode="before")
    @classmethod
    def validate_asset_review_decisions(
        cls,
        value: object,
    ) -> object:
        if type(value) is not bool or value is not True:
            raise ValueError("asset_review_decisions 必须是布尔值 true")
        return value
