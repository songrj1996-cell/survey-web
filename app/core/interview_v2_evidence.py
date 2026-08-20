"""Deterministic evidence construction and typed review overrides for V2."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import re
import unicodedata
from typing import Any

from app.core.interview_v2_structure import (
    InterviewV2StructureError,
    _question_key,
    _stable_id,
    _text,
    build_structure,
)


EVIDENCE_SCHEMA_VERSION = "interview-evidence/1.0"
EVIDENCE_RULES_VERSION = "interview-v2-evidence-rules/1.0"
EVIDENCE_POLICY_VERSION = "interview-evidence-policy/1.0"

_RESOLUTION_ACTIONS = {
    "assign_row_role",
    "assign_module",
    "assign_main_question",
    "set_evidence_identity",
    "exclude_evidence",
    "accept_suggestion",
}
_ROW_ROLES = {
    "module_header",
    "main_question",
    "follow_up",
    "observation_row",
    "unknown",
}
_EVIDENCE_TYPES = {
    "participant_self_report",
    "researcher_observation",
}


def _safe_content(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    return unicodedata.normalize(
        "NFC",
        text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n"),
    )


def _cell_content(cell: dict[str, Any]) -> tuple[str, str, str]:
    raw = _safe_content(cell.get("raw_value"))
    display = _safe_content(cell.get("display_value"))
    normalized = (display if display else raw).strip()
    return raw, display, normalized


def _address(cell: dict[str, Any]) -> str:
    address = _text(cell.get("address"))
    if address:
        return address
    row = cell.get("row")
    column = cell.get("column")
    return f"R{row}C{column}"


def _identity_for_occurrence(
    occurrence: dict[str, Any],
) -> tuple[str | None, str, str, str, float]:
    role = occurrence.get("row_role")
    question_id = occurrence.get("canonical_main_question_id")
    module_id = occurrence.get("canonical_module_id")
    if role == "main_question":
        verified = bool(question_id and module_id)
        return (
            "participant_self_report",
            "main_answer",
            "system_verified" if verified else "needs_review",
            "deterministic_context",
            1.0 if verified else 0.0,
        )
    if role == "follow_up":
        verified = bool(question_id and module_id)
        return (
            "participant_self_report",
            "follow_up_answer",
            "system_verified" if verified else "needs_review",
            "deterministic_context",
            1.0 if verified else 0.0,
        )
    if role == "observation_row":
        return (
            "researcher_observation",
            "observation",
            "system_verified",
            "explicit_source_type",
            1.0,
        )
    return None, "unknown", "needs_review", "deterministic_context", 0.0


def _review_issue(
    code: str,
    message: str,
    *,
    occurrence: dict[str, Any],
    evidence_id: str,
    suggested_action: str,
) -> dict[str, Any]:
    occurrence_id = str(occurrence["occurrence_id"])
    return {
        "issue_id": _stable_id("issue", code, occurrence_id, evidence_id),
        "code": code,
        "severity": "blocking",
        "status": "open",
        "message": message,
        "affected_ids": {
            "occurrence_ids": [occurrence_id],
            "evidence_ids": [evidence_id],
        },
        "source_context": {
            "group_id": occurrence.get("group_id"),
            "sheet_id": occurrence.get("sheet_id"),
            "row": occurrence.get("row"),
        },
        "suggested_action": suggested_action,
        "allowed_resolutions": ["assign_row_role", "exclude_evidence"],
        "reason": "participant_content_conflicts_with_structural_row",
        "report_impact": "该单元格在确认身份和归属前不可用于报告。",
        "suggested_resolution": {},
        "resolution": None,
    }


def _formula_cache_issue(
    *, occurrence: dict[str, Any], evidence_id: str
) -> dict[str, Any]:
    issue = _review_issue(
        "EVIDENCE_FORMULA_CACHE_UNAVAILABLE",
        "该玩家单元格是没有可读缓存值的公式，不能作为已确认回答。",
        occurrence=occurrence,
        evidence_id=evidence_id,
        suggested_action="open_and_save_in_excel_then_reupload",
    )
    issue["allowed_resolutions"] = ["exclude_evidence"]
    issue["reason"] = "participant_formula_has_no_cached_display_value"
    issue["report_impact"] = "公式字符串不能替代玩家回答进入报告。"
    return issue


def _expected_participants(mapping: dict[str, Any]) -> list[dict[str, str]]:
    expected: list[dict[str, str]] = []
    seen_participant_ids: set[str] = set()
    for group in mapping.get("groups") or []:
        group_id = _text(group.get("group_id"))
        if not group_id:
            raise InterviewV2StructureError(
                "CONFIRMED_MAPPING_INVALID", "已确认分组缺少分组标识。"
            )
        for participant in group.get("participants") or []:
            participant_id = _text(participant.get("participant_id"))
            if not participant_id or participant_id in seen_participant_ids:
                raise InterviewV2StructureError(
                    "CONFIRMED_MAPPING_INVALID",
                    "已确认玩家标识缺失或重复。",
                )
            seen_participant_ids.add(participant_id)
            expected.append(
                {"participant_id": participant_id, "group_id": group_id}
            )
    if not expected:
        raise InterviewV2StructureError(
            "CONFIRMED_MAPPING_INVALID", "已确认映射中没有玩家。"
        )
    return sorted(
        expected, key=lambda item: (item["group_id"], item["participant_id"])
    )


def build_evidence(
    snapshot: dict[str, Any],
    mapping: dict[str, Any],
    structure: dict[str, Any],
    *,
    project_id: str,
    import_id: str,
    workbook_revision_id: str,
    mapping_revision_id: str,
    mapping_sha256: str,
) -> dict[str, Any]:
    """Create one provenance-complete entry for each non-empty mapped cell."""

    source = structure.get("source") or {}
    expected_source = {
        "project_id": project_id,
        "import_id": import_id,
        "workbook_revision_id": workbook_revision_id,
        "mapping_revision_id": mapping_revision_id,
        "mapping_sha256": mapping_sha256,
    }
    if any(source.get(key) != value for key, value in expected_source.items()):
        raise InterviewV2StructureError(
            "STRUCTURE_INPUT_VERSION_MISMATCH",
            "证据构建引用了不一致的结构版本。",
        )
    sheets_by_id = {
        _text(sheet.get("sheet_id")): sheet
        for sheet in snapshot.get("sheets") or []
        if isinstance(sheet, dict) and _text(sheet.get("sheet_id"))
    }
    occurrences_by_sheet_row = {
        (str(item.get("sheet_id")), int(item.get("row") or 0)): item
        for item in structure.get("occurrences") or []
        if isinstance(item, dict)
    }
    cells_by_sheet_column: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for sheet_id, sheet in sheets_by_id.items():
        by_column: dict[int, list[dict[str, Any]]] = {}
        for cell in sheet.get("cells") or []:
            if not isinstance(cell, dict):
                continue
            column = cell.get("column")
            if isinstance(column, bool) or not isinstance(column, int):
                continue
            by_column.setdefault(column, []).append(cell)
        for cells in by_column.values():
            cells.sort(key=lambda item: int(item.get("row") or 0))
        cells_by_sheet_column[sheet_id] = by_column
    entries: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    expected_participants = _expected_participants(mapping)
    groups = sorted(
        (mapping.get("groups") or []),
        key=lambda item: _text(item.get("group_id")),
    )
    for group in groups:
        group_id = _text(group.get("group_id"))
        assignments = {
            _text(item.get("sheet_id")): item
            for item in group.get("sheets") or []
            if _text(item.get("role")) == "record"
        }
        for participant in sorted(
            (group.get("participants") or []),
            key=lambda item: _text(item.get("participant_id")),
        ):
            participant_id = _text(participant.get("participant_id"))
            if not participant_id:
                raise InterviewV2StructureError(
                    "CONFIRMED_MAPPING_INVALID",
                    "已确认玩家绑定缺少玩家标识。",
                )
            for binding in sorted(
                (participant.get("columns") or []),
                key=lambda item: (
                    _text(item.get("sheet_id")),
                    int(item.get("column_index") or 0),
                ),
            ):
                sheet_id = _text(binding.get("sheet_id"))
                assignment = assignments.get(sheet_id)
                if assignment is None:
                    continue
                column = binding.get("column_index")
                if isinstance(column, bool) or not isinstance(column, int):
                    raise InterviewV2StructureError(
                        "CONFIRMED_MAPPING_INVALID",
                        "已确认玩家绑定包含无效列。",
                        {"sheet_id": sheet_id},
                    )
                sheet = sheets_by_id.get(sheet_id)
                if sheet is None:
                    raise InterviewV2StructureError(
                        "CONFIRMED_MAPPING_INVALID",
                        "已确认玩家绑定引用了不存在的 Sheet。",
                        {"sheet_id": sheet_id},
                    )
                cells = cells_by_sheet_column.get(sheet_id, {}).get(column, [])
                for cell in cells:
                    row = int(cell.get("row") or 0)
                    occurrence = occurrences_by_sheet_row.get((sheet_id, row))
                    if occurrence is None:
                        continue
                    raw_content, display_content, normalized_content = _cell_content(cell)
                    formula_cache_status = _text(
                        cell.get("formula_cache_status")
                    ) or "not_applicable"
                    if formula_cache_status == "available":
                        normalized_content = display_content.strip()
                    if not normalized_content:
                        continue
                    address = _address(cell)
                    evidence_id = _stable_id(
                        "ev",
                        workbook_revision_id,
                        sheet_id,
                        address,
                        participant_id,
                    )
                    source_cell_id = _stable_id(
                        "cell", workbook_revision_id, sheet_id, address
                    )
                    (
                        evidence_type,
                        capture_context,
                        identity_status,
                        decision_source,
                        confidence,
                    ) = _identity_for_occurrence(occurrence)
                    if formula_cache_status == "unavailable":
                        identity_status = "needs_review"
                        decision_source = "formula_cache_unavailable"
                        confidence = 0.0
                    entry = {
                        "evidence_id": evidence_id,
                        "participant_id": participant_id,
                        "participant_label": _text(
                            participant.get("participant_label")
                        ),
                        "group_id": group_id,
                        "recorder_label": _text(
                            assignment.get("recorder_label")
                        ),
                        "module_id": occurrence.get("canonical_module_id"),
                        "main_question_id": occurrence.get(
                            "canonical_main_question_id"
                        ),
                        "occurrence_id": occurrence["occurrence_id"],
                        "evidence_type": evidence_type,
                        "capture_context": capture_context,
                        "prompt_text": occurrence.get("raw_prompt_text"),
                        "raw_content": raw_content,
                        "display_content": display_content,
                        "normalized_content": normalized_content,
                        "fragment_text_field": "normalized_content",
                        "fragment_start": 0,
                        "fragment_end": len(normalized_content),
                        "source_cell_id": source_cell_id,
                        "sheet_id": sheet_id,
                        "sheet_name": _text(sheet.get("name")),
                        "row": row,
                        "column": column,
                        "cell_address": address,
                        "source_value_sha256": _text(cell.get("value_sha256"))
                        or sha256(normalized_content.encode("utf-8")).hexdigest(),
                        "formula_cache_status": formula_cache_status,
                        "inclusion_status": "included",
                        "identity_decision_status": identity_status,
                        "decision_source": decision_source,
                        "confidence": confidence,
                        "confirmed_by": None,
                        "confirmed_at": None,
                    }
                    entries.append(entry)
                    if formula_cache_status == "unavailable":
                        issues.append(
                            _formula_cache_issue(
                                occurrence=occurrence,
                                evidence_id=evidence_id,
                            )
                        )
                    if occurrence.get("row_role") == "module_header":
                        issues.append(
                            _review_issue(
                                "PARTICIPANT_CONTENT_ON_MODULE_HEADER",
                                "模块标题行包含玩家列内容，无法自动判断其身份。",
                                occurrence=occurrence,
                                evidence_id=evidence_id,
                                suggested_action="assign_row_role",
                            )
                        )

    entries.sort(key=lambda item: item["evidence_id"])
    result_source = dict(source)
    result_source.update(
        {
            "rules_version": EVIDENCE_RULES_VERSION,
            "evidence_policy_version": EVIDENCE_POLICY_VERSION,
        }
    )
    return {
        "evidence": {
            "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
            "source": result_source,
            "expected_participants": expected_participants,
            "entries": entries,
        },
        "review_issues": issues,
    }


def _included_counts_by_participant(
    entries: list[dict[str, Any]] | dict[str, dict[str, Any]],
) -> dict[str, int]:
    values = entries.values() if isinstance(entries, dict) else entries
    counts: dict[str, int] = {}
    for entry in values:
        if entry.get("inclusion_status") != "included":
            continue
        participant_id = _text(entry.get("participant_id"))
        if participant_id:
            counts[participant_id] = counts.get(participant_id, 0) + 1
    return counts


def _can_exclude_linked_evidence(
    linked_ids: set[str] | list[str],
    entries_by_id: dict[str, dict[str, Any]],
    included_counts: dict[str, int],
) -> bool:
    linked_included = [
        entries_by_id[evidence_id]
        for evidence_id in linked_ids
        if evidence_id in entries_by_id
        and entries_by_id[evidence_id].get("inclusion_status") == "included"
    ]
    return bool(linked_included) and all(
        included_counts.get(_text(entry.get("participant_id")), 0) > 1
        for entry in linked_included
    )


def _merge_issue_evidence_links(
    issues: list[dict[str, Any]], entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    evidence_by_occurrence: dict[str, list[str]] = {}
    for entry in entries:
        evidence_by_occurrence.setdefault(str(entry["occurrence_id"]), []).append(
            str(entry["evidence_id"])
        )
    entries_by_id = {str(entry["evidence_id"]): entry for entry in entries}
    included_counts = _included_counts_by_participant(entries)
    merged: dict[str, dict[str, Any]] = {}
    for raw_issue in issues:
        issue = deepcopy(raw_issue)
        affected = issue.setdefault("affected_ids", {})
        linked = list(affected.get("evidence_ids") or [])
        for occurrence_id in affected.get("occurrence_ids") or []:
            linked.extend(evidence_by_occurrence.get(str(occurrence_id), []))
        if linked:
            linked_ids = sorted(set(linked))
            affected["evidence_ids"] = linked_ids
            can_exclude_one_evidence = _can_exclude_linked_evidence(
                linked_ids, entries_by_id, included_counts
            )
            if (
                issue.get("code") == "ROW_ROLE_UNKNOWN"
                and can_exclude_one_evidence
            ):
                issue.setdefault("allowed_resolutions", []).append(
                    "exclude_evidence"
                )
                issue["allowed_resolutions"] = sorted(
                    set(issue["allowed_resolutions"])
                )
            elif issue.get("code") in {
                "MAIN_QUESTION_TEXT_MISSING",
                "MAIN_QUESTION_TEXT_INVALID",
            }:
                issue["suggested_action"] = "fix_question_text_and_reupload"
                issue["allowed_resolutions"] = (
                    ["exclude_evidence"]
                    if can_exclude_one_evidence
                    else []
                )
            elif issue.get("code") == "EVIDENCE_FORMULA_CACHE_UNAVAILABLE":
                issue["allowed_resolutions"] = (
                    ["exclude_evidence"]
                    if can_exclude_one_evidence
                    else []
                )
        merged[str(issue["issue_id"])] = issue
    return sorted(merged.values(), key=lambda item: item["issue_id"])


def _missing_participant_evidence_issue(
    *, group_id: str, participant_id: str, workbook_revision_id: str
) -> dict[str, Any]:
    return {
        "issue_id": _stable_id(
            "issue",
            "PARTICIPANT_EVIDENCE_MISSING",
            workbook_revision_id,
            group_id,
            participant_id,
        ),
        "code": "PARTICIPANT_EVIDENCE_MISSING",
        "severity": "blocking",
        "status": "open",
        "message": "已确认的玩家列中没有可用访谈记录。",
        "affected_ids": {
            "group_ids": [group_id],
            "participant_ids": [participant_id],
        },
        "source_context": {
            "group_id": group_id,
            "sheet_id": None,
            "row": None,
        },
        "suggested_action": "add_participant_evidence_and_reupload",
        "allowed_resolutions": [],
        "reason": "confirmed_participant_columns_have_no_non_empty_evidence",
        "report_impact": "没有玩家证据时不能建立玩家档案或生成研究结论。",
        "suggested_resolution": {},
        "resolution": None,
    }


def _summary(
    structure: dict[str, Any],
    evidence: dict[str, Any],
    review_issues: list[dict[str, Any]],
    *,
    manual_overrides: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    blocking = sum(
        1
        for issue in review_issues
        if issue.get("status") == "open" and issue.get("severity") == "blocking"
    )
    included_evidence_count = sum(
        1
        for entry in evidence.get("entries") or []
        if entry.get("inclusion_status") == "included"
    )
    unsafe_evidence_count = sum(
        1
        for entry in evidence.get("entries") or []
        if entry.get("inclusion_status") == "included"
        and (
            entry.get("identity_decision_status")
            not in {"system_verified", "human_confirmed"}
            or not entry.get("module_id")
            or not entry.get("main_question_id")
        )
    )
    safe_participant_ids = {
        _text(entry.get("participant_id"))
        for entry in evidence.get("entries") or []
        if entry.get("inclusion_status") == "included"
        and entry.get("identity_decision_status")
        in {"system_verified", "human_confirmed"}
        and entry.get("module_id")
        and entry.get("main_question_id")
    }
    expected_participant_ids = {
        _text(item.get("participant_id"))
        for item in evidence.get("expected_participants") or []
        if _text(item.get("participant_id"))
    }
    participant_evidence_missing = bool(
        expected_participant_ids - safe_participant_ids
    )
    return {
        "structure": structure,
        "evidence": evidence,
        "review_issues": review_issues,
        "blocking_issue_count": blocking,
        "status": (
            "STRUCTURE_REVIEW_REQUIRED"
            if (
                blocking
                or unsafe_evidence_count
                or not included_evidence_count
                or participant_evidence_missing
            )
            else "READY_FOR_DOSSIERS"
        ),
        **(
            {"manual_overrides": list(manual_overrides)}
            if manual_overrides is not None
            else {}
        ),
    }


def build_structure_and_evidence(
    snapshot: dict[str, Any],
    mapping: dict[str, Any],
    *,
    project_id: str,
    import_id: str,
    workbook_revision_id: str,
    mapping_revision_id: str,
    mapping_sha256: str,
) -> dict[str, Any]:
    """Build the complete synchronous Batch 3A deterministic checkpoint."""

    structure_result = build_structure(
        snapshot,
        mapping,
        project_id=project_id,
        import_id=import_id,
        workbook_revision_id=workbook_revision_id,
        mapping_revision_id=mapping_revision_id,
        mapping_sha256=mapping_sha256,
    )
    evidence_result = build_evidence(
        snapshot,
        mapping,
        structure_result["structure"],
        project_id=project_id,
        import_id=import_id,
        workbook_revision_id=workbook_revision_id,
        mapping_revision_id=mapping_revision_id,
        mapping_sha256=mapping_sha256,
    )
    issues = _merge_issue_evidence_links(
        structure_result["review_issues"] + evidence_result["review_issues"],
        evidence_result["evidence"]["entries"],
    )
    participants_with_evidence = {
        _text(entry.get("participant_id"))
        for entry in evidence_result["evidence"]["entries"]
    }
    for participant in evidence_result["evidence"]["expected_participants"]:
        participant_id = participant["participant_id"]
        if participant_id in participants_with_evidence:
            continue
        issues.append(
            _missing_participant_evidence_issue(
                group_id=participant["group_id"],
                participant_id=participant_id,
                workbook_revision_id=workbook_revision_id,
            )
        )
    if issues:
        issues.sort(key=lambda item: item["issue_id"])
    return _summary(structure_result["structure"], evidence_result["evidence"], issues)


def _occurrence_state(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "occurrence_id",
            "row_role",
            "canonical_module_id",
            "canonical_main_question_id",
            "parent_main_occurrence_id",
            "mapping_method",
            "decision_status",
            "decision_source",
            "confidence",
            "confirmed_by",
            "confirmed_at",
        )
    }


def _evidence_state(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "evidence_id",
            "module_id",
            "main_question_id",
            "evidence_type",
            "capture_context",
            "inclusion_status",
            "identity_decision_status",
            "decision_source",
            "confidence",
            "confirmed_by",
            "confirmed_at",
        )
    }


def _single_affected_occurrence(
    issue: dict[str, Any], occurrences: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    ids = list((issue.get("affected_ids") or {}).get("occurrence_ids") or [])
    if len(ids) != 1 or ids[0] not in occurrences:
        raise InterviewV2StructureError(
            "REVIEW_RESOLUTION_INVALID",
            "该审核问题不能使用此结构修正动作。",
        )
    return occurrences[ids[0]]


def _index_evidence_by_occurrence(
    entries: dict[str, dict[str, Any]],
) -> dict[str, set[str]]:
    evidence_ids_by_occurrence: dict[str, set[str]] = {}
    for evidence_id, entry in entries.items():
        evidence_ids_by_occurrence.setdefault(
            str(entry.get("occurrence_id")), set()
        ).add(evidence_id)
    return evidence_ids_by_occurrence


def _linked_evidence_ids(
    issue: dict[str, Any],
    entries: dict[str, dict[str, Any]],
    evidence_ids_by_occurrence: dict[str, set[str]],
) -> set[str]:
    affected = issue.get("affected_ids") or {}
    result = {
        str(item) for item in affected.get("evidence_ids") or [] if str(item) in entries
    }
    for occurrence_id in affected.get("occurrence_ids") or []:
        result.update(evidence_ids_by_occurrence.get(str(occurrence_id), set()))
    return result


def _question_for_occurrence(
    structure: dict[str, Any], occurrence: dict[str, Any]
) -> dict[str, Any]:
    module_id = occurrence.get("canonical_module_id")
    prompt = occurrence.get("raw_prompt_text")
    key = _question_key(prompt)
    if not module_id or not prompt or not key:
        raise InterviewV2StructureError(
            "REVIEW_RESOLUTION_INVALID",
            "主问题必须具有已确认模块和问题文本。",
        )
    project_id = (structure.get("source") or {}).get("project_id")
    question_id = _stable_id("question", project_id, module_id, key)
    by_id = {
        str(item["main_question_id"]): item
        for item in structure.get("main_questions") or []
    }
    question = by_id.get(question_id)
    if question is None:
        question = {
            "main_question_id": question_id,
            "module_id": module_id,
            "canonical_text": prompt,
            "normalized_key": key,
            "raw_prompts": [prompt],
            "occurrence_ids": [],
            "alignment_method": "manual_creation",
            "decision_status": "confirmed",
            "decision_source": "user_selection",
            "confidence": 1.0,
            "confirmed_by": occurrence.get("confirmed_by"),
            "confirmed_at": occurrence.get("confirmed_at"),
        }
        structure.setdefault("main_questions", []).append(question)
    return question


def _refresh_collections(structure: dict[str, Any]) -> None:
    module_ids = {
        str(item["module_id"]): item for item in structure.get("modules") or []
    }
    question_ids = {
        str(item["main_question_id"]): item
        for item in structure.get("main_questions") or []
    }
    for module in module_ids.values():
        module["occurrence_ids"] = []
    for question in question_ids.values():
        question["occurrence_ids"] = []
    for occurrence in structure.get("occurrences") or []:
        occurrence_id = str(occurrence["occurrence_id"])
        module = module_ids.get(str(occurrence.get("canonical_module_id")))
        if module is not None:
            module["occurrence_ids"].append(occurrence_id)
        question = question_ids.get(
            str(occurrence.get("canonical_main_question_id"))
        )
        if question is not None:
            question["occurrence_ids"].append(occurrence_id)
    for item in [*module_ids.values(), *question_ids.values()]:
        item["occurrence_ids"] = sorted(set(item["occurrence_ids"]))
    structure["modules"] = sorted(module_ids.values(), key=lambda item: item["module_id"])
    structure["main_questions"] = sorted(
        question_ids.values(), key=lambda item: item["main_question_id"]
    )


def _sync_linked_evidence(
    occurrence: dict[str, Any],
    entries: dict[str, dict[str, Any]],
    evidence_ids_by_occurrence: dict[str, set[str]],
    *,
    actor: str,
    resolved_at: str,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    changes = []
    linked_ids = evidence_ids_by_occurrence.get(
        str(occurrence.get("occurrence_id")), set()
    )
    for evidence_id in sorted(linked_ids):
        entry = entries[evidence_id]
        before = _evidence_state(entry)
        evidence_type, capture, status, _source, confidence = _identity_for_occurrence(
            occurrence
        )
        entry.update(
            {
                "module_id": occurrence.get("canonical_module_id"),
                "main_question_id": occurrence.get("canonical_main_question_id"),
                "evidence_type": evidence_type,
                "capture_context": capture,
                "identity_decision_status": (
                    "human_confirmed" if evidence_type is not None else status
                ),
                "decision_source": "manual_override",
                "confidence": confidence,
                "confirmed_by": actor if evidence_type is not None else None,
                "confirmed_at": resolved_at if evidence_type is not None else None,
            }
        )
        changes.append((before, _evidence_state(entry)))
    return changes


def apply_review_resolutions(
    structure: dict[str, Any],
    evidence: dict[str, Any],
    review_issues: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
    *,
    actor: str,
    resolved_at: str,
    operation_fingerprint: str,
) -> dict[str, Any]:
    """Atomically apply a whitelist of typed, append-only review overrides."""

    actor = _text(actor)
    resolved_at = _text(resolved_at)
    operation_fingerprint = _text(operation_fingerprint)
    if not actor or len(actor) > 200 or not resolved_at or len(resolved_at) > 100:
        raise InterviewV2StructureError(
            "REVIEW_RESOLUTION_INVALID",
            "审核操作缺少有效的操作人或时间。",
        )
    if not re.fullmatch(r"[0-9a-f]{64}", operation_fingerprint):
        raise InterviewV2StructureError(
            "REVIEW_RESOLUTION_INVALID",
            "审核操作缺少稳定的请求指纹。",
        )
    if not isinstance(resolutions, list) or not 1 <= len(resolutions) <= 200:
        raise InterviewV2StructureError(
            "REVIEW_RESOLUTION_INVALID",
            "审核修正数量无效。",
        )
    new_structure = deepcopy(structure)
    new_evidence = deepcopy(evidence)
    new_issues = deepcopy(review_issues)
    occurrences = {
        str(item["occurrence_id"]): item
        for item in new_structure.get("occurrences") or []
    }
    modules = {
        str(item["module_id"]): item for item in new_structure.get("modules") or []
    }
    questions = {
        str(item["main_question_id"]): item
        for item in new_structure.get("main_questions") or []
    }
    entries = {
        str(item["evidence_id"]): item for item in new_evidence.get("entries") or []
    }
    evidence_ids_by_occurrence = _index_evidence_by_occurrence(entries)
    issues = {str(item["issue_id"]): item for item in new_issues}
    seen_issue_ids: set[str] = set()
    overrides: list[dict[str, Any]] = []

    for raw_resolution in resolutions:
        if not isinstance(raw_resolution, dict):
            raise InterviewV2StructureError(
                "REVIEW_RESOLUTION_INVALID", "审核修正结构无效。"
            )
        resolution = deepcopy(raw_resolution)
        issue_id = _text(resolution.get("issue_id"))
        action = _text(resolution.get("resolution"))
        comment = _text(resolution.get("comment"))
        if (
            not re.fullmatch(r"issue_[0-9a-f]{32}", issue_id)
            or issue_id in seen_issue_ids
            or action not in _RESOLUTION_ACTIONS
            or not comment
            or len(comment) > 500
        ):
            raise InterviewV2StructureError(
                "REVIEW_RESOLUTION_INVALID", "审核修正字段无效。"
            )
        seen_issue_ids.add(issue_id)
        issue = issues.get(issue_id)
        if issue is None or issue.get("status") != "open":
            raise InterviewV2StructureError(
                "REVIEW_ISSUE_NOT_OPEN", "审核问题不存在或已经处理。"
            )
        if action == "accept_suggestion":
            suggestion = issue.get("suggested_resolution") or {}
            suggested_action = _text(suggestion.get("resolution"))
            if suggested_action not in _RESOLUTION_ACTIONS - {"accept_suggestion"}:
                raise InterviewV2StructureError(
                    "REVIEW_RESOLUTION_INVALID", "该审核问题没有可接受的确定建议。"
                )
            resolution = {
                **suggestion,
                "issue_id": issue_id,
                "comment": comment,
            }
            action = suggested_action

        allowed_resolutions = set(issue.get("allowed_resolutions") or [])
        if action not in allowed_resolutions:
            raise InterviewV2StructureError(
                "REVIEW_RESOLUTION_INVALID",
                "该审核问题不支持所选修正动作。",
            )

        entity_changes: list[dict[str, Any]] = []
        if action == "assign_row_role":
            occurrence = _single_affected_occurrence(issue, occurrences)
            row_role = _text(resolution.get("row_role"))
            target_id = _text(resolution.get("target_id"))
            if row_role not in _ROW_ROLES - {"unknown"}:
                raise InterviewV2StructureError(
                    "REVIEW_RESOLUTION_INVALID", "人工行类型无效。"
                )
            if target_id and row_role not in {"follow_up", "observation_row"}:
                raise InterviewV2StructureError(
                    "REVIEW_RESOLUTION_INVALID",
                    "只有追问或观察行可在确认行类型时指定主问题。",
                )
            before = _occurrence_state(occurrence)
            occurrence.update(
                {
                    "row_role": row_role,
                    "decision_status": "confirmed",
                    "decision_source": "user_selection",
                    "mapping_method": "manual_override",
                    "confidence": 1.0,
                    "confirmed_by": actor,
                    "confirmed_at": resolved_at,
                }
            )
            if row_role == "main_question":
                occurrence["parent_main_occurrence_id"] = None
                question = _question_for_occurrence(new_structure, occurrence)
                questions[question["main_question_id"]] = question
                occurrence["canonical_main_question_id"] = question[
                    "main_question_id"
                ]
            elif row_role in {"follow_up", "observation_row"}:
                if target_id:
                    question = questions.get(target_id)
                    if question is None:
                        raise InterviewV2StructureError(
                            "REVIEW_RESOLUTION_INVALID",
                            "目标主问题不属于当前结构版本。",
                        )
                    occurrence["canonical_module_id"] = question["module_id"]
                    occurrence["canonical_main_question_id"] = target_id
                    prior = [
                        item
                        for item in occurrences.values()
                        if item.get("sheet_id") == occurrence.get("sheet_id")
                        and item.get("row_role") == "main_question"
                        and item.get("canonical_main_question_id") == target_id
                        and int(item.get("row") or 0)
                        < int(occurrence.get("row") or 0)
                    ]
                    occurrence["parent_main_occurrence_id"] = (
                        max(prior, key=lambda item: int(item.get("row") or 0))[
                            "occurrence_id"
                        ]
                        if prior
                        else None
                    )
                else:
                    prior = [
                        item
                        for item in occurrences.values()
                        if item.get("sheet_id") == occurrence.get("sheet_id")
                        and item.get("canonical_module_id")
                        == occurrence.get("canonical_module_id")
                        and item.get("row_role") == "main_question"
                        and int(item.get("row") or 0)
                        < int(occurrence.get("row") or 0)
                    ]
                    if not prior:
                        raise InterviewV2StructureError(
                            "REVIEW_RESOLUTION_INVALID",
                            "该行上方仍没有可继承的同模块主问题。",
                        )
                    parent = max(prior, key=lambda item: int(item.get("row") or 0))
                    occurrence["parent_main_occurrence_id"] = parent[
                        "occurrence_id"
                    ]
                    occurrence["canonical_main_question_id"] = parent.get(
                        "canonical_main_question_id"
                    )
            elif row_role == "module_header":
                if _linked_evidence_ids(
                    issue, entries, evidence_ids_by_occurrence
                ):
                    raise InterviewV2StructureError(
                        "REVIEW_RESOLUTION_INVALID",
                        "含玩家内容的行不能直接确认为模块标题。",
                    )
                occurrence["canonical_main_question_id"] = None
                occurrence["parent_main_occurrence_id"] = None
            entity_changes.append(
                {
                    "entity_type": "question_occurrence",
                    "entity_id": occurrence["occurrence_id"],
                    "before": before,
                    "after": _occurrence_state(occurrence),
                }
            )
            for before_entry, after_entry in _sync_linked_evidence(
                occurrence,
                entries,
                evidence_ids_by_occurrence,
                actor=actor,
                resolved_at=resolved_at,
            ):
                entity_changes.append(
                    {
                        "entity_type": "evidence",
                        "entity_id": after_entry["evidence_id"],
                        "before": before_entry,
                        "after": after_entry,
                    }
                )
        elif action == "assign_module":
            occurrence = _single_affected_occurrence(issue, occurrences)
            target_id = _text(resolution.get("target_id"))
            if target_id not in modules:
                raise InterviewV2StructureError(
                    "REVIEW_RESOLUTION_INVALID", "目标模块不属于当前结构版本。"
                )
            before = _occurrence_state(occurrence)
            occurrence.update(
                {
                    "canonical_module_id": target_id,
                    "decision_status": "confirmed",
                    "decision_source": "user_selection",
                    "mapping_method": "manual_override",
                    "confidence": 1.0,
                    "confirmed_by": actor,
                    "confirmed_at": resolved_at,
                }
            )
            if occurrence.get("row_role") == "main_question":
                question = _question_for_occurrence(new_structure, occurrence)
                questions[question["main_question_id"]] = question
                occurrence["canonical_main_question_id"] = question[
                    "main_question_id"
                ]
            entity_changes.append(
                {
                    "entity_type": "question_occurrence",
                    "entity_id": occurrence["occurrence_id"],
                    "before": before,
                    "after": _occurrence_state(occurrence),
                }
            )
            for before_entry, after_entry in _sync_linked_evidence(
                occurrence,
                entries,
                evidence_ids_by_occurrence,
                actor=actor,
                resolved_at=resolved_at,
            ):
                entity_changes.append(
                    {
                        "entity_type": "evidence",
                        "entity_id": after_entry["evidence_id"],
                        "before": before_entry,
                        "after": after_entry,
                    }
                )
        elif action == "assign_main_question":
            occurrence = _single_affected_occurrence(issue, occurrences)
            target_id = _text(resolution.get("target_id"))
            question = questions.get(target_id)
            if question is None:
                raise InterviewV2StructureError(
                    "REVIEW_RESOLUTION_INVALID", "目标主问题不属于当前结构版本。"
                )
            before = _occurrence_state(occurrence)
            occurrence.update(
                {
                    "canonical_module_id": question["module_id"],
                    "canonical_main_question_id": target_id,
                    "mapping_method": "manual_override",
                    "decision_status": "confirmed",
                    "decision_source": "user_selection",
                    "confidence": 1.0,
                    "confirmed_by": actor,
                    "confirmed_at": resolved_at,
                }
            )
            # Manual canonical alignment may cross sheets; only the optional
            # local parent-occurrence link remains constrained to this sheet.
            same_sheet_parents = [
                item
                for item in occurrences.values()
                if item.get("sheet_id") == occurrence.get("sheet_id")
                and item.get("row_role") == "main_question"
                and item.get("canonical_main_question_id") == target_id
                and int(item.get("row") or 0) < int(occurrence.get("row") or 0)
            ]
            occurrence["parent_main_occurrence_id"] = (
                max(same_sheet_parents, key=lambda item: int(item.get("row") or 0))[
                    "occurrence_id"
                ]
                if same_sheet_parents
                else None
            )
            entity_changes.append(
                {
                    "entity_type": "question_occurrence",
                    "entity_id": occurrence["occurrence_id"],
                    "before": before,
                    "after": _occurrence_state(occurrence),
                }
            )
            for before_entry, after_entry in _sync_linked_evidence(
                occurrence,
                entries,
                evidence_ids_by_occurrence,
                actor=actor,
                resolved_at=resolved_at,
            ):
                entity_changes.append(
                    {
                        "entity_type": "evidence",
                        "entity_id": after_entry["evidence_id"],
                        "before": before_entry,
                        "after": after_entry,
                    }
                )
        elif action in {"set_evidence_identity", "exclude_evidence"}:
            target_id = _text(resolution.get("target_id"))
            linked_evidence_ids = _linked_evidence_ids(
                issue, entries, evidence_ids_by_occurrence
            )
            if target_id not in linked_evidence_ids:
                raise InterviewV2StructureError(
                    "REVIEW_RESOLUTION_INVALID",
                    "目标证据不属于该审核问题或当前证据版本。",
                )
            entry = entries[target_id]
            before = _evidence_state(entry)
            if action == "set_evidence_identity":
                evidence_type = _text(resolution.get("evidence_type"))
                if evidence_type not in _EVIDENCE_TYPES:
                    raise InterviewV2StructureError(
                        "REVIEW_RESOLUTION_INVALID", "人工证据身份无效。"
                    )
                entry.update(
                    {
                        "evidence_type": evidence_type,
                        "identity_decision_status": "human_confirmed",
                        "decision_source": "manual_override",
                        "confidence": 1.0,
                        "confirmed_by": actor,
                        "confirmed_at": resolved_at,
                    }
                )
            else:
                participant_id = _text(entry.get("participant_id"))
                included_counts = _included_counts_by_participant(entries)
                if included_counts.get(participant_id, 0) <= 1:
                    raise InterviewV2StructureError(
                        "REVIEW_RESOLUTION_INVALID",
                        "每位玩家至少需要保留一条可用于玩家档案的证据。",
                    )
                entry.update(
                    {
                        "inclusion_status": "excluded_by_user",
                        "decision_source": "manual_override",
                        "confirmed_by": actor,
                        "confirmed_at": resolved_at,
                    }
                )
            entity_changes.append(
                {
                    "entity_type": "evidence",
                    "entity_id": target_id,
                    "before": before,
                    "after": _evidence_state(entry),
                }
            )

        issue_resolved = True
        if action in {"exclude_evidence", "set_evidence_identity"}:
            issue_resolved = all(
                entries[evidence_id].get("inclusion_status") == "excluded_by_user"
                or entries[evidence_id].get("identity_decision_status")
                in {"system_verified", "human_confirmed"}
                for evidence_id in linked_evidence_ids
            )
        issue["status"] = "resolved" if issue_resolved else "open"
        issue["resolution"] = {
            "action": action,
            "target_id": resolution.get("target_id"),
            "row_role": resolution.get("row_role"),
            "evidence_type": resolution.get("evidence_type"),
            "comment": comment,
            "resolved_by": actor,
            "resolved_at": resolved_at,
        }
        override_id = _stable_id(
            "override",
            issue_id,
            action,
            actor,
            resolution.get("target_id"),
            resolution.get("row_role"),
            resolution.get("evidence_type"),
            comment,
            operation_fingerprint,
        )
        overrides.append(
            {
                "manual_override_id": override_id,
                "issue_id": issue_id,
                "action": action,
                "changes": entity_changes,
                "reason": comment,
                "created_by": actor,
                "created_at": resolved_at,
                "operation_fingerprint": operation_fingerprint,
            }
        )

    _refresh_collections(new_structure)
    new_evidence["entries"] = sorted(entries.values(), key=lambda item: item["evidence_id"])
    remaining_counts = _included_counts_by_participant(entries)
    expected_participant_ids = {
        _text(item.get("participant_id"))
        for item in new_evidence.get("expected_participants") or []
        if _text(item.get("participant_id"))
    }
    if any(remaining_counts.get(participant_id, 0) < 1 for participant_id in expected_participant_ids):
        raise InterviewV2StructureError(
            "REVIEW_RESOLUTION_INVALID",
            "每位玩家至少需要保留一条可用于玩家档案的证据。",
        )
    for issue in issues.values():
        affected_evidence_ids = {
            str(item)
            for item in (issue.get("affected_ids") or {}).get("evidence_ids", [])
        }
        if not _can_exclude_linked_evidence(
            affected_evidence_ids, entries, remaining_counts
        ):
            issue["allowed_resolutions"] = [
                action
                for action in issue.get("allowed_resolutions") or []
                if action != "exclude_evidence"
            ]
    sorted_issues = sorted(issues.values(), key=lambda item: item["issue_id"])
    return _summary(
        new_structure,
        new_evidence,
        sorted_issues,
        manual_overrides=sorted(overrides, key=lambda item: item["manual_override_id"]),
    )


__all__ = [
    "EVIDENCE_POLICY_VERSION",
    "EVIDENCE_RULES_VERSION",
    "EVIDENCE_SCHEMA_VERSION",
    "InterviewV2StructureError",
    "apply_review_resolutions",
    "build_evidence",
    "build_structure_and_evidence",
]
