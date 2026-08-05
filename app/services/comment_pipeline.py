"""services/comment_pipeline:评论舆情分析的编排。

并发调用公司 LLM 分发服务、精选代表性原文引用、拼装最终报告。
纯解析/清洗/抽样逻辑在 comment_analysis 模块,此处只做编排。
"""
import asyncio
import json

import comment_analysis
from app.core.config import (
    COMMENT_ANALYSIS_CONCURRENCY,
    COMMENT_QUOTE_SELECT_CONCURRENCY,
    LLM_COMMENT_CLASSIFY_FALLBACK_MODELS,
    LLM_COMMENT_CLASSIFY_MAX_TOKENS,
    LLM_COMMENT_CLASSIFY_MODEL,
    LLM_COMMENT_EXTRACT_FALLBACK_MODELS,
    LLM_COMMENT_EXTRACT_MODEL,
    LLM_COMMENT_FAST_REASONING,
    LLM_COMMENT_MAX_TOKENS,
    LLM_COMMENT_MERGE_FALLBACK_MODELS,
    LLM_COMMENT_MERGE_MODEL,
    LLM_COMMENT_QUOTE_BATCH_FALLBACK_MODELS,
    LLM_COMMENT_QUOTE_BATCH_MODEL,
    LLM_COMMENT_QUOTE_FINAL_FALLBACK_MODELS,
    LLM_COMMENT_QUOTE_FINAL_MODEL,
    LLM_COMMENT_RELEVANCE_FALLBACK_MODELS,
    LLM_COMMENT_RELEVANCE_MODEL,
    LLM_COMMENT_REPORT_FALLBACK_MODELS,
    LLM_COMMENT_REPORT_MODEL,
    LLM_COMMENT_SYNTHESIS_REASONING,
)
from app.integrations.llm_client import collect_chat_completion
from app.services.glossary_service import (
    normalize_glossary_data,
    normalize_glossary_terms,
    prepare_glossary_messages,
)
from app.storage.prompts import (
    _get_comment_classify_system_prompt,
    _get_comment_extract_system_prompt,
    _get_comment_merge_system_prompt,
    _get_comment_quote_batch_system_prompt,
    _get_comment_quote_final_system_prompt,
    _get_comment_relevance_system_prompt,
    _get_comment_report_system_prompt,
)

_GLOSSARY_PROTECTED_DATA_KEYS = {
    "comment",
    "commentText",
    "comment_text",
    "original",
    "originalText",
    "original_text",
    "quote",
    "quotes",
    "rawText",
    "raw_text",
    "source_refs",
    "text",
}


def _model_chain(primary: str, fallbacks: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(x for x in (primary, *fallbacks) if str(x).strip()))


def _comment_query(**payload) -> str:
    """将原 Workflow 变量协议映射为直连 LLM 的单条 JSON 用户消息。"""
    return json.dumps(payload, ensure_ascii=False)


async def _comment_json_call(
    *,
    task: str,
    system_prompt: str,
    query: str,
    models: tuple[str, ...],
    reasoning_effort: str,
    max_tokens: int,
    validate,
) -> tuple[list, str, bool]:
    """调用结构化评论任务，schema 失败时原模型再生成一次，然后切备选。"""
    errors: list[str] = []
    for model in models:
        for schema_attempt in range(2):
            correction = ""
            if schema_attempt:
                correction = (
                    "\n\n上一次输出未通过结构校验。请重新完整生成，"
                    "只输出符合系统提示词契约的 JSON 数组。"
                )
            try:
                raw, actual_model = await collect_chat_completion(
                    prepare_glossary_messages(
                        [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": query + correction},
                        ]
                    ),
                    models=(model,),
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                )
            except Exception as exc:
                errors.append(f"{model}: {type(exc).__name__}")
                break
            parsed, parse_error = comment_analysis.loads_loose(raw)
            validation_error = validate(parsed)
            if isinstance(parsed, list) and not validation_error:
                return (
                    normalize_glossary_data(
                        parsed,
                        protected_keys=_GLOSSARY_PROTECTED_DATA_KEYS,
                    ),
                    actual_model,
                    bool(schema_attempt),
                )
            errors.append(
                f"{model}: {validation_error or parse_error or '输出不是 JSON 数组'}"
            )
    raise RuntimeError(f"{task} 直连 LLM 输出校验失败；" + "; ".join(errors[-4:]))


async def _comment_text_call(
    *,
    task: str,
    system_prompt: str,
    query: str,
    models: tuple[str, ...],
    reasoning_effort: str,
    max_tokens: int,
    validate,
) -> tuple[str, str, bool]:
    errors: list[str] = []
    for model in models:
        for schema_attempt in range(2):
            correction = ""
            if schema_attempt:
                correction = (
                    "\n\n上一次输出未满足报告结构。请按系统提示词"
                    "重新完整生成 Markdown，不要代码围栏或前言。"
                )
            try:
                raw, actual_model = await collect_chat_completion(
                    prepare_glossary_messages(
                        [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": query + correction},
                        ]
                    ),
                    models=(model,),
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                )
            except Exception as exc:
                errors.append(f"{model}: {type(exc).__name__}")
                break
            cleaned = str(raw or "").lstrip("\ufeff\u200b").strip()
            validation_error = validate(cleaned)
            if not validation_error:
                return (
                    normalize_glossary_terms(cleaned),
                    actual_model,
                    bool(schema_attempt),
                )
            errors.append(f"{model}: {validation_error}")
    raise RuntimeError(f"{task} 直连 LLM 输出校验失败；" + "; ".join(errors[-4:]))


def _comment_selected_raw_comments_md(items: list[dict]) -> str:
    if not items:
        return ""
    lines = [
        "## 玩家评论原文精选",
        "",
        "以下评论来自清洗后字数较长的评论候选池，并经模型筛选为与帖子主题相关、表达较完整的玩家反馈。内容已翻译为简体中文。",
        "",
    ]
    rendered_count = 0
    for idx, item in enumerate(items, start=1):
        translation = str(item.get("translation") or "").strip()
        if not translation:
            continue
        rendered_count += 1
        compact_translation = " ".join(line.strip() for line in translation.splitlines() if line.strip())
        lines.append(f"{idx}. {compact_translation}")
        lines.append("")
    if rendered_count <= 0:
        return ""
    return "\n".join(lines).strip()


def _comment_report_without_raw_comments(md: str) -> str:
    text = (md or "").strip()
    marker = "## 玩家评论原文精选"
    pos = text.find(marker)
    if pos >= 0:
        return text[:pos].strip()
    return text


def _comment_append_selected_raw_comments(report_md: str, selected_raw_comments: list[dict]) -> str:
    selected_md = _comment_selected_raw_comments_md(selected_raw_comments)
    base = _comment_report_without_raw_comments(report_md)
    if not selected_md:
        return base
    return (base + "\n\n" + selected_md).strip()


async def _select_comment_raw_quotes(sess: dict) -> list[dict]:
    """从清洗后的长评论候选中精选最多 50 条原文评论。失败由调用方隔离。"""
    post_title = sess.get("comment_post_title", "") or ""
    post_content = sess.get("comment_post_content", "") or ""
    long_candidates = [
        str(x or "").strip()
        for x in (sess.get("comment_long_candidates") or [])
        if str(x or "").strip()
    ][:comment_analysis.LONG_CANDIDATE_MAX_N]
    if not long_candidates:
        return []

    sem = asyncio.Semaphore(COMMENT_QUOTE_SELECT_CONCURRENCY)
    quote_batch_prompt = _get_comment_quote_batch_system_prompt()
    quote_final_prompt = _get_comment_quote_final_system_prompt()
    quote_batch_models = _model_chain(
        LLM_COMMENT_QUOTE_BATCH_MODEL,
        LLM_COMMENT_QUOTE_BATCH_FALLBACK_MODELS,
    )
    quote_final_models = _model_chain(
        LLM_COMMENT_QUOTE_FINAL_MODEL,
        LLM_COMMENT_QUOTE_FINAL_FALLBACK_MODELS,
    )

    def _first_nonempty(item: dict, keys: list[str]) -> str:
        for key in keys:
            value = item.get(key)
            if value is None:
                continue
            text_value = str(value).strip()
            if text_value:
                return text_value
        return ""

    def _has_cjk(text: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")

    def _normalize_quote_item(item, fallback_idx: int | None = None) -> dict | None:
        if not isinstance(item, dict):
            return None
        try:
            idx = int(item.get("idx", item.get("id", fallback_idx)))
        except (TypeError, ValueError):
            return None
        original_aliases = [
            "text",
            "original",
            "original_text",
            "originalText",
            "raw_text",
            "rawText",
            "comment",
            "comment_text",
            "commentText",
            "原文",
            "原始评论",
            "评论原文",
        ]
        translation_aliases = [
            "translation",
            "zh_translation",
            "translation_zh",
            "chinese_translation",
            "chineseTranslation",
            "translated_text",
            "translatedText",
            "translated_comment",
            "translatedComment",
            "comment_translation",
            "commentTranslation",
            "comment_zh",
            "commentZh",
            "text_zh",
            "textZh",
            "translated",
            "zh",
            "cn",
            "chinese",
            "Chinese translation",
            "Chinese Translation",
            "中文",
            "中文翻译",
            "中文译文",
            "评论翻译",
            "评论译文",
            "翻译",
            "译文",
            "简体中文",
        ]
        text = _first_nonempty(item, original_aliases)
        translation = _first_nonempty(item, translation_aliases)
        explicit_original = _first_nonempty(item, [k for k in original_aliases if k != "text"])
        raw_text = str(item.get("text") or "").strip()
        if not translation and raw_text and explicit_original and _has_cjk(raw_text):
            translation = raw_text
            text = explicit_original
        try:
            score = float(item.get("score", 0) or 0)
        except (TypeError, ValueError):
            score = 0.0
        return {
            "idx": idx,
            "text": text,
            "translation": translation,
            "score": score,
            "reason": str(item.get("reason") or ""),
        }

    async def _quote_batch(batch: list[dict]) -> list[dict]:
        expected = [int(x["idx"]) for x in batch]
        expected_set = set(expected)

        def _validate(parsed) -> str:
            if not isinstance(parsed, list):
                return "输出不是 JSON 数组"
            if len(parsed) > comment_analysis.QUOTE_SELECT_BATCH_KEEP_N:
                return f"候选数 {len(parsed)} 超过上限"
            for item in parsed:
                if not isinstance(item, dict):
                    return "候选项不是对象"
                try:
                    idx = int(item.get("idx"))
                except (TypeError, ValueError):
                    return "候选项缺少有效 idx"
                if idx not in expected_set:
                    return f"idx {idx} 不在当前批次"
                if not str(item.get("translation") or "").strip():
                    return f"idx {idx} 缺少中文翻译"
            return ""

        async with sem:
            parsed, actual_model, repaired = await _comment_json_call(
                task="评论原文初筛",
                system_prompt=quote_batch_prompt,
                query=_comment_query(
                    post_title=post_title,
                    post_content=post_content,
                    comments_json=batch,
                ),
                models=quote_batch_models,
                reasoning_effort=LLM_COMMENT_FAST_REASONING,
                max_tokens=LLM_COMMENT_MAX_TOKENS,
                validate=_validate,
            )
        print(
            f"[comment quote_select_batch] model={actual_model} repaired={repaired}",
            flush=True,
        )
        out: list[dict] = []
        for i, item in enumerate(parsed):
            fallback = expected[i] if i < len(expected) else None
            normalized = _normalize_quote_item(item, fallback)
            if normalized and normalized["idx"] not in expected_set and 0 <= normalized["idx"] < len(expected):
                normalized["idx"] = expected[normalized["idx"]]
            if normalized and normalized["idx"] in expected_set:
                normalized["text"] = long_candidates[normalized["idx"]]
                normalized["translation"] = str(normalized.get("translation") or "").strip()
                out.append(normalized)
        if not out and parsed:
            print(
                f"[comment quote_select_batch] no valid idx parsed; parsed={len(parsed)} expected={len(expected)}",
                flush=True,
            )
        translated_count = sum(1 for x in out if x.get("translation"))
        print(
            f"[comment quote_select_batch] selected={len(out)} translated={translated_count}",
            flush=True,
        )
        if out and translated_count == 0:
            first_raw = parsed[0] if parsed else {}
            first_keys = list(first_raw.keys()) if isinstance(first_raw, dict) else []
            first_preview = json.dumps(first_raw, ensure_ascii=False)[:500] if isinstance(first_raw, dict) else str(first_raw)[:500]
            print(
                f"[comment quote_select_batch] translation missing; first_keys={first_keys}; first_item={first_preview}",
                flush=True,
            )
        return sorted(out, key=lambda x: x.get("score", 0), reverse=True)[:comment_analysis.QUOTE_SELECT_BATCH_KEEP_N]

    def _fallback_selected_from_batch_candidates(
        batch_candidates: list[dict],
        require_translation: bool = False,
    ) -> list[dict]:
        selected: list[dict] = []
        seen_text: set[str] = set()
        for item in sorted(batch_candidates, key=lambda x: x.get("score", 0), reverse=True):
            text = str(item.get("text") or "").strip()
            translation = str(item.get("translation") or "").strip()
            if not text:
                continue
            if require_translation and not translation:
                continue
            key_text = text.lower()
            if key_text in seen_text:
                continue
            seen_text.add(key_text)
            selected.append({
                "text": text,
                "translation": translation,
            })
            if len(selected) >= comment_analysis.QUOTE_SELECT_FINAL_N:
                break
        return selected

    indexed_long = [{"idx": i, "text": text} for i, text in enumerate(long_candidates)]
    quote_batches = comment_analysis.make_batches(indexed_long, comment_analysis.QUOTE_SELECT_BATCH_SIZE)
    batch_candidates: list[dict] = []
    tasks = [asyncio.create_task(_quote_batch(b)) for b in quote_batches]
    try:
        for t in asyncio.as_completed(tasks):
            batch_candidates.extend(await t)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    by_idx: dict[int, dict] = {}
    for item in batch_candidates:
        idx = int(item.get("idx", -1))
        if idx < 0:
            continue
        old = by_idx.get(idx)
        if old is None or float(item.get("score", 0) or 0) > float(old.get("score", 0) or 0):
            by_idx[idx] = item
    batch_candidates = sorted(by_idx.values(), key=lambda x: x.get("score", 0), reverse=True)[
        :comment_analysis.QUOTE_SELECT_FINAL_POOL_N
    ]
    print(
        "[comment quote_select] "
        f"long_candidates={len(long_candidates)} "
        f"batches={len(quote_batches)} "
        f"batch_candidates={len(batch_candidates)} "
        f"batch_translated={sum(1 for x in batch_candidates if x.get('translation'))}",
        flush=True,
    )
    if not batch_candidates:
        return []

    candidates_by_idx = {int(x["idx"]): x for x in batch_candidates if "idx" in x}

    def _validate_final(parsed) -> str:
        if not isinstance(parsed, list):
            return "输出不是 JSON 数组"
        if len(parsed) > comment_analysis.QUOTE_SELECT_FINAL_N:
            return f"精选数 {len(parsed)} 超过上限"
        for item in parsed:
            if not isinstance(item, dict):
                return "精选项不是对象"
            try:
                idx = int(item.get("idx"))
            except (TypeError, ValueError):
                return "精选项缺少有效 idx"
            if idx not in candidates_by_idx:
                return f"idx {idx} 不在候选池"
            if not str(item.get("translation") or "").strip():
                return f"idx {idx} 缺少中文翻译"
        return ""

    try:
        async with sem:
            final_parsed, actual_model, repaired = await _comment_json_call(
                task="评论原文最终精选",
                system_prompt=quote_final_prompt,
                query=_comment_query(
                    post_title=post_title,
                    post_content=post_content,
                    comments_json=batch_candidates,
                ),
                models=quote_final_models,
                reasoning_effort=LLM_COMMENT_SYNTHESIS_REASONING,
                max_tokens=LLM_COMMENT_MAX_TOKENS,
                validate=_validate_final,
            )
        print(
            f"[comment quote_select_final] model={actual_model} repaired={repaired}",
            flush=True,
        )
    except RuntimeError as exc:
        print(f"[comment quote_select_final] {exc}; fallback to batch candidates", flush=True)
        return _fallback_selected_from_batch_candidates(batch_candidates)

    selected: list[dict] = []
    seen_text: set[str] = set()
    for i, item in enumerate(final_parsed):
        fallback = int(batch_candidates[i]["idx"]) if i < len(batch_candidates) and "idx" in batch_candidates[i] else None
        normalized = _normalize_quote_item(item, fallback)
        if not normalized:
            continue
        idx = normalized["idx"]
        if idx not in candidates_by_idx and 0 <= idx < len(batch_candidates):
            idx = int(batch_candidates[idx]["idx"])
        text = str(candidates_by_idx.get(idx, {}).get("text") or normalized["text"] or "").strip()
        if not text:
            continue
        translation = normalized.get("translation") or ""
        if not translation and idx in candidates_by_idx:
            translation = str(candidates_by_idx[idx].get("translation") or "").strip()
        key_text = text.lower()
        if key_text in seen_text:
            continue
        seen_text.add(key_text)
        selected.append({
            "text": text,
            "translation": str(translation or "").strip(),
        })
        if len(selected) >= comment_analysis.QUOTE_SELECT_FINAL_N:
            break
    if not selected:
        print(
            f"[comment quote_select_final] selected empty from parsed={len(final_parsed)}; fallback to batch candidates",
            flush=True,
        )
        return _fallback_selected_from_batch_candidates(batch_candidates)
    any_translated_candidate = any(x.get("translation") for x in batch_candidates)
    selected_translated = sum(1 for x in selected if x.get("translation"))
    print(
        f"[comment quote_select_final] parsed={len(final_parsed)} selected={len(selected)} translated={selected_translated}",
        flush=True,
    )
    if selected_translated == 0 and any_translated_candidate:
        print(
            "[comment quote_select_final] final returned no translations; fallback to translated batch candidates",
            flush=True,
        )
        return _fallback_selected_from_batch_candidates(batch_candidates, require_translation=True)
    if any_translated_candidate and selected_translated < len(selected):
        selected = [x for x in selected if x.get("translation")]
        seen_text = {str(x.get("text") or "").strip().lower() for x in selected}
        for item in sorted(batch_candidates, key=lambda x: x.get("score", 0), reverse=True):
            text = str(item.get("text") or "").strip()
            translation = str(item.get("translation") or "").strip()
            key_text = text.lower()
            if not text or not translation or key_text in seen_text:
                continue
            selected.append({"text": text, "translation": translation})
            seen_text.add(key_text)
            if len(selected) >= comment_analysis.QUOTE_SELECT_FINAL_N:
                break
        print(
            f"[comment quote_select_final] filled translated selected={len(selected)}",
            flush=True,
        )
    return selected


async def _comment_analysis_pipeline(sess: dict):
    """评论舆情分析流水线。异步生成器，yield ("progress", msg) / ("result", payload)。

    流程：relevance(并发) → extract(并发) → merge → classify(并发) → 本地统计 → report。
    七个任务直连公司 LLM 分发服务；批处理任务用 Semaphore 限流。
    """
    post_title = sess.get("comment_post_title", "") or ""
    post_content = sess.get("comment_post_content", "") or ""
    sample_pool: list[str] = list(sess.get("comment_sample", []) or [])
    if not post_title.strip():
        raise RuntimeError("请填写帖子主题后再开始评论分析")
    if not post_content.strip():
        raise RuntimeError("请填写帖子正文后再开始评论分析")
    if not sample_pool:
        raise RuntimeError("没有可分析的评论，请重新上传文件")

    sem = asyncio.Semaphore(COMMENT_ANALYSIS_CONCURRENCY)
    relevance_prompt = _get_comment_relevance_system_prompt()
    extract_prompt = _get_comment_extract_system_prompt()
    merge_prompt = _get_comment_merge_system_prompt()
    classify_prompt = _get_comment_classify_system_prompt()
    report_prompt = _get_comment_report_system_prompt()
    relevance_models = _model_chain(
        LLM_COMMENT_RELEVANCE_MODEL,
        LLM_COMMENT_RELEVANCE_FALLBACK_MODELS,
    )
    extract_models = _model_chain(
        LLM_COMMENT_EXTRACT_MODEL,
        LLM_COMMENT_EXTRACT_FALLBACK_MODELS,
    )
    merge_models = _model_chain(
        LLM_COMMENT_MERGE_MODEL,
        LLM_COMMENT_MERGE_FALLBACK_MODELS,
    )
    classify_models = _model_chain(
        LLM_COMMENT_CLASSIFY_MODEL,
        LLM_COMMENT_CLASSIFY_FALLBACK_MODELS,
    )
    report_models = _model_chain(
        LLM_COMMENT_REPORT_MODEL,
        LLM_COMMENT_REPORT_FALLBACK_MODELS,
    )

    # ── Phase 0：语义相关性筛选。先跑初始 5000；不足目标时从样本池补样 ─────
    sample_pool = sample_pool[:comment_analysis.SAMPLE_POOL_MAX_N]
    related_comments: list[str] = []
    relevance_results: list[dict] = []
    processed_sample_count = 0
    relevance_done = 0
    total_relevance_batches = 0
    relevance_rounds: list[dict] = []

    def _parse_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1", "是", "相关", "direct", "implicit"}
        return bool(value)

    def _normalize_relevance_item(item, fallback_idx: int | None = None) -> dict | None:
        if not isinstance(item, dict):
            return None
        try:
            idx = int(item.get("idx", fallback_idx))
        except (TypeError, ValueError):
            return None
        if idx < 0 or idx >= len(sample_pool):
            return None
        relation = str(item.get("relation") or item.get("relevance") or "").strip().lower()
        if "is_related" in item:
            is_related = _parse_bool(item.get("is_related"))
        else:
            is_related = relation in {"direct", "implicit", "related", "相关", "直接相关", "隐含相关"}
        if relation in {"off_topic", "irrelevant", "unrelated", "not_related", "无关"}:
            is_related = False
        return {
            "idx": idx,
            "is_related": is_related,
            "relation": relation or ("direct" if is_related else "off_topic"),
            "reason": str(item.get("reason") or ""),
        }

    async def _judge_relevance(batch: list[dict], depth: int = 0):
        expected_idxs = [int(x["idx"]) for x in batch]
        expected_set = set(expected_idxs)

        def _validate(parsed) -> str:
            if not isinstance(parsed, list):
                return "输出不是 JSON 数组"
            seen: set[int] = set()
            for item in parsed:
                normalized = _normalize_relevance_item(item)
                if not normalized:
                    return "存在缺少有效 idx 的筛选结果"
                idx = normalized["idx"]
                if idx not in expected_set:
                    return f"idx {idx} 不在当前批次"
                if idx in seen:
                    return f"idx {idx} 重复"
                seen.add(idx)
            return ""

        try:
            async with sem:
                arr, actual_model, repaired = await _comment_json_call(
                    task="评论相关性筛选",
                    system_prompt=relevance_prompt,
                    query=_comment_query(
                        post_title=post_title,
                        post_content=post_content,
                        comments_json=batch,
                    ),
                    models=relevance_models,
                    reasoning_effort=LLM_COMMENT_FAST_REASONING,
                    max_tokens=LLM_COMMENT_MAX_TOKENS,
                    validate=_validate,
                )
            print(
                f"[comment relevance] model={actual_model} repaired={repaired}",
                flush=True,
            )
        except RuntimeError:
            if len(batch) > 10 and depth < 3:
                mid = len(batch) // 2
                print(
                    f"[comment relevance] 批次校验失败，自动拆分为 {mid}+{len(batch) - mid} 条",
                    flush=True,
                )
                left = await _judge_relevance(batch[:mid], depth + 1)
                right = await _judge_relevance(batch[mid:], depth + 1)
                return left + right
            raise

        by_idx: dict[int, dict] = {}
        for item in arr:
            normalized = _normalize_relevance_item(item)
            if normalized:
                by_idx[normalized["idx"]] = normalized
        # 筛选任务只返回需保留评论；未返回 idx 按无关处理。
        return [
            by_idx.get(idx) or {
                "idx": idx,
                "is_related": False,
                "relation": "off_topic",
                "reason": "相关性筛选未返回该评论，按无关处理",
            }
            for idx in expected_idxs
        ]

    async def _run_relevance_slice(start_idx: int, end_idx: int, round_results: list[dict]):
        nonlocal processed_sample_count, relevance_done, total_relevance_batches
        indexed = [{"idx": i, "text": sample_pool[i]} for i in range(start_idx, end_idx)]
        batches = comment_analysis.make_batches(indexed, comment_analysis.RELEVANCE_BATCH_SIZE)
        if not batches:
            return
        total_relevance_batches += len(batches)
        yield ("progress", f"正在按帖子主题筛选相关评论（样本 {start_idx + 1}-{end_idx}，共 {len(batches)} 批）…")
        tasks = [asyncio.create_task(_judge_relevance(b)) for b in batches]
        try:
            for t in asyncio.as_completed(tasks):
                round_results.extend(await t)
                relevance_done += 1
                yield ("progress", f"相关性筛选进度 {relevance_done}/{total_relevance_batches} 批")
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        processed_sample_count = end_idx

    next_start = 0
    while next_start < len(sample_pool):
        next_end = min(next_start + comment_analysis.SAMPLE_MAX_N, len(sample_pool))
        before = len(related_comments)
        round_results: list[dict] = []
        async for item in _run_relevance_slice(next_start, next_end, round_results):
            yield item
        relevance_results.extend(round_results)
        relevance_by_idx = {int(x["idx"]): x for x in round_results if isinstance(x, dict)}
        related_comments.extend(
            sample_pool[i]
            for i in range(next_start, next_end)
            if relevance_by_idx.get(i, {}).get("is_related") is True
        )
        relevance_rounds.append({
            "start": next_start,
            "end": next_end,
            "sample_count": next_end - next_start,
            "related_count": len(related_comments) - before,
        })
        yield (
            "progress",
            f"本轮相关性筛选完成：新增相关评论 {len(related_comments) - before} 条，累计 {len(related_comments)} 条",
        )
        if len(related_comments) >= comment_analysis.RELATED_TARGET_N:
            break
        if next_end >= len(sample_pool):
            break
        yield (
            "progress",
            f"相关评论不足 {comment_analysis.RELATED_TARGET_N} 条，继续补抽下一批样本…",
        )
        next_start = next_end

    comments = related_comments
    related_count = len(comments)
    original_sample_count = processed_sample_count
    off_topic_count = original_sample_count - related_count
    relevance_stats = {
        "sample_count": original_sample_count,
        "sample_pool_count": len(sample_pool),
        "related_count": related_count,
        "off_topic_count": off_topic_count,
        "related_target": comment_analysis.RELATED_TARGET_N,
        "sample_cap": comment_analysis.SAMPLE_POOL_MAX_N,
        "rounds": relevance_rounds,
    }
    sess["comment_relevance_stats"] = relevance_stats
    meta = sess.get("comment_sample_meta") or {}
    meta.update({
        "topic_related_count": related_count,
        "off_topic_count": off_topic_count,
        "relevance_sample_count": original_sample_count,
        "related_target": comment_analysis.RELATED_TARGET_N,
    })
    sess["comment_sample_meta"] = meta
    yield ("progress", f"主题相关性筛选完成：使用样本 {original_sample_count} 条，保留 {related_count} 条，剔除 {off_topic_count} 条无关评论")
    if related_count <= 0:
        raise RuntimeError("未筛选出与帖子主题或正文相关的评论，无法生成可靠报告")

    extract_batches = comment_analysis.make_batches(comments, comment_analysis.BATCH_SIZE)
    n_extract_batch = len(extract_batches)

    # ── Phase 1：并发提取各批主题候选 ───────────────────────────────────
    yield ("progress", f"正在并发提取评论主题（共 {n_extract_batch} 批，并发 {COMMENT_ANALYSIS_CONCURRENCY}）…")
    extract_done = 0

    async def _extract(batch: list[str]):
        def _validate(parsed) -> str:
            if not isinstance(parsed, list) or not parsed:
                return "未返回候选主题数组"
            for item in parsed:
                if not isinstance(item, dict):
                    return "候选主题不是对象"
                if not str(item.get("theme_name") or "").strip():
                    return "候选主题缺少 theme_name"
                if not str(item.get("description") or "").strip():
                    return "候选主题缺少 description"
                if item.get("sentiment") not in {"positive", "negative", "neutral"}:
                    return "候选主题 sentiment 非法"
            return ""

        async with sem:
            parsed, actual_model, repaired = await _comment_json_call(
                task="评论主题提取",
                system_prompt=extract_prompt,
                query=_comment_query(
                    post_title=post_title,
                    post_content=post_content,
                    comments_json=batch,
                ),
                models=extract_models,
                reasoning_effort=LLM_COMMENT_SYNTHESIS_REASONING,
                max_tokens=LLM_COMMENT_MAX_TOKENS,
                validate=_validate,
            )
        print(
            f"[comment extract] model={actual_model} repaired={repaired}",
            flush=True,
        )
        return parsed

    extract_tasks = [asyncio.create_task(_extract(b)) for b in extract_batches]
    candidates: list[dict] = []
    for t in asyncio.as_completed(extract_tasks):
        part = await t
        candidates.extend(x for x in part if isinstance(x, dict))
        extract_done += 1
        yield ("progress", f"主题提取进度 {extract_done}/{n_extract_batch} 批")

    if not candidates:
        raise RuntimeError("主题提取未返回任何有效结果")

    # ── Phase 2：合并去重主题（单次） ───────────────────────────────────
    yield ("progress", "正在汇总并合并去重主题…")
    def _validate_merged(parsed) -> str:
        if not isinstance(parsed, list) or not parsed:
            return "未返回最终主题数组"
        seen: set[str] = set()
        for item in parsed:
            if not isinstance(item, dict):
                return "最终主题不是对象"
            tid = str(item.get("theme_id") or "").strip()
            name = str(item.get("theme_name") or "").strip()
            if not tid or not name or not str(item.get("description") or "").strip():
                return "最终主题缺少 theme_id/theme_name/description"
            if tid in seen:
                return f"theme_id {tid} 重复"
            seen.add(tid)
            if item.get("sentiment") not in {"positive", "negative", "neutral"}:
                return f"theme_id {tid} 的 sentiment 非法"
        return ""

    merged, merge_model, merge_repaired = await _comment_json_call(
        task="评论主题合并",
        system_prompt=merge_prompt,
        query=_comment_query(
            post_title=post_title,
            post_content=post_content,
            themes_json=candidates,
        ),
        models=merge_models,
        reasoning_effort=LLM_COMMENT_SYNTHESIS_REASONING,
        max_tokens=LLM_COMMENT_MAX_TOKENS,
        validate=_validate_merged,
    )
    print(
        f"[comment merge] model={merge_model} repaired={merge_repaired}",
        flush=True,
    )

    # LLM 契约用 theme_id / theme_name，内部统计用 id / name，转一层。
    final_themes: list[dict] = []
    for t in merged:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("theme_id") or "").strip()
        name = str(t.get("theme_name") or "").strip()
        if not tid or not name:
            continue
        final_themes.append({
            "id": tid,
            "name": name,
            "description": str(t.get("description") or ""),
            "sentiment": str(t.get("sentiment") or "neutral"),
        })
    if not final_themes:
        raise RuntimeError("主题合并结果缺少 theme_id / theme_name 字段")

    # 给 classify 用的主题列表（id + name + description）
    themes_for_classify = [
        {"theme_id": t["id"], "theme_name": t["name"], "description": t["description"]}
        for t in final_themes
    ]

    # ── Phase 3：并发分类各批评论 ───────────────────────────────────────
    indexed_comments = [{"idx": i, "text": text} for i, text in enumerate(comments)]
    classify_batches = comment_analysis.make_batches(indexed_comments, comment_analysis.CLASSIFY_BATCH_SIZE)
    n_classify_batch = len(classify_batches)
    yield ("progress", f"正在并发分类评论观点（共 {n_classify_batch} 批，每批 {comment_analysis.CLASSIFY_BATCH_SIZE} 条）…")
    classify_done = 0

    def _normalize_classify_item(item, fallback_idx: int | None = None) -> dict | None:
        if not isinstance(item, dict):
            return None
        try:
            idx = int(item.get("idx", fallback_idx))
        except (TypeError, ValueError):
            return None
        if idx < 0 or idx >= len(comments):
            return None
        tids = item.get("theme_ids")
        if not isinstance(tids, list) or not tids:
            single = item.get("theme_id")
            tids = [single] if single else ["other"]
        tids = [str(x) for x in tids if x]
        return {
            "idx": idx,
            "theme_ids": tids or ["other"],
            "sentiment": str(item.get("sentiment") or "neutral"),
            "is_quote_candidate": bool(item.get("is_quote_candidate")),
            "translation": str(item.get("translation") or ""),
            "original": comments[idx],
        }

    async def _classify(batch: list[dict], depth: int = 0):
        # classify 输入带 idx，输出也必须带回 idx；本地按 idx 回填。
        # 如果 LLM 返回部分结果，缺失 idx 会拆成更小批次补跑。
        expected_idxs = [int(x["idx"]) for x in batch]
        expected_set = set(expected_idxs)
        valid_theme_ids = {t["id"] for t in final_themes}

        def _validate(parsed) -> str:
            if not isinstance(parsed, list):
                return "输出不是 JSON 数组"
            by_idx: dict[int, dict] = {}
            for item in parsed:
                normalized = _normalize_classify_item(item)
                if not normalized:
                    return "存在缺少有效 idx 的分类结果"
                idx = normalized["idx"]
                if idx not in expected_set:
                    return f"idx {idx} 不在当前批次"
                if idx in by_idx:
                    return f"idx {idx} 重复"
                tids = normalized["theme_ids"]
                if any(tid != "other" and tid not in valid_theme_ids for tid in tids):
                    return f"idx {idx} 使用了未知 theme_id"
                if "other" in tids and len(tids) > 1:
                    return f"idx {idx} 的 other 不能与其他主题并存"
                if normalized["sentiment"] not in {
                    "positive", "negative", "neutral", "mixed"
                }:
                    return f"idx {idx} 的 sentiment 非法"
                by_idx[idx] = normalized
            missing = expected_set - set(by_idx)
            if missing:
                return f"缺少 {len(missing)} 条 idx"
            return ""

        try:
            async with sem:
                arr, actual_model, repaired = await _comment_json_call(
                    task="评论分类",
                    system_prompt=classify_prompt,
                    query=_comment_query(
                        post_title=post_title,
                        post_content=post_content,
                        comments_json=batch,
                        themes_json=themes_for_classify,
                    ),
                    models=classify_models,
                    reasoning_effort=LLM_COMMENT_FAST_REASONING,
                    max_tokens=LLM_COMMENT_CLASSIFY_MAX_TOKENS,
                    validate=_validate,
                )
            print(
                f"[comment classify] model={actual_model} repaired={repaired}",
                flush=True,
            )
        except RuntimeError:
            if len(batch) > 10 and depth < 3:
                mid = len(batch) // 2
                print(
                    f"[comment classify] 批次校验失败，自动拆分为 {mid}+{len(batch) - mid} 条",
                    flush=True,
                )
                left = await _classify(batch[:mid], depth + 1)
                right = await _classify(batch[mid:], depth + 1)
                return left + right
            raise
        by_idx = {}
        for item in arr:
            normalized = _normalize_classify_item(item)
            if normalized:
                by_idx[normalized["idx"]] = normalized
        return [by_idx[idx] for idx in expected_idxs]

    classify_tasks = [asyncio.create_task(_classify(b)) for b in classify_batches]
    classifications: list[dict] = []
    try:
        for t in asyncio.as_completed(classify_tasks):
            classifications.extend(await t)
            classify_done += 1
            yield ("progress", f"评论分类进度 {classify_done}/{n_classify_batch} 批")
    except BaseException:
        # 任一批失败/中止时，取消其余仍在跑的批，避免继续消耗 LLM 请求。
        for task in classify_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*classify_tasks, return_exceptions=True)
        raise

    if not classifications:
        raise RuntimeError("评论分类未返回任何有效结果")

    # ── Phase 4：本地统计聚合 ───────────────────────────────────────────
    yield ("progress", "正在统计占比、情感分布与代表引用…")
    stats = comment_analysis.aggregate(final_themes, classifications)

    # ── Phase 5：生成中文舆情简报 ───────────────────────────────────────
    yield ("progress", "正在生成中文舆情简报…")
    report_payload = {
        "post_title": post_title,
        "total_comments": len(classifications),
        "source_sample_count": original_sample_count,
        "off_topic_count": off_topic_count,
        "sentiment_overall": stats["sentiment_overall"],
        "themes": [
            {
                "name": t["name"],
                "description": t["description"],
                "percentage": t["percentage"],
                "count": t["count"],
                "positive_pct": t["positive_pct"],
                "negative_pct": t["negative_pct"],
                "neutral_pct": t["neutral_pct"],
                "quotes": t["quotes"],
            }
            for t in stats["themes"]
        ],
        "other_themes": stats["other_themes"],
    }
    def _validate_report(text: str) -> str:
        if not text:
            return "报告为空"
        if "```" in text:
            return "报告包含 Markdown 代码围栏"
        missing = [
            heading
            for heading in ("## 核心结论", "## 玩家核心观点", "## 业务建议")
            if heading not in text
        ]
        if missing:
            return "缺少章节：" + "、".join(missing)
        return ""

    report_md, report_model, report_repaired = await _comment_text_call(
        task="中文评论舆情简报",
        system_prompt=report_prompt,
        query=_comment_query(
            post_title=post_title,
            post_content=post_content,
            themes_json=report_payload,
        ),
        models=report_models,
        reasoning_effort=LLM_COMMENT_SYNTHESIS_REASONING,
        max_tokens=LLM_COMMENT_MAX_TOKENS,
        validate=_validate_report,
    )
    print(
        f"[comment report] model={report_model} repaired={report_repaired}",
        flush=True,
    )

    yield ("result", {
        "themes": stats["themes"],
        "other_themes": stats["other_themes"],
        "sentiment_overall": stats["sentiment_overall"],
        "total_classified": stats["total_classified"],
        "relevance_stats": relevance_stats,
        "selected_raw_comments": [],
        "report_md": (report_md or "").strip(),
    })
    return


def _comment_sample_note_md(sess: dict) -> str:
    meta = sess.get("comment_sample_meta") or {}
    if not meta:
        return ""
    warning = str(meta.get("warning") or "").strip()
    capped = bool(meta.get("scan_capped"))
    note = (
        "> 样本口径：本次评论分析基于抽样评论统计；"
        f"扫描 {meta.get('scan_rows', 0)} 行，"
        f"非空评论 {meta.get('nonempty_count', 0)} 条，"
        f"有效评论 {meta.get('valid_count', 0)} 条，"
        f"初始抽样 {meta.get('sample_count', 0)} 条"
    )
    relevance_sample = meta.get("relevance_sample_count")
    if relevance_sample is not None:
        note += f"，实际用于相关性筛选 {relevance_sample} 条"
    related = meta.get("topic_related_count")
    off_topic = meta.get("off_topic_count")
    if related is not None:
        note += f"，其中与帖子主题/正文相关 {related} 条"
        if off_topic is not None:
            note += f"，剔除无关评论 {off_topic} 条"
    note += "。"
    if capped:
        note += " 文件超过扫描上限，结果仅代表已扫描部分。"
    if warning:
        note += f" {warning}"
    return note
