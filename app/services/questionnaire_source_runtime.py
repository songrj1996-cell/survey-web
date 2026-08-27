"""装配共享问卷快照存储及可选外部只读连接的运行时。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

from app.schemas.questionnaire_source_runtime import (
    QuestionnaireSourceCapabilities,
)
from app.services.bested_questionnaire_snapshot_api import (
    BestedQuestionnaireSnapshotApi,
)
from app.services.google_forms_snapshot_api import (
    GoogleFormsCaptureClient,
    GoogleFormsQuestionnaireSnapshotApi,
)
from app.services.google_forms_family_api import (
    GoogleFormsFamilyApi,
    GoogleFormsFamilyClient,
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
from app.storage.questionnaire_asset_reviews import (
    FileQuestionnaireAssetReviewStorage,
)
from app.storage.questionnaire_families import FileQuestionnaireFamilyStorage
from app.storage.research_assets import FileResearchAssetStorage


class GoogleFormsRuntimeClient(
    GoogleFormsCaptureClient,
    GoogleFormsFamilyClient,
    Protocol,
):
    """Combined read-only client required by the unified Google flow."""


@dataclass(frozen=True, slots=True)
class QuestionnaireSourceRuntime:
    """共享显式根目录，并按注入能力开放外部只读连接。"""

    storage: FileResearchAssetStorage
    review_storage: FileQuestionnaireAssetReviewStorage
    snapshot_api: QuestionnaireSnapshotApi
    snapshot_analysis_api: QuestionnaireSnapshotAnalysisApi
    asset_review_api: QuestionnaireAssetReviewApi
    bested_api: BestedQuestionnaireSnapshotApi
    screenshot_material_api: QuestionnaireMaterialSnapshotApi
    pdf_material_api: QuestionnairePdfMaterialSnapshotApi
    google_forms_api: GoogleFormsQuestionnaireSnapshotApi | None = None
    family_storage: FileQuestionnaireFamilyStorage | None = None
    google_forms_family_api: GoogleFormsFamilyApi | None = None
    capabilities: QuestionnaireSourceCapabilities = field(
        default_factory=QuestionnaireSourceCapabilities,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.storage, FileResearchAssetStorage):
            raise TypeError("storage 必须是 FileResearchAssetStorage")
        if not isinstance(
            self.review_storage,
            FileQuestionnaireAssetReviewStorage,
        ):
            raise TypeError(
                "review_storage 必须是 "
                "FileQuestionnaireAssetReviewStorage"
            )
        if self.review_storage.root != self.storage.root:
            raise ValueError("review_storage 必须共享 runtime.storage 根目录")

        api_fields = (
            ("snapshot_api", self.snapshot_api, QuestionnaireSnapshotApi),
            (
                "snapshot_analysis_api",
                self.snapshot_analysis_api,
                QuestionnaireSnapshotAnalysisApi,
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

        if self.google_forms_api is not None:
            if not isinstance(
                self.google_forms_api,
                GoogleFormsQuestionnaireSnapshotApi,
            ):
                raise TypeError(
                    "google_forms_api 必须是 "
                    "GoogleFormsQuestionnaireSnapshotApi"
                )
            if self.google_forms_api.storage is not self.storage:
                raise ValueError(
                    "google_forms_api 必须共享 runtime.storage"
                )

        if self.family_storage is not None:
            if not isinstance(self.family_storage, FileQuestionnaireFamilyStorage):
                raise TypeError("family_storage 必须是 FileQuestionnaireFamilyStorage")
            if self.family_storage.root != self.storage.root:
                raise ValueError("family_storage 必须共享 runtime.storage 根目录")
        if self.google_forms_family_api is not None:
            if not isinstance(self.google_forms_family_api, GoogleFormsFamilyApi):
                raise TypeError("google_forms_family_api 必须是 GoogleFormsFamilyApi")
            if self.google_forms_family_api.snapshot_storage is not self.storage:
                raise ValueError("google_forms_family_api 必须共享 runtime.storage")
            if self.google_forms_family_api.family_storage is not self.family_storage:
                raise ValueError("google_forms_family_api 必须共享 runtime.family_storage")
            if self.google_forms_family_api.snapshot_api is not self.google_forms_api:
                raise ValueError("google_forms_family_api 必须共享 google_forms_api")

        if not isinstance(
            self.asset_review_api,
            QuestionnaireAssetReviewApi,
        ):
            raise TypeError(
                "asset_review_api 必须是 QuestionnaireAssetReviewApi"
            )
        if self.asset_review_api.storage is not self.storage:
            raise ValueError("asset_review_api 必须共享 runtime.storage")
        if self.asset_review_api.review_storage is not self.review_storage:
            raise ValueError(
                "asset_review_api 必须共享 runtime.review_storage"
            )

        if not isinstance(
            self.capabilities,
            QuestionnaireSourceCapabilities,
        ):
            raise TypeError(
                "capabilities 必须是 QuestionnaireSourceCapabilities"
            )
        if self.capabilities.google_forms_connection != (
            self.google_forms_api is not None
        ):
            raise ValueError(
                "google_forms_connection 必须与 google_forms_api 装配一致"
            )
        if self.capabilities.google_forms_unified_analysis != (
            self.google_forms_family_api is not None
        ):
            raise ValueError(
                "google_forms_unified_analysis 必须与 family API 装配一致"
            )


def create_questionnaire_source_runtime(
    storage_root: str | os.PathLike[str],
    *,
    google_forms_client: GoogleFormsRuntimeClient | None = None,
) -> QuestionnaireSourceRuntime:
    """从显式路径构造共享存储运行时，并按注入开放 Google 读取。"""

    storage = FileResearchAssetStorage(storage_root)
    review_storage = FileQuestionnaireAssetReviewStorage(storage.root)
    family_storage = FileQuestionnaireFamilyStorage(storage.root)
    google_forms_api = (
        GoogleFormsQuestionnaireSnapshotApi(google_forms_client, storage)
        if google_forms_client is not None
        else None
    )
    google_forms_family_api = (
        GoogleFormsFamilyApi(
            client=google_forms_client,
            snapshot_api=google_forms_api,
            snapshot_storage=storage,
            family_storage=family_storage,
        )
        if google_forms_client is not None and google_forms_api is not None
        else None
    )
    return QuestionnaireSourceRuntime(
        storage=storage,
        review_storage=review_storage,
        snapshot_api=QuestionnaireSnapshotApi(storage),
        snapshot_analysis_api=QuestionnaireSnapshotAnalysisApi(storage),
        asset_review_api=QuestionnaireAssetReviewApi(
            storage,
            review_storage,
        ),
        bested_api=BestedQuestionnaireSnapshotApi(storage),
        screenshot_material_api=QuestionnaireMaterialSnapshotApi(storage),
        pdf_material_api=QuestionnairePdfMaterialSnapshotApi(storage),
        google_forms_api=google_forms_api,
        family_storage=family_storage,
        google_forms_family_api=google_forms_family_api,
        capabilities=QuestionnaireSourceCapabilities(
            google_forms_connection=google_forms_client is not None,
            google_forms_unified_analysis=google_forms_family_api is not None,
        ),
    )
