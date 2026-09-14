"""Bounded, question-local quick summaries. No SSE, persistence or prompt reads.

The caller owns authenticated input, stored prompts, checkpoints and versioning.
Every source is read, while ordinary low-value output may be omitted. Intermediate
batch summaries and final findings deliberately have different contracts.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
import inspect
import json
import time

from app.core.config import (
    DEFAULT_QUICK_BATCH_SUMMARY_SYSTEM, DEFAULT_QUICK_QUESTION_MERGE_SYSTEM,
    DEFAULT_QUICK_QUESTION_SUMMARY_SYSTEM, LLM_QUICK_REPORT_CALL_TIMEOUT_SECONDS,
    LLM_QUICK_REPORT_CONCURRENCY, LLM_QUICK_REPORT_FALLBACK_MODELS,
    LLM_QUICK_REPORT_HTTP_ATTEMPT_CAP, LLM_QUICK_REPORT_INPUT_CHARS,
    LLM_QUICK_REPORT_MAX_MERGE_LEVELS, LLM_QUICK_REPORT_MAX_TOKENS,
    LLM_QUICK_REPORT_MODEL, LLM_QUICK_REPORT_QUESTION_TIMEOUT_SECONDS,
    LLM_QUICK_REPORT_REASONING, LLM_QUICK_REPORT_STAGE_TIMEOUT_SECONDS,
)
from app.integrations.llm_client import collect_chat_completion
from app.services.report_quick_mode import (
    QuickStructureError, fill_question_evidence, parse_question_output, question_output_contract,
    restore_missing_risk_candidates,
)

ALGORITHM_VERSION = "question-summary-v6-step-fallback"
# Only this reviewed predecessor has the same profile-aware output semantics.
# Do not accept arbitrary old algorithms, even when their source rows match.
_CHECKPOINT_PREDECESSORS = {
    "question-summary-v4-shared-profile": {"question-summary-v3-profile"},
    "question-summary-v5-step-recovery": {"question-summary-v3-profile", "question-summary-v4-shared-profile"},
    "question-summary-v6-step-fallback": {"question-summary-v3-profile", "question-summary-v4-shared-profile", "question-summary-v5-step-recovery"},
}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _source_profile(source: dict) -> dict:
    """Only confirmed profile cells belong in model input, never source/ID metadata."""
    profile = source.get("profile")
    return deepcopy(profile) if isinstance(profile, dict) else {}


def _merge_payload(candidates: list[dict], by_id: dict, **scope) -> dict:
    """Lossless, request-local profile dictionary; each referenced row appears once.

    Column values are dictionaries indexed by integer codes. A null row cell
    means the field was absent; an actual null/zero/false is a catalog value.
    No player IDs or model-authored metadata can enter this table.
    """
    refs = list(dict.fromkeys(ref for item in candidates for ref in item["evidence_ids"]))
    profiles = {ref: _source_profile(by_id[ref]) for ref in refs}
    fields = sorted({field for profile in profiles.values() for field in profile})
    payload = {"candidates": candidates, **scope}
    if not fields:
        return payload
    columns = [{"name": field, "values": []} for field in fields]
    indexes = [{} for _ in fields]
    rows = {}
    for ref, profile in profiles.items():
        if not profile:
            continue
        row = []
        for column, index in zip(columns, indexes):
            field = column["name"]
            if field not in profile:
                row.append(None)
                continue
            value = profile[field]
            key = _json(value)  # Distinguish numeric zero, false, null and strings.
            if key not in index:
                index[key] = len(column["values"])
                column["values"].append(value)
            row.append(index[key])
        rows[ref] = row
    payload["profile_table"] = {"columns": columns, "rows": rows}
    return payload


def _checkpoint_fingerprint(inputs: dict, algorithm: str) -> str:
    material = {**inputs, "algorithm": algorithm}
    if algorithm in {"question-summary-v3-profile", "question-summary-v4-shared-profile"}:
        material["budgets"] = {**inputs["budgets"],
            "stage_seconds": LLM_QUICK_REPORT_STAGE_TIMEOUT_SECONDS,
            "question_seconds": LLM_QUICK_REPORT_QUESTION_TIMEOUT_SECONDS}
    if algorithm == "question-summary-v3-profile":
        # v3 hashed all source/profile cells, prompts, models and budgets, but
        # its code-owned contracts were covered only by the algorithm version.
        material.pop("contracts", None)
    return _digest(material)


def _load_success_checkpoint(checkpoint, inputs: dict, questions: list[dict]):
    """Validate immutable successes before admitting a compatible older run."""
    message = "快速总结缓存与当前输入或配置不一致，请完整重新生成"
    try:
        if not isinstance(checkpoint, dict):
            raise ValueError(message)
        algorithm = checkpoint.get("algorithm")
        supported = {ALGORITHM_VERSION, *_CHECKPOINT_PREDECESSORS.get(ALGORITHM_VERSION, ())}
        if algorithm not in supported or checkpoint.get("fingerprint") != _checkpoint_fingerprint(inputs, algorithm):
            raise ValueError(message)
        stored = checkpoint.get("questions")
        provenance = checkpoint.get("question_algorithms", {})
        if not isinstance(stored, list) or not isinstance(provenance, dict):
            raise ValueError(message)
        originals = {q["question_key"]: q for q in questions}
        cached, producers = {}, {}
        for item in stored:
            key = item["question_key"]
            original = originals[key]
            producer = provenance.get(key, algorithm)
            if key in cached or item.get("status") != "complete" or producer not in supported:
                raise ValueError(message)
            if _json({field: item[field] for field in original}) != _json(original):
                raise ValueError(message)
            if producer != ALGORITHM_VERSION and any(not isinstance(s.get("profile"), dict) for s in original["sources"]):
                raise ValueError(message)
            # Stored quotes must still equal authoritative frozen rows. Parse
            # the model fields without their server-owned evidence cards.
            findings = []
            for finding in item["findings"]:
                model_fields = {k: v for k, v in finding.items() if k != "evidence"}
                if _json(finding.get("evidence")) != _json(fill_question_evidence([model_fields], original["sources"])[0]["evidence"]):
                    raise ValueError(message)
                findings.append(model_fields)
            required = _risk_sources(findings)
            parse_question_output(_json({"schema_version": 2, "stage": "question", "findings": findings,
                                         "empty_reason": item.get("empty_reason", "")}),
                                  original["sources"], required_risk_ids=required)
            cached[key] = deepcopy(item)
            producers[key] = producer
        return cached, producers
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError(message) from exc


class _Stopped(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _CheckpointError(RuntimeError):
    pass


async def _notify(callback, value):
    if callback:
        pending = callback(deepcopy(value))
        if inspect.isawaitable(pending):
            await pending


async def _gather_calls(coroutines):
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _normalize_questions(questions) -> list[dict]:
    if not isinstance(questions, list):
        raise ValueError("questions must be a list")
    seen = set()
    result = []
    for question in questions:
        if not isinstance(question, dict) or not str(question.get("question_key", "")).strip():
            raise ValueError("每题必须有稳定 question_key")
        key = str(question["question_key"])
        if key in seen:
            raise ValueError("question_key 不能重复")
        seen.add(key)
        sources = question.get("sources")
        if not isinstance(sources, list):
            raise ValueError("每题必须提供完整 sources")
        ids = set()
        normalized = []
        for source in sources:
            if not isinstance(source, dict) or not isinstance(source.get("text"), str):
                raise ValueError("原文必须是字符串")
            if not source["text"].strip():
                continue
            rid = source.get("response_id")
            if not isinstance(rid, str) or not rid or rid in ids:
                raise ValueError("每题原文 response_id 必须非空且唯一")
            ids.add(rid)
            normalized.append(deepcopy(source))
        result.append({**deepcopy(question), "question_key": key, "sources": normalized})
    return result


def split_source_batches(sources: list[dict], max_chars: int) -> list[list[dict]]:
    """Pack serialized fragments without sampling or dropping a long answer tail.

    Offsets are Python Unicode code-point offsets, not byte offsets. A source ID
    stays unchanged across fragments, so fragments can never masquerade as people.
    max_chars is the complete JSON array budget, including escapes and metadata.
    """
    if max_chars < 256:
        raise _Stopped("input_context_exceeded")
    fragments = []
    for source in sources:
        text = source["text"]
        offset = 0
        while offset < len(text):
            def fragment(end):
                return {"response_id": source["response_id"], "text": text[offset:end],
                        "offset": offset, "end_offset": end, "source_length": len(text),
                        "profile": _source_profile(source)}
            low, high = offset + 1, len(text)
            best = None
            while low <= high:
                end = (low + high) // 2
                item = fragment(end)
                if len(_json([item])) <= max_chars:
                    best = item
                    low = end + 1
                else:
                    high = end - 1
            if best is None:
                raise _Stopped("input_context_exceeded")
            fragments.append(best)
            offset = best["end_offset"]
    return _pack_items(fragments, max_chars)


def _pack_items(items: list[dict], max_chars: int, *, measure=None) -> list[list[dict]]:
    measure = measure or (lambda group: len(_json(group)))
    result, current = [], []
    for item in items:
        if measure([item]) > max_chars:
            raise _Stopped("merge_context_exceeded")
        if current and measure(current + [item]) > max_chars:
            result.append(current)
            current = []
        current.append(item)
    if current:
        result.append(current)
    return result


def _risk_sources(candidates):
    result = {}
    for item in candidates:
        for risk_id in item.get("risk_ids", []):
            result.setdefault(risk_id, set()).update(item["evidence_ids"])
    return result


async def run_quick_pipeline(questions, *, background="", checkpoint=None,
                             retry_failed=False, on_progress=None, on_checkpoint=None,
                             collect=None, prompts=None) -> dict:
    """Return complete/failed scopes; cancellation propagates to all model calls.

    Only retry_failed=True consumes a matching checkpoint. Complete regeneration
    ignores caches. The checkpoint callback receives successful scopes only, and
    is serialized so slow storage cannot overwrite a newer successful checkpoint.
    Validated steps of unfinished questions are also saved. Their exact request
    digests and strict output validation gate reuse; old algorithms reuse only
    whole-question successes, never unverified intermediate state.
    """
    questions = _normalize_questions(questions)
    collect = collect or collect_chat_completion
    prompts = dict(prompts or {})
    systems = {
        "question": prompts.get("quick_question_summary_system") or DEFAULT_QUICK_QUESTION_SUMMARY_SYSTEM,
        "batch": prompts.get("quick_batch_summary_system") or DEFAULT_QUICK_BATCH_SUMMARY_SYSTEM,
        "merge": prompts.get("quick_question_merge_system") or DEFAULT_QUICK_QUESTION_MERGE_SYSTEM,
    }
    budgets = {
        "concurrency": LLM_QUICK_REPORT_CONCURRENCY,
        "stage_seconds": None,
        "question_seconds": None,
        "call_seconds": LLM_QUICK_REPORT_CALL_TIMEOUT_SECONDS,
        "http_attempt_cap": LLM_QUICK_REPORT_HTTP_ATTEMPT_CAP,
        "input_chars": LLM_QUICK_REPORT_INPUT_CHARS,
        "merge_levels": LLM_QUICK_REPORT_MAX_MERGE_LEVELS,
        "max_tokens": LLM_QUICK_REPORT_MAX_TOKENS,
    }
    models = tuple(dict.fromkeys(m for m in (LLM_QUICK_REPORT_MODEL, *LLM_QUICK_REPORT_FALLBACK_MODELS) if m))
    fingerprint_inputs = {"questions": questions, "background": background, "prompts": systems,
                          "budgets": budgets, "models": models, "reasoning": LLM_QUICK_REPORT_REASONING,
                          "contracts": {f"{stage}:{merging}": question_output_contract(stage, merging=merging)
                                        for stage in ("batch", "question") for merging in (False, True)}}
    fingerprint = _checkpoint_fingerprint(fingerprint_inputs, ALGORITHM_VERSION)
    cached, producers = {}, {}
    if retry_failed and checkpoint is not None:
        cached, producers = _load_success_checkpoint(checkpoint, fingerprint_inputs, questions)
    steps = {}
    if retry_failed and checkpoint and checkpoint.get("algorithm") in {ALGORITHM_VERSION, "question-summary-v5-step-recovery"}:
        stored_steps = checkpoint.get("steps", {})
        known_keys = {q["question_key"] for q in questions}
        if not isinstance(stored_steps, dict) or set(stored_steps) - known_keys:
            raise ValueError("快速总结步骤缓存无效，请完整重新生成")
        steps = deepcopy(stored_steps)
        if any(not isinstance(value, dict) for value in steps.values()):
            raise ValueError("快速总结步骤缓存无效，请完整重新生成")
    work_keys = {q["question_key"] for q in questions} - cached.keys()
    started = time.perf_counter()
    results = {}
    diagnostics = {}
    question_slots = asyncio.Semaphore(budgets["concurrency"])
    call_slots = asyncio.Semaphore(budgets["concurrency"])
    checkpoint_lock = asyncio.Lock()

    def current_checkpoint():
        return {"fingerprint": fingerprint, "algorithm": ALGORITHM_VERSION,
                "steps": {key: deepcopy(value) for key, value in steps.items()
                          if value and results.get(key, {}).get("status") != "complete"},
                "question_algorithms": {key: producers.get(key, ALGORITHM_VERSION) for key, value in results.items()
                                        if value.get("status") == "complete"},
                "questions": [deepcopy(results[q["question_key"]]) for q in questions
                              if results.get(q["question_key"], {}).get("status") == "complete"]}

    async def save_checkpoint():
        async with checkpoint_lock:
            try:
                await _notify(on_checkpoint, current_checkpoint())
            except Exception as exc:
                raise _CheckpointError(str(exc)) from exc

    async def progress(question, status, **detail):
        await _notify(on_progress, {"phase": "quick_questions", "question_key": question["question_key"],
                                    "question": question.get("question", ""), "status": status,
                                    "completed": len(results), "total": len(questions),
                                    "run_total": len(work_keys), "run_question_keys": sorted(work_keys),
                                    "run_completed": sum(results.get(k, {}).get("status") == "complete" for k in work_keys),
                                    **detail})

    async def summarize(question, diag):
        sources = question["sources"]
        if not sources:
            return {**question, "status": "complete", "findings": [], "empty_reason": "本题没有有效文字回答。"}
        base = {"question_key": question["question_key"], "question": question.get("question", ""),
                "background": background, "total_responses": len(sources)}
        overhead = len(_json(base)) + max(len(v) for v in systems.values()) + max(
            len(question_output_contract(stage, merging=merging))
            for stage in ("batch", "question") for merging in (False, True)
        ) + 512
        available = budgets["input_chars"] - overhead
        batches = split_source_batches(sources, available)
        diag["batch_count"] = len(batches)
        diag["fragment_count"] = sum(len(batch) for batch in batches)
        fragment_counts = Counter(item["response_id"] for batch in batches for item in batch)
        diag["fragmented_source_count"] = sum(count > 1 for count in fragment_counts.values())
        diag["planned_logical_calls_min"] = len(batches) + (len(batches) > 1)
        diag["planned_steps"] = len(batches) + (len(batches) > 1)
        diag["planned_logical_calls_upper"] = budgets["http_attempt_cap"] * diag["planned_steps"]
        question_steps = steps.setdefault(question["question_key"], {})

        def messages_for(payload, *, stage, system_key):
            contract = question_output_contract(stage, merging=system_key == "merge")
            return [{"role": "system", "content": systems[system_key] + "\n\n" + contract},
                    {"role": "user", "content": _json({**base, **payload})}]

        def merge_chars(payload, *, stage):
            return sum(len(message["content"]) for message in messages_for(payload, stage=stage, system_key="merge"))

        async def request(payload, *, stage, system_key, allowed, required=()):
            messages = messages_for(payload, stage=stage, system_key=system_key)
            step_key = _digest({"messages": messages, "stage": stage, "operation": system_key})
            cached_step = question_steps.get(step_key)
            if cached_step is not None:
                try:
                    parsed = cached_step["result"]
                    if cached_step["digest"] != _digest(parsed):
                        raise ValueError("invalid step digest")
                    parsed = parse_question_output(_json(parsed), allowed, stage=stage, required_risk_ids=required)
                except (KeyError, TypeError, ValueError):
                    question_steps.pop(step_key, None)
                    diag["rejected_steps"] += 1
                else:
                    diag["reused_steps"] += 1
                    await progress(question, "running", step_status="reused", operation=system_key,
                                   batch_index=payload.get("batch_index"), reused_steps=diag["reused_steps"])
                    return parsed
            if system_key == "merge":
                diag["merge_inputs"].append({"stage": stage, "merge_level": payload.get("merge_level"),
                    "candidate_count": len(payload["candidates"]), "candidate_chars": len(_json(payload["candidates"])),
                    "profile_chars": len(_json(payload["profile_table"])) if "profile_table" in payload else 0,
                    "profile_rows": len(payload.get("profile_table", {}).get("rows", {})),
                    "message_chars": sum(len(message["content"]) for message in messages)})

            terminal_error_category = None

            async def call(attempt):
                nonlocal terminal_error_category
                terminal_error_category = None
                # Own the finite retry budget here, across model and structure
                # failures. An outer timeout must not prevent fallback, nor may
                # the client exhaust every HTTP attempt on the primary model.
                selected_model = models[min(attempt, len(models) - 1)] if models else None
                message_chars = sum(len(message["content"]) for message in messages)
                if message_chars > budgets["input_chars"]:
                    raise _Stopped("input_context_exceeded")
                queued_at = time.perf_counter()
                async with call_slots:
                    diag["call_queue_seconds"] += time.perf_counter() - queued_at
                    diag["logical_calls"] += 1
                    diag["model_input_chars"] += message_chars
                    observed = False

                    async def observe(event):
                        nonlocal observed, terminal_error_category
                        if event.get("status") == "started":
                            if not observed:
                                diag["observed_logical_calls"] += 1
                                observed = True
                            diag["http_attempts"] += 1
                        if event.get("fallback") or (models and event.get("model") and event["model"] != models[0]):
                            diag["fallback"] = True
                        if event.get("status") in {"completed", "failed"}:
                            terminal_error_category = event.get("error_category")
                            diag["attempts"].append({key: event.get(key) for key in (
                                "status", "model", "protocol", "attempt_kind", "elapsed_seconds",
                                "error_category", "finish_reason", "usage", "usage_complete")})

                    call_started = time.perf_counter()
                    try:
                        async with asyncio.timeout(budgets["call_seconds"]):
                            output = await collect(messages, models=(selected_model,) if selected_model else None,
                                                   max_tokens=budgets["max_tokens"], reasoning_effort=LLM_QUICK_REPORT_REASONING,
                                                   max_http_attempts=1, on_attempt_event=observe)
                    except TimeoutError as exc:
                        raise _Stopped("call_timeout") from exc
                    finally:
                        diag["model_call_seconds"] += time.perf_counter() - call_started
                    answer, model = output if isinstance(output, tuple) else (output, "")
                    if model and model not in diag["models"]:
                        diag["models"].append(model)
                    if models and model and model != models[0]:
                        diag["fallback"] = True
                    diag["output_chars"] += len(str(answer))
                    return answer

            def record_validation(error, *, repairing):
                # These values are generated by code, not copied from a model.
                # Question/source text and invalid field values never enter logs.
                issues = (error.issues if isinstance(error, QuickStructureError)
                          else [{"code": "invalid_json", "path": "$"}])
                diagnostic = {"stage": stage, "operation": system_key,
                              "batch_index": payload.get("batch_index"),
                              "merge_level": payload.get("merge_level"),
                              "repairing": repairing, "issues": deepcopy(issues)}
                if getattr(error, "json_location", None):
                    diagnostic["json_location"] = error.json_location
                diag["structure_validation_errors"].append(diagnostic)
                return issues

            async def accept(answer, *, repairing):
                try:
                    parsed = parse_question_output(answer, allowed, stage=stage, required_risk_ids=required)
                except ValueError as exc:
                    record_validation(exc, repairing=repairing)
                    if system_key != "merge":
                        raise
                    parsed, count = restore_missing_risk_candidates(answer, allowed, payload["candidates"],
                                                                    stage=stage, required_risk_ids=required)
                    diag["restored_risks"] += count
                question_steps[step_key] = {"result": deepcopy(parsed), "digest": _digest(parsed),
                    "operation": system_key, "merge_level": payload.get("merge_level")}
                await save_checkpoint()
                await progress(question, "running", step_status="complete", operation=system_key,
                               batch_index=payload.get("batch_index"), reused_steps=diag["reused_steps"])
                return parsed

            repairing = False
            for attempt in range(budgets["http_attempt_cap"]):
                try:
                    answer = await call(attempt)
                except Exception as exc:
                    if isinstance(exc, _Stopped) and exc.code != "call_timeout":
                        raise
                    if terminal_error_category in {"authentication_error", "refusal"} or attempt + 1 >= budgets["http_attempt_cap"]:
                        raise
                    diag["automatic_retries"] += 1
                    reason = exc.code if isinstance(exc, _Stopped) else terminal_error_category or "model_error"
                    diag["step_retries"].append({"operation": system_key, "batch_index": payload.get("batch_index"),
                                                 "attempt": attempt + 1, "reason": reason})
                    await progress(question, "running", step_status="retrying", operation=system_key,
                                   batch_index=payload.get("batch_index"), reason=reason)
                    continue
                try:
                    return await accept(answer, repairing=repairing)
                except QuickStructureError as exc:
                    if repairing or attempt + 1 >= budgets["http_attempt_cap"]:
                        raise _Stopped("invalid_structure") from exc
                    repairing = True
                    diag["structural_repairs"] += 1
                    # Regenerate from full authoritative input on the next
                    # configured model. Do not inject malformed private output.
                    feedback_issues = exc.issues[:3]
                    while True:
                        feedback = ("\n上次 JSON 结构校验失败：" + _json({"errors": feedback_issues})
                                    + "。按这些字段路径和上方当前阶段契约重新生成；引用只能逐字复制本次输入编号。"
                                    + ("原文提炼阶段不要生成 risk_ids；有风险只设置 risk=true。"
                                       if system_key != "merge" else "逐项保留输入风险编号与对应原文引用，勿将风险编号当作 evidence_ids。"))
                        if len(feedback) <= 512 or len(feedback_issues) <= 1:
                            break
                        feedback_issues.pop()
                    messages[0]["content"] += feedback

        if len(batches) == 1:
            parsed = await request({"sources": batches[0]}, stage="question", system_key="question", allowed=sources)
        else:
            candidates = []
            # Every batch uses the same global call semaphore, including when
            # multiple questions run. Failure cancels pending sibling calls.
            async def extract_batch(batch_index, batch):
                return await request({"sources": batch, "batch_index": batch_index,
                                              "batch_count": len(batches), "batch_unique_responses": len({s["response_id"] for s in batch})},
                                             stage="batch", system_key="batch", allowed=batch)
            parsed_batches = await _gather_calls(extract_batch(i, batch) for i, batch in enumerate(batches))
            for batch_index, (batch, parsed_batch) in enumerate(zip(batches, parsed_batches)):
                for index, candidate in enumerate(parsed_batch["candidates"]):
                    if candidate["risk"]:
                        candidate["risk_ids"] = [f"risk-{batch_index}-{index}"]
                    candidate["batch_scope"] = {"batch_index": batch_index,
                                                "unique_responses": len({s["response_id"] for s in batch}),
                                                "contains_fragments": any(s["offset"] > 0 or s["end_offset"] < s["source_length"] for s in batch)}
                    candidates.append(candidate)
            diag["candidate_count"] = len(candidates)
            if not candidates:
                parsed = {"findings": [], "empty_reason": "已阅读全部回答，未发现可归纳的实质意见。"}
            else:
                by_id = {source["response_id"]: source for source in sources}
                # Use the actual whole request, including the shared table,
                # system contract and a reserved structural-repair allowance.
                merge_budget = budgets["input_chars"] - 512
                def final_payload(items):
                    return _merge_payload(items, by_id, whole_question=True)
                def final_chars(items):
                    return merge_chars(final_payload(items), stage="question")
                level = 0
                while final_chars(candidates) > merge_budget:
                    if level >= budgets["merge_levels"]:
                        raise _Stopped("merge_level_limit")
                    def group_payload(items):
                        return _merge_payload(items, by_id, merge_level=level, whole_question=False)
                    merged = []
                    round_started, calls_before = time.perf_counter(), diag["logical_calls"]
                    round_diag = {"level": level, "input_candidates": len(candidates), "input_chars": final_chars(candidates),
                                  "group_count": 0, "status": "running"}
                    diag["merge_rounds"].append(round_diag)
                    try:
                        groups = _pack_items(candidates, merge_budget,
                                             measure=lambda items: merge_chars(group_payload(items), stage="batch"))
                        round_diag["group_count"] = len(groups)
                        diag["planned_steps"] += sum(len(group) > 1 for group in groups)
                        diag["planned_logical_calls_upper"] = budgets["http_attempt_cap"] * diag["planned_steps"]
                        for group in groups:
                            if len(group) == 1:
                                merged.extend(group)
                                continue
                            allowed_ids = {ref for item in group for ref in item["evidence_ids"]}
                            reduced = await request(group_payload(group), stage="batch", system_key="merge",
                                                    allowed=[by_id[ref] for ref in allowed_ids], required=_risk_sources(group))
                            for candidate in reduced["candidates"]:
                                # Tree compression cannot establish prevalence.
                                candidate["frequency"] = "暂无法判断"
                            merged.extend(reduced["candidates"])
                        output_chars = final_chars(merged)
                        round_diag.update(output_candidates=len(merged), output_chars=output_chars,
                                          reduction_ratio=round(1 - output_chars / round_diag["input_chars"], 4))
                        if output_chars >= round_diag["input_chars"]:
                            # Valid JSON is not a usable merge when the round
                            # makes no progress. Do not replay that same dead end.
                            for token, entry in list(question_steps.items()):
                                if entry.get("operation") == "merge" and entry.get("merge_level") == level:
                                    question_steps.pop(token)
                            await save_checkpoint()
                            raise _Stopped("merge_no_progress")
                        round_diag["status"] = "complete"
                    except BaseException as exc:
                        round_diag["status"] = exc.code if isinstance(exc, _Stopped) else "interrupted"
                        raise
                    finally:
                        round_diag["elapsed_seconds"] = round(time.perf_counter() - round_started, 3)
                        round_diag["logical_calls"] = diag["logical_calls"] - calls_before
                    candidates = merged
                    level += 1
                    diag["merge_levels"] = level
                allowed_ids = {ref for item in candidates for ref in item["evidence_ids"]}
                parsed = await request(final_payload(candidates), stage="question", system_key="merge",
                                       allowed=[s for s in sources if s["response_id"] in allowed_ids], required=_risk_sources(candidates))
                if diag["fragmented_source_count"] or level:
                    # Fragmentation/tree compression does not retain prevalence
                    # accounting; fail closed on frequency while retaining text.
                    for finding in parsed["findings"]:
                        finding["frequency"] = "暂无法判断"
        findings = fill_question_evidence(parsed["findings"], sources)
        diag["finding_count"] = len(findings)
        return {**question, "status": "complete", "findings": findings,
                "empty_reason": parsed.get("empty_reason", "")}

    async def run_question(question):
        key = question["question_key"]
        diag = {"logical_calls": 0, "http_attempts": 0, "observed_logical_calls": 0,
                "attempts": [], "models": [], "fallback": False, "structural_repairs": 0,
                "structure_validation_errors": [],
                "merge_inputs": [], "merge_rounds": [],
                "reused_steps": 0, "rejected_steps": 0, "restored_risks": 0,
                "automatic_retries": 0, "step_retries": [],
                "call_queue_seconds": 0.0, "model_call_seconds": 0.0,
                "input_responses": len(question["sources"]), "input_chars": sum(len(s["text"]) for s in question["sources"]),
                "output_chars": 0, "model_input_chars": 0, "batch_count": 0, "fragment_count": 0, "merge_levels": 0,
                "reused": False, "stop_reason": "complete"}
        diagnostics[key] = diag
        if key in cached:
            results[key] = deepcopy(cached[key])
            diag["reused"] = True
            diag["source_algorithm"] = producers[key]
            diag["elapsed_seconds"] = 0
            await progress(question, "reused")
            return
        queued_at = time.perf_counter()
        async with question_slots:
            diag["question_queue_seconds"] = round(time.perf_counter() - queued_at, 3)
            question_started = time.perf_counter()
            await progress(question, "running")
            try:
                result = await summarize(question, diag)
            except _CheckpointError:
                raise
            except Exception as exc:
                diag["stop_reason"] = exc.code if isinstance(exc, _Stopped) else "model_error"
                result = {**question, "status": "failed", "findings": [], "error": diag["stop_reason"]}
            finally:
                diag["elapsed_seconds"] = round(time.perf_counter() - question_started, 3)
            results[key] = result
            if result["status"] == "complete":
                steps.pop(key, None)
            await save_checkpoint()
            await progress(question, result["status"])

    tasks = [asyncio.create_task(run_question(q)) for q in questions]
    try:
        await asyncio.gather(*tasks)
    except _CheckpointError as exc:
        raise exc.__cause__ from exc
    finally:
        # Also runs for user cancellation or a failed persistence callback.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for question in questions:
        key = question["question_key"]
        if key not in results:
            results[key] = {**question, "status": "failed", "findings": [], "error": "interrupted"}
            diagnostics[key]["stop_reason"] = "interrupted"
        diagnostics[key]["http_attempts_known"] = diagnostics[key]["logical_calls"] == diagnostics[key]["observed_logical_calls"]
        for field in ("call_queue_seconds", "model_call_seconds"):
            diagnostics[key][field] = round(diagnostics[key][field], 3)
    ordered = [results[q["question_key"]] for q in questions]
    status = "complete" if all(q["status"] == "complete" for q in ordered) else "partial"
    diagnostic_result = {"algorithm": ALGORITHM_VERSION, "fingerprint": fingerprint, "budgets": budgets,
                         "elapsed_seconds": round(time.perf_counter() - started, 3),
                         "logical_calls": sum(d["logical_calls"] for d in diagnostics.values()),
                         "http_attempts": sum(d["http_attempts"] for d in diagnostics.values()),
                         "http_attempts_known": all(d["http_attempts_known"] for d in diagnostics.values()),
                         "completed_questions": sum(q["status"] == "complete" for q in ordered),
                         "failed_questions": sum(q["status"] != "complete" for q in ordered),
                         "reused_questions": sum(d["reused"] for d in diagnostics.values()),
                         "reused_steps": sum(d["reused_steps"] for d in diagnostics.values()),
                         "restored_risks": sum(d["restored_risks"] for d in diagnostics.values()),
                         "max_repairs_per_step": 1,
                         "stop_reason": status, "questions": diagnostics}
    return {"questions": ordered, "report_status": status, "diagnostics": diagnostic_result, "checkpoint": current_checkpoint()}
