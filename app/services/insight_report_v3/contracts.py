"""Cross-stage source identity and model-output contracts for Insight v3."""

from __future__ import annotations

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


FINDINGS_SCHEMA_VERSION = "insight-findings-v1"
SOURCE_ID_VERSION = "ir3v1"

MODEL_ROOT_FIELDS = frozenset({"findings"})
MODEL_FINDING_FIELDS = frozenset(
    {"id", "claim", "kind", "frequency_tier", "support_count", "response_ids", "caveat"}
)
MODEL_OUTPUT_FIELD_WHITELISTS = {
    "root": MODEL_ROOT_FIELDS,
    "finding": MODEL_FINDING_FIELDS,
}
FORBIDDEN_MODEL_OUTPUT_FIELDS = frozenset(
    {"quote", "text", "excerpt", "snippet", "original", "source_text", "translation"}
)
FINDING_KINDS = frozenset({"pain", "motivation", "risk", "wish", "fact", "uncertain"})
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")


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
