"""services/survey_service:问卷分析全部业务编排。

包含:上传处理、列题型识别 SSE、方案生成 SSE、方案修订 SSE、统计计算、
报告生成 SSE（大样本/标准两路）、当前会话 QA SSE、历史报告 QA SSE。
HTTP 参数解析与响应包装在 routers/survey。
"""
import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
import time

import crosstab_parser
import survey_plan
import survey_stats


def is_survey_plan_approval(user_text: str) -> bool:
    """用户意见是否表示直接确认方案（不修订）。"""
    return survey_plan.is_user_approval(user_text)
from fastapi import HTTPException, Request

from app.core.config import (
    LARGE_SAMPLE_THRESHOLD,
    LLM_API_KEY,
    LLM_COLUMN_FALLBACK_MODELS,
    LLM_COLUMN_MAX_TOKENS,
    LLM_COLUMN_MODEL,
    LLM_COLUMN_REASONING,
    LLM_PLANNER_FALLBACK_MODELS,
    LLM_PLANNER_MAX_TOKENS,
    LLM_PLANNER_MODEL,
    LLM_PLANNER_REASONING,
    LLM_QA_FALLBACK_MODELS,
    LLM_QA_MAX_TOKENS,
    LLM_QA_MODEL,
    LLM_QA_REASONING,
    LLM_REPORT_MODEL,
    LLM_STREAM_HEARTBEAT_SECONDS,
    MAX_REPORT_VERSIONS,
)
from app.core.parsing import _parse_file
from app.core.responses import sse_event
from app.core.security import (
    _assign_session_owner,
    _find_history_for_login,
    _history_owner_key,
)
from app.core.text import _short_text
from app.integrations.llm_client import collect_chat_completion
from app.schemas.requests import QualitativeContextRequest
from app.services.audit import audit_log
from app.services.analysis_preferences import (
    apply_analysis_preset,
    build_analysis_preset_fingerprint,
    get_analysis_preset_offer,
    save_analysis_preset,
)
from app.services.auth import _current_login
from app.services.branch_logic import infer_branch_rules
from app.services.glossary_service import (
    normalize_glossary_terms,
    prepare_glossary_messages,
)
from app.services.question_detect import (
    _build_column_detect_query,
    _enrich_questions,
    _group_googleform_matrix,
    _heuristic_questions,
    _reconcile_matrix_ranking_questions,
    _reconcile_question_roles,
    _sanitize_choice_options,
)
from app.services.questionnaire_import import (
    apply_questionnaire_translations,
    build_questionnaire_translation_query,
    parse_bested_qualitative_upload,
    parse_questionnaire_translations,
)
from app.services.qualitative_viewpoints import (
    build_viewpoint_diagnostics,
    build_report_viewpoint_stats,
    finalize_viewpoint_diagnostics,
    render_viewpoint_stats,
)
from app.services.report_engine import (
    _batch_qualitative_analysis,
    _build_analysis_approach_block,
    _build_analysis_focus_block,
    _build_analysis_focus_mode_block,
    _build_business_context_block,
    _build_crosstab_plan_revision_query,
    _build_crosstab_planner_query,
    _build_large_sample_writer_query,
    _build_plan_revision_query,
    _build_planner_query_with_confirmed,
    _build_planner_sample,
    _build_qa_context,
    _build_report_generation_instruction_block,
    _build_reused_analysis_preset_block,
    _describe_qa_context_scope,
    _build_writer_action_query,
    _build_writer_action_repair_query,
    _build_comparison_repair_query,
    _build_writer_bug_query,
    _build_writer_core_query,
    _build_writer_core_review_query,
    _build_writer_first_query,
    _build_writer_part_query,
    _normalize_action_section,
    _parse_comparison_repairs,
    _render_crosstab_plan_card,
    _resolve_core_coverage_review,
    _writer_parts_meta,
    MAX_COMPARISON_AUTO_REPAIRS,
)
from app.services.report_history import (
    DEFAULT_RERUN_VERSION_INSTRUCTION,
    _copy_report_version_state,
    append_exact_rerun_to_history,
    append_partial_rerun_to_history,
    delete_history_report_version as _delete_history_report_version,
    find_exact_survey_duplicate_entry,
    find_exact_survey_duplicate_report,
    save_to_history,
    sync_exact_rerun_qa_to_history,
)
from app.services.report_partial_rerun import (
    build_analysis_artifacts,
    build_partial_rerun_source,
    extract_h2_section,
    partial_rerun_capability,
    replace_action_section,
    replace_core_block,
    replace_h2_section,
    resolve_partial_rerun_target,
    scope_tuples_for_keys,
    validate_single_part,
)
from app.services.report_versions import (
    append_report_version,
    delete_report_version,
    normalize_report_versions,
    report_version_summaries,
    resolve_report_version,
    sync_active_report_version,
    update_report_version,
)
from app.services.session_access import require_session_access
from app.services.report_render import _inject_disclaimer, _inject_research_background
from app.services.stats_presentation import (
    inject_qualitative_stats,
    render_qualitative_stats_by_part,
    render_stats_appendix,
)
from app.storage.history import _load_history, mutate_history
from app.storage.analysis_presets import AnalysisPresetStorageError
from app.storage.prompts import (
    _get_column_detect_system_prompt,
    _get_crosstab_planner_system_prompt,
    _get_planner_extra,
    _get_questionnaire_translation_system_prompt,
    _get_report_qa_system_prompt,
    _get_report_writer_system_prompt,
    _get_survey_planner_system_prompt,
)
from app.storage.sessions import get_session, new_session, save_session


_REPORT_GENERATION_LOCKS: dict[str, asyncio.Lock] = {}
_REPORT_RERUN_TARGET_LOCKS: dict[str, asyncio.Lock] = {}
_NON_VERSIONED_REPORT_MODES = {"comment", "interview", "annotate"}
_CHINESE_CHARACTER_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_REPORT_LLM_PHASES = ("themes", "synthesis", "writing", "finalize")


def _empty_report_llm_phase() -> dict:
    return {
        "models_used": [],
        "fallback_models_used": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "call_count": 0,
        "usage_reported_call_count": 0,
        "usage_missing_call_count": 0,
        "active_calls": 0,
        "active_models": {},
    }


class _ReportLLMUsageTracker:
    """汇总一次报告生成中的真实上游模型调用和 usage 快照。"""

    def __init__(self) -> None:
        self._phases = {
            phase: _empty_report_llm_phase() for phase in _REPORT_LLM_PHASES
        }
        self._active_attempts: dict[str, dict[str, dict]] = {
            phase: {} for phase in _REPORT_LLM_PHASES
        }
        self._closed_call_ids: dict[str, set[str]] = {
            phase: set() for phase in _REPORT_LLM_PHASES
        }
        self._legacy_call_sequence = 0
        self._changed = asyncio.Event()

    def callback(self, phase: str):
        if phase not in self._phases:
            raise ValueError(f"unknown report LLM phase: {phase}")

        def _record(event: dict) -> None:
            if not isinstance(event, dict):
                return
            status = str(event.get("status") or "").strip().lower()
            attempted_model = str(
                event.get("model") or event.get("requested_model") or ""
            ).strip()
            phase_usage = self._phases[phase]
            active_attempts = self._active_attempts[phase]
            closed_call_ids = self._closed_call_ids[phase]
            call_id = str(event.get("call_id") or "").strip()

            if status == "started":
                if not call_id:
                    self._legacy_call_sequence += 1
                    call_id = f"legacy:{phase}:{self._legacy_call_sequence}"
                if call_id in active_attempts or call_id in closed_call_ids:
                    return
                active_attempts[call_id] = {
                    "model": attempted_model,
                    "fallback": bool(event.get("fallback")),
                }
                phase_usage["call_count"] += 1
                phase_usage["active_calls"] += 1
                if attempted_model:
                    active_models = phase_usage["active_models"]
                    active_models[attempted_model] = (
                        int(active_models.get(attempted_model) or 0) + 1
                    )
                self._changed.set()
                return

            if status not in {"completed", "failed"}:
                return

            if not call_id:
                for active_call_id, active_attempt in active_attempts.items():
                    if not attempted_model or active_attempt["model"] == attempted_model:
                        call_id = active_call_id
                        break
            if not call_id or call_id in closed_call_ids:
                return
            active_attempt = active_attempts.pop(call_id, None)
            if active_attempt is None:
                return
            closed_call_ids.add(call_id)
            started_model = str(active_attempt.get("model") or "").strip()
            response_model = str(event.get("response_model") or "").strip()
            resolved_model = response_model or attempted_model or started_model

            phase_usage["active_calls"] = max(
                0, int(phase_usage["active_calls"] or 0) - 1
            )
            if started_model:
                active_models = phase_usage["active_models"]
                remaining = max(0, int(active_models.get(started_model) or 0) - 1)
                if remaining:
                    active_models[started_model] = remaining
                else:
                    active_models.pop(started_model, None)
            if resolved_model and resolved_model not in phase_usage["models_used"]:
                phase_usage["models_used"].append(resolved_model)
            if (
                active_attempt.get("fallback")
                and resolved_model
                and resolved_model not in phase_usage["fallback_models_used"]
            ):
                phase_usage["fallback_models_used"].append(resolved_model)

            usage = event.get("usage")
            usage_values = None
            if isinstance(usage, dict):
                values = []
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    value = usage.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        values.append(value)
                    else:
                        break
                if len(values) == 3:
                    usage_values = values

            if usage_values is not None:
                phase_usage["input_tokens"] += usage_values[0]
                phase_usage["output_tokens"] += usage_values[1]
                phase_usage["total_tokens"] += usage_values[2]
                phase_usage["usage_reported_call_count"] += 1
            usage_complete = event.get("usage_complete") is True
            if "usage_complete" not in event and status == "completed":
                # 兼容现有测试桩/调用方；正式客户端始终显式提供完整性标记。
                usage_complete = usage_values is not None
            if usage_values is None or not usage_complete:
                phase_usage["usage_missing_call_count"] += 1
            self._changed.set()

        return _record

    def finalize_open_attempts(self) -> bool:
        """报告结束时把异常遗留的调用闭合为 usage 未知，避免保留活动态。"""
        changed = False
        for phase in _REPORT_LLM_PHASES:
            phase_usage = self._phases[phase]
            active_attempts = self._active_attempts[phase]
            closed_call_ids = self._closed_call_ids[phase]
            for call_id, active_attempt in list(active_attempts.items()):
                attempted_model = str(active_attempt.get("model") or "").strip()
                phase_usage["active_calls"] = max(
                    0, int(phase_usage["active_calls"] or 0) - 1
                )
                if attempted_model:
                    active_models = phase_usage["active_models"]
                    remaining = max(
                        0, int(active_models.get(attempted_model) or 0) - 1
                    )
                    if remaining:
                        active_models[attempted_model] = remaining
                    else:
                        active_models.pop(attempted_model, None)
                    if attempted_model not in phase_usage["models_used"]:
                        phase_usage["models_used"].append(attempted_model)
                    if (
                        active_attempt.get("fallback")
                        and attempted_model not in phase_usage["fallback_models_used"]
                    ):
                        phase_usage["fallback_models_used"].append(attempted_model)
                phase_usage["usage_missing_call_count"] += 1
                closed_call_ids.add(call_id)
                active_attempts.pop(call_id, None)
                changed = True
        if changed:
            self._changed.set()
        return changed

    async def wait_for_change(self) -> None:
        await self._changed.wait()

    def consume_change(self) -> bool:
        if not self._changed.is_set():
            return False
        self._changed.clear()
        return True

    def snapshot(self) -> dict:
        phases = deepcopy(self._phases)
        totals = _empty_report_llm_phase()
        for phase in _REPORT_LLM_PHASES:
            phase_usage = phases[phase]
            for key in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "call_count",
                "usage_reported_call_count",
                "usage_missing_call_count",
                "active_calls",
            ):
                totals[key] += int(phase_usage.get(key) or 0)
            for key in ("models_used", "fallback_models_used"):
                for model in phase_usage.get(key) or []:
                    if model not in totals[key]:
                        totals[key].append(model)
            for model, count in (phase_usage.get("active_models") or {}).items():
                totals["active_models"][model] = (
                    int(totals["active_models"].get(model) or 0) + int(count or 0)
                )
        return {
            "schema_version": 1,
            "phases": phases,
            "totals": totals,
        }
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+")


def _comparison_section(report_md: str, offset: int) -> str:
    headings = list(re.finditer(r"^#{1,6}\s+(.+?)\s*$", report_md[:offset], re.MULTILINE))
    return headings[-1].group(1).strip() if headings else "报告正文"


def _comparison_fact_basis(issue: dict) -> str:
    order = issue.get("expected_order") or []
    return " > ".join(
        f"{item.get('entity')} {float(item.get('value')):.2f}"
        for item in order
        if item.get("entity") is not None and item.get("value") is not None
    )


def _apply_verified_comparison_repairs(
    report_md: str,
    catalog: list[dict],
    initial_result: dict,
    candidates: dict[str, str],
    numeric_sources: str,
) -> tuple[str, dict]:
    """Independently validate and apply safe sentence replacements."""
    initial_issues = list(initial_result.get("issues") or [])
    accepted: list[tuple[dict, str]] = []
    rejected: dict[str, str] = {}
    rejected_by_claim: dict[tuple[str, str], str] = {}

    def reject(issue: dict, reason: str) -> None:
        claim_id = str(issue.get("claim_id") or "")
        rejected[claim_id] = reason
        rejected_by_claim[(
            str(issue.get("group_id") or ""),
            str(issue.get("original_sentence") or "").strip(),
        )] = reason

    for issue in initial_issues:
        claim_id = str(issue.get("claim_id") or "")
        replacement = str(candidates.get(claim_id) or "").strip()
        if not replacement:
            reject(
                issue,
                "未明确绑定量表均值口径，不满足自动修补条件"
                if not issue.get("repairable")
                else "未获得可通过复核的修补句，已保留原文",
            )
            continue
        if replacement == str(issue.get("original_sentence") or "").strip():
            reject(issue, "修补句与原句相同")
            continue
        drifted = survey_stats.find_numbers_not_in_stats(replacement, numeric_sources)
        if drifted:
            reject(issue, f"修补句出现无法由统计源支持的数字：{', '.join(drifted[:5])}")
            continue
        checked = survey_stats.analyze_comparison_claims(replacement, catalog)
        if checked.get("issues"):
            reject(issue, "修补句仍与确定性统计比较关系不一致")
            continue
        if int(checked.get("checked_claim_count") or 0) < 1:
            reject(issue, "修补句删除了原有比较结论，未通过等义复核")
            continue
        accepted.append((issue, replacement))

    repaired_report = report_md
    changes: list[dict] = []
    for issue, replacement in sorted(
        accepted,
        key=lambda item: int(item[0].get("start") or 0),
        reverse=True,
    ):
        start = int(issue["start"])
        end = int(issue["end"])
        original = repaired_report[start:end]
        if original.strip() != str(issue.get("original_sentence") or "").strip():
            reject(issue, "原文定位发生变化，未写入修补")
            continue
        repaired_report = repaired_report[:start] + replacement + repaired_report[end:]
        changes.append({
            "claim_id": issue.get("claim_id"),
            "section": _comparison_section(report_md, start),
            "original": issue.get("original_sentence", ""),
            "replacement": replacement,
            "reasons": deepcopy(issue.get("reasons") or []),
            "factual_basis": _comparison_fact_basis(issue),
            "risk": "已通过确定性比较复核与数字来源复核；建议查看前后文是否仍然自然。",
        })
    changes.reverse()

    final_result = survey_stats.analyze_comparison_claims(repaired_report, catalog)
    unresolved = []
    for issue in final_result.get("issues") or []:
        unresolved.append({
            "claim_id": issue.get("claim_id"),
            "section": _comparison_section(repaired_report, int(issue.get("start") or 0)),
            "original": issue.get("original_sentence", ""),
            "reasons": deepcopy(issue.get("reasons") or []),
            "factual_basis": _comparison_fact_basis(issue),
            "risk": rejected_by_claim.get((
                str(issue.get("group_id") or ""),
                str(issue.get("original_sentence") or "").strip(),
            ))
            or rejected.get(str(issue.get("claim_id") or ""))
            or "该句未能通过自动修补复核，报告保留原文，请人工确认。",
        })
    status = "needs_review" if unresolved else ("repaired" if changes else "passed")
    return repaired_report, {
        "status": status,
        "checked_claim_count": int(final_result.get("checked_claim_count") or 0),
        "detected_count": len(initial_issues),
        "applied_count": len(changes),
        "unresolved_count": len(unresolved),
        "changes": changes,
        "unresolved": unresolved,
        "repair_limit": MAX_COMPARISON_AUTO_REPAIRS,
    }


def _report_generation_lock(session_id: str) -> asyncio.Lock:
    lock = _REPORT_GENERATION_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _REPORT_GENERATION_LOCKS[session_id] = lock
    return lock


def _report_rerun_target_lock(history_id: str) -> asyncio.Lock:
    lock = _REPORT_RERUN_TARGET_LOCKS.get(history_id)
    if lock is None:
        lock = asyncio.Lock()
        _REPORT_RERUN_TARGET_LOCKS[history_id] = lock
    return lock


def _uses_report_versions(source: dict) -> bool:
    return (source.get("mode") or "") not in _NON_VERSIONED_REPORT_MODES


# ── 上传 ────────────────────────────────────────────────────────


def _normalize_questionnaire_translation_texts(
    translations: dict[str, dict],
) -> dict[str, dict]:
    """只规范化确定性翻译的展示字段，保留题号和原值映射。"""
    normalized: dict[str, dict] = {}
    for question_id, item in translations.items():
        copied = dict(item)
        copied["name_zh"] = normalize_glossary_terms(copied.get("name_zh", ""))
        normalized[question_id] = copied
    return normalized


def _questionnaire_titles_are_chinese(questions: list[dict]) -> bool:
    """按中文字符与拉丁词数量判断题干主体语言，保留英文产品名。"""
    source_titles = [
        str(question.get("name_zh") or "").strip()
        for question in questions
        if str(question.get("source_question_id") or "").strip()
    ]
    text = "\n".join(source_titles)
    chinese_units = len(_CHINESE_CHARACTER_RE.findall(text))
    latin_units = len(_LATIN_WORD_RE.findall(text))
    return chinese_units > 0 and chinese_units >= latin_units


def _normalize_question_display_texts(questions: list[dict]) -> list[dict]:
    """只改题目展示短名；选项、矩阵行和列映射仍使用上传原值。"""
    normalized: list[dict] = []
    for question in questions:
        copied = dict(question)
        if isinstance(copied.get("name_zh"), str):
            copied["name_zh"] = normalize_glossary_terms(copied["name_zh"])
        normalized.append(copied)
    return normalized


def _normalize_plan_display_texts(plan: dict) -> dict:
    """规范化方案卡片文案，不改 columns、筛选选项或索引等机器字段。"""
    normalized = dict(plan)
    parts: list = []
    for part in plan.get("parts") or []:
        if not isinstance(part, dict):
            parts.append(part)
            continue
        copied = dict(part)
        for field in ("name", "scope"):
            if isinstance(copied.get(field), str):
                copied[field] = normalize_glossary_terms(copied[field])
        parts.append(copied)
    if isinstance(plan.get("parts"), list):
        normalized["parts"] = parts
    open_questions = plan.get("open_questions")
    if isinstance(open_questions, list):
        normalized["open_questions"] = [
            normalize_glossary_terms(item) if isinstance(item, str) else item
            for item in open_questions
        ]
    if isinstance(plan.get("summary"), str):
        normalized["summary"] = normalize_glossary_terms(plan["summary"])
    focus = plan.get("analysis_focus")
    if isinstance(focus, dict):
        normalized_focus = dict(focus)
        for field in ("core_question", "report_organization", "evidence_role"):
            if isinstance(normalized_focus.get(field), str):
                normalized_focus[field] = normalize_glossary_terms(normalized_focus[field])
        for field in ("supporting_analyses", "expected_deliverables", "avoid_structures"):
            if isinstance(normalized_focus.get(field), list):
                normalized_focus[field] = [
                    normalize_glossary_terms(item) if isinstance(item, str) else item
                    for item in normalized_focus[field]
                ]
        normalized["analysis_focus"] = normalized_focus
    return normalized


async def handle_survey_upload(
    filename: str,
    content: bytes,
    login: dict | None,
    *,
    source_type: str = "google",
    questionnaire_filename: str | None = None,
    questionnaire_content: bytes | None = None,
) -> dict:
    """解析上传文件，创建 session，返回前端所需的 result dict。"""
    if source_type not in {"google", "bested"}:
        raise HTTPException(status_code=400, detail="不支持的数据来源")
    if questionnaire_content and source_type != "bested":
        raise HTTPException(status_code=400, detail="当前仅倍市得来源支持上传调研问卷")

    deterministic_questions: list[dict] | None = None
    questionnaire_text = ""
    matched_questions = 0
    if questionnaire_content:
        q_name = (questionnaire_filename or "").lower()
        if not q_name.endswith((".xls", ".xlsx")):
            raise HTTPException(
                status_code=400,
                detail="调研问卷仅支持倍市得导出的 .xls / .xlsx 文件",
            )
        try:
            imported = parse_bested_qualitative_upload(content, questionnaire_content)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"调研问卷匹配失败：{e}")
        rows = imported["rows"]
        deterministic_questions = imported["questions"]
        questionnaire_text = imported["questionnaire_text"]
        matched_questions = imported["matched_questions"]
    else:
        try:
            rows = _parse_file(filename, content)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    if not rows:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(rows) <= 1:
        raise HTTPException(status_code=400, detail="文件只有表头没有数据行")

    sid = new_session()
    sess = get_session(sid)
    sess["rows"] = rows
    sess["filename"] = filename
    sess["source_type"] = source_type
    sess["file_sha256"] = hashlib.sha256(content).hexdigest()
    sess["questionnaire_sha256"] = (
        hashlib.sha256(questionnaire_content).hexdigest()
        if deterministic_questions is not None and questionnaire_content is not None
        else ""
    )
    sess["questionnaire_used"] = deterministic_questions is not None
    if deterministic_questions is not None:
        sess["columns_detected"] = deterministic_questions
        sess["column_provider"] = "questionnaire"
        sess["questionnaire_text"] = questionnaire_text
        sess["questionnaire_filename"] = questionnaire_filename
    _assign_session_owner(sess, login)
    save_session(sid, sess)

    return {
        "session_id": sid,
        "filename": filename,
        "total_rows": len(rows) - 1,
        "headers": rows[0],
        "preview": rows[1: min(6, len(rows))],
        "source_type": source_type,
        "questionnaire_used": deterministic_questions is not None,
        "matched_questions": matched_questions,
    }


# ── 列题型识别 SSE ───────────────────────────────────────────────


async def _direct_llm_with_heartbeats(messages: list[dict], **kwargs):
    """等待完整直连结果期间发送心跳；只在成功后暴露完整回答。"""
    task = asyncio.create_task(
        collect_chat_completion(prepare_glossary_messages(messages), **kwargs)
    )
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=LLM_STREAM_HEARTBEAT_SECONDS)
            if task in done:
                yield ("result", task.result())
                return
            yield ("heartbeat", None)
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def _run_direct_llm(messages: list[dict], **kwargs):
    """把直连调用包装成供 SSE 流程消费的统一事件。"""
    async for kind, payload in _direct_llm_with_heartbeats(messages, **kwargs):
        if kind == "heartbeat":
            yield sse_event({"type": "heartbeat"}), None
        else:
            yield None, payload


def _json_repair_messages(
    system_prompt: str,
    original_query: str,
    previous_output: str,
    parse_error: str | None,
    repair_instruction: str,
) -> list[dict]:
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": original_query},
        {"role": "assistant", "content": previous_output},
        {
            "role": "user",
            "content": (
                f"上一次输出无法通过校验：{parse_error or '未知解析错误'}。\n"
                f"{repair_instruction}"
            ),
        },
    ]


async def columns_stream(session_id: str, request: Request):
    """LLM 列题型识别 SSE 流程（async generator）。"""
    sess = get_session(session_id)
    rows = sess.get("rows")
    try:
        if sess.get("column_provider") == "questionnaire":
            questions = sess.get("columns_detected") or []
            before_ranking_reconcile = deepcopy(questions)
            questions = _reconcile_matrix_ranking_questions(rows, questions)
            ranking_reconciled = questions != before_ranking_reconcile
            sess["columns_detected"] = questions
            session_saved = False
            if (
                sess.get("questionnaire_translation_status") != "translated"
                and _questionnaire_titles_are_chinese(questions)
            ):
                sess["questionnaire_translation_status"] = "translated"
                sess["questionnaire_translation_model"] = ""
                save_session(session_id, sess)
                session_saved = True
            if sess.get("questionnaire_translation_status") != "translated":
                yield sse_event({
                    "type": "chunk",
                    "content": "原问卷题型与结构已锁定，正在翻译题干、选项和矩阵行。\n",
                })
                query = build_questionnaire_translation_query(questions)
                system_prompt = _get_questionnaire_translation_system_prompt()
                models = (LLM_COLUMN_MODEL, *LLM_COLUMN_FALLBACK_MODELS)
                messages = [
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {"role": "user", "content": query},
                ]
                answer = ""
                used_model = ""
                async for event, result in _run_direct_llm(
                    messages,
                    models=models,
                    max_tokens=LLM_COLUMN_MAX_TOKENS,
                    reasoning_effort=LLM_COLUMN_REASONING or None,
                ):
                    if event:
                        yield event
                    if result:
                        answer, used_model = result
                try:
                    translations = parse_questionnaire_translations(answer, questions)
                except ValueError as exc:
                    retry_messages = _json_repair_messages(
                        system_prompt,
                        query,
                        answer,
                        str(exc),
                        (
                            "请只修复翻译 JSON。不得改变 question_id、数组长度或顺序，"
                            "并确保英文题干已翻译为简体中文。"
                        ),
                    )
                    retry_answer = ""
                    async for event, result in _run_direct_llm(
                        retry_messages,
                        models=models,
                        max_tokens=LLM_COLUMN_MAX_TOKENS,
                        reasoning_effort=LLM_COLUMN_REASONING or None,
                    ):
                        if event:
                            yield event
                        if result:
                            retry_answer, used_model = result
                    translations = parse_questionnaire_translations(
                        retry_answer, questions,
                    )
                translations = _normalize_questionnaire_translation_texts(
                    translations
                )
                questions = apply_questionnaire_translations(
                    questions, translations,
                )
                sess["columns_detected"] = questions
                sess["questionnaire_translation_status"] = "translated"
                sess["questionnaire_translation_model"] = used_model
                save_session(session_id, sess)
                session_saved = True
            if ranking_reconciled and not session_saved:
                save_session(session_id, sess)
            source_text_preserved = not bool(
                sess.get("questionnaire_translation_model")
            )
            yield sse_event({
                "type": "chunk",
                "content": (
                    "已从中文调研问卷读取题型、题干和选项；"
                    "AI 未参与题型判断或文本改写。\n"
                    if source_text_preserved
                    else "已从调研问卷读取题型和结构，并完成中文翻译；"
                    "AI 未参与题型判断。\n"
                ),
            })
            await audit_log(
                request, "survey", "读取问卷题型",
                f"会话：{session_id}；识别题目数：{len(questions)}",
                metadata={
                    "session_id": session_id,
                    "columns": len(questions),
                    "provider": "questionnaire",
                    "translation_model": sess.get(
                        "questionnaire_translation_model", "",
                    ),
                },
            )
            yield sse_event({"type": "columns_ready", "columns": questions})
            return

        groups = _group_googleform_matrix(rows[0])
        query = _build_column_detect_query(rows, groups)
        header_count = len(rows[0])
        system_prompt = _get_column_detect_system_prompt()
        models = (LLM_COLUMN_MODEL, *LLM_COLUMN_FALLBACK_MODELS)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ]
        answer = ""
        used_model = ""
        async for event, result in _run_direct_llm(
            messages,
            models=models,
            max_tokens=LLM_COLUMN_MAX_TOKENS,
            reasoning_effort=LLM_COLUMN_REASONING or None,
        ):
            if event:
                yield event
            if result:
                answer, used_model = result
        for event in _content_events(answer):
            yield event

        questions, err = survey_plan.parse_columns_from_llm(answer, header_count)

        if not questions:
            retry_messages = _json_repair_messages(
                system_prompt,
                query,
                answer,
                err,
                "请严格按 schema 用 ```json``` 围栏重新输出，不要附加任何解释文字。",
            )
            retry_answer = ""
            async for event, result in _run_direct_llm(
                retry_messages,
                models=models,
                max_tokens=LLM_COLUMN_MAX_TOKENS,
                reasoning_effort=LLM_COLUMN_REASONING or None,
            ):
                if event:
                    yield event
                if result:
                    retry_answer, used_model = result
            questions, err = survey_plan.parse_columns_from_llm(retry_answer, header_count)

        if not questions:
            print(f"[columns] LLM 解析失败，回退本地启发式：{err}")
            questions = _heuristic_questions(rows, groups)
            yield sse_event({"type": "chunk", "content": "\n（题型识别解析失败，已回退本地推断，请仔细核对）\n"})

        questions = _normalize_question_display_texts(questions)
        questions = _enrich_questions(questions, rows[0], groups)
        questions = _reconcile_question_roles(rows, questions)
        questions = _reconcile_matrix_ranking_questions(rows, questions)
        questions = _sanitize_choice_options(rows, questions)
        if sess.get("mode") == "crosstab":
            hdrs = rows[0]
            for q in questions:
                idx = q.get("index")
                if isinstance(idx, int) and 0 <= idx < len(hdrs) \
                        and str(hdrs[idx]).strip().endswith("__open"):
                    q["role"] = "open_text"
        sess["columns_detected"] = questions
        sess["column_provider"] = "direct_llm"
        sess["column_model"] = used_model
        save_session(session_id, sess)
        await audit_log(
            request, "survey", "识别题型",
            f"会话：{session_id}；识别列数：{len(questions)}",
            metadata={"session_id": session_id, "columns": len(questions)},
        )
        yield sse_event({"type": "columns_ready", "columns": questions})
    except Exception as e:
        import traceback; traceback.print_exc()
        yield sse_event({"type": "error", "message": str(e)})


# ── 列确认 ──────────────────────────────────────────────────────


def set_survey_columns(session_id: str, columns: list) -> None:
    """存储用户确认后的列题型配置。"""
    sess = get_session(session_id)
    columns = _reconcile_matrix_ranking_questions(sess.get("rows") or [], columns)
    previous_fingerprint = str(
        sess.get("analysis_preference_fingerprint")
        or build_analysis_preset_fingerprint(sess)
        or sess.get("applied_analysis_preset_fingerprint")
        or ""
    ).strip()
    sess["confirmed_columns"] = columns
    sess["branch_rules"] = infer_branch_rules(sess.get("rows") or [], columns)
    _discard_stale_applied_analysis_preset(
        sess,
        previous_fingerprint=previous_fingerprint,
    )
    save_session(session_id, sess)


def _discard_stale_applied_analysis_preset(
    sess: dict,
    *,
    previous_fingerprint: str | None = None,
) -> bool:
    """问卷指纹变化时清除属于旧问卷的预设和修订来源。"""
    current_fingerprint = build_analysis_preset_fingerprint(sess) or ""
    tracked_fingerprint = str(
        previous_fingerprint
        if previous_fingerprint is not None
        else sess.get("analysis_preference_fingerprint")
        or sess.get("applied_analysis_preset_fingerprint")
        or ""
    ).strip()
    changed = False
    if tracked_fingerprint and tracked_fingerprint != current_fingerprint:
        sess["plan_revision_texts"] = []
        for field in (
            "applied_analysis_preset_id",
            "applied_analysis_preset_fingerprint",
            "preset_analysis_focus",
            "preset_plan_revision_texts",
            "current_plan_revision_texts",
        ):
            sess.pop(field, None)
        changed = True

    if current_fingerprint:
        if sess.get("analysis_preference_fingerprint") != current_fingerprint:
            sess["analysis_preference_fingerprint"] = current_fingerprint
            changed = True
    elif sess.pop("analysis_preference_fingerprint", None) is not None:
        changed = True
    return changed


def _ensure_branch_rules(sess: dict) -> list[dict]:
    """兼容功能上线前已创建的 session，并确保 plan 始终携带确定性跳转关系。"""
    if sess.get("mode") == "crosstab":
        return []
    branch_rules = sess.get("branch_rules")
    if not isinstance(branch_rules, list):
        branch_rules = infer_branch_rules(
            sess.get("rows") or [],
            sess.get("confirmed_columns") or [],
        )
        sess["branch_rules"] = branch_rules
    plan = sess.get("plan")
    if isinstance(plan, dict):
        plan["branch_rules"] = branch_rules
    return branch_rules


def _is_focus_capable_planning_session(sess: dict) -> bool:
    """仅标准、非定量且未预计进入大样本路径的会话启用 analysis_focus。"""
    if sess.get("mode") == "crosstab" or sess.get("analysis_mode") == "quantitative":
        return False

    confirmed = sess.get("confirmed_columns")
    columns = confirmed if isinstance(confirmed, list) and confirmed else sess.get("columns_detected")
    open_text_indexes: set[int] = set()
    for column in columns or []:
        if not isinstance(column, dict):
            continue
        if (column.get("role") or column.get("confirmed_type")) != "open_text":
            continue
        indexes = column.get("column_indexes")
        if not isinstance(indexes, list) or not indexes:
            indexes = [column.get("index")]
        open_text_indexes.update(
            index for index in indexes if type(index) is int and index >= 0
        )

    rows = sess.get("rows") or []
    for index in open_text_indexes:
        response_count = 0
        for row in rows[1:]:
            if not isinstance(row, (list, tuple)) or index >= len(row):
                continue
            value = row[index]
            if value is not None and str(value).strip():
                response_count += 1
                if response_count > LARGE_SAMPLE_THRESHOLD:
                    return False
    return True


def get_analysis_preset_offer_for_session(
    session_id: str,
    login: dict | None,
) -> dict | None:
    """查询同 owner、同问卷的可复用分析预设；异常时不阻断主流程。"""
    sess = require_session_access(session_id, login, loader=get_session)
    try:
        return get_analysis_preset_offer(
            sess,
            login,
            eligible=_is_focus_capable_planning_session(sess),
        )
    except AnalysisPresetStorageError as exc:
        print(f"[analysis-preset] WARN offer unavailable: {exc}")
        return None


def apply_analysis_preset_to_session(
    session_id: str,
    login: dict | None,
    preset_id: str,
) -> dict:
    """二次校验预设归属和问卷指纹后，将其合并进当前 session。"""
    sess = require_session_access(session_id, login, loader=get_session)
    try:
        preset = apply_analysis_preset(
            sess,
            login,
            preset_id,
            eligible=_is_focus_capable_planning_session(sess),
        )
    except AnalysisPresetStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not preset:
        raise HTTPException(
            status_code=404,
            detail="该分析预设不存在、无权访问，或已不再匹配当前问卷。",
        )
    save_session(session_id, sess)
    return preset


def confirm_survey_plan(
    session_id: str,
    login: dict | None,
) -> dict:
    """确认当前方案，并在适用时保存同问卷可复用分析预设。"""
    sess = require_session_access(session_id, login, loader=get_session)
    sess["plan_approved_at"] = datetime.now().isoformat(timespec="milliseconds")
    save_session(session_id, sess)
    try:
        preset = save_analysis_preset(
            sess,
            login,
            eligible=_is_focus_capable_planning_session(sess),
        )
    except AnalysisPresetStorageError as exc:
        print(f"[analysis-preset] WARN save failed: {exc}")
        return {
            "approved": True,
            "preset_saved": False,
            "warning": "方案已确认，但本次分析思路没有成功保存为可复用预设。",
        }
    return {
        "approved": True,
        "preset_saved": bool(preset),
        "preset_id": (preset or {}).get("id", ""),
    }


def _report_completion_timing(
    sess: dict,
    *,
    completed_at: datetime | None = None,
) -> dict:
    """Build persisted wall-clock timing from plan approval to report completion."""
    finished = completed_at or datetime.now()
    result = {
        "report_completed_at": finished.isoformat(timespec="milliseconds"),
    }
    approved_text = str(sess.get("plan_approved_at") or "").strip()
    if not approved_text:
        return result
    try:
        approved = datetime.fromisoformat(approved_text)
    except ValueError:
        return result
    if approved.tzinfo != finished.tzinfo:
        return result
    result["plan_approved_at"] = approved_text
    result["report_duration_seconds"] = max(
        0,
        int(round((finished - approved).total_seconds())),
    )
    return result


def save_qualitative_context(
    session_id: str,
    ctx: QualitativeContextRequest,
    login: dict | None = None,
) -> dict | None:
    """存储数据确认上下文，并查找严格匹配的成功历史报告。"""
    sess = require_session_access(session_id, login, loader=get_session)
    _assign_session_owner(sess, login)
    if hasattr(ctx, "model_dump"):
        submitted = ctx.model_dump(exclude_unset=True)
    else:
        submitted = ctx.dict(exclude_unset=True)
    current = sess.get("qualitative_context")
    merged = dict(current) if isinstance(current, dict) else {}
    merged.update(submitted)
    for field in ("problem", "key_concerns", "target_users", "analysis_approach"):
        merged.setdefault(field, "")
    sess["qualitative_context"] = merged
    save_session(session_id, sess)
    return find_exact_survey_duplicate_report(sess, login)


def _next_history_version_number(entry: dict, versions: list[dict]) -> int:
    minimum = max((item["version"] for item in versions), default=0) + 1
    try:
        configured = int(entry.get("next_report_version") or minimum)
    except (TypeError, ValueError):
        configured = minimum
    return max(minimum, configured)


def prepare_duplicate_report_rerun(
    session_id: str,
    login: dict | None,
    *,
    history_id: str,
    instruction: str = "",
    base_version: int | None = None,
) -> dict:
    """Bind a fresh exact-match upload to one existing history card for rerun."""
    sess = require_session_access(session_id, login, loader=get_session)
    _assign_session_owner(sess, login)
    if not sess.get("rows") or not isinstance(sess.get("confirmed_columns"), list):
        raise HTTPException(status_code=400, detail="请先完成数据与题型确认")
    if sess.get("report_md") or normalize_report_versions(sess):
        raise HTTPException(status_code=409, detail="当前任务已经生成过报告，不能改为历史重跑")

    target_id = str(history_id or "").strip()
    if not target_id:
        raise HTTPException(status_code=400, detail="缺少原报告编号")
    if _report_rerun_target_lock(target_id).locked():
        raise HTTPException(status_code=409, detail="原报告正在重新生成，请稍后再试")

    entry = find_exact_survey_duplicate_entry(sess, login, target_id)
    if not entry:
        raise HTTPException(
            status_code=409,
            detail="原报告与当前上传数据或确认信息不再完全一致，请重新确认。",
        )
    plan = entry.get("plan")
    if not isinstance(plan, dict):
        raise HTTPException(status_code=409, detail="原报告缺少可复用的分析方案")

    try:
        versions = normalize_report_versions(entry)
        if len(versions) >= MAX_REPORT_VERSIONS:
            raise HTTPException(
                status_code=409,
                detail=f"报告版本已达上限（{MAX_REPORT_VERSIONS} 个），请先删除一个旧版本。",
            )
        resolved_base = (
            resolve_report_version(entry)["version"]
            if base_version is None
            else resolve_report_version(entry, base_version)["version"]
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    supplement = str(instruction or "").strip()
    version_instruction = supplement or DEFAULT_RERUN_VERSION_INSTRUCTION
    target_version = _next_history_version_number(entry, versions)
    sess["plan"] = deepcopy(plan)
    if isinstance(plan.get("branch_rules"), list):
        sess["branch_rules"] = deepcopy(plan["branch_rules"])
    sess.update({
        "rerun_target_history_id": target_id,
        "rerun_base_version": resolved_base,
        "rerun_supplement": supplement,
        "rerun_instruction": version_instruction,
        "rerun_target_version": target_version,
        "rerun_prepared_at": datetime.now().isoformat(timespec="seconds"),
    })
    save_session(session_id, sess)
    return {
        "ok": True,
        "session_id": session_id,
        "history_id": target_id,
        "base_version": resolved_base,
        "instruction": version_instruction,
        "target_version": target_version,
        "skip_plan": True,
        "plan": deepcopy(plan),
    }


# ── 方案生成 SSE ────────────────────────────────────────────────


async def plan_stream(session_id: str, request: Request):
    """分析方案生成 SSE 流程（async generator）。"""
    sess = get_session(session_id)
    if _discard_stale_applied_analysis_preset(sess):
        save_session(session_id, sess)
    rows = sess.get("rows")
    confirmed_columns = sess.get("confirmed_columns")
    branch_rules = _ensure_branch_rules(sess)
    is_crosstab = sess.get("mode") == "crosstab"
    qualitative_context = sess.get("qualitative_context")
    analysis_focus_enabled = _is_focus_capable_planning_session(sess)
    reused_analysis_focus = sess.get("preset_analysis_focus")
    reused_revision_texts = (
        sess.get("plan_revision_texts")
        if sess.get("applied_analysis_preset_id")
        else []
    )
    planning_context = (
        qualitative_context
        if sess.get("analysis_mode") != "quantitative"
        else None
    )
    try:
        if is_crosstab:
            cols = confirmed_columns or []
            open_names = [c["name"] for c in cols if c.get("role") == "open_text"]
            avail = sess.get("crosstab_questions", [])
            query = _build_crosstab_planner_query(
                sess.get("questionnaire_text", ""),
                avail,
                open_names,
                qualitative_context,
            )
            system_prompt = _get_crosstab_planner_system_prompt()
            models = (LLM_PLANNER_MODEL, *LLM_PLANNER_FALLBACK_MODELS)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ]
            answer = ""
            used_model = ""
            async for event, result in _run_direct_llm(
                messages,
                models=models,
                max_tokens=LLM_PLANNER_MAX_TOKENS,
                reasoning_effort=LLM_PLANNER_REASONING or None,
            ):
                if event:
                    yield event
                if result:
                    answer, used_model = result
            for event in _content_events(answer):
                yield event
            ctp, err = survey_plan.parse_crosstab_plan(answer)
            if not ctp:
                yield sse_event({"type": "progress", "message": "方案格式校验中，正在修订输出…"})
                retry_messages = _json_repair_messages(
                    system_prompt,
                    query,
                    answer,
                    err,
                    "请只输出一个含 parts 和 open_questions 的 JSON 对象，"
                    "用 ```json``` 围栏，不要解释文字。",
                )
                retry_answer = ""
                async for event, result in _run_direct_llm(
                    retry_messages,
                    models=models,
                    max_tokens=LLM_PLANNER_MAX_TOKENS,
                    reasoning_effort=LLM_PLANNER_REASONING or None,
                ):
                    if event:
                        yield event
                    if result:
                        retry_answer, used_model = result
                ctp, err = survey_plan.parse_crosstab_plan(retry_answer)
            if not ctp:
                yield sse_event({"type": "error", "message": f"章节大纲解析失败：{err}"}); return
            ctp = _normalize_plan_display_texts(ctp)
            plan = {
                "mode": "crosstab",
                "columns": cols,
                "parts": ctp["parts"],
                "open_questions": ctp["open_questions"],
                "cross_tabs": [],
            }
            sess["plan"] = plan
            sess["planner_conv_id"] = ""
            sess["planner_provider"] = "direct_llm"
            sess["planner_model"] = used_model
            save_session(session_id, sess)
            card_text = _render_crosstab_plan_card(plan)
            await audit_log(
                request, "survey", "生成章节大纲",
                f"会话：{session_id}；章节数：{len(plan['parts'])}",
                metadata={"session_id": session_id, "parts": len(plan["parts"]), "mode": "crosstab"},
            )
            yield sse_event({"type": "plan_ready", "plan": plan, "card_text": card_text, "headers": rows[0]})
            return

        if confirmed_columns:
            planner_query = _build_planner_query_with_confirmed(
                rows,
                confirmed_columns,
                qualitative_context=planning_context,
                branch_rules=branch_rules,
                analysis_focus_enabled=analysis_focus_enabled,
                reused_analysis_focus=reused_analysis_focus,
                reused_revision_texts=reused_revision_texts,
            )
        else:
            planner_query = (
                _build_planner_sample(rows)
                + "\n\n"
                + _get_planner_extra()
                + _build_business_context_block(
                    planning_context,
                    "用于辅助规划章节结构和分析重点",
                )
                + (
                    _build_analysis_approach_block(
                        planning_context,
                        "必须转译为 plan.analysis_focus",
                    )
                    if analysis_focus_enabled else ""
                )
                + (
                    _build_reused_analysis_preset_block(
                        reused_analysis_focus,
                        reused_revision_texts,
                    )
                    if analysis_focus_enabled else ""
                )
                + _build_analysis_focus_mode_block(analysis_focus_enabled)
            )

        system_prompt = (
            _get_survey_planner_system_prompt()
            + _build_analysis_focus_mode_block(analysis_focus_enabled)
        )
        models = (LLM_PLANNER_MODEL, *LLM_PLANNER_FALLBACK_MODELS)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": planner_query},
        ]
        full_answer = ""
        used_model = ""
        async for event, result in _run_direct_llm(
            messages,
            models=models,
            max_tokens=LLM_PLANNER_MAX_TOKENS,
            reasoning_effort=LLM_PLANNER_REASONING or None,
        ):
            if event:
                yield event
            if result:
                full_answer, used_model = result
        for event in _content_events(full_answer):
            yield event
        headers = rows[0]
        require_analysis_focus = analysis_focus_enabled and bool(
            str((planning_context or {}).get("analysis_approach") or "").strip()
            or reused_analysis_focus
            or reused_revision_texts
        )
        plan, err = survey_plan.parse_plan_from_llm(
            full_answer,
            len(headers),
            require_analysis_focus=require_analysis_focus,
            ignore_analysis_focus=not analysis_focus_enabled,
        )

        if not plan:
            yield sse_event({"type": "progress", "message": "方案格式校验中，正在修订输出…"})
            retry_messages = _json_repair_messages(
                system_prompt,
                planner_query,
                full_answer,
                err,
                "请严格按 JSON schema 重新输出完整 plan，不要附加解释文字。",
            )
            retry_answer = ""
            async for event, result in _run_direct_llm(
                retry_messages,
                models=models,
                max_tokens=LLM_PLANNER_MAX_TOKENS,
                reasoning_effort=LLM_PLANNER_REASONING or None,
            ):
                if event:
                    yield event
                if result:
                    retry_answer, used_model = result
            plan, err = survey_plan.parse_plan_from_llm(
                retry_answer,
                len(headers),
                require_analysis_focus=require_analysis_focus,
                ignore_analysis_focus=not analysis_focus_enabled,
            )

        if not plan:
            yield sse_event({"type": "error", "message": f"方案解析失败：{err}"}); return
        plan = _normalize_plan_display_texts(plan)

        if confirmed_columns:
            plan = survey_plan.merge_confirmed_into_plan(plan, confirmed_columns)
        if not analysis_focus_enabled:
            plan.pop("analysis_focus", None)
        plan["branch_rules"] = branch_rules

        sess["plan"] = plan
        sess["planner_conv_id"] = ""
        sess["planner_provider"] = "direct_llm"
        sess["planner_model"] = used_model
        save_session(session_id, sess)
        card_text = survey_plan.render_plan_for_user(plan, headers)
        await audit_log(
            request, "survey", "生成分析方案",
            f"会话：{session_id}；Part 数：{len(plan.get('parts', []))}",
            metadata={"session_id": session_id, "parts": len(plan.get("parts", []))},
        )
        yield sse_event({"type": "plan_ready", "plan": plan, "card_text": card_text, "headers": headers})
    except Exception as e:
        import traceback; traceback.print_exc()
        yield sse_event({"type": "error", "message": str(e)})


# ── 方案修订 SSE ────────────────────────────────────────────────


def _record_successful_plan_revision(sess: dict, user_text: str) -> None:
    """保留成功修订的完整原文，供同问卷后续任务复用。"""
    text = str(user_text or "").strip()
    if not text:
        return
    revisions = [
        str(item).strip()
        for item in (sess.get("plan_revision_texts") or [])
        if str(item).strip()
    ]
    if not revisions or revisions[-1] != text:
        revisions.append(text)
    sess["plan_revision_texts"] = revisions
    current_revisions = [
        str(item).strip()
        for item in (sess.get("current_plan_revision_texts") or [])
        if str(item).strip()
    ]
    if not current_revisions or current_revisions[-1] != text:
        current_revisions.append(text)
    sess["current_plan_revision_texts"] = current_revisions
    fingerprint = build_analysis_preset_fingerprint(sess)
    if fingerprint:
        sess["analysis_preference_fingerprint"] = fingerprint


async def plan_revision_stream(session_id: str, user_text: str, request: Request):
    """方案修订 SSE 流程（async generator）。"""
    sess = get_session(session_id)
    branch_rules = _ensure_branch_rules(sess)
    plan = sess.get("plan")
    rows = sess.get("rows")
    try:
        if sess.get("mode") == "crosstab":
            cols = sess.get("confirmed_columns") or []
            open_names = [c["name"] for c in cols if c.get("role") == "open_text"]
            rev_q = _build_crosstab_plan_revision_query(
                sess.get("questionnaire_text", ""),
                sess.get("crosstab_questions", []),
                open_names,
                plan.get("parts", []),
                user_text,
                sess.get("qualitative_context"),
            )
            system_prompt = _get_crosstab_planner_system_prompt()
            models = (LLM_PLANNER_MODEL, *LLM_PLANNER_FALLBACK_MODELS)
            answer = ""
            used_model = ""
            async for event, result in _run_direct_llm(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": rev_q},
                ],
                models=models,
                max_tokens=LLM_PLANNER_MAX_TOKENS,
                reasoning_effort=LLM_PLANNER_REASONING or None,
            ):
                if event:
                    yield event
                if result:
                    answer, used_model = result
            for event in _content_events(answer):
                yield event
            ctp, err = survey_plan.parse_crosstab_plan(answer)
            if not ctp:
                yield sse_event({"type": "progress", "message": "方案格式校验中，正在修订输出…"})
                retry_messages = _json_repair_messages(
                    system_prompt,
                    rev_q,
                    answer,
                    err,
                    "请只输出一个含 parts 和 open_questions 的 JSON 对象，"
                    "用 ```json``` 围栏，不要解释文字。",
                )
                retry_answer = ""
                async for event, result in _run_direct_llm(
                    retry_messages,
                    models=models,
                    max_tokens=LLM_PLANNER_MAX_TOKENS,
                    reasoning_effort=LLM_PLANNER_REASONING or None,
                ):
                    if event:
                        yield event
                    if result:
                        retry_answer, used_model = result
                ctp, err = survey_plan.parse_crosstab_plan(retry_answer)
            if not ctp:
                yield sse_event({"type": "error", "message": f"修订章节大纲解析失败：{err}"}); return
            ctp = _normalize_plan_display_texts(ctp)
            new_plan = dict(plan)
            new_plan["parts"] = ctp["parts"]
            new_plan["open_questions"] = ctp["open_questions"]
            sess["plan"] = new_plan
            sess["planner_conv_id"] = ""
            sess["planner_provider"] = "direct_llm"
            sess["planner_model"] = used_model
            _record_successful_plan_revision(sess, user_text)
            save_session(session_id, sess)
            card_text = _render_crosstab_plan_card(new_plan)
            await audit_log(
                request, "survey", "修订章节大纲",
                f"会话：{session_id}；修改意见：{_short_text(user_text)}",
                metadata={"session_id": session_id, "mode": "crosstab"},
            )
            yield sse_event({"type": "plan_ready", "plan": new_plan, "card_text": card_text, "headers": rows[0]})
            return

        headers = rows[0]
        qualitative_context = sess.get("qualitative_context")
        analysis_focus_enabled = _is_focus_capable_planning_session(sess)
        planning_context = (
            qualitative_context
            if sess.get("analysis_mode") != "quantitative"
            else None
        )
        require_analysis_focus = analysis_focus_enabled
        revision_query = _build_plan_revision_query(
            plan,
            headers,
            sess.get("confirmed_columns", []),
            user_text,
            qualitative_context=planning_context,
            branch_rules=branch_rules,
            require_analysis_focus=require_analysis_focus,
        )
        system_prompt = (
            _get_survey_planner_system_prompt()
            + _build_analysis_focus_mode_block(analysis_focus_enabled)
        )
        models = (LLM_PLANNER_MODEL, *LLM_PLANNER_FALLBACK_MODELS)
        full_answer = ""
        used_model = ""
        async for event, result in _run_direct_llm(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": revision_query},
            ],
            models=models,
            max_tokens=LLM_PLANNER_MAX_TOKENS,
            reasoning_effort=LLM_PLANNER_REASONING or None,
        ):
            if event:
                yield event
            if result:
                full_answer, used_model = result
        for event in _content_events(full_answer):
            yield event
        new_plan, err = survey_plan.parse_plan_from_llm(
            full_answer,
            len(headers),
            require_analysis_focus=require_analysis_focus,
            ignore_analysis_focus=not analysis_focus_enabled,
        )
        if not new_plan:
            retry_messages = _json_repair_messages(
                system_prompt,
                revision_query,
                full_answer,
                err,
                "请修正并只返回符合 schema 的完整 plan JSON 对象。",
            )
            yield sse_event({"type": "progress", "message": "方案格式校验中，正在修订输出…"})
            yield sse_event({"type": "chunk", "content": "\n\n正在按严格 JSON 格式重新修订方案...\n"})
            retry_answer = ""
            async for event, result in _run_direct_llm(
                retry_messages,
                models=models,
                max_tokens=LLM_PLANNER_MAX_TOKENS,
                reasoning_effort=LLM_PLANNER_REASONING or None,
            ):
                if event:
                    yield event
                if result:
                    retry_answer, used_model = result
            new_plan, err = survey_plan.parse_plan_from_llm(
                retry_answer,
                len(headers),
                require_analysis_focus=require_analysis_focus,
                ignore_analysis_focus=not analysis_focus_enabled,
            )
        if not new_plan:
            yield sse_event({"type": "error", "message": f"修订方案解析失败：{err}"}); return
        new_plan = _normalize_plan_display_texts(new_plan)

        if sess.get("confirmed_columns"):
            new_plan = survey_plan.merge_confirmed_into_plan(new_plan, sess["confirmed_columns"])
        if not analysis_focus_enabled:
            new_plan.pop("analysis_focus", None)
        new_plan["branch_rules"] = branch_rules

        sess["plan"] = new_plan
        sess["planner_conv_id"] = ""
        sess["planner_provider"] = "direct_llm"
        sess["planner_model"] = used_model
        _record_successful_plan_revision(sess, user_text)
        save_session(session_id, sess)
        card_text = survey_plan.render_plan_for_user(new_plan, headers)
        await audit_log(
            request, "survey", "修订分析方案",
            f"会话：{session_id}；修改意见：{_short_text(user_text)}",
            metadata={"session_id": session_id},
        )
        yield sse_event({"type": "plan_ready", "plan": new_plan, "card_text": card_text, "headers": headers})
    except Exception as e:
        import traceback; traceback.print_exc()
        yield sse_event({"type": "error", "message": str(e)})


# ── 统计计算 ────────────────────────────────────────────────────


async def compute_survey_stats(session_id: str, request: Request) -> str:
    """计算统计数据，写入 session，返回 stats_md。"""
    sess = get_session(session_id)
    _ensure_branch_rules(sess)
    plan = sess.get("plan")
    rows = sess.get("rows")
    if not plan or not rows:
        raise HTTPException(status_code=400, detail="会话状态丢失")
    loop = asyncio.get_event_loop()
    stats_source = sess.get("stats_source") or (
        "external_crosstab" if sess.get("mode") == "crosstab" else "python"
    )
    if stats_source == "external_crosstab":
        stats_md = sess.get("crosstab_md", "")
        open_text = await loop.run_in_executor(None, survey_stats.collect_open_text, rows, plan)
        stats_blocks = crosstab_parser.structured_tables(
            sess.get("crosstab_parsed") or {},
        )
    else:
        stats_md, open_text = await loop.run_in_executor(None, survey_stats.compute, rows, plan)
        stats_blocks = survey_stats.structured_tables(stats_md)
    sess["stats_md"] = stats_md
    sess["stats_blocks"] = stats_blocks
    sess["stats_source"] = stats_source
    sess["open_text"] = open_text
    sess["rows_fed"] = False
    save_session(session_id, sess)
    await audit_log(
        request, "survey", "计算统计",
        f"会话：{session_id}；样本行数：{max(0, len(rows) - 1)}",
        metadata={"session_id": session_id, "rows": max(0, len(rows) - 1)},
    )
    return stats_md


# ── 报告生成 SSE ────────────────────────────────────────────────


def _content_events(text: str, chunk_size: int = 1200):
    """把已完整生成的原子结果分块推给前端，避免单个 SSE 事件过大。"""
    for start in range(0, len(text), chunk_size):
        yield sse_event({"type": "chunk", "content": text[start:start + chunk_size]})


async def _direct_writer_round(
    messages: list[dict],
    query: str,
    *,
    on_attempt_event=None,
) -> tuple[str, str]:
    """直连 LLM 完成一轮写作；成功后才把本轮加入本地对话历史。"""
    user_message = {"role": "user", "content": query}
    answer, model = await collect_chat_completion(
        prepare_glossary_messages([*messages, user_message]),
        on_attempt_event=on_attempt_event,
    )
    answer = normalize_glossary_terms(answer)
    messages.extend([
        user_message,
        {"role": "assistant", "content": answer},
    ])
    return answer, model


def _build_direct_qa_messages(
    source: dict,
    question: str,
    qa_context: str,
) -> list[dict]:
    """把持久化的业务上下文和历史问答转换为标准 LLM messages。"""
    messages = [
        {"role": "system", "content": _get_report_qa_system_prompt()},
        {"role": "user", "content": qa_context},
    ]
    for item in source.get("qa_messages") or []:
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        role = "user" if item.get("role") == "user" else "assistant"
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question.strip()})
    return messages


async def _answer_qa_direct(
    source: dict,
    question: str,
) -> tuple[str, str, str]:
    """通过统一直连模型链回答报告追问，并返回实际使用的模型。"""
    if source.get("rows"):
        qa_context = _build_qa_context(source)
    else:
        qa_context = str(source.get("qa_context_md") or "").strip()
        if not qa_context:
            qa_context = _build_qa_context(source)
    models = (LLM_QA_MODEL, *LLM_QA_FALLBACK_MODELS)
    answer, model = await collect_chat_completion(
        prepare_glossary_messages(
            _build_direct_qa_messages(source, question, qa_context)
        ),
        models=models,
        max_tokens=LLM_QA_MAX_TOKENS,
        reasoning_effort=LLM_QA_REASONING or None,
    )
    return normalize_glossary_terms(answer), model, qa_context


async def report_stream(
    session_id: str,
    request: Request,
    *,
    instruction: str = "",
    base_version: int | None = None,
    generation_kind: str | None = None,
):
    """报告生成 SSE 流程（大样本/标准两路，async generator）。"""
    login = await _current_login(request)
    require_session_access(session_id, login, loader=get_session)
    writer_messages = [
        {"role": "system", "content": _get_report_writer_system_prompt()}
    ]
    writer_models_used: list[str] = []
    report_llm_usage = _ReportLLMUsageTracker()

    def _report_llm_status_event() -> str:
        return sse_event({
            "type": "report_llm_status",
            "report_llm_usage": report_llm_usage.snapshot(),
        })

    generation_lock = _report_generation_lock(session_id)
    rerun_target_lock: asyncio.Lock | None = None
    rerun_target_lock_acquired = False
    if generation_lock.locked():
        yield sse_event({
            "type": "error",
            "message": "当前任务正在生成报告，请等待本次生成完成后再试。",
        })
        return
    await generation_lock.acquire()

    try:
        # 请求可能在锁外等待登录态刷新；拿锁后必须重读，避免旧快照覆盖
        # 刚完成的新版本或追问。
        sess = require_session_access(session_id, login, loader=get_session)
        _assign_session_owner(sess, login)
        if not _uses_report_versions(sess):
            raise ValueError("该报告类型不支持生成报告版本")
        _ensure_branch_rules(sess)
        plan = sess.get("plan")
        rows = sess.get("rows")
        stats_md = sess.get("stats_md")
        open_text = sess.get("open_text", {})
        is_crosstab = sess.get("mode") == "crosstab"
        quantitative_first = (
            sess.get("analysis_mode") == "quantitative" or is_crosstab
        )
        qualitative_context = sess.get("qualitative_context")
        use_large_mode = is_crosstab or any(
            len(value) > LARGE_SAMPLE_THRESHOLD for value in open_text.values()
        )
        analysis_focus = (
            plan.get("analysis_focus")
            if not use_large_mode
            and not quantitative_first
            and isinstance(plan, dict)
            else None
        )
        if not _build_analysis_focus_block(analysis_focus):
            analysis_focus = None
        rerun_history_id = str(sess.get("rerun_target_history_id") or "").strip()
        rerun_entry: dict | None = None
        prompt_instruction = str(instruction or "").strip()
        version_instruction = prompt_instruction
        if rerun_history_id:
            if generation_kind not in (None, "initial"):
                raise ValueError("历史重跑只能从数据确认流程发起")
            if sess.get("rerun_completed_at"):
                raise ValueError("本次历史重跑已经完成，请到原报告查看新版本")
            rerun_target_lock = _report_rerun_target_lock(rerun_history_id)
            if rerun_target_lock.locked():
                raise ValueError("原报告正在重新生成，请等待本次生成完成后再试。")
            await rerun_target_lock.acquire()
            rerun_target_lock_acquired = True
            rerun_entry = find_exact_survey_duplicate_entry(
                sess,
                login,
                rerun_history_id,
            )
            if not rerun_entry:
                raise ValueError("原报告与当前上传数据或确认信息不再完全一致")
            if not isinstance(plan, dict) or plan != rerun_entry.get("plan"):
                raise ValueError("当前分析方案与原报告不一致，请重新确认")
            existing_versions = normalize_report_versions(rerun_entry)
            resolved_kind = "regenerate"
            try:
                resolved_base_version = int(sess.get("rerun_base_version"))
            except (TypeError, ValueError) as exc:
                raise ValueError("历史重跑缺少基础版本") from exc
            resolve_report_version(rerun_entry, resolved_base_version)
            prompt_instruction = str(sess.get("rerun_supplement") or "").strip()
            version_instruction = (
                str(sess.get("rerun_instruction") or "").strip()
                or DEFAULT_RERUN_VERSION_INSTRUCTION
            )
        else:
            existing_versions = normalize_report_versions(sess)
            resolved_kind = generation_kind or (
                "initial" if not existing_versions else "regenerate"
            )
            if resolved_kind not in {"initial", "regenerate"}:
                raise ValueError("不支持的报告生成类型")
            resolved_base_version = base_version
            if resolved_kind == "initial" and existing_versions:
                raise ValueError("当前任务已经有报告，请从数据确认页重新上传后生成。")
            if resolved_kind == "regenerate":
                if not existing_versions:
                    raise ValueError("当前任务还没有可用于重跑的报告版本")
                active_version = resolve_report_version(sess)["version"]
                if resolved_base_version is None:
                    resolved_base_version = active_version
                resolve_report_version(sess, resolved_base_version)

        if len(existing_versions) >= MAX_REPORT_VERSIONS:
            raise ValueError(
                f"报告版本已达上限（{MAX_REPORT_VERSIONS} 个），请先删除一个旧版本。"
            )

        async def _iterate_report_llm_stage(source):
            """让内部生成器事件与即时模型/usage 快照并行向外透传。"""
            iterator = source.__aiter__()
            next_item = asyncio.create_task(iterator.__anext__())
            changed = asyncio.create_task(report_llm_usage.wait_for_change())
            try:
                while True:
                    done, _ = await asyncio.wait(
                        {next_item, changed},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    has_change = changed in done
                    if report_llm_usage.consume_change():
                        has_change = True
                    if has_change:
                        yield "llm_status", _report_llm_status_event()
                        if not changed.done():
                            changed.cancel()
                            with suppress(asyncio.CancelledError):
                                await changed
                        changed = asyncio.create_task(
                            report_llm_usage.wait_for_change()
                        )
                    if next_item not in done:
                        continue
                    try:
                        item = next_item.result()
                    except StopAsyncIteration:
                        return
                    yield "item", item
                    next_item = asyncio.create_task(iterator.__anext__())
            finally:
                for task in (next_item, changed):
                    if not task.done():
                        task.cancel()
                for task in (next_item, changed):
                    with suppress(asyncio.CancelledError, StopAsyncIteration):
                        await task

        async def _writer_call(query: str, *, phase: str = "writing"):
            """等待完整写作轮次时发送轻量心跳，避免 SSE 代理空闲超时。"""
            task = asyncio.create_task(_direct_writer_round(
                writer_messages,
                query,
                on_attempt_event=report_llm_usage.callback(phase),
            ))
            changed = asyncio.create_task(report_llm_usage.wait_for_change())
            try:
                while True:
                    done, _ = await asyncio.wait(
                        {task, changed},
                        timeout=LLM_STREAM_HEARTBEAT_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    has_change = changed in done
                    if report_llm_usage.consume_change():
                        has_change = True
                    if has_change:
                        yield _report_llm_status_event()
                        if not changed.done():
                            changed.cancel()
                            with suppress(asyncio.CancelledError):
                                await changed
                        changed = asyncio.create_task(
                            report_llm_usage.wait_for_change()
                        )
                    if task in done:
                        _writer_call.out = task.result()
                        return
                    if not done:
                        yield sse_event({"type": "heartbeat"})
            finally:
                if not changed.done():
                    changed.cancel()
                    with suppress(asyncio.CancelledError):
                        await changed
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

        def _analysis_progress(phase: str, status: str, message: str, **extra):
            phase_indexes = {
                "themes": 1,
                "synthesis": 2,
                "writing": 3,
                "finalize": 4,
            }
            return sse_event({
                "type": "analysis_progress",
                "phase": phase,
                "phase_index": phase_indexes[phase],
                "phase_total": 4,
                "status": status,
                "message": message,
                "impact": "none",
                **extra,
            })

        def _elapsed_suffix(metrics: dict) -> str:
            seconds = max(0, int(round(float(metrics.get("elapsed_seconds") or 0))))
            if not seconds:
                return ""
            minutes, remaining = divmod(seconds, 60)
            return (
                f"，耗时 {minutes} 分 {remaining} 秒"
                if minutes else f"，耗时 {remaining} 秒"
            )

        viewpoint_stats_md = ""
        viewpoint_diagnostics = build_viewpoint_diagnostics({}, [], "")
        writer_context_included = False
        yield _report_llm_status_event()

        if use_large_mode:
            total_open_text = sum(len(v) for v in open_text.values())
            start_msg = (
                f"跑数表模式：数字取自跑数表，开始对 {total_open_text} 条主观题回复做聚类"
                if is_crosstab
                else "检测到超过500条回复，启用批量处理模式"
            )
            yield _analysis_progress("themes", "active", start_msg)
            clustered_themes: dict = {}
            cluster_diagnostics: dict = {}
            cluster_metrics: dict = {}
            async for stream_kind, item in _iterate_report_llm_stage(
                _batch_qualitative_analysis(
                    open_text,
                    plan,
                    rows[0],
                    session_id,
                    deduplicate_respondents=(
                        not quantitative_first and not is_crosstab
                    ),
                    on_attempt_event=report_llm_usage.callback("themes"),
                )
            ):
                if stream_kind == "llm_status":
                    yield item
                    continue
                if item[0] == "progress":
                    yield sse_event({"type": "progress", "message": item[1]})
                elif item[0] == "analysis_progress":
                    yield sse_event({"type": "analysis_progress", **item[1]})
                elif item[0] == "heartbeat":
                    yield sse_event({"type": "heartbeat"})
                elif item[0] == "diagnostics":
                    cluster_diagnostics = item[1]
                elif item[0] == "analysis_metrics":
                    cluster_metrics = item[1]
                elif item[0] == "result":
                    clustered_themes = item[1]

            failed_cols = [
                d.get("col_name", f"列{k}") for k, d in (cluster_diagnostics or {}).items()
                if d.get("status") == "failed"
            ]
            degraded_cols = [
                d.get("col_name", f"列{k}") for k, d in (cluster_diagnostics or {}).items()
                if d.get("quality_status") == "degraded"
            ]
            latest_session = require_session_access(
                session_id, login, loader=get_session,
            )
            latest_session["open_text_cluster_diagnostics"] = cluster_diagnostics
            latest_session["open_text_cluster_metrics"] = cluster_metrics
            save_session(session_id, latest_session)
            sess = latest_session
            if failed_cols or degraded_cols:
                if failed_cols:
                    msg = f"逐题主题分析完成，{len(failed_cols)} 道题将直接使用全部原文撰写"
                else:
                    msg = f"逐题主题分析完成，其中 {len(degraded_cols)} 道题使用了部分原文兜底"
                msg += _elapsed_suffix(cluster_metrics)
                yield _analysis_progress(
                    "themes",
                    "degraded",
                    msg,
                    impact=(
                        "全部原始回答仍会交给报告写作，但失败题目的主题人数、占比和跨题归纳可能不完整"
                    ),
                )
            else:
                yield _analysis_progress(
                    "themes",
                    "completed",
                    f"逐题主题分析完成，共处理 {len(cluster_diagnostics)} 道题"
                    + _elapsed_suffix(cluster_metrics),
                    elapsed_seconds=cluster_metrics.get("elapsed_seconds", 0),
                    scope_concurrency=cluster_metrics.get("scope_concurrency", 1),
                )

            report_viewpoints: list[dict] = []
            if not quantitative_first and not is_crosstab:
                synthesis_event_seen = False
                async for stream_kind, item in _iterate_report_llm_stage(
                    build_report_viewpoint_stats(
                        clustered_themes,
                        open_text,
                        plan,
                        rows[0],
                        on_attempt_event=report_llm_usage.callback("synthesis"),
                    )
                ):
                    if stream_kind == "llm_status":
                        yield item
                        continue
                    if item[0] == "progress":
                        yield sse_event({"type": "progress", "message": item[1]})
                    elif item[0] == "analysis_progress":
                        synthesis_event_seen = True
                        yield sse_event({"type": "analysis_progress", **item[1]})
                    elif item[0] == "heartbeat":
                        yield sse_event({"type": "heartbeat"})
                    elif item[0] == "result":
                        report_viewpoints = item[1]
                viewpoint_stats_md = render_viewpoint_stats(
                    clustered_themes, report_viewpoints
                )
                if not synthesis_event_seen:
                    yield _analysis_progress(
                        "synthesis",
                        "skipped",
                        "可用题目不足两道，跳过跨题观点归纳",
                    )
            else:
                yield _analysis_progress(
                    "synthesis",
                    "skipped",
                    "当前报告模式不需要跨题观点归纳",
                )

            viewpoint_diagnostics = build_viewpoint_diagnostics(
                clustered_themes,
                report_viewpoints,
                viewpoint_stats_md,
                cluster_diagnostics=cluster_diagnostics,
                cluster_metrics=cluster_metrics,
            )

            yield _analysis_progress(
                "writing",
                "active",
                "主题材料已准备完成，正在撰写报告正文",
                next_steps=["校验并保存"],
            )
            writer_query = _build_large_sample_writer_query(
                stats_md, clustered_themes, plan, rows[0], open_text,
                qualitative_context=qualitative_context,
                quantitative_first=quantitative_first,
                viewpoint_stats_md=viewpoint_stats_md,
            )
            writer_query = (
                _build_report_generation_instruction_block(prompt_instruction)
                + writer_query
            )
            if quantitative_first:
                writer_query = (
                    "<quantitative_report_rule>本报告以客观题统计为主、开放题分析为辅。"
                    "正文必须优先解释关键分布和显著差异；完整逐题统计表将由系统确定性追加，"
                    "不要自行重算或改写表内数字。</quantitative_report_rule>\n\n"
                    + writer_query
                )
            if is_crosstab:
                q_text = (sess.get("questionnaire_text") or "").strip()
                if q_text:
                    if len(q_text) > 8000:
                        q_text = q_text[:8000] + "\n…（问卷过长，已截断）"
                    writer_query = (
                        f"<questionnaire>\n以下是问卷原文（仅供理解题目意图与背景，"
                        f"不要直接搬运）：\n{q_text}\n</questionnaire>\n\n" + writer_query
                    )
            writer_context_included = bool(
                viewpoint_stats_md and viewpoint_stats_md in writer_query
            )
            async for heartbeat in _writer_call(writer_query):
                yield heartbeat
            full_report, model_used = _writer_call.out
            writer_models_used.append(model_used)
            yield _analysis_progress(
                "writing",
                "completed",
                "报告正文已生成，准备校验并保存",
            )
            for event in _content_events(full_report):
                yield event
        else:
            clustered_themes: dict = {}
            cluster_diagnostics: dict = {}
            cluster_metrics: dict = {}
            report_viewpoints: list[dict] = []
            if not quantitative_first and open_text:
                yield _analysis_progress(
                    "themes",
                    "active",
                    "正在逐题提炼主题并按玩家去重统计提及人数",
                )
                async for stream_kind, item in _iterate_report_llm_stage(
                    _batch_qualitative_analysis(
                        open_text,
                        plan,
                        rows[0],
                        session_id,
                        deduplicate_respondents=True,
                        on_attempt_event=report_llm_usage.callback("themes"),
                    )
                ):
                    if stream_kind == "llm_status":
                        yield item
                        continue
                    if item[0] == "progress":
                        yield sse_event({"type": "progress", "message": item[1]})
                    elif item[0] == "analysis_progress":
                        yield sse_event({"type": "analysis_progress", **item[1]})
                    elif item[0] == "heartbeat":
                        yield sse_event({"type": "heartbeat"})
                    elif item[0] == "diagnostics":
                        cluster_diagnostics = item[1]
                    elif item[0] == "analysis_metrics":
                        cluster_metrics = item[1]
                    elif item[0] == "result":
                        clustered_themes = item[1]

                sess["open_text_cluster_diagnostics"] = cluster_diagnostics
                sess["open_text_cluster_metrics"] = cluster_metrics

                failed_count = sum(
                    1 for item in cluster_diagnostics.values()
                    if item.get("status") == "failed"
                )
                degraded_count = sum(
                    1 for item in cluster_diagnostics.values()
                    if item.get("quality_status") == "degraded"
                )
                yield _analysis_progress(
                    "themes",
                    "degraded" if failed_count or degraded_count else "completed",
                    (
                        f"逐题主题分析完成，{failed_count} 道题将使用原文兜底"
                        if failed_count
                        else f"逐题主题分析完成，{degraded_count} 道题使用了部分原文兜底"
                        if degraded_count
                        else f"逐题主题分析完成，共处理 {len(cluster_diagnostics)} 道题"
                    ) + _elapsed_suffix(cluster_metrics),
                    impact=(
                        "原文完整保留，但兜底题目的主题统计和跨题归纳可能不完整"
                        if failed_count or degraded_count else "none"
                    ),
                    elapsed_seconds=cluster_metrics.get("elapsed_seconds", 0),
                    scope_concurrency=cluster_metrics.get("scope_concurrency", 1),
                )

                synthesis_event_seen = False
                async for stream_kind, item in _iterate_report_llm_stage(
                    build_report_viewpoint_stats(
                        clustered_themes,
                        open_text,
                        plan,
                        rows[0],
                        on_attempt_event=report_llm_usage.callback("synthesis"),
                    )
                ):
                    if stream_kind == "llm_status":
                        yield item
                        continue
                    if item[0] == "progress":
                        yield sse_event({"type": "progress", "message": item[1]})
                    elif item[0] == "analysis_progress":
                        synthesis_event_seen = True
                        yield sse_event({"type": "analysis_progress", **item[1]})
                    elif item[0] == "heartbeat":
                        yield sse_event({"type": "heartbeat"})
                    elif item[0] == "result":
                        report_viewpoints = item[1]
                viewpoint_stats_md = render_viewpoint_stats(
                    clustered_themes, report_viewpoints
                )
                if not synthesis_event_seen:
                    yield _analysis_progress(
                        "synthesis",
                        "skipped",
                        "可用题目不足两道，跳过跨题观点归纳",
                    )
            else:
                yield _analysis_progress(
                    "themes",
                    "skipped",
                    "当前报告没有需要提炼的开放题",
                )
                yield _analysis_progress(
                    "synthesis",
                    "skipped",
                    "没有逐题主题可用于跨题观点归纳",
                )

            viewpoint_diagnostics = build_viewpoint_diagnostics(
                clustered_themes,
                report_viewpoints,
                viewpoint_stats_md,
                cluster_diagnostics=cluster_diagnostics,
                cluster_metrics=cluster_metrics,
            )

            yield _analysis_progress(
                "writing",
                "active",
                "分析材料已准备完成，开始分章撰写报告",
                next_steps=["校验并保存"],
            )
            parts_meta = _writer_parts_meta(plan, rows[0])

            async def _round(query: str):
                async for heartbeat in _writer_call(query):
                    yield heartbeat
                text, model = _writer_call.out
                writer_models_used.append(model)
                for event in _content_events(text):
                    yield event
                _round.out = text

            total_rounds = len(parts_meta) + 4
            yield sse_event({"type": "progress",
                             "message": f"分章生成 1/{total_rounds}：准备数据并生成标题…"})
            first_q = _build_writer_first_query(
                stats_md,
                open_text,
                plan,
                rows[0],
                qualitative_context=qualitative_context,
                analysis_focus=analysis_focus,
                viewpoint_stats_md=viewpoint_stats_md,
            )
            first_q = (
                _build_report_generation_instruction_block(prompt_instruction)
                + first_q
            )
            writer_context_included = bool(
                viewpoint_stats_md and viewpoint_stats_md in first_q
            )
            async for ev in _round(first_q):
                yield ev
            title_text = _round.out
            title_lines = []
            for ln in title_text.split("\n"):
                if ln.lstrip().startswith("## "):
                    break
                title_lines.append(ln)
            title_block = "\n".join(title_lines).strip() or title_text.strip()

            part_sections: list[str] = []
            for m in parts_meta:
                rnd = m["i"] + 1
                yield sse_event({"type": "progress",
                                 "message": f"分章生成 {rnd}/{total_rounds}：Part {m['i']} {m['name']}…"})
                yield sse_event({"type": "chunk", "content": "\n\n"})
                async for ev in _round(_build_writer_part_query(
                    m,
                    quantitative_first=quantitative_first,
                )):
                    yield ev
                sec = _round.out
                part_sections.append(sec.strip())

            yield sse_event({"type": "progress",
                             "message": f"分章生成 {total_rounds - 2}/{total_rounds}：核查待确认问题…"})
            async for ev in _round(_build_writer_bug_query()):
                yield ev
            bug_text = _round.out
            bug_clean = bug_text.strip()
            has_bug = bool(bug_clean) and bug_clean.upper().strip(" .。`*") != "NONE" and "## Bug" in bug_clean
            bug_section = bug_clean if has_bug else ""

            yield sse_event({"type": "progress",
                             "message": f"分章生成 {total_rounds - 1}/{total_rounds}：汇总核心结论…"})
            yield sse_event({"type": "chunk", "content": "\n\n"})
            async for heartbeat in _writer_call(_build_writer_core_query(
                parts_meta,
                has_bug,
                qualitative_context,
                analysis_focus=analysis_focus,
            )):
                yield heartbeat
            core_text, core_model = _writer_call.out
            writer_models_used.append(core_model)
            core_block = core_text.strip()

            yield sse_event({
                "type": "progress",
                "message": "正在局部复核核心结论的证据边界、原因场景与分析交付覆盖…",
            })
            review_history_len = len(writer_messages)
            selected_core = core_block
            try:
                async for heartbeat in _writer_call(
                    _build_writer_core_review_query(analysis_focus, has_bug)
                ):
                    yield heartbeat
                review_text, review_model = _writer_call.out
                writer_models_used.append(review_model)
                selected_core = _resolve_core_coverage_review(core_block, review_text)
            except Exception as review_error:
                print(
                    "[report] WARN optional core evidence review skipped: "
                    f"{type(review_error).__name__}"
                )
                yield sse_event({
                    "type": "progress",
                    "message": "核心结论证据复核未完成，已沿用原核心结论继续生成报告。",
                })
            finally:
                # 复核轮只用于选择核心结论；成功、无效或异常输出都不进入后续会话。
                del writer_messages[review_history_len:]
            if selected_core != core_block:
                core_block = selected_core
                if writer_messages and writer_messages[-1].get("role") == "assistant":
                    writer_messages[-1]["content"] = core_block

            for event in _content_events(core_block):
                yield event

            yield sse_event({"type": "progress",
                             "message": f"分章生成 {total_rounds}/{total_rounds}：生成行动建议…"})
            yield sse_event({"type": "chunk", "content": "\n\n"})
            async for heartbeat in _writer_call(
                _build_writer_action_query(
                    parts_meta,
                    has_bug,
                    qualitative_context,
                    analysis_focus=analysis_focus,
                    selected_core=core_block,
                )
            ):
                yield heartbeat
            action_text, action_model = _writer_call.out
            writer_models_used.append(action_model)
            action_section = _normalize_action_section(action_text)
            if not action_section:
                yield sse_event({
                    "type": "progress",
                    "message": "行动建议格式校验中，正在修正 Markdown 结构…",
                })
                async for heartbeat in _writer_call(_build_writer_action_repair_query()):
                    yield heartbeat
                repaired_text, repaired_model = _writer_call.out
                writer_models_used.append(repaired_model)
                action_section = _normalize_action_section(repaired_text)
                if not action_section:
                    fallback_body = repaired_text.strip() or action_text.strip()
                    if not fallback_body:
                        raise RuntimeError("行动建议生成结果为空")
                    # 内容已经由行动建议专用轮生成；这里只补齐固定标题，不改任何分析内容。
                    action_section = f"## 行动建议\n\n{fallback_body}"
            for event in _content_events(action_section):
                yield event

            yield _analysis_progress(
                "writing",
                "completed",
                f"报告正文 {total_rounds}/{total_rounds} 个生成步骤已完成",
            )

            details_divider = "---------------- 以下为详细信息，各位可以按需查看 ----------------"
            assembled = [title_block, core_block, details_divider, *part_sections]
            if bug_section:
                assembled.append(bug_section)
            assembled.append(action_section)
            full_report = "\n\n".join(b for b in assembled if b)

        yield _analysis_progress(
            "finalize",
            "active",
            "正在核对统计引用、整理格式并保存报告",
        )

        if quantitative_first:
            appendix = render_stats_appendix(
                sess.get("stats_blocks") or [],
                sess.get("stats_source") or "python",
            )
            if appendix:
                full_report = "\n\n".join((full_report.rstrip(), appendix))
        else:
            full_report = inject_qualitative_stats(full_report, stats_md, plan)

        numeric_sources = "\n".join(
            source for source in (stats_md, viewpoint_stats_md) if source
        )
        comparison_validation = {
            "status": "incomplete",
            "checked_claim_count": 0,
            "detected_count": 0,
            "applied_count": 0,
            "unresolved_count": 0,
            "changes": [],
            "unresolved": [],
            "repair_limit": MAX_COMPARISON_AUTO_REPAIRS,
            "auto_repair_attempted": False,
            "parser_warnings": [],
            "coverage": "量表均值的最高/最低、两两关系、排序、名次和并列关系",
        }
        try:
            comparison_catalog = survey_stats.build_comparison_fact_catalog(
                sess.get("rows") or [],
                plan,
            )
            initial_comparison_result = survey_stats.analyze_comparison_claims(
                full_report,
                comparison_catalog,
            )
            repairable_issues = [
                issue
                for issue in initial_comparison_result.get("issues") or []
                if issue.get("repairable")
            ]
            comparison_candidates: dict[str, str] = {}
            parser_warnings: list[str] = []
            repair_error = ""
            repair_attempted = False
            if repairable_issues and len(repairable_issues) <= MAX_COMPARISON_AUTO_REPAIRS:
                repair_attempted = True
                yield sse_event({
                    "type": "progress",
                    "message": (
                        f"检测到 {len(initial_comparison_result['issues'])} 处统计比较风险，"
                        "正在进行一次批量句子修补与独立复核…"
                    ),
                })
                try:
                    async for heartbeat in _writer_call(
                        _build_comparison_repair_query(repairable_issues),
                        phase="finalize",
                    ):
                        yield heartbeat
                    repair_text, repair_model = _writer_call.out
                    writer_models_used.append(repair_model)
                    comparison_candidates, parser_warnings = _parse_comparison_repairs(
                        repair_text,
                        repairable_issues,
                    )
                except Exception as repair_exc:
                    repair_error = f"自动修补调用失败：{type(repair_exc).__name__}"

            full_report, comparison_validation = _apply_verified_comparison_repairs(
                full_report,
                comparison_catalog,
                initial_comparison_result,
                comparison_candidates,
                numeric_sources,
            )
            comparison_validation.update({
                "auto_repair_attempted": repair_attempted,
                "parser_warnings": parser_warnings,
                "coverage": "量表均值的最高/最低、两两关系、排序、名次和并列关系",
                "catalog_group_count": len(comparison_catalog),
            })
            if repair_error:
                comparison_validation["repair_error"] = repair_error
            if len(repairable_issues) > MAX_COMPARISON_AUTO_REPAIRS:
                comparison_validation["repair_error"] = (
                    f"检测到 {len(repairable_issues)} 处可修补风险，超过单次 "
                    f"{MAX_COMPARISON_AUTO_REPAIRS} 处的安全上限，已保留原文。"
                )
        except Exception as comparison_exc:
            comparison_validation["error"] = (
                f"确定性比较校验未完整执行：{type(comparison_exc).__name__}"
            )
            comparison_validation["risk"] = (
                "报告已保存，但量表均值比较关系未完成自动复核，请人工检查相关结论。"
            )

        drifted = survey_stats.find_numbers_not_in_stats(full_report, numeric_sources)
        if drifted:
            print(f"[stats] WARN drifted numbers: {drifted[:20]}")

        full_report = _inject_disclaimer(full_report, mode=sess.get("mode") or "")
        full_report = _inject_research_background(full_report, qualitative_context)
        full_report = normalize_glossary_terms(full_report)
        viewpoint_diagnostics = finalize_viewpoint_diagnostics(
            viewpoint_diagnostics,
            full_report,
            writer_context_included=writer_context_included,
        )
        viewpoint_catalog = viewpoint_diagnostics["catalog"]
        viewpoint_output = viewpoint_diagnostics["writer_output"]
        print(
            "[viewpoint-diagnostics] "
            f"session={session_id} "
            f"catalog_entries={viewpoint_catalog['entry_count']} "
            f"context_included={writer_context_included} "
            f"viewpoint_blocks={viewpoint_output['viewpoint_block_count']} "
            f"mention_blocks={viewpoint_output['mention_block_count']} "
            f"status={viewpoint_output['status']}",
            flush=True,
        )
        qa_context_md = _build_qa_context(sess, full_report)
        writer_model = ",".join(dict.fromkeys(writer_models_used))
        # 生成期间仍可能发生改名等非模型写入；提交前重读最新 session，
        # 普通首版写回本 session；精确重复重跑则在历史事务里追加到原卡片。
        sess = require_session_access(session_id, login, loader=get_session)
        precommit_session = deepcopy(sess)
        completion_timing = _report_completion_timing(sess)
        sess.update(completion_timing)
        report_llm_usage.finalize_open_attempts()
        partial_rerun_source = build_partial_rerun_source(sess)
        snapshot = {
            "report_md": full_report,
            "title": "",
            "qa_context_md": qa_context_md,
            "qa_messages": [],
            "qa_provider": "",
            "qa_model": "",
            "analyst_conv_id": "",
            "analyst_app": "large" if use_large_mode else "standard",
            "report_writer_provider": "direct_llm",
            "report_writer_model": writer_model,
            "report_llm_usage": report_llm_usage.snapshot(),
            "viewpoint_diagnostics": viewpoint_diagnostics,
            "analysis_artifacts": build_analysis_artifacts(
                partial_rerun_source,
                use_large_mode=use_large_mode,
                clustered_themes=clustered_themes,
                report_viewpoints=report_viewpoints,
                viewpoint_stats_md=viewpoint_stats_md,
                cluster_diagnostics=cluster_diagnostics,
                cluster_metrics=cluster_metrics,
            ),
            "comparison_validation": comparison_validation,
            **completion_timing,
        }
        if rerun_history_id:
            if str(sess.get("rerun_target_history_id") or "").strip() != rerun_history_id:
                raise ValueError("历史重跑目标已变化，本次结果未保存")
            committed_entry, committed_version = append_exact_rerun_to_history(
                rerun_history_id,
                sess,
                snapshot,
                base_version=resolved_base_version,
                instruction=version_instruction,
                login=login,
            )
            _copy_report_version_state(sess, committed_entry)
            sess["rows_fed"] = False
            sess["rerun_target_version"] = committed_version["version"]
            sess["rerun_completed_at"] = datetime.now().isoformat(timespec="seconds")
            try:
                save_session(session_id, sess)
            except Exception as save_error:
                # history 已原子提交，不能为恢复临时 session 而反向覆盖并发 QA。
                print(
                    "[report-rerun] WARN history committed but session sync failed: "
                    f"{type(save_error).__name__}"
                )
        else:
            committed_version = append_report_version(
                sess,
                snapshot,
                kind=resolved_kind,
                base_version=resolved_base_version,
                instruction=version_instruction,
            )
            sess["rows_fed"] = False
            try:
                save_session(session_id, sess)
                save_to_history(session_id, sess)
            except Exception:
                # history 使用原子写；若其提交失败，恢复 session，确保失败不占版本号。
                sess.clear()
                sess.update(precommit_session)
                save_session(session_id, sess)
                raise
        await audit_log(
            request, "survey", "生成报告",
            f"会话：{session_id}；文件：{sess.get('filename', 'unknown')}；模式：{'大样本' if use_large_mode else '标准'}",
            metadata={"session_id": session_id, "filename": sess.get("filename", "unknown"),
                      "large_mode": use_large_mode, "version": committed_version["version"],
                      **({"history_id": rerun_history_id} if rerun_history_id else {})},
        )
        yield _analysis_progress(
            "finalize",
            "completed",
            "报告已完成最终处理并保存",
        )
        yield sse_event({
            "type": "report_done",
            "report_md": full_report,
            "comparison_validation": comparison_validation,
            "report_llm_usage": report_llm_usage.snapshot(),
            "version": committed_version["version"],
            **completion_timing,
            **(
                {
                    "history_id": rerun_history_id,
                    "report_no": committed_entry.get("report_no", ""),
                }
                if rerun_history_id
                else {}
            ),
            **_session_report_version_payload(sess),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        report_llm_usage.finalize_open_attempts()
        yield _report_llm_status_event()
        yield sse_event({"type": "error", "message": str(e)})
    finally:
        if rerun_target_lock is not None and rerun_target_lock_acquired:
            rerun_target_lock.release()
        generation_lock.release()


# ── 历史版本局部重做 SSE ────────────────────────────────────────


def _partial_scope_evidence(scopes: list) -> str:
    blocks: list[str] = []
    for scope_key, col_idx, part_index, part, entries in scopes:
        lines = [
            f"### scope={scope_key}｜Part {part_index} {part.get('name', '')}｜列 {col_idx}"
        ]
        for entry in entries:
            ids = " / ".join(
                str(value).strip()
                for value in (entry.get("ids") or {}).values()
                if str(value).strip()
            )
            profile = " / ".join(
                f"{key}={value}"
                for key, value in (entry.get("profile") or {}).items()
            )
            prefix = " | ".join(filter(None, [
                f"玩家ID={ids}" if ids else "",
                f"画像={profile}" if profile else "",
            ]))
            lines.append(
                f"- {f'[{prefix}] ' if prefix else ''}{str(entry.get('text') or '').strip()}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "（该范围没有开放题原文）"


def _partial_part_theme_evidence(clustered_themes: dict, part_index: int) -> str:
    selected = {
        str(scope_key): deepcopy(data)
        for scope_key, data in (clustered_themes or {}).items()
        if int(data.get("part_index") or 0) == part_index
    }
    return json.dumps(selected, ensure_ascii=False, separators=(",", ":"))


def _replace_report_in_qa_context(qa_context: str, report_md: str) -> str:
    source = str(qa_context or "")
    pattern = re.compile(r"<report>\n.*?\n</report>", re.DOTALL)
    replacement = f"<report>\n{report_md.strip()}\n</report>"
    if len(pattern.findall(source)) == 1:
        return pattern.sub(lambda _match: replacement, source, count=1)
    return ""


async def partial_report_rerun_stream(
    history_id: str,
    request: Request,
    *,
    base_version: int,
    target_type: str,
    target_key: str,
    instruction: str = "",
):
    """Only rerun selected qualitative scopes and atomically append one version."""
    login = await _current_login(request)
    target_id = str(history_id or "").strip()
    lock = _report_rerun_target_lock(target_id)
    if lock.locked():
        yield sse_event({
            "type": "error",
            "message": "该报告正在生成新版本，请等待本次生成完成后再试。",
        })
        return
    await lock.acquire()
    started = time.monotonic()
    writer_models: list[str] = []
    writer_round_count = 0

    def progress(phase: str, phase_index: int, status: str, message: str, **extra):
        return sse_event({
            "type": "partial_rerun_progress",
            "phase": phase,
            "phase_index": phase_index,
            "phase_total": 5,
            "status": status,
            "message": message,
            **extra,
        })

    async def writer_round(query: str, *, messages: list[dict] | None = None):
        nonlocal writer_round_count
        request_messages = messages or [
            {"role": "system", "content": _get_report_writer_system_prompt()},
            {"role": "user", "content": query},
        ]
        task = asyncio.create_task(
            collect_chat_completion(prepare_glossary_messages(request_messages))
        )
        try:
            while True:
                done, _ = await asyncio.wait(
                    {task}, timeout=LLM_STREAM_HEARTBEAT_SECONDS
                )
                if task in done:
                    answer, model = task.result()
                    writer_round_count += 1
                    writer_models.append(model)
                    writer_round.out = normalize_glossary_terms(answer)
                    writer_round.model = model
                    return
                yield sse_event({"type": "heartbeat"})
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    try:
        history = _load_history()
        entry = _find_history_for_login(history, target_id, login)
        if not entry:
            raise HTTPException(status_code=404, detail="历史报告不存在或无权访问")
        if len(normalize_report_versions(entry)) >= MAX_REPORT_VERSIONS:
            raise ValueError(
                f"报告版本已达上限（{MAX_REPORT_VERSIONS} 个），请先删除一个旧版本。"
            )
        base = resolve_report_version(entry, base_version)
        capability = partial_rerun_capability(entry, base)
        if not capability.get("available"):
            raise ValueError(capability.get("reason") or "该版本不能局部重做")
        plan = deepcopy(entry.get("plan") or {})
        source = deepcopy(entry.get("partial_rerun_source") or {})
        artifacts = deepcopy(base.get("analysis_artifacts") or {})
        target = resolve_partial_rerun_target(
            plan,
            source,
            target_type=target_type,
            target_key=target_key,
        )
        expected_plan_fingerprint = str(capability["plan_fingerprint"])
        expected_source_fingerprint = str(capability["source_fingerprint"])
        scope_keys = list(target["scope_keys"])
        target_scopes = scope_tuples_for_keys(plan, source, scope_keys)
        if {str(scope[0]) for scope in target_scopes} != set(scope_keys):
            raise ValueError("局部重做范围已与基础版本不一致")
        yield progress(
            "validate",
            1,
            "completed",
            f"已绑定基础版本 V{base_version}；只处理 {target['target_label']}",
            base_version=base_version,
            target=target,
        )

        clustered_themes = {
            str(key): deepcopy(value)
            for key, value in (artifacts.get("clustered_themes") or {}).items()
        }
        cluster_diagnostics = {
            str(key): deepcopy(value)
            for key, value in (
                artifacts.get("open_text_cluster_diagnostics") or {}
            ).items()
        }
        partial_cluster_metrics: dict = {
            "scope_count": 0,
            "elapsed_seconds": 0,
        }
        changed_themes = False
        if target_scopes:
            yield progress(
                "themes",
                2,
                "active",
                f"正在重做 {len(target_scopes)} 个目标 scope；其他 scope 直接复用",
                scope_keys=scope_keys,
            )
            new_themes: dict = {}
            new_diagnostics: dict = {}
            async for item in _batch_qualitative_analysis(
                source.get("open_text") or {},
                plan,
                source.get("headers") or [],
                target_id,
                deduplicate_respondents=True,
                _scopes_override=target_scopes,
            ):
                if item[0] == "heartbeat":
                    yield sse_event({"type": "heartbeat"})
                elif item[0] in {"progress", "analysis_progress"}:
                    payload = item[1] if isinstance(item[1], dict) else {"message": item[1]}
                    yield progress(
                        "themes",
                        2,
                        payload.get("status") or "active",
                        payload.get("message") or "正在处理目标题目",
                        scope_key=payload.get("scope_key"),
                    )
                elif item[0] == "diagnostics":
                    new_diagnostics = item[1]
                elif item[0] == "analysis_metrics":
                    partial_cluster_metrics = item[1]
                elif item[0] == "result":
                    new_themes = item[1]
            for key, value in new_themes.items():
                clustered_themes[str(key)] = deepcopy(value)
            for key, value in new_diagnostics.items():
                cluster_diagnostics[str(key)] = deepcopy(value)
            changed_themes = bool(new_themes)
            yield progress(
                "themes",
                2,
                "completed",
                f"目标 scope 已处理完成；复用了 {max(0, len(clustered_themes) - len(new_themes))} 个其他 scope",
                elapsed_seconds=partial_cluster_metrics.get("elapsed_seconds", 0),
            )
        else:
            yield progress(
                "themes",
                2,
                "skipped",
                "目标 Part 没有开放题，仅重写该 Part 的客观统计解读",
            )

        report_viewpoints = deepcopy(artifacts.get("report_viewpoints") or [])
        viewpoint_stats_md = str(artifacts.get("viewpoint_stats_md") or "")
        if changed_themes:
            yield progress(
                "synthesis",
                3,
                "active",
                "目标题目的主题已变化，正在重新执行跨题归纳",
            )
            report_viewpoints = []
            synthesis_seen = False
            async for item in build_report_viewpoint_stats(
                clustered_themes,
                source.get("open_text") or {},
                plan,
                source.get("headers") or [],
            ):
                if item[0] == "heartbeat":
                    yield sse_event({"type": "heartbeat"})
                elif item[0] in {"progress", "analysis_progress"}:
                    payload = item[1] if isinstance(item[1], dict) else {"message": item[1]}
                    synthesis_seen = True
                    yield progress(
                        "synthesis",
                        3,
                        payload.get("status") or "active",
                        payload.get("message") or "正在重算跨题观点",
                    )
                elif item[0] == "result":
                    report_viewpoints = item[1]
            viewpoint_stats_md = render_viewpoint_stats(
                clustered_themes,
                report_viewpoints,
            )
            yield progress(
                "synthesis",
                3,
                "completed" if synthesis_seen else "skipped",
                "跨题观点已更新" if synthesis_seen else "可用题目不足两道，未生成跨题观点",
            )
        else:
            yield progress(
                "synthesis",
                3,
                "skipped",
                "主题未变化，沿用基础版本的跨题观点",
            )

        yield progress(
            "writing",
            4,
            "active",
            f"正在重写 {target['part_title']}，随后同步核心结论和行动建议",
        )
        headers = source.get("headers") or []
        parts_meta = _writer_parts_meta(plan, headers)
        part_meta = parts_meta[target["part_index"] - 1]
        base_report = str(base.get("report_md") or "")
        base_part = extract_h2_section(base_report, target["part_title"])
        part_stats = render_qualitative_stats_by_part(
            source.get("stats_md") or "",
            plan,
        ).get(target["part_title"], "")
        supplement = str(instruction or "").strip()
        part_query = (
            "这是一次报告局部重做。只允许重写指定完整 Part；标题、其他 Part、Bug 模块不在本轮范围。\n\n"
            f"<partial_rerun>\n基础版本：V{base_version}\n重做类型：{target['target_type']}\n"
            f"重做目标：{target['target_label']}\n补充要求：{supplement or '无'}\n</partial_rerun>\n\n"
            f"<base_part>\n{base_part}\n</base_part>\n\n"
            f"<stats>\n{part_stats or '（本 Part 无可注入的客观统计表）'}\n</stats>\n\n"
            f"<open_text>\n{_partial_scope_evidence(target_scopes)}\n</open_text>\n\n"
            f"<part_theme_catalog>\n{_partial_part_theme_evidence(clustered_themes, target['part_index'])}\n"
            "</part_theme_catalog>\n\n"
            f"{viewpoint_stats_md}\n\n"
            "基础 Part 中不属于重做目标、且未被新证据影响的有效信息应保留；受新主题影响的总结、观点和引用必须更新。"
            "不要自行复制客观统计表，系统会在新 Part 校验后确定性注入一次。\n\n"
            + _build_writer_part_query(part_meta, quantitative_first=False)
        )
        async for event in writer_round(part_query):
            yield event
        new_part = validate_single_part(writer_round.out, target["part_title"])
        if part_stats and part_stats in new_part:
            raise ValueError("模型输出夹带了系统统计块，已拒绝重复插表")
        new_part = inject_qualitative_stats(
            new_part,
            source.get("stats_md") or "",
            plan,
        )
        validate_single_part(new_part, target["part_title"])
        if part_stats and new_part.count(part_stats) != 1:
            raise ValueError("新 Part 的客观统计未能安全地只注入一次")
        patched_report = replace_h2_section(
            base_report,
            target["part_title"],
            new_part,
        )

        has_bug = "## Bug 或待确认问题" in patched_report
        analysis_focus = plan.get("analysis_focus") if isinstance(plan, dict) else None
        core_query = (
            "下面是只替换了目标 Part 后的完整报告。请把它视为已经生成的全部章节，"
            "只重新输出核心结论；未受目标变化影响的结论应保持稳定。\n\n"
            f"<current_report>\n{patched_report}\n</current_report>\n\n"
            f"{viewpoint_stats_md}\n\n"
            + _build_writer_core_query(
                parts_meta,
                has_bug,
                source.get("qualitative_context") or {},
                analysis_focus=analysis_focus,
            )
        )
        async for event in writer_round(core_query):
            yield event
        core_block = writer_round.out.strip()
        review_messages = [
            {"role": "system", "content": _get_report_writer_system_prompt()},
            {"role": "user", "content": core_query},
            {"role": "assistant", "content": core_block},
            {"role": "user", "content": _build_writer_core_review_query(analysis_focus, has_bug)},
        ]
        try:
            async for event in writer_round("", messages=review_messages):
                yield event
            core_block = _resolve_core_coverage_review(core_block, writer_round.out)
        except Exception as review_error:
            print(
                "[partial-rerun] WARN optional core review skipped: "
                f"{type(review_error).__name__}",
                flush=True,
            )
        patched_report = replace_core_block(patched_report, core_block)

        action_query = (
            "下面是目标 Part 与核心结论已经更新后的完整报告。只重新输出行动建议；"
            "建议必须承接新结论，未受影响的有效动作保持稳定。\n\n"
            f"<current_report>\n{patched_report}\n</current_report>\n\n"
            f"{viewpoint_stats_md}\n\n"
            + _build_writer_action_query(
                parts_meta,
                has_bug,
                source.get("qualitative_context") or {},
                analysis_focus=analysis_focus,
                selected_core=core_block,
            )
        )
        async for event in writer_round(action_query):
            yield event
        action_raw = writer_round.out
        action_section = _normalize_action_section(action_raw)
        if not action_section:
            repair_messages = [
                {"role": "system", "content": _get_report_writer_system_prompt()},
                {"role": "user", "content": action_query},
                {"role": "assistant", "content": action_raw},
                {"role": "user", "content": _build_writer_action_repair_query()},
            ]
            async for event in writer_round("", messages=repair_messages):
                yield event
            action_section = _normalize_action_section(writer_round.out)
        if not action_section:
            raise ValueError("新行动建议未通过结构校验")
        patched_report = replace_action_section(patched_report, action_section)
        yield progress(
            "writing",
            4,
            "completed",
            "目标 Part、核心结论和行动建议已完成严格结构校验",
        )

        yield progress(
            "finalize",
            5,
            "active",
            "正在核对 fingerprints、比较关系并原子保存新版本",
        )
        comparison_catalog = source.get("comparison_catalog") or []
        comparison_result = survey_stats.analyze_comparison_claims(
            patched_report,
            comparison_catalog,
        )
        patched_report, comparison_validation = _apply_verified_comparison_repairs(
            patched_report,
            comparison_catalog,
            comparison_result,
            {},
            "\n".join(filter(None, [source.get("stats_md") or "", viewpoint_stats_md])),
        )
        comparison_validation.update({
            "auto_repair_attempted": False,
            "coverage": "量表均值的最高/最低、两两关系、排序、名次和并列关系",
            "catalog_group_count": len(comparison_catalog),
        })
        elapsed_seconds = round(time.monotonic() - started, 3)
        base_cluster_metrics = deepcopy(
            artifacts.get("open_text_cluster_metrics") or {}
        )
        base_cluster_metrics["last_partial_rerun"] = deepcopy(partial_cluster_metrics)
        updated_artifacts = build_analysis_artifacts(
            source,
            use_large_mode=artifacts.get("report_generation_mode") == "large",
            clustered_themes=clustered_themes,
            report_viewpoints=report_viewpoints,
            viewpoint_stats_md=viewpoint_stats_md,
            cluster_diagnostics=cluster_diagnostics,
            cluster_metrics=base_cluster_metrics,
        )
        rerun_details = {
            "base_version": base_version,
            "target_type": target["target_type"],
            "target_key": target["target_key"],
            "target_label": target["target_label"],
            "target_part": target["part_title"],
            "scope_keys": scope_keys,
            "changed_sections": [target["part_title"], "核心结论", "行动建议"],
            "full_report_rerun": False,
            "elapsed_seconds": elapsed_seconds,
            "theme_elapsed_seconds": partial_cluster_metrics.get("elapsed_seconds", 0),
            "writer_round_count": writer_round_count,
            "writer_models": list(dict.fromkeys(writer_models)),
            "token_usage": {
                "available": False,
                "reason": "当前 LLM 客户端未返回可持久化的 Token usage。",
            },
        }
        version_instruction = supplement or "未填写补充要求，本次为局部重做"
        qa_context_md = _replace_report_in_qa_context(
            base.get("qa_context_md") or "",
            patched_report,
        ) or _build_qa_context({
            "plan": plan,
            "stats_md": source.get("stats_md") or "",
            "qualitative_context": source.get("qualitative_context") or {},
        }, patched_report)
        snapshot = {
            "report_md": patched_report,
            "title": str(base.get("title") or ""),
            "qa_context_md": qa_context_md,
            "qa_messages": [],
            "qa_provider": "",
            "qa_model": "",
            "report_writer_provider": "direct_llm",
            "report_writer_model": ",".join(dict.fromkeys(writer_models)),
            "analyst_conv_id": "",
            "analyst_app": str(base.get("analyst_app") or "standard"),
            "comparison_validation": comparison_validation,
            "analysis_artifacts": updated_artifacts,
            "rerun_details": rerun_details,
            "report_completed_at": datetime.now().isoformat(timespec="milliseconds"),
            "report_duration_seconds": elapsed_seconds,
        }
        committed_entry, committed = append_partial_rerun_to_history(
            target_id,
            snapshot,
            base_version=base_version,
            expected_plan_fingerprint=expected_plan_fingerprint,
            expected_source_fingerprint=expected_source_fingerprint,
            instruction=version_instruction,
            login=login,
        )
        await audit_log(
            request,
            "survey",
            "局部重做报告",
            f"报告：{target_id}；基础版本：V{base_version}；范围：{target['target_label']}",
            metadata={
                "history_id": target_id,
                "base_version": base_version,
                "version": committed["version"],
                "target_type": target["target_type"],
                "scope_keys": scope_keys,
                "elapsed_seconds": elapsed_seconds,
                "full_report_rerun": False,
            },
        )
        yield progress(
            "finalize",
            5,
            "completed",
            f"V{committed['version']} 已原子保存；未执行整份报告重跑",
        )
        yield sse_event({
            "type": "partial_rerun_done",
            "history_id": target_id,
            "version": committed["version"],
            "active_version": committed["version"],
            "versions": report_version_summaries(committed_entry),
            "report_md": patched_report,
            "comparison_validation": comparison_validation,
            "rerun_details": rerun_details,
        })
    except Exception as exc:
        import traceback
        traceback.print_exc()
        message = exc.detail if isinstance(exc, HTTPException) else str(exc)
        yield sse_event({"type": "error", "message": message or "局部重做失败"})
    finally:
        lock.release()


# ── 当前会话 QA SSE ─────────────────────────────────────────────


def _session_report_version_payload(sess: dict) -> dict:
    versions = normalize_report_versions(sess)
    active_version = (
        resolve_report_version(sess)["version"] if versions else None
    )
    highest_version = max((item["version"] for item in versions), default=0)
    try:
        next_version = int(sess.get("next_report_version") or highest_version + 1)
    except (TypeError, ValueError):
        next_version = highest_version + 1
    next_version = max(next_version, highest_version + 1)
    return {
        "versions": report_version_summaries(sess),
        "active_version": active_version,
        "next_version": next_version,
        "version_count": len(versions),
        "max_versions": MAX_REPORT_VERSIONS,
        # 新版本只能从重新上传后的数据确认页发起；报告页仅保留查看能力。
        "can_generate_version": False,
    }


def get_session_report_versions(session_id: str) -> dict:
    """返回当前 session 的报告版本元数据，不包含多份正文。"""
    sess = get_session(session_id)
    if not _uses_report_versions(sess):
        raise HTTPException(status_code=400, detail="该报告类型不支持版本管理")
    if not normalize_report_versions(sess):
        raise HTTPException(status_code=404, detail="当前任务还没有报告版本")
    return _session_report_version_payload(sess)


def get_session_report_version(session_id: str, version: int) -> dict:
    """读取当前 session 的指定报告版本，不改变 active 版本。"""
    sess = get_session(session_id)
    if not _uses_report_versions(sess):
        raise HTTPException(status_code=400, detail="该报告类型不支持版本管理")
    try:
        snapshot = resolve_report_version(sess, version)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        **snapshot,
        "version": snapshot["version"],
        "selected_version": snapshot["version"],
        **_session_report_version_payload(sess),
    }


def delete_session_report_version(
    session_id: str,
    version: int,
    login: dict | None = None,
) -> dict:
    """在用户明确选择后删除一个旧版本；最后一版受保护。"""
    sess = require_session_access(session_id, login, loader=get_session)
    generation_lock = _report_generation_lock(session_id)
    if generation_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="当前任务正在生成报告，完成后才能删除旧版本。",
        )
    if _report_rerun_target_lock(session_id).locked():
        raise HTTPException(
            status_code=409,
            detail="该报告正在重新生成，完成后才能删除旧版本。",
        )
    if not _uses_report_versions(sess):
        raise HTTPException(status_code=400, detail="该报告类型不支持版本管理")
    rerun_history_id = str(sess.get("rerun_target_history_id") or "").strip()
    if rerun_history_id:
        result = delete_owned_history_report_version(
            rerun_history_id,
            version,
            login,
        )
        entry = find_exact_survey_duplicate_entry(sess, login, rerun_history_id)
        if entry:
            _copy_report_version_state(sess, entry)
            save_session(session_id, sess)
        return result
    predelete_session = deepcopy(sess)
    try:
        deleted = delete_report_version(sess, version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        save_session(session_id, sess)
        save_to_history(
            session_id,
            sess,
            replace_report_versions=True,
        )
    except Exception:
        sess.clear()
        sess.update(predelete_session)
        save_session(session_id, sess)
        raise
    active = resolve_report_version(sess)
    return {
        **active,
        "ok": True,
        "deleted_version": deleted["version"],
        "version": active["version"],
        "selected_version": active["version"],
        **_session_report_version_payload(sess),
    }


def delete_owned_history_report_version(
    history_id: str,
    version: int,
    login: dict | None,
) -> dict:
    """Delete from a history card unless it is being regenerated or queried."""
    target_id = str(history_id or "").strip()
    if _report_rerun_target_lock(target_id).locked():
        raise HTTPException(
            status_code=409,
            detail="原报告正在重新生成，完成后才能删除旧版本。",
        )
    if _report_generation_lock(target_id).locked():
        raise HTTPException(
            status_code=409,
            detail="该历史报告正在处理追问，完成后才能删除旧版本。",
        )
    return _delete_history_report_version(target_id, version, login)


async def qa_stream(
    session_id: str,
    question: str,
    request: Request,
    version: int | None = None,
):
    """当前会话 QA SSE 流程（async generator）。"""
    login = await _current_login(request)
    require_session_access(session_id, login, loader=get_session)
    operation_lock = _report_generation_lock(session_id)
    if operation_lock.locked():
        yield sse_event({
            "type": "error",
            "message": "当前任务正在生成新版本或处理追问，请完成后再试。",
        })
        return
    await operation_lock.acquire()
    try:
        # 与报告重跑共用同一把锁；锁内重读，避免排队请求回写旧版本快照。
        sess = require_session_access(session_id, login, loader=get_session)
        _assign_session_owner(sess, login)
        uses_versions = _uses_report_versions(sess)
        if uses_versions:
            try:
                snapshot = resolve_report_version(sess, version)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
            selected_version = snapshot["version"]
            qa_source = dict(sess)
            qa_source.update(snapshot)
        else:
            if version not in (None, 1):
                raise ValueError("该报告不支持版本切换")
            snapshot = sess
            selected_version = None
            qa_source = sess
        qa_context = (
            str(qa_source.get("qa_context_md") or "").strip()
            or _build_qa_context(qa_source)
        )
        yield sse_event({"type": "qa_scope", "message": _describe_qa_context_scope(qa_context)})
        answer_text, qa_model, qa_context = await _answer_qa_direct(qa_source, question)
        for event in _content_events(answer_text):
            yield event
        # 模型回答期间可能发生改名；提交 QA 前重读并在最新版本快照上更新。
        sess = require_session_access(session_id, login, loader=get_session)
        precommit_session = deepcopy(sess)
        snapshot = (
            resolve_report_version(sess, selected_version)
            if uses_versions
            else sess
        )
        qa_messages = list(snapshot.get("qa_messages") or [])
        qa_messages.extend([
            {"role": "user", "content": question, "ts": datetime.now().isoformat()},
            {"role": "ai", "content": answer_text, "ts": datetime.now().isoformat()},
        ])
        if uses_versions:
            update_report_version(
                sess,
                selected_version,
                qa_context_md=qa_context,
                qa_provider="direct_llm",
                qa_model=qa_model,
                qa_messages=qa_messages,
            )
        else:
            sess["qa_context_md"] = qa_context
            sess["qa_provider"] = "direct_llm"
            sess["qa_model"] = qa_model
            sess["qa_messages"] = qa_messages
        sess["rows_fed"] = True
        try:
            save_session(session_id, sess)
            rerun_history_id = str(sess.get("rerun_target_history_id") or "").strip()
            if rerun_history_id:
                sync_exact_rerun_qa_to_history(
                    rerun_history_id,
                    sess,
                    selected_version,
                    login=login,
                )
            else:
                save_to_history(session_id, sess)
        except Exception:
            sess.clear()
            sess.update(precommit_session)
            save_session(session_id, sess)
            raise
        await audit_log(
            request, "report", "追问当前报告",
            f"会话：{session_id}；问题：{_short_text(question)}",
            metadata={
                "session_id": session_id,
                **({"version": selected_version} if selected_version else {}),
            },
        )
        yield sse_event({
            "type": "qa_done",
            "answer": answer_text,
            **({"version": selected_version} if selected_version else {}),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        yield sse_event({"type": "error", "message": str(e)})
    finally:
        operation_lock.release()


# ── 历史报告 QA SSE ─────────────────────────────────────────────


async def history_qa_stream(
    history_id: str,
    question: str,
    history: list,
    request: Request,
    version: int | None = None,
):
    """历史报告续聊 QA SSE 流程（async generator）。"""
    operation_lock = _report_generation_lock(history_id)
    if operation_lock.locked():
        yield sse_event({
            "type": "error",
            "message": "当前任务正在生成新版本或处理追问，请完成后再试。",
        })
        return
    await operation_lock.acquire()
    try:
        # 路由鉴权后到 SSE 真正开始之间可能有短暂间隔；拿到互斥锁后
        # 重新读取，避免基于生成新版本前的旧快照回答。
        history = _load_history()
        entry = next(h for h in history if h["id"] == history_id)
        uses_versions = _uses_report_versions(entry)
        if uses_versions:
            snapshot = resolve_report_version(entry, version)
            selected_version = snapshot["version"]
            qa_source = dict(entry)
            qa_source.update(snapshot)
        else:
            if version not in (None, 1):
                raise ValueError("该报告不支持版本切换")
            snapshot = entry
            selected_version = None
            qa_source = entry
        qa_context = (
            str(qa_source.get("qa_context_md") or "").strip()
            or _build_qa_context(qa_source)
        )
        yield sse_event({"type": "qa_scope", "message": _describe_qa_context_scope(qa_context)})
        answer_text, qa_model, qa_context = await _answer_qa_direct(qa_source, question)
        for event in _content_events(answer_text):
            yield event

        committed_qa_state: dict = {}

        def _commit_history_qa(current_history: list) -> None:
            current_entry = next(
                (item for item in current_history if item.get("id") == history_id),
                None,
            )
            if current_entry is None:
                raise ValueError("历史记录不存在")
            current_snapshot = (
                resolve_report_version(current_entry, selected_version)
                if uses_versions
                else current_entry
            )
            qa_messages = list(current_snapshot.get("qa_messages") or [])
            qa_messages.extend([
                {"role": "user", "content": question, "ts": datetime.now().isoformat()},
                {"role": "ai", "content": answer_text, "ts": datetime.now().isoformat()},
            ])
            if uses_versions:
                update_report_version(
                    current_entry,
                    selected_version,
                    qa_context_md=qa_context,
                    qa_provider="direct_llm",
                    qa_model=qa_model,
                    qa_messages=qa_messages,
                )
            else:
                current_entry["qa_context_md"] = qa_context
                current_entry["qa_provider"] = "direct_llm"
                current_entry["qa_model"] = qa_model
                current_entry["qa_messages"] = qa_messages
            current_entry["rows_fed"] = True
            committed_qa_state.update({
                "owner_key": _history_owner_key(current_entry),
                "qa_context_md": qa_context,
                "qa_provider": "direct_llm",
                "qa_model": qa_model,
                "qa_messages": qa_messages,
            })

        mutate_history(_commit_history_qa)
        try:
            live_session = get_session(history_id)
            if _history_owner_key(live_session) != committed_qa_state["owner_key"]:
                raise ValueError("历史记录与临时会话归属不一致")
            if uses_versions:
                if not _uses_report_versions(live_session):
                    raise ValueError("历史记录与临时会话的版本模式不一致")
                update_report_version(
                    live_session,
                    selected_version,
                    qa_context_md=committed_qa_state["qa_context_md"],
                    qa_provider=committed_qa_state["qa_provider"],
                    qa_model=committed_qa_state["qa_model"],
                    qa_messages=committed_qa_state["qa_messages"],
                )
            else:
                live_session["qa_context_md"] = committed_qa_state["qa_context_md"]
                live_session["qa_provider"] = committed_qa_state["qa_provider"]
                live_session["qa_model"] = committed_qa_state["qa_model"]
                live_session["qa_messages"] = committed_qa_state["qa_messages"]
            live_session["rows_fed"] = True
            save_session(history_id, live_session)
        except (HTTPException, ValueError):
            # 历史记录仍可追问；临时会话已过期或版本不一致时不阻断结果。
            pass
        await audit_log(
            request, "report", "追问历史报告",
            f"历史报告：{history_id}；问题：{_short_text(question)}",
            metadata={
                "history_id": history_id,
                **({"version": selected_version} if selected_version else {}),
            },
        )
        yield sse_event({
            "type": "qa_done",
            "answer": answer_text,
            **({"version": selected_version} if selected_version else {}),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        yield sse_event({"type": "error", "message": str(e)})
    finally:
        operation_lock.release()


# ── Router 前置校验函数 ──────────────────────────────────────────


def validate_columns_ready(session_id: str) -> None:
    """校验列识别前置条件（rows 存在），不满足则 raise HTTPException。"""
    sess = get_session(session_id)
    if not sess.get("rows"):
        raise HTTPException(status_code=400, detail="会话中没有数据")


def columns_require_llm(session_id: str) -> bool:
    """原问卷结构不需模型判断；中文原文直出，其他语言首次需翻译。"""
    sess = get_session(session_id)
    if sess.get("column_provider") != "questionnaire":
        return True
    if _questionnaire_titles_are_chinese(sess.get("columns_detected") or []):
        return False
    return sess.get("questionnaire_translation_status") != "translated"


def validate_plan_ready(session_id: str) -> str:
    """校验方案生成前置条件，并返回当前分析模式。"""
    sess = get_session(session_id)
    if not sess.get("rows"):
        raise HTTPException(status_code=400, detail="会话中没有数据，请先上传文件")
    return sess.get("mode", "")


def validate_plan_confirm_ready(session_id: str) -> None:
    """校验方案确认/修订前置条件，不满足则 raise HTTPException。"""
    sess = get_session(session_id)
    if not sess.get("plan") or not sess.get("rows"):
        raise HTTPException(status_code=400, detail="会话状态丢失，请重新上传文件")


def validate_report_ready(session_id: str) -> bool:
    """校验报告生成前置条件，返回 use_large_mode 供 router 选择正确的 analyst key。"""
    sess = get_session(session_id)
    if not all([sess.get("plan"), sess.get("rows"), sess.get("stats_md")]):
        raise HTTPException(status_code=400, detail="请先完成统计计算")
    if not LLM_API_KEY:
        raise HTTPException(status_code=500, detail="未配置 LLM_API_KEY")
    if not LLM_REPORT_MODEL:
        raise HTTPException(status_code=500, detail="未配置 LLM_REPORT_MODEL")
    return sess.get("mode") == "crosstab" or any(
        len(v) > LARGE_SAMPLE_THRESHOLD for v in sess.get("open_text", {}).values()
    )


def validate_qa_ready(session_id: str, version: int | None = None) -> None:
    """校验统一直连 QA 的报告与模型配置前置条件。"""
    sess = get_session(session_id)
    if _uses_report_versions(sess):
        try:
            resolve_report_version(sess, version)
        except ValueError as exc:
            if not normalize_report_versions(sess):
                raise HTTPException(status_code=400, detail="请先生成报告") from exc
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    elif version not in (None, 1):
        raise HTTPException(status_code=400, detail="该报告不支持版本切换")
    if not sess.get("report_md"):
        raise HTTPException(status_code=400, detail="请先生成报告")
    if not LLM_API_KEY:
        raise HTTPException(status_code=500, detail="未配置 LLM_API_KEY")
    if not LLM_QA_MODEL:
        raise HTTPException(status_code=500, detail="未配置 LLM_QA_MODEL")


def prepare_history_qa_context(
    history_id: str,
    login: dict | None,
    version: int | None = None,
) -> list:
    """加载历史记录并校验统一直连 QA 的前置条件。"""
    history = _load_history()
    entry = _find_history_for_login(history, history_id, login)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    if _uses_report_versions(entry):
        try:
            resolve_report_version(entry, version)
        except ValueError as exc:
            if not normalize_report_versions(entry):
                raise HTTPException(
                    status_code=400,
                    detail="该历史记录没有可追问的报告",
                ) from exc
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    elif version not in (None, 1):
        raise HTTPException(status_code=400, detail="该报告不支持版本切换")
    if not entry.get("report_md"):
        raise HTTPException(status_code=400, detail="该历史记录没有可追问的报告")
    if not LLM_API_KEY:
        raise HTTPException(status_code=500, detail="未配置 LLM_API_KEY")
    if not LLM_QA_MODEL:
        raise HTTPException(status_code=500, detail="未配置 LLM_QA_MODEL")
    return history
