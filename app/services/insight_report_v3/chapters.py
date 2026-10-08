"""Stage two planning, profile-gated verification and chapter writing."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from copy import deepcopy
import inspect
import json
import re
import time

from app.core.config import (
    DEFAULT_INSIGHT_V3_ACTION_WRITER_SYSTEM,
    DEFAULT_INSIGHT_V3_CHAPTER_PLAN_SYSTEM,
    DEFAULT_INSIGHT_V3_CHAPTER_WRITER_SYSTEM,
    DEFAULT_INSIGHT_V3_CORE_WRITER_SYSTEM,
    DEFAULT_INSIGHT_V3_GROUP_FINDINGS_SYSTEM,
    DEFAULT_INSIGHT_V3_SKEW_VERIFY_SYSTEM,
    INSIGHT_V3_CHAPTER_CONCURRENCY,
    INSIGHT_V3_CHAPTER_MAX_RETRIES,
    INSIGHT_V3_MAX_GENERAL_ACTIONS,
    INSIGHT_V3_MAX_GROUP_ACTIONS,
    INSIGHT_V3_MAX_BLOCKS_PER_CHAPTER,
    INSIGHT_V3_MAX_CHAPTERS,
    INSIGHT_V3_MAX_GROUP_CHAPTERS,
    INSIGHT_V3_MAX_SKEW_CANDIDATES,
    INSIGHT_V3_MAX_SKEW_VERIFICATION_CALLS,
    LLM_REPORT_FALLBACK_MODELS,
    LLM_REPORT_MAX_TOKENS,
    LLM_REPORT_MODEL,
)
from app.services.insight_report_v3.frequency import (
    CROSS_QUESTION_FREQUENCY_LABEL,
    FREQUENCY_TIER_RULES,
)
from app.services.insight_report_v3.profiles import (
    evaluate_skew,
    filter_group_sources,
    preflight_skew_candidate,
    profile_catalog,
    profile_option_exists,
    validate_profile_language,
    writer_profile_payload,
)
from app.services.report_engine import (
    _direct_json_call,
    _response_ref,
    _run_bounded_calls,
)
from app.services.report_source_translation import translate_report_source_items


_PLANNER_ROOT_FIELDS = frozenset({"report_title", "chapters", "suspected_skews"})
_PLANNER_CHAPTER_FIELDS = frozenset(
    {"id", "title", "purpose", "finding_ids", "stat_ids", "group_focus"}
)
_GROUP_FOCUS_FIELDS = frozenset({"profile_name", "profile_value"})
_SUSPECTED_SKEW_FIELDS = frozenset(
    {"finding_id", "profile_name", "profile_value", "reason"}
)
_GROUP_ROOT_FIELDS = frozenset({"findings"})
_GROUP_FINDING_FIELDS = frozenset(
    {"id", "claim", "kind", "response_ids", "caveat"}
)
_SKEW_ROOT_FIELDS = frozenset({"matches"})
_SKEW_MATCH_FIELDS = frozenset({"finding_id", "response_ids"})
_WRITER_ROOT_FIELDS = frozenset({"chapter_id", "blocks"})
_WRITER_BLOCK_FIELDS = frozenset(
    {"id", "heading", "body", "finding_ids", "evidence_ids", "evidence_points"}
)
_EVIDENCE_POINT_FIELDS = frozenset({"evidence_id", "summary"})
_ACTION_ROOT_FIELDS = frozenset({"actions"})
_ACTION_FIELDS = frozenset({
    "id",
    "title",
    "action_scope",
    "target_group",
    "problem",
    "objective",
    "design_directions",
    "validation_plan",
    "finding_ids",
    "evidence_gap",
    "required_data",
})
_ACTION_GROUP_FIELDS = frozenset({"profile_name", "profile_value"})
_FINDING_KINDS = frozenset({"pain", "motivation", "risk", "wish", "fact", "uncertain"})
_FORBIDDEN_RAW_FIELDS = frozenset(
    {"quote", "excerpt", "snippet", "original", "source_text", "translation"}
)
_FORBIDDEN_FIXED_TITLES = ("调研背景", "样本概况", "核心发现", "统计附录")
_NUMBER_RE = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?%?")
_MARKDOWN_HEADING_RE = re.compile(r"(?m)^\s*#{1,6}\s+")
_MARKDOWN_TABLE_RE = re.compile(r"(?m)^\s*\|.*\|\s*$")
_PLAYER_SUBJECT_PATTERN = r"(?:[\u4e00-\u9fffA-Za-z0-9_-]{1,12})?玩家"
_FREQUENCY_SUBJECT_PREFIX_RE = re.compile(
    r"^(?:(?:绝大多数|大多数|多数|普遍|大量|很多|不少|多名|部分|少数|个别)"
    rf"(?:{_PLAYER_SUBJECT_PATTERN}|受访者|用户|人)|有玩家|有受访者)\s*"
    r"(?:认为|表示|提到|指出|反馈|强调|关注|观察到|报告称)?\s*"
)
_FREQUENCY_CLAIM_PATTERNS = (
    re.compile(
        r"(?:绝大多数|大多数|多数|普遍|大量|很多|不少|多名|部分|少数|个别)\s*"
        rf"(?:{_PLAYER_SUBJECT_PATTERN}|受访者|用户|人|反馈|回答|描述|提及|样本|观点)"
    ),
    re.compile(rf"(?:{_PLAYER_SUBJECT_PATTERN}|受访者|用户|人)(?:大多数|多数)"),
    re.compile(r"普遍"),
    re.compile(r"(?:反复|多次|频繁)(?:被)?(?:提到|提及|反馈|描述|列为|写到)"),
    re.compile(r"常见(?:情形|观点|反馈|回答|说法|原因|选择|模式)"),
    re.compile(r"往往"),
    re.compile(r"(?:有玩家|有受访者|多道题均有提及)"),
    re.compile(r"(?:高度一致|(?:形成了?)?一致认知|共同认知)"),
    re.compile(r"(?:核心|绝对|高度|广泛|共同|一致)共识"),
    re.compile(r"(?:被)?广泛(?:认为|视为|认可|接受|提到|提及)"),
    re.compile(r"一致(?:认为|表示|提到|提及|反馈|认定)"),
    re.compile(r"(?:所有|全部)(?:玩家|受访者|用户|人群|群体)"),
    re.compile(r"均(?:认为|表示|提到|提及|反映|反馈|认定)"),
    re.compile(r"全部分析章节"),
    re.compile(r"(?:反复|多次|频繁)(?:出现|呈现)"),
    re.compile(r"未见(?:明显)?分歧"),
)


def _clean(value) -> str:
    return str(value or "").strip()


def _safe_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def _claim_without_frequency_prefix(value) -> str:
    claim = _clean(value)
    stripped = _FREQUENCY_SUBJECT_PREFIX_RE.sub("", claim, count=1).lstrip("，,：:；;。 ")
    neutralized, _terms = _neutralize_frequency_claims(stripped or claim)
    return neutralized or claim


def _frequency_claim_term(value: str) -> str:
    text = _clean(value)
    for pattern in _FREQUENCY_CLAIM_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return ""


def _neutralize_frequency_claims(value: str) -> tuple[str, list[str]]:
    """Remove participant-frequency wording while preserving gameplay terms."""
    text = _clean(value)
    terms: list[str] = []
    while True:
        matched_terms = [
            match.group(0)
            for pattern in _FREQUENCY_CLAIM_PATTERNS
            for match in pattern.finditer(text)
        ]
        if not matched_terms:
            break
        terms.extend(matched_terms)
        updated = re.sub(
            r"(?:绝大多数|大多数|多数|大量|很多|不少|多名|部分|少数|个别)\s*"
            rf"({_PLAYER_SUBJECT_PATTERN}|受访者|用户|人|反馈|回答|描述|提及|样本|观点)",
            lambda match: "提及者" if match.group(1) == "人" else match.group(1),
            text,
        )
        updated = re.sub(
            rf"({_PLAYER_SUBJECT_PATTERN}|受访者|用户|人)(?:大多数|多数)",
            r"\1",
            updated,
        )
        updated = re.sub(r"(?<![所没])有(?:玩家|受访者)", "有提及者", updated)
        updated = re.sub(r"多道题均有提及", "相关材料有提及", updated)
        updated = re.sub(r"普遍", "", updated)
        updated = re.sub(
            r"(?:反复|多次|频繁)(被)?(提到|提及|反馈|描述|列为|写到)",
            lambda match: f"{match.group(1) or ''}{match.group(2)}",
            updated,
        )
        updated = re.sub(
            r"常见(情形|观点|反馈|回答|说法|原因|选择|模式)",
            r"相关\1",
            updated,
        )
        updated = re.sub(r"往往", "", updated)
        updated = re.sub(r"(?:形成了?)?一致认知", "呈现相关认知", updated)
        updated = re.sub(r"共同认知", "相关认知", updated)
        updated = re.sub(r"高度一致", "围绕相近主题", updated)
        updated = re.sub(
            r"(?:核心|绝对|高度|广泛|共同|一致)共识",
            "相关观点",
            updated,
        )
        updated = re.sub(
            r"(?:被)?广泛(?:认为|视为|认可|接受)",
            "被描述为",
            updated,
        )
        updated = re.sub(
            r"(?:被)?广泛(?:提到|提及)",
            "有材料提及",
            updated,
        )
        updated = re.sub(
            r"一致(认为|表示|提到|提及|反馈|认定)",
            r"\1",
            updated,
        )
        updated = re.sub(
            r"(?:所有|全部)(玩家|受访者|用户|人群|群体)",
            r"相关\1",
            updated,
        )
        updated = re.sub(
            r"均(认为|表示|提到|提及|反映|反馈|认定)",
            r"\1",
            updated,
        )
        updated = re.sub(r"全部分析章节", "相关分析章节", updated)
        updated = re.sub(
            r"(?:反复|多次|频繁)(出现|呈现)",
            r"\1",
            updated,
        )
        updated = re.sub(r"(?:且|并且|，)?未见(?:明显)?分歧", "", updated)
        updated = re.sub(r"[ \t]{2,}", " ", updated).strip()
        if updated == text:
            break
        text = updated
    return text, list(dict.fromkeys(terms))


async def _forward_attempt_event(callback, event: dict) -> None:
    if callback is None:
        return
    result = callback(event)
    if inspect.isawaitable(result):
        await result


def _forbidden_field_path(value, path: str = "$") -> str:
    if isinstance(value, dict):
        for key, nested in value.items():
            if _clean(key).lower() in _FORBIDDEN_RAW_FIELDS:
                return f"{path}.{key}"
            found = _forbidden_field_path(nested, f"{path}.{key}")
            if found:
                return found
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found = _forbidden_field_path(nested, f"{path}[{index}]")
            if found:
                return found
    return ""


def flatten_findings(artifact: dict) -> list[dict]:
    """Create chapter-planning units; degraded questions remain visible."""
    units: list[dict] = []
    for question in artifact.get("questions") or []:
        base = {
            "question_key": _clean(question.get("question_key")),
            "scope_key": _clean(question.get("scope_key")),
            "question": _clean(question.get("question")),
            "audience": _clean(question.get("audience")),
            "column_indexes": list(question.get("column_indexes") or []),
        }
        if question.get("status") == "complete":
            for finding in question.get("findings") or []:
                item = deepcopy(finding)
                item.update(base)
                item["degraded"] = False
                units.append(item)
        elif question.get("status") == "degraded":
            units.append({
                **base,
                "id": f"{base['scope_key']}/degraded",
                "claim": f"《{base['question']}》未完成逐题提炼，写作时需直接阅读该题全部有效原文。",
                "kind": "uncertain",
                "frequency_tier": "",
                "support_count": None,
                "response_ids": list(question.get("degraded_source_ids") or []),
                "caveat": _clean((question.get("failure") or {}).get("message")),
                "profile_presence": [],
                "skewed_toward": [],
                "support_basis": "degraded_full_source_passthrough",
                "degraded": True,
            })
    return units


def _planner_finding(finding: dict) -> dict:
    return {
        "id": finding["id"],
        "claim": _clean(finding.get("claim")),
        "kind": finding.get("kind"),
        "frequency_tier": _clean(finding.get("frequency_tier")),
        "caveat": _clean(finding.get("caveat")),
        "question": _clean(finding.get("question")),
        "audience": _clean(finding.get("audience")),
        "degraded": bool(finding.get("degraded")),
        "profile_presence": deepcopy(finding.get("profile_presence") or []),
    }


def build_stats_catalog(structured_tables: list[dict]) -> list[dict]:
    catalog = []
    for index, block in enumerate(structured_tables or [], 1):
        if not isinstance(block, dict) or not _clean(block.get("table_markdown")):
            continue
        catalog.append({
            "id": f"s{index:03d}",
            "title": _clean(block.get("title")) or f"统计表 {index}",
            "part": _clean(block.get("part")),
            "table_markdown": _clean(block.get("table_markdown")),
        })
    return catalog


def _validate_chapter_plan(
    data: dict | None,
    *,
    finding_ids: set[str],
    non_degraded_ids: set[str],
    stat_ids: set[str],
    profiles: list[dict],
) -> str | None:
    if not isinstance(data, dict) or set(data) != _PLANNER_ROOT_FIELDS:
        return "根节点必须且只能包含 report_title、chapters、suspected_skews"
    title = data.get("report_title")
    if not isinstance(title, str) or not title.strip() or "\n" in title or title.lstrip().startswith("#"):
        return "report_title 必须是单行普通文本"
    chapters = data.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        return "chapters 必须是非空数组"
    if len(chapters) > INSIGHT_V3_MAX_CHAPTERS:
        return f"chapters 不得超过 {INSIGHT_V3_MAX_CHAPTERS} 个"

    seen_chapter_ids: set[str] = set()
    seen_titles: set[str] = set()
    assigned: list[str] = []
    group_count = 0
    for index, chapter in enumerate(chapters, 1):
        if not isinstance(chapter, dict) or set(chapter) != _PLANNER_CHAPTER_FIELDS:
            return f"chapters[{index - 1}] 字段不符合契约"
        expected_id = f"c{index:02d}"
        if chapter.get("id") != expected_id or expected_id in seen_chapter_ids:
            return f"章节 ID 必须从 c01 连续编号，当前应为 {expected_id}"
        seen_chapter_ids.add(expected_id)
        chapter_title = _clean(chapter.get("title"))
        if not chapter_title or chapter_title in seen_titles or any(
            forbidden in chapter_title for forbidden in _FORBIDDEN_FIXED_TITLES
        ) or "Part" in chapter_title or "\n" in chapter_title:
            return f"章节 {expected_id} 标题为空、重复或占用了固定章节"
        seen_titles.add(chapter_title)
        if not isinstance(chapter.get("purpose"), str) or not _clean(chapter.get("purpose")):
            return f"章节 {expected_id} 缺少 purpose"
        ids = chapter.get("finding_ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids):
            return f"章节 {expected_id} 的 finding_ids 必须是非空字符串数组"
        if len(ids) != len(set(ids)) or any(item not in finding_ids for item in ids):
            return f"章节 {expected_id} 的 finding_ids 含重复或未知 ID"
        assigned.extend(ids)
        selected_stats = chapter.get("stat_ids")
        if not isinstance(selected_stats, list) or any(
            not isinstance(item, str) or item not in stat_ids for item in selected_stats
        ) or len(selected_stats) != len(set(selected_stats)) or len(selected_stats) > 3:
            return f"章节 {expected_id} 的 stat_ids 无效或超过 3 个"
        focus = chapter.get("group_focus")
        if focus is not None:
            group_count += 1
            if not isinstance(focus, dict) or set(focus) != _GROUP_FOCUS_FIELDS:
                return f"章节 {expected_id} 的 group_focus 无效"
            if not profile_option_exists(
                profiles,
                _clean(focus.get("profile_name")),
                _clean(focus.get("profile_value")),
            ):
                return f"章节 {expected_id} 使用了不存在或不可分析的画像取值"
    if group_count > INSIGHT_V3_MAX_GROUP_CHAPTERS:
        return f"人群专章不得超过 {INSIGHT_V3_MAX_GROUP_CHAPTERS} 个"
    if len(assigned) != len(set(assigned)) or set(assigned) != finding_ids:
        return "每个 finding 必须且只能分配到一个章节"

    suspected = data.get("suspected_skews")
    if not isinstance(suspected, list) or len(suspected) > INSIGHT_V3_MAX_SKEW_CANDIDATES:
        return f"suspected_skews 必须是数组且不得超过 {INSIGHT_V3_MAX_SKEW_CANDIDATES} 项"
    seen_suspects: set[tuple[str, str, str]] = set()
    for index, item in enumerate(suspected):
        if not isinstance(item, dict) or set(item) != _SUSPECTED_SKEW_FIELDS:
            return f"suspected_skews[{index}] 字段不符合契约"
        finding_id = _clean(item.get("finding_id"))
        profile_name = _clean(item.get("profile_name"))
        profile_value = _clean(item.get("profile_value"))
        key = (finding_id, profile_name, profile_value)
        if finding_id not in non_degraded_ids or key in seen_suspects:
            return f"suspected_skews[{index}] 引用了未知、降级或重复 finding"
        seen_suspects.add(key)
        if not profile_option_exists(profiles, profile_name, profile_value):
            return f"suspected_skews[{index}] 使用了不存在或不可分析的画像取值"
        if not isinstance(item.get("reason"), str) or not _clean(item.get("reason")):
            return f"suspected_skews[{index}] 缺少 reason"
    return None


def _fallback_chapter_plan(findings: list[dict]) -> dict:
    buckets: list[list[dict]] = []
    by_question: dict[str, list[dict]] = defaultdict(list)
    question_order: list[str] = []
    for finding in findings:
        key = _clean(finding.get("question")) or _clean(finding.get("scope_key"))
        if key not in by_question:
            question_order.append(key)
        by_question[key].append(finding)
    for key in question_order:
        values = by_question[key]
        if len(buckets) < INSIGHT_V3_MAX_CHAPTERS:
            buckets.append(values)
        else:
            buckets[-1].extend(values)
    chapters = []
    for index, bucket in enumerate(buckets, 1):
        question_names = list(dict.fromkeys(_clean(item.get("question")) for item in bucket))
        title = question_names[0][:40] if len(question_names) == 1 else f"玩家反馈主题 {index}"
        chapters.append({
            "id": f"c{index:02d}",
            "title": title or f"玩家反馈主题 {index}",
            "purpose": "章节规划调用失败后的保守覆盖",
            "finding_ids": [item["id"] for item in bucket],
            "stat_ids": [],
            "group_focus": None,
        })
    return {
        "report_title": "观点洞察报告",
        "chapters": chapters,
        "suspected_skews": [],
    }


_GROUP_DELIVERABLE_MARKERS = (
    "人群",
    "群体",
    "玩家",
    "用户",
    "受访者",
    "专章",
    "专项",
    "观察",
    "画像",
)


def _profile_value_aliases(profile_name: str, profile_value: str) -> set[str]:
    value = _clean(profile_value).casefold()
    name = _clean(profile_name).casefold()
    aliases = {value}
    if "性别" in name:
        if value in {"女", "女性", "female"}:
            aliases.update({"女性", "女玩家", "女性玩家", "female"})
        elif value in {"男", "男性", "male"}:
            aliases.update({"男性", "男玩家", "男性玩家", "male"})
    return {alias for alias in aliases if alias}


def _deliverable_mentions_option(
    deliverable: str,
    *,
    profile_name: str,
    profile_value: str,
) -> bool:
    text = _clean(deliverable).casefold().replace(" ", "")
    if not text or not any(marker in text for marker in _GROUP_DELIVERABLE_MARKERS):
        return False
    name = _clean(profile_name).casefold().replace(" ", "")
    aliases = _profile_value_aliases(profile_name, profile_value)
    for alias in aliases:
        normalized = alias.replace(" ", "")
        if len(normalized) >= 2 and normalized in text:
            return True
        if len(normalized) == 1 and (
            f"{normalized}玩家" in text
            or f"{normalized}用户" in text
            or f"{normalized}群体" in text
            or (name in text and normalized in text)
        ):
            return True
    return False


def required_group_chapters(
    *,
    plan: dict,
    profiles: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Resolve explicitly named expected-deliverable groups without guessing."""
    focus = (plan or {}).get("analysis_focus")
    if not isinstance(focus, dict) or not focus:
        return [], []
    deliverables = focus.get("expected_deliverables")
    if not isinstance(deliverables, list):
        return [], []
    options = [
        {
            "profile_name": _clean(item.get("profile_name")),
            "profile_value": _clean(value),
        }
        for item in profiles or []
        for value in item.get("values") or []
        if _clean(item.get("profile_name")) and _clean(value)
    ]
    resolved: list[dict] = []
    unresolved: list[dict] = []
    for raw in deliverables:
        deliverable = _clean(raw)
        matches = [
            option for option in options
            if _deliverable_mentions_option(
                deliverable,
                profile_name=option["profile_name"],
                profile_value=option["profile_value"],
            )
        ]
        unique = list({
            (item["profile_name"], item["profile_value"]): item
            for item in matches
        }.values())
        if len(unique) == 1:
            item = {**unique[0], "expected_deliverable": deliverable}
            if not any(
                existing["profile_name"] == item["profile_name"]
                and existing["profile_value"] == item["profile_value"]
                for existing in resolved
            ):
                resolved.append(item)
        elif len(unique) > 1:
            unresolved.append({
                "expected_deliverable": deliverable,
                "reason": "ambiguous_profile_group",
                "matches": unique,
            })
        elif any(
            marker in deliverable
            for marker in ("人群", "群体", "专章", "专项", "画像")
        ):
            unresolved.append({
                "expected_deliverable": deliverable,
                "reason": "profile_group_not_in_catalog",
                "matches": [],
            })
    return resolved, unresolved


def _renumber_chapters(chapters: list[dict]) -> None:
    for index, chapter in enumerate(chapters, 1):
        chapter["id"] = f"c{index:02d}"


def _merge_last_regular_chapter(chapters: list[dict]) -> bool:
    regular_indexes = [
        index for index, chapter in enumerate(chapters)
        if not isinstance(chapter.get("group_focus"), dict)
    ]
    if len(regular_indexes) < 2:
        return False
    source_index = regular_indexes[-1]
    target_index = regular_indexes[-2]
    source = chapters[source_index]
    target = chapters[target_index]
    target["finding_ids"] = list(dict.fromkeys([
        *(target.get("finding_ids") or []),
        *(source.get("finding_ids") or []),
    ]))
    target["stat_ids"] = list(dict.fromkeys([
        *(target.get("stat_ids") or []),
        *(source.get("stat_ids") or []),
    ]))[:3]
    target["purpose"] = (
        f"{_clean(target.get('purpose'))}；并覆盖{_clean(source.get('title'))}相关发现"
    ).strip("；")
    del chapters[source_index]
    return True


def ensure_required_group_chapters(
    chapter_plan: dict,
    *,
    plan: dict,
    profiles: list[dict],
) -> tuple[dict, dict]:
    """Guarantee uniquely resolved group deliverables after model planning."""
    updated = deepcopy(chapter_plan)
    chapters = updated.setdefault("chapters", [])
    focus = (plan or {}).get("analysis_focus")
    if not isinstance(focus, dict) or not focus:
        return updated, {
            "status": "missing",
            "message": "未提供分析主线，章节按问卷结构组织，未规划人群专章",
            "required": [],
            "inserted": [],
            "already_present": [],
            "unresolved": [],
        }

    requested, unresolved = required_group_chapters(plan=plan, profiles=profiles)
    required = requested[:INSIGHT_V3_MAX_GROUP_CHAPTERS]
    for overflow in requested[INSIGHT_V3_MAX_GROUP_CHAPTERS:]:
        unresolved.append({
            **overflow,
            "reason": "group_chapter_limit",
        })
    required_keys = {
        (item["profile_name"], item["profile_value"])
        for item in required
    }
    existing_by_key = {
        (
            _clean((chapter.get("group_focus") or {}).get("profile_name")),
            _clean((chapter.get("group_focus") or {}).get("profile_value")),
        ): chapter
        for chapter in chapters
        if isinstance(chapter.get("group_focus"), dict)
    }
    already_present: list[dict] = []
    inserted: list[dict] = []

    for request in required:
        key = (request["profile_name"], request["profile_value"])
        if key in existing_by_key:
            already_present.append(deepcopy(request))
            continue

        current_group_chapters = [
            chapter for chapter in chapters
            if isinstance(chapter.get("group_focus"), dict)
        ]
        if len(current_group_chapters) >= INSIGHT_V3_MAX_GROUP_CHAPTERS:
            group_key_counts: dict[tuple[str, str], int] = defaultdict(int)
            for current in current_group_chapters:
                current_key = (
                    _clean(current["group_focus"].get("profile_name")),
                    _clean(current["group_focus"].get("profile_value")),
                )
                group_key_counts[current_key] += 1
            replacement = None
            for candidate in reversed(current_group_chapters):
                candidate_key = (
                    _clean(candidate["group_focus"].get("profile_name")),
                    _clean(candidate["group_focus"].get("profile_value")),
                )
                if candidate_key not in required_keys or group_key_counts[candidate_key] > 1:
                    replacement = candidate
                    break
            if replacement is not None:
                replacement["group_focus"] = {
                    "profile_name": request["profile_name"],
                    "profile_value": request["profile_value"],
                }
                replacement["title"] = f"{request['profile_value']}群体观察"
                replacement["purpose"] = (
                    f"按已确认交付要求归纳{request['profile_name']}为"
                    f"{request['profile_value']}玩家的开放题体验，不进行群体高低比较"
                )
                existing_by_key[key] = replacement
                inserted.append({**deepcopy(request), "mode": "replaced_unrequested_group"})
                continue
            unresolved.append({**deepcopy(request), "reason": "group_chapter_limit"})
            continue

        if len(chapters) >= INSIGHT_V3_MAX_CHAPTERS and not _merge_last_regular_chapter(chapters):
            replacement = next((
                chapter for chapter in reversed(chapters)
                if not isinstance(chapter.get("group_focus"), dict)
            ), None)
            if replacement is None:
                unresolved.append({**deepcopy(request), "reason": "chapter_capacity_exhausted"})
                continue
            replacement["group_focus"] = {
                "profile_name": request["profile_name"],
                "profile_value": request["profile_value"],
            }
            replacement["title"] = f"{request['profile_value']}群体观察"
            replacement["purpose"] = (
                f"按已确认交付要求归纳{request['profile_name']}为"
                f"{request['profile_value']}玩家的开放题体验，不进行群体高低比较"
            )
            existing_by_key[key] = replacement
            inserted.append({**deepcopy(request), "mode": "repurposed_regular_chapter"})
            continue

        chapter = {
            "id": "",
            "title": f"{request['profile_value']}群体观察",
            "purpose": (
                f"按已确认交付要求归纳{request['profile_name']}为"
                f"{request['profile_value']}玩家的开放题体验，不进行群体高低比较"
            ),
            "finding_ids": [],
            "stat_ids": [],
            "group_focus": {
                "profile_name": request["profile_name"],
                "profile_value": request["profile_value"],
            },
        }
        chapters.append(chapter)
        existing_by_key[key] = chapter
        inserted.append({**deepcopy(request), "mode": "appended"})

    _renumber_chapters(chapters)
    return updated, {
        "status": "present",
        "message": "已按明确分析主线核对人群专章",
        "required": deepcopy(required),
        "inserted": inserted,
        "already_present": already_present,
        "unresolved": unresolved,
    }


async def plan_chapters(
    *,
    findings: list[dict],
    plan: dict,
    qualitative_context: dict | None,
    stats_catalog: list[dict],
    profiles: list[dict],
    on_attempt_event=None,
) -> tuple[dict, dict]:
    finding_ids = {item["id"] for item in findings}
    non_degraded_ids = {item["id"] for item in findings if not item.get("degraded")}
    stat_ids = {item["id"] for item in stats_catalog}
    contract = {
        "report_title": "单行中文标题",
        "chapters": [{
            "id": "c01",
            "title": "分析维度标题",
            "purpose": "本章回答的问题",
            "finding_ids": ["finding_id"],
            "stat_ids": ["s001"],
            "group_focus": None,
        }],
        "suspected_skews": [{
            "finding_id": "finding_id",
            "profile_name": "性别",
            "profile_value": "女性",
            "reason": "仅说明为何值得验证，不得断言已有差异",
        }],
    }
    query = (
        "<contract>\n" + _safe_json(contract) + "\n</contract>\n"
        + f"<limits>最多 {INSIGHT_V3_MAX_CHAPTERS} 个章节；人群专章最多 "
        + f"{INSIGHT_V3_MAX_GROUP_CHAPTERS} 个；suspected_skews 最多 "
        + f"{INSIGHT_V3_MAX_SKEW_CANDIDATES} 项。</limits>\n"
        + "<analysis_focus>\n"
        + _safe_json((plan or {}).get("analysis_focus") or {})
        + "\n</analysis_focus>\n"
        + "<business_context>\n"
        + _safe_json(qualitative_context or {})
        + "\n</business_context>\n"
        + "<optional_plan_parts>\n"
        + _safe_json([_clean(item.get("name")) for item in (plan or {}).get("parts") or []])
        + "\n</optional_plan_parts>\n"
        + "<analysis_profiles>\n" + _safe_json(profiles) + "\n</analysis_profiles>\n"
        + "<available_stats>\n"
        + _safe_json([{key: item[key] for key in ("id", "title", "part")} for item in stats_catalog])
        + "\n</available_stats>\n"
        + "<findings>\n"
        + _safe_json([_planner_finding(item) for item in findings])
        + "\n</findings>"
    )
    attempts = []
    models_used = []

    async def _on_attempt(event: dict):
        await _forward_attempt_event(on_attempt_event, {
            **event,
            "phase": "writing",
            "step": "chapter_plan",
        })

    for logical_attempt in range(INSIGHT_V3_CHAPTER_MAX_RETRIES + 1):
        repair_errors: list[str] = []
        result = await _direct_json_call(
            DEFAULT_INSIGHT_V3_CHAPTER_PLAN_SYSTEM,
            query,
            models=tuple(model for model in (LLM_REPORT_MODEL, *LLM_REPORT_FALLBACK_MODELS) if model),
            max_tokens=LLM_REPORT_MAX_TOKENS,
            reasoning_effort=None,
            validator=lambda data: _validate_chapter_plan(
                data,
                finding_ids=finding_ids,
                non_degraded_ids=non_degraded_ids,
                stat_ids=stat_ids,
                profiles=profiles,
            ),
            on_repair=repair_errors.append,
            on_attempt_event=_on_attempt,
        )
        if result.get("model"):
            models_used.append(result["model"])
        attempts.append({
            "attempt": logical_attempt + 1,
            "model": _clean(result.get("model")),
            "repaired": bool(result.get("repaired")),
            "repair_errors": repair_errors,
            "error": _clean(result.get("error"))[:300],
            "duration_seconds": float(result.get("duration_seconds") or 0.0),
        })
        if result.get("data") is not None:
            return result["data"], {
                "status": "complete",
                "logical_attempts": logical_attempt + 1,
                "attempts": attempts,
                "models": list(dict.fromkeys(models_used)),
            }
    return _fallback_chapter_plan(findings), {
        "status": "degraded",
        "logical_attempts": len(attempts),
        "attempts": attempts,
        "models": list(dict.fromkeys(models_used)),
        "reason": "chapter_plan_retry_exhausted",
    }


def _group_model_inputs(records: list[dict]) -> tuple[list[dict], dict[str, list[str]]]:
    by_respondent: dict[str, dict] = {}
    for record in records:
        respondent_ref = _clean(record.get("respondent_ref"))
        if not respondent_ref:
            continue
        group = by_respondent.setdefault(
            respondent_ref,
            {"responses": [], "source_ids": []},
        )
        group["source_ids"].append(record["response_id"])
        group["responses"].append({
            "question_key": str(record.get("column_index", "")),
            "text": _clean(record.get("text")),
        })
    sources = []
    short_map: dict[str, list[str]] = {}
    for index, group in enumerate(by_respondent.values()):
        short_id = _response_ref(index)
        short_map[short_id] = list(group["source_ids"])
        sources.append({"response_id": short_id, "responses": group["responses"]})
    return sources, short_map


def _validate_group_findings(data: dict | None, valid_ids: set[str]) -> str | None:
    if not isinstance(data, dict) or set(data) != _GROUP_ROOT_FIELDS:
        return "根节点只能包含 findings"
    forbidden = _forbidden_field_path(data)
    if forbidden:
        return f"模型输出包含禁止的原文字段：{forbidden}"
    findings = data.get("findings")
    if not isinstance(findings, list) or not findings:
        return "findings 必须是非空数组"
    seen: set[str] = set()
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict) or set(finding) != _GROUP_FINDING_FIELDS:
            return f"findings[{index}] 字段不符合契约"
        finding_id = _clean(finding.get("id"))
        if not finding_id or finding_id in seen:
            return f"findings[{index}] 的 id 缺失或重复"
        seen.add(finding_id)
        if not _clean(finding.get("claim")) or finding.get("kind") not in _FINDING_KINDS:
            return f"findings[{index}] 的 claim 或 kind 无效"
        if not isinstance(finding.get("caveat"), str):
            return f"findings[{index}] 的 caveat 必须是字符串"
        ids = finding.get("response_ids")
        if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)) or any(
            not isinstance(item, str) or item not in valid_ids for item in ids
        ):
            return f"findings[{index}] 的 response_ids 无效"
    return None


async def extract_group_findings(
    *,
    chapter: dict,
    source_registry: dict[str, dict],
    plan: dict,
    on_attempt_event=None,
) -> tuple[list[dict], dict]:
    focus = chapter.get("group_focus")
    if not isinstance(focus, dict):
        return [], {"status": "not_requested", "logical_attempts": 0, "models": []}
    profile_name = _clean(focus.get("profile_name"))
    profile_value = _clean(focus.get("profile_value"))
    records = filter_group_sources(
        source_registry,
        plan=plan,
        profile_name=profile_name,
        profile_value=profile_value,
    )
    sources, short_map = _group_model_inputs(records)
    if not sources:
        return [], {
            "status": "degraded",
            "logical_attempts": 0,
            "models": [],
            "reason": "group_has_no_open_text",
        }
    contract = {"findings": [{
        "id": "g1",
        "claim": "该人群反馈中的定性发现",
        "kind": "pain|motivation|risk|wish|fact|uncertain",
        "response_ids": ["r0001"],
        "caveat": "限制或空字符串",
    }]}
    query = (
        "<contract>\n" + _safe_json(contract) + "\n</contract>\n"
        + f"<group>{_safe_json({'profile_name': profile_name, 'profile_value': profile_value})}</group>\n"
        + "<responses>\n" + _safe_json(sources) + "\n</responses>"
    )

    async def _on_attempt(event: dict):
        await _forward_attempt_event(on_attempt_event, {
            **event,
            "phase": "writing",
            "step": "group_findings",
            "chapter_id": chapter.get("id"),
        })

    result = await _direct_json_call(
        DEFAULT_INSIGHT_V3_GROUP_FINDINGS_SYSTEM,
        query,
        models=tuple(model for model in (LLM_REPORT_MODEL, *LLM_REPORT_FALLBACK_MODELS) if model),
        max_tokens=LLM_REPORT_MAX_TOKENS,
        reasoning_effort=None,
        validator=lambda data: _validate_group_findings(data, set(short_map)),
        on_attempt_event=_on_attempt,
    )
    if result.get("data") is None:
        return [], {
            "status": "degraded",
            "logical_attempts": 1,
            "models": [result["model"]] if result.get("model") else [],
            "reason": _clean(result.get("error")) or "group_findings_failed",
        }
    hydrated = []
    for raw in result["data"]["findings"]:
        response_ids = list(dict.fromkeys(
            response_id
            for short_id in raw["response_ids"]
            for response_id in short_map[short_id]
        ))
        hydrated.append({
            "id": f"group:{chapter['id']}/{raw['id']}",
            "claim": _clean(raw.get("claim")),
            "kind": raw.get("kind"),
            "frequency_tier": CROSS_QUESTION_FREQUENCY_LABEL,
            "support_count": None,
            "response_ids": response_ids,
            "caveat": _clean(raw.get("caveat")),
            "profile_presence": [{
                "profile_name": profile_name,
                "values": [profile_value],
                "basis": "code_filtered_group_all_open_text",
            }],
            "skewed_toward": [],
            "support_basis": "code_filtered_group_qualitative_extraction",
            "question_key": f"group:{chapter['id']}",
            "scope_key": f"group:{chapter['id']}",
            "question": f"{profile_name}={profile_value} 人群全部开放题",
            "audience": f"{profile_name}：{profile_value}",
            "column_indexes": sorted({record.get("column_index") for record in records}),
            "degraded": False,
        })
    return hydrated, {
        "status": "complete",
        "logical_attempts": 1,
        "models": [result["model"]] if result.get("model") else [],
        "respondent_count": len(sources),
        "source_count": len(records),
    }


def _validate_skew_matches(
    data: dict | None,
    *,
    finding_ids: list[str],
    valid_short_ids: set[str],
) -> str | None:
    if not isinstance(data, dict) or set(data) != _SKEW_ROOT_FIELDS:
        return "根节点只能包含 matches"
    matches = data.get("matches")
    if not isinstance(matches, list):
        return "matches 必须是数组"
    seen: set[str] = set()
    for index, item in enumerate(matches):
        if not isinstance(item, dict) or set(item) != _SKEW_MATCH_FIELDS:
            return f"matches[{index}] 字段不符合契约"
        finding_id = _clean(item.get("finding_id"))
        if finding_id not in finding_ids or finding_id in seen:
            return f"matches[{index}] 的 finding_id 未知或重复"
        seen.add(finding_id)
        ids = item.get("response_ids")
        if not isinstance(ids, list) or len(ids) != len(set(ids)) or any(
            not isinstance(value, str) or value not in valid_short_ids for value in ids
        ):
            return f"matches[{index}] 的 response_ids 无效"
    if seen != set(finding_ids):
        return "每个待验证 finding 必须返回且只能返回一次"
    return None


async def verify_suspected_skews(
    *,
    findings: list[dict],
    suspected_skews: list[dict],
    source_contract: dict,
    plan: dict,
    on_attempt_event=None,
) -> tuple[list[dict], dict]:
    result_findings = deepcopy(findings)
    by_id = {item["id"]: item for item in result_findings}
    questions_by_scope = {
        _clean(item.get("scope_key")): item
        for item in source_contract.get("questions") or []
    }
    registry = source_contract.get("source_registry") or {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    diagnostics: list[dict] = []
    for suspect in (suspected_skews or [])[:INSIGHT_V3_MAX_SKEW_CANDIDATES]:
        finding = by_id.get(_clean(suspect.get("finding_id")))
        if finding is None:
            continue
        scope_key = _clean(finding.get("scope_key"))
        if scope_key not in questions_by_scope:
            diagnostics.append({
                **deepcopy(suspect),
                "status": "unavailable",
                "reason": "scope_source_mapping_missing",
            })
            continue
        question = questions_by_scope[scope_key]
        scope_response_ids = list(dict.fromkeys(
            source_id
            for source_ids in (question.get("short_id_map") or {}).values()
            for source_id in source_ids
        ))
        preflight = preflight_skew_candidate(
            scope_response_ids=scope_response_ids,
            source_registry=registry,
            plan=plan,
            profile_name=_clean(suspect.get("profile_name")),
            profile_value=_clean(suspect.get("profile_value")),
        )
        if preflight.get("status") != "eligible":
            diagnostics.append({
                **deepcopy(suspect),
                "status": "unavailable",
                "reason": preflight.get("reason") or "skew_preflight_failed",
                "diagnostics": deepcopy(preflight.get("diagnostics") or {}),
                "preflight": True,
            })
            continue
        grouped[scope_key].append(deepcopy(suspect))

    selected_scopes = list(grouped)[:INSIGHT_V3_MAX_SKEW_VERIFICATION_CALLS]
    for scope_key in list(grouped)[INSIGHT_V3_MAX_SKEW_VERIFICATION_CALLS:]:
        for suspect in grouped[scope_key]:
            diagnostics.append({
                **suspect,
                "status": "unavailable",
                "reason": "verification_call_limit",
            })

    async def _verify_scope(scope_key: str):
        question = questions_by_scope[scope_key]
        suspects = grouped[scope_key]
        query = (
            "<contract>\n"
            + _safe_json({"matches": [{"finding_id": "原 finding_id", "response_ids": ["r0001"]}]})
            + "\n</contract>\n<question>\n"
            + _clean(question.get("question"))
            + "\n</question>\n<findings>\n"
            + _safe_json([{
                "finding_id": item["finding_id"],
                "claim": by_id[item["finding_id"]]["claim"],
            } for item in suspects])
            + "\n</findings>\n<responses>\n"
            + _safe_json(question.get("model_sources") or [])
            + "\n</responses>"
        )

        async def _on_attempt(event: dict):
            await _forward_attempt_event(on_attempt_event, {
                **event,
                "phase": "writing",
                "step": "skew_verify",
                "scope_key": scope_key,
            })

        call = await _direct_json_call(
            DEFAULT_INSIGHT_V3_SKEW_VERIFY_SYSTEM,
            query,
            models=tuple(model for model in (LLM_REPORT_MODEL, *LLM_REPORT_FALLBACK_MODELS) if model),
            max_tokens=LLM_REPORT_MAX_TOKENS,
            reasoning_effort=None,
            validator=lambda data: _validate_skew_matches(
                data,
                finding_ids=[item["finding_id"] for item in suspects],
                valid_short_ids=set(question.get("short_id_map") or {}),
            ),
            on_attempt_event=_on_attempt,
        )
        return scope_key, call

    factories = [lambda scope_key=scope_key: _verify_scope(scope_key) for scope_key in selected_scopes]
    calls: dict[str, dict] = {}
    async for event_type, payload in _run_bounded_calls(
        factories,
        min(INSIGHT_V3_CHAPTER_CONCURRENCY, max(1, len(factories))),
    ):
        if event_type == "result":
            _index, (scope_key, call) = payload
            calls[scope_key] = call

    for scope_key in selected_scopes:
        question = questions_by_scope[scope_key]
        call = calls.get(scope_key) or {}
        suspects = grouped[scope_key]
        if call.get("data") is None:
            for suspect in suspects:
                diagnostics.append({
                    **suspect,
                    "status": "unavailable",
                    "reason": _clean(call.get("error")) or "verification_failed",
                })
            continue
        matched = {
            item["finding_id"]: item["response_ids"]
            for item in call["data"]["matches"]
        }
        scope_response_ids = list(dict.fromkeys(
            source_id
            for source_ids in (question.get("short_id_map") or {}).values()
            for source_id in source_ids
        ))
        for suspect in suspects:
            supporter_response_ids = list(dict.fromkeys(
                source_id
                for short_id in matched.get(suspect["finding_id"], [])
                for source_id in question["short_id_map"][short_id]
            ))
            evaluation = evaluate_skew(
                scope_response_ids=scope_response_ids,
                supporter_response_ids=supporter_response_ids,
                source_registry=registry,
                plan=plan,
                profile_name=suspect["profile_name"],
                profile_value=suspect["profile_value"],
            )
            if evaluation.get("skewed_toward"):
                by_id[suspect["finding_id"]].setdefault("skewed_toward", []).append(
                    evaluation["skewed_toward"]
                )
            diagnostics.append({
                **suspect,
                "status": evaluation["status"],
                "reason": evaluation["reason"],
                "diagnostics": evaluation["diagnostics"],
                "model": _clean(call.get("model")),
            })
    return result_findings, {
        "requested_count": len(suspected_skews or []),
        "prefiltered_count": sum(bool(item.get("preflight")) for item in diagnostics),
        "verification_call_count": len(calls),
        "verified_count": sum(item.get("status") == "verified" for item in diagnostics),
        "items": diagnostics,
        "models": list(dict.fromkeys(
            _clean(call.get("model")) for call in calls.values() if _clean(call.get("model"))
        )),
    }


def _chapter_sources(
    finding_ids: list[str],
    *,
    findings_by_id: dict[str, dict],
    source_registry: dict[str, dict],
) -> tuple[list[dict], dict[str, str], dict[str, set[str]]]:
    global_to_short: dict[str, str] = {}
    short_to_global: dict[str, str] = {}
    allowed_by_short: dict[str, set[str]] = defaultdict(set)
    sources = []
    for finding_id in finding_ids:
        finding = findings_by_id[finding_id]
        for response_id in finding.get("response_ids") or []:
            record = source_registry.get(response_id)
            if not isinstance(record, dict):
                continue
            short_id = global_to_short.get(response_id)
            if short_id is None:
                short_id = _response_ref(len(global_to_short))
                global_to_short[response_id] = short_id
                short_to_global[short_id] = response_id
                sources.append({
                    "response_id": short_id,
                    "question_key": _clean(finding.get("question_key")) or str(record.get("column_index", "")),
                    "question": _clean(finding.get("question")),
                    "text": _clean(record.get("text")),
                })
            allowed_by_short[short_id].add(finding_id)
    return sources, short_to_global, allowed_by_short


def _writer_finding(finding: dict, global_to_short: dict[str, str]) -> dict:
    return {
        "id": finding["id"],
        "claim": _claim_without_frequency_prefix(finding.get("claim")),
        "kind": finding.get("kind"),
        "frequency_tier": _clean(finding.get("frequency_tier")),
        "caveat": _clean(finding.get("caveat")),
        "question": _clean(finding.get("question")),
        "audience": _clean(finding.get("audience")),
        "degraded": bool(finding.get("degraded")),
        "representative_evidence_ids": list(dict.fromkeys(
            global_to_short[response_id]
            for response_id in finding.get("response_ids") or []
            if response_id in global_to_short
        )),
        **writer_profile_payload(finding),
    }


def _validate_writer_output(
    data: dict | None,
    *,
    chapter_id: str,
    assigned_finding_ids: set[str],
    required_finding_ids: set[str],
    valid_short_ids: set[str],
    allowed_by_short: dict[str, set[str]],
    findings_by_id: dict[str, dict],
    profiles: list[dict],
    numeric_sources: str,
    maximum_blocks: int | None = None,
    frequency_rewrites: list[dict] | None = None,
) -> str | None:
    if not isinstance(data, dict) or set(data) != _WRITER_ROOT_FIELDS:
        return "根节点必须且只能包含 chapter_id、blocks"
    if data.get("chapter_id") != chapter_id:
        return "chapter_id 与当前章节不一致"
    blocks = data.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return "blocks 必须是非空数组"
    if maximum_blocks is not None and len(blocks) > maximum_blocks:
        return f"blocks 不得超过 {maximum_blocks} 项"
    seen_blocks: set[str] = set()
    covered: set[str] = set()
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or set(block) != _WRITER_BLOCK_FIELDS:
            return f"blocks[{index}] 字段不符合契约"
        block_id = _clean(block.get("id"))
        if not block_id or block_id in seen_blocks:
            return f"blocks[{index}] 的 id 缺失或重复"
        seen_blocks.add(block_id)
        finding_ids = block.get("finding_ids")
        if not isinstance(finding_ids, list) or not finding_ids or len(finding_ids) != len(set(finding_ids)) or any(
            not isinstance(item, str) or item not in assigned_finding_ids for item in finding_ids
        ):
            return f"blocks[{index}] 的 finding_ids 无效"
        covered.update(finding_ids)
        evidence_ids = block.get("evidence_ids")
        if not isinstance(evidence_ids, list) or len(evidence_ids) != len(set(evidence_ids)) or any(
            not isinstance(item, str) or item not in valid_short_ids for item in evidence_ids
        ):
            return f"blocks[{index}] 的 evidence_ids 无效"
        evidence_points = block.get("evidence_points")
        if not isinstance(evidence_points, list):
            return f"blocks[{index}] 的 evidence_points 必须是数组"
        seen_point_ids: set[str] = set()
        for point_index, point in enumerate(evidence_points):
            if not isinstance(point, dict) or set(point) != _EVIDENCE_POINT_FIELDS:
                return f"blocks[{index}].evidence_points[{point_index}] 字段不符合契约"
            evidence_id = _clean(point.get("evidence_id"))
            summary = point.get("summary")
            if evidence_id not in evidence_ids or evidence_id in seen_point_ids:
                return f"blocks[{index}].evidence_points[{point_index}] 引用无效或重复"
            seen_point_ids.add(evidence_id)
            if not isinstance(summary, str) or not _clean(summary):
                return f"blocks[{index}].evidence_points[{point_index}] 缺少 summary"
            normalized_summary, summary_terms = _neutralize_frequency_claims(
                " ".join(summary.split())
            )
            if (
                not normalized_summary
                or len(normalized_summary) > 180
                or len(re.findall(r"[。！？!?]", normalized_summary)) > 1
            ):
                return f"blocks[{index}].evidence_points[{point_index}] 必须是一句不超过 180 字的概括"
            if _MARKDOWN_HEADING_RE.search(normalized_summary) or _MARKDOWN_TABLE_RE.search(normalized_summary):
                return f"blocks[{index}].evidence_points[{point_index}] 不得包含 Markdown 标题或表格"
            point["summary"] = normalized_summary
            if summary_terms and frequency_rewrites is not None:
                frequency_rewrites.append({
                    "block_id": block_id,
                    "evidence_id": evidence_id,
                    "terms": list(dict.fromkeys(summary_terms)),
                })
            profile_error = validate_profile_language(
                normalized_summary,
                findings=[findings_by_id[item] for item in finding_ids],
                catalog=profiles,
            )
            if profile_error:
                return f"blocks[{index}].evidence_points[{point_index}]：{profile_error}"
            for number in _NUMBER_RE.findall(normalized_summary):
                if number not in numeric_sources:
                    return (
                        f"blocks[{index}].evidence_points[{point_index}] "
                        f"包含来源中不存在的数字：{number}"
                    )
        if not any(allowed_by_short[item] & set(finding_ids) for item in valid_short_ids):
            return f"blocks[{index}] 的 finding 没有可回绑的代表证据"
        heading = block.get("heading")
        body = block.get("body")
        if not isinstance(heading, str) or not _clean(heading) or not isinstance(body, str) or not _clean(body):
            return f"blocks[{index}] 的 heading/body 必须是非空字符串"
        normalized_heading, heading_terms = _neutralize_frequency_claims(heading)
        normalized_body, body_terms = _neutralize_frequency_claims(body)
        if not normalized_heading or not normalized_body:
            return f"blocks[{index}] 去除模型自报频次后 heading/body 为空"
        block["heading"] = normalized_heading
        block["body"] = normalized_body
        rewritten_terms = list(dict.fromkeys([*heading_terms, *body_terms]))
        if rewritten_terms and frequency_rewrites is not None:
            frequency_rewrites.append({
                "block_id": block_id,
                "terms": rewritten_terms,
            })
        combined = f"{normalized_heading}\n{normalized_body}"
        if _MARKDOWN_HEADING_RE.search(combined) or _MARKDOWN_TABLE_RE.search(combined):
            return f"blocks[{index}] 不得包含 Markdown 标题或表格"
        used_frequency = _frequency_claim_term(combined)
        if used_frequency:
            return f"blocks[{index}] 不得自行写频次词“{used_frequency}”"
        profile_error = validate_profile_language(
            combined,
            findings=[findings_by_id[item] for item in finding_ids],
            catalog=profiles,
        )
        if profile_error:
            return f"blocks[{index}]：{profile_error}"
        for number in _NUMBER_RE.findall(combined):
            if number not in numeric_sources:
                return f"blocks[{index}] 包含未由所选统计表提供的数字：{number}"
    missing_required = required_finding_ids - covered
    if missing_required:
        return f"章节遗漏必须覆盖的风险或降级 finding：{sorted(missing_required)[0]}"
    return None


def _canonicalize_block_evidence(
    blocks: list[dict],
    *,
    allowed_by_short: dict[str, set[str]],
    maximum_evidence_per_block: int = 2,
) -> tuple[list[dict], int]:
    """Rebind model-selected short IDs to representative evidence for each block."""
    canonical: list[dict] = []
    rebound_count = 0
    ordered_short_ids = list(allowed_by_short)
    evidence_limit = max(1, int(maximum_evidence_per_block))
    for block in blocks:
        item = deepcopy(block)
        finding_ids = set(item.get("finding_ids") or [])
        requested = list(dict.fromkeys(item.get("evidence_ids") or []))
        selected = [
            short_id
            for short_id in requested
            if short_id in allowed_by_short and allowed_by_short[short_id] & finding_ids
        ][:evidence_limit]
        covered_findings = set().union(
            *(allowed_by_short[short_id] & finding_ids for short_id in selected)
        ) if selected else set()
        for finding_id in item.get("finding_ids") or []:
            if finding_id in covered_findings or len(selected) >= evidence_limit:
                continue
            short_id = next((
                candidate
                for candidate in ordered_short_ids
                if candidate not in selected and finding_id in allowed_by_short[candidate]
            ), None)
            if short_id is not None:
                selected.append(short_id)
                covered_findings.update(allowed_by_short[short_id] & finding_ids)
        if not selected:
            short_id = next((
                candidate
                for candidate in ordered_short_ids
                if allowed_by_short[candidate] & finding_ids
            ), None)
            if short_id is not None:
                selected.append(short_id)
        if selected != requested:
            rebound_count += 1
        item["evidence_ids"] = selected
        points_by_id = {
            _clean(point.get("evidence_id")): deepcopy(point)
            for point in item.get("evidence_points") or []
            if isinstance(point, dict)
        }
        item["evidence_points"] = [
            points_by_id[short_id]
            for short_id in selected
            if short_id in points_by_id
        ]
        canonical.append(item)
    return canonical, rebound_count


async def _translate_evidence(
    *,
    blocks: list[dict],
    sources: list[dict],
    short_to_global: dict[str, str],
    namespace: str,
) -> list[dict]:
    requested = list(dict.fromkeys(
        short_id
        for block in blocks
        for short_id in block.get("evidence_ids") or []
    ))
    source_by_short = {item["response_id"]: item for item in sources}
    translated_by_short: dict[str, dict] = {}
    if requested:
        translated = await translate_report_source_items(
            [source_by_short[short_id] for short_id in requested],
            namespace=namespace,
        )
        translated_by_short = {item["response_id"]: item for item in translated}
    hydrated = []
    for block in blocks:
        point_by_short = {
            _clean(point.get("evidence_id")): _clean(point.get("summary"))
            for point in block.get("evidence_points") or []
            if isinstance(point, dict)
        }
        item = {
            key: deepcopy(value)
            for key, value in block.items()
            if key not in {"evidence_ids", "evidence_points"}
        }
        item["evidence"] = []
        for short_id in block.get("evidence_ids") or []:
            translated = translated_by_short.get(short_id) or {}
            item["evidence"].append({
                "response_id": short_to_global[short_id],
                "evidence_point": point_by_short.get(short_id, ""),
                "translation_zh": _clean(translated.get("translation_zh")),
                "translation_status": translated.get("translation_status") or "failed",
                **(
                    {"translation_error": translated.get("translation_error")}
                    if translated.get("translation_error") else {}
                ),
            })
        hydrated.append(item)
    return hydrated


def _fallback_blocks(
    finding_ids: list[str],
    findings_by_id: dict[str, dict],
    global_to_short: dict[str, str],
    *,
    maximum_blocks: int = 5,
) -> list[dict]:
    available = [finding_id for finding_id in dict.fromkeys(finding_ids) if finding_id in findings_by_id]
    if not available:
        return []
    block_limit = max(1, int(maximum_blocks))
    input_order = {finding_id: index for index, finding_id in enumerate(available)}
    tier_order = {
        tier: len(FREQUENCY_TIER_RULES) - index
        for index, tier in enumerate(FREQUENCY_TIER_RULES)
    }

    def _priority(finding_id: str) -> tuple[int, int, int]:
        finding = findings_by_id[finding_id]
        required_rank = 0 if finding.get("kind") == "risk" else 1 if finding.get("degraded") else 2
        frequency_rank = tier_order.get(_clean(finding.get("frequency_tier")), 0)
        return (required_rank, -frequency_rank, input_order[finding_id])

    ordered = sorted(available, key=_priority)
    required = [
        finding_id
        for finding_id in ordered
        if findings_by_id[finding_id].get("kind") == "risk" or findings_by_id[finding_id].get("degraded")
    ]
    optional = [finding_id for finding_id in ordered if finding_id not in set(required)]
    if len(required) <= block_limit:
        groups = [[finding_id] for finding_id in required]
        groups.extend([[finding_id] for finding_id in optional[:block_limit - len(groups)]])
    else:
        groups = [[] for _ in range(block_limit)]
        for index, finding_id in enumerate(required):
            groups[index % block_limit].append(finding_id)

    blocks = []
    for index, group in enumerate(groups, 1):
        findings = [findings_by_id[finding_id] for finding_id in group]
        claims = [_claim_without_frequency_prefix(finding.get("claim")) for finding in findings]
        first_claim = claims[0] if claims else ""
        heading = first_claim if len(first_claim) <= 36 else f"{first_claim[:35].rstrip()}…"
        caveats = list(dict.fromkeys(
            _clean(finding.get("caveat")) for finding in findings if _clean(finding.get("caveat"))
        ))
        body = "；".join(claim for claim in claims if claim)
        if caveats:
            body += f"（证据边界：{'；'.join(caveats)}）"
        if any(finding.get("degraded") for finding in findings):
            body += " 本题提炼阶段已降级，此处仅保留原文直送写作的可追溯入口。"
        evidence_ids = []
        for finding in findings:
            short_id = next((
                global_to_short[response_id]
                for response_id in finding.get("response_ids") or []
                if response_id in global_to_short and global_to_short[response_id] not in evidence_ids
            ), None)
            if short_id is not None:
                evidence_ids.append(short_id)
            if len(evidence_ids) >= 2:
                break
        blocks.append({
            "id": f"fallback-{index}",
            "heading": heading or "待人工复核的发现",
            "body": body or "该发现需要人工复核。",
            "finding_ids": group,
            "evidence_ids": evidence_ids,
            "evidence_points": [],
        })
    return blocks


async def _write_one_section(
    *,
    chapter: dict,
    findings_by_id: dict[str, dict],
    source_registry: dict[str, dict],
    stats_by_id: dict[str, dict],
    profiles: list[dict],
    qualitative_context: dict | None,
    translation_namespace: str,
    core_context: list[dict] | None = None,
    on_attempt_event=None,
) -> dict:
    chapter_id = chapter["id"]
    finding_ids = list(dict.fromkeys(chapter.get("finding_ids") or []))
    group_focus = chapter.get("group_focus")
    if not finding_ids and isinstance(group_focus, dict):
        profile_name = _clean(group_focus.get("profile_name"))
        profile_value = _clean(group_focus.get("profile_value"))
        return {
            **deepcopy(chapter),
            "blocks": [{
                "id": "group-evidence-gap",
                "heading": f"{profile_value or '该'}群体材料暂不足",
                "body": (
                    f"已按{profile_name or '画像'}为{profile_value or '目标取值'}筛选开放题材料，"
                    "但尚未形成可通过证据绑定校验的稳定发现。若要完成该人群观察，"
                    "需要补充该人群更完整的有效开放题反馈及具体体验场景。"
                ),
                "finding_ids": [],
                "evidence": [],
            }],
            "diagnostics": {
                "status": "degraded",
                "logical_attempts": 0,
                "attempts": [],
                "models": [],
                "reason": "group_findings_unavailable",
                "evidence_rebound_count": 0,
                "frequency_rewrite_count": 0,
                "frequency_rewrites": [],
            },
        }
    sources, short_to_global, allowed_by_short = _chapter_sources(
        finding_ids,
        findings_by_id=findings_by_id,
        source_registry=source_registry,
    )
    global_to_short = {
        global_id: short_id
        for short_id, global_id in short_to_global.items()
    }
    selected_stats = [stats_by_id[item] for item in chapter.get("stat_ids") or [] if item in stats_by_id]
    is_core = chapter_id == "core"
    maximum_blocks = 5 if is_core else INSIGHT_V3_MAX_BLOCKS_PER_CHAPTER
    required_finding_ids = {
        finding_id
        for finding_id in finding_ids
        if findings_by_id[finding_id].get("kind") == "risk" or findings_by_id[finding_id].get("degraded")
    } if not is_core else set()
    numeric_sources = "\n".join([
        *(item["table_markdown"] for item in selected_stats),
        *(
            "\n".join(
                _clean(findings_by_id[finding_id].get(field))
                for field in ("claim", "caveat", "question")
            )
            for finding_id in finding_ids
        ),
        *(_clean(source.get("text")) for source in sources),
        _safe_json(core_context or []),
    ])
    system_prompt = DEFAULT_INSIGHT_V3_CORE_WRITER_SYSTEM if is_core else DEFAULT_INSIGHT_V3_CHAPTER_WRITER_SYSTEM
    contract = {"chapter_id": chapter_id, "blocks": [{
        "id": "b1",
        "heading": "短标题",
        "body": "中文分析正文，不含频次词；引用位置由 evidence_ids 表示",
        "finding_ids": [finding_ids[0]] if finding_ids else [],
        "evidence_ids": [sources[0]["response_id"]] if sources else [],
        "evidence_points": ([{
            "evidence_id": sources[0]["response_id"],
            "summary": "该条证据的一句话中文要点",
        }] if sources else []),
    }]}
    query = (
        "<contract>\n" + _safe_json(contract) + "\n</contract>\n"
        + "<chapter>\n" + _safe_json({
            "id": chapter_id,
            "title": chapter.get("title"),
            "purpose": chapter.get("purpose"),
            "group_focus": chapter.get("group_focus"),
        }) + "\n</chapter>\n"
        + "<business_context>\n" + _safe_json(qualitative_context or {}) + "\n</business_context>\n"
        + "<findings>\n"
        + _safe_json([_writer_finding(findings_by_id[item], global_to_short) for item in finding_ids])
        + "\n</findings>\n"
        + "<writer_constraints>\n"
        + _safe_json({
            "maximum_blocks": maximum_blocks,
            "required_finding_ids": sorted(required_finding_ids),
        })
        + "\n</writer_constraints>\n"
        + "<objective_stats>\n"
        + _safe_json([{key: item[key] for key in ("id", "title", "table_markdown")} for item in selected_stats])
        + "\n</objective_stats>\n"
        + ("<completed_chapters>\n" + _safe_json(core_context or []) + "\n</completed_chapters>\n" if is_core else "")
        + "<evidence_sources>\n" + _safe_json(sources) + "\n</evidence_sources>"
    )
    attempts = []
    models = []
    valid_short_ids = set(short_to_global)

    async def _on_attempt(event: dict):
        await _forward_attempt_event(on_attempt_event, {
            **event,
            "phase": "writing",
            "step": "core" if is_core else "chapter",
            "chapter_id": chapter_id,
        })

    for logical_attempt in range(INSIGHT_V3_CHAPTER_MAX_RETRIES + 1):
        repairs: list[str] = []
        frequency_rewrites: list[dict] = []

        def _validate(data):
            frequency_rewrites.clear()
            return _validate_writer_output(
                data,
                chapter_id=chapter_id,
                assigned_finding_ids=set(finding_ids),
                required_finding_ids=required_finding_ids,
                valid_short_ids=valid_short_ids,
                allowed_by_short=allowed_by_short,
                findings_by_id=findings_by_id,
                profiles=profiles,
                numeric_sources=numeric_sources,
                maximum_blocks=maximum_blocks,
                frequency_rewrites=frequency_rewrites,
            )

        result = await _direct_json_call(
            system_prompt,
            query,
            models=tuple(model for model in (LLM_REPORT_MODEL, *LLM_REPORT_FALLBACK_MODELS) if model),
            max_tokens=LLM_REPORT_MAX_TOKENS,
            reasoning_effort=None,
            validator=_validate,
            on_repair=repairs.append,
            on_attempt_event=_on_attempt,
        )
        if result.get("model"):
            models.append(result["model"])
        attempts.append({
            "attempt": logical_attempt + 1,
            "model": _clean(result.get("model")),
            "repaired": bool(result.get("repaired")),
            "repair_errors": repairs,
            "error": _clean(result.get("error"))[:300],
            "duration_seconds": float(result.get("duration_seconds") or 0.0),
        })
        if result.get("data") is None:
            continue
        canonical_blocks, evidence_rebound_count = _canonicalize_block_evidence(
            result["data"]["blocks"],
            allowed_by_short=allowed_by_short,
        )
        blocks = await _translate_evidence(
            blocks=canonical_blocks,
            sources=sources,
            short_to_global=short_to_global,
            namespace=translation_namespace,
        )
        return {
            **deepcopy(chapter),
            "blocks": blocks,
            "diagnostics": {
                "status": "complete",
                "logical_attempts": logical_attempt + 1,
                "attempts": attempts,
                "models": list(dict.fromkeys(models)),
                "evidence_rebound_count": evidence_rebound_count,
                "frequency_rewrite_count": len(frequency_rewrites),
                "frequency_rewrites": deepcopy(frequency_rewrites),
            },
        }

    fallback_raw = _fallback_blocks(
        finding_ids,
        findings_by_id,
        {
            global_id: short_id
            for short_id, global_id in short_to_global.items()
        },
        maximum_blocks=maximum_blocks,
    )
    fallback = await _translate_evidence(
        blocks=fallback_raw,
        sources=sources,
        short_to_global=short_to_global,
        namespace=translation_namespace,
    )
    return {
        **deepcopy(chapter),
        "blocks": fallback,
        "diagnostics": {
            "status": "degraded",
            "logical_attempts": len(attempts),
            "attempts": attempts,
            "models": list(dict.fromkeys(models)),
            "reason": "writer_retry_exhausted",
            "evidence_rebound_count": 0,
            "frequency_rewrite_count": 0,
            "frequency_rewrites": [],
        },
    }


async def write_chapters(
    *,
    chapters: list[dict],
    findings_by_id: dict[str, dict],
    source_registry: dict[str, dict],
    stats_catalog: list[dict],
    profiles: list[dict],
    qualitative_context: dict | None,
    translation_namespace: str,
    concurrency: int | None = None,
    on_attempt_event=None,
):
    stats_by_id = {item["id"]: item for item in stats_catalog}
    factories = [
        lambda chapter=chapter: _write_one_section(
            chapter=chapter,
            findings_by_id=findings_by_id,
            source_registry=source_registry,
            stats_by_id=stats_by_id,
            profiles=profiles,
            qualitative_context=qualitative_context,
            translation_namespace=translation_namespace,
            on_attempt_event=on_attempt_event,
        )
        for chapter in chapters
    ]
    worker_count = INSIGHT_V3_CHAPTER_CONCURRENCY if concurrency is None else max(1, int(concurrency))
    completed: dict[int, dict] = {}
    async for event_type, payload in _run_bounded_calls(factories, worker_count):
        if event_type == "heartbeat":
            yield ("heartbeat", "")
        elif event_type == "result":
            index, chapter = payload
            completed[index] = chapter
            yield ("chapter_result", deepcopy(chapter))
    yield ("result", [completed[index] for index in sorted(completed)])


async def write_core_findings(
    *,
    chapters: list[dict],
    findings_by_id: dict[str, dict],
    source_registry: dict[str, dict],
    profiles: list[dict],
    qualitative_context: dict | None,
    translation_namespace: str,
    on_attempt_event=None,
) -> dict:
    finding_ids = list(dict.fromkeys(
        finding_id
        for chapter in chapters
        for finding_id in chapter.get("finding_ids") or []
        if finding_id in findings_by_id
    ))
    virtual = {
        "id": "core",
        "title": "核心发现",
        "purpose": "汇总全部分析章节中最重要、最有决策价值的判断",
        "finding_ids": finding_ids,
        "stat_ids": [],
        "group_focus": None,
    }
    compact_chapters = [
        {
            "title": chapter.get("title"),
            "blocks": [
                {
                    "heading": block.get("heading"),
                    "body": block.get("body"),
                    "finding_ids": block.get("finding_ids"),
                }
                for block in chapter.get("blocks") or []
            ],
        }
        for chapter in chapters
    ]
    return await _write_one_section(
        chapter=virtual,
        findings_by_id=findings_by_id,
        source_registry=source_registry,
        stats_by_id={},
        profiles=profiles,
        qualitative_context=qualitative_context,
        translation_namespace=translation_namespace,
        core_context=compact_chapters,
        on_attempt_event=on_attempt_event,
    )


def _action_group_bindings(chapters: list[dict]) -> tuple[set[tuple[str, str]], dict[str, tuple[str, str]]]:
    group_options: set[tuple[str, str]] = set()
    finding_groups: dict[str, tuple[str, str]] = {}
    for chapter in chapters or []:
        focus = chapter.get("group_focus")
        if not isinstance(focus, dict):
            continue
        key = (
            _clean(focus.get("profile_name")),
            _clean(focus.get("profile_value")),
        )
        if not all(key):
            continue
        group_options.add(key)
        for finding_id in chapter.get("finding_ids") or []:
            finding_groups[_clean(finding_id)] = key
    return group_options, finding_groups


def _normalize_action_text(value: str) -> tuple[str, list[str]]:
    text = " ".join(_clean(value).split())
    text = re.sub(r"^(?:[-*]|\d+[.)、])\s*", "", text)
    return _neutralize_frequency_claims(text)


def _validate_action_output(
    data: dict | None,
    *,
    findings_by_id: dict[str, dict],
    chapters: list[dict],
    profiles: list[dict],
    numeric_sources: str,
) -> str | None:
    if not isinstance(data, dict) or set(data) != _ACTION_ROOT_FIELDS:
        return "根节点必须且只能包含 actions"
    forbidden = _forbidden_field_path(data)
    if forbidden:
        return f"行动建议包含禁止的原文字段：{forbidden}"
    actions = data.get("actions")
    if not isinstance(actions, list):
        return "actions 必须是数组"
    general_count = sum(item.get("action_scope") == "general" for item in actions if isinstance(item, dict))
    group_count = sum(item.get("action_scope") == "group" for item in actions if isinstance(item, dict))
    if general_count > INSIGHT_V3_MAX_GENERAL_ACTIONS:
        return f"普通行动建议不得超过 {INSIGHT_V3_MAX_GENERAL_ACTIONS} 条"
    if group_count > INSIGHT_V3_MAX_GROUP_ACTIONS:
        return f"人群专项建议不得超过 {INSIGHT_V3_MAX_GROUP_ACTIONS} 条"

    group_options, finding_groups = _action_group_bindings(chapters)
    seen_ids: set[str] = set()
    for index, action in enumerate(actions, 1):
        if not isinstance(action, dict) or set(action) != _ACTION_FIELDS:
            return f"actions[{index - 1}] 必须恰好包含内部 11 字段"
        expected_id = f"a{index:02d}"
        if action.get("id") != expected_id or expected_id in seen_ids:
            return f"行动建议 ID 必须从 a01 连续编号，当前应为 {expected_id}"
        seen_ids.add(expected_id)
        action_scope = action.get("action_scope")
        if action_scope not in {"general", "group"}:
            return f"actions[{index - 1}] 的 action_scope 无效"
        finding_ids = action.get("finding_ids")
        if not isinstance(finding_ids, list) or not 1 <= len(finding_ids) <= 3:
            return f"actions[{index - 1}] 必须绑定 1 至 3 个 finding_id"
        if len(finding_ids) != len(set(finding_ids)) or any(
            not isinstance(item, str) or item not in findings_by_id
            for item in finding_ids
        ):
            return f"actions[{index - 1}] 的 finding_ids 含重复或未知 ID"

        target_group = action.get("target_group")
        if action_scope == "general":
            if target_group is not None:
                return f"actions[{index - 1}] 的普通建议不得设置 target_group"
        else:
            if not isinstance(target_group, dict) or set(target_group) != _ACTION_GROUP_FIELDS:
                return f"actions[{index - 1}] 的人群建议缺少合法 target_group"
            target_key = (
                _clean(target_group.get("profile_name")),
                _clean(target_group.get("profile_value")),
            )
            if target_key not in group_options:
                return f"actions[{index - 1}] 指向未规划的人群专章"
            if not any(finding_groups.get(finding_id) == target_key for finding_id in finding_ids):
                return f"actions[{index - 1}] 未绑定对应人群专章的 finding"

        scalar_fields = (
            "title",
            "problem",
            "objective",
            "validation_plan",
            "evidence_gap",
            "required_data",
        )
        normalized_texts: list[str] = []
        for field in scalar_fields:
            value = action.get(field)
            if not isinstance(value, str):
                return f"actions[{index - 1}].{field} 必须是字符串"
            normalized, _terms = _normalize_action_text(value)
            if field in {"title", "problem", "objective", "validation_plan"} and not normalized:
                return f"actions[{index - 1}].{field} 不得为空"
            if "\n" in value or _MARKDOWN_HEADING_RE.search(value) or _MARKDOWN_TABLE_RE.search(value):
                return f"actions[{index - 1}].{field} 不得包含 Markdown 标题或表格"
            action[field] = normalized
            if normalized:
                normalized_texts.append(normalized)

        directions = action.get("design_directions")
        if not isinstance(directions, list) or not 1 <= len(directions) <= 3:
            return f"actions[{index - 1}].design_directions 必须包含 1 至 3 项"
        normalized_directions: list[str] = []
        for direction_index, direction in enumerate(directions):
            if not isinstance(direction, str):
                return f"actions[{index - 1}].design_directions[{direction_index}] 必须是字符串"
            normalized, _terms = _normalize_action_text(direction)
            if not normalized or _MARKDOWN_HEADING_RE.search(direction) or _MARKDOWN_TABLE_RE.search(direction):
                return f"actions[{index - 1}].design_directions[{direction_index}] 无效"
            normalized_directions.append(normalized)
            normalized_texts.append(normalized)
        action["design_directions"] = normalized_directions

        bound_findings = [findings_by_id[finding_id] for finding_id in finding_ids]
        needs_gap = any(
            finding.get("degraded") or finding.get("kind") == "uncertain"
            for finding in bound_findings
        )
        has_gap = bool(action["evidence_gap"])
        has_required_data = bool(action["required_data"])
        if has_gap != has_required_data:
            return f"actions[{index - 1}] 的 evidence_gap 与 required_data 必须同时填写或同时留空"
        if needs_gap and not has_gap:
            return f"actions[{index - 1}] 绑定不确定或降级 finding 时必须说明证据缺口与所需数据"

        combined = "\n".join(normalized_texts)
        if _frequency_claim_term(combined):
            return f"actions[{index - 1}] 不得自行写观点频次"
        profile_error = validate_profile_language(
            combined,
            findings=bound_findings,
            catalog=profiles,
        )
        if profile_error:
            return f"actions[{index - 1}]：{profile_error}"
        for number in _NUMBER_RE.findall(combined):
            if number not in numeric_sources:
                return f"actions[{index - 1}] 包含来源中不存在的数字：{number}"
    return None


async def write_actions(
    *,
    chapters: list[dict],
    core_blocks: list[dict],
    findings_by_id: dict[str, dict],
    profiles: list[dict],
    qualitative_context: dict | None,
    on_attempt_event=None,
) -> dict:
    covered_ids = list(dict.fromkeys(
        finding_id
        for collection in (
            *(chapter.get("blocks") or [] for chapter in chapters),
            core_blocks or [],
        )
        for block in collection
        for finding_id in block.get("finding_ids") or []
        if finding_id in findings_by_id
    ))
    if not covered_ids:
        return {
            "actions": [],
            "chapter_note": (
                "现有材料尚未形成可通过 finding 绑定校验的行动建议。"
                "如需形成方案，应补充明确的产品约束、目标用户和可验证的业务问题。"
            ),
            "diagnostics": {
                "status": "skipped",
                "logical_attempts": 0,
                "attempts": [],
                "models": [],
                "reason": "no_written_findings",
            },
        }

    compact_chapters = [
        {
            "id": chapter.get("id"),
            "title": chapter.get("title"),
            "purpose": chapter.get("purpose"),
            "group_focus": deepcopy(chapter.get("group_focus")),
            "finding_ids": list(dict.fromkeys(chapter.get("finding_ids") or [])),
            "blocks": [
                {
                    "heading": block.get("heading"),
                    "body": block.get("body"),
                    "finding_ids": block.get("finding_ids"),
                }
                for block in chapter.get("blocks") or []
            ],
        }
        for chapter in chapters
    ]
    compact_core = [
        {
            "heading": block.get("heading"),
            "body": block.get("body"),
            "finding_ids": block.get("finding_ids"),
        }
        for block in core_blocks or []
    ]
    finding_payload = [
        {
            "id": finding_id,
            "claim": _claim_without_frequency_prefix(findings_by_id[finding_id].get("claim")),
            "kind": findings_by_id[finding_id].get("kind"),
            "caveat": _clean(findings_by_id[finding_id].get("caveat")),
            "degraded": bool(findings_by_id[finding_id].get("degraded")),
            **writer_profile_payload(findings_by_id[finding_id]),
        }
        for finding_id in covered_ids
    ]
    contract = {"actions": [{
        "id": "a01",
        "title": "面向业务读者的建议标题",
        "action_scope": "general|group",
        "target_group": None,
        "problem": "该建议要处理的、已由 finding 支撑的问题",
        "objective": "希望改善的体验或决策目标",
        "design_directions": ["可考虑的设计方向，不写编号"],
        "validation_plan": "如何验证方向是否有效，不新增无来源数值目标",
        "finding_ids": [covered_ids[0]],
        "evidence_gap": "证据足够时留空；不足时说明缺口",
        "required_data": "无缺口时留空；有缺口时说明所需补充数据",
    }]}
    query = (
        "<contract>\n" + _safe_json(contract) + "\n</contract>\n"
        + "<limits>普通建议最多 " + str(INSIGHT_V3_MAX_GENERAL_ACTIONS)
        + " 条；人群专项建议最多 " + str(INSIGHT_V3_MAX_GROUP_ACTIONS)
        + " 条；允许 actions=[]，不得为凑数量提出无证据建议。</limits>\n"
        + "<business_context>\n" + _safe_json(qualitative_context or {}) + "\n</business_context>\n"
        + "<core_findings>\n" + _safe_json(compact_core) + "\n</core_findings>\n"
        + "<completed_chapters>\n" + _safe_json(compact_chapters) + "\n</completed_chapters>\n"
        + "<findings>\n" + _safe_json(finding_payload) + "\n</findings>"
    )
    numeric_sources = "\n".join((
        _safe_json(finding_payload),
        _safe_json(compact_core),
        _safe_json(compact_chapters),
    ))
    attempts: list[dict] = []
    models: list[str] = []

    async def _on_attempt(event: dict):
        await _forward_attempt_event(on_attempt_event, {
            **event,
            "phase": "writing",
            "step": "actions",
        })

    for logical_attempt in range(INSIGHT_V3_CHAPTER_MAX_RETRIES + 1):
        repair_errors: list[str] = []
        result = await _direct_json_call(
            DEFAULT_INSIGHT_V3_ACTION_WRITER_SYSTEM,
            query,
            models=tuple(model for model in (LLM_REPORT_MODEL, *LLM_REPORT_FALLBACK_MODELS) if model),
            max_tokens=LLM_REPORT_MAX_TOKENS,
            reasoning_effort=None,
            validator=lambda data: _validate_action_output(
                data,
                findings_by_id=findings_by_id,
                chapters=chapters,
                profiles=profiles,
                numeric_sources=numeric_sources,
            ),
            on_repair=repair_errors.append,
            on_attempt_event=_on_attempt,
        )
        if result.get("model"):
            models.append(result["model"])
        attempts.append({
            "attempt": logical_attempt + 1,
            "model": _clean(result.get("model")),
            "repaired": bool(result.get("repaired")),
            "repair_errors": repair_errors,
            "error": _clean(result.get("error"))[:300],
            "duration_seconds": float(result.get("duration_seconds") or 0.0),
        })
        if result.get("data") is None:
            continue
        actions = deepcopy(result["data"]["actions"])
        return {
            "actions": actions,
            "chapter_note": (
                "现有材料未形成可通过 finding 绑定校验的行动建议。"
                "如需形成方案，应补充明确的产品约束、目标用户和验证条件。"
                if not actions else ""
            ),
            "diagnostics": {
                "status": "complete",
                "logical_attempts": logical_attempt + 1,
                "attempts": attempts,
                "models": list(dict.fromkeys(models)),
                "reason": "no_grounded_actions" if not actions else "",
                "action_count": len(actions),
                "general_action_count": sum(item["action_scope"] == "general" for item in actions),
                "group_action_count": sum(item["action_scope"] == "group" for item in actions),
            },
        }

    return {
        "actions": [],
        "chapter_note": (
            "行动建议生成未通过证据绑定校验，因此未用合理化叙述补齐。"
            "如需形成方案，应补充明确的产品约束、目标用户和验证条件后重新生成。"
        ),
        "diagnostics": {
            "status": "degraded",
            "logical_attempts": len(attempts),
            "attempts": attempts,
            "models": list(dict.fromkeys(models)),
            "reason": "action_writer_retry_exhausted",
            "action_count": 0,
            "general_action_count": 0,
            "group_action_count": 0,
        },
    }
