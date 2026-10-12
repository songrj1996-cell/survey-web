"""Cross-stage source identity and model-output contracts for Insight v3."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import re

from app.core.config import INSIGHT_V3_FINDINGS_PROMPT_VERSION
from app.services.report_engine import (
    _open_text_scopes,
    _part_filter_desc,
    _question_name_with_branch,
    _response_ref,
)
from app.services.report_partial_rerun import build_plan_fingerprint
from app.services.insight_report_v3.frequency import (
    FREQUENCY_RULES_VERSION,
    validate_frequency,
)


FINDINGS_SCHEMA_VERSION = "insight-findings-v2-research-detail"
SOURCE_ID_VERSION = "ir3v1"

MODEL_ROOT_FIELDS = frozenset({"findings"})
MODEL_FINDING_FIELDS = frozenset(
    {"id", "claim", "kind", "frequency_tier", "support_count", "response_ids", "caveat", "preserve_reason"}
)
MODEL_AUDIT_ROOT_FIELDS = frozenset({"checked_claim_ids", "issues", "finding_checks"})
SOURCE_RELATIONS = ("supports", "opposes", "context_only", "mixed", "unclear")
ACTION_REPAIR_TEXT_FIELDS = frozenset({
    "title", "problem", "objective", "design_directions", "validation_plan",
    "evidence_gap", "required_data",
})
LEGACY_AUDIT_ISSUE_FIELDS = frozenset(
    {"claim_id", "problem_type", "problem", "severity"}
)
MODEL_AUDIT_ISSUE_FIELDS = LEGACY_AUDIT_ISSUE_FIELDS | {"target_ids"}
MODEL_AUDIT_REPAIR_ROOT_FIELDS = frozenset({"repairs"})
MODEL_AUDIT_REPAIR_FIELDS = frozenset({"claim_id", "heading", "body"})
MODEL_OUTPUT_FIELD_WHITELISTS = {
    "root": MODEL_ROOT_FIELDS,
    "finding": MODEL_FINDING_FIELDS,
    "audit_root": MODEL_AUDIT_ROOT_FIELDS,
    "audit_issue": MODEL_AUDIT_ISSUE_FIELDS,
    "audit_repair_root": MODEL_AUDIT_REPAIR_ROOT_FIELDS,
    "audit_repair": MODEL_AUDIT_REPAIR_FIELDS,
}
FORBIDDEN_MODEL_OUTPUT_FIELDS = frozenset(
    {"quote", "text", "excerpt", "snippet", "original", "source_text", "translation"}
)
FINDING_KINDS = frozenset({"pain", "motivation", "risk", "wish", "fact", "uncertain"})
AUDIT_PROBLEM_TYPES = frozenset({
    "contradiction",
    "unsupported_detail",
    "unsupported_causality",
    "scope_shift",
    "polarity_shift",
    "condition_shift",
    "mistranslation",
    "other",
    "missing_requirement",
    "missing_counterexample",
    "shallow_analysis",
    "statistic_scope",
})
AUDIT_SEVERITIES = frozenset({"warning", "blocking"})
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_MARKDOWN_HEADING_RE = re.compile(r"(?m)^\s*#{1,6}\s+")
_MARKDOWN_TABLE_RE = re.compile(r"(?m)^\s*\|.*\|\s*$")
_INLINE_RESPONSE_ID_RE = re.compile(r"(?<![A-Za-z0-9_])r[0-9]+(?![A-Za-z0-9_])")


def _canonical_sha256(value) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalized_file_sha256(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if _HEX_64_RE.fullmatch(normalized):
        return normalized
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_global_response_id(
    *,
    file_sha256: str,
    column_index: int,
    source_index: int,
    respondent_key: str,
) -> str:
    """Build a code-only stable ID; this value must never be sent to the model."""
    file_hash = _normalized_file_sha256(file_sha256)
    respondent_digest = hashlib.sha256(
        f"{file_hash}\0{str(respondent_key or '')}".encode("utf-8")
    ).hexdigest()[:12]
    return (
        f"{SOURCE_ID_VERSION}-{file_hash[:16]}-c{int(column_index)}-"
        f"i{int(source_index)}-p{respondent_digest}"
    )


def _forbidden_field_path(value, path: str = "$") -> str:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key or "").strip().lower()
            if normalized in FORBIDDEN_MODEL_OUTPUT_FIELDS:
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


def validate_findings_payload(
    data: dict | None,
    *,
    valid_short_ids: set[str],
    respondent_total: int,
) -> str | None:
    """Validate only the model-facing short-ID contract."""
    if not isinstance(data, dict):
        return "JSON 根节点必须是对象"
    forbidden_path = _forbidden_field_path(data)
    if forbidden_path:
        return f"模型输出包含禁止的原文字段：{forbidden_path}"
    if set(data) != MODEL_ROOT_FIELDS:
        return "JSON 根节点只能包含 findings"
    findings = data.get("findings")
    if not isinstance(findings, list) or not findings:
        return "findings 必须是非空数组"
    seen_ids: set[str] = set()
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            return f"findings[{index}] 不是对象"
        if set(finding) != MODEL_FINDING_FIELDS:
            return f"findings[{index}] 字段不符合白名单"
        finding_id = str(finding.get("id") or "").strip()
        if not finding_id or finding_id in seen_ids:
            return f"findings[{index}] 的 id 缺失或重复"
        seen_ids.add(finding_id)
        if not str(finding.get("claim") or "").strip():
            return f"findings[{index}] 缺少 claim"
        if finding.get("kind") not in FINDING_KINDS:
            return f"findings[{index}] 的 kind 不在白名单内"
        if not isinstance(finding.get("caveat"), str):
            return f"findings[{index}] 的 caveat 必须是字符串"
        for field in ("claim", "caveat"):
            # Only call-local tokens are references here. Never rewrite source
            # text or treat arbitrary account identifiers as sensitive data.
            inline_id = next((
                match.group() for match in _INLINE_RESPONSE_ID_RE.finditer(str(finding[field]))
                if match.group() in valid_short_ids
            ), None)
            if inline_id:
                return (
                    f"findings[{index}].{field} 含内联来源短 ID：{inline_id}；"
                    "来源只能写入 response_ids。事实性反例须独立成 finding 并绑定自己的来源；"
                    "caveat 只说明当前发现的证据边界，不得夹带其它事实。"
                )
        if finding.get("preserve_reason") not in {"", "specific_issue", "counterexample", "key_condition"}:
            return f"findings[{index}] 的 preserve_reason 无效"
        response_ids = finding.get("response_ids")
        if (
            not isinstance(response_ids, list)
            or not response_ids
            or any(not isinstance(item, str) for item in response_ids)
        ):
            return f"findings[{index}] 的 response_ids 必须是非空字符串数组"
        if len(response_ids) != len(set(response_ids)):
            return f"findings[{index}] 的 response_ids 不得重复"
        unknown = [item for item in response_ids if item not in valid_short_ids]
        if unknown:
            return f"findings[{index}] 引用了不存在的短 ID：{unknown[0]}"
        frequency_error = validate_frequency(
            finding.get("frequency_tier"),
            finding.get("support_count"),
            len(response_ids),
            respondent_total,
        )
        if frequency_error:
            return f"findings[{index}]：{frequency_error}"
    return None


def _audit_relation_coverage_error(claim_id: str, ids: list[str], expected: set[str]) -> str:
    counts = Counter(ids)
    actual = set(counts)
    differences = {
        "missing": expected - actual,
        "extra": actual - expected,
        "duplicate": {item for item, count in counts.items() if count > 1},
    }
    details = []
    for label, values in differences.items():
        # Repair feedback may echo only short IDs, never arbitrary model text or
        # accidentally returned global IDs. Invalid values remain rejected.
        short_ids = sorted(item for item in values if re.fullmatch(r"r[0-9]{3,}", item))
        invalid_count = len(values) - len(short_ids)
        if invalid_count:
            short_ids.append(f"<non_short_id_count={invalid_count}>")
        details.append(f"{label}=[{','.join(short_ids)}]")
    safe_claim_id = claim_id if re.fullmatch(r"c[0-9]{3,}", claim_id) else "<non_short_claim_id>"
    return (
        "来源关系必须恰好覆盖此 finding 的全部来源，不得跨 finding 借用："
        f"{safe_claim_id} {'; '.join(details)}"
    )


def validate_audit_payload(
    data: dict | None,
    *,
    valid_claim_ids: set[str],
    expected_source_ids_by_claim: dict[str, set[str]] | None = None,
    expected_targets_by_claim: dict[str, set[str]] | None = None,
) -> str | None:
    """Validate the short-ID-only semantic-audit response."""
    # Legacy validation is only for explicitly reading old artifacts. Live callers
    # always supply both expected mappings (including empty mappings).
    if expected_targets_by_claim is not None and expected_source_ids_by_claim is None:
        return "对象绑定契约缺少来源核对范围"
    if expected_targets_by_claim is not None and (
        set(expected_targets_by_claim) & set(expected_source_ids_by_claim)
        or set(expected_targets_by_claim) | set(expected_source_ids_by_claim) != valid_claim_ids
    ):
        return "审校对象范围必须恰好覆盖本批且不得混用"
    fields = {"issues"} if expected_source_ids_by_claim is None else MODEL_AUDIT_ROOT_FIELDS
    if not isinstance(data, dict) or set(data) != fields:
        return "审校根节点字段不符合当前版本契约"
    forbidden_path = _forbidden_field_path(data)
    if forbidden_path:
        return f"审校输出包含禁止的原文字段：{forbidden_path}"
    if expected_source_ids_by_claim is not None:
        checked = data.get("checked_claim_ids")
        if (not isinstance(checked, list) or any(not isinstance(x, str) for x in checked)
                or len(checked) != len(set(checked)) or set(checked) != valid_claim_ids):
            return "checked_claim_ids 必须恰好覆盖本批全部单元"
        checks = data.get("finding_checks")
        if not isinstance(checks, list):
            return "finding_checks 必须是数组"
        seen_checks = set()
        for check in checks:
            if not isinstance(check, dict) or set(check) != {"claim_id", "status", "relations", "problem"}:
                return "finding_checks 字段不符合契约"
            cid = check.get("claim_id")
            if not isinstance(cid, str) or cid not in expected_source_ids_by_claim or cid in seen_checks:
                return "finding_checks 的 claim_id 未知或重复"
            seen_checks.add(cid)
            if check.get("status") not in {"supported", "needs_review", "inconclusive"}:
                return "finding_checks 的 status 无效"
            problem = check.get("problem")
            if (not isinstance(problem, str) or len(problem) > 240 or "\n" in problem
                    or (check["status"] != "supported" and not problem.strip())):
                return "finding_checks 的问题说明无效"
            relations = check.get("relations")
            if not isinstance(relations, dict) or set(relations) != set(SOURCE_RELATIONS):
                return "来源关系必须包含五个分类"
            ids = []
            for bucket in relations.values():
                if not isinstance(bucket, list) or any(not isinstance(x, str) for x in bucket):
                    return "来源关系必须是短 ID 数组"
                ids.extend(bucket)
            if len(ids) != len(set(ids)) or set(ids) != expected_source_ids_by_claim[cid]:
                return _audit_relation_coverage_error(cid, ids, expected_source_ids_by_claim[cid])
        if seen_checks != set(expected_source_ids_by_claim):
            return "finding_checks 不得遗漏来源核对单元"
    issues = data.get("issues")
    if not isinstance(issues, list):
        return "issues 必须是数组"
    seen: set[str] = set()
    for index, issue in enumerate(issues):
        issue_fields = (LEGACY_AUDIT_ISSUE_FIELDS if expected_targets_by_claim is None
                        else MODEL_AUDIT_ISSUE_FIELDS)
        if not isinstance(issue, dict) or set(issue) != issue_fields:
            return f"issues[{index}] 字段不符合白名单"
        claim_id = issue.get("claim_id")
        if not isinstance(claim_id, str) or claim_id not in valid_claim_ids:
            return f"issues[{index}] 引用了不存在的 claim_id"
        if expected_source_ids_by_claim is not None and claim_id in expected_source_ids_by_claim:
            return "来源核对问题必须写入 finding_checks，不得混入正文 issues"
        if expected_targets_by_claim is not None:
            targets = issue.get("target_ids")
            if (not isinstance(targets, list) or not targets
                    or any(not isinstance(t, str) for t in targets)
                    or len(targets) != len(set(targets))
                    or not set(targets) <= expected_targets_by_claim.get(claim_id, set())):
                return f"issues[{index}] 的 target_ids 必须绑定本单元的实际字段，不得引用原文、参考材料或其它单元"
        if claim_id in seen:
            return f"issues[{index}] 的 claim_id 重复"
        seen.add(claim_id)
        if issue.get("problem_type") not in AUDIT_PROBLEM_TYPES:
            return f"issues[{index}] 的 problem_type 不在白名单内"
        if issue.get("severity") not in AUDIT_SEVERITIES:
            return f"issues[{index}] 的 severity 不在白名单内"
        problem = issue.get("problem")
        if (
            not isinstance(problem, str)
            or not problem.strip()
            or len(problem.strip()) > 240
            or "\n" in problem
        ):
            return f"issues[{index}] 的 problem 必须是 1—240 字单行说明"
    return None


def validate_audit_repair_payload(
    data: dict | None,
    *,
    expected_claim_ids: set[str],
    action_claim_ids: set[str] | None = None,
) -> str | None:
    """Validate a one-shot text-only repair without allowing binding changes."""
    if not isinstance(data, dict) or set(data) != MODEL_AUDIT_REPAIR_ROOT_FIELDS:
        return "JSON 根节点必须且只能包含 repairs"
    forbidden_path = _forbidden_field_path(data)
    if forbidden_path:
        return f"审校修复输出包含禁止的原文字段：{forbidden_path}"
    repairs = data.get("repairs")
    if not isinstance(repairs, list):
        return "repairs 必须是数组"
    ids: list[str] = []
    for index, repair in enumerate(repairs):
        if not isinstance(repair, dict):
            return f"repairs[{index}] 必须是对象"
        claim_id = repair.get("claim_id")
        if not isinstance(claim_id, str) or claim_id not in expected_claim_ids:
            return f"repairs[{index}] 引用了不存在的 claim_id"
        is_action = claim_id in (action_claim_ids or set())
        fields = {"claim_id", "action_text"} if is_action else MODEL_AUDIT_REPAIR_FIELDS
        if set(repair) != fields:
            return f"repairs[{index}] 字段不符合白名单"
        ids.append(claim_id)
        if is_action:
            action = repair.get("action_text")
            if not isinstance(action, dict) or set(action) != ACTION_REPAIR_TEXT_FIELDS:
                return "建议修复只允许七个文本字段，不得更改绑定"
            for field, value in action.items():
                if field == "design_directions":
                    if not isinstance(value, list) or not 1 <= len(value) <= 3 or any(not isinstance(x, str) or not x.strip() for x in value):
                        return "建议修复的 design_directions 无效"
                elif not isinstance(value, str):
                    return "建议修复文本必须是字符串"
            continue
        for field in ("heading", "body"):
            value = repair.get(field)
            if not isinstance(value, str) or not value.strip():
                return f"repairs[{index}].{field} 必须是非空字符串"
            if _MARKDOWN_HEADING_RE.search(value) or _MARKDOWN_TABLE_RE.search(value):
                return f"repairs[{index}].{field} 不得包含 Markdown 标题或表格"
    if len(ids) != len(set(ids)):
        return "repairs 的 claim_id 不得重复"
    if set(ids) != expected_claim_ids:
        return "repairs 必须恰好覆盖本次全部 claim_id"
    return None


def hydrate_findings(
    data: dict,
    *,
    scope_key: str,
    short_id_map: dict[str, list[str]],
    source_registry: dict[str, dict],
) -> list[dict]:
    """Translate model short IDs into code-owned stable source IDs."""
    hydrated: list[dict] = []
    for index, raw in enumerate(data.get("findings") or [], 1):
        source_ids: list[str] = []
        for short_id in raw["response_ids"]:
            source_ids.extend(short_id_map[short_id])
        source_ids = list(dict.fromkeys(source_ids))
        respondent_refs = {
            source_registry[source_id]["respondent_ref"]
            for source_id in source_ids
            if source_id in source_registry
        }
        item = deepcopy(raw)
        model_finding_id = str(item.pop("id") or f"f{index}").strip()
        item["id"] = f"{scope_key}/{model_finding_id}"
        item["response_ids"] = source_ids
        item["representative_respondent_count"] = len(respondent_refs)
        item["support_basis"] = "unique_respondents_model_estimate"
        hydrated.append(item)
    return hydrated


def build_stage_source_contract(
    *,
    open_text: dict,
    plan: dict,
    headers: list,
    file_sha256: str,
) -> dict:
    """Build deterministic source registry and per-scope model inputs.

    Non-open questions are excluded before the question list is created.
    Each model-facing short ID represents one unique respondent in one scope.
    """
    file_hash = _normalized_file_sha256(file_sha256)
    open_columns = {
        int(column["index"]): column
        for column in plan.get("columns") or []
        if isinstance(column, dict)
        and column.get("role") == "open_text"
        and isinstance(column.get("index"), int)
    }
    source_registry: dict[str, dict] = {}
    identity_to_source_id: dict[tuple[int, int], str] = {}
    for raw_column_index, entries in (open_text or {}).items():
        try:
            column_index = int(raw_column_index)
        except (TypeError, ValueError):
            continue
        if column_index not in open_columns or not isinstance(entries, list):
            continue
        for source_index, entry in enumerate(entries, 1):
            if not isinstance(entry, dict) or not str(entry.get("text") or "").strip():
                continue
            respondent_key = str(entry.get("respondent_key") or f"entry:{source_index}")
            response_id = build_global_response_id(
                file_sha256=file_hash,
                column_index=column_index,
                source_index=source_index,
                respondent_key=respondent_key,
            )
            respondent_ref = hashlib.sha256(
                f"{file_hash}\0{respondent_key}".encode("utf-8")
            ).hexdigest()
            source_registry[response_id] = {
                "response_id": response_id,
                "column_index": column_index,
                "source_index": source_index,
                "respondent_ref": respondent_ref,
                "respondent_key": respondent_key,
                "text": str(entry.get("text") or "").strip(),
                "ids": deepcopy(entry.get("ids") or {}),
                "profile": deepcopy(entry.get("profile") or {}),
                "segments": deepcopy(entry.get("segments") or {}),
                "source": str(entry.get("source") or "open_text"),
            }
            identity_to_source_id[(column_index, id(entry))] = response_id

    questions: list[dict] = []
    for scope_key, column_index, part_index, part, entries in _open_text_scopes(open_text, plan):
        column = open_columns.get(column_index)
        if column is None:
            continue
        grouped: dict[str, dict] = {}
        for entry in entries:
            response_id = identity_to_source_id.get((column_index, id(entry)))
            if not response_id:
                continue
            record = source_registry[response_id]
            respondent_ref = record["respondent_ref"]
            group = grouped.setdefault(
                respondent_ref,
                {
                    "respondent_ref": respondent_ref,
                    "source_response_ids": [],
                    "responses": [],
                },
            )
            group["source_response_ids"].append(response_id)
            group["responses"].append(record["text"])

        model_sources: list[dict] = []
        short_id_map: dict[str, list[str]] = {}
        for index, group in enumerate(grouped.values()):
            short_id = _response_ref(index)
            short_id_map[short_id] = list(group["source_response_ids"])
            model_sources.append(
                {
                    "response_id": short_id,
                    "responses": list(group["responses"]),
                }
            )
        question_name = str(column.get("name") or "").strip() or (
            str(headers[column_index])
            if column_index < len(headers)
            else f"列{column_index}"
        )
        question_name = _question_name_with_branch(question_name, plan, column_index)
        questions.append(
            {
                "question_key": str(column_index),
                "scope_key": str(scope_key),
                "column_indexes": [column_index],
                "part_index": int(part_index),
                "question": question_name,
                "audience": _part_filter_desc(part, plan),
                "respondent_count": len(model_sources),
                "model_sources": model_sources,
                "short_id_map": short_id_map,
                "all_source_ids": [
                    source_id
                    for source_ids in short_id_map.values()
                    for source_id in source_ids
                ],
            }
        )

    source_binding = {
        "file_sha256": file_hash,
        "plan_fingerprint": build_plan_fingerprint(plan),
        "source_registry_fingerprint": _canonical_sha256(source_registry),
        "findings_input_fingerprint": _canonical_sha256([
            {
                "question_key": item["question_key"],
                "scope_key": item["scope_key"],
                "question": item["question"],
                "audience": item["audience"],
                "respondent_count": item["respondent_count"],
                "model_sources": item["model_sources"],
                "short_id_map": item["short_id_map"],
            }
            for item in questions
        ]),
        "frequency_rules_version": FREQUENCY_RULES_VERSION,
        "findings_prompt_version": INSIGHT_V3_FINDINGS_PROMPT_VERSION,
    }
    return {
        "source_binding": source_binding,
        "source_registry": source_registry,
        "questions": questions,
    }


def artifact_without_source_text(artifact: dict) -> dict:
    """Return evidence safe for diagnostics without duplicating raw answers."""
    public = deepcopy(artifact)
    registry = public.pop("source_registry", {})
    public["source_registry_summary"] = {
        "source_count": len(registry),
        "fingerprint": (public.get("source_binding") or {}).get(
            "source_registry_fingerprint", ""
        ),
    }
    return public
