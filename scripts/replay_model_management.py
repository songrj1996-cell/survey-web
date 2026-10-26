"""Prepare a bounded model-settings replay; network is reachable only after approval.

No app.main import, production writes, automatic retries, or implicit live mode.
The approved RMB 55 pilot compares the complete writer request twice, not the full audit.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import types
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "model-management-replay/1"


class ReplayStop(ValueError):
    """A local preflight/budget stop; never contains vendor bodies or credentials."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def progress(status, message, **fields):
    print(json.dumps({"timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
                      "status": status, "message": message, **fields}, ensure_ascii=False), flush=True)


def number(value, *, positive=False):
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ReplayStop("金额或费率必须是有效十进制数")
    try:
        result = Decimal(value)
    except Exception as exc:
        raise ReplayStop("金额或费率格式无效") from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ReplayStop("金额或费率超出有效范围")
    return result


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ReplayStop("请求次数或 Token 数无效")
    return value


def money(value):
    return str(number(value).quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def cost_cny(card, model, input_tokens, output_tokens, *, displayed_discount=False):
    """Reserve the higher ordinary/cache-write input quote and the full output."""
    integer(input_tokens)
    integer(output_tokens)
    quote = card.get("models", {}).get(model)
    if not isinstance(quote, dict) or quote.get("currency") != "USD":
        raise ReplayStop("模型缺少已核对的美元费率")
    prefix = "displayed_" if displayed_discount else "list_"
    rates = [number(quote.get(prefix + k + "_per_million"), positive=True) for k in ("input", "output")]
    cache_write = number(quote.get(prefix + "cache_write_per_million"), positive=True)
    rates[0] = max(rates[0], cache_write)
    fx = number(card.get("fx", {}).get("planning_cny_per_usd"), positive=True)
    return (input_tokens * rates[0] + output_tokens * rates[1]) * fx / 1000000


@dataclass
class Budget:
    cap_cny: Decimal
    max_requests: int = 2
    max_seconds: int = 1200
    reserved_cny: Decimal = Decimal("0")
    attempts: int = 0
    uncertain: bool = False
    started: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        self.cap_cny = number(self.cap_cny, positive=True)
        if self.cap_cny > 55:
            raise ReplayStop("本轮总预算不得超过已批准的 55 元")
        integer(self.max_requests, 1)
        integer(self.max_seconds, 1)

    def reserve(self, amount):
        amount = number(amount, positive=True).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        if self.uncertain:
            raise ReplayStop("上次用量未知，停止新增请求；未知消耗不是零")
        if self.attempts >= self.max_requests or time.monotonic() - self.started >= self.max_seconds:
            raise ReplayStop("已达到本轮请求次数或时间上限")
        if self.reserved_cny + amount > self.cap_cny:
            raise ReplayStop("下一次请求的预留金额将超过总预算")
        self.reserved_cny += amount
        self.attempts += 1

    def settle(self, *, usage_complete):
        # Never refund a local estimate: provider billing is reconciled separately.
        self.uncertain |= usage_complete is not True


def privacy_inventory(path, kind):
    path = Path(path).resolve(strict=True)
    raw = path.read_bytes()
    value = json.loads(raw)
    text = raw.decode("utf-8")
    packet = value.get("postwrite_packet", {})
    units = packet.get("source_units", [])
    flags = {
        "email_matches": len(re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)),
        "phone_like_matches": len(re.findall(r"(?<!\d)1[3-9]\d{9}(?!\d)", text)),
        "id_card_like_matches": len(re.findall(r"(?<!\d)\d{17}[\dXx](?!\d)", text)),
    }
    return {"id": kind, "source_path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw), "provenance": "historical_frozen_candidate" if kind == "survey" else "synthetic",
            "privacy_status": "pending_manual_review" if kind == "survey" else "synthetic_only",
            "privacy_flags": flags, "manual_review_required": True,
            "player_ids_are_pseudonyms": bool(packet.get("players")) and all(
                isinstance(p, str) and re.fullmatch(r"P\d{3,}", p) for p in packet.get("players", [])),
            "counts": {"players": len(packet.get("players", [])), "questions": len(packet.get("questions", [])),
                       "sources": len(units), "open_answers": sum(u.get("kind") == "open_text" for u in units),
                       "candidates": value.get("expected_candidate_count")},
            "note": "仅扫描常见标识模式，不能证明姓名、账号、项目名及敏感语义均已脱敏；未复制或上传原文"}


def configure(output, env_file):
    """Set every data location before importing application configuration."""
    from dotenv import load_dotenv
    output = Path(output).resolve()
    if (not output.is_absolute() or output == ROOT or output == Path(output.anchor)
            or "data" in {p.lower() for p in output.parts}
            or (output.is_relative_to(ROOT) and not output.is_relative_to(ROOT / ".test-artifacts"))):
        raise ReplayStop("请使用独立的验证目录，不能写入代码或真实 data 目录")
    os.environ["DATA_DIR"] = str(output / "runtime-data")
    os.environ["LLM_MODEL_SETTINGS_FILE"] = str(output / "runtime-data" / "models.json")
    load_dotenv(Path(env_file).resolve(strict=True), override=False)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    # Existing callers may already have imported config; do not silently rebind it.
    if "app.core.config" in sys.modules:
        from app.core import config
        if Path(config.DATA_DIR).resolve() != output / "runtime-data":
            raise ReplayStop("应用已在其他数据目录初始化，请用独立进程准备回放")
    from app.core import config
    return config


def writer_material(source, config):
    from app.services.report_postwrite_audit import messages
    from app.services.report_postwrite_pipeline import estimate_tokens
    frozen = json.loads(Path(source).read_bytes())
    request = messages(config.DEFAULT_POSTWRITE_WRITER, {**frozen["postwrite_packet"], "instruction": ""}, "")
    # Byte count + overhead is a conservative planning proxy, not provider tokenization.
    bound = sum(len(json.dumps(m, ensure_ascii=False).encode("utf-8")) for m in request) + 4096
    return request, {"messages_sha256": digest(request), "input_local_estimate": estimate_tokens(request),
                     "input_budget_proxy": bound, "proxy_method": "utf8_bytes_plus_4096_not_provider_token_count"}


def approval_blockers(plan, approval, *, now=None):
    problems = []
    if not isinstance(approval, dict) or approval.get("approved") is not True:
        return ["尚未取得样本和费用的明确批准"]
    if approval.get("plan_sha256") != digest(plan):
        problems.append("批准记录与当前计划不一致")
    for key in ("samples_reviewed", "fees_reviewed", "billing_bound_confirmed", "baseline_equivalence_reviewed"):
        if approval.get(key) is not True:
            problems.append("批准前置项未完成：" + key)
    if not plan.get("estimate", {}).get("within_budget"):
        problems.append("完整的两次写作对照不能容纳在预算内")
    try:
        checked = datetime.fromisoformat(plan["rate_card"]["observed_at"])
        age = ((now or datetime.now(timezone.utc)) - checked).total_seconds()
        if age < 0 or age > 86400:
            problems.append("价格证据超过 24 小时或时间无效，需重新核实")
    except (KeyError, ValueError, TypeError):
        problems.append("价格证据时间无效")
    if plan.get("rate_card", {}).get("catalog_verified") is not True:
        problems.append("网关价格尚未核实")
    if any(plan.get("sample", {}).get("privacy_flags", {}).values()):
        problems.append("样本存在待处理的敏感标识，需提供脱敏副本并重新准备")
    return problems


def prepare(args):
    config = configure(args.output_dir, args.env_file)
    from app.core.llm_model_registry import environment_defaults, task_catalog
    output = Path(args.output_dir).resolve()
    if output.exists() and any((output / name).exists() for name in ("plan.json", "sample-manifest.json", "review.md")):
        raise ReplayStop("该目录已有准备结果，请使用新的子目录，保留原审阅记录")
    card = json.loads(Path(args.rate_card).read_bytes())
    origin = lambda url: (urlsplit(url).scheme, urlsplit(url).hostname, urlsplit(url).port)
    if origin(card.get("source_url", "")) != origin(config.LLM_API_BASE):
        raise ReplayStop("费率来源与实际配置的网关不一致")
    sample = privacy_inventory(args.survey_source, "survey")
    request, info = writer_material(sample["source_path"], config)
    settings = environment_defaults()
    writer = settings["tasks"]["insight.writer"]["primary"]
    if writer["model"] not in card.get("models", {}):
        raise ReplayStop("当前写作模型没有对应网关报价，不能借用其他模型价格")
    output_tokens = integer(writer["max_tokens"], 1)
    cap = number(args.budget_cny, positive=True)
    Budget(cap)
    upper = Decimal(money(cost_cny(card, writer["model"], info["input_budget_proxy"], output_tokens)))
    reference = cost_cny(card, writer["model"], info["input_local_estimate"], output_tokens, displayed_discount=True)
    quote = card["models"][writer["model"]]
    candidate = deepcopy(settings)
    for definition in candidate["models"]:
        if definition["id"] == writer["model"]:
            definition.update(context_tokens=integer(quote["context_tokens"], 1),
                              max_output_tokens=integer(quote["max_output_tokens"], 1))
    app_files = ["app/core/config.py", "app/core/llm_context.py", "app/core/llm_model_registry.py",
                 "app/integrations/llm_client.py", "app/services/llm_model_settings.py",
                 "app/services/report_postwrite_audit.py", "app/services/report_postwrite_inputs.py",
                 "app/services/report_postwrite_pipeline.py"]
    baseline_commit = subprocess.check_output(["git", "rev-parse", args.baseline_ref + "^{commit}"], cwd=ROOT, text=True).strip()
    baseline_source = subprocess.check_output(["git", "show", baseline_commit + ":app/integrations/llm_client.py"], cwd=ROOT)
    plan = {"schema": SCHEMA, "prepared_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "status": "awaiting_sample_and_fee_approval", "scope": "writer_pair_only_not_complete_pipeline",
            "candidate_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "baseline_ref": baseline_commit, "baseline_client_sha256": hashlib.sha256(baseline_source).hexdigest(),
            "baseline_scope": "old_transport_with_same_frozen_writer_messages_not_old_full_pipeline",
            "sample": sample, "writer": writer,
            "material": info, "prompt_source": "code_default_frozen_for_both_runs",
            "configuration": candidate, "rate_card": card, "output_dir": str(output),
            "code_sha256": {p: file_sha(ROOT / p) for p in app_files},
            "script_sha256": file_sha(__file__), "gateway_origin_sha256": digest(origin(config.LLM_API_BASE)),
            "budget": {"cap_cny": money(cap), "max_http_requests": 2, "max_seconds": 1200,
                       "per_request_seconds": 480, "cache_discount_assumed": False, "automatic_retry": False},
            "estimate": {"per_request_reserved_cny": money(upper), "total_reserved_cny": money(upper * 2),
                         "displayed_discount_reference_cny": money(reference * 2),
                         "unused_cny": money(max(Decimal(0), cap - upper * 2)), "within_budget": upper * 2 <= cap,
                         "actual_model_calls": 0, "actual_cost_cny": "0.00",
                         "input_reservation_basis": "max_ordinary_and_cache_write_undiscounted_rate",
                         "limitations": "预算按普通输入与缓存写入两者较高的未折扣目录价、完整输出预留及字节代理估算；不是供应商精确计数或最终账单，执行前须确认账户费率、汇率上限及其他计费边界"},
            "not_covered": ["38环节真实调用全覆盖", "完整登记与原意/人数核验", "评论和访谈完整链路", "备用及在途切换真实验证"]}
    inventory = [sample]
    for label, path in [("cross_173", ROOT / "tests/fixtures/report_pipeline/cross_question_173_sanitized.json"),
                        ("quick", ROOT / "tests/fixtures/report_pipeline/quick_mode_cases.json")]:
        inventory.append(privacy_inventory(path, label))
    inventory += [{"id": kind, "source_path": None, "privacy_status": "not_selected", "manual_review_required": True}
                  for kind in ("comments", "interview")]
    output.mkdir(parents=True, exist_ok=True)
    write_new(output / "plan.json", plan)
    write_new(output / "sample-manifest.json", {"schema": SCHEMA, "samples": inventory, "raw_material_copied": False})
    write_new(output / "estimate.json", {"budget": plan["budget"], **plan["estimate"], "plan_sha256": digest(plan)})
    write_new(output / "task-catalog.json", {"tasks": task_catalog(), "live_coverage_this_pilot": ["insight.writer"]})
    text = ("# 模型管理回放准备（未执行）\n\n"
            f"计划指纹：{digest(plan)}\n\n样本：{sample['counts']}；隐私状态：待人工审阅。\n\n"
            f"完整写作两次，模型 {writer['model']}，推理 {writer['reasoning']}，每次输出预留 {output_tokens:,}。\n\n"
            f"按普通输入与缓存写入两者较高费率计算，折后目录价参考 {money(reference * 2)} 元；未折扣预留 {money(upper * 2)} 元，预算 {money(cap)} 元。\n\n"
            "字节代理不等于精确 Token；折扣适用账户、缓存写入/长上下文附加计费、人民币换算边界仍须确认。\n\n"
            "只比较完整写作阶段，不表示登记、核验或全平台验收通过。材料和64K输出不得为省钱裁剪。\n\n"
            "尚未批准样本及费用；真实调用 0 次、费用 0 元。原始材料未复制或上传。\n")
    with (output / "review.md").open("x", encoding="utf-8") as handle:
        handle.write(text)
    return plan


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def verify_frozen(plan, config):
    if file_sha(plan["sample"]["source_path"]) != plan["sample"]["sha256"]:
        raise ReplayStop("样本发生变化，需重新准备和确认")
    if any(file_sha(ROOT / path) != expected for path, expected in plan["code_sha256"].items()):
        raise ReplayStop("应用代码发生变化，不能沿用旧批准")
    if file_sha(__file__) != plan["script_sha256"]:
        raise ReplayStop("回放脚本发生变化，不能沿用旧批准")
    origin = urlsplit(config.LLM_API_BASE)
    if digest((origin.scheme, origin.hostname, origin.port)) != plan["gateway_origin_sha256"]:
        raise ReplayStop("目标网关发生变化")
    from app.core.llm_model_registry import environment_defaults
    if environment_defaults()["tasks"]["insight.writer"]["primary"] != plan["writer"]:
        raise ReplayStop("环境写作参数已变化")
    request, info = writer_material(plan["sample"]["source_path"], config)
    if info != plan["material"]:
        raise ReplayStop("完整请求或估算依据发生变化")
    return request


@asynccontextmanager
async def single_http_guard(expected_model, output_cap, journal):
    """Stop before a second physical send, independently of client observer callbacks."""
    import httpx
    original = httpx.AsyncClient.stream
    @asynccontextmanager
    async def stream(client, method, url, **kwargs):
        payload = kwargs.get("json") or {}
        cap = payload.get("max_tokens", payload.get("max_output_tokens"))
        if journal["http_requests"] >= 1 or payload.get("model") != expected_model or cap != output_cap:
            raise ReplayStop("已阻止额外请求或与批准不符的实际参数")
        journal["http_requests"] += 1
        async with original(client, method, url, **kwargs) as response:
            yield response
    httpx.AsyncClient.stream = stream
    try:
        yield
    finally:
        httpx.AsyncClient.stream = original


async def execute(plan, approval, config):
    problems = approval_blockers(plan, approval)
    if problems:
        raise ReplayStop("；".join(problems))
    request = verify_frozen(plan, config)
    expected_cost = money(cost_cny(plan["rate_card"], plan["writer"]["model"],
                                   plan["material"]["input_budget_proxy"], plan["writer"]["max_tokens"]))
    if expected_cost != plan["estimate"]["per_request_reserved_cny"]:
        raise ReplayStop("请求预留金额与费率或材料不符")
    baseline_source = subprocess.check_output(["git", "show", plan["baseline_ref"] + ":app/integrations/llm_client.py"], cwd=ROOT)
    if hashlib.sha256(baseline_source).hexdigest() != plan["baseline_client_sha256"]:
        raise ReplayStop("基线客户端源码与批准不符")
    output = Path(plan["output_dir"])
    # Exclusive marker forbids an accidental rerun/reset after a crash or unknown spend.
    write_new(output / "live-started.json", {"plan_sha256": digest(plan), "started_at": datetime.now(timezone.utc).isoformat()})
    from app.core.llm_context import bind_llm_model_snapshot
    from app.core.llm_model_registry import fingerprint
    from app.integrations import llm_client
    from app.services.llm_model_settings import call_model
    baseline_name = "app.integrations._approved_replay_baseline"
    baseline = types.ModuleType(baseline_name)
    sys.modules[baseline_name] = baseline
    exec(compile(baseline_source, "<approved-baseline-client>", "exec"), baseline.__dict__)
    budget = Budget(number(plan["budget"]["cap_cny"]), max_requests=2, max_seconds=plan["budget"]["max_seconds"])
    records = []
    writer = plan["writer"]
    snapshot = {"version": 1, "revision": "replay-only", "configuration": plan["configuration"],
                "configuration_sha256": fingerprint(plan["configuration"])}
    for mode in ("baseline", "candidate"):
        verify_frozen(plan, config)
        budget.reserve(plan["estimate"]["per_request_reserved_cny"])
        record = {"mode": mode, "reserved_cny": money(budget.reserved_cny), "http_requests": 0, "events": []}
        records.append(record)
        write_new(output / (mode + "-reserved.json"), record)
        progress("request_prepared", ("基线" if mode == "baseline" else "候选") + "写作已预留预算，即将请求模型",
                 reserved_cny=money(budget.reserved_cny), model=writer["model"])
        def observe(event):
            keys = ("call_id", "status", "model", "protocol", "http_status", "reasoning_effort", "max_output_tokens",
                    "usage", "usage_complete", "finish_reason", "elapsed_seconds", "model_config_version", "error_category")
            record["events"].append({k: deepcopy(event[k]) for k in keys if k in event})
        options = dict(models=[writer["model"]], max_tokens=writer["max_tokens"], max_http_attempts=1,
                       reasoning_effort=None if writer["reasoning"] == "default" else writer["reasoning"],
                       api_key=config.LLM_API_KEY, on_attempt_event=observe)
        started = time.monotonic()
        try:
            async with single_http_guard(writer["model"], writer["max_tokens"], record):
                if mode == "baseline":
                    pending = baseline.collect_chat_completion(request, **options)
                    answer, actual_model = await asyncio.wait_for(pending, timeout=min(480, budget.max_seconds - (time.monotonic() - budget.started)))
                else:
                    with bind_llm_model_snapshot(snapshot):
                        pending = call_model(llm_client.collect_chat_completion, "insight.writer", request, **options)
                        answer, actual_model = await asyncio.wait_for(pending, timeout=min(480, budget.max_seconds - (time.monotonic() - budget.started)))
            terminals = [e for e in record["events"] if e.get("status") in {"completed", "failed"}]
            complete = bool(terminals) and all(e.get("usage_complete") is True for e in terminals)
            if record["http_requests"] != 1:
                raise ReplayStop("实际请求数与单次调用约束不一致")
            budget.settle(usage_complete=complete)
            record.update(status="draft_returned_unreviewed", actual_model=actual_model, usage_complete=complete)
            with (output / (mode + "-draft.md")).open("x", encoding="utf-8") as handle:
                handle.write(answer)
            progress("draft_returned_unreviewed", "写作初稿已返回，来源和语义仍待审阅；不代表完整报告验收通过",
                     usage_complete=complete)
        except BaseException:
            budget.settle(usage_complete=False)
            record["status"] = "stopped"
            progress("stopped", "本次写作未完成，已停止后续调用；请核对隔离目录记录，不要重复启动")
            raise
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)
            write_new(output / (mode + "-result.json"), record)
    return {"status": "drafts_await_manual_review", "records": records, "reserved_cny": money(budget.reserved_cny)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--survey-source", type=Path)
    parser.add_argument("--rate-card", type=Path)
    parser.add_argument("--baseline-ref", default="8f0bd89")
    parser.add_argument("--budget-cny", default="50")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--approval", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.live:
            if not args.approval:
                raise ReplayStop("真实调用尚未批准，请先确认样本与费用；本次未发送请求")
            config = configure(args.output_dir, args.env_file)
            plan = json.loads((args.output_dir / "plan.json").read_bytes())
            if Path(plan["output_dir"]).resolve() != args.output_dir.resolve():
                raise ReplayStop("批准结果目录与当前执行目录不一致")
            result = asyncio.run(execute(plan, json.loads(args.approval.read_bytes()), config))
            write_new(args.output_dir / "live-result.json", result)
        else:
            if not args.survey_source or not args.rate_card:
                parser.error("准备模式需要 --survey-source 和 --rate-card")
            result = prepare(args)
        progress(result["status"], "写作对照已返回，等待人工审阅" if args.live else
                 "回放准备完成，等待确认样本和费用；本次没有模型请求", scope=result.get("scope"),
                 estimate=result.get("estimate"), output_dir=str(args.output_dir))
        return 0
    except ReplayStop as exc:
        progress("blocked", str(exc))
        return 2
    except Exception as exc:
        progress("stopped", "准备或执行未完成，请核对隔离目录内记录；未自动重试", error_type=type(exc).__name__)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
