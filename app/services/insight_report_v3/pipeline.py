"""Three-stage Insight v3 orchestration exposed as an async generator."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import time

from app.core.config import (
    INSIGHT_V3_ACTION_PROMPT_VERSION,
    INSIGHT_V3_CHAPTER_CONCURRENCY,
    INSIGHT_V3_CHAPTER_PROMPT_VERSION,
    LLM_STREAM_HEARTBEAT_SECONDS,
)
from app.services.insight_report_v3.assemble import (
    assemble_report,
    build_evidence_index,
    render_research_background,
    render_sample_overview,
)
from app.services.insight_report_v3.chapters import (
    build_stats_catalog,
    ensure_required_group_chapters,
    extract_group_findings,
    flatten_findings,
    plan_chapters,
    verify_suspected_skews,
    write_chapters,
    write_actions,
    write_core_findings,
)
from app.services.insight_report_v3.contracts import (
    FINDINGS_SCHEMA_VERSION,
    build_stage_source_contract,
)
from app.services.insight_report_v3.findings import generate_insight_findings
from app.services.insight_report_v3.profiles import (
    attach_profile_presence,
    profile_catalog,
)
from app.services.report_engine import _run_bounded_calls


PIPELINE_SCHEMA_VERSION = "insight-report-v3-stage2-v2"
DIAGNOSTICS_SCHEMA_VERSION = "insight-v3-diagnostics-v2"


def _analysis_progress(phase: str, status: str, message: str, **extra) -> dict:
    indexes = {"themes": 1, "writing": 2, "audit": 3, "finalize": 4}
    return {
        "phase": phase,
        "phase_index": indexes[phase],
        "phase_total": 4,
        "status": status,
        "message": message,
        "impact": "none",
        **extra,
    }


def _can_reuse_findings(existing: dict | None, source_contract: dict) -> bool:
    return bool(
        isinstance(existing, dict)
        and existing.get("schema_version") == FINDINGS_SCHEMA_VERSION
        and existing.get("source_binding") == source_contract.get("source_binding")
        and (existing.get("diagnostics") or {}).get("stop_reason") == "all_questions_finished"
        and isinstance(existing.get("source_registry"), dict)
        and isinstance(existing.get("questions"), list)
    )


def _checkpoint_with_question(checkpoint: dict, question_result: dict) -> dict:
    updated = deepcopy(checkpoint)
    by_scope = {
        item["scope_key"]: item
        for item in updated.get("questions") or []
    }
    by_scope[question_result["scope_key"]] = deepcopy(question_result)
    updated["questions"] = list(by_scope.values())
    updated.setdefault("diagnostics", {})["completed_question_count"] = len(by_scope)
    return updated


def _replace_base_findings(artifact: dict, findings: list[dict]) -> dict:
    updated = deepcopy(artifact)
    by_id = {item["id"]: item for item in findings if not item.get("id", "").startswith("group:")}
    for question in updated.get("questions") or []:
        question["findings"] = [
            deepcopy(by_id.get(item.get("id"), item))
            for item in question.get("findings") or []
        ]
    return updated


async def _wait_task_with_heartbeats(coro):
    task = asyncio.create_task(coro)
    try:
        while not task.done():
            done, _ = await asyncio.wait(
                {task},
                timeout=LLM_STREAM_HEARTBEAT_SECONDS,
            )
            if task in done:
                break
            yield ("heartbeat", "")
        _wait_task_with_heartbeats.result = await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _diagnostic_question_rows(findings_artifact: dict) -> list[dict]:
    return [
        {
            "question_key": item.get("question_key"),
            "scope_key": item.get("scope_key"),
            "status": item.get("status"),
            "failure": deepcopy(item.get("failure")),
            "logical_attempts": int((item.get("diagnostics") or {}).get("logical_attempts") or 0),
            "upstream_attempts": int((item.get("diagnostics") or {}).get("upstream_attempts") or 0),
        }
        for item in findings_artifact.get("questions") or []
    ]


async def generate_insight_report_v3(
    *,
    open_text: dict,
    plan: dict,
    headers: list,
    file_sha256: str,
    stats_md: str,
    structured_tables: list[dict],
    qualitative_context: dict | None = None,
    existing_findings: dict | None = None,
    question_concurrency: int | None = None,
    chapter_concurrency: int | None = None,
    translation_namespace: str,
    on_attempt_event=None,
):
    """Yield progress/heartbeat/artifacts/result without owning persistence."""
    started = time.monotonic()
    source_contract = build_stage_source_contract(
        open_text=open_text,
        plan=plan,
        headers=headers,
        file_sha256=file_sha256,
    )
    item_total = len(source_contract.get("questions") or [])
    yield ("analysis_progress", _analysis_progress(
        "themes",
        "active",
        f"正在逐题提炼 {item_total} 道开放题",
        item_total=item_total,
        item_index=0,
    ))

    reused_findings = _can_reuse_findings(existing_findings, source_contract)
    findings_artifact = deepcopy(existing_findings) if reused_findings else None
    if reused_findings:
        yield ("analysis_progress", _analysis_progress(
            "themes",
            "completed",
            f"已复用兼容指纹下完成的 {item_total} 道逐题 findings",
            item_total=item_total,
            item_index=item_total,
            reused=True,
        ))
    else:
        checkpoint = None
        completed_count = 0
        async for event_type, payload in generate_insight_findings(
            open_text=open_text,
            plan=plan,
            headers=headers,
            file_sha256=file_sha256,
            qualitative_context=qualitative_context,
            concurrency=question_concurrency,
            on_attempt_event=on_attempt_event,
        ):
            if event_type == "heartbeat":
                yield ("heartbeat", "")
            elif event_type == "stage_started":
                checkpoint = payload
                yield ("artifacts", {"insight_v3_findings": deepcopy(checkpoint)})
            elif event_type == "question_result":
                completed_count += 1
                checkpoint = _checkpoint_with_question(checkpoint or {}, payload)
                yield ("artifacts", {"insight_v3_findings": deepcopy(checkpoint)})
                yield ("analysis_progress", _analysis_progress(
                    "themes",
                    "active",
                    f"逐题提炼 {completed_count}/{item_total}：{payload.get('status')}",
                    item_total=item_total,
                    item_index=completed_count,
                    question_status=payload.get("status"),
                    scope_key=payload.get("scope_key"),
                ))
            elif event_type == "warning":
                yield ("progress", payload.get("message") or "逐题提炼耗时超过告警阈值，继续运行。")
            elif event_type == "result":
                findings_artifact = payload
        if findings_artifact is None:
            raise RuntimeError("V3 逐题 findings 阶段没有产生最终产物")
        counts = (findings_artifact.get("diagnostics") or {}).get("status_counts") or {}
        theme_status = "degraded" if counts.get("degraded") else "completed"
        yield ("analysis_progress", _analysis_progress(
            "themes",
            theme_status,
            (
                f"逐题提炼完成：{counts.get('complete', 0)} 题成功，"
                f"{counts.get('degraded', 0)} 题降级，{counts.get('skipped', 0)} 题无有效回答"
            ),
            item_total=item_total,
            item_index=item_total,
            impact=(
                "降级题原文将完整直送章节写作，其它题不受影响"
                if counts.get("degraded") else "none"
            ),
        ))

    findings_artifact = attach_profile_presence(findings_artifact, plan=plan)
    yield ("artifacts", {"insight_v3_findings": deepcopy(findings_artifact)})
    base_findings = flatten_findings(findings_artifact)
    profiles = profile_catalog(findings_artifact.get("source_registry") or {}, plan)
    stats_catalog = build_stats_catalog(structured_tables)

    yield ("analysis_progress", _analysis_progress(
        "writing",
        "active",
        "正在按分析重点规划章节并准备证据",
        item_total=0,
        item_index=0,
    ))

    if base_findings:
        async for kind, payload in _wait_task_with_heartbeats(plan_chapters(
            findings=base_findings,
            plan=plan,
            qualitative_context=qualitative_context,
            stats_catalog=stats_catalog,
            profiles=profiles,
            on_attempt_event=on_attempt_event,
        )):
            yield (kind, payload)
        chapter_plan, plan_diagnostics = _wait_task_with_heartbeats.result
    else:
        chapter_plan = {
            "report_title": "观点洞察报告",
            "chapters": [],
            "suspected_skews": [],
        }
        plan_diagnostics = {
            "status": "skipped",
            "logical_attempts": 0,
            "attempts": [],
            "models": [],
            "reason": "no_findings",
        }

    chapter_plan, analysis_focus_diagnostics = ensure_required_group_chapters(
        chapter_plan,
        plan=plan,
        profiles=profiles,
    )

    group_diagnostics: dict[str, dict] = {}
    group_findings: list[dict] = []
    group_chapters = [
        chapter for chapter in chapter_plan.get("chapters") or []
        if isinstance(chapter.get("group_focus"), dict)
    ]
    group_factories = [
        lambda chapter=chapter: extract_group_findings(
            chapter=chapter,
            source_registry=findings_artifact.get("source_registry") or {},
            plan=plan,
            on_attempt_event=on_attempt_event,
        )
        for chapter in group_chapters
    ]
    async for event_type, payload in _run_bounded_calls(
        group_factories,
        min(2, max(1, len(group_factories))),
    ):
        if event_type == "heartbeat":
            yield ("heartbeat", "")
        elif event_type == "result":
            index, (items, diagnostics) = payload
            chapter = group_chapters[index]
            group_diagnostics[chapter["id"]] = diagnostics
            group_findings.extend(items)
            chapter["finding_ids"].extend(item["id"] for item in items)

    if base_findings and chapter_plan.get("suspected_skews"):
        async for kind, payload in _wait_task_with_heartbeats(verify_suspected_skews(
            findings=base_findings,
            suspected_skews=chapter_plan.get("suspected_skews") or [],
            source_contract=source_contract,
            plan=plan,
            on_attempt_event=on_attempt_event,
        )):
            yield (kind, payload)
        base_findings, skew_diagnostics = _wait_task_with_heartbeats.result
    else:
        skew_diagnostics = {
            "requested_count": 0,
            "prefiltered_count": 0,
            "verification_call_count": 0,
            "verified_count": 0,
            "items": [],
            "models": [],
        }

    findings_artifact = _replace_base_findings(findings_artifact, base_findings)
    all_findings = [*base_findings, *group_findings]
    findings_by_id = {item["id"]: item for item in all_findings}
    yield ("artifacts", {
        "insight_v3_findings": deepcopy(findings_artifact),
        "insight_v3_chapter_plan": deepcopy(chapter_plan),
    })

    written_chapters: list[dict] = []
    if chapter_plan.get("chapters"):
        chapter_total = len(chapter_plan["chapters"])
        yield ("analysis_progress", _analysis_progress(
            "writing",
            "active",
            f"章节规划完成，开始并发撰写 {chapter_total} 个分析章节",
            item_total=chapter_total,
            item_index=0,
        ))
        async for event_type, payload in write_chapters(
            chapters=chapter_plan["chapters"],
            findings_by_id=findings_by_id,
            source_registry=findings_artifact.get("source_registry") or {},
            stats_catalog=stats_catalog,
            profiles=profiles,
            qualitative_context=qualitative_context,
            translation_namespace=translation_namespace,
            concurrency=chapter_concurrency,
            on_attempt_event=on_attempt_event,
        ):
            if event_type == "heartbeat":
                yield ("heartbeat", "")
            elif event_type == "chapter_result":
                written_chapters.append(payload)
                yield ("analysis_progress", _analysis_progress(
                    "writing",
                    "active",
                    f"分析章节已完成 {len(written_chapters)}/{chapter_total}",
                    item_total=chapter_total,
                    item_index=len(written_chapters),
                    chapter_id=payload.get("id"),
                    chapter_status=(payload.get("diagnostics") or {}).get("status"),
                ))
            elif event_type == "result":
                written_chapters = payload

        async for kind, payload in _wait_task_with_heartbeats(write_core_findings(
            chapters=written_chapters,
            findings_by_id=findings_by_id,
            source_registry=findings_artifact.get("source_registry") or {},
            profiles=profiles,
            qualitative_context=qualitative_context,
            translation_namespace=translation_namespace,
            on_attempt_event=on_attempt_event,
        )):
            yield (kind, payload)
        core = _wait_task_with_heartbeats.result
        core_blocks = core.get("blocks") or []
        core_diagnostics = core.get("diagnostics") or {}
    else:
        core_blocks = [{
            "id": "no-open-findings",
            "heading": "开放题证据不足",
            "body": "当前确认范围内没有可用于观点提炼的有效开放题回答，本报告仅保留确定性样本与统计信息。",
            "finding_ids": [],
            "evidence": [],
        }]
        core_diagnostics = {
            "status": "skipped",
            "logical_attempts": 0,
            "attempts": [],
            "models": [],
            "reason": "no_findings",
        }

    async for kind, payload in _wait_task_with_heartbeats(write_actions(
        chapters=written_chapters,
        core_blocks=core_blocks,
        findings_by_id=findings_by_id,
        profiles=profiles,
        qualitative_context=qualitative_context,
        on_attempt_event=on_attempt_event,
    )):
        yield (kind, payload)
    action_result = _wait_task_with_heartbeats.result
    actions = action_result.get("actions") or []
    action_diagnostics = action_result.get("diagnostics") or {}

    background_md = render_research_background(qualitative_context)
    sample_md, selected_sample_tables = render_sample_overview(
        stats_md,
        structured_tables,
        analysis_focus=(plan or {}).get("analysis_focus"),
    )
    full_report = assemble_report(
        report_title=chapter_plan.get("report_title") or "观点洞察报告",
        background_md=background_md,
        sample_md=sample_md,
        core_blocks=core_blocks,
        chapters=written_chapters,
        findings_by_id=findings_by_id,
        actions=actions,
        action_note=action_result.get("chapter_note") or "",
        evidence_index=(evidence_index := build_evidence_index(
            core_blocks=core_blocks,
            chapters=written_chapters,
        )),
    )

    yield ("analysis_progress", _analysis_progress(
        "writing",
        "completed",
        f"{len(written_chapters)} 个分析章节、核心发现与行动建议已完成",
        item_total=len(written_chapters),
        item_index=len(written_chapters),
    ))
    yield ("analysis_progress", _analysis_progress(
        "audit",
        "skipped",
        "阶段三语义审校尚未启用；代码证据门禁已执行",
        impact="曲解原文的语义风险仍需阶段三模型审校或人工复核",
    ))

    chapter_rows = [
        {
            "chapter_id": chapter.get("id"),
            "title": chapter.get("title"),
            "status": (chapter.get("diagnostics") or {}).get("status"),
            "logical_attempts": int((chapter.get("diagnostics") or {}).get("logical_attempts") or 0),
            "attempts": deepcopy((chapter.get("diagnostics") or {}).get("attempts") or []),
            "models": list((chapter.get("diagnostics") or {}).get("models") or []),
            "reason": (chapter.get("diagnostics") or {}).get("reason"),
            "evidence_rebound_count": int(
                (chapter.get("diagnostics") or {}).get("evidence_rebound_count") or 0
            ),
            "frequency_rewrite_count": int(
                (chapter.get("diagnostics") or {}).get("frequency_rewrite_count") or 0
            ),
            "frequency_rewrites": deepcopy(
                (chapter.get("diagnostics") or {}).get("frequency_rewrites") or []
            ),
        }
        for chapter in written_chapters
    ]
    models = list(dict.fromkeys(
        model
        for collection in (
            plan_diagnostics.get("models") or [],
            skew_diagnostics.get("models") or [],
            core_diagnostics.get("models") or [],
            action_diagnostics.get("models") or [],
            *(
                (diagnostics.get("models") or [])
                for diagnostics in group_diagnostics.values()
            ),
            *((chapter.get("diagnostics") or {}).get("models") or [] for chapter in written_chapters),
        )
        for model in collection
        if model
    ))
    diagnostics = {
        "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        "engine": "insight_v3",
        "chapter_prompt_version": INSIGHT_V3_CHAPTER_PROMPT_VERSION,
        "findings_reused": reused_findings,
        "questions": _diagnostic_question_rows(findings_artifact),
        "chapter_plan": deepcopy(plan_diagnostics),
        "analysis_focus": deepcopy(analysis_focus_diagnostics),
        "group_chapters": deepcopy(group_diagnostics),
        "skew_verification": deepcopy(skew_diagnostics),
        "chapters": chapter_rows,
        "core": deepcopy(core_diagnostics),
        "actions": {
            **deepcopy(action_diagnostics),
            "prompt_version": INSIGHT_V3_ACTION_PROMPT_VERSION,
            "items": deepcopy(actions),
        },
        "evidence_index": {
            "entry_count": len(evidence_index),
            "reference_count": sum(len(item.get("references") or []) for item in evidence_index),
        },
        "sample_overview": {
            "table_count": len(selected_sample_tables),
            "table_titles": [item.get("title") for item in selected_sample_tables],
        },
        "audit": {"status": "not_enabled", "unresolved_count": 0},
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    yield ("artifacts", {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "insight_v3_findings": deepcopy(findings_artifact),
        "insight_v3_chapter_plan": deepcopy(chapter_plan),
        "viewpoint_diagnostics": diagnostics,
        "writer_models_used": models,
    })
    yield ("result", full_report)
