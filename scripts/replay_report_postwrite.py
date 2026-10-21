"""Bounded four-case integration replay. Default is prepare-only; --live spends one call.

Run with an explicit output directory and isolated DATA_DIR. Never imports app.main.
The optional demo serves synthetic UI fixtures; it cannot contact a model.
"""
import argparse
import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import runpy
import re
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.dont_write_bytecode = True


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def verify_statistics(packet):
    """Recompute this frozen fixture's tables from selections, never model counts."""
    rows = packet["records"]
    if not rows or len({r["source_row"] for r in rows}) != len(rows) or len({r["player"] for r in rows}) != len(rows):
        raise ValueError("样本物理行或玩家重复")
    for row in rows:
        if int(row["player"][1:]) != row["source_row"]:
            raise ValueError("玩家编号与物理行不一致")
        for col, value in row["selections_zh"].items():
            if packet["option_mapping"][col].get(row["answers"][col]) != value:
                raise ValueError("客观题选项映射不一致")
    calculated = deepcopy(packet["statistics"])
    def percentage(n, total):
        return round(n * 100 / total, 1) if total else 0
    for ref, col in (("S01", "F"), ("S02", "G"), ("S03", "K")):
        for item in calculated[ref]["data"]:
            item["n"] = sum(r["selections_zh"][col] == item["value"] for r in rows)
            item["denominator"] = len(rows)
            item["percent"] = percentage(item["n"], len(rows))
        if sum(x["n"] for x in calculated[ref]["data"]) != len(rows):
            raise ValueError("客观统计选项不完整")
    for ref, col in (("S04", "F"), ("S05", "K")):
        for group in calculated[ref]["data"]:
            members = [r for r in rows if r["selections_zh"][col] == group["group"]]
            group["denominator"] = len(members)
            for item in group["ratings"]:
                item["n"] = sum(r["selections_zh"]["G"] == item["value"] for r in members)
                item["percent"] = percentage(item["n"], len(members))
    answered = lambda r: bool(str(r["answers"].get("O", "")).strip())
    boring = [r for r in rows if r["selections_zh"]["G"] == "有点无聊"]
    other = [r for r in rows if r not in boring]
    calculated["S06"].update(nonempty=sum(map(answered, rows)), blank=sum(not answered(r) for r in rows),
                             denominator=len(rows), positive_or_neutral={"answered": sum(map(answered, other)), "eligible": len(other)},
                             boring={"answered": sum(map(answered, boring)), "eligible": len(boring)})
    for group in calculated["S07"]["data"]:
        members = [r for r in rows if group["group"] == "全体" or r["selections_zh"]["K"] == group["group"]]
        positive = [r["player"] for r in members if r["selections_zh"]["G"] in {"非常有趣", "比较有趣"}]
        group.update(positive_players=positive, positive_n=len(positive), denominator=len(members),
                     positive_percent=percentage(len(positive), len(members)), other_n=len(members)-len(positive),
                     other_percent=percentage(len(members)-len(positive), len(members)))
    n = sum(r["selections_zh"]["K"] == "偏好普通乱斗" and r["selections_zh"]["G"] in {"非常有趣", "比较有趣"} for r in rows)
    calculated["S07"]["brawl_positive_share_among_all"] = {"numerator": n, "denominator": len(rows), "percent": percentage(n, len(rows))}
    if calculated != packet["statistics"]:
        raise ValueError("Python复算与冻结统计不一致，未调用模型")
    return calculated


def adapt_full_packet(original):
    from app.services.report_postwrite_inputs import ENGINE, fingerprint
    statistics = verify_statistics(original)
    questions, units, source_questions = [], [], []
    for col, question in original["questions"].items():
        role = "open_text" if question["type"] == "open" else question["type"]
        definition = {**deepcopy(question), "options": deepcopy(original["option_mapping"].get(col, {})),
                      "branch": original.get("branches", {}).get(col, "")}
        questions.append({"key": col, "title": question["translation"], "type": role, "selected": True, "definition": definition})
        sources = []
        for row in original["records"]:
            raw = row["answers"].get(col, "")
            if not str(raw).strip():
                continue
            value = row["selections_zh"].get(col, raw)
            unit = {"ref": f'{col}{row["source_row"]}', "player": row["player"], "question_key": col,
                    "question": deepcopy(question), "original": value, "kind": "open_text" if role == "open_text" else "objective_selection",
                    "source_row": row["source_row"], "column_index": ord(col)-ord("A")}
            if value != raw:
                unit["raw_selection"] = raw
            units.append(unit)
            sources.append({"response_id": unit["ref"], "source_id": row["player"], "text": raw, "profile": {}, "ids": {}})
        source_questions.append({"question_key": col, "question": question["translation"], "sources": sources})
    markdown = "\n\n".join(f'[{ref}] {v["description"]}\n' + json.dumps(v, ensure_ascii=False) for ref, v in statistics.items())
    packet = {"questions": questions, "source_units": units, "statistics": statistics, "statistics_markdown": markdown,
              "players": [r["player"] for r in original["records"]],
              "research_task": {**deepcopy(original["research_task"]), "sample_metadata": deepcopy(original["source"])},
              "branches": deepcopy(original.get("branches", {})), "source_reference_rule": original["source_reference_rule"]}
    frozen = {"report_engine": ENGINE, "report_mode": "insight", "postwrite_packet": packet,
              "packet_fingerprint": fingerprint(packet), "source_questions": source_questions,
              "source_fingerprint": fingerprint(original), "stats_md": markdown, "stats_blocks": statistics,
              "qualitative_context": deepcopy(original["research_task"])}
    return frozen


def trial_budget(args):
    """Separate approved registry-only limits from the earlier full-run profile."""
    from app.core import config
    registry_only = getattr(args, "registry_only", False)
    limits = (1, 200000, 900) if registry_only else (12, 2000000, 2700)
    values = [getattr(args, name, None) for name in ("max_calls", "token_budget", "seconds")]
    values = [limit if value is None else value for value, limit in zip(values, limits)]
    if any(not 1 <= value <= limit for value, limit in zip(values, limits)):
        raise ValueError("试跑参数超出批准预算")
    if registry_only and config.POSTWRITE_REGISTRY_OUTPUT_TOKENS != 64000:
        raise ValueError("本轮登记输出额度必须为64000")
    return {"max_calls": values[0], "max_tokens": values[1], "max_seconds": values[2],
            "call_seconds": min(values[2], 900 if registry_only else 480), "output_tokens": 24000,
            "registry_output_tokens": config.POSTWRITE_REGISTRY_OUTPUT_TOKENS if registry_only else 24000}


def prepare_full(args):
    from app.core import config
    from app.services.report_postwrite_pipeline import initial_draft_state, estimate_tokens
    from app.services.report_postwrite_audit import messages, registry_payload, REGISTRY_FORMAT
    if config.LLM_POSTWRITE_MODEL != "claude-opus-5-5" or config.LLM_POSTWRITE_REASONING != "medium":
        raise ValueError("本轮仅授权当前Claude medium配置")
    original = json.loads((args.draft_dir / "source-packet.json").read_text(encoding="utf-8"))
    draft = (args.draft_dir / "report-draft.md").read_text(encoding="utf-8")
    frozen = adapt_full_packet(original)
    prompts = {"writer": config.DEFAULT_POSTWRITE_WRITER, "registry": config.DEFAULT_POSTWRITE_REGISTRY, "audit": config.DEFAULT_POSTWRITE_AUDITOR}
    seed = initial_draft_state(frozen, prompts=prompts, draft=draft)
    protected = [args.draft_dir / "source-packet.json", args.draft_dir / "report-draft.md"]
    if args.fixture_root:
        spec = json.loads((args.fixture_root / "trial-spec.json").read_text(encoding="utf-8"))
        protected += [Path(x["path"]) for x in spec.get("protected_inputs", [])]
        protected += [args.fixture_root / "claims.json", args.fixture_root / "run_audit.py", args.fixture_root / "trial-spec.json"]
    hashes = {str(p.resolve()): sha(p) for p in protected}
    request = messages(prompts["registry"], registry_payload(draft, frozen["postwrite_packet"]), REGISTRY_FORMAT)
    registry_only = getattr(args, "registry_only", False)
    comparison = None
    if registry_only:
        if not getattr(args, "baseline_run", None):
            raise ValueError("仅登记复测必须指定上次试跑目录")
        baseline_request = args.baseline_run / "requests" / "01.json"
        baseline_plan = args.baseline_run / "trial-spec.json"
        old = json.loads(baseline_plan.read_text(encoding="utf-8"))
        if request != json.loads(baseline_request.read_text(encoding="utf-8")):
            raise ValueError("登记输入与上次不同，停止复测")
        if old["model"] != config.LLM_POSTWRITE_MODEL or old["reasoning"] != config.LLM_POSTWRITE_REASONING:
            raise ValueError("模型或推理强度与上次不同")
        for path in (args.draft_dir / "source-packet.json", args.draft_dir / "report-draft.md"):
            if sha(path) != old["protected_hashes"].get(str(path.resolve())):
                raise ValueError("初稿或原材料与上次不同")
        hashes.update({str(p.resolve()): sha(p) for p in (baseline_request, baseline_plan)})
        comparison = {"messages_identical": True, "model_identical": True, "reasoning_identical": True,
                      "draft_and_sources_identical": True, "baseline_request_sha256": sha(baseline_request)}
    plan = {"model": config.LLM_POSTWRITE_MODEL, "reasoning": config.LLM_POSTWRITE_REASONING,
            "budget": trial_budget(args), "registry_only": registry_only, "baseline_comparison": comparison,
            "claims_per_batch": config.POSTWRITE_CLAIMS_PER_BATCH, "players_per_batch": config.POSTWRITE_PLAYERS_PER_BATCH,
            "registry_input_estimate": estimate_tokens(request), "protected_hashes": hashes,
            "draft_reused": True, "prior_answers_used": False, "duties": ["来源归属", "原意支持", "人数范围"]}
    for name, value in (("trial-spec.json", plan), ("frozen-inputs.json", frozen), ("initial-state.json", seed),
                        ("reference-map.json", [{k: u[k] for k in ("ref", "player", "question_key", "source_row", "column_index")} for u in frozen["postwrite_packet"]["source_units"]])):
        write_json(args.output_dir / name, value)
    return frozen, prompts, seed, plan


def normalize_wire_usage(raw):
    required = ("input_tokens", "output_tokens")
    if not all(type(raw.get(k)) is int and raw[k] >= 0 for k in required):
        raise ValueError("网关未返回完整用量")
    cached = sum(raw.get(k, 0) or 0 for k in ("cache_creation_input_tokens", "cache_read_input_tokens"))
    return {"input_tokens": raw["input_tokens"] + cached, "output_tokens": raw["output_tokens"],
            "cached_input_tokens": cached, "total_tokens": raw["input_tokens"] + raw["output_tokens"] + cached}


def resume_budget(args, seed):
    """New-run limits and cumulative pipeline limits must not erase past usage."""
    limits = (8, 1566633, 2336)
    values = [getattr(args, name, None) for name in ("max_calls", "token_budget", "seconds")]
    values = [limit if value is None else value for value, limit in zip(values, limits)]
    if any(not 1 <= value <= limit for value, limit in zip(values, limits)):
        raise ValueError("续跑参数超出批准预算")
    calls = seed.get("calls") or []
    if any(c.get("state") != "complete" or not c.get("usage_complete") for c in calls):
        raise ValueError("历史调用或用量不完整，不能继续付费")
    budget = {"max_calls": values[0], "max_tokens": values[1], "max_seconds": values[2],
              "call_seconds": min(480, values[2]), "output_tokens": 64000, "registry_output_tokens": 64000}
    cumulative = {**budget, "max_calls": values[0] + len(calls),
        "max_tokens": values[1] + sum(c.get("charged_or_reserved_tokens", 0) for c in calls),
        "max_seconds": values[2] + seed.get("elapsed_seconds", sum(c.get("elapsed_seconds", 0) for c in calls))}
    return budget, cumulative


def load_meaning_requests(source, seed, protected, visited=None):
    """Resolve inherited and local requests without treating inherited calls as new."""
    from app.core import config
    from app.services.report_postwrite_inputs import fingerprint
    visited = set() if visited is None else visited
    source = Path(source).resolve()
    if source in visited:
        raise ValueError("检查点继承链循环")
    visited.add(source)
    requests_by_stage = {}
    reuse = seed.get("meaning_reuse") or {}
    provenance = reuse.get("provenance") or {}
    if provenance.get("source_run"):
        parent = Path(provenance["source_run"]).resolve()
        for path, expected in provenance.get("protected_hashes", {}).items():
            if sha(path) != expected:
                raise ValueError("继承检查点原文件发生变化")
            protected[str(Path(path).resolve())] = expected
        parent_path = parent / "checkpoint.json"
        protected[str(parent_path)] = sha(parent_path)
        parent_seed = json.loads(parent_path.read_text(encoding="utf-8"))
        if (parent_seed["calls"] != seed["calls"][:len(parent_seed["calls"])]
                or parent_seed["draft"] != seed["draft"] or parent_seed["registry_raw"] != seed["registry_raw"]
                or parent_seed["prompts"] != seed["prompts"]):
            raise ValueError("继承调用账本、初稿、登记或业务规则发生变化")
        inherited = load_meaning_requests(parent, parent_seed, protected, visited)
        certificates = {b["stage"]: b for b in reuse.get("batches", [])}
        if set(certificates) != set(inherited):
            raise ValueError("继承批次复用记录不完整")
        for stage, request in inherited.items():
            raw = parent_seed["responses"][stage]
            cert = certificates[stage]
            if (raw != seed["responses"].get(stage) or fingerprint(request) != cert["request_fingerprint"]
                    or hashlib.sha256(raw.encode("utf-8")).hexdigest() != cert["response_sha256"]):
                raise ValueError("继承原始请求或响应不一致")
        requests_by_stage.update(inherited)
    for name in ("trial-spec.json", "summary.json", "wire-usage.json"):
        protected[str(source / name)] = sha(source / name)
    source_plan = json.loads((source / "trial-spec.json").read_text(encoding="utf-8"))
    source_summary = json.loads((source / "summary.json").read_text(encoding="utf-8"))
    wires = json.loads((source / "wire-usage.json").read_text(encoding="utf-8"))
    if (not wires or len(wires) > len(source_plan["requests"])
            or source_plan.get("model") != config.LLM_POSTWRITE_MODEL
            or source_plan.get("reasoning") != config.LLM_POSTWRITE_REASONING
            or source_summary.get("usage") != seed.get("usage")):
        raise ValueError("旧模型配置、调用记录或累计用量不一致")
    for index, wire in enumerate(wires, 1):
        if (not wire.get("complete") or not wire.get("message_stop") or wire.get("stop_reason") != "end_turn"
                or wire.get("actual_model") != config.LLM_POSTWRITE_MODEL or not wire.get("normalized_usage")):
            raise ValueError("旧原意请求未正常结束，不能复用")
        request_path, response_path = source / "requests" / f"{index:02d}.json", source / "responses" / f"{index:02d}.txt"
        protected.update({str(p): sha(p) for p in (request_path, response_path)})
        request = json.loads(request_path.read_text(encoding="utf-8"))
        planned = source_plan["requests"][index - 1]
        if planned["stage"] != "meaning" or fingerprint(request) != planned["fingerprint"]:
            raise ValueError("旧原意请求指纹与原计划不一致")
        key = "meaning:" + fingerprint(planned["target_ids"])[:16]
        if key in requests_by_stage:
            raise ValueError("继承与新增请求重复")
        if response_path.read_text(encoding="utf-8") != (seed.get("responses") or {}).get(key):
            raise ValueError("原始响应文件与检查点不一致")
        requests_by_stage[key] = request
    if set(requests_by_stage) != set(seed.get("steps") or {}):
        raise ValueError("旧原意批次与实际请求数量不一致")
    return requests_by_stage


def prepare_resume(args):
    from app.core import config
    from app.services.report_postwrite_inputs import validate_frozen, fingerprint
    from app.services.report_postwrite_pipeline import estimate_tokens, reuse_meaning_state, pending_meaning_batches
    from app.services.report_postwrite_audit import (parse_json, validate_registry, count_scope, option_values,
        aggregate_count, messages, meaning_payload, COUNT_FORMAT, count_method, deterministic_count)
    if config.LLM_POSTWRITE_MODEL != "claude-opus-5-5" or config.LLM_POSTWRITE_REASONING != "medium":
        raise ValueError("续跑仅允许当前Claude medium配置")
    source = args.resume_from
    names = ("checkpoint.json", "results.json", "frozen-inputs.json", "saved-snapshot.json",
             "summary.json", "trial-spec.json", "wire-usage.json")
    protected = {str((source / name).resolve()): sha(source / name) for name in names}
    seed = json.loads((source / "checkpoint.json").read_text(encoding="utf-8"))
    frozen = json.loads((source / "frozen-inputs.json").read_text(encoding="utf-8"))
    snapshot = json.loads((source / "saved-snapshot.json").read_text(encoding="utf-8"))
    if (seed != json.loads((source / "results.json").read_text(encoding="utf-8"))
            or seed != snapshot["report_audit"]["checkpoint"] or frozen != snapshot["input_snapshot"]):
        raise ValueError("保存版本与续跑检查点不一致")
    packet = validate_frozen(frozen)
    prompts = {"writer": config.DEFAULT_POSTWRITE_WRITER, "registry": config.DEFAULT_POSTWRITE_REGISTRY,
               "audit": config.DEFAULT_POSTWRITE_AUDITOR}
    requests_by_stage = load_meaning_requests(source, seed, protected)
    old_seed = seed
    seed = reuse_meaning_state(frozen, prompts=prompts, previous=seed, requests_by_stage=requests_by_stage,
                              provenance={"source_run": str(source), "protected_hashes": protected}, allow_partial=True)
    raw = seed["registry_raw"]
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != seed["registry_reuse"]["source_response_sha256"]:
        raise ValueError("登记原始响应指纹不一致")
    registry = validate_registry(parse_json(raw), seed["draft"], packet, raw_registry=raw,
        legacy_note_dispositions=seed["registry"].get("note_dispositions"))
    if registry != seed["registry"] or not registry["complete"]:
        raise ValueError("登记未完成或登记内容发生变化；禁止重新登记")
    budget, cumulative = resume_budget(args, seed)
    requests, objective, subjective = [], [], []
    def planned(stage, request, **details):
        requests.append({"stage": stage, "fingerprint": fingerprint(request),
                         "input_estimate": estimate_tokens(request), **details})
    done = {c["id"] for step in seed["steps"].values() for c in step.get("checks", []) if c.get("complete")}
    for batch in pending_meaning_batches(seed):
        planned("meaning", messages(prompts["audit"], meaning_payload(seed["draft"], packet, batch)),
                target_ids=[c["id"] for c in batch])
    questions = {q["key"]: q for q in packet["questions"]}
    for task in registry["tasks"]:
        players, units = count_scope(task, packet)  # Resolve every mapping before paying for any meaning batch.
        if count_method(task) != "semantic":
            objective.append(deterministic_count(task, players, units, packet))
        else:
            subjective.append((task, players, units))
    population = sorted({p for _, players, _ in subjective for p in players})
    for start in range(0, len(population), config.POSTWRITE_PLAYERS_PER_BATCH):
        group, tasks, sources = population[start:start + config.POSTWRITE_PLAYERS_PER_BATCH], [], {}
        for task, players, units in subjective:
            selected = [p for p in group if p in players]
            if not selected:
                continue
            scoped = [u for u in units if u["player"] in selected]
            tasks.append({"id": task["id"], "task": task, "players": selected, "allowed_refs": [u["ref"] for u in scoped]})
            sources.update({u["ref"]: u for u in scoped})
        if tasks:
            protocol = COUNT_FORMAT + '\n多任务批次：每项只使用该task的allowed_refs。返回 {"tasks":[{"id":"T001","members":[...]}]}，每个任务一次。'
            planned("quantity", messages(prompts["audit"], {"tasks": tasks, "source_units": list(sources.values())}, protocol),
                    players=len(group), player_task_judgments=sum(len(t["players"]) for t in tasks))
    if len(requests) > budget["max_calls"]:
        raise ValueError("待核验批次超过批准调用数，未调用模型")
    estimate = sum(r["input_estimate"] + budget["output_tokens"] for r in requests)
    if estimate > budget["max_tokens"]:
        raise ValueError("剩余批次的保守预估超过批准tokens预算，未调用模型")
    plan = {"resume_only": True, "source_run": str(source), "model": config.LLM_POSTWRITE_MODEL,
        "reasoning": config.LLM_POSTWRITE_REASONING, "budget": budget, "caller_budget": cumulative,
        "source_usage": deepcopy(seed["usage"]), "protected_hashes": protected, "requests": requests,
        "estimated_tokens_with_output_reserve": estimate, "reused_claims": len(done),
        "objective_tasks": len(objective), "subjective_tasks": len(subjective),
        "duties": ["来源归属", "原意支持", "人数范围"]}
    for name, value in (("trial-spec.json", plan), ("frozen-inputs.json", frozen), ("initial-state.json", seed),
                        ("objective-preflight.json", objective), ("reused-checkpoint.json", seed),
                        ("reuse-validation.json", {"reused_claims": len(done), "new_model_calls": 0,
                         "raw_responses_unchanged": seed["responses"] == old_seed["responses"],
                         "registry_unchanged": seed["registry"] == old_seed["registry"],
                         "legacy_notes_preserved": seed["registry"]["legacy_not_checked"] == old_seed["registry"]["legacy_not_checked"],
                         "completed_checks_unchanged": all(
                             c in seed["steps"][key]["checks"] for key, step in old_seed["steps"].items()
                             for c in step.get("checks", []) if c.get("complete")),
                         "batches": seed["meaning_reuse"]["batches"]})):
        write_json(args.output_dir / name, value)
    return frozen, prompts, seed, plan


def validate_resume_request(request, kwargs, plan, index):
    from app.services.report_postwrite_inputs import fingerprint
    expected = plan["requests"]
    if (index >= len(expected) or fingerprint(request) != expected[index]["fingerprint"]
            or kwargs.get("models") != [plan["model"]] or kwargs.get("reasoning_effort") != plan["reasoning"]
            or kwargs.get("max_tokens") != plan["budget"]["output_tokens"] or kwargs.get("max_http_attempts") != 1):
        raise ValueError("续跑只允许已冻结的原意和人数批次；写作、登记或配置变化已阻止")


class FullWireGuard:
    """Trial-only observation; no credentials, headers or vendor error text persisted."""
    def __init__(self, output, budget, protected):
        self.output, self.budget, self.protected = output, budget, protected
        self.records = []
        self.started = time.monotonic()
        self.current = None

    def before(self, request, *, output_tokens=None):
        from app.services.report_postwrite_pipeline import estimate_tokens
        if any(sha(p) != expected for p, expected in self.protected.items()):
            raise ValueError("冻结输入发生变化")
        if len(self.records) >= self.budget["max_calls"] or time.monotonic()-self.started >= self.budget["max_seconds"]:
            raise ValueError("已达本轮实际请求或时间上限")
        if any(not r.get("complete") for r in self.records):
            raise ValueError("上次请求未完整结束，停止调用")
        used = sum(r.get("normalized_usage", {}).get("total_tokens", 0) for r in self.records)
        output_cap = self.budget["output_tokens"] if output_tokens is None else output_tokens
        if output_cap > max(self.budget["output_tokens"], self.budget.get("registry_output_tokens", 0)):
            raise ValueError("请求输出额度超出试跑计划")
        if used + estimate_tokens(request) + output_cap > self.budget["max_tokens"]:
            raise ValueError("下一批将超过本轮tokens预算")
        self.current = {"number": len(self.records)+1, "http_requests": 0, "usage": {}, "message_stop": False,
                        "complete": False, "requested_output_cap": output_cap}
        self.records.append(self.current)
        write_json(self.output / "requests" / f'{self.current["number"]:02d}.json', request)
        self.flush()

    def observe(self, event):
        if event.get("type") == "message_start":
            msg = event.get("message") or {}
            self.current["usage"].update(msg.get("usage") or {})
            self.current["actual_model"] = msg.get("model")
        if event.get("type") == "message_delta":
            self.current["usage"].update(event.get("usage") or {})
            self.current["stop_reason"] = (event.get("delta") or {}).get("stop_reason")
        if event.get("type") == "message_stop":
            self.current["message_stop"] = True

    def flush(self):
        write_json(self.output / "wire-usage.json", self.records)

    @asynccontextmanager
    async def installed(self):
        import httpx
        original = httpx.AsyncClient.stream
        @asynccontextmanager
        async def observed(client, *pos, **kw):
            body = kw.get("json") or {}
            if body.get("max_tokens") != self.current["requested_output_cap"]:
                raise ValueError("实际HTTP请求输出额度与计划不一致")
            self.current["request_parameters"] = {k: body[k] for k in ("model", "max_tokens", "stream", "thinking", "output_config") if k in body}
            self.current["http_requests"] += 1
            if self.current["http_requests"] != 1:
                raise ValueError("自动重试已阻止")
            self.flush()
            async with original(client, *pos, **kw) as resp:
                self.current["http_status"] = resp.status_code
                lines = resp.aiter_lines
                async def read():
                    async for line in lines():
                        if line.startswith("data:"):
                            try:
                                event = json.loads(line[5:].strip())
                            except ValueError:
                                event = {}
                            self.observe(event)
                            delta = event.get("delta") or {}
                            if event.get("type") == "content_block_delta" and isinstance(delta.get("text"), str):
                                with (self.output / "responses" / f'{self.current["number"]:02d}.txt').open("a", encoding="utf-8") as f:
                                    f.write(delta["text"])
                        yield line
                resp.aiter_lines = read
                yield resp
        try:
            httpx.AsyncClient.stream = observed
            yield
        finally:
            httpx.AsyncClient.stream = original
            self.flush()


async def persist_full_result(output, frozen, prompts, result):
    """Replay the completed state through the actual service's save path, with network forbidden."""
    from unittest.mock import patch
    from starlette.requests import Request
    from app.services import survey_service
    from app.services.report_versions import append_report_version, resolve_report_version
    from app.services.report_modes import prepare_report_markdown, report_source_page
    from app.services.report_postwrite_pipeline import initial_draft_state
    from app.storage.sessions import new_session, save_session, get_session
    from app.storage.history import _load_history
    from app.core import config
    sid = new_session()
    session = {"mode": "standard", "analysis_mode": "qualitative", "report_mode": "insight"}
    seed = initial_draft_state(frozen, prompts=prompts, draft=result["draft"])
    base = append_report_version(session, {"report_md": result["draft"], "report_mode": "insight", "input_snapshot": frozen,
        "report_audit": {"status": "partial", "checkpoint": seed}})
    base_before = deepcopy(base)
    save_session(sid, session)
    # The business flow archives the initial report before a later audit resume.
    # Prepare that synthetic archive too; do not rely on creating a second version.
    survey_service.save_to_history(sid, session)
    async def saved_pipeline(*args, **kwargs):
        await kwargs["on_checkpoint"](deepcopy(result))
        return deepcopy(result)
    async def forbidden(*args, **kwargs):
        raise AssertionError("保存展示阶段禁止额外模型调用")
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    with patch.object(config, "REPORT_POSTWRITE_ENABLED", True), patch.object(config, "FEISHU_LOGIN_REQUIRED", False), \
         patch.object(survey_service, "run_postwrite_pipeline", new=saved_pipeline), \
         patch.object(survey_service, "collect_chat_completion", new=forbidden):
        request = Request({"type": "http", "headers": [], "method": "GET", "path": "/"}, receive=receive)
        raw = [event async for event in survey_service.report_stream(sid, request, retry_failed=True, base_version=1)]
    events = [json.loads(x[6:].strip()) for x in raw if x.startswith("data: ")]
    write_json(output / "save-events.json", events)
    saved = get_session(sid)
    snapshot = resolve_report_version(saved, 1)
    checks = {"version_saved": snapshot["version"] == 1,
              "original_report_unchanged": all(snapshot.get(key) == base_before.get(key) for key in
                  ("report_md", "input_snapshot", "created_at", "version", "kind", "base_version", "qa_messages")),
              "version_count_unchanged": len(saved["report_versions"]) == 1 and saved["next_report_version"] == 2,
              "draft_unchanged": snapshot["report_md"] == result["draft"], "audit_matches": snapshot["report_audit"]["status"] == result["audit"]["status"],
              "history_saved": any(x.get("session_id") == sid or x.get("id") == sid for x in _load_history()),
              "source_count": report_source_page(snapshot)["total"], "extra_model_requests": 0}
    for scope in ("body", "evidence"):
        (output / f"export-{scope}.md").write_text(prepare_report_markdown(snapshot, scope), encoding="utf-8")
    write_json(output / "saved-snapshot.json", snapshot)
    write_json(output / "save-validation.json", checks)
    if not all(checks[k] for k in ("version_saved", "original_report_unchanged", "version_count_unchanged", "draft_unchanged", "audit_matches", "history_saved")):
        raise ValueError("保存回放未通过，详见save-validation.json")
    return checks


async def replay_full(args, frozen, prompts, seed, plan):
    from app.core import config
    from app.integrations.llm_client import collect_chat_completion
    from app.services.report_postwrite_pipeline import run_pipeline
    from app.storage.report_postwrite_runs import RunStore
    if not config.LLM_API_KEY:
        raise ValueError("未配置调用凭证，未发起请求")
    marker = args.output_dir / "live-started.json"
    with marker.open("x", encoding="utf-8") as f:
        json.dump({"started_at": datetime.now(timezone.utc).isoformat(), "budget": plan["budget"]}, f)
    guard = FullWireGuard(args.output_dir, plan["budget"], plan["protected_hashes"])
    store = RunStore(args.output_dir / "data" / "report_postwrite_runs")
    inputs = {"frozen": frozen, "prompts": prompts, "trial": plan}
    run_id = store.create(inputs, seed)
    async def checkpoint(state):
        store.save(run_id, {"inputs": inputs, "state": state})
        write_json(args.output_dir / "checkpoint.json", state)
    async def progress(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)
    async def collect(request, **kwargs):
        if plan.get("resume_only"):
            validate_resume_request(request, kwargs, plan, len(guard.records))
        guard.before(request, output_tokens=kwargs["max_tokens"])
        started = time.monotonic()
        callback = kwargs.get("on_attempt_event")
        try:
            async with guard.installed():
                text, model = await collect_chat_completion(request, api_key=config.LLM_API_KEY, **kwargs)
            (args.output_dir / "responses" / f'{guard.current["number"]:02d}.txt').write_text(text, encoding="utf-8")
            usage = normalize_wire_usage(guard.current["usage"])
            guard.current["normalized_usage"] = usage
            if callback:
                await callback({"status": "completed", "usage": usage, "usage_complete": bool(guard.current["message_stop"]),
                                "model": model, "finish_reason": guard.current.get("stop_reason")})
            if not guard.current["message_stop"] or guard.current.get("actual_model") != config.LLM_POSTWRITE_MODEL:
                raise ValueError("模型或用量结束标记不符合本轮配置")
            guard.current["complete"] = True
            return text, model
        finally:
            guard.current["elapsed_seconds"] = round(time.monotonic()-started, 3)
            guard.flush()
    result = await run_pipeline(frozen, prompts=prompts, previous=seed, collect=collect, on_checkpoint=checkpoint,
                                instruction=seed.get("instruction", ""), on_progress=progress,
                                caller_options=plan.get("caller_budget", plan["budget"]),
                                stop_after="registry" if plan.get("registry_only") else None)
    write_json(args.output_dir / "results.json", result)
    summary = {"run_id": run_id, "status": result["audit"]["status"], "stop_reason": result["stop_reason"],
               "usage": result["usage"], "actual_http_requests": sum(r["http_requests"] for r in guard.records),
               "model_window_seconds": round(time.monotonic()-guard.started, 3),
               "registered_claims": result["audit"]["registered_claims"], "checked_claims": result["audit"]["checked_claims"],
               "count_tasks": len(result["audit"]["counts"]), "counts_complete": sum(c.get("complete", False) for c in result["audit"]["counts"]),
               "model_calls_closed": True, "draft_unchanged": result["draft"] == seed["draft"],
               "registry_only": bool(plan.get("registry_only")),
               "registration_complete": bool((result.get("registry") or {}).get("complete")),
               "registration_errors": (result.get("registry") or {}).get("errors", []),
               "whole_report_quality_assessed": False}
    if plan.get("resume_only"):
        usages = [r["normalized_usage"] for r in guard.records if r.get("normalized_usage")]
        summary.update(resume_only=True, source_usage=plan["source_usage"],
            new_usage={"requests": summary["actual_http_requests"],
                       **{key: sum(u.get(key, 0) for u in usages) for key in ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens")},
                       "usage_complete": all(r.get("normalized_usage") and r.get("message_stop") for r in guard.records)},
            registry_unchanged=result["registry"] == seed["registry"],
            registry_raw_unchanged=result["registry_raw"] == seed["registry_raw"],
            legacy_notes_preserved=result["registry"]["legacy_not_checked"] == seed["registry"]["legacy_not_checked"])
    write_json(args.output_dir / "summary.json", summary)
    if plan.get("registry_only"):
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return
    try:
        summary["save_validation"] = await persist_full_result(args.output_dir, frozen, prompts, result)
    except Exception as exc:
        summary["save_error_type"] = type(exc).__name__
    write_json(args.output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def full_main(args):
    if not args.draft_dir:
        raise ValueError("完整试跑必须指定冻结初稿目录")
    trial_budget(args)
    if (args.output_dir / "live-started.json").exists():
        raise ValueError("本轮调用已启动过，不允许重复付费运行")
    frozen, prompts, seed, plan = prepare_full(args)
    print(json.dumps({"prepared": True, "players": len(frozen["postwrite_packet"]["players"]),
                      "source_units": len(frozen["postwrite_packet"]["source_units"]), "registry_input_estimate": plan["registry_input_estimate"],
                      "budget": plan["budget"]}), flush=True)
    try:
        if args.live:
            asyncio.run(replay_full(args, frozen, prompts, seed, plan))
    finally:
        unchanged = all(sha(p) == expected for p, expected in plan["protected_hashes"].items())
        write_json(args.output_dir / "input-integrity.json", {"files": len(plan["protected_hashes"]), "unchanged": unchanged})
        if not unchanged:
            raise RuntimeError("冻结输入发生变化")


def resume_main(args):
    if (args.output_dir / "live-started.json").exists():
        raise ValueError("本轮续跑已启动，不允许自动重试或重复付费运行")
    frozen, prompts, seed, plan = prepare_resume(args)
    print(json.dumps({"prepared": True, "resume_only": True, "batches": len(plan["requests"]),
        "objective_tasks": plan["objective_tasks"], "subjective_tasks": plan["subjective_tasks"],
        "budget": plan["budget"], "estimated_tokens_with_output_reserve": plan["estimated_tokens_with_output_reserve"]}, ensure_ascii=False), flush=True)
    try:
        if args.live:
            asyncio.run(replay_full(args, frozen, prompts, seed, plan))
    finally:
        unchanged = all(sha(p) == expected for p, expected in plan["protected_hashes"].items())
        write_json(args.output_dir / "input-integrity.json", {"files": len(plan["protected_hashes"]), "unchanged": unchanged})
        if not unchanged:
            raise RuntimeError("续跑冻结输入发生变化")


@contextmanager
def offline_count_guard():
    """Fail closed on model/HTTP access; allow only asyncio's local sockets."""
    import socket
    from unittest.mock import patch
    import httpx
    from app.services import report_postwrite_pipeline as pipeline
    attempts = []
    original = socket.socket.connect
    def network(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            attempts.append("external_socket")
            raise AssertionError("离线核验禁止外部网络")
        return original(sock, address)
    def forbidden(*args, **kwargs):
        attempts.append("model_or_http")
        raise AssertionError("离线核验禁止模型或HTTP调用")
    with patch.object(socket.socket, "connect", network), patch.object(httpx.Client, "send", forbidden), \
         patch.object(httpx.AsyncClient, "send", forbidden), patch.object(pipeline.BoundedCaller, "call", forbidden):
        yield attempts


def load_count_replay(source):
    """Verify raw local requests/responses before deriving new count states."""
    from app.services.report_postwrite_inputs import validate_frozen, fingerprint
    from app.services.report_postwrite_pipeline import run_identity
    from app.services.report_postwrite_audit import (parse_json, validate_registry, bind_meanings, meaning_payload,
        count_scope, bind_members)
    names = ("checkpoint.json", "results.json", "frozen-inputs.json", "saved-snapshot.json", "summary.json",
             "trial-spec.json", "wire-usage.json", "initial-state.json")
    protected = {str(source / n): sha(source / n) for n in names}
    read = lambda n: json.loads((source / n).read_text(encoding="utf-8"))
    seed, frozen, snapshot, initial = read("checkpoint.json"), read("frozen-inputs.json"), read("saved-snapshot.json"), read("initial-state.json")
    packet = validate_frozen(frozen)
    if (seed != read("results.json") or seed != snapshot["report_audit"]["checkpoint"]
            or frozen != snapshot["input_snapshot"] or seed["draft"] != snapshot["report_md"]
            or seed["identity"] != run_identity(frozen, seed["prompts"], seed.get("instruction", ""))
            or seed["draft_fingerprint"] != fingerprint(seed["draft"])):
        raise ValueError("离线重算的检查点、来源、规则或保存版本不一致")
    registry = validate_registry(parse_json(seed["registry_raw"]), seed["draft"], packet,
        raw_registry=seed["registry_raw"], legacy_note_dispositions=seed["registry"].get("note_dispositions"))
    if registry != seed["registry"] or not registry["complete"]:
        raise ValueError("登记结果不完整或已变化")
    plan, wires = read("trial-spec.json"), read("wire-usage.json")
    if (read("summary.json")["usage"] != seed["usage"] or initial["calls"] != seed["calls"][:len(initial["calls"])]
            or initial["registry"] != seed["registry"] or initial["draft"] != seed["draft"]
            or len(wires) != len(seed["calls"]) - len(initial["calls"])):
        raise ValueError("历史用量或调用继承不一致")
    for path, expected in plan["protected_hashes"].items():
        if sha(path) != expected:
            raise ValueError("继承检查点原文件发生变化")
        protected[path] = expected
    for key, step in initial["steps"].items():
        if seed["steps"].get(key) != step or seed["responses"].get(key) != initial["responses"].get(key):
            raise ValueError("继承的已完成判断发生变化")
    claims = {c["id"]: c for c in registry["claims"]}
    for key, step in seed["steps"].items():
        if key.startswith("meaning:"):
            raw = seed["responses"][key]
            obj = parse_json(raw)
            ids = {c["id"] for c in obj.get("checks", [])} | set(step.get("missing", []))
            targets = [c for c in registry["claims"] if c["id"] in ids]
            if ids - claims.keys() or {**bind_meanings(obj, meaning_payload(seed["draft"], packet, targets)), "raw": raw} != step:
                raise ValueError("观点原始响应与已保存判断不一致")
    member_keys, stages = set(), set(initial["responses"]) - {"registry"}
    for index, wire in enumerate(wires, 1):
        if (not wire.get("complete") or not wire.get("message_stop") or wire.get("stop_reason") != "end_turn"
                or wire.get("actual_model") != plan["model"] or not wire.get("normalized_usage")):
            raise ValueError("原始请求未正常结束或用量不完整")
        rp, ap = source / "requests" / f"{index:02d}.json", source / "responses" / f"{index:02d}.txt"
        protected.update({str(p): sha(p) for p in (rp, ap)})
        request, raw = json.loads(rp.read_text(encoding="utf-8")), ap.read_text(encoding="utf-8")
        planned = plan["requests"][index - 1]
        if fingerprint(request) != planned["fingerprint"]:
            raise ValueError("原请求与冻结计划不一致")
        payload = json.loads(request[-1]["content"])
        if planned["stage"] == "meaning":
            key = "meaning:" + fingerprint(planned["target_ids"])[:16]
        elif planned["stage"] == "quantity":
            answers = parse_json(raw).get("tasks", [])
            if len({a["id"] for a in answers}) != len(answers) or {a["id"] for a in answers} != {t["id"] for t in payload["tasks"]}:
                raise ValueError("人数原始响应任务缺失或重复")
            answer_map = {a["id"]: a for a in answers}
            keys = []
            for item in payload["tasks"]:
                task = next((t for t in registry["tasks"] if t["id"] == item["id"]), None)
                if task != item["task"]:
                    raise ValueError("人数原请求口径与登记不一致")
                players, units = count_scope(task, packet)
                selected = item["players"]
                units = [u for u in units if u["player"] in selected]
                if (len(selected) != len(set(selected)) or set(selected) - set(players)
                        or item["allowed_refs"] != [u["ref"] for u in units]):
                    raise ValueError("人数原请求题目或群体范围不一致")
                mk = "members:" + task["id"] + ":" + fingerprint(selected)[:12]
                rebound = bind_members(answer_map[task["id"]], task, selected, units)
                if mk in member_keys or rebound["members"] != seed["steps"].get(mk, {}).get("members") or rebound["errors"]:
                    raise ValueError("人数原始响应存在缺项、错位或已保存判断变化")
                member_keys.add(mk); keys.append(mk)
            key = "quantity:" + fingerprint(keys)[:12]
        else:
            raise ValueError("只允许复用原意和人数请求")
        call = seed["calls"][len(initial["calls"]) + index - 1]
        if (call.get("stage") != key or call.get("state") != "complete" or not call.get("usage_complete")
                or call.get("usage") != wire["normalized_usage"] or raw != seed["responses"].get(key)):
            raise ValueError("原始响应与调用账本不一致")
        stages.add(key)
    if (member_keys != {k for k in seed["steps"] if k.startswith("members:")}
            or stages != set(seed["responses"]) - {"registry"}):
        raise ValueError("人数或原意原始记录未完整覆盖")
    return frozen, seed, protected


async def offline_count_replay(args):
    from app.services.report_postwrite_pipeline import run_pipeline
    from app.services.report_postwrite_audit import count_method
    from app.services.report_postwrite_inputs import fingerprint
    frozen, seed, protected = load_count_replay(args.recount_from)
    started = time.monotonic()
    with offline_count_guard() as attempts:
        async def checkpoint(state):
            write_json(args.output_dir / "checkpoint.json", state)
        result = await run_pipeline(frozen, prompts=seed["prompts"], previous=seed,
            instruction=seed.get("instruction", ""), on_checkpoint=checkpoint)
        validation = {"all_meanings_unchanged": result["audit"]["checks"] == seed["audit"]["checks"],
            "raw_responses_unchanged": result["responses"] == seed["responses"],
            "registry_unchanged": result["registry"] == seed["registry"], "draft_unchanged": result["draft"] == seed["draft"],
            "historical_usage_unchanged": result["usage"] == seed["usage"], "model_attempts": attempts,
            "new_model_calls": len(result["calls"]) - len(seed["calls"]), "new_tokens": 0}
        if not all(validation[k] for k in ("all_meanings_unchanged", "raw_responses_unchanged", "registry_unchanged", "draft_unchanged", "historical_usage_unchanged")) or attempts or validation["new_model_calls"]:
            raise ValueError("离线复用不变量校验失败")
        result["count_state_reuse"] = {"source_run": str(args.recount_from), "protected_hashes": protected,
            "task_methods": [{"id": t["id"], "task_fingerprint": fingerprint(t), "method": count_method(t)} for t in seed["registry"]["tasks"]]}
        write_json(args.output_dir / "checkpoint.json", result)
        write_json(args.output_dir / "results.json", result)
        write_json(args.output_dir / "frozen-inputs.json", frozen)
        write_json(args.output_dir / "reuse-validation.json", validation)
        write_json(args.output_dir / "deterministic-task-map.json", result["count_state_reuse"])
        await persist_full_result(args.output_dir, frozen, seed["prompts"], result)
        summary = {"status": result["audit"]["status"], "checked_claims": result["audit"]["checked_claims"],
            **{k: result["audit"][k] for k in ("counts_reviewed", "counts_exact", "counts_ambiguous", "counts_unreviewed", "uncertain_members")},
            "new_model_calls": 0, "new_tokens": 0, "offline_seconds": round(time.monotonic() - started, 3)}
        write_json(args.output_dir / "summary.json", summary)
    unchanged = all(sha(path) == expected for path, expected in protected.items())
    write_json(args.output_dir / "input-integrity.json", {"files": len(protected), "unchanged": unchanged})
    if not unchanged:
        raise ValueError("离线重算改变了原始文件")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def load_registry_reuse(source, dispositions):
    """Read a completed saved response; explicit note review is bound to source files."""
    from app.core import config
    from app.services.report_postwrite_audit import parse_json, registry_payload
    from app.services.report_postwrite_pipeline import reuse_registry_state
    required = ("results.json", "frozen-inputs.json", "requests/01.json", "wire-usage.json", "trial-spec.json")
    if any(dispositions.get("source_files", {}).get(name) != sha(source / name) for name in required):
        raise ValueError("复用文件指纹与备注分类记录不一致")
    previous = json.loads((source / "results.json").read_text(encoding="utf-8"))
    frozen = json.loads((source / "frozen-inputs.json").read_text(encoding="utf-8"))
    request = json.loads((source / "requests/01.json").read_text(encoding="utf-8"))
    wire = json.loads((source / "wire-usage.json").read_text(encoding="utf-8"))
    plan = json.loads((source / "trial-spec.json").read_text(encoding="utf-8"))
    prompts = {"writer": config.DEFAULT_POSTWRITE_WRITER, "registry": config.DEFAULT_POSTWRITE_REGISTRY,
               "audit": config.DEFAULT_POSTWRITE_AUDITOR}
    if plan["model"] != config.LLM_POSTWRITE_MODEL or plan["reasoning"] != config.LLM_POSTWRITE_REASONING:
        raise ValueError("复用模型或推理强度不一致")
    if len(wire) != 1 or not wire[0].get("complete") or not wire[0].get("message_stop") or wire[0].get("stop_reason") != "end_turn":
        raise ValueError("原始登记未正常结束，不能离线视为完整")
    if (json.loads(request[-1]["content"]) != registry_payload(previous["draft"], frozen["postwrite_packet"])
            or not request[0]["content"].startswith(prompts["registry"])):
        raise ValueError("复用材料、初稿或登记业务规则与原请求不一致")
    state = reuse_registry_state(frozen, prompts=prompts, previous=previous,
        legacy_note_dispositions=dispositions, provenance={"source_run": str(source), "source_files": dispositions["source_files"]})
    if state["registry"]["claims"] != previous["registry"]["claims"] or state["registry"]["tasks"] != previous["registry"]["tasks"]:
        raise ValueError("离线归类不得改变原有观点或人数任务")
    if state["registry"]["legacy_not_checked"] != parse_json(previous["registry_raw"]).get("not_checked", []):
        raise ValueError("原始备注未完整保留")
    return frozen, prompts, state


async def offline_registry_reuse(args):
    from unittest.mock import patch
    from app.services.report_postwrite_pipeline import run_pipeline
    from app.storage.report_postwrite_runs import RunStore
    started = time.monotonic()
    source = args.reuse_registry_from
    dispositions = json.loads(args.note_dispositions.read_text(encoding="utf-8"))
    frozen, prompts, seed = load_registry_reuse(source, dispositions)
    attempts = []
    async def forbidden(*args, **kwargs):
        attempts.append("forbidden_model_request")
        raise AssertionError("离线复用禁止调用模型")
    with patch("app.integrations.llm_client.collect_chat_completion", new=forbidden), \
         patch("httpx.AsyncClient.send", new=forbidden), \
         patch("httpx.AsyncClient.stream", side_effect=AssertionError("离线复用禁止HTTP请求")):
        result = await run_pipeline(frozen, prompts=prompts, previous=seed, collect=forbidden, stop_after="registry")
        if attempts or result["calls"] != seed["calls"]:
            raise ValueError("离线复用错误触发了新的登记调用")
        write_json(args.output_dir / "frozen-inputs.json", frozen)
        write_json(args.output_dir / "results.json", result)
        write_json(args.output_dir / "checkpoint.json", result)
        store = RunStore(args.output_dir / "data" / "report_postwrite_runs")
        run_id = store.create({"frozen": frozen, "prompts": prompts, "registry_reuse": result["registry_reuse"]}, result)
        saved = await persist_full_result(args.output_dir, frozen, prompts, result)
        snapshot = json.loads((args.output_dir / "saved-snapshot.json").read_text(encoding="utf-8"))
        saved_checkpoint = snapshot["report_audit"]["checkpoint"]
        if saved_checkpoint != result or store.load(run_id)["state"] != result:
            raise ValueError("离线保存后检查点不一致，不能视为可接续")
        for key in ("verification_notes", "registration_notes", "legacy_not_checked"):
            if snapshot["report_audit"].get(key) != result["audit"][key]:
                raise ValueError("保存版本未完整保留登记备注")
        probe = {"first_stage": None, "model_requests": 0, "checkpoint_reloaded": True}
        async def stop_before_audit(request, **kwargs):
            payload = json.loads(request[-1]["content"])
            probe.update(first_stage="meaning" if "targets" in payload else "unexpected",
                         target_ids=[c["id"] for c in payload.get("targets", [])],
                         model=kwargs["models"][0], reasoning=kwargs.get("reasoning_effort"))
            raise ValueError("离线接续探针在首个原意请求前停止")
        if result["registry"]["complete"]:
            probe_state = await run_pipeline(snapshot["input_snapshot"], prompts=saved_checkpoint["prompts"],
                instruction=saved_checkpoint.get("instruction", ""), previous=saved_checkpoint, collect=stop_before_audit)
            probe["stop_reason"] = probe_state["stop_reason"]
            if probe["first_stage"] != "meaning":
                raise ValueError("复用结果未直接进入原意核验")
        write_json(args.output_dir / "resume-probe.json", probe)
    summary = {"run_id": run_id, "registration_complete": result["registry"]["complete"],
        "status": result["audit"]["status"], "registered_claims": len(result["registry"]["claims"]),
        "count_tasks": len(result["registry"]["tasks"]), "checked_claims": result["audit"]["checked_claims"],
        "verification_notes": len(result["registry"]["verification_notes"]),
        "registration_notes": len(result["registry"]["registration_notes"]),
        "legacy_notes_preserved": len(result["registry"]["legacy_not_checked"]),
        "new_usage": {"requests": 0, "tokens": 0}, "source_usage": result["registry_reuse"]["source_usage"],
        "resume_probe": probe, "save_validation": saved, "offline_seconds": round(time.monotonic()-started, 3)}
    write_json(args.output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def prepare(fixture):
    old = runpy.run_path(str(fixture / "run_audit.py"))
    payload, _, _, _ = old["full_payload"](old["source_unit_batch"]())
    # The fixture adapter only locates historical inputs. The production modules
    # build the actual request and bind the response; no legacy validators run.
    from app.services.report_postwrite_audit import meaning_payload, messages
    from app.core.config import DEFAULT_POSTWRITE_AUDITOR
    for unit in payload["source_units"]:
        unit["kind"] = "objective_selection" if unit.get("kind") == "objective_selection_zh" else "open_text"
        unit["question_key"] = unit.get("column")
    packet = {"source_units": payload["source_units"], "questions": payload["questions"],
              "research_task": payload["research_task"], "statistics": payload["statistics"],
              "statistics_markdown": ""}
    actual = meaning_payload(payload["report"], packet, payload["targets"])
    return actual, messages(DEFAULT_POSTWRITE_AUDITOR, actual)


async def replay(args, payload, request):
    from app.core import config
    from app.services.report_postwrite_audit import bind_meanings, parse_json
    from app.services.report_postwrite_pipeline import BoundedCaller, estimate_tokens
    from app.integrations.llm_client import collect_chat_completion
    import httpx
    if config.LLM_POSTWRITE_MODEL != "claude-opus-5-5":
        raise ValueError("本轮仅授权当前Claude模型")
    if not config.LLM_API_KEY:
        raise ValueError("未配置调用凭证；未发起请求")
    if estimate_tokens(request) + 12000 > 40000:
        raise ValueError("四例输入加输出预留超过40000 tokens；未调用")
    marker = args.output_dir / "live-call-started.json"
    with marker.open("x", encoding="utf-8") as f:
        json.dump({"model": config.LLM_POSTWRITE_MODEL, "requests": 1, "retries": 0,
                   "max_tokens": 40000, "timeout_seconds": 480}, f)
    # Observe only non-sensitive usage fields, including Anthropic cache tokens
    # that the shared client's generic usage callback does not currently expose.
    wire = {"requests": 0, "usage": {}, "stop_reason": None, "message_stop": False}
    original_stream = httpx.AsyncClient.stream
    @asynccontextmanager
    async def observed(client, *pos, **kw):
        wire["requests"] += 1
        if wire["requests"] > 1:
            raise ValueError("额外HTTP请求已阻止")
        async with original_stream(client, *pos, **kw) as resp:
            original_lines = resp.aiter_lines
            async def lines():
                async for line in original_lines():
                    if line.startswith("data:"):
                        try:
                            event = json.loads(line[5:].strip())
                        except ValueError:
                            event = {}
                        if event.get("type") == "message_start":
                            wire["usage"].update((event.get("message") or {}).get("usage") or {})
                            wire["model"] = (event.get("message") or {}).get("model")
                        if event.get("type") == "message_delta":
                            wire["usage"].update(event.get("usage") or {})
                            wire["stop_reason"] = (event.get("delta") or {}).get("stop_reason")
                        if event.get("type") == "message_stop":
                            wire["message_stop"] = True
                    yield line
            resp.aiter_lines = lines
            yield resp
    async def collect(messages, **kwargs):
        return await collect_chat_completion(messages, api_key=config.LLM_API_KEY, **kwargs)
    caller = BoundedCaller(collect=collect, max_calls=1, max_tokens=40000, max_seconds=480,
                          call_seconds=480, output_tokens=12000)
    result = {"validation": None, "failure": None}
    started = time.perf_counter()
    try:
        httpx.AsyncClient.stream = observed
        raw = await caller.call("four-case-meaning", request)
        (args.output_dir / "claude-response.txt").write_text(raw, encoding="utf-8")
        result["validation"] = bind_meanings(parse_json(raw), payload)
    except Exception as exc:
        result["failure"] = type(exc).__name__
    finally:
        httpx.AsyncClient.stream = original_stream
        result.update(usage=caller.summary(), calls=caller.ledger, wire=wire, elapsed_seconds=round(time.perf_counter()-started, 3))
        cached = sum(wire["usage"].get(k, 0) or 0 for k in ("cache_creation_input_tokens", "cache_read_input_tokens"))
        result["usage"]["tokens"] += cached
        result["usage"]["cached_input_tokens"] = cached
        result["within_budget"] = wire["requests"] == 1 and result["usage"]["tokens"] <= 40000 and result["elapsed_seconds"] <= 480
        (args.output_dir / "claude-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("failure", "usage", "within_budget", "elapsed_seconds")}, ensure_ascii=False))


def serve_demo(args):
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=REPO / "static"), name="static")
    source = (REPO / "static/js/features/report.js").read_text(encoding="utf-8")
    renderer = source[source.index("function renderPostwriteAudit("):source.index("function renderReportWorkspace(")]
    audit = {"status": "partial", "checked_claims": 1, "registered_claims": 2, "stop_reason": "合成示例：请求超时",
             "usage": {"requests": 3, "tokens": 420, "usage_complete": True, "model_seconds": 1},
             "checks": [{"id": "C1", "verdict": "issue", "original": "合理解释保留，供人工复核", "reason": "此处只显示提示，不修改正文",
                         "sources": [{"ref": "q1/r2", "meaning": "卡牌有趣", "part": "支持体验", "source": {
                             "player": "P00002", "question": {"translation": "体验评价"}, "original": "卡牌有趣，但说明的字太小。"}}]}], "counts": []}
    report_text = "合理解释保留，供人工复核。"
    if args.serve_results:
        snapshot = json.loads((args.output_dir / "saved-snapshot.json").read_text(encoding="utf-8"))
        audit = {k: v for k, v in snapshot["report_audit"].items() if k != "checkpoint"}
        report_text = snapshot["report_md"]
    @app.get("/", response_class=HTMLResponse)
    def page():
        return '''<!doctype html><html lang="zh"><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css">
<body><main style="max-width:900px;margin:24px auto;padding:16px"><h1>核验面板本地验收</h1><p>本地保存结果；按钮仅用于离线验证，不会调用模型。</p>
<button id="old">切换旧版本</button><button id="new">切换核验版本</button><button id="readonly">只读查看</button>
<section id="report-postwrite-audit" class="card"></section><p id="action"></p><article><h2>报告正文</h2><pre id="body" style="white-space:pre-wrap"></pre></article></main><script>
const audit=''' + json.dumps(audit, ensure_ascii=False).replace("<", "\\u003c") + ''';
document.getElementById('body').textContent=''' + json.dumps(report_text, ensure_ascii=False).replace("<", "\\u003c") + ''';
const ctx={id:'synthetic-history',reportAudit:audit,canEdit:true,versions:[{},{}],maxVersions:5};
const $=id=>document.getElementById(id), activeReportCtx=()=>ctx, activeVersionNumber=()=>2, reportInteractionBusy=()=>false;
const state={viewMode:'history'};function startReport(args){$('action').textContent='合成请求：'+JSON.stringify(args);}
''' + renderer + '''
$('old').onclick=()=>{ctx.reportAudit=null;renderPostwriteAudit();};
$('new').onclick=()=>{ctx.reportAudit=audit;ctx.canEdit=true;renderPostwriteAudit();};
$('readonly').onclick=()=>{ctx.canEdit=false;renderPostwriteAudit();};renderPostwriteAudit();</script></body></html>'''
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--serve-demo", action="store_true")
    parser.add_argument("--serve-results", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--registry-only", action="store_true")
    parser.add_argument("--baseline-run", type=Path)
    parser.add_argument("--reuse-registry-from", type=Path)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--recount-from", type=Path)
    parser.add_argument("--note-dispositions", type=Path)
    parser.add_argument("--draft-dir", type=Path)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--token-budget", type=int)
    parser.add_argument("--seconds", type=int)
    parser.add_argument("--port", type=int, default=8879)
    args = parser.parse_args()
    if args.recount_from and (args.live or args.full or args.registry_only or args.resume_from or args.reuse_registry_from or args.serve_demo or args.serve_results):
        parser.error("离线人数重算不能与模型调用或其他模式组合")
    if args.registry_only and (args.full or args.serve_demo or args.serve_results):
        parser.error("--registry-only不能与全文或页面模式同时使用")
    if args.reuse_registry_from and (args.live or args.full or args.registry_only or args.serve_demo or args.serve_results or not args.note_dispositions):
        parser.error("离线复用须提供备注分类文件，不能与模型调用或其他运行模式组合")
    if args.resume_from and (args.reuse_registry_from or args.full or args.registry_only or args.serve_demo or args.serve_results):
        parser.error("续跑不能与写作、登记、离线迁移或页面模式组合")
    for name in ("output_dir", "env_file", "fixture_root", "draft_dir", "baseline_run", "reuse_registry_from", "note_dispositions", "resume_from", "recount_from"):
        if getattr(args, name):
            setattr(args, name, getattr(args, name).resolve())
    if args.reuse_registry_from and (args.output_dir.is_relative_to(args.reuse_registry_from)
                                    or args.reuse_registry_from.is_relative_to(args.output_dir)):
        parser.error("离线复用产物目录须与原始结果目录分离")
    if args.resume_from and (args.output_dir.is_relative_to(args.resume_from) or args.resume_from.is_relative_to(args.output_dir)):
        parser.error("续跑产物目录须与原检查点目录分离")
    if args.recount_from and (args.output_dir.is_relative_to(args.recount_from) or args.recount_from.is_relative_to(args.output_dir)):
        parser.error("离线人数重算目录须与原结果分离")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    os.environ["DATA_DIR"] = str(args.output_dir / "data")
    os.environ["FEISHU_LOGIN_REQUIRED"] = "false"  # Isolated replay only; no real login/session used.
    os.chdir(args.output_dir)  # sessions also use cwd/data; isolate before imports.
    (args.output_dir / "tmp").mkdir(exist_ok=True)
    os.environ["TEMP"] = os.environ["TMP"] = str(args.output_dir / "tmp")
    for name in ("requests", "responses"):
        (args.output_dir / name).mkdir(exist_ok=True)
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=False)
    if args.recount_from:
        asyncio.run(offline_count_replay(args))
        return
    if args.resume_from:
        resume_main(args)
        return
    if args.reuse_registry_from:
        asyncio.run(offline_registry_reuse(args))
        return
    if args.serve_demo or args.serve_results:
        serve_demo(args)
        return
    if args.full or args.registry_only:
        full_main(args)
        return
    if not args.fixture_root:
        parser.error("--fixture-root is required for replay")
    spec = json.loads((args.fixture_root / "trial-spec.json").read_text(encoding="utf-8"))
    paths = [Path(item["path"]) for item in spec.get("protected_inputs", [])]
    paths += [args.fixture_root / "claims.json", args.fixture_root / "run_audit.py", Path(spec["v8_dir"]) / "report-draft.md", Path(spec["v8_dir"]) / "source-packet.json"]
    before = {str(p): sha(p) for p in paths}
    payload, request = prepare(args.fixture_root)
    from app.services.report_postwrite_pipeline import estimate_tokens
    record = {"payload": payload, "messages": request, "estimated_input_tokens": estimate_tokens(request), "output_cap": 12000, "protected_hashes": before}
    (args.output_dir / "four-case-input.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"cases": [c["id"] for c in payload["targets"]], "source_units": len(payload["source_units"]), "estimated_total_tokens": estimate_tokens(request)+12000}))
    try:
        if args.live:
            asyncio.run(replay(args, payload, request))
    finally:
        after = {str(p): sha(p) for p in paths}
        integrity = {"files": len(before), "unchanged": before == after}
        (args.output_dir / "input-integrity.json").write_text(json.dumps(integrity), encoding="utf-8")
        if before != after:
            raise RuntimeError("冻结输入发生变化")


if __name__ == "__main__":
    main()
