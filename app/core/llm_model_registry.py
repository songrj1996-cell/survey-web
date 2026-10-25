"""Pure business-stage directory and environment defaults; no persistence or calls."""
from dataclasses import dataclass
import hashlib
import json
import math
import re

from app.core import config


def _capacity_result(model, output_tokens, *, reason=None, input_tokens=None,
                     context_tokens=None, warning=None):
    result = {"status": "unknown", "method": "local_estimate_v1", "model": model,
              "output_reserved": output_tokens, "reason": reason,
              "occupancy_percent": None,
              "message": "容量检查：暂时无法估算容量占用",
              "impact": "容量风险尚不明确，按原规则继续处理；本提示不阻断请求。"}
    if reason:
        return result
    required = input_tokens + output_tokens
    percentage = required * 100 / context_tokens
    shown = math.ceil(required * 1000 / context_tokens) / 10
    status = ("over_limit" if required >= context_tokens else
              "near_limit" if percentage > warning else "sufficient")
    label, impact = {
        "sufficient": ("预估余量充足", "预估值仅供参考，是否接受请求以模型服务实际返回为准。"),
        "near_limit": ("接近上限", "可能发生截断或生成失败，按原规则继续处理；本提示不阻断请求。"),
        "over_limit": ("已达到或超出上限", "可能发生截断或生成失败，按原规则继续处理；本提示不阻断请求。"),
    }[status]
    percent_text = "接近100" if status == "near_limit" and shown >= 100 else f"{shown:g}"
    result.pop("reason")
    result.update(status=status, input_estimate=input_tokens, context_tokens=context_tokens,
                  required_estimate=required, occupancy_percent=shown, warning_percent=warning,
                  message=f"容量预估：{percent_text}%（含输出预留） · {label}", impact=impact)
    return result


def estimate_candidate_capacity(messages, *, model, context_tokens, output_tokens):
    """Read the whole text request; advisory only, never billed usage or admission.

    Keep the writer's conservative character weights and structural reserve.
    Unsupported message shapes (including images/tool calls) remain unknown.
    """
    try:
        warning = config.POSTWRITE_WRITER_WARNING_PERCENT
        if context_tokens is None:
            return _capacity_result(model, output_tokens, reason="unknown_model")
        if (type(context_tokens) is not int or context_tokens <= 0
                or type(output_tokens) is not int or output_tokens <= 0
                or type(warning) not in (int, float) or not math.isfinite(warning)
                or not 0 < warning < 100):
            return _capacity_result(model, output_tokens, reason="invalid_configuration")
        if not isinstance(messages, list) or any(
                not isinstance(m, dict) or not isinstance(m.get("content"), str)
                or set(m) - {"role", "content", "name"}
                or ("name" in m and not isinstance(m["name"], str)) for m in messages):
            return _capacity_result(model, output_tokens, reason="unsupported_input")
        text = "\n".join(m["content"] + ("\n" + m["name"] if "name" in m else "") for m in messages)
        estimate = int(sum(1.5 if ord(c) > 127 else 0.4 for c in text)) + 2048
        return _capacity_result(model, output_tokens, input_tokens=estimate,
                                context_tokens=context_tokens, warning=warning)
    except Exception:
        return _capacity_result(model, output_tokens, reason="estimate_failed")


def capacity_estimate_metadata(value):
    """Rebuild safe numeric advice; event text and arbitrary fields are not trusted."""
    if not isinstance(value, dict):
        return {}
    model, output = value.get("model"), value.get("output_reserved")
    if (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", model)
            or type(output) is not int or output <= 0):
        return {}
    if value.get("status") == "unknown":
        reason = value.get("reason")
        if reason not in {"unknown_model", "invalid_configuration", "unsupported_input", "estimate_failed"}:
            reason = "estimate_failed"
        return _capacity_result(model, output, reason=reason)
    tokens, context, warning = (value.get(k) for k in ("input_estimate", "context_tokens", "warning_percent"))
    if (type(tokens) is not int or tokens < 0 or type(context) is not int or context <= 0
            or type(warning) not in (int, float) or not math.isfinite(warning) or not 0 < warning < 100):
        return {}
    return _capacity_result(model, output, input_tokens=tokens, context_tokens=context, warning=warning)


@dataclass(frozen=True)
class TaskSpec:
    key: str
    business: str
    label: str
    prefix: str
    reasoning_key: str | None = None
    tokens_key: str | None = None
    fallback_key: str | None = None


TASKS = (
    TaskSpec("survey.translation", "问卷分析", "问卷文本翻译", "LLM_COLUMN"),
    TaskSpec("survey.columns", "问卷分析", "题型识别", "LLM_COLUMN"),
    TaskSpec("survey.plan", "问卷分析", "方案规划", "LLM_PLANNER"),
    TaskSpec("survey.report", "问卷分析", "标准报告写作", "LLM_REPORT", ""),
    TaskSpec("survey.qa", "问卷分析", "报告追问", "LLM_QA"),
    TaskSpec("quant.plan", "定量与跑数表", "方案规划", "LLM_PLANNER"),
    TaskSpec("quant.report", "定量与跑数表", "报告写作", "LLM_REPORT", ""),
    TaskSpec("quant.qa", "定量与跑数表", "报告追问", "LLM_QA"),
    TaskSpec("insight.writer", "定性与洞察报告", "初稿写作", "LLM_POSTWRITE", tokens_key="POSTWRITE_WRITER_OUTPUT_TOKENS", fallback_key=""),
    TaskSpec("insight.registry", "定性与洞察报告", "报告内容登记", "LLM_POSTWRITE", tokens_key="POSTWRITE_REGISTRY_OUTPUT_TOKENS", fallback_key=""),
    TaskSpec("insight.meaning", "定性与洞察报告", "原意与引用核验", "LLM_POSTWRITE", tokens_key="POSTWRITE_OUTPUT_TOKENS", fallback_key=""),
    TaskSpec("insight.quantity", "定性与洞察报告", "人数范围核验", "LLM_POSTWRITE", tokens_key="POSTWRITE_OUTPUT_TOKENS", fallback_key=""),
    TaskSpec("insight.extract", "定性与洞察报告", "主题提炼", "LLM_THEME_EXTRACT"),
    TaskSpec("insight.merge", "定性与洞察报告", "逐题主题合并", "LLM_THEME_MERGE"),
    TaskSpec("insight.cross", "定性与洞察报告", "跨题观点筛选", "LLM_THEME_MERGE"),
    TaskSpec("insight.classify", "定性与洞察报告", "回答分类", "LLM_CLASSIFY"),
    TaskSpec("quick.summary", "定性与洞察报告", "快速报告总结与合并", "LLM_QUICK_REPORT"),
    TaskSpec("evidence.translation", "定性与洞察报告", "证据中文翻译", "LLM_QUICK_REPORT", tokens_key="REPORT_SOURCE_TRANSLATION_MAX_TOKENS"),
    *(TaskSpec("comment." + key, "评论分析", label, "LLM_COMMENT_" + prefix,
               "LLM_COMMENT_" + ("FAST" if fast else "SYNTHESIS") + "_REASONING",
               "LLM_COMMENT_CLASSIFY_MAX_TOKENS" if key == "classify" else "LLM_COMMENT_MAX_TOKENS")
      for key, label, prefix, fast in (
          ("relevance", "相关性筛选", "RELEVANCE", True), ("extract", "主题提取", "EXTRACT", False),
          ("merge", "主题合并", "MERGE", False), ("classify", "评论分类", "CLASSIFY", True),
          ("report", "简报写作", "REPORT", False), ("quote_batch", "原文初筛", "QUOTE_BATCH", True),
          ("quote_final", "原文精选", "QUOTE_FINAL", False))),
    *(TaskSpec("annotate." + key, "数据标注", label, "LLM_ANNOTATE_" + prefix)
      for key, label, prefix in (("ai", "AI 作答识别", "AI"), ("quality", "逐题质量标注", "QUALITY"), ("translation", "中文翻译", "TRANSLATION"))),
    *(TaskSpec("interview." + key, "访谈报告", label, "INTERVIEW_" + prefix,
               tokens_key="INTERVIEW_EXTRACT_MAX_TOKENS" if key == "extract" else "LLM_REPORT_MAX_TOKENS", fallback_key="INTERVIEW_FALLBACK_MODELS")
      for key, label, prefix in (("extract", "证据归并", "EXTRACT"), ("report", "报告写作", "REPORT"), ("audit", "报告审校", "AUDIT"), ("repair", "问题修复", "REPAIR"))),
    *(TaskSpec("interview_v2." + key, "访谈 V2", label, "INTERVIEW_V2_" + prefix, fallback_key="INTERVIEW_V2_MODEL_FALLBACKS")
      for key, label, prefix in (("attribute", "证据归属", "ATTRIBUTE"), ("dossier", "玩家档案", "DOSSIER"),
                                 ("analysis", "综合分析", "ANALYSIS"), ("report", "报告写作", "REPORT"), ("audit", "报告审校", "REPORT_AUDIT"))),
    TaskSpec("other.announcement", "其他辅助业务", "任务公告规则提取", "LLM_TASK_ANNOUNCEMENT", "", "LLM_TASK_ANNOUNCEMENT_MAX_TOKENS", ""),
)


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def task_catalog() -> list[dict]:
    return [{"key": s.key, "business": s.business, "label": s.label, "default_group": s.prefix} for s in TASKS]


def environment_defaults() -> dict:
    tasks, models = {}, {}
    for s in TASKS:
        model = getattr(config, s.prefix + "_MODEL")
        reasoning = getattr(config, s.reasoning_key if s.reasoning_key is not None else s.prefix + "_REASONING", "") or "default"
        max_tokens = getattr(config, s.tokens_key if s.tokens_key is not None else s.prefix + "_MAX_TOKENS", config.LLM_REPORT_MAX_TOKENS)
        fallbacks = getattr(config, s.fallback_key if s.fallback_key is not None else s.prefix + "_FALLBACK_MODELS", ())
        candidates = [{"model": m, "reasoning": reasoning, "max_tokens": max_tokens} for m in dict.fromkeys([model, *fallbacks]) if m]
        for c in candidates:
            entry = models.setdefault(c["model"], {"id": c["model"], "name": c["model"], "efforts": ["default"], "context_tokens": None, "max_output_tokens": None, "verified": False, "verification_note": "环境配置中已有模型；能力与容量尚未核实"})
            if reasoning not in entry["efforts"]:
                entry["efforts"].append(reasoning)
        primary = {"model": model, "reasoning": reasoning, "max_tokens": max_tokens}
        tasks[s.key] = {"primary": primary, "fallbacks": [c for c in candidates if c["model"] != model], "timeout_seconds": None, "max_http_attempts": 1 if s.prefix == "LLM_POSTWRITE" else None}
    return {"models": list(models.values()), "tasks": tasks}


def check_configuration(configuration: dict, baseline: dict) -> dict:
    """Validate declared abilities. Unchanged legacy values remain displayable."""
    problems, warnings = [], []
    expected = {s.key for s in TASKS}
    if set(configuration["tasks"]) != expected:
        problems.append("业务环节清单与当前版本不一致，请重新加载配置后再编辑")
    models = {m["id"]: m for m in configuration["models"]}
    if len(models) != len(configuration["models"]):
        problems.append("模型目录中有重复标识")
    for m in models.values():
        if not m["name"].strip() or len(set(m["efforts"])) != len(m["efforts"]) or "default" not in m["efforts"]:
            problems.append(f"{m['id']}：名称不能为空，推理选项不能重复且须保留模型默认")
        context, output = m["context_tokens"], m["max_output_tokens"]
        if context is not None and output is not None and output >= context:
            problems.append(f"{m['id']}：最大输出必须小于上下文容量")
        if m["verified"] and (context is None or output is None or not m["verification_note"].strip()):
            problems.append(f"{m['id']}：标记已核实前，请填写容量、输出限制与核实依据")
        if not m["verified"]:
            warnings.append(f"{m['id']}：模型能力尚未核实，环境原配置可保留；新增的模型选择须先核实")
    labels = {s.key: s.label for s in TASKS}
    for key, settings in configuration["tasks"].items():
        label, seen = labels.get(key, key), set()
        old_candidates = [baseline["tasks"][key]["primary"], *baseline["tasks"][key]["fallbacks"]] if key in baseline["tasks"] else []
        for candidate in [settings["primary"], *settings["fallbacks"]]:
            model = candidate["model"]
            legacy = candidate in old_candidates
            if not model and legacy:
                warnings.append(f"{label}：环境中尚未配置主模型")
                continue
            if model not in models:
                problems.append(f"{label}：所选模型不在目录中")
                continue
            if model in seen:
                problems.append(f"{label}：主模型与备用模型不能重复")
            seen.add(model)
            definition = models[model]
            if candidate["reasoning"] not in definition["efforts"]:
                problems.append(f"{label}：{model} 不支持目录中所选的推理程度")
            if definition["max_output_tokens"] is not None and candidate["max_tokens"] > definition["max_output_tokens"]:
                problems.append(f"{label}：输出长度超过 {model} 的目录上限")
            if not legacy and not definition["verified"]:
                problems.append(f"{label}：请先在模型目录核实 {model} 的能力，再修改其调用设置")
        if settings["fallbacks"] and settings["max_http_attempts"] == 1:
            problems.append(f"{label}：请求上限只有 1 次，无法尝试备用模型")
    return {"valid": not problems, "errors": problems, "warnings": warnings}
