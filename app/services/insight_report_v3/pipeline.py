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
    select_sample_tables,
)
from app.services.insight_report_v3.audit import (
    audit_report_sections,
    scan_frequency_review_candidates,
)
from app.services.insight_report_v3.chapters import (
    apply_cross_group_capacity_preflight,
    build_stats_catalog,
    ensure_required_group_chapters,
    extract_group_findings,
    flatten_findings,
    plan_chapters,
    retained_group_findings,
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
from app.services.insight_report_v3.research_context import (
    complete_stats_tables, linked_source_context, coverage_ledger,
)


PIPELINE_SCHEMA_VERSION = "insight-report-v3-stage3-v1"
DIAGNOSTICS_SCHEMA_VERSION = "insight-v3-diagnostics-v5-retained-groups"
_LEGACY_FINDINGS_BINDING_FIELDS = (
    "file_sha256",
    "plan_fingerprint",
    "source_registry_fingerprint",
    "frequency_rules_version",
    "findings_prompt_version",
)


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


def _question_progress_fields(
    question_result: dict,
    source_question: dict,
    plan: dict,
) -> dict:
    """Mirror the legacy per-question progress contract for the V3 UI."""
    part_index = source_question.get("part_index")
    parts = list((plan or {}).get("parts") or [])
    part_name = "未分章开放题"
    if (
        type(part_index) is int
        and 1 <= part_index <= len(parts)
        and isinstance(parts[part_index - 1], dict)
    ):
        part_name = str(parts[part_index - 1].get("name") or part_name)
    question_status = str(question_result.get("status") or "")
    return {
        "status": {
            "complete": "completed",
            "degraded": "degraded",
            "skipped": "skipped",
        }.get(question_status, "completed"),
        "step": "completed",
        "question_status": question_status,
        "scope_key": str(question_result.get("scope_key") or ""),
        "part_index": part_index,
        "part_name": part_name,
        "question_name": source_question.get("question") or "开放题",
        "audience": source_question.get("audience") or "",
        "respondent_count": int(source_question.get("respondent_count") or 0),
        "count_unit": "players",
    }


def _binding_matches(
    existing_binding: dict,
    expected_binding: dict,
    *,
    include_findings_input: bool,
) -> bool:
    if not all(
        existing_binding.get(field) == expected_binding.get(field)
        for field in _LEGACY_FINDINGS_BINDING_FIELDS
    ):
        return False
    existing_input = existing_binding.get("findings_input_fingerprint")
    return bool(
        not include_findings_input
        or not existing_input
        or existing_input == expected_binding.get("findings_input_fingerprint")
    )


def _can_reuse_findings(
    existing: dict | None,
    source_contract: dict,
    *,
    baseline_source_contract: dict | None = None,
) -> bool:
    if not (
        isinstance(existing, dict)
        and existing.get("schema_version") == FINDINGS_SCHEMA_VERSION
        and (existing.get("diagnostics") or {}).get("stop_reason") == "all_questions_finished"
        and isinstance(existing.get("source_registry"), dict)
        and isinstance(existing.get("questions"), list)
    ):
        return False
    existing_binding = existing.get("source_binding") or {}
    current_binding = source_contract.get("source_binding") or {}
    if _binding_matches(
        existing_binding,
        current_binding,
        include_findings_input=True,
    ):
        return True
    if not isinstance(baseline_source_contract, dict):
        return False
    baseline_binding = baseline_source_contract.get("source_binding") or {}
    return bool(
        _binding_matches(
            existing_binding,
            baseline_binding,
            include_findings_input=False,
        )
        and baseline_binding.get("findings_input_fingerprint")
        and baseline_binding.get("findings_input_fingerprint")
        == current_binding.get("findings_input_fingerprint")
    )


def _rebind_reused_findings(existing: dict, source_contract: dict) -> dict:
    """Move a source-compatible artifact onto the run-specific plan binding."""
    updated = deepcopy(existing)
    updated["source_binding"] = deepcopy(source_contract["source_binding"])
    updated["source_registry"] = deepcopy(source_contract["source_registry"])
    question_by_scope = {
        str(item.get("scope_key")): item
        for item in source_contract.get("questions") or []
    }
    for result in updated.get("questions") or []:
        source = question_by_scope.get(str(result.get("scope_key")))
        if source is None:
            continue
        for field in (
            "question_key",
            "scope_key",
            "column_indexes",
            "question",
            "audience",
            "respondent_count",
        ):
            result[field] = deepcopy(source[field])
    return updated


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
    rows: list | None = None,
    qualitative_context: dict | None = None,
    existing_findings: dict | None = None,
    baseline_source_contract: dict | None = None,
    report_instruction: str = "",
    planning_instruction: str = "",
    regeneration_diagnostics: dict | None = None,
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
    source_questions = source_contract.get("questions") or []
    item_total = len(source_questions)
    question_by_scope = {
        str(item.get("scope_key")): item
        for item in source_questions
        if isinstance(item, dict)
    }
    yield ("analysis_progress", _analysis_progress(
        "themes",
        "active",
        f"正在逐题提炼 {item_total} 道开放题",
        item_total=item_total,
        item_index=0,
    ))

    reused_findings = _can_reuse_findings(
        existing_findings,
        source_contract,
        baseline_source_contract=baseline_source_contract,
    )
    findings_artifact = (
        _rebind_reused_findings(existing_findings, source_contract)
        if reused_findings else None
    )
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
                source_question = question_by_scope.get(
                    str(payload.get("scope_key")),
                    payload,
                )
                progress_fields = _question_progress_fields(
                    payload,
                    source_question,
                    plan,
                )
                yield ("analysis_progress", _analysis_progress(
                    "themes",
                    progress_fields.pop("status"),
                    f"逐题提炼 {completed_count}/{item_total}：{payload.get('status')}",
                    item_total=item_total,
                    item_index=completed_count,
                    **progress_fields,
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
    complete_tables = complete_stats_tables(stats_md, structured_tables)
    stats_catalog = build_stats_catalog(complete_tables)
    sample_stats = select_sample_tables(stats_catalog, analysis_focus=(plan or {}).get("analysis_focus"))
    writer_registry, source_context_diagnostics = linked_source_context(
        findings_artifact.get("source_registry") or {}, rows=rows, plan=plan,
    )

    yield ("analysis_progress", _analysis_progress(
        "writing",
        "active",
        "正在按分析重点规划章节并准备证据",
        item_total=0,
        item_index=0,
    ))

    cross_chapter_groups = [
        deepcopy(item)
        for item in (regeneration_diagnostics or {}).get("cross_chapter_groups") or []
        if isinstance(item, dict)
    ]
    cross_chapter_requested = bool(
        (regeneration_diagnostics or {}).get("cross_chapter_requested")
        and cross_chapter_groups
    )
    requested_cross_keys = {
        (str(item.get("profile_name") or ""), str(item.get("profile_value") or ""))
        for item in cross_chapter_groups
    }
    group_diagnostics: dict[str, dict] = {}
    group_findings: list[dict] = []
    available_cross_keys: set[tuple[str, str]] = set()

    if cross_chapter_requested:
        cross_factories = [
            lambda index=index, group=group: extract_group_findings(
                chapter={
                    "id": f"cross-g{index:02d}",
                    "group_focus": deepcopy(group),
                },
                source_registry=findings_artifact.get("source_registry") or {},
                plan=plan,
                on_attempt_event=on_attempt_event,
            )
            for index, group in enumerate(cross_chapter_groups, 1)
        ]
        async for event_type, payload in _run_bounded_calls(
            cross_factories,
            min(2, max(1, len(cross_factories))),
        ):
            if event_type == "heartbeat":
                yield ("heartbeat", "")
            elif event_type == "result":
                index, (items, item_diagnostics) = payload
                group = cross_chapter_groups[index]
                key = (
                    str(group.get("profile_name") or ""),
                    str(group.get("profile_value") or ""),
                )
                diagnostic_key = f"cross:{key[0]}={key[1]}"
                group_diagnostics[diagnostic_key] = item_diagnostics
                if items:
                    available_cross_keys.add(key)
                    group_findings.extend(items)

    cross_groups_ready = bool(
        cross_chapter_requested
        and len(requested_cross_keys) == 2
        and available_cross_keys == requested_cross_keys
    )
    planning_findings = [
        *base_findings,
        *(group_findings if cross_groups_ready else []),
    ]

    if planning_findings:
        async for kind, payload in _wait_task_with_heartbeats(plan_chapters(
            findings=planning_findings,
            plan=plan,
            qualitative_context=qualitative_context,
            stats_catalog=stats_catalog,
            profiles=profiles,
            report_instruction=planning_instruction,
            cross_chapter_groups=(cross_chapter_groups if cross_groups_ready else []),
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

    if cross_chapter_requested:
        analysis_focus_diagnostics = {
            "status": "present",
            "message": "已收到跨章节双人群要求，最终执行结果待逐章预检与写作完成后确认",
            "required": deepcopy(cross_chapter_groups),
            "inserted": [],
            "already_present": [],
            "unresolved": (
                [] if cross_groups_ready
                else [{"reason": "group_findings_unavailable"}]
            ),
            "rejected_unrequested_group_chapters": [],
            "fallback_used": not cross_groups_ready,
        }
    else:
        chapter_plan, analysis_focus_diagnostics = ensure_required_group_chapters(
            chapter_plan,
            plan=plan,
            profiles=profiles,
            findings=base_findings,
        )

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
                index, (items, item_diagnostics) = payload
                chapter = group_chapters[index]
                group_diagnostics[chapter["id"]] = item_diagnostics
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
    effective_group_findings = (
        group_findings
        if not cross_chapter_requested or cross_groups_ready
        else []
    )
    all_findings = [*base_findings, *effective_group_findings]
    findings_by_id = {item["id"]: item for item in all_findings}
    for chapter in chapter_plan.get("chapters") or []:
        chapter["analysis_focus"] = deepcopy((plan or {}).get("analysis_focus") or {})
    if cross_chapter_requested:
        chapter_plan, cross_chapter_diagnostics = apply_cross_group_capacity_preflight(
            chapter_plan,
            requested_groups=cross_chapter_groups,
            available_groups=available_cross_keys,
            findings_by_id=findings_by_id,
            source_registry=writer_registry,
            stats_catalog=stats_catalog,
            qualitative_context=qualitative_context,
            report_instruction=report_instruction,
            chapter_plan_status=plan_diagnostics.get("status") or "incomplete",
        )
    else:
        cross_chapter_diagnostics = {
            "status": "not_requested",
            "requested_groups": [],
            "available_groups": [],
            "chapter_count": 0,
            "enabled_chapter_count": 0,
            "fallback_chapter_count": 0,
            "chapters": [],
        }
    yield ("artifacts", {
        "insight_v3_findings": deepcopy(findings_artifact),
        "insight_v3_chapter_plan": deepcopy(chapter_plan),
    })

    written_chapters: list[dict] = []
    sample_interpretation = None
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
            source_registry=writer_registry,
            stats_catalog=stats_catalog,
            profiles=profiles,
            qualitative_context=qualitative_context,
            report_instruction=report_instruction,
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
            source_registry=writer_registry,
            profiles=profiles,
            qualitative_context=qualitative_context,
            report_instruction=report_instruction,
            translation_namespace=translation_namespace,
            on_attempt_event=on_attempt_event,
            stats_catalog=stats_catalog,
            sample_stats=sample_stats,
            analysis_focus=(plan or {}).get("analysis_focus"),
        )):
            yield (kind, payload)
        core = _wait_task_with_heartbeats.result
        core_blocks = core.get("blocks") or []
        core_diagnostics = core.get("diagnostics") or {}
        sample_interpretation = core.get("sample_overview")
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
        report_instruction=report_instruction,
        on_attempt_event=on_attempt_event,
    )):
        yield (kind, payload)
    action_result = _wait_task_with_heartbeats.result
    actions = action_result.get("actions") or []
    action_diagnostics = action_result.get("diagnostics") or {}

    yield ("analysis_progress", _analysis_progress(
        "writing",
        "completed",
        f"{len(written_chapters)} 个分析章节、核心发现与行动建议已完成",
        item_total=len(written_chapters),
        item_index=len(written_chapters),
    ))
    yield ("analysis_progress", _analysis_progress(
        "audit",
        "active",
        "正在核对正文证据、客观统计与研究任务覆盖",
        item_total=0,
        item_index=0,
    ))

    async for kind, payload in _wait_task_with_heartbeats(audit_report_sections(
        core_blocks=core_blocks,
        chapters=written_chapters,
        findings_by_id=findings_by_id,
        source_registry=writer_registry,
        profiles=profiles,
        on_attempt_event=on_attempt_event,
        sample_overview=sample_interpretation,
        research_tasks=chapter_plan.get("research_tasks") or [],
        analysis_focus=(plan or {}).get("analysis_focus") or {},
        actions=actions,
    )):
        yield (kind, payload)
    audit_result = _wait_task_with_heartbeats.result
    actions = audit_result.get("actions", actions)
    core_blocks = audit_result.get("core_blocks") or []
    written_chapters = audit_result.get("chapters") or []
    audit_unresolved = audit_result.get("unresolved") or []
    audit_diagnostics = audit_result.get("diagnostics") or {}
    sample_interpretation = audit_result.get("sample_overview", sample_interpretation)
    audit_status = audit_diagnostics.get("status") or "incomplete"
    audit_progress_status = (
        "degraded" if audit_status in {"needs_review", "incomplete"} else "completed"
    )
    audit_messages = {
        "passed": "语义审校完成，未发现与绑定原文冲突的表述",
        "repaired": "语义审校完成，已定向修复并复核发现的问题",
        "needs_review": f"语义审校完成，仍有 {len(audit_unresolved)} 项需要人工复核",
        "incomplete": f"语义审校未完整执行，仍有 {len(audit_unresolved)} 项需要人工复核",
        "skipped": "没有需要语义审校的模型正文",
    }
    yield ("analysis_progress", _analysis_progress(
        "audit",
        audit_progress_status,
        audit_messages.get(audit_status, audit_messages["incomplete"]),
        audit_status=audit_status,
        unresolved_count=len(audit_unresolved),
        impact=(
            "报告已保存并在标题下方逐项标出待人工复核内容"
            if audit_unresolved else "none"
        ),
    ))
    if audit_diagnostics.get("warned"):
        yield ("progress", "语义审校耗时超过告警阈值，但未中止报告生成。")

    frequency_review = scan_frequency_review_candidates(
        core_blocks=core_blocks,
        chapters=written_chapters,
        actions=actions,
    )

    background_md = render_research_background(qualitative_context)
    sample_md, selected_sample_tables = render_sample_overview(
        stats_md,
        stats_catalog,
        analysis_focus=(plan or {}).get("analysis_focus"),
        interpretation=sample_interpretation,
    )
    research_gaps = deepcopy(analysis_focus_diagnostics.get("unresolved") or [])
    deliverables = ((plan or {}).get("analysis_focus") or {}).get("expected_deliverables") or []
    task_by_index = {item["deliverable_index"]: item for item in chapter_plan.get("research_tasks") or []}
    for index, deliverable in enumerate(deliverables):
        task = task_by_index.get(index)
        if not task or task.get("gap"):
            research_gaps.append({"expected_deliverable": deliverable, "gap": (task or {}).get("gap") or "章节规划未能为该要求建立可核查的材料与章节对应关系"})
    important_coverage = coverage_ledger(all_findings, written_chapters)
    for item in important_coverage:
        if item["required"] and not item["locations"]:
            research_gaps.append({"title": findings_by_id[item["finding_id"]]["claim"], "gap": "该重要材料尚未被正文覆盖，需人工核对"})
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
        audit_unresolved=audit_unresolved,
        research_gaps=research_gaps,
    )

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
            "cross_group_focus": deepcopy(chapter.get("cross_group_focus") or []),
            "cross_group_preflight": deepcopy(
                chapter.get("cross_group_preflight") or {}
            ),
        }
        for chapter in written_chapters
    ]
    if cross_chapter_requested:
        # Extraction, allocation/preflight and final writing are separate facts.
        # In particular, successful extraction must not imply grouped output.
        written_by_id = {item["id"]: item for item in written_chapters}
        final_group_rows = []
        for preflight in cross_chapter_diagnostics["chapters"]:
            written = written_by_id.get(preflight["chapter_id"], {})
            written_keys = set(retained_group_findings(written, findings_by_id).values()) & requested_cross_keys
            enabled = bool(
                preflight["status"] == "enabled"
                and written_keys == requested_cross_keys
            )
            partial = bool(preflight["status"] == "enabled" and written_keys and not enabled)
            final_preflight = written.get("cross_group_preflight") or preflight
            final_group_rows.append({
                "chapter_id": preflight["chapter_id"],
                "title": preflight["title"],
                "status": "enabled" if enabled else "partial" if partial else "ordinary_fallback",
                "retained_groups": [
                    {"profile_name": key[0], "profile_value": key[1]} for key in sorted(written_keys)
                ],
                "missing_groups": [
                    {"profile_name": key[0], "profile_value": key[1]} for key in sorted(requested_cross_keys - written_keys)
                ],
                "failed_block_ids": [block.get("id") for block in written.get("blocks") or [] if block.get("degraded")],
                "reason": "" if enabled else (
                    "group_writing_partial" if partial else final_preflight.get("reason")
                    or (written.get("diagnostics") or {}).get("reason")
                    or "group_writing_unavailable"
                ),
            })
        written_count = sum(item["status"] == "enabled" for item in final_group_rows)
        partial_count = sum(item["status"] == "partial" for item in final_group_rows)
        chapter_count = len(final_group_rows)
        fallback_count = chapter_count - written_count - partial_count
        writing_status = (
            "complete" if chapter_count and written_count == chapter_count
            else "partial" if written_count or partial_count else "degraded"
        )
        cross_chapter_diagnostics.update({
            "writing_status": writing_status,
            "written_group_chapter_count": written_count,
            "written_partial_group_chapter_count": partial_count,
            "written_fallback_chapter_count": fallback_count,
            "written_chapters": final_group_rows,
        })
        analysis_focus_diagnostics.update({
            "message": (
                f"跨章节人群提炼成功 {len(available_cross_keys)}/{len(requested_cross_keys)} 组；"
                f"章节规划状态 {plan_diagnostics.get('status') or 'incomplete'}；"
                f"容量与材料预检启用 {cross_chapter_diagnostics['enabled_chapter_count']}/{chapter_count} 章；"
                f"最终保留双人群视角 {written_count}/{chapter_count} 章，"
                f"{partial_count} 章仅保留部分人群视角，"
                f"{fallback_count} 章退回普通写法"
            ),
            "group_extraction_status": "complete" if cross_groups_ready else "degraded",
            "chapter_plan_status": plan_diagnostics.get("status") or "incomplete",
            "preflight_enabled_chapter_count": cross_chapter_diagnostics["enabled_chapter_count"],
            "written_group_chapter_count": written_count,
            "written_partial_group_chapter_count": partial_count,
            "written_fallback_chapter_count": fallback_count,
            "cross_group_status": writing_status,
            "unresolved": [
                deepcopy(item) for item in final_group_rows
                if item["status"] != "enabled"
            ] or ([] if cross_groups_ready else [{"reason": "group_findings_unavailable"}]),
            "fallback_used": bool(fallback_count or partial_count or not cross_groups_ready or not chapter_count),
        })
    models = list(dict.fromkeys(
        model
        for collection in (
            plan_diagnostics.get("models") or [],
            skew_diagnostics.get("models") or [],
            core_diagnostics.get("models") or [],
            action_diagnostics.get("models") or [],
            (regeneration_diagnostics or {}).get("models") or [],
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
        "regeneration": deepcopy(regeneration_diagnostics or {
            "status": "not_requested",
            "logical_calls": 0,
            "models": [],
        }),
        "questions": _diagnostic_question_rows(findings_artifact),
        "chapter_plan": deepcopy(plan_diagnostics),
        "analysis_focus": deepcopy(analysis_focus_diagnostics),
        "group_chapters": deepcopy(group_diagnostics),
        "cross_chapter_groups": deepcopy(cross_chapter_diagnostics),
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
            "table_keys": [item.get("table_key") for item in selected_sample_tables],
            "interpretation_status": "complete" if sample_interpretation else "fallback",
        },
        "research_quality": {
            "source_context": source_context_diagnostics,
            "stats_table_count": len(stats_catalog),
            "cross_table_count": sum(item.get("kind") == "cross_tab" for item in stats_catalog),
            "tasks": deepcopy(chapter_plan.get("research_tasks") or []),
            "gaps": research_gaps,
            "finding_coverage": important_coverage,
        },
        "audit": deepcopy(audit_diagnostics),
        "frequency_review": deepcopy(frequency_review),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    yield ("artifacts", {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "insight_v3_findings": deepcopy(findings_artifact),
        "insight_v3_chapter_plan": deepcopy(chapter_plan),
        "viewpoint_diagnostics": diagnostics,
        "writer_models_used": models,
        "insight_v3_stats_tables": deepcopy(complete_tables),
    })
    yield ("result", full_report)
