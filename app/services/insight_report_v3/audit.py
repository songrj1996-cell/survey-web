"""Bounded semantic audit and non-blocking frequency discovery for Insight v3."""

from __future__ import annotations

from copy import deepcopy
import inspect
import json
import re
import time

from app.core.config import (
    DEFAULT_INSIGHT_V3_AUDIT_REPAIR_SYSTEM,
    DEFAULT_INSIGHT_V3_AUDIT_SYSTEM,
    INSIGHT_V3_AUDIT_BATCH_UNIT_LIMIT,
    INSIGHT_V3_AUDIT_CONCURRENCY,
    INSIGHT_V3_AUDIT_INPUT_CHARS,
    INSIGHT_V3_AUDIT_PROMPT_VERSION,
    INSIGHT_V3_AUDIT_REPAIR_PROMPT_VERSION,
    INSIGHT_V3_AUDIT_WARN_SECONDS,
    LLM_INSIGHT_V3_AUDIT_FALLBACK_MODELS,
    LLM_INSIGHT_V3_AUDIT_MAX_TOKENS,
    LLM_INSIGHT_V3_AUDIT_MODEL,
    LLM_INSIGHT_V3_AUDIT_REASONING,
    LLM_REPORT_FALLBACK_MODELS,
    LLM_REPORT_MAX_TOKENS,
    LLM_REPORT_MODEL,
)
from app.services.insight_report_v3.chapters import normalize_report_block_text, validate_action_repair
from app.services.insight_report_v3.contracts import (
    validate_audit_payload,
    validate_audit_repair_payload,
    ACTION_REPAIR_TEXT_FIELDS,
    SOURCE_RELATIONS,
)
from app.services.report_engine import _direct_json_call, _run_bounded_calls
from app.services.insight_report_v3.research_context import (
    expand_stat_references, bound_statistic_facts, model_statistic_facts, STAT_REF_RE,
)


AUDIT_SCHEMA_VERSION = "insight-audit-v4-clause-bound"
AUDIT_RULES_VERSION = "audit-rules-v7-clause-bound"
FREQUENCY_DISCOVERY_RULES_VERSION = "frequency-discovery-v1-review-only"

_REPAIR_CONTRACT = {
    "repairs": [{
        "claim_id": "c001",
        "heading": "修复后的短标题",
        "body": "只依据绑定证据修复后的中文正文",
    }]
}
_FREQUENCY_SUBJECT = r"(?:玩家|受访者|用户|反馈者|样本|人群|群体)"
_FREQUENCY_QUANTIFIER = (
    r"(?:绝大部分|绝大多数|大多数|多数|相当一部分|一大批|大批|众多|许多|"
    r"不少|很多|大量|少数|个别|几乎(?:所有|全部)|所有|全部|普遍|广泛|"
    r"高频|常见|主流|集中|一致|共同|往往|经常|频繁|均|都)"
)
_FREQUENCY_DISCOVERY_PATTERNS = (
    ("quantifier_before_subject", re.compile(
        rf"{_FREQUENCY_QUANTIFIER}[\u4e00-\u9fffA-Za-z0-9_\-、，, ]{{0,12}}{_FREQUENCY_SUBJECT}"
    )),
    ("subject_before_quantifier", re.compile(
        rf"{_FREQUENCY_SUBJECT}[\u4e00-\u9fffA-Za-z0-9_\-、，, ]{{0,12}}{_FREQUENCY_QUANTIFIER}"
    )),
    ("subject_proportion", re.compile(
        rf"{_FREQUENCY_SUBJECT}.{{0,12}}(?:占比|比例|比重|过半|超过半数|接近半数|为主)"
    )),
    ("collective_judgement", re.compile(
        rf"(?:一致|普遍|广泛|共同)(?:认为|表示|反馈|提到|提及).{{0,12}}{_FREQUENCY_SUBJECT}"
    )),
)
_SENTENCE_BREAK_RE = re.compile(r"(?<=[。！？!?；;])|(?<=[.])\s+|[\r\n]+")


def _clean(value) -> str:
    return str(value or "").strip()


def _safe_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def _sentence_targets(value) -> list[str]:
    """Split report prose into deterministic audit targets without changing it."""
    text = _clean(value)
    if not text:
        return []
    return [part.strip() for part in _SENTENCE_BREAK_RE.split(text) if part.strip()]


async def _forward_attempt(
    callback,
    event: dict,
    *,
    step: str,
    batch_index: int,
    local_events: list[dict],
    call_events: list[dict],
):
    annotated = {
        **deepcopy(event),
        "phase": "audit",
        "step": step,
        "batch_index": batch_index,
    }
    local_events.append(deepcopy(annotated))
    call_events.append(deepcopy(annotated))
    if callback is None:
        return
    result = callback(annotated)
    if inspect.isawaitable(result):
        await result


def _finding_summary(finding: dict) -> dict:
    return {
        "id": _clean(finding.get("id")),
        "claim": _clean(finding.get("claim")),
        "kind": _clean(finding.get("kind")),
        "caveat": _clean(finding.get("caveat")),
        "question": _clean(finding.get("question")),
        "preserve_reason": _clean(finding.get("preserve_reason")),
        "source_ids": list(dict.fromkeys(finding.get("response_ids") or [])),
    }


def _block_sources(block: dict, findings_by_id: dict[str, dict]) -> list[str]:
    return list(dict.fromkeys(
        response_id
        for finding_id in block.get("finding_ids") or []
        for response_id in (findings_by_id.get(finding_id) or {}).get("response_ids") or []
        if _clean(response_id)
    ))


def build_audit_units(
    *,
    core_blocks: list[dict],
    chapters: list[dict],
    findings_by_id: dict[str, dict],
    source_registry: dict[str, dict],
    actions: list[dict] | None = None,
    include_source_checks: bool = True,
) -> list[dict]:
    """Build code-owned short claim IDs without exposing global source IDs."""
    units: list[dict] = []
    sections = [
        ("core", -1, "核心发现", core_blocks or []),
        ("action", -1, "行动建议", [
            {**deepcopy(action), "heading": action.get("title"),
             "body": _safe_json({k: action.get(k) for k in sorted(ACTION_REPAIR_TEXT_FIELDS)}),
             "action_text": {k: deepcopy(action.get(k)) for k in sorted(ACTION_REPAIR_TEXT_FIELDS)}}
            for action in actions or []
        ]),
        *(
            (
                "chapter",
                chapter_index,
                _clean(chapter.get("title")) or f"分析主题 {chapter_index + 1}",
                chapter.get("blocks") or [],
            )
            for chapter_index, chapter in enumerate(chapters or [])
        ),
    ]
    for section_kind, chapter_index, section_title, blocks in sections:
        for block_index, block in enumerate(blocks):
            if _clean(block.get("id")) == "no-open-findings":
                continue
            finding_ids = [
                finding_id
                for finding_id in dict.fromkeys(block.get("finding_ids") or [])
                if finding_id in findings_by_id
            ]
            source_ids = [
                response_id
                for response_id in _block_sources(block, findings_by_id)
                if isinstance(source_registry.get(response_id), dict)
                and _clean(source_registry[response_id].get("text"))
            ]
            units.append({
                "claim_id": f"c{len(units) + 1:03d}",
                "kind": "action" if section_kind == "action" else "sample" if section_title == "样本概况" else "block",
                "section_kind": section_kind,
                "chapter_index": chapter_index,
                "block_index": block_index,
                "evidence_index": None,
                "section_title": section_title,
                "block_id": _clean(block.get("id")) or f"b{block_index + 1}",
                "heading": _clean(block.get("heading")),
                "body": _clean(block.get("body")),
                "summary": "",
                "finding_ids": finding_ids,
                "findings": [_finding_summary(findings_by_id[item]) for item in finding_ids],
                "source_ids": source_ids,
                "objective_facts": bound_statistic_facts(
                    block.get("objective_facts") or {},
                    STAT_REF_RE.findall(f"{block.get('heading', '')}\n{block.get('body', '')}"),
                ),
                "sample_stats": deepcopy(block.get("sample_stats") or []),
                **({"action_text": deepcopy(block["action_text"])} if section_kind == "action" else {}),
            })
            for evidence_index, evidence in enumerate(block.get("evidence") or []):
                summary = _clean(evidence.get("evidence_point"))
                response_id = _clean(evidence.get("response_id"))
                if not summary:
                    continue
                point_sources = [
                    response_id
                    for response_id in [response_id]
                    if isinstance(source_registry.get(response_id), dict)
                    and _clean(source_registry[response_id].get("text"))
                ]
                units.append({
                    "claim_id": f"c{len(units) + 1:03d}",
                    "kind": "evidence_point",
                    "section_kind": section_kind,
                    "chapter_index": chapter_index,
                    "block_index": block_index,
                    "evidence_index": evidence_index,
                    "section_title": section_title,
                    "block_id": _clean(block.get("id")) or f"b{block_index + 1}",
                    "heading": _clean(block.get("heading")),
                    "body": "",
                    "summary": summary,
                    "finding_ids": finding_ids,
                    "findings": [_finding_summary(findings_by_id[item]) for item in finding_ids],
                    "source_ids": point_sources,
                })
    if include_source_checks:
        consumers = {}
        for unit in units:
            for fid in unit["finding_ids"]:
                consumers.setdefault(fid, []).append(unit["claim_id"])
        for fid, consumer_ids in consumers.items():
            finding = findings_by_id[fid]
            expected = list(dict.fromkeys(finding.get("response_ids") or []))
            present = [sid for sid in expected if isinstance(source_registry.get(sid), dict)
                       and _clean(source_registry[sid].get("text"))]
            units.append({
                "claim_id": f"c{len(units) + 1:03d}", "kind": "finding_binding",
                "section_kind": "finding", "chapter_index": -1, "block_index": -1,
                "evidence_index": None, "section_title": "来源关系核对", "block_id": fid,
                "heading": _clean(finding.get("claim")), "body": "", "summary": "",
                "finding_ids": [fid], "findings": [_finding_summary(finding)],
                "source_ids": present, "missing_source_ids": [sid for sid in expected if sid not in present],
                "consumer_ids": consumer_ids,
            })
    return units


def _audit_targets(unit: dict) -> list[dict]:
    """Code-owned field references; raw sources and reference context are never targets."""
    kind = unit["kind"]
    if kind == "finding_binding":
        finding = unit["findings"][0]
        fields = [
            (key, _clean(finding.get(key)), f"finding_{key}")
            for key in ("claim", "caveat")
        ]
    elif kind == "evidence_point":
        fields = [("evidence_point", unit["summary"], "evidence_summary")]
    elif kind == "action":
        fields = []
        for key in sorted(ACTION_REPAIR_TEXT_FIELDS):
            value = (unit.get("action_text") or {}).get(key, "")
            if key == "design_directions":
                fields.extend(
                    (f"action_text.{key}[{index}]", text, "action_design_direction")
                    for index, text in enumerate(value or [])
                )
            else:
                fields.append((f"action_text.{key}", value, "action_field"))
    else:
        facts = unit.get("objective_facts") or {}
        body_field = "report_scope" if kind == "research_review" else "body"
        body_role = (
            "research_scope_sentence" if kind == "research_review"
            else "sample_interpretation_sentence" if kind == "sample"
            else "report_sentence"
        )
        fields = [("heading", expand_stat_references(unit["heading"], facts), "report_heading")]
        rendered_body = expand_stat_references(unit["body"], facts)
        sentences = [rendered_body] if kind == "research_review" else _sentence_targets(rendered_body)
        fields.extend(
            (
                body_field if len(sentences) == 1 else f"{body_field}.sentence[{index}]",
                sentence,
                body_role,
            )
            for index, sentence in enumerate(sentences, 1)
        )
    targets = []
    for index, (field, text, content_role) in enumerate(fields):
        suffix = f"t{index + 1:02d}"
        if kind == "action":
            # Adding/removing design directions during a repair must not move
            # the IDs of other fields in diagnostics or the next audit call.
            name = field.removeprefix("action_text.")
            if name.startswith("design_directions["):
                suffix = f"d{int(name.split('[')[1][:-1]) + 1:02d}"
            else:
                suffix = f"t{sorted(ACTION_REPAIR_TEXT_FIELDS).index(name) + 1:02d}"
        targets.append({
            "target_id": f"{unit['claim_id']}-{suffix}",
            "field": field,
            "content_role": content_role,
            "text": text,
        })
    return targets


def _expected_issue_targets(units: list[dict]) -> dict[str, set[str]]:
    return {unit["claim_id"]: {t["target_id"] for t in _audit_targets(unit)}
            for unit in units if unit["kind"] != "finding_binding"}


def _model_payload(units: list[dict], source_registry: dict[str, dict], *, purpose: str = "audit") -> dict:
    if purpose not in {"audit", "repair"}:
        raise ValueError("Unknown audit payload purpose")
    ordered_source_ids = list(dict.fromkeys(
        response_id for unit in units for response_id in unit.get("source_ids") or []
    ))
    source_short = {
        response_id: f"r{index + 1:03d}"
        for index, response_id in enumerate(ordered_source_ids)
    }
    player_keys = {
        source_id: (source_id if source_registry[source_id].get("case_link_status") == "ambiguous_rows"
                    else source_registry[source_id].get("respondent_ref") or source_id)
        for source_id in ordered_source_ids
    }
    players = list(dict.fromkeys(player_keys.values()))
    model_units = []
    for unit in units:
        item = {
            "claim_id": unit["claim_id"],
            "kind": unit["kind"],
            "section": unit["section_title"],
            "source_ids": [source_short[item] for item in unit.get("source_ids") or []],
        }
        findings = [
            {**{k: deepcopy(v) for k, v in finding.items() if k != "source_ids"},
             "source_ids": [source_short[sid] for sid in finding.get("source_ids", []) if sid in source_short]}
            for finding in unit["findings"]
        ]
        if purpose == "audit":
            item["targets"] = _audit_targets(unit)
            context = {}
            if unit["kind"] == "finding_binding":
                context = {key: deepcopy(findings[0].get(key)) for key in ("question", "kind", "preserve_reason")}
            elif unit["kind"] == "research_review":
                # These are coverage hints, not established facts or report text.
                context["unverified_candidate_findings"] = findings
            else:
                # Keep the question needed to understand short replies, without
                # repeating the model's candidate claim as if it were body text.
                question_groups = []
                for finding in findings:
                    group = {"question": finding.get("question", ""),
                             "source_ids": [sid for sid in finding["source_ids"] if sid in item["source_ids"]]}
                    if group["question"] and group not in question_groups:
                        question_groups.append(group)
                if question_groups:
                    context["source_questions"] = question_groups
            if unit.get("objective_facts"):
                context["objective_stats"] = model_statistic_facts(unit["objective_facts"])
            for key in ("sample_stats", "research_context"):
                if unit.get(key):
                    context[key] = deepcopy(unit[key])
            item["reference_context"] = context
            model_units.append(item)
            continue
        # Repair retains its separate text-editing contract, not the audit layout.
        item["findings"] = findings
        if unit["kind"] in {"block", "sample", "research_review", "action", "finding_binding"}:
            item.update({"heading": unit["heading"], "body": unit["body"]})
            item["objective_stats"] = model_statistic_facts(unit.get("objective_facts") or {})
            item["rendered_body"] = expand_stat_references(unit["body"], unit.get("objective_facts") or {})
            if unit.get("sample_stats"):
                item["sample_stats"] = deepcopy(unit["sample_stats"])
            if unit.get("research_context"):
                item["research_context"] = deepcopy(unit["research_context"])
            if unit.get("action_text"):
                item["action_text"] = deepcopy(unit["action_text"])
            if unit.get("source_checks"):
                item["source_checks"] = deepcopy(unit["source_checks"])
        else:
            item.update({"evidence_point": unit["summary"]})
        model_units.append(item)
    return {
        "units": model_units,
        "sources": [
            {
                "source_id": source_short[response_id],
                "role": "original_response",
                "player_id": f"p{players.index(player_keys[response_id]) + 1:03d}",
                "text": _clean(source_registry[response_id].get("text")),
                "case_context": deepcopy(source_registry[response_id].get("case_context") or []),
            }
            for response_id in ordered_source_ids
        ],
    }


def _audit_query(units: list[dict], source_registry: dict[str, dict]) -> str:
    payload = _model_payload(units, source_registry)
    body_units = [u for u in payload["units"] if u["kind"] != "finding_binding"]
    source_units = [u for u in payload["units"] if u["kind"] == "finding_binding"]
    contract = {
        "checked_claim_ids": [u["claim_id"] for u in payload["units"]],
        "finding_checks": [{
            "claim_id": source_units[0]["claim_id"], "status": "supported|needs_review|inconclusive",
            "relations": {key: [] for key in SOURCE_RELATIONS},
            "problem": "通过时可为空，否则简述归纳与来源的支持方向或条件问题",
        }] if source_units else [],
        "issues": [{
            "claim_id": body_units[0]["claim_id"],
            "target_ids": [body_units[0]["targets"][0]["target_id"]],
            "problem_type": (
                "contradiction|unsupported_detail|unsupported_causality|scope_shift|"
                "polarity_shift|condition_shift|mistranslation|missing_requirement|"
                "missing_counterexample|shallow_analysis|statistic_scope|other"
            ),
            "problem": "仅概括所选 targets 的问题，不把参考材料当作正文",
            "severity": "warning|blocking",
        }] if body_units else [],
    }
    scope = {
        "issue_targets": {u["claim_id"]: [t["target_id"] for t in u["targets"]] for u in body_units},
        "finding_check_claim_ids": [u["claim_id"] for u in source_units],
        "rules": "只审 targets，并按 content_role 区分正文句、建议字段、候选归纳与参考材料。issues 无问题可为空；finding_checks 必须恰好覆盖列明编号，无编号必须为空。每个 claim_id 最多一条 issues，可绑定多个字段。示例不是问题判定。",
    }
    return (
        "<contract>\n" + _safe_json(contract) + "\n</contract>\n"
        "<audit_scope>\n" + _safe_json(scope) + "\n</audit_scope>\n"
        "<audit_input>\n" + _safe_json(payload) + "\n</audit_input>"
    )


def _repair_query(
    units: list[dict],
    source_registry: dict[str, dict],
    issues_by_id: dict[str, dict],
) -> str:
    payload = _model_payload(units, source_registry, purpose="repair")
    target_fields = {u["claim_id"]: {t["target_id"]: t["field"] for t in _audit_targets(u)} for u in units}
    for unit in payload["units"]:
        issue = issues_by_id[unit["claim_id"]]
        unit["audit_issue"] = {
            "problem_type": issue["problem_type"],
            "problem": issue["problem"],
            "severity": issue["severity"],
            "target_fields": {tid: target_fields[unit["claim_id"]][tid]
                              for tid in issue.get("target_ids") or []},
        }
    contract = {"repairs": [
        ({"claim_id": unit["claim_id"], "action_text": unit["action_text"]}
         if unit["kind"] == "action" else {"claim_id": unit["claim_id"], "heading": "修复后标题", "body": "修复后正文"})
        for unit in units
    ]}
    return (
        "<contract>\n" + _safe_json(contract) + "\n</contract>\n"
        "<repair_input>\n" + _safe_json(payload) + "\n</repair_input>"
    )


def _pack_units(
    units: list[dict],
    *,
    source_registry: dict[str, dict],
    query_builder,
) -> tuple[list[list[dict]], list[dict]]:
    batches: list[list[dict]] = []
    over_target: list[dict] = []
    current: list[dict] = []
    for unit in units:
        if len(query_builder([unit], source_registry)) > INSIGHT_V3_AUDIT_INPUT_CHARS:
            if current:
                batches.append(current)
                current = []
            # The configured character limit is a packing target, not a reason
            # to skip evidence. A single oversized claim keeps every bound
            # source and is attempted in its own batch; a real upstream failure
            # remains explicit as incomplete.
            batches.append([unit])
            over_target.append(unit)
            continue
        candidate = [*current, unit]
        if current and (
            len(current) >= INSIGHT_V3_AUDIT_BATCH_UNIT_LIMIT
            or len(query_builder(candidate, source_registry)) > INSIGHT_V3_AUDIT_INPUT_CHARS
        ):
            batches.append(current)
            current = [unit]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches, over_target


def _block_for_unit(core_blocks: list[dict], chapters: list[dict], unit: dict) -> dict:
    if unit["section_kind"] == "core":
        return core_blocks[unit["block_index"]]
    return chapters[unit["chapter_index"]]["blocks"][unit["block_index"]]


def _delivery_issue(unit: dict, issue: dict, *, reason: str) -> dict:
    target = {"evidence_point": "证据要点", "sample": "样本解释", "research_review": "研究任务覆盖", "action": "建议事实依据", "finding_binding": "来源关系"}.get(unit.get("kind"), "正文结论")
    return {
        "claim_id": unit.get("claim_id"),
        "section": unit.get("section_title"),
        "heading": unit.get("heading"),
        "target": target,
        "problem_type": issue.get("problem_type") or "other",
        "problem": issue.get("problem") or "该项未完成证据审校",
        "severity": issue.get("severity") or "blocking",
        "reason": reason,
        "audit_target_fields": {
            t["target_id"]: t["field"] for t in _audit_targets(unit)
            if t["target_id"] in (issue.get("target_ids") or [])
        },
    }


def _attempt_failure_counts(events: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        if event.get("status") != "failed":
            continue
        category = _clean(event.get("error_category") or event.get("error_type") or "unknown")
        counts[category] = counts.get(category, 0) + 1
    return counts


def _call_diagnostic(
    *,
    step: str,
    batch_index: int,
    query: str,
    units: list[dict],
    result: dict,
    attempt_events: list[dict],
) -> dict:
    return {
        "step": step,
        "batch_index": batch_index,
        "unit_count": len(units),
        "target_fields": {u["claim_id"]: {t["target_id"]: t["field"] for t in _audit_targets(u)} for u in units},
        "input_chars": len(query),
        "input_target_exceeded": len(query) > INSIGHT_V3_AUDIT_INPUT_CHARS,
        "model": _clean(result.get("model")),
        "schema_repaired": bool(result.get("repaired")),
        "error": _clean(result.get("error"))[:300],
        "duration_seconds": float(result.get("duration_seconds") or 0.0),
        "upstream_attempt_count": sum(
            event.get("status") == "started" for event in attempt_events
        ),
        "attempt_failures": _attempt_failure_counts(attempt_events),
        "max_output_tokens": max(
            (
                int(event.get("max_output_tokens") or 0)
                for event in attempt_events
                if event.get("status") == "started"
            ),
            default=0,
        ),
    }


async def _run_bounded_factories(call_factories: list) -> tuple[list, int]:
    """Run every factory with the configured bound and preserve input order."""
    active = 0
    peak = 0
    ordered: dict[int, object] = {}

    async def tracked(factory):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            return await factory()
        finally:
            active -= 1

    tracked_factories = [
        (lambda factory=factory: tracked(factory))
        for factory in call_factories
    ]
    async for event_type, payload in _run_bounded_calls(
        tracked_factories,
        INSIGHT_V3_AUDIT_CONCURRENCY,
    ):
        if event_type == "result":
            index, value = payload
            ordered[index] = value
    return [ordered[index] for index in range(len(call_factories))], peak


async def audit_report_sections(
    *,
    core_blocks: list[dict],
    chapters: list[dict],
    findings_by_id: dict[str, dict],
    source_registry: dict[str, dict],
    profiles: list[dict],
    on_attempt_event=None,
    sample_overview: dict | None = None,
    research_tasks: list[dict] | None = None,
    analysis_focus: dict | None = None,
    actions: list[dict] | None = None,
) -> dict:
    """Audit every model-written claim with bounded concurrency and one repair round."""
    started = time.monotonic()
    audited_core = deepcopy(core_blocks or [])
    audited_chapters = deepcopy(chapters or [])
    audited_actions = deepcopy(actions or [])
    sample_chapter_index = len(audited_chapters)
    if sample_overview:
        audited_chapters.append({"id": "sample", "title": "样本概况", "blocks": [{
            **deepcopy(sample_overview), "id": "sample-overview", "finding_ids": [], "evidence": [],
        }]})
    units = build_audit_units(
        core_blocks=audited_core,
        chapters=audited_chapters,
        findings_by_id=findings_by_id,
        source_registry=source_registry,
        actions=audited_actions,
    )
    if research_tasks is not None or analysis_focus is not None:
        units.append({
            "claim_id": f"c{len(units) + 1:03d}", "kind": "research_review",
            "section_kind": "research", "chapter_index": -1, "block_index": -1,
            "evidence_index": None, "section_title": "研究任务覆盖", "block_id": "research-review",
            "heading": "已确认研究任务、重要材料与解释深度", "summary": "",
            "body": _safe_json({
                "core": [{"heading": b.get("heading"), "body": b.get("body")} for b in audited_core],
                "chapters": [{"title": c.get("title"), "purpose": c.get("purpose"), "blocks": [
                    {"heading": b.get("heading"), "body": b.get("body"), "finding_ids": b.get("finding_ids")}
                    for b in c.get("blocks") or []
                ]} for c in chapters or []],
            }),
            "findings": [_finding_summary(f) for f in findings_by_id.values()],
            "finding_ids": [], "source_ids": [],
            "objective_facts": {
                key: value for section in [audited_core, *(c.get("blocks") or [] for c in audited_chapters)]
                for block in section for key, value in (block.get("objective_facts") or {}).items()
            },
            "research_context": {"analysis_focus": deepcopy(analysis_focus or {}), "task_assignments": deepcopy(research_tasks or [])},
        })
    unit_by_id = {unit["claim_id"]: unit for unit in units}
    local_attempt_events: list[dict] = []
    calls: list[dict] = []
    unresolved: list[dict] = []
    resolved: list[dict] = []
    frequency_rewrites: list[dict] = []
    unit_statuses = {
        unit["claim_id"]: {
            "claim_id": unit["claim_id"],
            "kind": unit["kind"],
            "section": unit["section_title"],
            "status": "pending",
            "reason": "",
        }
        for unit in units
    }

    def set_unit_status(unit: dict, status: str, reason: str) -> None:
        item = unit_statuses[unit["claim_id"]]
        item["status"] = status
        item["reason"] = reason

    def add_unresolved(
        unit: dict,
        issue: dict,
        *,
        reason: str,
        status: str,
    ) -> None:
        unresolved.append(_delivery_issue(unit, issue, reason=reason))
        set_unit_status(unit, status, reason)

    missing_evidence = [unit for unit in units if unit.get("missing_source_ids") or (not unit.get("source_ids") and unit["kind"] not in {"sample", "research_review"})]
    audit_candidates = [unit for unit in units if unit not in missing_evidence]

    for unit in missing_evidence:
        add_unresolved(
            unit,
            {
                "problem_type": "unsupported_detail",
                "problem": "该项没有可供语义核查的绑定原文",
                "severity": "blocking",
            },
            reason="missing_bound_source",
            status="incomplete",
        )

    if not units:
        elapsed = time.monotonic() - started
        return {
            "core_blocks": audited_core,
            "chapters": audited_chapters,
            "actions": audited_actions,
            "unresolved": [],
            "diagnostics": {
                "schema_version": AUDIT_SCHEMA_VERSION,
                "prompt_version": INSIGHT_V3_AUDIT_PROMPT_VERSION,
                "repair_prompt_version": INSIGHT_V3_AUDIT_REPAIR_PROMPT_VERSION,
                "rules_version": AUDIT_RULES_VERSION,
                "status": "skipped",
                "stop_reason": "no_auditable_units",
                "planned_unit_count": 0,
                "audited_unit_count": 0,
                "initial_coverage_rate": 1.0,
                "unit_status_counts": {
                    "passed": 0,
                    "evidence_fallback": 0,
                    "repaired": 0,
                    "needs_review": 0,
                    "incomplete": 0,
                },
                "unit_statuses": [],
                "unresolved_count": 0,
                "initial_batch_count": 0,
                "repair_batch_count": 0,
                "reaudit_batch_count": 0,
                "concurrency": INSIGHT_V3_AUDIT_CONCURRENCY,
                "peak_concurrency": 0,
                "logical_call_count": 0,
                "upstream_attempt_count": 0,
                "calls": [],
                "models": [],
                "elapsed_seconds": round(elapsed, 3),
                "warned": elapsed > INSIGHT_V3_AUDIT_WARN_SECONDS,
            },
        }

    batches, over_target_units = _pack_units(
        audit_candidates,
        source_registry=source_registry,
        query_builder=_audit_query,
    )

    audit_models = tuple(dict.fromkeys(
        model for model in (
            LLM_INSIGHT_V3_AUDIT_MODEL,
            *LLM_INSIGHT_V3_AUDIT_FALLBACK_MODELS,
        ) if _clean(model)
    ))

    async def run_audit_batch(batch_index: int, batch: list[dict], *, step: str):
        query = _audit_query(batch, source_registry)
        expected = {u["claim_id"]: set(u["source_ids"]) for u in _model_payload(batch, source_registry)["units"] if u["kind"] == "finding_binding"}
        call_events: list[dict] = []

        async def attempt(event: dict):
            await _forward_attempt(
                on_attempt_event,
                event,
                step=step,
                batch_index=batch_index,
                local_events=local_attempt_events,
                call_events=call_events,
            )

        if not audit_models:
            result = {
                "data": None,
                "model": "",
                "repaired": False,
                "error": "audit_model_not_configured",
                "duration_seconds": 0.0,
            }
        else:
            result = await _direct_json_call(
                DEFAULT_INSIGHT_V3_AUDIT_SYSTEM,
                query,
                models=audit_models,
                max_tokens=LLM_INSIGHT_V3_AUDIT_MAX_TOKENS,
                reasoning_effort=LLM_INSIGHT_V3_AUDIT_REASONING,
                validator=lambda data: validate_audit_payload(
                    data,
                    valid_claim_ids={unit["claim_id"] for unit in batch},
                    expected_source_ids_by_claim=expected,
                    expected_targets_by_claim=_expected_issue_targets(batch),
                ),
                on_attempt_event=attempt,
            )
        diagnostic = _call_diagnostic(
            step=step,
            batch_index=batch_index,
            query=query,
            units=batch,
            result=result,
            attempt_events=call_events,
        )
        return batch, result, diagnostic

    initial_factories = [
        (
            lambda index=index, batch=batch: run_audit_batch(
                index + 1,
                batch,
                step="initial_audit",
            )
        )
        for index, batch in enumerate(batches)
    ]
    initial_results, initial_peak = (
        await _run_bounded_factories(initial_factories)
        if initial_factories else ([], 0)
    )
    calls.extend(item[2] for item in initial_results)

    audited_ids: set[str] = set()
    initial_issues: list[dict] = []
    source_checks: dict[str, dict] = {}
    for batch, result, _diagnostic in initial_results:
        if result.get("data") is None:
            for unit in batch:
                add_unresolved(
                    unit,
                    {
                        "problem_type": "other",
                        "problem": "该项语义审校调用未完成，不能视为已通过",
                        "severity": "blocking",
                    },
                    reason="initial_audit_failed",
                    status="incomplete",
                )
            continue
        audited_ids.update(unit["claim_id"] for unit in batch)
        for unit in batch:
            set_unit_status(unit, "passed", "initial_audit_passed")
        initial_issues.extend(deepcopy(result["data"].get("issues") or []))
        payload = _model_payload(batch, source_registry)
        source_order = list(dict.fromkeys(sid for unit in batch for sid in unit.get("source_ids") or []))
        reverse_sources = {row["source_id"]: sid for row, sid in zip(payload["sources"], source_order)}
        for check in result["data"].get("finding_checks") or []:
            unit = unit_by_id[check["claim_id"]]
            source_checks[unit["finding_ids"][0]] = {
                **deepcopy(check), "consumer_ids": unit["consumer_ids"],
                "consumer_locations": [
                    {"claim_id": cid, "section": unit_by_id[cid]["section_title"], "block_id": unit_by_id[cid]["block_id"], "heading": unit_by_id[cid]["heading"]}
                    for cid in unit["consumer_ids"]
                ],
                "relations": {key: [reverse_sources[sid] for sid in ids] for key, ids in check["relations"].items()},
            }

    issues_by_id = {issue["claim_id"]: issue for issue in initial_issues}
    for unit in units:
        if unit["kind"] != "finding_binding":
            continue
        fid = unit["finding_ids"][0]
        check = source_checks.get(fid)
        if not check:
            source_checks[fid] = {
                "claim_id": unit["claim_id"], "status": "incomplete", "relations": {},
                "problem": "来源关系核对未完成", "consumer_ids": unit["consumer_ids"],
                "missing_source_ids": unit.get("missing_source_ids") or [],
                "consumer_locations": [
                    {"claim_id": cid, "section": unit_by_id[cid]["section_title"], "block_id": unit_by_id[cid]["block_id"], "heading": unit_by_id[cid]["heading"]}
                    for cid in unit["consumer_ids"]
                ],
            }
        if check and check["status"] == "supported":
            continue
        reason = check["problem"] if check else "该发现的来源关系核对未完成，不能视为已通过"
        if check:
            set_unit_status(unit, "needs_review", "source_relation_issue")
        for cid in unit["consumer_ids"]:
            consumer = unit_by_id[cid]
            consumer.setdefault("source_checks", []).append({"finding_id": fid, "status": check["status"] if check else "incomplete", "problem": reason})
            issue = {"claim_id": cid, "problem_type": "contradiction", "problem": reason, "severity": "blocking"}
            if not check:
                if unit_statuses[cid]["status"] != "incomplete":
                    add_unresolved(consumer, issue, reason="source_check_incomplete", status="incomplete")
                issues_by_id.pop(cid, None)
            elif unit_statuses[cid]["status"] != "incomplete":
                # Preserve both the paragraph finding and all source-check reasons.
                if cid in issues_by_id:
                    issue["problem"] = issues_by_id[cid]["problem"] + "；来源核对：" + reason
                    if "target_ids" in issues_by_id[cid]:
                        issue["target_ids"] = deepcopy(issues_by_id[cid]["target_ids"])
                issues_by_id[cid] = issue
    body_issues: list[dict] = []
    evidence_fallback_count = 0
    for claim_id, issue in issues_by_id.items():
        unit = unit_by_id[claim_id]
        if unit_statuses[claim_id]["status"] == "incomplete":
            continue
        if unit["kind"] == "evidence_point":
            block = _block_for_unit(audited_core, audited_chapters, unit)
            evidence = (block.get("evidence") or [])[unit["evidence_index"]]
            evidence["evidence_point"] = ""
            evidence_fallback_count += 1
            resolved.append({
                **_delivery_issue(unit, issue, reason="evidence_point_fallback"),
                "resolution": "evidence_point_fallback",
            })
            set_unit_status(unit, "evidence_fallback", "evidence_point_fallback")
        elif unit["kind"] == "research_review":
            # A report-level omission cannot be repaired by inventing new bindings.
            add_unresolved(unit, issue, reason="research_task_needs_review", status="needs_review")
        else:
            body_issues.append(unit)
            set_unit_status(unit, "needs_review", "audit_issue_detected")

    repair_batches: list[list[dict]] = []
    repair_over_target: list[dict] = []
    if body_issues:
        repair_builder = lambda batch, registry: _repair_query(batch, registry, issues_by_id)
        repair_batches, repair_over_target = _pack_units(
            body_issues,
            source_registry=source_registry,
            query_builder=repair_builder,
        )

    valid_repairs: list[dict] = []
    repair_models = tuple(dict.fromkeys(
        model for model in (LLM_REPORT_MODEL, *LLM_REPORT_FALLBACK_MODELS) if _clean(model)
    )) or audit_models

    async def run_repair_batch(batch_index: int, batch: list[dict]):
        repair_query = _repair_query(batch, source_registry, issues_by_id)
        call_events: list[dict] = []

        async def repair_attempt(event: dict):
            await _forward_attempt(
                on_attempt_event,
                event,
                step="targeted_repair",
                batch_index=batch_index,
                local_events=local_attempt_events,
                call_events=call_events,
            )

        if not repair_models:
            repair_result = {
                "data": None,
                "model": "",
                "repaired": False,
                "error": "repair_model_not_configured",
                "duration_seconds": 0.0,
            }
        else:
            repair_result = await _direct_json_call(
                DEFAULT_INSIGHT_V3_AUDIT_REPAIR_SYSTEM,
                repair_query,
                models=repair_models,
                max_tokens=LLM_REPORT_MAX_TOKENS,
                reasoning_effort=None,
                validator=lambda data: validate_audit_repair_payload(
                    data,
                    expected_claim_ids={unit["claim_id"] for unit in batch},
                    action_claim_ids={unit["claim_id"] for unit in batch if unit["kind"] == "action"},
                ),
                on_attempt_event=repair_attempt,
            )
        diagnostic = _call_diagnostic(
            step="targeted_repair",
            batch_index=batch_index,
            query=repair_query,
            units=batch,
            result=repair_result,
            attempt_events=call_events,
        )
        return batch, repair_result, diagnostic

    repair_factories = [
        (
            lambda index=index, batch=batch: run_repair_batch(index + 1, batch)
        )
        for index, batch in enumerate(repair_batches)
    ]
    repair_results, repair_peak = (
        await _run_bounded_factories(repair_factories)
        if repair_factories else ([], 0)
    )
    calls.extend(item[2] for item in repair_results)

    for repair_candidates, repair_result, _diagnostic in repair_results:
        if repair_result.get("data") is None:
            for unit in repair_candidates:
                add_unresolved(
                    unit,
                    issues_by_id[unit["claim_id"]],
                    reason="repair_call_failed",
                    status="needs_review",
                )
        else:
            repairs_by_id = {
                repair["claim_id"]: repair
                for repair in repair_result["data"].get("repairs") or []
            }
            for unit in repair_candidates:
                repair = repairs_by_id[unit["claim_id"]]
                if unit["kind"] == "action":
                    old_action = audited_actions[unit["block_index"]]
                    numeric_sources = _safe_json([unit["findings"], old_action, [source_registry[sid]["text"] for sid in unit["source_ids"]]])
                    normalized_action, error = validate_action_repair(
                        original=old_action, action_text=repair["action_text"],
                        findings_by_id=findings_by_id, chapters=audited_chapters[:sample_chapter_index],
                        profiles=profiles, numeric_sources=numeric_sources,
                    )
                    if error or normalized_action == old_action:
                        add_unresolved(unit, {**issues_by_id[unit["claim_id"]], "problem": issues_by_id[unit["claim_id"]]["problem"] + "；" + (error or "修复无进展")}, reason="repair_validation_failed" if error else "repair_no_progress", status="needs_review")
                        continue
                    audited_actions[unit["block_index"]] = normalized_action
                    updated_unit = deepcopy(unit)
                    updated_unit["heading"] = normalized_action["title"]
                    updated_unit["action_text"] = {k: normalized_action[k] for k in sorted(ACTION_REPAIR_TEXT_FIELDS)}
                    updated_unit["body"] = _safe_json(updated_unit["action_text"])
                    valid_repairs.append(updated_unit)
                    continue
                numeric_sources = "\n".join([
                    unit["heading"],
                    unit["body"],
                    *(
                        "\n".join(_clean(finding.get(field)) for field in ("claim", "caveat", "question"))
                        for finding in unit["findings"]
                    ),
                    *(
                        _clean(source_registry[source_id].get("text"))
                        for source_id in unit.get("source_ids") or []
                    ),
                ])
                normalized, validation_error = normalize_report_block_text(
                    heading=repair.get("heading"),
                    body=repair.get("body"),
                    findings=[findings_by_id[item] for item in unit["finding_ids"]],
                    profiles=profiles,
                    numeric_sources=numeric_sources,
                    stat_facts=unit.get("objective_facts") or {},
                )
                if unit["kind"] == "sample" and normalized and len(expand_stat_references(normalized["body"], unit.get("objective_facts") or {})) > 500:
                    validation_error = "样本解读超过500字"
                if validation_error:
                    add_unresolved(
                        unit,
                        {
                            **issues_by_id[unit["claim_id"]],
                            "problem": (
                                issues_by_id[unit["claim_id"]]["problem"]
                                + f"；定向修复未通过代码门禁：{validation_error}"
                            ),
                        },
                        reason="repair_validation_failed",
                        status="needs_review",
                    )
                    continue
                if (
                    normalized["heading"] == unit["heading"]
                    and normalized["body"] == unit["body"]
                ):
                    add_unresolved(
                        unit,
                        issues_by_id[unit["claim_id"]],
                        reason="repair_no_progress",
                        status="needs_review",
                    )
                    continue
                block = _block_for_unit(audited_core, audited_chapters, unit)
                block["heading"] = normalized["heading"]
                block["body"] = normalized["body"]
                block["objective_facts"] = bound_statistic_facts(
                    unit.get("objective_facts") or {},
                    STAT_REF_RE.findall(normalized["heading"] + "\n" + normalized["body"]),
                )
                if normalized["frequency_terms"]:
                    frequency_rewrites.append({
                        "claim_id": unit["claim_id"],
                        "block_id": unit["block_id"],
                        "terms": normalized["frequency_terms"],
                    })
                updated_unit = deepcopy(unit)
                updated_unit["heading"] = normalized["heading"]
                updated_unit["body"] = normalized["body"]
                updated_unit["objective_facts"] = deepcopy(block["objective_facts"])
                valid_repairs.append(updated_unit)

    reaudit_batches, reaudit_over_target = _pack_units(
        valid_repairs,
        source_registry=source_registry,
        query_builder=_audit_query,
    ) if valid_repairs else ([], [])

    async def run_reaudit_batch(batch_index: int, batch: list[dict]):
        reaudit_query = _audit_query(batch, source_registry)
        call_events: list[dict] = []

        async def reaudit_attempt(event: dict):
            await _forward_attempt(
                on_attempt_event,
                event,
                step="repair_reaudit",
                batch_index=batch_index,
                local_events=local_attempt_events,
                call_events=call_events,
            )

        if not audit_models:
            reaudit_result = {
                "data": None,
                "model": "",
                "repaired": False,
                "error": "audit_model_not_configured",
                "duration_seconds": 0.0,
            }
        else:
            reaudit_result = await _direct_json_call(
                DEFAULT_INSIGHT_V3_AUDIT_SYSTEM,
                reaudit_query,
                models=audit_models,
                max_tokens=LLM_INSIGHT_V3_AUDIT_MAX_TOKENS,
                reasoning_effort=LLM_INSIGHT_V3_AUDIT_REASONING,
                validator=lambda data: validate_audit_payload(
                    data,
                    valid_claim_ids={unit["claim_id"] for unit in batch},
                    expected_source_ids_by_claim={},
                    expected_targets_by_claim=_expected_issue_targets(batch),
                ),
                on_attempt_event=reaudit_attempt,
            )
        diagnostic = _call_diagnostic(
            step="repair_reaudit",
            batch_index=batch_index,
            query=reaudit_query,
            units=batch,
            result=reaudit_result,
            attempt_events=call_events,
        )
        return batch, reaudit_result, diagnostic

    reaudit_factories = [
        (
            lambda index=index, batch=batch: run_reaudit_batch(index + 1, batch)
        )
        for index, batch in enumerate(reaudit_batches)
    ]
    reaudit_results, reaudit_peak = (
        await _run_bounded_factories(reaudit_factories)
        if reaudit_factories else ([], 0)
    )
    calls.extend(item[2] for item in reaudit_results)

    for reaudit_candidates, reaudit_result, _diagnostic in reaudit_results:
        if reaudit_result.get("data") is None:
            for unit in reaudit_candidates:
                add_unresolved(
                    unit,
                    issues_by_id[unit["claim_id"]],
                    reason="reaudit_failed",
                    status="incomplete",
                )
        else:
            remaining = {
                issue["claim_id"]: issue
                for issue in reaudit_result["data"].get("issues") or []
            }
            for unit in reaudit_candidates:
                if unit["claim_id"] in remaining:
                    add_unresolved(
                        unit,
                        remaining[unit["claim_id"]],
                        reason="unresolved_after_repair",
                        status="needs_review",
                    )
                else:
                    resolved.append({
                        **_delivery_issue(
                            unit,
                            issues_by_id[unit["claim_id"]],
                            reason="targeted_repair",
                        ),
                        "resolution": "targeted_repair_passed_reaudit",
                    })
                    set_unit_status(unit, "repaired", "targeted_repair_passed_reaudit")

    for unit in units:
        if unit["kind"] == "finding_binding" and unit_statuses[unit["claim_id"]]["reason"] == "source_relation_issue":
            if all(unit_statuses[cid]["status"] in {"passed", "repaired", "evidence_fallback"} for cid in unit["consumer_ids"]):
                set_unit_status(unit, "repaired", "consumers_resolved_original_finding_unchanged")
            else:
                # Consumer notices identify report locations; the immutable upstream
                # relation issue remains separately traceable in diagnostics.
                set_unit_status(unit, "needs_review", "source_issue_consumers_unresolved")
    for unit in units:
        if unit_statuses[unit["claim_id"]]["status"] != "pending":
            continue
        add_unresolved(
            unit,
            {
                "problem_type": "other",
                "problem": "该项没有获得终态，不能视为已通过",
                "severity": "blocking",
            },
            reason="internal_status_missing",
            status="incomplete",
        )

    elapsed = time.monotonic() - started
    status_names = (
        "passed",
        "evidence_fallback",
        "repaired",
        "needs_review",
        "incomplete",
    )
    unit_status_counts = {
        name: sum(item["status"] == name for item in unit_statuses.values())
        for name in status_names
    }
    if unit_status_counts["incomplete"]:
        status = "incomplete"
        stop_reason = "audit_incomplete"
    elif unit_status_counts["needs_review"]:
        status = "needs_review"
        stop_reason = "unresolved_after_single_repair"
    elif unit_status_counts["evidence_fallback"] or unit_status_counts["repaired"]:
        status = "repaired"
        stop_reason = "issues_resolved"
    else:
        status = "passed"
        stop_reason = "all_claims_supported"
    models = list(dict.fromkeys(
        item["model"] for item in calls if item.get("model")
    ))
    upstream_attempt_count = sum(
        event.get("status") == "started" for event in local_attempt_events
    )
    attempt_failures_by_step: dict[str, dict[str, int]] = {}
    for call in calls:
        if not call.get("attempt_failures"):
            continue
        attempt_failures_by_step.setdefault(call["step"], {})
        for category, count in call["attempt_failures"].items():
            step_counts = attempt_failures_by_step[call["step"]]
            step_counts[category] = step_counts.get(category, 0) + count
    capacity_reasons: set[str] = set()
    reaudit_success_count = sum(
        len(batch)
        for batch, result, _diagnostic in reaudit_results
        if result.get("data") is not None
    )
    sample_unit = next((unit for unit in units if unit["kind"] == "sample"), None)
    sample_status = unit_statuses.get((sample_unit or {}).get("claim_id"), {}).get("status")
    audited_sample = (
        audited_chapters[sample_chapter_index]["blocks"][0]
        if sample_overview and sample_status in {"passed", "repaired"}
        else None
    )
    return {
        "core_blocks": audited_core,
        "chapters": audited_chapters[:sample_chapter_index],
        "actions": audited_actions,
        "sample_overview": audited_sample,
        "unresolved": unresolved,
        "diagnostics": {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "prompt_version": INSIGHT_V3_AUDIT_PROMPT_VERSION,
            "repair_prompt_version": INSIGHT_V3_AUDIT_REPAIR_PROMPT_VERSION,
            "rules_version": AUDIT_RULES_VERSION,
            "status": status,
            "stop_reason": stop_reason,
            "planned_unit_count": len(units),
            "missing_evidence_count": len(missing_evidence),
            "oversized_unit_count": len(over_target_units),
            "over_target_initial_unit_count": len(over_target_units),
            "audited_unit_count": len(audited_ids),
            "initial_coverage_rate": round(len(audited_ids) / len(units), 4),
            "initial_issue_count": len(initial_issues),
            "body_issue_count": len(body_issues),
            "evidence_point_fallback_count": evidence_fallback_count,
            "repair_requested_count": len(body_issues),
            "repair_attempted_count": sum(len(batch) for batch in repair_batches),
            "repair_oversized_count": len(repair_over_target),
            "over_target_repair_unit_count": len(repair_over_target),
            "reaudit_requested_count": len(valid_repairs),
            "reaudited_unit_count": reaudit_success_count,
            "reaudit_coverage_rate": (
                round(reaudit_success_count / len(valid_repairs), 4)
                if valid_repairs else 1.0
            ),
            "over_target_reaudit_unit_count": len(reaudit_over_target),
            "repaired_count": sum(
                item.get("resolution") == "targeted_repair_passed_reaudit"
                for item in resolved
            ),
            "resolved_count": len(resolved),
            "unresolved_count": len(unresolved),
            "capacity_issue_count": sum(
                item.get("reason") in capacity_reasons for item in unresolved
            ),
            "over_target_call_count": sum(
                bool(item.get("input_target_exceeded")) for item in calls
            ),
            "unit_status_counts": unit_status_counts,
            "unit_statuses": [unit_statuses[unit["claim_id"]] for unit in units],
            "sample_fallback_reason": (
                "audit_" + (sample_status or "missing")
                if sample_overview and audited_sample is None else ""
            ),
            "source_checks": source_checks,
            "source_check_unit_count": sum(u["kind"] == "finding_binding" for u in units),
            "action_unit_count": len(audited_actions),
            "initial_batch_count": len(batches),
            "repair_batch_count": len(repair_batches),
            "reaudit_batch_count": len(reaudit_batches),
            "input_char_limit": INSIGHT_V3_AUDIT_INPUT_CHARS,
            "batch_unit_limit": INSIGHT_V3_AUDIT_BATCH_UNIT_LIMIT,
            "concurrency": INSIGHT_V3_AUDIT_CONCURRENCY,
            "peak_concurrency": max(initial_peak, repair_peak, reaudit_peak),
            "logical_call_budget": None,
            "logical_call_count": len(calls),
            "upstream_attempt_count": upstream_attempt_count,
            "attempt_failures_by_step": attempt_failures_by_step,
            "calls": calls,
            "models": models,
            "resolved": resolved,
            "unresolved": deepcopy(unresolved),
            "frequency_rewrites": frequency_rewrites,
            "elapsed_seconds": round(elapsed, 3),
            "warn_seconds": INSIGHT_V3_AUDIT_WARN_SECONDS,
            "warned": elapsed > INSIGHT_V3_AUDIT_WARN_SECONDS,
        },
    }


def _frequency_text_fields(
    *,
    core_blocks: list[dict],
    chapters: list[dict],
    actions: list[dict],
):
    sections = [
        ("核心发现", core_blocks or []),
        *(
            (_clean(chapter.get("title")) or "分析章节", chapter.get("blocks") or [])
            for chapter in chapters or []
        ),
    ]
    for section, blocks in sections:
        for block in blocks:
            item_id = _clean(block.get("id"))
            for field in ("heading", "body"):
                value = _clean(block.get(field))
                if value:
                    yield section, item_id, field, value
            for index, evidence in enumerate(block.get("evidence") or [], 1):
                value = _clean(evidence.get("evidence_point"))
                if value:
                    yield section, item_id, f"evidence_point[{index}]", value
    for action in actions or []:
        item_id = _clean(action.get("id"))
        for field in (
            "title", "problem", "objective", "validation_plan", "evidence_gap", "required_data"
        ):
            value = _clean(action.get(field))
            if value:
                yield "行动建议", item_id, field, value
        for index, direction in enumerate(action.get("design_directions") or [], 1):
            value = _clean(direction)
            if value:
                yield "行动建议", item_id, f"design_directions[{index}]", value


def scan_frequency_review_candidates(
    *,
    core_blocks: list[dict],
    chapters: list[dict],
    actions: list[dict],
) -> dict:
    """High-recall post-writing scan. Hits never rewrite or block a report."""
    candidates: list[dict] = []
    seen: set[tuple[str, str, str, int, int]] = set()
    for section, item_id, field, text in _frequency_text_fields(
        core_blocks=core_blocks,
        chapters=chapters,
        actions=actions,
    ):
        for category, pattern in _FREQUENCY_DISCOVERY_PATTERNS:
            for match in pattern.finditer(text):
                key = (section, item_id, field, match.start(), match.end())
                if key in seen:
                    continue
                seen.add(key)
                start = max(0, match.start() - 36)
                end = min(len(text), match.end() + 36)
                candidates.append({
                    "candidate_id": f"fq{len(candidates) + 1:03d}",
                    "section": section,
                    "item_id": item_id,
                    "field": field,
                    "category": category,
                    "matched_text": match.group(0),
                    "context": text[start:end],
                })
    return {
        "rules_version": FREQUENCY_DISCOVERY_RULES_VERSION,
        "status": "needs_manual_review" if candidates else "clear",
        "candidate_count": len(candidates),
        "auto_blocked_count": 0,
        "candidates": candidates,
    }
