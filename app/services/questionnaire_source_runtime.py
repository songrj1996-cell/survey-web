"""只装配本地问卷来源能力的显式根目录运行时。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from app.schemas.questionnaire_source_runtime import (
    QuestionnaireSourceCapabilities,
)
from app.services.bested_questionnaire_snapshot_api import (
    BestedQuestionnaireSnapshotApi,
)
from app.services.questionnaire_material_snapshot_api import (
    QuestionnaireMaterialSnapshotApi,
)
from app.services.questionnaire_asset_review_api import (
    QuestionnaireAssetReviewApi,
)
from app.services.questionnaire_pdf_material_snapshot_api import (
    QuestionnairePdfMaterialSnapshotApi,
)
from app.services.questionnaire_snapshot_api import QuestionnaireSnapshotApi
from app.services.questionnaire_snapshot_analysis_api import (
    QuestionnaireSnapshotAnalysisApi,
)
from app.storage.research_assets import FileResearchAssetStorage


@dataclass(frozen=True, slots=True)
class QuestionnaireSourceRuntime:
    """共享一个文件存储、且不包含外部连接器或工作流的运行时。"""

    storage: FileResearchAssetStorage
    snapshot_api: QuestionnaireSnapshotApi
    snapshot_analysis_api: QuestionnaireSnapshotAnalysisApi
    asset_review_api: QuestionnaireAssetReviewApi
    bested_api: BestedQuestionnaireSnapshotApi
    screenshot_material_api: QuestionnaireMaterialSnapshotApi
    pdf_material_api: QuestionnairePdfMaterialSnapshotApi
    capabilities: QuestionnaireSourceCapabilities = field(
        default_factory=QuestionnaireSourceCapabilities,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.storage, FileResearchAssetStorage):
            raise TypeError("storage 必须是 FileResearchAssetStorage")

        api_fields = (
            ("snapshot_api", self.snapshot_api, QuestionnaireSnapshotApi),
            (
                "snapshot_analysis_api",
                self.snapshot_analysis_api,
                QuestionnaireSnapshotAnalysisApi,
            ),
            (
                "asset_review_api",
                self.asset_review_api,
                QuestionnaireAssetReviewApi,
            ),
            ("bested_api", self.bested_api, BestedQuestionnaireSnapshotApi),
            (
                "screenshot_material_api",
                self.screenshot_material_api,
                QuestionnaireMaterialSnapshotApi,
            ),
            (
                "pdf_material_api",
                self.pdf_material_api,
                QuestionnairePdfMaterialSnapshotApi,
            ),
        )
        for name, api, api_type in api_fields:
            if not isinstance(api, api_type):
                raise TypeError(f"{name} 必须是 {api_type.__name__}")
            if api.storage is not self.storage:
                raise ValueError(f"{name} 必须共享 runtime.storage")

        if not isinstance(
            self.capabilities,
            QuestionnaireSourceCapabilities,
        ):
            raise TypeError(
                "capabilities 必须是 QuestionnaireSourceCapabilities"
            )


def create_questionnaire_source_runtime(
    storage_root: str | os.PathLike[str],
) -> QuestionnaireSourceRuntime:
    """从调用方显式路径构造仅包含本地能力的共享存储运行时。"""

    storage = FileResearchAssetStorage(storage_root)
    return QuestionnaireSourceRuntime(
        storage=storage,
        snapshot_api=QuestionnaireSnapshotApi(storage),
        snapshot_analysis_api=QuestionnaireSnapshotAnalysisApi(storage),
        asset_review_api=QuestionnaireAssetReviewApi(storage),
        bested_api=BestedQuestionnaireSnapshotApi(storage),
        screenshot_material_api=QuestionnaireMaterialSnapshotApi(storage),
        pdf_material_api=QuestionnairePdfMaterialSnapshotApi(storage),
    )
