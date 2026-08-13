"""将问卷来源工作流的最终候选连同完整媒体原子保存。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TypeAlias

from app.schemas.questionnaire import QuestionnaireSourceMode
from app.schemas.questionnaire_sources import (
    QuestionnaireAcquisitionRoute,
    QuestionnaireMergeCandidate,
    QuestionnaireSourceNextAction,
    QuestionnaireSourceWorkflowResult,
    QuestionnaireSourceWorkflowStatus,
)
from app.services.questionnaire_source_service import (
    QuestionnaireSourceScopeError,
    save_questionnaire_source_snapshot,
)
from app.services.questionnaire_source_workflow import (
    QuestionnaireSourceStep,
    run_questionnaire_source_workflow,
)
from app.storage.research_assets import (
    ResearchAssetBundle,
    ResearchSnapshotStorage,
    build_snapshot_package,
)


@dataclass(frozen=True, slots=True)
class QuestionnaireMaterializedCandidate:
    """带有按内容哈希索引媒体字节的问卷候选。"""

    candidate: QuestionnaireMergeCandidate
    media: Mapping[str, bytes]

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, QuestionnaireMergeCandidate):
            raise TypeError("candidate 必须是 QuestionnaireMergeCandidate")
        if not isinstance(self.media, Mapping):
            raise TypeError("media 必须是按内容哈希索引的映射")
        object.__setattr__(self, "media", dict(self.media))


QuestionnaireMaterializedLoader: TypeAlias = Callable[
    [],
    QuestionnaireMaterializedCandidate
    | Awaitable[QuestionnaireMaterializedCandidate],
]


@dataclass(frozen=True, slots=True)
class QuestionnaireSourceMaterializedStep:
    """一个能同时取得问卷候选和其媒体字节的惰性来源步骤。"""

    route: QuestionnaireAcquisitionRoute
    source_id: str
    source_mode: QuestionnaireSourceMode
    owner_ref: str
    load: QuestionnaireMaterializedLoader

    def __post_init__(self) -> None:
        if not callable(self.load):
            raise TypeError("load 必须可调用")
        QuestionnaireSourceStep(
            route=self.route,
            source_id=self.source_id,
            source_mode=self.source_mode,
            owner_ref=self.owner_ref,
            load=self.load,
        )


def _capture_loader(
    step: QuestionnaireSourceMaterializedStep,
    captured: dict[
        tuple[QuestionnaireAcquisitionRoute, str],
        QuestionnaireMaterializedCandidate,
    ],
) -> Callable[
    [],
    QuestionnaireMergeCandidate | Awaitable[QuestionnaireMergeCandidate],
]:
    """保留同步/异步 loader 语义，并把候选媒体绑定到完整路径身份。"""

    def capture(
        materialized: QuestionnaireMaterializedCandidate,
    ) -> QuestionnaireMergeCandidate:
        if not isinstance(materialized, QuestionnaireMaterializedCandidate):
            raise TypeError(
                "来源步骤必须返回 QuestionnaireMaterializedCandidate"
            )
        copied = QuestionnaireMaterializedCandidate(
            candidate=materialized.candidate.model_copy(deep=True),
            media=dict(materialized.media),
        )
        captured[(step.route, step.source_id)] = copied
        return copied.candidate

    def load():
        loaded = step.load()
        if inspect.isawaitable(loaded):
            async def await_and_capture() -> QuestionnaireMergeCandidate:
                return capture(await loaded)

            return await_and_capture()
        return capture(loaded)

    return load


def _validate_owner(
    owner_ref: str,
    materialized: QuestionnaireMaterializedCandidate,
) -> None:
    collection = materialized.candidate.collection
    if collection is None:
        raise ValueError("最终问卷候选缺少素材集合")
    if collection.owner_ref != owner_ref or any(
        source.owner_ref != owner_ref
        for source in collection.sources
    ):
        raise QuestionnaireSourceScopeError()


def _validate_media(
    owner_ref: str,
    materialized: QuestionnaireMaterializedCandidate,
) -> None:
    snapshot = materialized.candidate.snapshot
    collection = materialized.candidate.collection
    if snapshot is None or collection is None:
        raise ValueError("最终问卷候选缺少快照聚合")
    build_snapshot_package(
        owner_ref,
        ResearchAssetBundle(snapshot, collection),
        materialized.media,
    )


def _selected_materialized_candidate(
    workflow: QuestionnaireSourceWorkflowResult,
    captured: Mapping[
        tuple[QuestionnaireAcquisitionRoute, str],
        QuestionnaireMaterializedCandidate,
    ],
    owner_ref: str,
) -> QuestionnaireMaterializedCandidate:
    if workflow.result is None or workflow.route is None:
        raise ValueError("resolved 工作流缺少最终问卷结果或路径")
    if len(workflow.result.selected_source_ids) != 1:
        raise ValueError("来源工作流必须只选择一个最终问卷候选")

    source_id = workflow.result.selected_source_ids[0]
    materialized = captured.get((workflow.route, source_id))
    if materialized is None:
        raise ValueError("最终问卷来源没有对应的媒体候选")

    candidate = materialized.candidate
    if candidate.source_id != source_id:
        raise ValueError("最终问卷来源与媒体候选 source_id 不一致")
    if candidate.source_mode != workflow.result.snapshot.source_mode:
        raise ValueError("最终问卷来源与媒体候选 source_mode 不一致")
    if (
        candidate.snapshot != workflow.result.snapshot
        or candidate.collection != workflow.result.collection
    ):
        raise ValueError("最终问卷结果与媒体候选的快照聚合不一致")
    return materialized


def _validate_and_save(
    owner_ref: str,
    workflow: QuestionnaireSourceWorkflowResult,
    materialized: QuestionnaireMaterializedCandidate,
    storage: ResearchSnapshotStorage,
) -> None:
    if workflow.result is None:
        raise ValueError("resolved 工作流缺少最终问卷结果")
    _validate_owner(owner_ref, materialized)
    _validate_media(owner_ref, materialized)
    save_questionnaire_source_snapshot(
        workflow.result,
        materialized.media,
        storage,
    )


async def run_and_persist_questionnaire_source_workflow(
    *,
    owner_ref: str,
    steps: Iterable[QuestionnaireSourceMaterializedStep],
    storage: ResearchSnapshotStorage,
    available_actions: Iterable[QuestionnaireSourceNextAction] = (),
    selected_source_id: str | None = None,
    selection_token: str | None = None,
    response_only: bool = False,
) -> QuestionnaireSourceWorkflowResult:
    """运行既有来源工作流，并只在最终解决后原子保存选中媒体。"""
    declared_steps = list(steps)
    captured: dict[
        tuple[QuestionnaireAcquisitionRoute, str],
        QuestionnaireMaterializedCandidate,
    ] = {}
    workflow_steps: list[QuestionnaireSourceStep] = []
    for step in declared_steps:
        if not isinstance(step, QuestionnaireSourceMaterializedStep):
            raise TypeError(
                "steps 必须包含 QuestionnaireSourceMaterializedStep"
            )
        workflow_steps.append(QuestionnaireSourceStep(
            route=step.route,
            source_id=step.source_id,
            source_mode=step.source_mode,
            owner_ref=step.owner_ref,
            load=_capture_loader(step, captured),
        ))

    workflow = await run_questionnaire_source_workflow(
        owner_ref=owner_ref,
        steps=workflow_steps,
        available_actions=available_actions,
        selected_source_id=selected_source_id,
        selection_token=selection_token,
        response_only=response_only,
    )
    if workflow.status not in {
        QuestionnaireSourceWorkflowStatus.RESOLVED,
        QuestionnaireSourceWorkflowStatus.RESOLVED_PARTIAL,
    }:
        return workflow

    materialized = _selected_materialized_candidate(
        workflow,
        captured,
        owner_ref,
    )
    await asyncio.to_thread(
        _validate_and_save,
        owner_ref,
        workflow,
        materialized,
        storage,
    )
    return workflow
