"""问卷来源运行时对外公开的安全能力契约。"""

from typing import Literal

from pydantic import ConfigDict

from app.schemas.research_assets import ContractModel


class QuestionnaireSourceCapabilities(ContractModel):
    """仅声明当前已装配能力，不暴露存储路径或内部依赖。"""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    snapshot_package_upload: Literal[True] = True
    bested_original_questionnaire_upload: Literal[True] = True
    screenshot_material_upload: Literal[True] = True
    pdf_material_upload: Literal[True] = True
    google_forms_connection: Literal[False] = False
    source_workflow: Literal[False] = False
