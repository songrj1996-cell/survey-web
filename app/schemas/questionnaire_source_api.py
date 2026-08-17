"""问卷快照上传、查询与降级材料接口的安全响应契约。"""

from enum import Enum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from app.schemas.questionnaire import (
    CollectionState,
    MappingStatus,
    QuestionnaireSourceMode,
)
from app.schemas.research_assets import ContractModel, ProcessingStatus, Provider


GoogleFormId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
]

QuestionnaireSnapshotCatalogCursor = Annotated[
    str,
    StringConstraints(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
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


class QuestionnaireSnapshotCatalogResponse(ContractModel):
    """按当前用户隔离、且不暴露存储细节的快照目录。"""

    schema_version: Literal[1] = 1
    items: list[QuestionnaireSnapshotSummary] = Field(max_length=50)
    next_cursor: QuestionnaireSnapshotCatalogCursor | None = None


class QuestionnaireSnapshotAnalysisSessionResponse(ContractModel):
    """复用旧问卷上传结果、但只增加安全快照引用的响应。"""

    session_id: str = Field(
        min_length=32,
        max_length=36,
        pattern=r"^[0-9a-f-]{32,36}$",
    )
    filename: str = Field(min_length=1, max_length=255)
    total_rows: int = Field(ge=1)
    headers: list[str] = Field(min_length=1)
    preview: list[list[str]] = Field(max_length=5)
    source_type: Literal["google", "bested"]
    questionnaire_used: Literal[True] = True
    matched_questions: int = Field(ge=0)
    questionnaire_snapshot_id: str = Field(min_length=1, max_length=1024)


class QuestionnaireMaterialTrustLevel(str, Enum):
    """材料恢复问卷结构时采用的保守可信等级。"""

    LOW = "low"
    MEDIUM = "medium"


SCREENSHOT_MATERIAL_REVIEW_WARNING_CODE = (
    "screenshot_material_requires_review"
)
PDF_MATERIAL_REVIEW_WARNING_CODE = "pdf_material_requires_review"


class QuestionnaireMaterialUploadSummary(ContractModel):
    """不暴露文件名、哈希、路径或原始媒体的材料上传摘要。"""

    schema_version: Literal[1] = 1
    snapshot_id: str = Field(min_length=1)
    provider: Literal[Provider.LOCAL_UPLOAD] = Provider.LOCAL_UPLOAD
    source_mode: Literal[QuestionnaireSourceMode.MATERIAL_UPLOAD] = (
        QuestionnaireSourceMode.MATERIAL_UPLOAD
    )
    mapping_status: Literal[MappingStatus.NEEDS_REVIEW] = (
        MappingStatus.NEEDS_REVIEW
    )
    processing_status: Literal[ProcessingStatus.NEEDS_REVIEW] = (
        ProcessingStatus.NEEDS_REVIEW
    )
    trust_level: Literal[QuestionnaireMaterialTrustLevel.LOW] = (
        QuestionnaireMaterialTrustLevel.LOW
    )
    file_count: int = Field(ge=1)
    total_size_bytes: int = Field(ge=1)
    image_count: int = Field(ge=1)
    requires_human_review: Literal[True] = True
    warning_codes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_material_counts(self) -> "QuestionnaireMaterialUploadSummary":
        if self.file_count != self.image_count:
            raise ValueError("截图材料的 file_count 必须等于 image_count")
        if len(self.warning_codes) != len(set(self.warning_codes)):
            raise ValueError("warning_codes 不能重复")
        if any(not code.strip() for code in self.warning_codes):
            raise ValueError("warning_codes 不能包含空值")
        if self.warning_codes != [SCREENSHOT_MATERIAL_REVIEW_WARNING_CODE]:
            raise ValueError("截图材料必须返回稳定的人工复核告警")
        return self


class QuestionnairePdfMaterialUploadSummary(ContractModel):
    """不暴露文件名、哈希、路径或原始 PDF 的中可信材料摘要。"""

    schema_version: Literal[1] = 1
    snapshot_id: str = Field(min_length=1)
    provider: Literal[Provider.LOCAL_UPLOAD] = Provider.LOCAL_UPLOAD
    source_mode: Literal[QuestionnaireSourceMode.MATERIAL_UPLOAD] = (
        QuestionnaireSourceMode.MATERIAL_UPLOAD
    )
    mapping_status: Literal[MappingStatus.NEEDS_REVIEW] = (
        MappingStatus.NEEDS_REVIEW
    )
    processing_status: Literal[ProcessingStatus.NEEDS_REVIEW] = (
        ProcessingStatus.NEEDS_REVIEW
    )
    trust_level: Literal[QuestionnaireMaterialTrustLevel.MEDIUM] = (
        QuestionnaireMaterialTrustLevel.MEDIUM
    )
    file_count: Literal[1] = 1
    total_size_bytes: int = Field(ge=1)
    document_count: Literal[1] = 1
    image_count: Literal[0] = 0
    page_count: int = Field(ge=1, le=200)
    requires_human_review: Literal[True] = True
    warning_codes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_pdf_warning(self) -> "QuestionnairePdfMaterialUploadSummary":
        if len(self.warning_codes) != len(set(self.warning_codes)):
            raise ValueError("warning_codes 不能重复")
        if any(not code.strip() for code in self.warning_codes):
            raise ValueError("warning_codes 不能包含空值")
        if self.warning_codes != [PDF_MATERIAL_REVIEW_WARNING_CODE]:
            raise ValueError("PDF 材料必须返回稳定的人工复核告警")
        return self
