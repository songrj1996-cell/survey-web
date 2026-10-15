"""Evidence-bearing suggestions only. Never edits an existing task or supplies a payout date."""
import asyncio
import json
import re

from fastapi import HTTPException

from app.core import config
from app.core.llm_context import bind_llm_api_key, bind_llm_attempt_observer
from app.integrations.llm_client import collect_chat_completion
from app.services.feature_access import require_permission
from app.services.llm_credentials import require_background_llm_api_key
from app.services.llm_usage import start_llm_usage_task
from app.storage.prompts import _get_prompt_text


async def extract_announcement(text: str, login):
    require_permission(login, "annotate", write=True)
    if not text.strip():
        raise HTTPException(422, "请先填写公告原文")
    protocol = ('返回一个 JSON 对象，字段为 name、base_reward、excellent_reward、requires_count、min_count、allow_repeat；'
                '每个字段格式为 {"value":值或null,"evidence":"连续原文"}。name 为字符串，金额和次数为整数，'
                'requires_count/allow_repeat 为布尔或null。另外 warnings 是字符串数组。不得返回发奖日期。')
    recorder = start_llm_usage_task(login, category="annotate", action="识别任务公告")
    try:
        with bind_llm_api_key(require_background_llm_api_key(login)), bind_llm_attempt_observer(recorder.on_attempt_event if recorder else None):
            async with asyncio.timeout(min(config.GROUP_RUN_TIMEOUT_SECONDS, 120)):
                output, _ = await collect_chat_completion([
                    {"role": "system", "content": _get_prompt_text("task_announcement_system") + "\n" + protocol},
                    {"role": "user", "content": json.dumps({"announcement": text}, ensure_ascii=False)},
                ], models=(config.LLM_TASK_ANNOUNCEMENT_MODEL,), max_tokens=4000, max_http_attempts=2)
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", output.strip())
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            raise ValueError("invalid announcement result")
        suggestions, uncertain = {}, []
        for name in ("name", "base_reward", "excellent_reward", "requires_count", "min_count", "allow_repeat"):
            item = parsed.get(name)
            value = item.get("value") if isinstance(item, dict) else None
            evidence = item.get("evidence", "") if isinstance(item, dict) else ""
            valid = isinstance(evidence, str) and bool(evidence.strip()) and evidence in text
            if name in ("base_reward", "excellent_reward", "min_count"):
                valid = valid and type(value) is int and (1 if name == "min_count" else 0) <= value <= 2147483647
            elif name in ("requires_count", "allow_repeat"):
                valid = valid and type(value) is bool
            else:
                valid = valid and isinstance(value, str) and 0 < len(value.strip()) <= 200
            if valid:
                suggestions[name] = {"value": value, "evidence": evidence}
            else:
                uncertain.append(name)
        if suggestions.get("requires_count", {}).get("value") is True and "min_count" not in suggestions:
            uncertain.append("requires_count")
        warnings = [str(item)[:500] for item in parsed.get("warnings", []) if isinstance(item, str)] if isinstance(parsed.get("warnings"), list) else []
        return {"suggestions": suggestions, "needs_confirmation": sorted(set(uncertain + ["payout_date"])),
                "warnings": warnings + ["奖励发放日期请由操作者确认，公告截止时间不会自动填入。"]}
    except (ValueError, TimeoutError, RuntimeError):
        if recorder:
            recorder.finish("failed")
        raise HTTPException(502, "公告识别未完成或结果没有可靠证据；已保留当前表单，可重试或手工填写") from None
    finally:
        if recorder:
            recorder.finish("completed")
