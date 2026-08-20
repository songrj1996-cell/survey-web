"""Deterministic outline extraction for confirmed interview V2 mappings.

The module operates only on immutable physical snapshot facts and a confirmed
mapping.  It performs no persistence, HTTP, or model work.
"""

from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import json
import re
import unicodedata
from typing import Any


STRUCTURE_SCHEMA_VERSION = "interview-structure/1.0"
STRUCTURE_RULES_VERSION = "interview-v2-structure-rules/1.0"

_ROW_ROLES = {
    "module_header",
    "main_question",
    "follow_up",
    "observation_row",
    "unknown",
}
_MODULE_HEADERS = {
    "功能模块",
    "模块",
    "研究模块",
    "主题模块",
    "module",
    "researchmodule",
}
_TYPE_HEADERS = {
    "行类型",
    "问题类型",
    "记录类型",
    "类型",
    "rowtype",
    "questiontype",
    "recordtype",
    "type",
}
_PROMPT_HEADERS = {
    "问题",
    "问题备注",
    "问题或备注",
    "访谈问题",
    "访谈提纲",
    "提纲",
    "主问题追问",
    "prompt",
    "question",
    "questionprompt",
    "note",
}
_ROW_TYPE_ALIASES = {
    "模块": "module_header",
    "模块标题": "module_header",
    "功能模块": "module_header",
    "module": "module_header",
    "moduleheader": "module_header",
    "主问题": "main_question",
    "核心问题": "main_question",
    "主要问题": "main_question",
    "mainquestion": "main_question",
    "main": "main_question",
    "追问": "follow_up",
    "后续追问": "follow_up",
    "补充追问": "follow_up",
    "followup": "follow_up",
    "probe": "follow_up",
    "观察": "observation_row",
    "观察备注": "observation_row",
    "研究员观察": "observation_row",
    "记录员观察": "observation_row",
    "observation": "observation_row",
    "observationnote": "observation_row",
}


class InterviewV2StructureError(Exception):
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


def _stable_id(prefix: str, *parts: object) -> str:
    digest = sha256()
    for part in parts:
        encoded = unicodedata.normalize("NFC", str(part)).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"{prefix}_{digest.hexdigest()[:32]}"


def _ensure_unicode_scalars(value: Any) -> None:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise InterviewV2StructureError(
                "STRUCTURE_INPUT_INVALID",
                "结构化输入包含无效 Unicode 文本。",
            ) from exc
    elif isinstance(value, dict):
        for key, child in value.items():
            _ensure_unicode_scalars(key)
            _ensure_unicode_scalars(child)
    elif isinstance(value, list):
        for child in value:
            _ensure_unicode_scalars(child)


def _canonical_json_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise InterviewV2StructureError(
            "STRUCTURE_INPUT_INVALID",
            "结构化输入无法形成稳定快照。",
        ) from exc
    return sha256(encoded).hexdigest()


def _text(value: object) -> str:
    if value is None:
        return ""
    return unicodedata.normalize(
        "NFC",
        str(value).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n"),
    ).strip()


def _token(value: object) -> str:
    text = unicodedata.normalize("NFKC", _text(value)).casefold()
    return "".join(character for character in text if character.isalnum())


def _question_key(value: object) -> str:
    """Normalize only numbering, whitespace, and punctuation—not semantics."""

    text = unicodedata.normalize("NFKC", _text(value)).casefold()
    text = re.sub(
        r"^\s*(?:(?:q|问题)\s*)?[0-9一二三四五六七八九十百]+"
        r"(?:\s*[\.、:：)）\-]\s*|\s+)(?=\S)",
        "",
        text,
        count=1,
    )
    return "".join(character for character in text if character.isalnum())


def _cell_text(cell: dict[str, Any] | None) -> str:
    if not cell:
        return ""
    value = cell.get("display_value")
    if value is None:
        value = cell.get("normalized_text")
    if value is None:
        value = cell.get("raw_value")
    return _text(value)


def _issue(
    code: str,
    message: str,
    *,
    severity: str,
    suggested_action: str,
    affected_ids: dict[str, list[str]],
    source_context: dict[str, Any],
    reason: str,
    report_impact: str,
    suggested_resolution: dict[str, Any] | None = None,
    allowed_resolutions: list[str] | None = None,
) -> dict[str, Any]:
    flattened_ids = sorted(
        str(item)
        for values in affected_ids.values()
        for item in values
    )
    identity = [code, *flattened_ids]
    if not flattened_ids:
        identity.extend(
            [
                source_context.get("sheet_id", ""),
                source_context.get("row", ""),
            ]
        )
    return {
        "issue_id": _stable_id("issue", *identity),
        "code": code,
        "severity": severity,
        "status": "open",
        "message": message,
        "affected_ids": {
            key: sorted(set(values)) for key, values in affected_ids.items()
        },
        "source_context": source_context,
        "suggested_action": suggested_action,
        "allowed_resolutions": sorted(set(allowed_resolutions or [])),
        "reason": reason,
        "report_impact": report_impact,
        "suggested_resolution": dict(suggested_resolution or {}),
        "resolution": None,
    }


def _sheet_cells(sheet: dict[str, Any]) -> tuple[dict[tuple[int, int], dict], dict[int, list[dict]]]:
    by_position: dict[tuple[int, int], dict] = {}
    by_row: dict[int, list[dict]] = defaultdict(list)
    for cell in sheet.get("cells") or []:
        if not isinstance(cell, dict):
            continue
        row = cell.get("row")
        column = cell.get("column")
        if isinstance(row, bool) or isinstance(column, bool):
            continue
        if not isinstance(row, int) or not isinstance(column, int):
            continue
        if row < 1 or column < 1:
            continue
        by_position[(row, column)] = cell
        by_row[row].append(cell)
    for cells in by_row.values():
        cells.sort(key=lambda item: int(item["column"]))
    return by_position, by_row


def _participant_columns(group: dict[str, Any], sheet_id: str) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for participant in group.get("participants") or []:
        for column in participant.get("columns") or []:
            if str(column.get("sheet_id") or "") != sheet_id:
                continue
            index = column.get("column_index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 1:
                raise InterviewV2StructureError(
                    "CONFIRMED_MAPPING_INVALID",
                    "已确认玩家映射包含无效列。",
                    {"sheet_id": sheet_id},
                )
            if index in result:
                raise InterviewV2StructureError(
                    "CONFIRMED_MAPPING_INVALID",
                    "已确认玩家映射重复绑定了同一列。",
                    {"sheet_id": sheet_id, "column_index": index},
                )
            result[index] = participant
    return result


def _structure_columns(
    sheet: dict[str, Any], participant_columns: dict[int, dict[str, Any]]
) -> list[int]:
    candidate = sheet.get("candidate_structure") or {}
    start = candidate.get("start_column")
    end = candidate.get("end_column")
    if (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 1 <= start <= end
    ):
        return list(range(start, end + 1))
    if participant_columns:
        first_participant = min(participant_columns)
        populated = sorted(
            {
                int(cell.get("column"))
                for cell in sheet.get("cells") or []
                if isinstance(cell, dict)
                and isinstance(cell.get("column"), int)
                and not isinstance(cell.get("column"), bool)
                and int(cell["column"]) < first_participant
            }
        )
        return populated
    dimensions = sheet.get("dimensions") or {}
    minimum = dimensions.get("content_min_column")
    maximum = dimensions.get("content_max_column")
    if (
        isinstance(minimum, int)
        and not isinstance(minimum, bool)
        and isinstance(maximum, int)
        and not isinstance(maximum, bool)
        and 1 <= minimum <= maximum
    ):
        return list(range(minimum, maximum + 1))
    return sorted(
        {
            int(cell.get("column"))
            for cell in sheet.get("cells") or []
            if isinstance(cell, dict)
            and isinstance(cell.get("column"), int)
            and not isinstance(cell.get("column"), bool)
        }
    )


def _column_roles(
    sheet: dict[str, Any],
    columns: list[int],
    by_position: dict[tuple[int, int], dict],
) -> tuple[dict[str, int], set[int]]:
    candidate_region = sheet.get("candidate_participant_region") or {}
    candidate_header_row = candidate_region.get("header_row")
    row_limit = max(
        10,
        candidate_header_row
        if isinstance(candidate_header_row, int)
        and not isinstance(candidate_header_row, bool)
        else 0,
    )
    rows = sorted(
        {
            row
            for row, column in by_position
            if column in columns and row <= row_limit
        }
    )
    roles: dict[str, int] = {}
    header_rows: set[int] = set()
    for row in rows:
        matches: list[tuple[str, int]] = []
        for column in columns:
            token = _token(_cell_text(by_position.get((row, column))))
            role = None
            if token in _MODULE_HEADERS:
                role = "module"
            elif token in _TYPE_HEADERS:
                role = "row_type"
            elif token in _PROMPT_HEADERS:
                role = "prompt"
            if role is not None and role not in roles:
                matches.append((role, column))
        distinct_matches = {role for role, _column in matches}
        is_declared_header_row = (
            isinstance(candidate_header_row, int)
            and not isinstance(candidate_header_row, bool)
            and row == candidate_header_row
        )
        if len(distinct_matches) >= 2 or (is_declared_header_row and matches):
            header_rows.add(row)
            for role, column in matches:
                roles.setdefault(role, column)
    return roles, header_rows


def _row_role(raw_type: str) -> str:
    return _ROW_TYPE_ALIASES.get(_token(raw_type), "unknown")


def _validate_inputs(
    snapshot: dict[str, Any],
    mapping: dict[str, Any],
    *,
    project_id: str,
    import_id: str,
    workbook_revision_id: str,
    mapping_revision_id: str,
    mapping_sha256: str,
) -> None:
    _ensure_unicode_scalars(snapshot)
    _ensure_unicode_scalars(mapping)
    metadata = {
        "project_id": project_id,
        "import_id": import_id,
        "workbook_revision_id": workbook_revision_id,
        "mapping_revision_id": mapping_revision_id,
        "mapping_sha256": mapping_sha256,
    }
    if any(not _text(value) for value in metadata.values()):
        raise InterviewV2StructureError(
            "STRUCTURE_INPUT_INVALID",
            "结构化输入缺少冻结版本标识。",
        )
    if not re.fullmatch(r"mapping_[0-9a-f]{32}", mapping_revision_id):
        raise InterviewV2StructureError(
            "STRUCTURE_INPUT_INVALID",
            "分组版本标识无效。",
        )
    if not re.fullmatch(r"[0-9a-f]{64}", mapping_sha256):
        raise InterviewV2StructureError(
            "STRUCTURE_INPUT_INVALID",
            "分组版本摘要无效。",
        )
    if _canonical_json_sha256(mapping) != mapping_sha256:
        raise InterviewV2StructureError(
            "MAPPING_DIGEST_MISMATCH",
            "分组版本内容与摘要不一致。",
        )
    expected = {
        "project_id": project_id,
        "import_id": import_id,
        "workbook_revision_id": workbook_revision_id,
    }
    for key, value in expected.items():
        declared = _text(mapping.get(key))
        if declared and declared != value:
            raise InterviewV2StructureError(
                "STRUCTURE_INPUT_VERSION_MISMATCH",
                "结构化输入引用了不一致的工作区版本。",
                {"field": key},
            )
    snapshot_sha = _text(snapshot.get("snapshot_sha256"))
    mapped_snapshot_sha = _text(mapping.get("base_snapshot_sha256"))
    if not snapshot_sha or snapshot_sha != mapped_snapshot_sha:
        raise InterviewV2StructureError(
            "STRUCTURE_INPUT_VERSION_MISMATCH",
            "分组版本与物理快照不一致。",
        )
    for group in mapping.get("groups") or []:
        if group.get("decision_status") != "confirmed":
            raise InterviewV2StructureError(
                "MAPPING_NOT_CONFIRMED",
                "只有已确认的分组版本可以进入结构化。",
            )
        for sheet in group.get("sheets") or []:
            if sheet.get("decision_status") != "confirmed":
                raise InterviewV2StructureError(
                    "MAPPING_NOT_CONFIRMED",
                    "只有已确认的 Sheet 角色可以进入结构化。",
                )
        for participant in group.get("participants") or []:
            if participant.get("decision_status") != "confirmed":
                raise InterviewV2StructureError(
                    "MAPPING_NOT_CONFIRMED",
                    "只有已确认的玩家绑定可以进入结构化。",
                )
            for column in participant.get("columns") or []:
                if column.get("decision_status") not in {None, "confirmed"}:
                    raise InterviewV2StructureError(
                        "MAPPING_NOT_CONFIRMED",
                        "只有已确认的玩家列绑定可以进入结构化。",
                    )


def build_structure(
    snapshot: dict[str, Any],
    mapping: dict[str, Any],
    *,
    project_id: str,
    import_id: str,
    workbook_revision_id: str,
    mapping_revision_id: str,
    mapping_sha256: str,
) -> dict[str, Any]:
    """Build modules, canonical main questions, occurrences, and review issues."""

    _validate_inputs(
        snapshot,
        mapping,
        project_id=project_id,
        import_id=import_id,
        workbook_revision_id=workbook_revision_id,
        mapping_revision_id=mapping_revision_id,
        mapping_sha256=mapping_sha256,
    )
    sheets_by_id = {
        _text(sheet.get("sheet_id")): sheet
        for sheet in snapshot.get("sheets") or []
        if isinstance(sheet, dict) and _text(sheet.get("sheet_id"))
    }
    modules_by_key: dict[str, dict[str, Any]] = {}
    modules_by_id: dict[str, dict[str, Any]] = {}
    questions_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    questions_by_id: dict[str, dict[str, Any]] = {}
    occurrences: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    groups = sorted(
        (mapping.get("groups") or []),
        key=lambda item: _text(item.get("group_id")),
    )
    for group in groups:
        group_id = _text(group.get("group_id"))
        if not group_id:
            raise InterviewV2StructureError(
                "CONFIRMED_MAPPING_INVALID",
                "已确认分组缺少组标识。",
            )
        assigned_sheets = sorted(
            (group.get("sheets") or []),
            key=lambda item: (int(item.get("index") or 0), _text(item.get("sheet_id"))),
        )
        for assignment in assigned_sheets:
            sheet_role = _text(assignment.get("role"))
            if sheet_role == "attribute_reference":
                continue
            if sheet_role not in {"record", "guide_reference"}:
                continue
            sheet_id = _text(assignment.get("sheet_id"))
            sheet = sheets_by_id.get(sheet_id)
            if sheet is None:
                raise InterviewV2StructureError(
                    "CONFIRMED_MAPPING_INVALID",
                    "已确认分组引用了不存在的 Sheet。",
                    {"sheet_id": sheet_id},
                )
            participants = _participant_columns(group, sheet_id)
            columns = _structure_columns(sheet, participants)
            by_position, by_row = _sheet_cells(sheet)
            roles, header_rows = _column_roles(sheet, columns, by_position)
            if "row_type" not in roles or "prompt" not in roles:
                issues.append(
                    _issue(
                        "STRUCTURE_COLUMNS_UNRESOLVED",
                        "无法仅依据显式表头确定行类型列和问题列。",
                        severity="blocking",
                        suggested_action="fix_structure_headers_and_reupload",
                        allowed_resolutions=[],
                        affected_ids={"sheet_ids": [sheet_id]},
                        source_context={
                            "group_id": group_id,
                            "sheet_id": sheet_id,
                            "row": None,
                        },
                        reason="explicit_structure_headers_missing",
                        report_impact="该 Sheet 的提纲与证据身份不能可靠确定。",
                    )
                )
            rows = sorted(by_row)
            current_module_id: str | None = None
            current_main_occurrence_id: str | None = None
            current_main_question_id: str | None = None
            for row in rows:
                if row in header_rows:
                    continue
                participant_cells = [
                    by_position.get((row, column)) for column in participants
                ]
                participant_has_content = any(
                    _cell_text(cell) for cell in participant_cells if cell is not None
                )
                raw_module = _cell_text(
                    by_position.get((row, roles.get("module", -1)))
                )
                raw_type = _cell_text(
                    by_position.get((row, roles.get("row_type", -1)))
                )
                raw_prompt = _cell_text(
                    by_position.get((row, roles.get("prompt", -1)))
                )
                if not any((raw_module, raw_type, raw_prompt, participant_has_content)):
                    continue

                role = _row_role(raw_type)
                if (
                    role == "unknown"
                    and not raw_type
                    and raw_module
                    and not raw_prompt
                    and not participant_has_content
                    and "module" in roles
                ):
                    role = "module_header"
                if role == "module_header" and not raw_module and raw_prompt:
                    raw_module = raw_prompt
                    raw_prompt = ""

                if raw_module:
                    module_key = _question_key(raw_module)
                    if module_key:
                        module = modules_by_key.get(module_key)
                        if module is None:
                            module = {
                                "module_id": _stable_id(
                                    "module", project_id, module_key
                                ),
                                "canonical_name": raw_module,
                                "normalized_key": module_key,
                                "raw_titles": [],
                                "occurrence_ids": [],
                                "mapping_method": "normalized_exact",
                                "decision_status": "proposed",
                                "decision_source": "deterministic_rule",
                                "confidence": 1.0,
                                "confirmed_by": None,
                                "confirmed_at": None,
                            }
                            modules_by_key[module_key] = module
                            modules_by_id[module["module_id"]] = module
                        if raw_module not in module["raw_titles"]:
                            module["raw_titles"].append(raw_module)
                        new_module_id = module["module_id"]
                        if new_module_id != current_module_id:
                            current_main_occurrence_id = None
                            current_main_question_id = None
                        current_module_id = new_module_id
                    else:
                        current_module_id = None
                        current_main_occurrence_id = None
                        current_main_question_id = None

                occurrence_id = _stable_id(
                    "occ", workbook_revision_id, sheet_id, row
                )
                parent_occurrence_id = None
                canonical_question_id = None
                mapping_method = "explicit_row_type"
                confidence = 1.0 if role != "unknown" else 0.0
                decision_status = "proposed" if role != "unknown" else "needs_review"

                if role == "module_header":
                    current_main_occurrence_id = None
                    current_main_question_id = None
                elif role == "main_question":
                    if current_module_id and raw_prompt:
                        question_key = _question_key(raw_prompt)
                        if question_key:
                            key = (current_module_id, question_key)
                            question = questions_by_key.get(key)
                            if question is None:
                                question = {
                                    "main_question_id": _stable_id(
                                        "question", project_id, current_module_id, question_key
                                    ),
                                    "module_id": current_module_id,
                                    "canonical_text": raw_prompt,
                                    "normalized_key": question_key,
                                    "raw_prompts": [],
                                    "occurrence_ids": [],
                                    "alignment_method": "normalized_exact_within_module",
                                    "decision_status": "proposed",
                                    "decision_source": "deterministic_rule",
                                    "confidence": 1.0,
                                    "confirmed_by": None,
                                    "confirmed_at": None,
                                }
                                questions_by_key[key] = question
                                questions_by_id[question["main_question_id"]] = question
                                mapping_method = "create_new_from_explicit_main_question"
                            else:
                                mapping_method = (
                                    "raw_exact_within_module"
                                    if raw_prompt == question["canonical_text"]
                                    else "normalized_exact_within_module"
                                )
                            if raw_prompt not in question["raw_prompts"]:
                                question["raw_prompts"].append(raw_prompt)
                            canonical_question_id = question["main_question_id"]
                            current_main_occurrence_id = occurrence_id
                            current_main_question_id = canonical_question_id
                        else:
                            current_main_occurrence_id = None
                            current_main_question_id = None
                    else:
                        current_main_occurrence_id = None
                        current_main_question_id = None
                elif role in {"follow_up", "observation_row"}:
                    if current_main_occurrence_id is not None:
                        parent_occurrence_id = current_main_occurrence_id
                        canonical_question_id = current_main_question_id
                        mapping_method = "inherit_previous_main_question"
                    else:
                        mapping_method = "parent_missing"
                        decision_status = "needs_review"
                        confidence = 0.0

                occurrence = {
                    "occurrence_id": occurrence_id,
                    "group_id": group_id,
                    "sheet_id": sheet_id,
                    "sheet_name": _text(sheet.get("name")),
                    "recorder_label": _text(assignment.get("recorder_label")),
                    "row": row,
                    "row_role": role,
                    "raw_module_text": raw_module or None,
                    "raw_type_text": raw_type or None,
                    "raw_prompt_text": raw_prompt or None,
                    "canonical_module_id": current_module_id,
                    "canonical_main_question_id": canonical_question_id,
                    "parent_main_occurrence_id": parent_occurrence_id,
                    "mapping_method": mapping_method,
                    "confidence": confidence,
                    "decision_status": decision_status,
                    "decision_source": "deterministic_rule",
                    "confirmed_by": None,
                    "confirmed_at": None,
                    "has_participant_content": participant_has_content,
                }
                occurrences.append(occurrence)
                if current_module_id:
                    modules_by_id[current_module_id]["occurrence_ids"].append(
                        occurrence_id
                    )
                if canonical_question_id:
                    questions_by_id[canonical_question_id]["occurrence_ids"].append(
                        occurrence_id
                    )

                affected = {"occurrence_ids": [occurrence_id]}
                source_context = {
                    "group_id": group_id,
                    "sheet_id": sheet_id,
                    "row": row,
                }
                if role == "unknown":
                    issues.append(
                        _issue(
                            "ROW_ROLE_UNKNOWN",
                            "该行没有可验证的显式行类型。",
                            severity="blocking" if participant_has_content else "recommended",
                            suggested_action="assign_row_role",
                            allowed_resolutions=["assign_row_role"],
                            affected_ids=affected,
                            source_context=source_context,
                            reason="explicit_row_type_missing_or_unknown",
                            report_impact=(
                                "该行证据身份和问题归属在确认前不可用于报告。"
                            ),
                        )
                    )
                if role in {"follow_up", "observation_row"} and not parent_occurrence_id:
                    code = (
                        "FOLLOW_UP_PARENT_MISSING"
                        if role == "follow_up"
                        else "OBSERVATION_PARENT_MISSING"
                    )
                    issues.append(
                        _issue(
                            code,
                            "该行找不到同 Sheet、同模块内可继承的上方主问题。",
                            severity="blocking",
                            suggested_action="assign_main_question",
                            allowed_resolutions=["assign_main_question"],
                            affected_ids=affected,
                            source_context=source_context,
                            reason="nearest_main_question_not_found",
                            report_impact="该行证据在确认父问题前不可用于报告。",
                        )
                    )
                if role == "main_question" and not canonical_question_id:
                    issues.append(
                        _issue(
                            (
                                "MAIN_QUESTION_TEXT_MISSING"
                                if not raw_prompt
                                else "MAIN_QUESTION_TEXT_INVALID"
                            ),
                            (
                                "主问题行缺少问题文本。"
                                if not raw_prompt
                                else "主问题文本规范化后为空，不能建立稳定问题标识。"
                            ),
                            severity="blocking",
                            suggested_action="assign_row_role",
                            allowed_resolutions=["assign_row_role"],
                            affected_ids=affected,
                            source_context=source_context,
                            reason="explicit_main_question_without_prompt",
                            report_impact="无法建立可引用的标准主问题。",
                        )
                    )
                if role in {"main_question", "follow_up", "observation_row"} and not current_module_id:
                    issues.append(
                        _issue(
                            "MODULE_CONTEXT_MISSING",
                            "该行之前没有可继承的明确功能模块。",
                            severity="blocking",
                            suggested_action="assign_module",
                            allowed_resolutions=["assign_module"],
                            affected_ids=affected,
                            source_context=source_context,
                            reason="explicit_module_context_missing",
                            report_impact="该行在确认模块归属前不可进入模块分析。",
                        )
                    )

    occurrences.sort(
        key=lambda item: (
            item["group_id"],
            item["sheet_id"],
            item["row"],
            item["occurrence_id"],
        )
    )
    modules = sorted(modules_by_key.values(), key=lambda item: item["module_id"])
    questions = sorted(
        questions_by_key.values(), key=lambda item: item["main_question_id"]
    )
    for module in modules:
        module["raw_titles"].sort()
        module["occurrence_ids"] = sorted(set(module["occurrence_ids"]))
    for question in questions:
        question["raw_prompts"].sort()
        question["occurrence_ids"] = sorted(set(question["occurrence_ids"]))
    if not modules:
        for issue in issues:
            if issue.get("code") == "MODULE_CONTEXT_MISSING":
                issue["suggested_action"] = "fix_structure_headers_and_reupload"
                issue["allowed_resolutions"] = []
    issues.sort(key=lambda item: item["issue_id"])
    source = {
        "project_id": project_id,
        "import_id": import_id,
        "workbook_revision_id": workbook_revision_id,
        "base_snapshot_sha256": _text(snapshot.get("snapshot_sha256")),
        "mapping_revision_id": mapping_revision_id,
        "mapping_sha256": mapping_sha256,
        "rules_version": STRUCTURE_RULES_VERSION,
    }
    return {
        "structure": {
            "structure_schema_version": STRUCTURE_SCHEMA_VERSION,
            "source": source,
            "modules": modules,
            "main_questions": questions,
            "occurrences": occurrences,
        },
        "review_issues": issues,
    }


__all__ = [
    "InterviewV2StructureError",
    "STRUCTURE_RULES_VERSION",
    "STRUCTURE_SCHEMA_VERSION",
    "build_structure",
]
