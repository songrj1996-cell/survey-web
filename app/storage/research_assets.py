"""调研素材存储端口。

第 1 批只定义接口，不提供任何写入 ``data/`` 的实现。
"""

from __future__ import annotations

from typing import NamedTuple, Protocol, runtime_checkable

from app.schemas.questionnaire import QuestionnaireSnapshot
from app.schemas.research_assets import ResearchAssetCollection


class ResearchAssetBundle(NamedTuple):
    """必须作为一个事务整体读取和保存的问卷快照与素材集合。"""

    snapshot: QuestionnaireSnapshot
    collection: ResearchAssetCollection


@runtime_checkable
class ResearchAssetStorage(Protocol):
    """按用户隔离、以聚合为原子边界的最小同步存储端口。

    实现必须拒绝空 ``owner_ref``，并在保存前确认它与
    ``bundle.collection.owner_ref`` 一致；快照与素材集合不得分步提交。
    """

    def load_bundle(
        self,
        owner_ref: str,
        snapshot_id: str,
    ) -> ResearchAssetBundle | None:
        ...

    def save_bundle(
        self,
        owner_ref: str,
        bundle: ResearchAssetBundle,
    ) -> None:
        ...
