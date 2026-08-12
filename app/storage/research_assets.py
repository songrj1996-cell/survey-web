"""调研素材存储端口。

第 1 批只定义接口，不提供任何写入 ``data/`` 的实现。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.questionnaire import QuestionnaireSnapshot
from app.schemas.research_assets import ResearchAssetCollection


@runtime_checkable
class ResearchAssetStorage(Protocol):
    """后续存储实现必须满足的最小同步接口。"""

    def load_questionnaire_snapshot(
        self,
        snapshot_id: str,
    ) -> QuestionnaireSnapshot | None:
        ...

    def save_questionnaire_snapshot(
        self,
        snapshot: QuestionnaireSnapshot,
    ) -> None:
        ...

    def load_asset_collection(
        self,
        document_id: str,
    ) -> ResearchAssetCollection | None:
        ...

    def save_asset_collection(
        self,
        collection: ResearchAssetCollection,
    ) -> None:
        ...
