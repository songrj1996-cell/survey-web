"""Local writer-only capacity advice; never an admission or batching decision."""
import json
import math
import time

from app.core import config
from app.core.llm_context import current_llm_model_snapshot


def unknown_capacity(reason="estimate_failed"):
    return {"status": "unknown", "method": "local_estimate_v1", "reason": reason,
            "occupancy_percent": None,
            "message": "容量检查：暂时无法估算容量占用",
            "impact": "容量风险尚不明确，报告继续生成。"}


def estimate_writer_capacity(request, *, model, output_tokens, estimate_input):
    """Measure the actual unchanged request with the existing budget estimator.

    The percentage includes the full configured output reserve. It is not a
    billed token count or a guarantee that the provider will accept the input.
    No network, source rewriting, request limits or control-flow decisions here.
    """
    started = time.monotonic()
    try:
        limits = json.loads(config.POSTWRITE_WRITER_CONTEXT_LIMITS)
        if not isinstance(limits, dict):
            return unknown_capacity("invalid_configuration")
        snapshot = current_llm_model_snapshot()
        if snapshot and (snapshot["version"] > 0 or snapshot.get("use_saved_defaults")):
            definition = next((m for m in snapshot["configuration"]["models"] if m["id"] == model), {})
            context_tokens = definition.get("context_tokens")
        else:
            context_tokens = limits.get(model)
        if context_tokens is None:
            return unknown_capacity("unknown_model")
        warning = config.POSTWRITE_WRITER_WARNING_PERCENT
        if (type(context_tokens) is not int or context_tokens <= 0
                or type(output_tokens) is not int or output_tokens <= 0
                or type(warning) not in (int, float) or not math.isfinite(warning)
                or not 0 < warning < 100):
            return unknown_capacity("invalid_configuration")
        input_tokens = estimate_input(request)
        if type(input_tokens) is not int or input_tokens < 0:
            return unknown_capacity("estimate_failed")
        required = input_tokens + output_tokens
        percentage = required * 100 / context_tokens
        # Round upward for display; keep the real boundary comparison separate.
        shown = math.ceil(required * 1000 / context_tokens) / 10
        status = ("over_limit" if required >= context_tokens else
                  "near_limit" if percentage > warning else "sufficient")
        percent_text = "接近100" if status == "near_limit" and shown >= 100 else f"{shown:g}"
        label, impact = {
            "sufficient": ("容量充足", "预计可一次完成写作；预估值仅供参考。"),
            "near_limit": ("接近上限", "上下文余量较少，可能出现截断或生成失败；报告继续生成。"),
            "over_limit": ("已达到或超出上限", "可能发生截断或生成失败，报告仍继续生成。"),
        }[status]
        return {"status": status, "method": "local_estimate_v1", "model": model,
                "input_estimate": input_tokens, "output_reserved": output_tokens,
                "context_tokens": context_tokens, "required_estimate": required,
                "occupancy_percent": shown, "warning_percent": warning,
                "message": f"容量预估：{percent_text}%（含输出预留） · {label}",
                "impact": impact,
                "elapsed_seconds": round(time.monotonic() - started, 6)}
    except Exception:
        # Neither a counter/configuration problem nor its private error body
        # may prevent writing or enter the UI/run record.
        return unknown_capacity()
