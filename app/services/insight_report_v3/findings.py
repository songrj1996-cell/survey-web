"""Stage one: evidence-bound, question-local finding extraction."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import inspect
import json
import time

from app.core.config import (
    DEFAULT_INSIGHT_V3_FINDINGS_SYSTEM,
    INSIGHT_V3_FINDINGS_WARN_SECONDS,
    INSIGHT_V3_QUESTION_CONCURRENCY,
    INSIGHT_V3_QUESTION_INPUT_CHARS,
    INSIGHT_V3_QUESTION_MAX_RETRIES,
    LLM_THEME_EXTRACT_FALLBACK_MODELS,
    LLM_THEME_EXTRACT_MAX_TOKENS,
    LLM_THEME_EXTRACT_MODEL,
    LLM_THEME_EXTRACT_REASONING,
)
from app.services.report_engine import _direct_json_call, _run_bounded_calls
from app.services.insight_report_v3.contracts import (
    FINDINGS_SCHEMA_VERSION,
    build_stage_source_contract,
    hydrate_findings,
    validate_findings_payload,
)
from app.services.insight_report_v3.frequency import model_frequency_rules


_CONTEXT_FIELDS = (
    "problem",
    "background",
    "target_users",
    "key_concerns",
    "report_usage",
)


def _context_payload(qualitative_context: dict | None) -> tuple[dict, str]:
    context = qualitative_context if isinstance(qualitative_context, dict) else {}
    business = {
        field: str(context.get(field) or "").strip()
        for field in _CONTEXT_FIELDS
        if str(context.get(field) or "").strip()
    }
    return business, str(context.get("analysis_approach") or "").strip()


def build_findings_query(question: dict, qualitative_context: dict | None) -> str:
    """Build one-question input. Only short IDs are exposed to the model."""
    business_context, analysis_approach = _context_payload(qualitative_context)
    response_blocks = []
    for source in question.get("model_sources") or []:
        lines = [f"[{source['response_id']}]（一名玩家）"]
        responses = source.get("responses") or []
        if len(responses) == 1:
            lines.append(str(responses[0]))
        else:
            lines.extend(
                f"- 同一玩家反馈 {index}：{response}"
                for index, response in enumerate(responses, 1)
            )
        response_blocks.append("\n".join(lines))
    frequency_rules = "\n".join(
        model_frequency_rules(int(question.get("respondent_count") or 0))
    )
    return (
        "<business_context>\n"
        + json.dumps(business_context, ensure_ascii=False, separators=(",", ":"))
        + "\n</business_context>\n\n"
        + "<analysis_approach>\n"
        + (analysis_approach or "（未提供）")
        + "\n</analysis_approach>\n\n"
        + "<question>\n"
        + str(question.get("question") or "")
        + "\n</question>\n"
        + (f"<audience>{question['audience']}</audience>\n" if question.get("audience") else "")
        + f"<respondent_total>{question.get('respondent_count', 0)}</respondent_total>\n\n"
        + "<frequency_rules>\n"
        + frequency_rules
        + "\n只能选满足门槛的档位；不满足时必须降低档位，不得虚增 support_count。"
        + "\n</frequency_rules>\n\n"
        + "<responses>\n"
        + "\n\n".join(response_blocks)
        + "\n</responses>\n\n"
        + "请完整阅读本题全部玩家条目，按 system prompt 的 JSON 契约提炼 findings。"
        + "独立保留影响体验解释的具体异常、反例与关键条件，使用 preserve_reason 标记；"
        + "不要把可核查问题消融为泛泛的平衡性或体验主题，也不要把孤立反馈写成已确认故障。"
        + "输出前逐项回看绑定回答：claim 和 caveat 的肯否、时态、主体、触发条件必须与原文一致；"
        + "不要把并列现象连成因果，不因常识判断而补否定词或修正原文。"
        + "遇到含混、矛盾或疑似笔误，保留待核实边界，不替玩家确定解释。"
        + "玩家猜测必须在 claim 内明确归属，不能只在 caveat 补免责声明；"
        + "一人的疑问不能扩写为人群分歧，事件经历不能扩写为系统裁定。"
        + "保留能／不能、允许／不允许等模态与完整谓语，不能把有方向的原句压成无方向名词短语。"
        + "每个代表 ID 要直接支持判断，不能借相近主题凑数。"
        + "事实性反例独立成 finding，preserve_reason=counterexample，response_ids 只绑定支持反例的来源；"
        + "不要把相反观点的来源或人数混在一起。caveat 只说明当前发现的证据边界，不夹带另一事实。"
        + "调用内短 ID 只能写入 response_ids，claim 和 caveat 不得内联来源编号。"
    )


async def _forward_attempt_event(callback, event: dict) -> None:
    if callback is None:
        return
    result = callback(event)
    if inspect.isawaitable(result):
        await result


def _usage_summary(events: list[dict]) -> dict:
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    available = False
    complete = True
    for event in events:
        if event.get("status") not in {"completed", "failed"}:
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            complete = False
            continue
        available = True
        for field in totals:
            value = usage.get(field)
            if type(value) is int and value >= 0:
                totals[field] += value
        if not event.get("usage_complete"):
            complete = False
    return {"available": available, "complete": available and complete, **totals}


def _base_question_result(question: dict) -> dict:
    return {
        "question_key": question["question_key"],
        "scope_key": question["scope_key"],
        "column_indexes": list(question["column_indexes"]),
        "question": question["question"],
        "audience": question.get("audience", ""),
        "respondent_count": question["respondent_count"],
    }


def _degraded_question(
    question: dict,
    *,
    code: str,
    message: str,
    logical_attempts: int,
    attempt_events: list[dict] | None = None,
    elapsed_seconds: float = 0.0,
) -> dict:
    events = attempt_events or []
    upstream_attempts = sum(1 for event in events if event.get("status") == "started")
    return {
        **_base_question_result(question),
        "status": "degraded",
        "findings": [],
        "degraded_source_ids": list(question.get("all_source_ids") or []),
        "failure": {
            "code": code,
            "message": str(message or code)[:300],
            "logical_attempts": logical_attempts,
            "upstream_attempts": upstream_attempts,
        },
        "diagnostics": {
            "elapsed_seconds": round(elapsed_seconds, 3),
            "logical_attempts": logical_attempts,
            "upstream_attempts": upstream_attempts,
            "usage": _usage_summary(events),
        },
    }


async def _extract_question(
    question: dict,
    *,
    source_registry: dict[str, dict],
    qualitative_context: dict | None,
    max_retries: int,
    input_char_limit: int,
    on_attempt_event=None,
) -> dict:
    started = time.monotonic()
    if question["respondent_count"] == 0:
        return {
            **_base_question_result(question),
            "status": "skipped",
            "findings": [],
            "degraded_source_ids": [],
            "failure": None,
            "diagnostics": {
                "elapsed_seconds": 0.0,
                "logical_attempts": 0,
                "upstream_attempts": 0,
                "usage": _usage_summary([]),
                "stop_reason": "no_valid_responses",
            },
        }

    query = build_findings_query(question, qualitative_context)
    if len(query) > input_char_limit:
        return _degraded_question(
            question,
            code="input_too_large",
            message=f"本题输入 {len(query)} 字符，超过保护阈值 {input_char_limit}",
            logical_attempts=0,
            elapsed_seconds=time.monotonic() - started,
        )

    attempt_events: list[dict] = []

    async def _on_attempt(event: dict):
        tagged = {
            **event,
            "phase": "themes",
            "question_key": question["question_key"],
            "scope_key": question["scope_key"],
        }
        attempt_events.append(tagged)
        await _forward_attempt_event(on_attempt_event, tagged)

    valid_short_ids = set(question["short_id_map"])
    last_error = ""
    attempts: list[dict] = []
    for attempt_index in range(max_retries + 1):
        repair_errors: list[str] = []
        result = await _direct_json_call(
            DEFAULT_INSIGHT_V3_FINDINGS_SYSTEM,
            query,
            models=(LLM_THEME_EXTRACT_MODEL, *LLM_THEME_EXTRACT_FALLBACK_MODELS),
            max_tokens=LLM_THEME_EXTRACT_MAX_TOKENS,
            reasoning_effort=LLM_THEME_EXTRACT_REASONING or None,
            validator=lambda data: validate_findings_payload(
                data,
                valid_short_ids=valid_short_ids,
                respondent_total=question["respondent_count"],
            ),
            on_repair=repair_errors.append,
            on_attempt_event=_on_attempt,
        )
        attempts.append(
            {
                "attempt": attempt_index + 1,
                "model": str(result.get("model") or ""),
                "repaired": bool(result.get("repaired")),
                "repair_errors": repair_errors,
                "error": str(result.get("error") or "")[:300],
                "raw_len": int(result.get("raw_len") or 0),
                "duration_seconds": float(result.get("duration_seconds") or 0.0),
            }
        )
        if result.get("data") is not None:
            findings = hydrate_findings(
                result["data"],
                scope_key=question["scope_key"],
                short_id_map=question["short_id_map"],
                source_registry=source_registry,
            )
            return {
                **_base_question_result(question),
                "status": "complete",
                "findings": findings,
                "degraded_source_ids": [],
                "failure": None,
                "diagnostics": {
                    "input_char_count": len(query),
                    "logical_attempts": attempt_index + 1,
                    "upstream_attempts": sum(
                        1 for event in attempt_events if event.get("status") == "started"
                    ),
                    "attempts": attempts,
                    "usage": _usage_summary(attempt_events),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "stop_reason": "contract_validated",
                },
            }
        last_error = str(result.get("error") or "模型结果未通过校验")

    degraded = _degraded_question(
        question,
        code="retry_exhausted",
        message=last_error,
        logical_attempts=max_retries + 1,
        attempt_events=attempt_events,
        elapsed_seconds=time.monotonic() - started,
    )
    degraded["diagnostics"].update(
        {"input_char_count": len(query), "attempts": attempts, "stop_reason": "retry_exhausted"}
    )
    return degraded


def _checkpoint_base(source_contract: dict, question_count: int) -> dict:
    return {
        "schema_version": FINDINGS_SCHEMA_VERSION,
        "source_binding": deepcopy(source_contract["source_binding"]),
        "source_registry": deepcopy(source_contract["source_registry"]),
        "questions": [],
        "diagnostics": {
            "status": "running",
            "question_count": question_count,
            "completed_question_count": 0,
        },
    }


async def generate_insight_findings(
    *,
    open_text: dict,
    plan: dict,
    headers: list,
    file_sha256: str,
    qualitative_context: dict | None = None,
    concurrency: int | None = None,
    max_retries: int | None = None,
    input_char_limit: int | None = None,
    warn_seconds: float | None = None,
    hard_timeout_seconds: float | None = None,
    question_limit: int | None = None,
    on_attempt_event=None,
):
    """Yield stage/checkpoint events; a failed question never aborts its peers.

    A hard timeout is disabled by default and exists only for explicit experiments.
    """
    started = time.monotonic()
    source_contract = build_stage_source_contract(
        open_text=open_text,
        plan=plan,
        headers=headers,
        file_sha256=file_sha256,
    )
    questions = source_contract["questions"]
    if question_limit is not None:
        questions = questions[:max(0, int(question_limit))]
    retry_limit = INSIGHT_V3_QUESTION_MAX_RETRIES if max_retries is None else max(0, min(2, int(max_retries)))
    worker_count = INSIGHT_V3_QUESTION_CONCURRENCY if concurrency is None else max(1, int(concurrency))
    char_limit = INSIGHT_V3_QUESTION_INPUT_CHARS if input_char_limit is None else max(1, int(input_char_limit))
    warning_after = INSIGHT_V3_FINDINGS_WARN_SECONDS if warn_seconds is None else max(0.0, float(warn_seconds))
    deadline = (
        time.monotonic() + float(hard_timeout_seconds)
        if hard_timeout_seconds is not None and float(hard_timeout_seconds) > 0
        else None
    )

    checkpoint = _checkpoint_base(source_contract, len(questions))
    checkpoint["diagnostics"].update(
        {
            "concurrency": worker_count,
            "max_retries_per_question": retry_limit,
            "planned_logical_calls": len(questions),
            "maximum_logical_calls": len(questions) * (retry_limit + 1),
            "warn_seconds": warning_after,
            "hard_timeout_seconds": float(hard_timeout_seconds or 0),
        }
    )
    yield ("stage_started", deepcopy(checkpoint))

    factories = []
    for question in questions:
        async def _call(question=question):
            try:
                return await _extract_question(
                    question,
                    source_registry=source_contract["source_registry"],
                    qualitative_context=qualitative_context,
                    max_retries=retry_limit,
                    input_char_limit=char_limit,
                    on_attempt_event=on_attempt_event,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return _degraded_question(
                    question,
                    code="unexpected_error",
                    message=str(exc).strip() or type(exc).__name__,
                    logical_attempts=0,
                    elapsed_seconds=0.0,
                )
        factories.append(_call)

    completed: dict[int, dict] = {}
    warned = False
    hard_timeout_hit = False
    try:
        async for event_type, payload in _run_bounded_calls(
            factories,
            worker_count,
            deadline=deadline,
        ):
            elapsed = time.monotonic() - started
            if not warned and warning_after and elapsed >= warning_after:
                warned = True
                yield (
                    "warning",
                    {
                        "code": "findings_stage_slow",
                        "elapsed_seconds": round(elapsed, 3),
                        "message": "逐题提炼已超过告警阈值，但不会中止。",
                    },
                )
            if event_type == "heartbeat":
                yield ("heartbeat", "")
                continue
            index, question_result = payload
            completed[index] = question_result
            yield ("question_result", deepcopy(question_result))
    except asyncio.TimeoutError:
        hard_timeout_hit = True

    if hard_timeout_hit:
        for index, question in enumerate(questions):
            if index in completed:
                continue
            result = _degraded_question(
                question,
                code="experiment_hard_timeout",
                message="显式试验硬超时已触发",
                logical_attempts=0,
                elapsed_seconds=time.monotonic() - started,
            )
            completed[index] = result
            yield ("question_result", deepcopy(result))

    ordered_results = [completed[index] for index in sorted(completed)]
    status_counts = {
        status: sum(1 for item in ordered_results if item.get("status") == status)
        for status in ("complete", "degraded", "skipped")
    }
    logical_calls = sum(
        int((item.get("diagnostics") or {}).get("logical_attempts") or 0)
        for item in ordered_results
    )
    upstream_attempts = sum(
        int((item.get("diagnostics") or {}).get("upstream_attempts") or 0)
        for item in ordered_results
    )
    artifact = {
        "schema_version": FINDINGS_SCHEMA_VERSION,
        "source_binding": source_contract["source_binding"],
        "source_registry": source_contract["source_registry"],
        "questions": ordered_results,
        "diagnostics": {
            **checkpoint["diagnostics"],
            "status": "partial" if status_counts["degraded"] else "complete",
            "status_counts": status_counts,
            "completed_question_count": len(ordered_results),
            "logical_calls": logical_calls,
            "upstream_attempts": upstream_attempts,
            "warning_emitted": warned,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "stop_reason": (
                "experiment_hard_timeout"
                if hard_timeout_hit
                else "all_questions_finished"
            ),
        },
    }
    yield ("result", artifact)
