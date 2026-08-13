"""问卷快照上传与查询接口的安全响应契约。"""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from app.schemas.questionnaire import (
    CollectionState,
    MappingStatus,
    QuestionnaireSourceMode,
)
from app.schemas.research_assets import ContractModel, Provider


GoogleFormId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]


class GoogleFormsSnapshotImportRequest(ContractModel):
    """Google Forms 授权导入请求；授权信息只来自服务端连接。"""

    form_id: GoogleFormId


class QuestionnaireSnapshotSummary(ContractModel):
    """不包含用户标识、原始载荷、媒体字节或存储位置的快照摘要。"""

    schema_version: Literal[1] = 1
    snapshot_id: str = Field(min_length=1)
    provider: Provider
    source_mode: QuestionnaireSourceMode
    collection_state: CollectionState
    mapping_status: MappingStatus
    item_count: int = Field(ge=0)
    question_count: int = Field(ge=0)
    asset_count: int = Field(ge=0)
    image_asset_count: int = Field(ge=0)
    asset_reference_count: int = Field(ge=0)
