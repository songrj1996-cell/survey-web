"""On-demand faithful source translations, isolated from report/version state.

The caller must authenticate and select authoritative frozen source items. This
service never trusts a translation supplied on those items or guesses source IDs.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import re
import unicodedata
import weakref

from app.core import config
from app.integrations.llm_client import collect_chat_completion
from app.storage.prompts import _get_prompt_text
from app.storage.report_source_translations import load_source_translation, save_source_translation


_CONTRACT = """只返回 JSON 对象：
{"schema_version":1,"translations":[{"response_id":"原编号","question_key":"原题号","offset":0,"end_offset":12,"translation_zh":"对应 text 的完整中文译文"}]}。
每条输入都须有一条对应输出。response_id、question_key、offset、end_offset 必须逐字/逐值复制同一输入，不能重排对应关系或创建新编号。
translation_zh 必须为非空普通文本，完整保留内容和段落；不要输出 HTML、代码围栏、摘要或额外说明。"""
_REPAIR = "\n部分条目缺失或格式无效。本次仅重发这些条目，请按同一契约完整返回，严格复制四个定位字段。"
_LOOP_STATES = weakref.WeakKeyDictionary()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _han(char):
    point = ord(char)
    return (0x3400 <= point <= 0x4DBF or 0x4E00 <= point <= 0x9FFF
            or 0xF900 <= point <= 0xFAFF or 0x20000 <= point <= 0x323AF)


def _already_chinese(text):
    # Mixed foreign-language text is not skipped merely because it has Han text.
    return all(not unicodedata.category(char).startswith("L") or _han(char) for char in text)


def _outcome(text="", error=None):
    if error:
        return {"translation_zh": "", "translation_status": "failed", "translation_error": error}
    return {"translation_zh": text, "translation_status": "complete"}


def _fragment_key(item):
    return item["question_key"], item["response_id"], item["offset"], item["end_offset"]


def _messages(fragments, system, repairing=False):
    return [{"role": "system", "content": system + "\n" + _CONTRACT + (_REPAIR if repairing else "")},
            {"role": "user", "content": _json({"sources": fragments})}]


def _fits(fragments, system, settings):
    return sum(len(message["content"]) for message in _messages(fragments, system, True)) <= settings["input_chars"]


def _split_source(item, system, settings):
    text = item["text"]
    if len(text) > settings["input_chars"] * settings["max_batches"]:
        return None
    fragments, offset = [], 0
    while offset < len(text):
        def fragment(end):
            return {"question_key": item["question_key"], "question": item.get("question", ""),
                    "response_id": item["response_id"], "offset": offset, "end_offset": end,
                    "text": text[offset:end]}
        low, high, best = offset + 1, len(text), None
        while low <= high:
            end = (low + high) // 2
            candidate = fragment(end)
            if _fits([candidate], system, settings):
                best, low = candidate, end + 1
            else:
                high = end - 1
        if best is None:
            return None
        end = best["end_offset"]
        if end < len(text):
            # Prefer an existing paragraph/sentence boundary without dropping text.
            boundary = max(text.rfind(mark, offset + (end - offset) // 2, end)
                           for mark in ("\n", "。", ". ", "! ", "? "))
            if boundary >= offset:
                best = fragment(boundary + 1)
        fragments.append(best)
        offset = best["end_offset"]
    return fragments


def _pack(fragments, system, settings):
    batches, current = [], []
    for fragment in fragments:
        if current and not _fits(current + [fragment], system, settings):
            batches.append(current)
            current = []
        current.append(fragment)
    if current:
        batches.append(current)
    return batches


def _parse(answer, fragments):
    expected = {_fragment_key(fragment): fragment for fragment in fragments}
    failed = {key: "missing_translation" for key in expected}
    if isinstance(answer, str):
        answer = answer.strip().removeprefix("\ufeff").strip()
        fence = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", answer, re.DOTALL | re.IGNORECASE)
        if fence:
            answer = fence[1]
    try:
        parsed = json.loads(answer)
    except (TypeError, ValueError):
        return {}, {key: "invalid_structure" for key in expected}
    if (not isinstance(parsed, dict) or type(parsed.get("schema_version")) is not int
            or parsed["schema_version"] != 1 or not isinstance(parsed.get("translations"), list)):
        return {}, {key: "invalid_structure" for key in expected}
    translated, seen = {}, set()
    for item in parsed["translations"]:
        if not isinstance(item, dict):
            continue
        if (not isinstance(item.get("response_id"), str) or not isinstance(item.get("question_key"), str)
                or type(item.get("offset")) is not int or type(item.get("end_offset")) is not int):
            continue
        key = _fragment_key(item)
        if key not in expected:
            # An unknown ID never acquires a quote or another item's translation.
            continue
        if key in seen:
            translated.pop(key, None)
            failed[key] = "invalid_translation"
            continue
        seen.add(key)
        text = item.get("translation_zh")
        if (not isinstance(text, str) or not text.strip() or not any(_han(char) for char in text)
                or re.search(r"<[^>]*>|```", text)):
            failed[key] = "invalid_translation"
            continue
        translated[key] = text
        failed.pop(key, None)
    return translated, failed


async def _translate_batch(fragments, *, system, settings, models, collect, slots, on_result):
    translated, pending, failures = {}, fragments, {}
    for repairing in (False, True):
        if not pending:
            break
        try:
            async with slots:
                async with asyncio.timeout(settings["call_seconds"]):
                    output = await collect(_messages(pending, system, repairing), models=models or None,
                                           max_tokens=settings["max_tokens"],
                                           reasoning_effort=settings["reasoning"],
                                           max_http_attempts=settings["http_attempt_cap"])
        except TimeoutError:
            failures = {_fragment_key(item): "call_timeout" for item in pending}
            break
        except Exception:
            failures = {_fragment_key(item): "model_error" for item in pending}
            break
        answer = output[0] if isinstance(output, tuple) else output
        valid, failures = _parse(answer, pending)
        translated.update(valid)
        await on_result(valid, {})
        pending = [item for item in pending if _fragment_key(item) in failures]
    await on_result({}, failures)
    return translated, failures


async def _run_owned(owned, *, state, system, settings, models, collect):
    """One bounded shared job; each source future resolves independently."""
    outcomes, per_source, tasks = {}, {}, []
    try:
        async with asyncio.timeout(settings["page_seconds"]):
            # Reserve in-flight IDs before reading cache, avoiding stale misses
            # racing with another request that has just saved its translation.
            saved = await asyncio.gather(*(asyncio.to_thread(load_source_translation, key) for key in owned))
            for (fingerprint, (item, _)), cached in zip(owned.items(), saved):
                if cached is not None:
                    outcomes[fingerprint] = _outcome(cached)
                    continue
                fragments = _split_source(item, system, settings)
                if fragments is None:
                    outcomes[fingerprint] = _outcome(error="input_budget_exceeded")
                else:
                    per_source[fingerprint] = fragments
            batches = _pack([fragment for fragments in per_source.values() for fragment in fragments], system, settings)
            if len(batches) > settings["max_batches"]:
                included = {_fragment_key(fragment) for batch in batches[:settings["max_batches"]] for fragment in batch}
                for fingerprint, fragments in list(per_source.items()):
                    if any(_fragment_key(fragment) not in included for fragment in fragments):
                        outcomes[fingerprint] = _outcome(error="input_budget_exceeded")
                        del per_source[fingerprint]
                batches = _pack([fragment for fragments in per_source.values() for fragment in fragments], system, settings)
            translated, failures = {}, {}

            async def accept_partial(valid, errors):
                translated.update(valid)
                failures.update(errors)
                for fingerprint, fragments in per_source.items():
                    if fingerprint in outcomes:
                        continue
                    keys = [_fragment_key(fragment) for fragment in fragments]
                    error = next((failures[key] for key in keys if key in failures), None)
                    if error:
                        outcomes[fingerprint] = _outcome(error=error)
                    elif all(key in translated for key in keys):
                        text = "\n".join(translated[key] for key in keys)
                        outcomes[fingerprint] = _outcome(text)
                        try:
                            await asyncio.to_thread(save_source_translation, fingerprint, text)
                        except OSError:
                            pass  # A cache failure must not erase a valid translation.
            tasks = [asyncio.create_task(_translate_batch(batch, system=system, settings=settings,
                                                        models=models, collect=collect, slots=state["slots"],
                                                        on_result=accept_partial)) for batch in batches]
            await asyncio.gather(*tasks)
    except TimeoutError:
        for fingerprint in owned:
            outcomes.setdefault(fingerprint, _outcome(error="page_timeout"))
    except asyncio.CancelledError:
        for fingerprint in owned:
            outcomes.setdefault(fingerprint, _outcome(error="cancelled"))
        raise
    except Exception:
        for fingerprint in owned:
            outcomes.setdefault(fingerprint, _outcome(error="translation_unavailable"))
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for fingerprint, (_, future) in owned.items():
            if not future.done():
                future.set_result(outcomes.get(fingerprint, _outcome(error="translation_unavailable")))
            if state["pending"].get(fingerprint) is future:
                state["pending"].pop(fingerprint, None)


async def translate_report_source_items(items, *, namespace: str, collect=None) -> list[dict]:
    """Return original-order source/translation pairs, using only verified cache.

    namespace must contain the caller's authenticated owner/report/version scope.
    A caller cancellation leaves a shared job running only until its page deadline.
    """
    if not isinstance(namespace, str) or not namespace.strip():
        raise ValueError("translation namespace is required")
    if not isinstance(items, list) or len(items) > config.REPORT_SOURCE_TRANSLATION_MAX_ITEMS:
        raise ValueError("translation page exceeds source limit")
    originals, identifiers = [], set()
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("response_id"), str) or not item["response_id"]
                or not isinstance(item.get("question_key"), str) or not isinstance(item.get("text"), str)
                or not isinstance(item.get("question", ""), str)):
            raise ValueError("invalid authoritative translation source")
        identifier = (item["question_key"], item["response_id"])
        if identifier in identifiers:
            raise ValueError("translation source identifiers must be unique")
        identifiers.add(identifier)
        originals.append({key: deepcopy(value) for key, value in item.items()
                          if key not in {"translation_zh", "translation_status", "translation_error"}})
    if not originals:
        return []
    system = await asyncio.to_thread(_get_prompt_text, "report_source_translation_system")
    settings = {"input_chars": config.REPORT_SOURCE_TRANSLATION_INPUT_CHARS,
                "max_tokens": config.REPORT_SOURCE_TRANSLATION_MAX_TOKENS,
                "max_batches": config.REPORT_SOURCE_TRANSLATION_MAX_BATCHES,
                "call_seconds": config.REPORT_SOURCE_TRANSLATION_CALL_TIMEOUT_SECONDS,
                "page_seconds": config.REPORT_SOURCE_TRANSLATION_PAGE_TIMEOUT_SECONDS,
                "reasoning": config.LLM_QUICK_REPORT_REASONING,
                "http_attempt_cap": config.LLM_QUICK_REPORT_HTTP_ATTEMPT_CAP}
    models = tuple(dict.fromkeys(model for model in (config.LLM_QUICK_REPORT_MODEL,
                                                     *config.LLM_QUICK_REPORT_FALLBACK_MODELS) if model))
    configuration = _digest({"schema": 1, "system": system, "contract": _CONTRACT, "models": models, "settings": settings})
    fingerprints = [_digest({"namespace": namespace, "question_key": item["question_key"],
                             "response_id": item["response_id"], "text": item["text"],
                             "question": item.get("question", ""), "configuration": configuration}) for item in originals]
    loop = asyncio.get_running_loop()
    state = _LOOP_STATES.setdefault(loop, {"pending": {}, "jobs": set(),
                                        "slots": asyncio.Semaphore(config.REPORT_SOURCE_TRANSLATION_CONCURRENCY)})
    waiting, owned = [], {}
    for item, fingerprint in zip(originals, fingerprints):
        future = loop.create_future()
        if _already_chinese(item["text"]):
            future.set_result(_outcome(item["text"]))
        else:
            future = state["pending"].get(fingerprint)
            if future is None:
                future = loop.create_future()
                state["pending"][fingerprint] = future
                owned[fingerprint] = item, future
        waiting.append(future)
    if owned:
        task = asyncio.create_task(_run_owned(owned, state=state, system=system, settings=settings,
                                               models=models, collect=collect or collect_chat_completion))
        state["jobs"].add(task)
        task.add_done_callback(state["jobs"].discard)
    results = await asyncio.shield(asyncio.gather(*waiting))
    return [{**item, **result} for item, result in zip(originals, results)]
