"""Free draft -> registration -> one audit pass, with resumable checkpoints and opt-in trial budgets."""
import asyncio
from copy import deepcopy
import hashlib
import json
import re
import time
from app.services.llm_model_settings import call_model, model_operation, task_model_settings, model_configuration_fingerprint, record_model_snapshot
from app.core import config
from app.integrations.llm_client import collect_chat_completion
from app.services.report_postwrite_inputs import fingerprint, validate_frozen, check_comparisons
from app.services.report_writer_capacity import estimate_writer_capacity, unknown_capacity
from app.services.report_postwrite_audit import (
    messages, parse_json, registry_payload, validate_registry, REGISTRY_FORMAT,
    meaning_payload, bind_meanings, count_scope, COUNT_FORMAT, bind_members, aggregate_count,
    SOURCE_UNIT_FORMAT, LEGACY_SOURCE_UNIT_FORMAT, option_values, count_method, deterministic_count,
)


class BudgetStop(ValueError):
    pass


def estimate_tokens(request):
    # Deliberately conservative preflight; billing still comes from gateway usage.
    text = "\n".join(m["content"] for m in request)
    return int(sum(1.5 if ord(c) > 127 else 0.4 for c in text)) + 2048


_ERROR_CATEGORIES = {
    "output_truncated", "authentication_error", "rate_limited", "server_error",
    "connect_timeout", "read_timeout", "write_timeout", "pool_timeout", "timeout",
    "transport_error", "empty_output", "upstream_stream_error", "unexpected_error",
    "protocol_incompatible", "usage_option_incompatible", "content_filtered",
    "incomplete_output", "refusal", "http_error", "cancelled",
}
_TRUNCATED_REASONS = {"length", "max_tokens", "max_output_tokens"}
_ERROR_TYPES = {
    "RuntimeError", "ValueError", "TimeoutError", "CancelledError", "_LLMRequestError",
    "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException",
    "TransportError", "ConnectError", "ReadError", "WriteError", "CloseError", "ProxyError",
    "ProtocolError", "LocalProtocolError", "RemoteProtocolError", "DecodingError", "TooManyRedirects",
}


def _call_failure(entry, error, model):
    """Use structured upstream facts only; never persist exception bodies or text."""
    terminal = next((e for e in reversed(entry["events"]) if e.get("status") in {"failed", "completed"}), {})
    truncated = any(e.get("finish_reason") in _TRUNCATED_REASONS
                    or e.get("error_category") == "output_truncated" for e in entry["events"])
    category = "output_truncated" if truncated else terminal.get("error_category", "unknown_error")
    error_type = terminal.get("error_type") or type(error).__name__
    stage = "初稿" if entry["stage"] == "writer" else "本批"
    explanations = {
        "transport_error": "与模型服务通信时发生传输错误，未取得完整结果，具体断开原因尚未确认。",
        "read_timeout": "等待模型后续响应超时，未取得完整结果。",
        "connect_timeout": "连接模型服务超时，未取得完整结果。",
        "write_timeout": "向模型服务发送请求超时，未取得完整结果。",
        "pool_timeout": "等待可用连接超时，未取得完整结果。",
        "upstream_stream_error": "模型服务在返回响应的过程中报错，未取得完整结果。",
    }
    message = (f"{stage}输出达到本次上限（{entry['output_cap']:,} tokens），内容被截断，尚未完成。"
               "本次已停止，没有自动重试。") if truncated else (
                   explanations[category] + "本次已停止，没有自动重试。" if category in explanations else
                   f"模型调用失败（{error_type}），具体原因尚未确认。")
    return {"stage": entry["stage"], "model": model, "error_type": error_type,
            "wrapper_error_type": type(error).__name__, "call_id": terminal.get("call_id"),
            "request_id": terminal.get("request_id"),
            "error_category": category, "http_status": terminal.get("http_status"),
            "finish_reason": next((e["finish_reason"] for e in reversed(entry["events"])
                                   if e.get("finish_reason") in _TRUNCATED_REASONS), None),
            "output_cap": entry["output_cap"], "message": message}


class BoundedCaller:
    def __init__(self, *, collect=None, model=None, reasoning=None, max_calls=None, max_tokens=None,
                 max_seconds=None, call_seconds=None, output_tokens=None, registry_output_tokens=None,
                 writer_output_tokens=None,
                 on_event=None, ledger=None):
        self.collect = collect or collect_chat_completion
        self.model = model or config.LLM_POSTWRITE_MODEL
        self.reasoning = reasoning or config.LLM_POSTWRITE_REASONING
        self.max_calls = config.POSTWRITE_MAX_CALLS if max_calls is None else max_calls
        self.max_tokens = config.POSTWRITE_MAX_TOKENS if max_tokens is None else max_tokens
        self.max_seconds = config.POSTWRITE_MAX_SECONDS if max_seconds is None else max_seconds
        self.call_seconds = config.POSTWRITE_CALL_SECONDS if call_seconds is None else call_seconds
        self.output_tokens = output_tokens or config.POSTWRITE_OUTPUT_TOKENS
        # Explicit acceptance overrides remain compatible; production gets a writer-only cap.
        self.writer_output_tokens = min(64000, max(1024, int(
            writer_output_tokens if writer_output_tokens is not None else
            output_tokens if output_tokens is not None else config.POSTWRITE_WRITER_OUTPUT_TOKENS)))
        self.registry_output_tokens = min(64000, max(1024, int(
            registry_output_tokens if registry_output_tokens is not None else config.POSTWRITE_REGISTRY_OUTPUT_TOKENS)))
        writer = task_model_settings("insight.writer")
        if writer:
            self.model = writer["primary"]["model"]
            self.writer_output_tokens = writer["primary"]["max_tokens"]
        self.ledger = ledger if ledger is not None else []
        self.started = time.monotonic()
        self.on_event = on_event
        self.reserved = sum(c.get("charged_or_reserved_tokens", 0) for c in self.ledger)
        self.previous_seconds = sum(c.get("elapsed_seconds", 0) for c in self.ledger)

    async def call(self, stage, request):
        output_tokens = (self.writer_output_tokens if stage == "writer" else
                         self.registry_output_tokens if stage == "registry" else self.output_tokens)
        task_key = "insight." + (stage if stage in {"writer", "registry"} else "meaning" if stage.startswith("meaning:") else "quantity")
        managed = task_model_settings(task_key)
        if managed:
            output_tokens = max(c["max_tokens"] for c in [managed["primary"], *managed["fallbacks"]])
        reserve = (estimate_tokens(request) + output_tokens) * (managed["max_http_attempts"] or 1) if managed else estimate_tokens(request) + output_tokens
        remaining = (self.max_seconds - self.previous_seconds - (time.monotonic() - self.started)
                     if self.max_seconds > 0 else None)
        if (self.max_calls > 0 and len(self.ledger) >= self.max_calls
                or self.max_tokens > 0 and self.reserved + reserve > self.max_tokens
                or remaining is not None and remaining <= 0):
            raise BudgetStop("已达调用、tokens或时间预算，保留初稿及已完成核验")
        # Only budgeted trials fail closed on unknown spend; production keeps the
        # incomplete usage record but may resume the unfinished stage.
        budgeted = any(limit > 0 for limit in (self.max_calls, self.max_tokens, self.max_seconds, self.call_seconds))
        if budgeted and any(c.get("state") in {"started", "cancelled", "failed"}
                            and not c.get("usage_complete") for c in self.ledger):
            raise BudgetStop("上次请求用量不完整，停止继续付费调用")
        entry = {"stage": stage, "state": "started", "input_estimate": estimate_tokens(request),
                 "output_cap": output_tokens,
                 "charged_or_reserved_tokens": reserve, "events": [], "usage_complete": False}
        self.ledger.append(entry)
        self.reserved += reserve
        async def event(item):
            safe = {k: deepcopy(item[k]) for k in ("event", "status", "attempt", "model", "protocol", "usage", "usage_complete", "finish_reason", "fallback", "model_config_version", "model_task_key", "reasoning_effort") if k in item}
            if type(item.get("http_status")) is int and 100 <= item["http_status"] <= 599:
                safe["http_status"] = item["http_status"]
            if item.get("error_category") in _ERROR_CATEGORIES:
                safe["error_category"] = item["error_category"]
            if item.get("error_type") in _ERROR_TYPES:
                safe["error_type"] = item["error_type"]
            for key in ("call_id", "request_id"):
                if isinstance(item.get(key), str) and re.fullmatch(r"[0-9a-f]{32}", item[key]):
                    safe[key] = item[key]
            entry["events"].append(safe)
            if item.get("status") in {"completed", "failed"}:
                terminals = [e for e in entry["events"] if e.get("status") in {"completed", "failed"}]
                usages = [e.get("usage") or {} for e in terminals]
                entry["usage"] = {key: sum(u.get(key, 0) for u in usages) for key in ("input_tokens", "output_tokens")}
                entry["usage"]["total_tokens"] = sum(u.get("total_tokens", u.get("input_tokens", 0) + u.get("output_tokens", 0)) for u in usages)
                entry["usage_complete"] = bool(terminals) and all(e.get("usage_complete") for e in terminals)
            if self.on_event:
                result = self.on_event(item)
                if hasattr(result, "__await__"):
                    await result
        started = time.monotonic()
        try:
            deadlines = [limit for limit in (self.call_seconds, remaining) if limit is not None and limit > 0]
            pending = call_model(self.collect, task_key, request, models=[self.model], max_tokens=output_tokens,
                reasoning_effort=self.reasoning or None, max_http_attempts=(managed["max_http_attempts"] or 1) if managed else 1, on_attempt_event=event)
            text, model = await asyncio.wait_for(pending, timeout=min(deadlines)) if deadlines else await pending
            if any(e.get("status") != "failed" and e.get("finish_reason") in {"length", "max_tokens", "max_output_tokens"} for e in entry["events"]):
                raise ValueError("模型输出被截断，本批未完成")
            entry.update(state="complete", actual_model=model, output_chars=len(text))
            return text
        except asyncio.CancelledError:
            entry["state"] = "cancelled"
            raise
        except Exception as exc:
            entry.update(state="failed", error_type=type(exc).__name__)
            entry["failure"] = _call_failure(entry, exc, self.model)
            raise
        finally:
            entry["elapsed_seconds"] = round(time.monotonic() - started, 3)
            if entry.get("usage_complete"):
                usage = entry.get("usage") or {}
                actual = usage.get("total_tokens", usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
                self.reserved += actual - reserve
                entry["charged_or_reserved_tokens"] = actual

    def summary(self):
        return {"requests": len(self.ledger), "tokens": sum((c.get("usage") or {}).get("total_tokens",
                (c.get("usage") or {}).get("input_tokens", 0) + (c.get("usage") or {}).get("output_tokens", 0)) for c in self.ledger),
                "usage_complete": all(c.get("usage_complete") for c in self.ledger),
                "charged_or_reserved_tokens": self.reserved,
                "model_seconds": round(sum(c.get("elapsed_seconds", 0) for c in self.ledger), 3)}


def run_identity(frozen, prompts, instruction="", *, source_protocol=SOURCE_UNIT_FORMAT):
    validate_frozen(frozen)
    identity = {"frozen": frozen, "prompts": prompts, "instruction": instruction,
                            "model": config.LLM_POSTWRITE_MODEL, "reasoning": config.LLM_POSTWRITE_REASONING,
                            "protocols": [REGISTRY_FORMAT, source_protocol, COUNT_FORMAT],
                            "batch_sizes": [config.POSTWRITE_CLAIMS_PER_BATCH, config.POSTWRITE_PLAYERS_PER_BATCH]}
    if model_configuration_fingerprint():
        identity["model_configuration"] = model_configuration_fingerprint()
    return fingerprint(identity)


def initial_draft_state(frozen, *, prompts, draft, instruction=""):
    """Seed only a verified draft, never prior registry/audit answers."""
    if not isinstance(draft, str) or not draft.strip():
        raise ValueError("复用初稿不能为空")
    return {"identity": run_identity(frozen, prompts, instruction), "draft": draft,
            "draft_fingerprint": fingerprint(draft), "registry": None, "steps": {}, "calls": [],
            "prompts": deepcopy(prompts), "instruction": instruction, "elapsed_seconds": 0}


def reuse_registry_state(frozen, *, prompts, previous, legacy_note_dispositions=None, provenance=None):
    """Explicitly import only a saved registration; never manufacture audit verdicts."""
    packet = validate_frozen(frozen)
    raw = previous.get("registry_raw")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("没有可复用的登记原始响应")
    if previous.get("draft_fingerprint") != fingerprint(previous.get("draft")):
        raise ValueError("初稿指纹不一致")
    if previous.get("prompts") != prompts or previous.get("steps"):
        raise ValueError("本入口仅迁移相同业务规则下、尚未核验的登记结果")
    calls = previous.get("calls") or []
    if not calls or any(c.get("stage") != "registry" or c.get("state") != "complete"
                        or not c.get("usage_complete") or c.get("actual_model") != config.LLM_POSTWRITE_MODEL for c in calls):
        raise ValueError("旧登记请求未完整成功或模型不一致，不能复用")
    state = initial_draft_state(frozen, prompts=prompts, draft=previous["draft"], instruction=previous.get("instruction", ""))
    state["registry_raw"] = raw
    state["registry"] = validate_registry(parse_json(raw), state["draft"], packet,
        raw_registry=raw, legacy_note_dispositions=legacy_note_dispositions)
    state["calls"] = deepcopy(calls)
    state["usage"] = deepcopy(previous.get("usage", {}))
    state["elapsed_seconds"] = previous.get("elapsed_seconds", 0)
    state["responses"] = {"registry": raw}
    state["registry_reuse"] = {"source_identity": previous.get("identity"),
        "source_response_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "source_usage": deepcopy(previous.get("usage", {})), "provenance": deepcopy(provenance or {})}
    state["stop_reason"] = "登记结果已离线复用，原意和人数尚未核验"
    state["audit"] = audit_summary(state)
    return state


def reuse_meaning_state(frozen, *, prompts, previous, requests_by_stage, provenance=None, allow_partial=False):
    """Verify saved responses offline, preserving judgments and genuine missing items."""
    packet = validate_frozen(frozen)
    instruction = previous.get("instruction", "")
    current = previous.get("identity") == run_identity(frozen, prompts, instruction)
    if (not current and previous.get("identity") != run_identity(frozen, prompts, instruction, source_protocol=LEGACY_SOURCE_UNIT_FORMAT)
            or previous.get("prompts") != prompts
            or previous.get("draft_fingerprint") != fingerprint(previous.get("draft"))):
        raise ValueError("旧原意检查点的材料、协议、初稿或业务规则不一致")
    calls = previous.get("calls") or []
    if not calls or any(c.get("state") != "complete" or not c.get("usage_complete")
                        or c.get("actual_model") != config.LLM_POSTWRITE_MODEL for c in calls):
        raise ValueError("旧调用未完整完成或用量、模型不一致")
    raw_registry = previous.get("registry_raw", "")
    if hashlib.sha256(raw_registry.encode("utf-8")).hexdigest() != (previous.get("registry_reuse") or {}).get("source_response_sha256"):
        raise ValueError("旧登记响应指纹不一致")
    registry = validate_registry(parse_json(raw_registry), previous["draft"], packet, raw_registry=raw_registry,
        legacy_note_dispositions=(previous.get("registry") or {}).get("note_dispositions"))
    if registry != previous.get("registry") or not registry["complete"]:
        raise ValueError("登记结果不一致或未完成，禁止重新登记")
    steps = previous.get("steps") or {}
    if not steps or set(steps) != set(requests_by_stage) or any(not k.startswith("meaning:") for k in steps):
        raise ValueError("本入口只复用具有完整原请求的原意批次")
    state, reviewed, seen = deepcopy(previous), [], set()
    claims = {c["id"]: c for c in registry["claims"]}
    for stage, old_step in steps.items():
        request = requests_by_stage[stage]
        payload = json.loads(request[-1]["content"])
        ids = [c["id"] for c in payload["targets"]]
        if (len(set(ids)) != len(ids) or set(ids) & seen or any(i not in claims for i in ids)
                or stage != "meaning:" + fingerprint(ids)[:16]):
            raise ValueError("旧原意请求的目标编号不一致")
        expected = meaning_payload(previous["draft"], packet, [claims[i] for i in ids])
        legacy_request = request == messages(prompts["audit"], expected, LEGACY_SOURCE_UNIT_FORMAT)
        if not legacy_request and request != messages(prompts["audit"], expected):
            raise ValueError("旧原意请求与冻结材料不一致")
        if not current and not legacy_request:
            raise ValueError("旧检查点与原请求协议不一致")
        raw = old_step.get("raw")
        if not raw or raw != (previous.get("responses") or {}).get(stage) or sum(c.get("stage") == stage for c in calls) != 1:
            raise ValueError("旧原意响应与检查点或调用记录不一致")
        rebound = bind_meanings(parse_json(raw), expected)
        if not rebound["complete"]:
            missing_errors = [f"{cid}:观点未核验" for cid in rebound.get("missing", [])]
            if (not allow_partial or not current or not missing_errors
                    or sorted(rebound.get("errors", [])) != sorted(missing_errors)
                    or any(not c.get("complete") for c in rebound["checks"])):
                raise ValueError("旧原意响应存在实际缺项或无效判断，不能离线放行")
        if current and {**rebound, "raw": raw} != old_step:
            raise ValueError("已保存原意判断与原始响应不一致，禁止改判复用")
        state["steps"][stage] = {**rebound, "raw": raw}
        reviewed.append({"stage": stage, "target_ids": ids, "request_fingerprint": fingerprint(request),
                         "response_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                         "scope_notes": rebound["scope_notes"], "legacy_not_checked": rebound["legacy_not_checked"],
                         "missing": rebound["missing"]})
        seen.update(ids)
    state["identity"] = run_identity(frozen, prompts, instruction)
    state["meaning_reuse"] = {"source_identity": previous["identity"], "batches": reviewed,
                              "provenance": deepcopy(provenance or {})}
    state["stop_reason"] = "已有原意响应已离线复用，后续核验尚未完成"
    state["audit"] = audit_summary(state)
    return state


def pending_meaning_batches(state):
    """Repair omissions in saved batches first; never retry within the same run."""
    done = {c["id"] for step in state["steps"].values() for c in step.get("checks", []) if c.get("complete")}
    gaps = {cid for step in state["steps"].values() for cid in step.get("missing", [])} - done
    remaining = [c for c in state["registry"]["claims"] if c["id"] not in done]
    repairs = [[c] for c in remaining if c["id"] in gaps]
    untouched = [c for c in remaining if c["id"] not in gaps]
    return repairs + [untouched[i:i + config.POSTWRITE_CLAIMS_PER_BATCH]
                      for i in range(0, len(untouched), config.POSTWRITE_CLAIMS_PER_BATCH)]


@model_operation(resume_from="previous", persist_result=True)
async def run_pipeline(frozen, *, prompts, instruction="", previous=None, collect=None,
                       on_progress=None, on_checkpoint=None, on_event=None, caller_options=None, stop_after=None):
    if stop_after not in (None, "registry"):
        raise ValueError("不支持的阶段停止位置")
    if stop_after == "registry" and (not previous or not previous.get("draft")):
        raise ValueError("仅登记复测必须复用已有初稿")
    packet = validate_frozen(frozen)
    identity = run_identity(frozen, prompts, instruction)
    if previous and previous.get("identity") != identity:
        raise ValueError("来源、规则或模型配置与原核验不同，不能混用检查点")
    state = deepcopy(previous or {"identity": identity, "draft": "", "registry": None, "steps": {}, "calls": []})
    record_model_snapshot(state)
    state.pop("registration_only", None)
    state.pop("failure", None)
    state["stop_reason"] = ""
    state["prompts"] = deepcopy(prompts)
    state["instruction"] = instruction
    caller = BoundedCaller(collect=collect, ledger=state["calls"], on_event=on_event, **(caller_options or {}))
    previous_elapsed = (previous or {}).get("elapsed_seconds", caller.previous_seconds)
    caller.previous_seconds = previous_elapsed
    async def checkpoint():
        state["elapsed_seconds"] = round(previous_elapsed + time.monotonic() - caller.started, 3)
        state["usage"] = caller.summary()
        if on_checkpoint:
            await on_checkpoint(deepcopy(state))
    async def progress(stage, message, **extra):
        if on_progress:
            await on_progress({"phase": "postwrite", "stage": stage.split(":")[0], "status": "running", "message": message, **extra})
    async def invoke(stage, request):
        await progress(stage, {"writer": "正在依据研究目标撰写初稿", "registry": "正在整理报告中需要核对的内容"}.get(stage, "正在核对原意、引用和人数范围"))
        # Persist pending reservation before the network request as well as after it.
        task = asyncio.create_task(caller.call(stage, request))
        try:
            await asyncio.sleep(0)
            await checkpoint()
            answer = await task
            state.setdefault("responses", {})[stage] = answer
            return answer
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await checkpoint()
    try:
        if not state["draft"]:
            writer_request = messages(prompts["writer"], {**packet, "instruction": instruction}, "")
            await progress("writer_capacity", "正在检查写作容量", writer_capacity={
                "status": "checking", "message": "容量检查：正在估算本次输入及输出预留…", "impact": ""})
            await asyncio.sleep(0)
            try:
                state["writer_capacity"] = estimate_writer_capacity(writer_request, model=caller.model,
                    output_tokens=caller.writer_output_tokens, estimate_input=estimate_tokens)
            except Exception:
                state["writer_capacity"] = unknown_capacity()
            await progress("writer_capacity", "容量预估已结束，报告继续生成", status="completed",
                           writer_capacity=deepcopy(state["writer_capacity"]))
            # Advisory only: every result uses the same complete, original A request.
            state["draft"] = await invoke("writer", writer_request)
            if not state["draft"].strip():
                raise ValueError("写作未返回初稿")
            state["draft_fingerprint"] = fingerprint(state["draft"])
            await checkpoint()
        elif state.get("draft_fingerprint") != fingerprint(state["draft"]):
            raise ValueError("初稿指纹不一致")
        comparison = state.get("comparison_validation") or {}
        if (comparison.get("status") not in {"passed", "needs_review", "not_applicable"}
                or comparison.get("draft_fingerprint") != state["draft_fingerprint"]
                or comparison.get("input_fingerprint") != fingerprint(frozen)):
            state["comparison_validation"] = check_comparisons(frozen, state["draft"])
            await checkpoint()
        if state.get("registry_reuse"):
            raw = state.get("registry_raw", "")
            if hashlib.sha256(raw.encode("utf-8")).hexdigest() != state["registry_reuse"]["source_response_sha256"]:
                raise ValueError("复用登记响应发生变化")
            state["registry"] = validate_registry(parse_json(raw), state["draft"], packet, raw_registry=raw,
                legacy_note_dispositions=(state.get("registry") or {}).get("note_dispositions"))
            if not state["registry"]["complete"]:
                raise ValueError("复用登记仍有缺项，保留结果，不自动重新登记")
        if not state["registry"] or not state["registry"].get("complete"):
            raw = await invoke("registry", messages(prompts["registry"], registry_payload(state["draft"], packet), REGISTRY_FORMAT))
            state["registry_raw"] = raw
            state["registry"] = validate_registry(parse_json(raw), state["draft"], packet)
            await checkpoint()
        registry = state["registry"]
        if not registry["complete"]:
            raise ValueError("核对清单尚未整理完成，报告正文已保留")
        if stop_after == "registry":
            state["stop_reason"] = "登记阶段已完成，按复测范围停止；原意和人数尚未核验"
            return state
        # A resumed call checks only missing claims, never repeats successful ones.
        for batch in pending_meaning_batches(state):
            payload = meaning_payload(state["draft"], packet, batch)
            key = "meaning:" + fingerprint([c["id"] for c in batch])[:16]
            raw = await invoke(key, messages(prompts["audit"], payload))
            state["steps"][key] = {**bind_meanings(parse_json(raw), payload), "raw": raw}
            await checkpoint()
            if not state["steps"][key]["complete"]:
                raise ValueError("原意批次返回不完整，停止后续调用并保留已完成项")
        pending_counts = []
        for task in registry["tasks"]:
            key = "count:" + task["id"]
            try:
                players, units = count_scope(task, packet)
            except ValueError as exc:
                state["steps"][key] = {"complete": False, "errors": [str(exc)]}
                continue
            if count_method(task) != "semantic":
                state["steps"][key] = deterministic_count(task, players, units, packet)
            else:
                pending_counts.append((task, players, units))
            await checkpoint()
        # Send each player block once for all applicable tasks. Each task still has
        # its own question scope and membership validation; no cross-task totals.
        population = sorted({p for _, players, _ in pending_counts for p in players})
        for start in range(0, len(population), config.POSTWRITE_PLAYERS_PER_BATCH):
            group = population[start:start + config.POSTWRITE_PLAYERS_PER_BATCH]
            todo = []
            for task, players, units in pending_counts:
                scoped_players = [p for p in group if p in players]
                key = "members:" + task["id"] + ":" + fingerprint(scoped_players)[:12]
                scoped_units = [u for u in units if u["player"] in scoped_players]
                existing = state["steps"].get(key)
                if scoped_players and existing is not None:
                    rebound = bind_members({"members": existing.get("members", [])}, task, scoped_players, scoped_units)
                    structural = [e for e in existing.get("errors", []) if not e.endswith(":计数有歧义")]
                    rebound["errors"] = list(dict.fromkeys(rebound["errors"] + structural))
                    rebound["reviewed"] = rebound["complete"] = not rebound["errors"]
                    state["steps"][key] = rebound
                if scoped_players and not state["steps"].get(key, {}).get("reviewed"):
                    todo.append((key, task, scoped_players, scoped_units))
            if not todo:
                continue
            payload = {"tasks": [{"id": task["id"], "task": task, "players": players,
                                  "allowed_refs": [u["ref"] for u in units]} for _, task, players, units in todo],
                       "source_units": list({u["ref"]: u for _, _, _, units in todo for u in units}.values())}
            protocol = COUNT_FORMAT + '\n多任务批次：每项只使用该task的allowed_refs。返回 {"tasks":[{"id":"T001","members":[...]}]}，每个任务一次。'
            batchkey = "quantity:" + fingerprint([key for key, _, _, _ in todo])[:12]
            raw = await invoke(batchkey, messages(prompts["audit"], payload, protocol))
            answers = parse_json(raw).get("tasks") or []
            ids = [a.get("id") for a in answers]
            if len(ids) != len(set(ids)) or set(ids) != {task["id"] for _, task, _, _ in todo}:
                raise ValueError("人数任务返回缺失或重复，本批未完成")
            answers = {a["id"]: a for a in answers}
            for key, task, players, units in todo:
                state["steps"][key] = bind_members(answers[task["id"]], task, players, units)
            await checkpoint()
            if any(error for key, _, _, _ in todo for error in state["steps"][key].get("errors", [])
                   if not error.endswith(":计数有歧义")):
                raise ValueError("人数批次返回不完整，停止后续调用并保留已完成项")
        for task, players, units in pending_counts:
            members, errors = [], []
            for start in range(0, len(population), config.POSTWRITE_PLAYERS_PER_BATCH):
                group = [p for p in population[start:start + config.POSTWRITE_PLAYERS_PER_BATCH] if p in players]
                if not group:
                    continue
                key = "members:" + task["id"] + ":" + fingerprint(group)[:12]
                step = state["steps"].get(key, {})
                members.extend(step.get("members", []))
                errors.extend(step.get("errors", []))
            result = aggregate_count(task, players, members)
            result["errors"] = errors
            result["reviewed"] = result["reviewed"] and not errors
            result["complete"] = result["complete"] and not errors
            if not result["reviewed"]:
                result["review_status"] = "unreviewed"
            state["steps"]["count:" + task["id"]] = result
            await checkpoint()
    except asyncio.CancelledError:
        state["stop_reason"] = "cancelled"
    except Exception as exc:
        # Never expose vendor error bodies or credentials in persistent UI data.
        failure = (state["calls"][-1].get("failure") if not isinstance(exc, BudgetStop) and state["calls"]
                   and state["calls"][-1].get("state") == "failed" else None)
        if failure:
            state["failure"] = deepcopy(failure)
            state["stop_reason"] = failure["message"]
        else:
            state["stop_reason"] = str(exc) if isinstance(exc, (ValueError, BudgetStop)) else type(exc).__name__
    finally:
        state["audit"] = audit_summary(state)
        if stop_after == "registry":
            state["audit"]["status"] = "partial"
            state["registration_only"] = True
        await checkpoint()
    return state


def audit_summary(state):
    registry = state.get("registry") or {}
    checks = {}
    for step in state.get("steps", {}).values():
        for c in step.get("checks", []):
            if c.get("complete") or c["id"] not in checks:
                checks[c["id"]] = deepcopy(c)
    missing = [c["id"] for c in registry.get("claims", []) if not checks.get(c["id"], {}).get("complete")]
    counts = [deepcopy(state.get("steps", {}).get("count:" + t["id"], {"id": t["id"], "complete": False})) for t in registry.get("tasks", [])]
    reviewed_counts = sum(bool(c.get("reviewed", c.get("complete", False))) for c in counts)
    ambiguous_counts = sum(c.get("review_status") == "ambiguous" for c in counts)
    complete = bool(registry.get("complete")) and not missing and reviewed_counts == len(counts)
    return {"status": "complete" if complete else "partial", "checks": list(checks.values()), "counts": counts,
            "counts_reviewed": reviewed_counts, "counts_exact": sum(bool(c.get("exact", c.get("complete", False))) for c in counts),
            "counts_ambiguous": ambiguous_counts, "counts_unreviewed": len(counts) - reviewed_counts,
            "uncertain_members": sum(len(c.get("uncertain_players", [])) for c in counts), "has_ambiguity": bool(ambiguous_counts),
            "registration_status": "complete" if registry.get("complete") else "incomplete",
            "verification_notes": deepcopy(registry.get("verification_notes", [])),
            "registration_notes": deepcopy(registry.get("registration_notes", [])),
            "legacy_not_checked": deepcopy(registry.get("legacy_not_checked", [])),
            "registered_claims": len(registry.get("claims", [])), "checked_claims": sum(c.get("complete", False) for c in checks.values()),
            "missing_claims": missing, "registration_errors": registry.get("errors", []),
            "stop_reason": state.get("stop_reason", ""), "usage": deepcopy(state.get("usage") or {}),
            **({"writer_capacity": deepcopy(state["writer_capacity"])} if state.get("writer_capacity") else {}),
            "notice": "仅核对来源、原意和人数范围；提示供人工审阅，不自动改写正文。"}
