"""Narrow runtime for Google Forms families only."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from app.schemas.questionnaire_source_runtime import QuestionnaireSourceCapabilities
from app.services.google_forms_family_api import (
    GoogleFormsFamilyApi,
    GoogleFormsFamilyClient,
)
from app.services.google_forms_snapshot_api import (
    GoogleFormsCaptureClient,
    GoogleFormsQuestionnaireSnapshotApi,
)
from app.storage.questionnaire_families import FileQuestionnaireFamilyStorage
from app.storage.research_assets import FileResearchAssetStorage


class GoogleFormsRuntimeClient(
    GoogleFormsCaptureClient,
    GoogleFormsFamilyClient,
    Protocol,
):
    """Combined read-only Forms definition and response client."""


@dataclass(frozen=True, slots=True)
class QuestionnaireSourceRuntime:
    storage: FileResearchAssetStorage
    family_storage: FileQuestionnaireFamilyStorage
    google_forms_api: GoogleFormsQuestionnaireSnapshotApi
    google_forms_family_api: GoogleFormsFamilyApi
    capabilities: QuestionnaireSourceCapabilities

    def __post_init__(self) -> None:
        if not isinstance(self.storage, FileResearchAssetStorage):
            raise TypeError("storage 必须是 FileResearchAssetStorage")
        if not isinstance(self.family_storage, FileQuestionnaireFamilyStorage):
            raise TypeError("family_storage 必须是 FileQuestionnaireFamilyStorage")
        if self.family_storage.root != self.storage.root:
            raise ValueError("family_storage 必须共享 runtime.storage 根目录")
        if not isinstance(
            self.google_forms_api,
            GoogleFormsQuestionnaireSnapshotApi,
        ):
            raise TypeError("google_forms_api 类型无效")
        if self.google_forms_api.storage is not self.storage:
            raise ValueError("google_forms_api 必须共享 runtime.storage")
        if not isinstance(self.google_forms_family_api, GoogleFormsFamilyApi):
            raise TypeError("google_forms_family_api 类型无效")
        if self.google_forms_family_api.snapshot_storage is not self.storage:
            raise ValueError("google_forms_family_api 必须共享 runtime.storage")
        if self.google_forms_family_api.family_storage is not self.family_storage:
            raise ValueError("google_forms_family_api 必须共享 runtime.family_storage")
        if self.google_forms_family_api.snapshot_api is not self.google_forms_api:
            raise ValueError("google_forms_family_api 必须共享 google_forms_api")
        if not isinstance(self.capabilities, QuestionnaireSourceCapabilities):
            raise TypeError("capabilities 类型无效")
        if not (
            self.capabilities.google_forms_connection
            and self.capabilities.google_forms_unified_analysis
        ):
            raise ValueError("Google Forms runtime 必须完整启用")


def create_questionnaire_source_runtime(
    storage_root: str | os.PathLike[str],
    *,
    google_forms_client: GoogleFormsRuntimeClient,
) -> QuestionnaireSourceRuntime:
    """Build one owner-isolated storage runtime with no non-Google APIs."""

    if google_forms_client is None:
        raise ValueError("启用 Google Forms 定性入口时必须注入只读客户端")
    storage = FileResearchAssetStorage(storage_root)
    family_storage = FileQuestionnaireFamilyStorage(storage.root)
    snapshot_api = GoogleFormsQuestionnaireSnapshotApi(
        google_forms_client,
        storage,
    )
    family_api = GoogleFormsFamilyApi(
        client=google_forms_client,
        snapshot_api=snapshot_api,
        snapshot_storage=storage,
        family_storage=family_storage,
    )
    return QuestionnaireSourceRuntime(
        storage=storage,
        family_storage=family_storage,
        google_forms_api=snapshot_api,
        google_forms_family_api=family_api,
        capabilities=QuestionnaireSourceCapabilities(
            google_forms_connection=True,
            google_forms_unified_analysis=True,
        ),
    )


__all__ = [
    "GoogleFormsRuntimeClient",
    "QuestionnaireSourceRuntime",
    "create_questionnaire_source_runtime",
]
