"""Pure contracts for safe, version-bound partial survey-report reruns."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

import survey_stats

from app.services.report_engine import _open_text_scopes


PARTIAL_RERUN_SCHEMA_VERSION = 1
_ALLOWED_OPEN_TEXT_FIELDS = (
    "respondent_key",
    "ids",
    "profile",
    "segments",
    "text",
    "source",
    "parent_question",
    "parent_index",
    "other_option",
)
_H1_OR_H2_RE = re.compile(r"(?m)^(#{1,2})[ \t]+(.+?)[ \t]*$")


def _canonical_sha256(value) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_plan_fingerprint(plan: dict) -> str:
    if not isinstance(plan, dict):
        return ""
    return _canonical_sha256(plan)


def _sanitize_open_text(open_text: dict) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for raw_key, raw_entries in (open_text or {}).items():
        if not isinstance(raw_entries, list):
            continue
        entries: list[dict] = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                continue
            entry = {
                field: deepcopy(raw_entry[field])
                for field in _ALLOWED_OPEN_TEXT_FIELDS
                if field in raw_entry
            }
            entry["text"] = str(entry.get("text") or "").strip()
            if entry["text"]:
                entries.append(entry)
        result[str(raw_key)] = entries
    return result


def _headers_from_session(sess: dict, plan: dict) -> list[str]:
    rows = sess.get("rows") or []
    if rows and isinstance(rows[0], list):
        return [str(value or "") for value in rows[0]]
    indexes = [
        int(col.get("index"))
        for col in plan.get("columns") or []
        if isinstance(col, dict) and isinstance(col.get("index"), int)
    ]
    headers = [""] * (max(indexes, default=-1) + 1)
    for col in plan.get("columns") or []:
        index = col.get("index") if isinstance(col, dict) else None
        if isinstance(index, int) and index < len(headers):
            headers[index] = str(col.get("name") or f"列{index}")
    return headers


def build_partial_rerun_source(sess: dict) -> dict | None:
    """Persist only source material required after the temporary session expires."""
    plan = sess.get("plan")
    if (
        not isinstance(plan, dict)
        or (sess.get("mode") or "") in {"comment", "interview", "annotate", "crosstab"}
        or not isinstance(sess.get("open_text"), dict)
        or not str(sess.get("stats_md") or "").strip()
    ):
        return None

    headers = _headers_from_session(sess, plan)
    source_payload = {
        "headers": headers,
        "open_text": _sanitize_open_text(sess.get("open_text") or {}),
        "stats_md": str(sess.get("stats_md") or ""),
        "stats_source": str(sess.get("stats_source") or "python"),
        "analysis_mode": str(sess.get("analysis_mode") or ""),
        "mode": str(sess.get("mode") or ""),
        "qualitative_context": deepcopy(sess.get("qualitative_context") or {}),
        "file_sha256": str(sess.get("file_sha256") or ""),
        "questionnaire_sha256": str(sess.get("questionnaire_sha256") or ""),
    }
    comparison_catalog = []
    if sess.get("rows"):
        try:
            comparison_catalog = survey_stats.build_comparison_fact_catalog(
                sess.get("rows") or [],
                plan,
            )
        except Exception:
            comparison_catalog = []
    source_payload["comparison_catalog"] = comparison_catalog
    return {
        "schema_version": PARTIAL_RERUN_SCHEMA_VERSION,
        "plan_fingerprint": build_plan_fingerprint(plan),
        "source_fingerprint": _canonical_sha256(source_payload),
        **source_payload,
    }


def verify_partial_rerun_source(source: dict) -> bool:
    if not isinstance(source, dict):
        return False
    payload = {
        key: deepcopy(value)
        for key, value in source.items()
        if key not in {"schema_version", "plan_fingerprint", "source_fingerprint"}
    }
    expected = str(source.get("source_fingerprint") or "")
    return bool(expected) and _canonical_sha256(payload) == expected


def build_analysis_artifacts(
    source: dict | None,
    *,
    use_large_mode: bool,
    clustered_themes: dict,
    report_viewpoints: list[dict],
    viewpoint_stats_md: str,
    cluster_diagnostics: dict,
    cluster_metrics: dict,
) -> dict:
    return {
        "schema_version": PARTIAL_RERUN_SCHEMA_VERSION,
        "plan_fingerprint": str((source or {}).get("plan_fingerprint") or ""),
        "source_fingerprint": str((source or {}).get("source_fingerprint") or ""),
        "qualitative_mode": "clustered" if clustered_themes else "raw_text",
        "report_generation_mode": "large" if use_large_mode else "standard",
        "clustered_themes": deepcopy(clustered_themes or {}),
        "report_viewpoints": deepcopy(report_viewpoints or []),
        "viewpoint_stats_md": str(viewpoint_stats_md or ""),
        "open_text_cluster_diagnostics": deepcopy(cluster_diagnostics or {}),
        "open_text_cluster_metrics": deepcopy(cluster_metrics or {}),
    }


def _scope_rows(plan: dict, source: dict) -> list[dict]:
    headers = source.get("headers") or []
    rows: list[dict] = []
    for scope_key, col_idx, part_index, part, entries in _open_text_scopes(
        source.get("open_text") or {},
        plan,
    ):
        if part_index < 1:
            continue
        col = next(
            (
                item for item in plan.get("columns") or []
                if item.get("index") == col_idx
            ),
            None,
        )
        question_name = str(
            (col or {}).get("name")
            or (headers[col_idx] if col_idx < len(headers) else f"列{col_idx}")
        )
        rows.append({
            "scope_key": str(scope_key),
            "column_index": col_idx,
            "question_name": question_name,
            "part_index": part_index,
            "part_title": f"Part {part_index} {part.get('name', '')}".strip(),
            "response_count": len(entries),
        })
    return rows


def partial_rerun_capability(entry: dict, snapshot: dict) -> dict:
    unavailable = {
        "available": False,
        "reason": "该版本生成时尚未保存局部重做所需的受控分析产物。",
        "parts": [],
        "questions": [],
    }
    if (entry.get("mode") or "") in {"comment", "interview", "annotate", "crosstab"}:
        return {**unavailable, "reason": "当前报告类型暂不支持局部重做。"}
    plan = entry.get("plan")
    source = entry.get("partial_rerun_source")
    artifacts = snapshot.get("analysis_artifacts")
    if not isinstance(plan, dict) or not isinstance(source, dict) or not isinstance(artifacts, dict):
        return unavailable
    plan_fingerprint = build_plan_fingerprint(plan)
    if (
        not plan_fingerprint
        or not verify_partial_rerun_source(source)
        or plan_fingerprint != source.get("plan_fingerprint")
        or plan_fingerprint != artifacts.get("plan_fingerprint")
        or source.get("source_fingerprint") != artifacts.get("source_fingerprint")
    ):
        return {**unavailable, "reason": "报告的数据或分析方案指纹不一致，已拒绝套用旧产物。"}

    questions = _scope_rows(plan, source)
    parts = [
        {
            "part_index": index,
            "part_title": f"Part {index} {part.get('name', '')}".strip(),
            "scope_keys": [
                item["scope_key"]
                for item in questions
                if item["part_index"] == index
            ],
        }
        for index, part in enumerate(plan.get("parts") or [], 1)
    ]
    return {
        "available": bool(parts),
        "reason": "" if parts else "当前分析方案没有可重写的 Part。",
        "parts": parts,
        "questions": questions,
        "plan_fingerprint": plan_fingerprint,
        "source_fingerprint": source.get("source_fingerprint"),
        "qualitative_mode": artifacts.get("qualitative_mode") or "raw_text",
    }


def resolve_partial_rerun_target(
    plan: dict,
    source: dict,
    *,
    target_type: str,
    target_key: str,
) -> dict:
    capability = partial_rerun_capability(
        {"plan": plan, "partial_rerun_source": source},
        {
            "analysis_artifacts": {
                "plan_fingerprint": source.get("plan_fingerprint"),
                "source_fingerprint": source.get("source_fingerprint"),
            }
        },
    )
    target_type = str(target_type or "").strip().lower()
    if target_type == "question":
        target = next(
            (item for item in capability["questions"] if item["scope_key"] == str(target_key)),
            None,
        )
        if not target:
            raise ValueError("目标题目不属于当前基础版本")
        return {
            "target_type": "question",
            "target_key": target["scope_key"],
            "target_label": target["question_name"],
            "part_index": target["part_index"],
            "part_title": target["part_title"],
            "scope_keys": [target["scope_key"]],
        }
    if target_type == "part":
        try:
            part_index = int(target_key)
        except (TypeError, ValueError) as exc:
            raise ValueError("目标 Part 无效") from exc
        target = next(
            (item for item in capability["parts"] if item["part_index"] == part_index),
            None,
        )
        if not target:
            raise ValueError("目标 Part 不属于当前基础版本")
        return {
            "target_type": "part",
            "target_key": str(part_index),
            "target_label": target["part_title"],
            "part_index": part_index,
            "part_title": target["part_title"],
            "scope_keys": list(target["scope_keys"]),
        }
    raise ValueError("重做范围必须是 question 或 part")


def scope_tuples_for_keys(plan: dict, source: dict, scope_keys: list[str]) -> list:
    wanted = {str(key) for key in scope_keys}
    return [
        scope
        for scope in _open_text_scopes(source.get("open_text") or {}, plan)
        if str(scope[0]) in wanted
    ]


def extract_h2_section(report_md: str, heading_text: str) -> str:
    heading = f"## {heading_text}"
    lines = str(report_md or "").splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == heading]
    if len(starts) != 1:
        raise ValueError(f"基础报告中的 `{heading}` 必须且只能出现一次")
    start = starts[0]
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end]).strip()


def validate_single_part(part_md: str, expected_title: str) -> str:
    text = str(part_md or "").strip()
    headings = list(_H1_OR_H2_RE.finditer(text))
    expected = f"## {expected_title}"
    if not text or sum(match.group(0).strip() == expected for match in headings) != 1:
        raise ValueError(f"新 Part 必须且只能包含标题 `{expected}`")
    invalid = [
        match.group(0).strip()
        for match in headings
        if match.group(1) == "#" or match.group(0).strip() != expected
    ]
    if invalid:
        raise ValueError("新 Part 不得夹带其他 H1/H2 标题")
    if not text.startswith(expected):
        raise ValueError("新 Part 必须从目标 H2 标题开始")
    return text


def replace_h2_section(report_md: str, heading_text: str, replacement: str) -> str:
    validated = validate_single_part(replacement, heading_text)
    old = extract_h2_section(report_md, heading_text)
    if str(report_md).count(old) != 1:
        raise ValueError("基础 Part 无法唯一定位")
    return str(report_md).replace(old, validated, 1)


def replace_core_block(report_md: str, replacement: str) -> str:
    text = str(replacement or "").strip()
    if (
        not text.startswith("<!--CORE_START-->")
        or not text.endswith("<!--CORE_END-->")
        or text.count("<!--CORE_START-->") != 1
        or text.count("<!--CORE_END-->") != 1
        or len(re.findall(r"(?m)^## 核心结论[ \t]*$", text)) != 1
    ):
        raise ValueError("新核心结论未通过严格结构校验")
    pattern = re.compile(
        r"<!--CORE_START-->.*?<!--CORE_END-->",
        re.DOTALL,
    )
    if len(pattern.findall(report_md or "")) != 1:
        raise ValueError("基础报告的核心结论无法唯一定位")
    return pattern.sub(lambda _match: text, report_md, count=1)


def replace_action_section(report_md: str, replacement: str) -> str:
    return replace_h2_section(report_md, "行动建议", replacement)
