"""Deterministic analysis-boundary and coverage rules for interview V2.

The module only consumes already frozen structure/evidence payloads.  It does
not persist data, call models, or infer facts from blank cells.  In particular,
source scope and evaluation-object decisions remain explicit reviewable data.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import re
import unicodedata
from typing import Any


ANALYSIS_BOUNDARY_SCHEMA_VERSION = "interview-analysis-boundary/1.0"
ANALYSIS_BOUNDARY_RULES_VERSION = "interview-v2-analysis-boundary-rules/1.0"
COVERAGE_SCHEMA_VERSION = "interview-question-coverage/1.0"
COVERAGE_RULES_VERSION = "interview-v2-question-coverage-rules/1.0"

_BOUNDARY_STATUSES = {"draft", "confirmed"}
_DECISION_STATUSES = {
    "proposed",
    "draft",
    "needs_review",
    "confirmed",
    "superseded",
}
_DECISION_SOURCES = {
    "deterministic_rule",
    "user_selection",
    "manual_override",
}
_SOURCE_TYPES = {"interview_body", "participant_background", "excluded"}
_LABEL_SCOPE_MODES = {
    "disabled",
    "all_analysis",
    "selected_modules",
    "selected_evaluation_objects",
}
_REPORTABLE_IDENTITY_STATUSES = {"system_verified", "human_confirmed"}
_ID_PATTERNS = {
    "project": re.compile(r"^project_[0-9a-f]{32}$"),
    "import": re.compile(r"^import_[0-9a-f]{32}$"),
    "structure": re.compile(r"^structure_[0-9a-f]{32}$"),
    "evidence": re.compile(r"^evidence_[0-9a-f]{32}$"),
    "evaluation": re.compile(r"^evaluation_[0-9a-f]{32}$"),
    "scope": re.compile(r"^scope_[0-9a-f]{32}$"),
    "label_scope": re.compile(r"^label_scope_[0-9a-f]{32}$"),
}


class InterviewV2AnalysisBoundaryError(Exception):
    """Stable pure-core error consumed by the orchestration service."""

    def __init__(
        self,
        code: str,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = dict(context or {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "context": dict(self.context),
        }


def _error(
    code: str, message: str, context: dict[str, Any] | None = None
) -> InterviewV2AnalysisBoundaryError:
    return InterviewV2AnalysisBoundaryError(code, message, context)


def _ensure_unicode_scalars(value: Any) -> None:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _error(
                "ANALYSIS_BOUNDARY_INPUT_INVALID",
                "分析边界输入包含无效 Unicode 文本。",
            ) from exc
    elif isinstance(value, dict):
        for key, child in value.items():
            _ensure_unicode_scalars(key)
            _ensure_unicode_scalars(child)
    elif isinstance(value, list):
        for child in value:
            _ensure_unicode_scalars(child)


def _text(value: object) -> str:
    if value is None:
        return ""
    return unicodedata.normalize(
        "NFC",
        str(value).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n"),
    ).strip()


def _stable_id(prefix: str, *parts: object) -> str:
    digest = sha256()
    for part in parts:
        encoded = unicodedata.normalize("NFC", str(part)).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"{prefix}_{digest.hexdigest()[:32]}"


def canonical_json_sha256(value: Any) -> str:
    """Return the stable digest used to bind a preview to its boundary."""

    _ensure_unicode_scalars(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _error(
            "ANALYSIS_BOUNDARY_INPUT_INVALID",
            "分析边界输入无法形成稳定快照。",
        ) from exc
    return sha256(encoded).hexdigest()


def _require_id(kind: str, value: object, field: str) -> str:
    text = _text(value)
    if not _ID_PATTERNS[kind].fullmatch(text):
        raise _error(
            "ANALYSIS_BOUNDARY_INPUT_INVALID",
            f"{field} 格式无效。",
            {"field": field},
        )
    return text


def _unique_ids(values: object, *, pattern: re.Pattern[str], field: str) -> list[str]:
    if not isinstance(values, list):
        raise _error(
            "ANALYSIS_BOUNDARY_INPUT_INVALID",
            f"{field} 必须是列表。",
            {"field": field},
        )
    result: list[str] = []
    for raw in values:
        value = _text(raw)
        if not pattern.fullmatch(value):
            raise _error(
                "ANALYSIS_BOUNDARY_INPUT_INVALID",
                f"{field} 包含无效标识。",
                {"field": field},
            )
        if value in result:
            raise _error(
                "ANALYSIS_BOUNDARY_INPUT_INVALID",
                f"{field} 包含重复标识。",
                {"field": field, "entity_id": value},
            )
        result.append(value)
    return sorted(result)


def _source(
    structure: dict[str, Any],
    evidence: dict[str, Any],
    *,
    project_id: str,
    import_id: str,
    structure_revision_id: str,
    evidence_revision_id: str,
) -> dict[str, str]:
    _ensure_unicode_scalars(structure)
    _ensure_unicode_scalars(evidence)
    project_id = _require_id("project", project_id, "project_id")
    import_id = _require_id("import", import_id, "import_id")
    structure_revision_id = _require_id(
        "structure", structure_revision_id, "structure_revision_id"
    )
    evidence_revision_id = _require_id(
        "evidence", evidence_revision_id, "evidence_revision_id"
    )
    structure_source = structure.get("source") or {}
    evidence_source = evidence.get("source") or {}
    for name, expected in (("project_id", project_id), ("import_id", import_id)):
        if _text(structure_source.get(name)) != expected:
            raise _error(
                "ANALYSIS_BOUNDARY_SOURCE_MISMATCH",
                "结构版本与分析边界工作区不一致。",
                {"field": name},
            )
        if _text(evidence_source.get(name)) != expected:
            raise _error(
                "ANALYSIS_BOUNDARY_SOURCE_MISMATCH",
                "证据版本与分析边界工作区不一致。",
                {"field": name},
            )
    return {
        "project_id": project_id,
        "import_id": import_id,
        "structure_revision_id": structure_revision_id,
        "evidence_revision_id": evidence_revision_id,
        "rules_version": ANALYSIS_BOUNDARY_RULES_VERSION,
    }


def _indexes(structure: dict[str, Any]) -> tuple[dict, dict, dict]:
    modules = {
        _text(item.get("module_id")): item
        for item in structure.get("modules") or []
        if isinstance(item, dict) and _text(item.get("module_id"))
    }
    questions = {
        _text(item.get("main_question_id")): item
        for item in structure.get("main_questions") or []
        if isinstance(item, dict) and _text(item.get("main_question_id"))
    }
    occurrences = {
        _text(item.get("occurrence_id")): item
        for item in structure.get("occurrences") or []
        if isinstance(item, dict) and _text(item.get("occurrence_id"))
    }
    if len(modules) != len(structure.get("modules") or []):
        raise _error("ANALYSIS_BOUNDARY_SOURCE_INVALID", "结构中存在无效或重复模块。")
    if len(questions) != len(structure.get("main_questions") or []):
        raise _error("ANALYSIS_BOUNDARY_SOURCE_INVALID", "结构中存在无效或重复主问题。")
    if len(occurrences) != len(structure.get("occurrences") or []):
        raise _error("ANALYSIS_BOUNDARY_SOURCE_INVALID", "结构中存在无效或重复 occurrence。")
    return modules, questions, occurrences


def _proposal_objects(
    structure: dict[str, Any], source: dict[str, str]
) -> list[dict[str, Any]]:
    modules, questions, _occurrences = _indexes(structure)
    module_order = {module_id: index for index, module_id in enumerate(modules, 1)}
    counters: dict[str, int] = {}
    result: list[dict[str, Any]] = []
    ordered_questions = sorted(
        questions.values(),
        key=lambda item: (
            module_order.get(_text(item.get("module_id")), 10**9),
            _text(item.get("main_question_id")),
        ),
    )
    for question in ordered_questions:
        module_id = _text(question.get("module_id"))
        question_id = _text(question.get("main_question_id"))
        if module_id not in modules:
            raise _error(
                "ANALYSIS_BOUNDARY_SOURCE_INVALID",
                "主问题引用了不存在的模块。",
                {"main_question_id": question_id},
            )
        counters[module_id] = counters.get(module_id, 0) + 1
        result.append(
            {
                "evaluation_object_id": _stable_id(
                    "evaluation",
                    source["project_id"],
                    source["structure_revision_id"],
                    module_id,
                    question_id,
                ),
                "module_id": module_id,
                "parent_evaluation_object_id": None,
                "object_type": "concept",
                "display_name": _text(question.get("canonical_text")) or "未命名被测对象",
                "display_order": counters[module_id],
                "main_question_ids": [question_id],
                "occurrence_ids": sorted(
                    {
                        _text(value)
                        for value in question.get("occurrence_ids") or []
                        if _text(value)
                    }
                ),
                "supersedes_evaluation_object_ids": [],
                "decision_status": "proposed",
                "decision_source": "deterministic_rule",
            }
        )
    return result


def _observed_rows_by_sheet(
    structure: dict[str, Any], evidence: dict[str, Any]
) -> dict[str, list[int]]:
    rows_by_sheet: dict[str, list[int]] = {}
    for occurrence in structure.get("occurrences") or []:
        sheet_id = _text(occurrence.get("sheet_id"))
        row = occurrence.get("row")
        if not sheet_id or isinstance(row, bool) or not isinstance(row, int) or row < 1:
            continue
        rows_by_sheet.setdefault(sheet_id, []).append(row)
    for entry in evidence.get("entries") or []:
        sheet_id = _text(entry.get("sheet_id"))
        row = entry.get("row")
        if not sheet_id or isinstance(row, bool) or not isinstance(row, int) or row < 1:
            continue
        rows_by_sheet.setdefault(sheet_id, []).append(row)
    return {
        sheet_id: sorted(set(rows))
        for sheet_id, rows in rows_by_sheet.items()
        if rows
    }


def _proposal_source_rules(
    structure: dict[str, Any], evidence: dict[str, Any], source: dict[str, str]
) -> list[dict[str, Any]]:
    rows_by_sheet = _observed_rows_by_sheet(structure, evidence)
    group_by_sheet: dict[str, set[str]] = {}
    for collection in (
        structure.get("occurrences") or [],
        evidence.get("entries") or [],
    ):
        for item in collection:
            sheet_id = _text(item.get("sheet_id"))
            group_id = _text(item.get("group_id"))
            if sheet_id and group_id:
                group_by_sheet.setdefault(sheet_id, set()).add(group_id)
    result: list[dict[str, Any]] = []
    for order, sheet_id in enumerate(sorted(rows_by_sheet), 1):
        rows = rows_by_sheet[sheet_id]
        start_row, end_row = min(rows), max(rows)
        groups = sorted(group_by_sheet.get(sheet_id, set()))
        result.append(
            {
                "source_scope_rule_id": _stable_id(
                    "scope",
                    source["project_id"],
                    source["structure_revision_id"],
                    sheet_id,
                    start_row,
                    end_row,
                ),
                "group_id": groups[0] if len(groups) == 1 else None,
                "sheet_id": sheet_id,
                "start_row": start_row,
                "end_row": end_row,
                "scope_type": "interview_body",
                "allowed_split_rows": rows[1:],
                "display_order": order,
                "decision_status": "proposed",
                "decision_source": "deterministic_rule",
            }
        )
    return result


def build_analysis_boundary_proposal(
    structure: dict[str, Any],
    evidence: dict[str, Any],
    *,
    project_id: str,
    import_id: str,
    structure_revision_id: str,
    evidence_revision_id: str,
) -> dict[str, Any]:
    """Create a deterministic, non-persisting first boundary proposal."""

    source = _source(
        structure,
        evidence,
        project_id=project_id,
        import_id=import_id,
        structure_revision_id=structure_revision_id,
        evidence_revision_id=evidence_revision_id,
    )
    boundary = {
        "analysis_boundary_schema_version": ANALYSIS_BOUNDARY_SCHEMA_VERSION,
        "source": source,
        "status": "draft",
        "evaluation_objects": _proposal_objects(structure, source),
        "source_scope_rules": _proposal_source_rules(structure, evidence, source),
        "label_scope_rules": [],
    }
    canonical = validate_analysis_boundary(
        boundary,
        structure,
        evidence,
        project_id=project_id,
        import_id=import_id,
        structure_revision_id=structure_revision_id,
        evidence_revision_id=evidence_revision_id,
    )
    return {
        "analysis_boundary": canonical,
        "coverage_preview": build_coverage_preview(structure, evidence, canonical),
    }


def _normalize_evaluation_objects(
    raw_objects: object,
    *,
    modules: dict[str, dict],
    questions: dict[str, dict],
    occurrences: dict[str, dict],
    boundary_status: str,
) -> list[dict[str, Any]]:
    if not isinstance(raw_objects, list):
        raise _error("ANALYSIS_BOUNDARY_INPUT_INVALID", "evaluation_objects 必须是列表。")
    objects: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_objects:
        if not isinstance(raw, dict):
            raise _error("ANALYSIS_BOUNDARY_INPUT_INVALID", "被测对象必须是对象。")
        object_id = _require_id(
            "evaluation", raw.get("evaluation_object_id"), "evaluation_object_id"
        )
        if object_id in seen_ids:
            raise _error(
                "ANALYSIS_BOUNDARY_INPUT_INVALID",
                "被测对象标识重复。",
                {"evaluation_object_id": object_id},
            )
        seen_ids.add(object_id)
        module_id = _text(raw.get("module_id"))
        if module_id not in modules:
            raise _error(
                "EVALUATION_OBJECT_BINDING_INVALID",
                "被测对象引用了不存在的模块。",
                {"evaluation_object_id": object_id},
            )
        object_type = _text(raw.get("object_type"))
        if object_type not in {"concept", "variant"}:
            raise _error("ANALYSIS_BOUNDARY_INPUT_INVALID", "被测对象类型无效。")
        parent_id = _text(raw.get("parent_evaluation_object_id")) or None
        if parent_id is not None and not _ID_PATTERNS["evaluation"].fullmatch(parent_id):
            raise _error("ANALYSIS_BOUNDARY_INPUT_INVALID", "父被测对象标识无效。")
        display_name = _text(raw.get("display_name"))
        if not display_name or len(display_name) > 300:
            raise _error("ANALYSIS_BOUNDARY_INPUT_INVALID", "被测对象名称不能为空或过长。")
        display_order = raw.get("display_order")
        if isinstance(display_order, bool) or not isinstance(display_order, int) or display_order < 1:
            raise _error("ANALYSIS_BOUNDARY_INPUT_INVALID", "被测对象顺序必须为正整数。")
        question_ids = _unique_ids(
            raw.get("main_question_ids", []),
            pattern=re.compile(r"^question_[0-9a-f]{32}$"),
            field="main_question_ids",
        )
        occurrence_ids = _unique_ids(
            raw.get("occurrence_ids", []),
            pattern=re.compile(r"^occ_[0-9a-f]{32}$"),
            field="occurrence_ids",
        )
        supersedes = _unique_ids(
            raw.get("supersedes_evaluation_object_ids", []),
            pattern=_ID_PATTERNS["evaluation"],
            field="supersedes_evaluation_object_ids",
        )
        if object_id in supersedes:
            raise _error("EVALUATION_OBJECT_LINEAGE_INVALID", "被测对象不能替代自身。")
        decision_status = _text(raw.get("decision_status")) or "draft"
        decision_source = _text(raw.get("decision_source")) or "user_selection"
        if decision_status not in _DECISION_STATUSES:
            raise _error("ANALYSIS_BOUNDARY_INPUT_INVALID", "被测对象确认状态无效。")
        if decision_source not in _DECISION_SOURCES:
            raise _error("ANALYSIS_BOUNDARY_INPUT_INVALID", "被测对象决策来源无效。")
        if boundary_status == "confirmed" and decision_status not in {
            "confirmed",
            "superseded",
        }:
            raise _error(
                "ANALYSIS_BOUNDARY_NOT_CONFIRMABLE",
                "已确认边界仍包含未确认的被测对象。",
                {"evaluation_object_id": object_id},
            )
        if decision_status != "superseded" and (not question_ids or not occurrence_ids):
            raise _error(
                "EVALUATION_OBJECT_BINDING_INVALID",
                "有效被测对象必须绑定主问题和真实 occurrence。",
                {"evaluation_object_id": object_id},
            )
        for question_id in question_ids:
            question = questions.get(question_id)
            if question is None or _text(question.get("module_id")) != module_id:
                raise _error(
                    "EVALUATION_OBJECT_BINDING_INVALID",
                    "被测对象绑定的主问题不属于该模块。",
                    {"evaluation_object_id": object_id, "main_question_id": question_id},
                )
        for occurrence_id in occurrence_ids:
            occurrence = occurrences.get(occurrence_id)
            if occurrence is None:
                raise _error(
                    "EVALUATION_OBJECT_BINDING_INVALID",
                    "被测对象绑定了不存在的 occurrence。",
                    {"evaluation_object_id": object_id, "occurrence_id": occurrence_id},
                )
            if (
                _text(occurrence.get("canonical_module_id")) != module_id
                or _text(occurrence.get("canonical_main_question_id")) not in question_ids
            ):
                raise _error(
                    "EVALUATION_OBJECT_BINDING_INVALID",
                    "被测对象的 occurrence 与模块或主问题不一致。",
                    {"evaluation_object_id": object_id, "occurrence_id": occurrence_id},
                )
        objects.append(
            {
                "evaluation_object_id": object_id,
                "module_id": module_id,
                "parent_evaluation_object_id": parent_id,
                "object_type": object_type,
                "display_name": display_name,
                "display_order": display_order,
                "main_question_ids": question_ids,
                "occurrence_ids": occurrence_ids,
                "supersedes_evaluation_object_ids": supersedes,
                "decision_status": decision_status,
                "decision_source": decision_source,
            }
        )
    by_id = {item["evaluation_object_id"]: item for item in objects}
    for item in objects:
        parent_id = item["parent_evaluation_object_id"]
        if item["object_type"] == "concept" and parent_id is not None:
            raise _error("EVALUATION_OBJECT_HIERARCHY_INVALID", "概念对象不能有父对象。")
        if item["object_type"] == "variant":
            parent = by_id.get(parent_id or "")
            if (
                parent is None
                or parent["object_type"] != "concept"
                or parent["module_id"] != item["module_id"]
                or parent["decision_status"] == "superseded"
            ):
                raise _error(
                    "EVALUATION_OBJECT_HIERARCHY_INVALID",
                    "variant 必须绑定同模块的有效 concept 父对象。",
                    {"evaluation_object_id": item["evaluation_object_id"]},
                )
    claimed_occurrences: dict[str, str] = {}
    for item in objects:
        if item["decision_status"] == "superseded":
            continue
        actual_question_ids = {
            _text(occurrences[occurrence_id].get("canonical_main_question_id"))
            for occurrence_id in item["occurrence_ids"]
        }
        if actual_question_ids != set(item["main_question_ids"]):
            raise _error(
                "EVALUATION_OBJECT_BINDING_INVALID",
                "被测对象声明的主问题必须与其 occurrence 实际问题完全一致。",
                {"evaluation_object_id": item["evaluation_object_id"]},
            )
        for occurrence_id in item["occurrence_ids"]:
            existing_object_id = claimed_occurrences.get(occurrence_id)
            if existing_object_id is not None:
                raise _error(
                    "EVALUATION_OBJECT_OCCURRENCE_CONFLICT",
                    "同一 occurrence 不能同时绑定多个有效被测对象。",
                    {
                        "occurrence_id": occurrence_id,
                        "evaluation_object_ids": sorted(
                            [existing_object_id, item["evaluation_object_id"]]
                        ),
                    },
                )
            claimed_occurrences[occurrence_id] = item["evaluation_object_id"]
    return sorted(
        objects,
        key=lambda item: (
            item["module_id"],
            item["parent_evaluation_object_id"] or "",
            item["display_order"],
            item["evaluation_object_id"],
        ),
    )


def _normalize_source_rules(
    raw_rules: object,
    *,
    observed_rows_by_sheet: dict[str, list[int]],
    boundary_status: str,
) -> list[dict[str, Any]]:
    if not isinstance(raw_rules, list):
        raise _error("ANALYSIS_BOUNDARY_INPUT_INVALID", "source_scope_rules 必须是列表。")
    rules: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_rules:
        if not isinstance(raw, dict):
            raise _error("ANALYSIS_BOUNDARY_INPUT_INVALID", "来源范围规则必须是对象。")
        rule_id = _require_id("scope", raw.get("source_scope_rule_id"), "source_scope_rule_id")
        if rule_id in seen_ids:
            raise _error("SOURCE_SCOPE_INVALID", "来源范围规则标识重复。")
        seen_ids.add(rule_id)
        sheet_id = _text(raw.get("sheet_id"))
        if not sheet_id or sheet_id not in observed_rows_by_sheet:
            raise _error("SOURCE_SCOPE_INVALID", "来源范围规则引用了未知 Sheet。")
        group_id = _text(raw.get("group_id")) or None
        start_row, end_row = raw.get("start_row"), raw.get("end_row")
        if (
            isinstance(start_row, bool)
            or isinstance(end_row, bool)
            or not isinstance(start_row, int)
            or not isinstance(end_row, int)
            or start_row < 1
            or end_row < start_row
            or end_row > 1_048_576
        ):
            raise _error("SOURCE_SCOPE_INVALID", "来源范围必须是有效且有序的 Excel 行区间。")
        scope_type = _text(raw.get("scope_type"))
        if scope_type not in _SOURCE_TYPES:
            raise _error("SOURCE_SCOPE_INVALID", "来源范围类型无效。")
        display_order = raw.get("display_order")
        if isinstance(display_order, bool) or not isinstance(display_order, int) or display_order < 1:
            raise _error("SOURCE_SCOPE_INVALID", "来源范围顺序必须为正整数。")
        decision_status = _text(raw.get("decision_status")) or "draft"
        decision_source = _text(raw.get("decision_source")) or "user_selection"
        if decision_status not in _DECISION_STATUSES - {"superseded"}:
            raise _error("SOURCE_SCOPE_INVALID", "来源范围确认状态无效。")
        if decision_source not in _DECISION_SOURCES:
            raise _error("SOURCE_SCOPE_INVALID", "来源范围决策来源无效。")
        if boundary_status == "confirmed" and decision_status != "confirmed":
            raise _error("ANALYSIS_BOUNDARY_NOT_CONFIRMABLE", "已确认边界仍包含未确认来源范围。")
        rules.append(
            {
                "source_scope_rule_id": rule_id,
                "group_id": group_id,
                "sheet_id": sheet_id,
                "start_row": start_row,
                "end_row": end_row,
                "scope_type": scope_type,
                # This is derived below from frozen physical rows.  A client
                # value with this name is intentionally never trusted.
                "allowed_split_rows": [],
                "display_order": display_order,
                "decision_status": decision_status,
                "decision_source": decision_source,
            }
        )
    by_sheet: dict[str, list[dict[str, Any]]] = {}
    for rule in rules:
        by_sheet.setdefault(rule["sheet_id"], []).append(rule)
    for sheet_id, sheet_rules in by_sheet.items():
        ordered = sorted(sheet_rules, key=lambda item: (item["start_row"], item["end_row"]))
        observed_rows = observed_rows_by_sheet[sheet_id]
        observed_start, observed_end = observed_rows[0], observed_rows[-1]
        allowed_splits = set(observed_rows[1:])
        if (
            ordered[0]["start_row"] != observed_start
            or ordered[-1]["end_row"] != observed_end
        ):
            raise _error(
                "SOURCE_SCOPE_COVERAGE_INVALID",
                "来源范围必须完整覆盖该 Sheet 的已观察行区间。",
                {
                    "sheet_id": sheet_id,
                    "observed_start_row": observed_start,
                    "observed_end_row": observed_end,
                },
            )
        for previous, current in zip(ordered, ordered[1:]):
            if current["start_row"] <= previous["end_row"]:
                raise _error(
                    "SOURCE_SCOPE_OVERLAP",
                    "同一 Sheet 的来源范围不能重叠。",
                    {
                        "sheet_id": sheet_id,
                        "source_scope_rule_ids": [
                            previous["source_scope_rule_id"],
                            current["source_scope_rule_id"],
                        ],
                    },
                )
            if current["start_row"] != previous["end_row"] + 1:
                raise _error(
                    "SOURCE_SCOPE_GAP",
                    "同一 Sheet 的来源范围必须连续且不能留空洞。",
                    {"sheet_id": sheet_id},
                )
            if current["start_row"] not in allowed_splits:
                raise _error(
                    "SOURCE_SCOPE_SPLIT_UNSAFE",
                    "来源范围只能在结构或证据实际出现的安全行边界切分。",
                    {"sheet_id": sheet_id, "split_row": current["start_row"]},
                )
        for rule in ordered:
            rule["allowed_split_rows"] = sorted(
                split_row
                for split_row in allowed_splits
                if rule["start_row"] < split_row <= rule["end_row"]
            )
    missing_sheet_ids = sorted(set(observed_rows_by_sheet) - set(by_sheet))
    if missing_sheet_ids:
        raise _error(
            "SOURCE_SCOPE_COVERAGE_INVALID",
            "每个含结构或证据的 Sheet 都必须有完整来源范围。",
            {"sheet_ids": missing_sheet_ids},
        )
    return sorted(
        rules,
        key=lambda item: (
            item["sheet_id"],
            item["start_row"],
            item["end_row"],
            item["source_scope_rule_id"],
        ),
    )


def _normalize_label_rules(
    raw_rules: object,
    *,
    modules: dict[str, dict],
    objects: list[dict[str, Any]],
    boundary_status: str,
) -> list[dict[str, Any]]:
    if not isinstance(raw_rules, list):
        raise _error("ANALYSIS_BOUNDARY_INPUT_INVALID", "label_scope_rules 必须是列表。")
    active_object_ids = {
        item["evaluation_object_id"]
        for item in objects
        if item["decision_status"] != "superseded"
    }
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    for raw in raw_rules:
        if not isinstance(raw, dict):
            raise _error("ANALYSIS_BOUNDARY_INPUT_INVALID", "标签作用域规则必须是对象。")
        rule_id = _require_id("label_scope", raw.get("label_scope_rule_id"), "label_scope_rule_id")
        label_key = _text(raw.get("label_key"))
        label_name = _text(raw.get("label_name"))
        if rule_id in seen_ids or not label_key or label_key in seen_keys:
            raise _error("LABEL_SCOPE_INVALID", "标签规则标识或标签键缺失、重复。")
        if len(label_key) > 200 or not label_name or len(label_name) > 300:
            raise _error("LABEL_SCOPE_INVALID", "标签键或显示名称为空或过长。")
        seen_ids.add(rule_id)
        seen_keys.add(label_key)
        mode = _text(raw.get("scope_mode"))
        if mode not in _LABEL_SCOPE_MODES:
            raise _error("LABEL_SCOPE_INVALID", "标签作用域必须明确为四种允许状态之一。")
        module_ids = _unique_ids(
            raw.get("module_ids", []),
            pattern=re.compile(r"^module_[0-9a-f]{32}$"),
            field="module_ids",
        )
        object_ids = _unique_ids(
            raw.get("evaluation_object_ids", []),
            pattern=_ID_PATTERNS["evaluation"],
            field="evaluation_object_ids",
        )
        if any(module_id not in modules for module_id in module_ids):
            raise _error("LABEL_SCOPE_INVALID", "标签规则引用了不存在的模块。")
        if any(object_id not in active_object_ids for object_id in object_ids):
            raise _error("LABEL_SCOPE_INVALID", "标签规则引用了不存在或已替代的被测对象。")
        valid_shape = (
            (mode in {"disabled", "all_analysis"} and not module_ids and not object_ids)
            or (mode == "selected_modules" and bool(module_ids) and not object_ids)
            or (
                mode == "selected_evaluation_objects"
                and bool(object_ids)
                and not module_ids
            )
        )
        if not valid_shape:
            raise _error("LABEL_SCOPE_INVALID", "标签作用域与目标列表不一致。")
        decision_status = _text(raw.get("decision_status")) or "draft"
        decision_source = _text(raw.get("decision_source")) or "user_selection"
        if decision_status not in _DECISION_STATUSES - {"superseded"}:
            raise _error("LABEL_SCOPE_INVALID", "标签作用域确认状态无效。")
        if decision_source not in _DECISION_SOURCES:
            raise _error("LABEL_SCOPE_INVALID", "标签作用域决策来源无效。")
        if boundary_status == "confirmed" and decision_status != "confirmed":
            raise _error("ANALYSIS_BOUNDARY_NOT_CONFIRMABLE", "已确认边界仍包含未确认标签作用域。")
        result.append(
            {
                "label_scope_rule_id": rule_id,
                "label_key": label_key,
                "label_name": label_name,
                "scope_mode": mode,
                "module_ids": module_ids,
                "evaluation_object_ids": object_ids,
                "decision_status": decision_status,
                "decision_source": decision_source,
            }
        )
    return sorted(result, key=lambda item: (item["label_key"], item["label_scope_rule_id"]))


def validate_analysis_boundary(
    boundary: dict[str, Any],
    structure: dict[str, Any],
    evidence: dict[str, Any],
    *,
    project_id: str,
    import_id: str,
    structure_revision_id: str,
    evidence_revision_id: str,
    base_boundary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and canonicalize an editable boundary without persisting it."""

    if not isinstance(boundary, dict):
        raise _error("ANALYSIS_BOUNDARY_INPUT_INVALID", "分析边界必须是对象。")
    _ensure_unicode_scalars(boundary)
    expected_source = _source(
        structure,
        evidence,
        project_id=project_id,
        import_id=import_id,
        structure_revision_id=structure_revision_id,
        evidence_revision_id=evidence_revision_id,
    )
    supplied_source = boundary.get("source") or {}
    for field in (
        "project_id",
        "import_id",
        "structure_revision_id",
        "evidence_revision_id",
    ):
        supplied = _text(supplied_source.get(field))
        if supplied and supplied != expected_source[field]:
            raise _error(
                "ANALYSIS_BOUNDARY_SOURCE_MISMATCH",
                "分析边界引用了不一致的上游版本。",
                {"field": field},
            )
    status = _text(boundary.get("status")) or "draft"
    if status not in _BOUNDARY_STATUSES:
        raise _error("ANALYSIS_BOUNDARY_INPUT_INVALID", "分析边界状态无效。")
    modules, questions, occurrences = _indexes(structure)
    objects = _normalize_evaluation_objects(
        boundary.get("evaluation_objects", []),
        modules=modules,
        questions=questions,
        occurrences=occurrences,
        boundary_status=status,
    )
    observed_rows_by_sheet = _observed_rows_by_sheet(structure, evidence)
    source_rules = _normalize_source_rules(
        boundary.get("source_scope_rules", []),
        observed_rows_by_sheet=observed_rows_by_sheet,
        boundary_status=status,
    )
    label_rules = _normalize_label_rules(
        boundary.get("label_scope_rules", []),
        modules=modules,
        objects=objects,
        boundary_status=status,
    )
    canonical = {
        "analysis_boundary_schema_version": ANALYSIS_BOUNDARY_SCHEMA_VERSION,
        "source": expected_source,
        "status": status,
        "evaluation_objects": objects,
        "source_scope_rules": source_rules,
        "label_scope_rules": label_rules,
    }
    if base_boundary is not None:
        base = validate_analysis_boundary(
            base_boundary,
            structure,
            evidence,
            project_id=project_id,
            import_id=import_id,
            structure_revision_id=structure_revision_id,
            evidence_revision_id=evidence_revision_id,
            base_boundary=None,
        )
        _validate_object_lineage(canonical, base)
    return canonical


def _validate_object_lineage(
    boundary: dict[str, Any], base_boundary: dict[str, Any]
) -> None:
    current_by_id = {
        item["evaluation_object_id"]: item
        for item in boundary["evaluation_objects"]
    }
    base_by_id = {
        item["evaluation_object_id"]: item
        for item in base_boundary["evaluation_objects"]
    }
    missing_ids = sorted(set(base_by_id) - set(current_by_id))
    if missing_ids:
        raise _error(
            "EVALUATION_OBJECT_LINEAGE_INVALID",
            "已有被测对象不能被删除；替换时必须保留为 superseded。",
            {"evaluation_object_ids": missing_ids},
        )
    immutable_fields = (
        "module_id",
        "parent_evaluation_object_id",
        "object_type",
        "main_question_ids",
        "occurrence_ids",
        "supersedes_evaluation_object_ids",
    )
    for object_id, base_item in base_by_id.items():
        current_item = current_by_id[object_id]
        changed = [
            field
            for field in immutable_fields
            if current_item.get(field) != base_item.get(field)
        ]
        if changed:
            raise _error(
                "EVALUATION_OBJECT_IDENTITY_REUSE",
                "已有被测对象的结构身份发生变化，必须创建新 ID。",
                {
                    "evaluation_object_id": object_id,
                    "changed_fields": changed,
                },
            )
        if (
            base_item["decision_status"] == "superseded"
            and current_item["decision_status"] != "superseded"
        ):
            raise _error(
                "EVALUATION_OBJECT_LINEAGE_INVALID",
                "已被替代的对象不能在后续版本中直接恢复为有效对象。",
                {"evaluation_object_id": object_id},
            )
    base_active_ids = {
        object_id
        for object_id, item in base_by_id.items()
        if item["decision_status"] != "superseded"
    }
    new_items = [
        item
        for object_id, item in current_by_id.items()
        if object_id not in base_by_id
    ]
    referenced_base_ids: set[str] = set()
    for item in new_items:
        supersedes = set(item["supersedes_evaluation_object_ids"])
        if item["decision_status"] == "superseded" or not supersedes:
            raise _error(
                "EVALUATION_OBJECT_LINEAGE_INVALID",
                "新增被测对象必须是有效对象并显式替代上一版对象。",
                {"evaluation_object_id": item["evaluation_object_id"]},
            )
        if not supersedes <= base_active_ids:
            raise _error(
                "EVALUATION_OBJECT_LINEAGE_INVALID",
                "新增被测对象的 supersedes 只能引用上一版有效对象。",
                {"evaluation_object_id": item["evaluation_object_id"]},
            )
        referenced_base_ids.update(supersedes)
    for object_id in base_active_ids:
        is_superseded_now = (
            current_by_id[object_id]["decision_status"] == "superseded"
        )
        is_referenced = object_id in referenced_base_ids
        if is_superseded_now != is_referenced:
            raise _error(
                "EVALUATION_OBJECT_LINEAGE_INVALID",
                "替换关系必须同时保留旧对象为 superseded 并由新对象追溯引用。",
                {"evaluation_object_id": object_id},
            )


def _scope_for(
    rules_by_sheet: dict[str, list[dict[str, Any]]], sheet_id: str, row: int
) -> str | None:
    for rule in rules_by_sheet.get(sheet_id, []):
        if rule["start_row"] <= row <= rule["end_row"]:
            return rule["scope_type"]
    return None


def _assert_confirmable(
    boundary: dict[str, Any], structure: dict[str, Any], evidence: dict[str, Any]
) -> None:
    active = [
        item
        for item in boundary["evaluation_objects"]
        if item["decision_status"] != "superseded"
    ]
    occurrence_counts: dict[str, int] = {}
    for item in active:
        for occurrence_id in item["occurrence_ids"]:
            occurrence_counts[occurrence_id] = occurrence_counts.get(occurrence_id, 0) + 1
    required_occurrence_ids = {
        _text(item.get("occurrence_id"))
        for item in structure.get("occurrences") or []
        if _text(item.get("canonical_main_question_id"))
        and _text(item.get("occurrence_id"))
    }
    invalid_occurrences = sorted(
        occurrence_id
        for occurrence_id in required_occurrence_ids
        if occurrence_counts.get(occurrence_id, 0) != 1
    )
    if invalid_occurrences:
        raise _error(
            "ANALYSIS_BOUNDARY_NOT_CONFIRMABLE",
            "每个具有标准主问题的 structure occurrence 必须恰好绑定一次。",
            {"occurrence_ids": invalid_occurrences},
        )
    rules_by_sheet: dict[str, list[dict[str, Any]]] = {}
    for rule in boundary["source_scope_rules"]:
        rules_by_sheet.setdefault(rule["sheet_id"], []).append(rule)
    unscoped_evidence_ids: list[str] = []
    for entry in evidence.get("entries") or []:
        if entry.get("inclusion_status") != "included":
            continue
        sheet_id = _text(entry.get("sheet_id"))
        row = entry.get("row")
        if (
            not isinstance(row, int)
            or isinstance(row, bool)
            or _scope_for(rules_by_sheet, sheet_id, row) is None
        ):
            evidence_id = _text(entry.get("evidence_id"))
            if evidence_id:
                unscoped_evidence_ids.append(evidence_id)
    if unscoped_evidence_ids:
        raise _error(
            "ANALYSIS_BOUNDARY_NOT_CONFIRMABLE",
            "仍有纳入证据未确认来源范围。",
            {"evidence_ids": sorted(unscoped_evidence_ids)},
        )


def confirm_analysis_boundary(
    boundary: dict[str, Any],
    structure: dict[str, Any],
    evidence: dict[str, Any],
    *,
    project_id: str,
    import_id: str,
    structure_revision_id: str,
    evidence_revision_id: str,
) -> dict[str, Any]:
    """Confirm a reviewed boundary while preserving object IDs and lineage."""

    draft = deepcopy(boundary)
    draft["status"] = "draft"
    canonical = validate_analysis_boundary(
        draft,
        structure,
        evidence,
        project_id=project_id,
        import_id=import_id,
        structure_revision_id=structure_revision_id,
        evidence_revision_id=evidence_revision_id,
    )
    _assert_confirmable(canonical, structure, evidence)
    canonical["status"] = "confirmed"
    for collection in (
        "evaluation_objects",
        "source_scope_rules",
        "label_scope_rules",
    ):
        for item in canonical[collection]:
            if item.get("decision_status") != "superseded":
                item["decision_status"] = "confirmed"
                if item.get("decision_source") == "deterministic_rule":
                    item["decision_source"] = "user_selection"
    return validate_analysis_boundary(
        canonical,
        structure,
        evidence,
        project_id=project_id,
        import_id=import_id,
        structure_revision_id=structure_revision_id,
        evidence_revision_id=evidence_revision_id,
    )


def build_coverage_preview(
    structure: dict[str, Any],
    evidence: dict[str, Any],
    boundary: dict[str, Any],
) -> dict[str, Any]:
    """Build participant × object × question coverage without blank inference."""

    _ensure_unicode_scalars(boundary)
    source = boundary.get("source") or {}
    project_id = _require_id("project", source.get("project_id"), "project_id")
    structure_revision_id = _require_id(
        "structure", source.get("structure_revision_id"), "structure_revision_id"
    )
    evidence_revision_id = _require_id(
        "evidence", source.get("evidence_revision_id"), "evidence_revision_id"
    )
    _modules, _questions, occurrences = _indexes(structure)
    participants: list[dict[str, str]] = []
    seen_participants: set[str] = set()
    for raw in evidence.get("expected_participants") or []:
        participant_id = _text(raw.get("participant_id"))
        group_id = _text(raw.get("group_id"))
        if not re.fullmatch(r"^participant_[0-9a-f]{32}$", participant_id):
            raise _error("ANALYSIS_BOUNDARY_SOURCE_INVALID", "证据版本包含无效玩家标识。")
        if participant_id in seen_participants:
            raise _error("ANALYSIS_BOUNDARY_SOURCE_INVALID", "证据版本重复声明同一玩家。")
        seen_participants.add(participant_id)
        participants.append({"participant_id": participant_id, "group_id": group_id})
    participants.sort(key=lambda item: (item["group_id"], item["participant_id"]))
    rules_by_sheet: dict[str, list[dict[str, Any]]] = {}
    for rule in boundary.get("source_scope_rules") or []:
        rules_by_sheet.setdefault(_text(rule.get("sheet_id")), []).append(rule)
    scoped_entries: list[dict[str, Any]] = []
    for entry in evidence.get("entries") or []:
        row = entry.get("row")
        if (
            entry.get("inclusion_status") != "included"
            or entry.get("identity_decision_status") not in _REPORTABLE_IDENTITY_STATUSES
            or isinstance(row, bool)
            or not isinstance(row, int)
            or _scope_for(rules_by_sheet, _text(entry.get("sheet_id")), row)
            != "interview_body"
        ):
            continue
        scoped_entries.append(entry)
    entries_by_participant: dict[str, list[dict[str, Any]]] = {}
    for entry in scoped_entries:
        entries_by_participant.setdefault(_text(entry.get("participant_id")), []).append(entry)
    active_objects = [
        item
        for item in boundary.get("evaluation_objects") or []
        if item.get("decision_status") != "superseded"
    ]
    rows: list[dict[str, Any]] = []
    boundary_confirmed = boundary.get("status") == "confirmed"
    for obj in active_objects:
        object_occurrence_ids = set(obj.get("occurrence_ids") or [])
        for question_id in obj.get("main_question_ids") or []:
            question_occurrences = {
                occurrence_id: occurrences[occurrence_id]
                for occurrence_id in object_occurrence_ids
                if occurrence_id in occurrences
                and _text(occurrences[occurrence_id].get("canonical_main_question_id"))
                == question_id
            }
            for participant in participants:
                participant_id = participant["participant_id"]
                candidate_occurrence_ids = sorted(
                    occurrence_id
                    for occurrence_id, occurrence in question_occurrences.items()
                    if _text(occurrence.get("group_id")) == participant["group_id"]
                    and _scope_for(
                        rules_by_sheet,
                        _text(occurrence.get("sheet_id")),
                        int(occurrence.get("row") or 0),
                    )
                    == "interview_body"
                )
                # Orthogonal coverage is scoped to the interview body actually
                # available to this participant's group.  A background-only or
                # excluded object/question is outside the matrix, not no_record.
                if not candidate_occurrence_ids:
                    continue
                candidate_occurrence_id_set = set(candidate_occurrence_ids)
                participant_entries = [
                    entry
                    for entry in entries_by_participant.get(participant_id, [])
                    if _text(entry.get("occurrence_id"))
                    in candidate_occurrence_id_set
                    and _text(entry.get("main_question_id")) == question_id
                ]
                self_reports = [
                    entry
                    for entry in participant_entries
                    if entry.get("evidence_type") == "participant_self_report"
                ]
                observations = [
                    entry
                    for entry in participant_entries
                    if entry.get("evidence_type") == "researcher_observation"
                ]
                follow_up_count = sum(
                    1
                    for entry in self_reports
                    if (question_occurrences.get(_text(entry.get("occurrence_id"))) or {}).get("row_role")
                    == "follow_up"
                )
                if self_reports:
                    source_presence = "present"
                    asked_status = "asked"
                    applicability = "applicable"
                    derived_status = "answered"
                    review_status = "system_verified" if boundary_confirmed else "proposed"
                elif observations:
                    source_presence = "present"
                    asked_status = "unknown"
                    applicability = "unknown"
                    derived_status = "observation_only"
                    review_status = "needs_review" if boundary_confirmed else "proposed"
                else:
                    source_presence = "absent"
                    asked_status = "unknown"
                    applicability = "unknown"
                    derived_status = "no_record"
                    review_status = "needs_review" if boundary_confirmed else "proposed"
                rows.append(
                    {
                        "coverage_id": _stable_id(
                            "coverage",
                            project_id,
                            structure_revision_id,
                            evidence_revision_id,
                            participant_id,
                            obj["evaluation_object_id"],
                            question_id,
                        ),
                        "participant_id": participant_id,
                        "group_id": participant["group_id"],
                        "evaluation_object_id": obj["evaluation_object_id"],
                        "module_id": obj["module_id"],
                        "main_question_id": question_id,
                        "source_presence": source_presence,
                        "asked_status": asked_status,
                        "applicability": applicability,
                        "self_report_count": len(self_reports),
                        "follow_up_count": follow_up_count,
                        "observation_count": len(observations),
                        "review_status": review_status,
                        "derived_status": derived_status,
                        "source_occurrence_ids": candidate_occurrence_ids,
                        "self_report_evidence_ids": sorted(
                            _text(entry.get("evidence_id")) for entry in self_reports
                        ),
                        "observation_evidence_ids": sorted(
                            _text(entry.get("evidence_id")) for entry in observations
                        ),
                    }
                )
    rows.sort(
        key=lambda item: (
            item["module_id"],
            item["evaluation_object_id"],
            item["main_question_id"],
            item["group_id"],
            item["participant_id"],
        )
    )
    summaries: list[dict[str, Any]] = []
    summary_keys = sorted(
        {
            (row["module_id"], row["evaluation_object_id"], row["main_question_id"])
            for row in rows
        }
    )
    for module_id, object_id, question_id in summary_keys:
        scoped_rows = [
            row
            for row in rows
            if row["evaluation_object_id"] == object_id
            and row["main_question_id"] == question_id
        ]
        denominator_reliable = bool(scoped_rows) and all(
            row["asked_status"] in {"asked", "not_asked"}
            and row["applicability"] in {"applicable", "not_applicable"}
            and row["review_status"] in {"system_verified", "confirmed"}
            for row in scoped_rows
        )
        denominator = (
            sum(row["applicability"] == "applicable" for row in scoped_rows)
            if denominator_reliable
            else None
        )
        covered = sum(row["self_report_count"] > 0 for row in scoped_rows)
        proportion = (
            covered / denominator
            if denominator_reliable and denominator
            else (0.0 if denominator_reliable and denominator == 0 else None)
        )
        summaries.append(
            {
                "module_id": module_id,
                "evaluation_object_id": object_id,
                "main_question_id": question_id,
                "participant_count": len(scoped_rows),
                "covered_participant_count": covered,
                "observation_only_participant_count": sum(
                    row["derived_status"] == "observation_only" for row in scoped_rows
                ),
                "no_record_participant_count": sum(
                    row["derived_status"] == "no_record" for row in scoped_rows
                ),
                "denominator_reliable": denominator_reliable,
                "denominator_participant_count": denominator,
                "proportion": proportion,
            }
        )
    return {
        "coverage_schema_version": COVERAGE_SCHEMA_VERSION,
        "source": {
            "project_id": project_id,
            "import_id": _text(source.get("import_id")),
            "structure_revision_id": structure_revision_id,
            "evidence_revision_id": evidence_revision_id,
            "analysis_boundary_sha256": canonical_json_sha256(boundary),
            "rules_version": COVERAGE_RULES_VERSION,
        },
        "participant_count": len(participants),
        "row_count": len(rows),
        "rows": rows,
        "summaries": summaries,
    }


__all__ = [
    "ANALYSIS_BOUNDARY_RULES_VERSION",
    "ANALYSIS_BOUNDARY_SCHEMA_VERSION",
    "COVERAGE_RULES_VERSION",
    "COVERAGE_SCHEMA_VERSION",
    "InterviewV2AnalysisBoundaryError",
    "build_analysis_boundary_proposal",
    "build_coverage_preview",
    "canonical_json_sha256",
    "confirm_analysis_boundary",
    "validate_analysis_boundary",
]
