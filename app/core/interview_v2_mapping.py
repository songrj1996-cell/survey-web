"""访谈报告 V2 的确定性 Sheet 分组与玩家列映射。

本模块只消费批次一生成的物理快照，不读写文件、不做权限判断，也不调用
生成式模型。Sheet 名和列头只能形成建议；只有用户提交的完整映射才会被
规范化为 confirmed 决策。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any

from openpyxl.utils import get_column_letter

from app.core.config import INTERVIEW_V2_MAX_COLUMNS_PER_SHEET


PROPOSAL_SCHEMA_VERSION = "interview-group-proposal/1.0"
MAPPING_SCHEMA_VERSION = "interview-group-mapping/1.0"
_RECORD_ROLES = {"record"}
_REFERENCE_ROLES = {"guide_reference", "attribute_reference"}
_ALLOWED_ROLES = _RECORD_ROLES | _REFERENCE_ROLES
_PARTICIPANT_HEADER_RE = re.compile(
    r"^(?:p(?:layer)?|participant|user|玩家|用户|受访者|访谈对象)"
    r"[\s_\-:#（）()]*[a-z0-9一二三四五六七八九十]+$",
    re.IGNORECASE,
)
_GROUP_PATTERNS = (
    re.compile(r"(?i)(?:第\s*)?([0-9一二三四五六七八九十百]+)\s*[组組]"),
    re.compile(r"(?i)\bgroup\s*[-_ ]*([a-z0-9]+)\b"),
)
_RECORDER_PATTERNS = (
    re.compile(r"(?i)(记录(?:员)?\s*[-_ ]*[a-z0-9一二三四五六七八九十百]+)"),
    re.compile(r"(?i)\b(recorder\s*[-_ ]*[a-z0-9]+)\b"),
)


class InterviewV2MappingError(Exception):
    """表示无法安全表示或规范化的映射请求。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.context = deepcopy(context or {})


def _stable_id(prefix: str, *parts: object) -> str:
    try:
        payload = "\0".join(str(part) for part in parts).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InterviewV2MappingError(
            "MAPPING_REQUEST_INVALID",
            "映射文本包含无效的 Unicode 字符。",
        ) from exc
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:32]}"


def _normalized_text(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "").strip())


def _participant_key(value: object) -> str:
    return _normalized_text(value).casefold()


def _issue(
    code: str,
    message: str,
    *,
    level: str,
    suggested_action: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "level": level,
        "suggested_action": suggested_action,
        "context": deepcopy(context or {}),
    }


def _ensure_unicode_scalars(value: Any) -> None:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise InterviewV2MappingError(
                "MAPPING_REQUEST_INVALID",
                "映射文本包含无效的 Unicode 字符。",
            ) from exc
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _ensure_unicode_scalars(key)
            _ensure_unicode_scalars(item)
        return
    if isinstance(value, list):
        for item in value:
            _ensure_unicode_scalars(item)


def _snapshot_sheets(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("sheets"), list):
        raise InterviewV2MappingError(
            "MAPPING_INPUT_INVALID",
            "工作簿物理快照中的 Sheet 列表无效。",
        )
    sheets = snapshot["sheets"]
    if any(not isinstance(item, dict) for item in sheets):
        raise InterviewV2MappingError(
            "MAPPING_INPUT_INVALID",
            "工作簿物理快照中的 Sheet 记录无效。",
            context={"sheet_count": len(sheets)},
        )
    identifiers = [str(sheet.get("sheet_id") or "") for sheet in sheets]
    if any(not identifier for identifier in identifiers) or len(set(identifiers)) != len(
        identifiers
    ):
        raise InterviewV2MappingError(
            "MAPPING_INPUT_INVALID",
            "工作簿物理快照中的 Sheet 标识无效。",
            context={"sheet_count": len(sheets)},
        )
    indexes: list[int] = []
    try:
        for sheet in sheets:
            value = sheet.get("index")
            if isinstance(value, bool):
                raise ValueError("boolean Sheet index")
            indexes.append(int(value or 0))
    except (TypeError, ValueError) as exc:
        raise InterviewV2MappingError(
            "MAPPING_INPUT_INVALID",
            "工作簿物理快照中的 Sheet 顺序无效。",
            context={"sheet_count": len(sheets)},
        ) from exc
    indexed_sheets = list(zip(indexes, sheets, strict=True))
    return [
        sheet
        for _, sheet in sorted(
            indexed_sheets,
            key=lambda item: (item[0], str(item[1].get("sheet_id"))),
        )
    ]


def _candidate_columns(sheet: dict[str, Any]) -> list[dict[str, Any]]:
    region = sheet.get("candidate_participant_region")
    if not isinstance(region, dict):
        return []
    try:
        start = int(region.get("start_column") or 0)
        end = int(region.get("end_column") or 0)
    except (TypeError, ValueError):
        return []
    if (
        start < 1
        or end < start
        or end > INTERVIEW_V2_MAX_COLUMNS_PER_SHEET
    ):
        return []

    basis = {
        str(item)
        for item in region.get("basis") or []
        if isinstance(item, str)
    }
    header_row = region.get("header_row")
    explicit_header_contract = basis == {"participant_like_column_headers"}

    profiles = {
        int(profile.get("column")): profile
        for profile in sheet.get("column_profiles") or []
        if isinstance(profile, dict)
        and isinstance(profile.get("column"), int)
    }
    result: list[dict[str, Any]] = []
    for column_index in range(start, end + 1):
        profile = profiles.get(column_index, {})
        header = profile.get("header_value")
        try:
            first_non_empty_row = int(profile.get("first_non_empty_row") or 0)
        except (TypeError, ValueError):
            first_non_empty_row = 0
        raw_header = ""
        if (
            explicit_header_contract
            and header is not None
            and first_non_empty_row == header_row
            and _PARTICIPANT_HEADER_RE.fullmatch(str(header).strip())
        ):
            raw_header = str(header).strip()
        result.append(
            {
                "sheet_id": str(sheet.get("sheet_id")),
                "column_index": column_index,
                "column_letter": str(
                    profile.get("column_letter") or get_column_letter(column_index)
                ),
                "raw_header": raw_header,
                "header_address": profile.get("header_address"),
            }
        )
    return result


def _name_hints(name: str) -> tuple[str | None, str]:
    normalized = _normalized_text(name)
    group_hint: str | None = None
    for pattern in _GROUP_PATTERNS:
        match = pattern.search(normalized)
        if match:
            group_hint = _participant_key(match.group(1))
            break
    recorder = ""
    for pattern in _RECORDER_PATTERNS:
        match = pattern.search(normalized)
        if match:
            recorder = re.sub(r"\s+", "", match.group(1).strip())
            break
    return group_hint, recorder


def _group_id(project_id: str, sheet_ids: list[str]) -> str:
    return _stable_id("group", project_id, *sorted(sheet_ids))


def _new_group_id(
    project_id: str,
    sheet_ids: list[str],
    *,
    identity_role: str,
    target_mapping_revision: int,
) -> str:
    return _stable_id(
        "group",
        project_id,
        "new_in_mapping_revision",
        target_mapping_revision,
        identity_role,
        *sorted(sheet_ids),
    )


def _proposal_participants(
    *, project_id: str, group_id: str, sheets: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_header: dict[str, list[dict[str, Any]]] = defaultdict(list)
    header_counts_by_sheet: dict[str, Counter[str]] = {}
    all_columns: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    for sheet in sheets:
        columns = _candidate_columns(sheet)
        all_columns.extend(columns)
        counts = Counter(
            _participant_key(column["raw_header"])
            for column in columns
            if column["raw_header"]
        )
        header_counts_by_sheet[str(sheet.get("sheet_id"))] = counts
        for header, count in counts.items():
            if count > 1:
                issues.append(
                    _issue(
                        "PARTICIPANT_COLUMN_DUPLICATE",
                        "同一 Sheet 中存在重复玩家列头，需要人工区分。",
                        level="confirmation_required",
                        suggested_action="review_participant_columns",
                        context={
                            "sheet_id": str(sheet.get("sheet_id")),
                            "duplicate_count": count,
                        },
                    )
                )
        for column in columns:
            key = _participant_key(column["raw_header"])
            if key and counts[key] == 1:
                by_header[key].append(column)

    assigned: set[tuple[str, int]] = set()
    participants: list[dict[str, Any]] = []
    for header_key in sorted(by_header):
        columns = by_header[header_key]
        if len({column["sheet_id"] for column in columns}) != len(columns):
            continue
        label = columns[0]["raw_header"]
        participant_id = _stable_id("participant", project_id, group_id, header_key)
        proposal_columns = []
        for column in sorted(
            columns, key=lambda item: (item["sheet_id"], item["column_index"])
        ):
            assigned.add((column["sheet_id"], column["column_index"]))
            proposal_columns.append(
                {
                    **column,
                    "binding_id": _stable_id(
                        "binding", column["sheet_id"], column["column_index"]
                    ),
                    "participant_id": participant_id,
                    "mapping_method": "exact_header",
                    "confidence": 1.0,
                    "decision_status": "proposed",
                    "decision_source": "deterministic_rule",
                }
            )
        participants.append(
            {
                "participant_id": participant_id,
                "participant_label": label,
                "decision_status": "proposed",
                "decision_source": "deterministic_rule",
                "columns": proposal_columns,
            }
        )

    for column in sorted(
        all_columns, key=lambda item: (item["sheet_id"], item["column_index"])
    ):
        key = (column["sheet_id"], column["column_index"])
        if key in assigned:
            continue
        label = column["raw_header"] or column["column_letter"]
        participant_id = _stable_id(
            "participant", project_id, group_id, column["sheet_id"], column["column_index"]
        )
        participants.append(
            {
                "participant_id": participant_id,
                "participant_label": label,
                "decision_status": "proposed",
                "decision_source": "deterministic_rule",
                "columns": [
                    {
                        **column,
                        "binding_id": _stable_id(
                            "binding", column["sheet_id"], column["column_index"]
                        ),
                        "participant_id": participant_id,
                        "mapping_method": "suggested_by_context",
                        "confidence": 0.0,
                        "decision_status": "proposed",
                        "decision_source": "deterministic_rule",
                    }
                ],
            }
        )
    return participants, issues


def _preview(groups: list[dict[str, Any]]) -> dict[str, Any]:
    participants: list[dict[str, Any]] = []
    for group in groups:
        for participant in group.get("participants") or []:
            sources = [
                {
                    "sheet_id": column.get("sheet_id"),
                    "column_index": column.get("column_index"),
                    "column_letter": column.get("column_letter"),
                }
                for column in participant.get("columns") or []
            ]
            participants.append(
                {
                    "group_id": group.get("group_id"),
                    "group_display_name": group.get("display_name"),
                    "participant_id": participant.get("participant_id"),
                    "participant_label": participant.get("participant_label"),
                    "decision_status": participant.get("decision_status"),
                    "sources": sources,
                }
            )
    return {
        "participant_count": len(participants),
        "participants": participants,
    }


def build_group_proposals(
    snapshot: dict[str, Any],
    *,
    project_id: str,
    import_id: str,
    workbook_revision_id: str,
) -> dict[str, Any]:
    """从物理快照生成只供确认的确定性建议。"""

    sheets = _snapshot_sheets(snapshot)
    record_sheets: list[dict[str, Any]] = []
    unassigned_sheets: list[dict[str, Any]] = []
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for sheet in sheets:
        columns = _candidate_columns(sheet)
        group_hint, recorder = _name_hints(str(sheet.get("name") or ""))
        public_sheet = {
            "sheet_id": str(sheet.get("sheet_id")),
            "index": int(sheet.get("index") or 0),
            "name": str(sheet.get("name") or ""),
            "state": str(sheet.get("state") or "visible"),
            "role": "record" if columns else "unknown",
            "recorder_label": recorder,
            "decision_status": "proposed",
            "candidate_columns": columns,
        }
        if not columns:
            unassigned_sheets.append(public_sheet)
            continue
        record_sheets.append(sheet)
        bucket_key = f"named:{group_hint}" if group_hint else f"sheet:{sheet['sheet_id']}"
        buckets[bucket_key].append(sheet)

    groups: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for position, bucket_key in enumerate(sorted(buckets), start=1):
        bucket = buckets[bucket_key]
        sheet_ids = [str(sheet.get("sheet_id")) for sheet in bucket]
        group_id = _group_id(project_id, sheet_ids)
        participants, participant_issues = _proposal_participants(
            project_id=project_id,
            group_id=group_id,
            sheets=bucket,
        )
        issues.extend(participant_issues)
        group_hint, _ = _name_hints(str(bucket[0].get("name") or ""))
        display_name = f"第{group_hint}组" if group_hint else f"建议组 {position}"
        group_sheets = []
        for sheet in bucket:
            _, recorder = _name_hints(str(sheet.get("name") or ""))
            group_sheets.append(
                {
                    "sheet_id": str(sheet.get("sheet_id")),
                    "index": int(sheet.get("index") or 0),
                    "name": str(sheet.get("name") or ""),
                    "state": str(sheet.get("state") or "visible"),
                    "role": "record",
                    "recorder_label": recorder,
                    "decision_status": "proposed",
                    "candidate_columns": _candidate_columns(sheet),
                }
            )
        groups.append(
            {
                "group_id": group_id,
                "display_name": display_name,
                "decision_status": "proposed",
                "basis": [
                    "sheet_name_group_hint"
                    if bucket_key.startswith("named:")
                    else "separate_sheet_pending_confirmation"
                ],
                "sheets": group_sheets,
                "participants": participants,
            }
        )

    groups.sort(
        key=lambda group: (
            min(int(sheet.get("index") or 0) for sheet in group["sheets"]),
            group["group_id"],
        )
    )

    if groups:
        issues.append(
            _issue(
                "GROUP_MAPPING_CONFIRMATION_REQUIRED",
                "Sheet 分组建议必须由用户整体确认。",
                level="confirmation_required",
                suggested_action="confirm_sheet_groups",
                context={"group_count": len(groups)},
            )
        )
        issues.append(
            _issue(
                "PARTICIPANT_MAPPING_CONFIRMATION_REQUIRED",
                "玩家列对应建议必须由用户整体确认。",
                level="confirmation_required",
                suggested_action="confirm_participant_mapping",
                context={"record_sheet_count": len(record_sheets)},
            )
        )
    for sheet in unassigned_sheets:
        issues.append(
            _issue(
                "SHEET_ROLE_AMBIGUOUS",
                "未发现玩家候选列，请确认 Sheet 是参考页还是不参与分析。",
                level="confirmation_required",
                suggested_action="assign_sheet_role",
                context={"sheet_id": sheet["sheet_id"]},
            )
        )

    return {
        "proposal_schema_version": PROPOSAL_SCHEMA_VERSION,
        "project_id": project_id,
        "import_id": import_id,
        "workbook_revision_id": workbook_revision_id,
        "source_snapshot_sha256": str(snapshot.get("snapshot_sha256") or ""),
        "groups": groups,
        "ignored_sheets": [],
        "unassigned_sheets": unassigned_sheets,
        "issues": issues,
        "confirmation_ready": False,
        "final_participant_preview": _preview(groups),
    }


def _mapping_error(
    code: str, message: str, **context: Any
) -> InterviewV2MappingError:
    return InterviewV2MappingError(code, message, context=context)


def _base_identity_catalog(
    base_mapping: dict[str, Any] | None,
    *,
    project_id: str,
    import_id: str,
    workbook_revision_id: str,
) -> dict[str, Any] | None:
    """只信任当前服务端基线中已经签发的组和玩家身份。"""

    if base_mapping is None:
        return None
    if not isinstance(base_mapping, dict):
        raise _mapping_error(
            "BASE_MAPPING_INVALID",
            "当前分组映射基线无效，请刷新后重试。",
        )
    expected_identity = {
        "project_id": project_id,
        "import_id": import_id,
        "workbook_revision_id": workbook_revision_id,
    }
    if any(
        str(base_mapping.get(key) or "") != expected
        for key, expected in expected_identity.items()
    ):
        raise _mapping_error(
            "BASE_MAPPING_INVALID",
            "当前分组映射基线与工作簿版本不一致，请刷新后重试。",
        )
    base_groups = base_mapping.get("groups")
    if not isinstance(base_groups, list):
        raise _mapping_error(
            "BASE_MAPPING_INVALID",
            "当前分组映射基线无效，请刷新后重试。",
        )

    groups: dict[str, dict[str, Any]] = {}
    sheet_owner: dict[str, str] = {}
    participant_owner: dict[str, str] = {}
    for group in base_groups:
        if not isinstance(group, dict):
            raise _mapping_error(
                "BASE_MAPPING_INVALID",
                "当前分组映射基线无效，请刷新后重试。",
            )
        group_id = str(group.get("group_id") or "").strip()
        sheets = group.get("sheets")
        participants = group.get("participants")
        if (
            not group_id
            or group_id in groups
            or not isinstance(sheets, list)
            or not isinstance(participants, list)
        ):
            raise _mapping_error(
                "BASE_MAPPING_INVALID",
                "当前分组映射基线无效，请刷新后重试。",
            )
        sheet_ids: set[str] = set()
        record_sheet_ids: set[str] = set()
        participant_ids: set[str] = set()
        for sheet in sheets:
            if not isinstance(sheet, dict):
                raise _mapping_error(
                    "BASE_MAPPING_INVALID",
                    "当前分组映射基线无效，请刷新后重试。",
                )
            sheet_id = str(sheet.get("sheet_id") or "").strip()
            if not sheet_id or sheet_id in sheet_ids or sheet_id in sheet_owner:
                raise _mapping_error(
                    "BASE_MAPPING_INVALID",
                    "当前分组映射基线无效，请刷新后重试。",
                )
            sheet_ids.add(sheet_id)
            if sheet.get("role") == "record":
                record_sheet_ids.add(sheet_id)
        for identity_sheet_id in record_sheet_ids or sheet_ids:
            sheet_owner[identity_sheet_id] = group_id
        for participant in participants:
            if not isinstance(participant, dict):
                raise _mapping_error(
                    "BASE_MAPPING_INVALID",
                    "当前分组映射基线无效，请刷新后重试。",
                )
            participant_id = str(participant.get("participant_id") or "").strip()
            if (
                not participant_id
                or participant_id in participant_ids
                or participant_id in participant_owner
            ):
                raise _mapping_error(
                    "BASE_MAPPING_INVALID",
                    "当前分组映射基线无效，请刷新后重试。",
                )
            participant_ids.add(participant_id)
            participant_owner[participant_id] = group_id
        groups[group_id] = {
            "sheet_ids": sheet_ids,
            "participant_ids": participant_ids,
            "identity_role": "record" if record_sheet_ids else "reference",
        }
    return {
        "groups": groups,
        "sheet_owner": sheet_owner,
        "participant_owner": participant_owner,
    }


def _requested_group_ancestry(
    requested_groups: list[Any],
    base_catalog: dict[str, Any] | None,
) -> tuple[list[set[str]], dict[str, set[int]]]:
    """计算每个新组消费了哪些旧组，用于拦截合并和拆分继承。"""

    if base_catalog is None:
        return [set() for _ in requested_groups], {}
    sheet_owner = base_catalog["sheet_owner"]
    ancestries: list[set[str]] = []
    consumers: dict[str, set[int]] = defaultdict(set)
    for position, requested_group in enumerate(requested_groups):
        if not isinstance(requested_group, dict):
            raise _mapping_error(
                "GROUP_MAPPING_INVALID",
                "访谈组结构无效。",
                group_index=position,
            )
        ancestry: set[str] = set()
        requested_sheets = [
            sheet
            for sheet in requested_group.get("sheets") or []
            if isinstance(sheet, dict)
        ]
        record_sheets = [
            sheet for sheet in requested_sheets if sheet.get("role") == "record"
        ]
        for sheet in record_sheets or requested_sheets:
            if not isinstance(sheet, dict):
                continue
            owner = sheet_owner.get(str(sheet.get("sheet_id") or ""))
            if (
                owner
                and not record_sheets
                and base_catalog["groups"][owner]["identity_role"] != "reference"
            ):
                owner = None
            if owner:
                ancestry.add(owner)
                consumers[owner].add(position)
        ancestries.append(ancestry)
    return ancestries, consumers


def normalize_and_validate_mapping(
    snapshot: dict[str, Any],
    mapping: dict[str, Any],
    *,
    project_id: str,
    import_id: str,
    workbook_revision_id: str,
    base_mapping: dict[str, Any] | None = None,
    target_mapping_revision: int = 1,
) -> dict[str, Any]:
    """规范化用户提交的完整映射，并返回确认质量门结果。"""

    _ensure_unicode_scalars(mapping)
    if (
        isinstance(target_mapping_revision, bool)
        or not isinstance(target_mapping_revision, int)
        or target_mapping_revision < 1
    ):
        raise _mapping_error(
            "BASE_MAPPING_INVALID",
            "目标映射版本无效，请刷新后重试。",
        )
    sheets = _snapshot_sheets(snapshot)
    by_sheet_id = {str(sheet.get("sheet_id")): sheet for sheet in sheets}
    requested_groups = mapping.get("groups") or []
    if not isinstance(requested_groups, list):
        raise _mapping_error(
            "GROUP_MAPPING_INVALID",
            "访谈组结构无效。",
        )
    base_catalog = _base_identity_catalog(
        base_mapping,
        project_id=project_id,
        import_id=import_id,
        workbook_revision_id=workbook_revision_id,
    )
    group_ancestries, base_group_consumers = _requested_group_ancestry(
        requested_groups,
        base_catalog,
    )
    ignored_sheet_ids = [str(item) for item in mapping.get("ignored_sheet_ids") or []]
    if len(set(ignored_sheet_ids)) != len(ignored_sheet_ids):
        raise _mapping_error(
            "SHEET_ASSIGNMENT_DUPLICATE",
            "同一 Sheet 不能被重复忽略。",
        )
    unknown_ignored = sorted(set(ignored_sheet_ids) - set(by_sheet_id))
    if unknown_ignored:
        raise _mapping_error(
            "SHEET_ASSIGNMENT_INVALID",
            "映射引用了不存在的 Sheet。",
            unknown_sheet_ids=unknown_ignored,
        )

    assigned_sheet_group: dict[str, int] = {}
    normalized_groups: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    any_record_sheet = False
    claimed_participant_ids: set[str] = set()

    for group_position, requested_group in enumerate(requested_groups):
        if not isinstance(requested_group, dict):
            raise _mapping_error(
                "GROUP_MAPPING_INVALID",
                "访谈组结构无效。",
                group_index=group_position,
            )
        group_sheets = requested_group.get("sheets") or []
        if not group_sheets:
            raise _mapping_error(
                "GROUP_MAPPING_INVALID",
                "访谈组至少需要包含一个 Sheet。",
                group_index=group_position,
            )
        normalized_sheet_ids: list[str] = []
        normalized_sheets: list[dict[str, Any]] = []
        sheet_roles: dict[str, str] = {}
        for sheet_assignment in group_sheets:
            sheet_id = str(sheet_assignment.get("sheet_id") or "")
            if sheet_id not in by_sheet_id:
                raise _mapping_error(
                    "SHEET_ASSIGNMENT_INVALID",
                    "映射引用了不存在的 Sheet。",
                    sheet_id=sheet_id,
                )
            if sheet_id in ignored_sheet_ids or sheet_id in assigned_sheet_group:
                raise _mapping_error(
                    "SHEET_ASSIGNMENT_DUPLICATE",
                    "同一 Sheet 必须且只能归入一个组或忽略列表。",
                    sheet_id=sheet_id,
                )
            role = str(sheet_assignment.get("role") or "")
            if role not in _ALLOWED_ROLES:
                raise _mapping_error(
                    "SHEET_ROLE_INVALID",
                    "Sheet 角色无效。",
                    sheet_id=sheet_id,
                )
            assigned_sheet_group[sheet_id] = group_position
            normalized_sheet_ids.append(sheet_id)
            sheet_roles[sheet_id] = role
            recorder_label = str(sheet_assignment.get("recorder_label") or "").strip()
            if role == "record":
                any_record_sheet = True
                if not recorder_label:
                    issues.append(
                        _issue(
                            "GROUP_MAPPING_CONFIRMATION_REQUIRED",
                            "记录 Sheet 需要填写记录员标识。",
                            level="confirmation_required",
                            suggested_action="set_recorder_label",
                            context={"sheet_id": sheet_id},
                        )
                    )
            normalized_sheets.append(
                {
                    "sheet_id": sheet_id,
                    "index": int(by_sheet_id[sheet_id].get("index") or 0),
                    "name": str(by_sheet_id[sheet_id].get("name") or ""),
                    "role": role,
                    "recorder_label": recorder_label,
                    "decision_status": "confirmed",
                    "decision_source": "user_selection",
                }
            )

        record_sheet_ids = [
            sheet_id
            for sheet_id in normalized_sheet_ids
            if sheet_roles.get(sheet_id) == "record"
        ]
        identity_sheet_ids = record_sheet_ids or normalized_sheet_ids
        identity_role = "record" if record_sheet_ids else "reference"
        generated_group_id = (
            _new_group_id(
                project_id,
                identity_sheet_ids,
                identity_role=identity_role,
                target_mapping_revision=target_mapping_revision,
            )
            if base_catalog is not None
            else _group_id(project_id, identity_sheet_ids)
        )
        requested_group_id_value = requested_group.get("group_id")
        requested_group_id = (
            str(requested_group_id_value).strip()
            if requested_group_id_value is not None
            else ""
        )
        inherited_group_id: str | None = None
        if base_catalog is not None and requested_group_id:
            if requested_group_id not in base_catalog["groups"]:
                raise _mapping_error(
                    "GROUP_ID_INVALID",
                    "组标识不是当前映射基线签发的标识，请刷新后重试。",
                    group_index=group_position,
                )
            if (
                base_catalog["groups"][requested_group_id]["identity_role"]
                != identity_role
            ):
                raise _mapping_error(
                    "GROUP_ID_INHERITANCE_INVALID",
                    "组的记录身份发生变化时不能继承旧组标识，请移除该标识后重试。",
                    group_index=group_position,
                )
            ancestry = group_ancestries[group_position]
            consumers = base_group_consumers.get(requested_group_id, set())
            if ancestry != {requested_group_id} or consumers != {group_position}:
                raise _mapping_error(
                    "GROUP_ID_INHERITANCE_INVALID",
                    "组发生合并或拆分时不能继承旧组标识，请移除该标识后重试。",
                    group_index=group_position,
                )
            inherited_group_id = requested_group_id
        group_id = inherited_group_id or generated_group_id
        participants: list[dict[str, Any]] = []
        bound_columns: dict[tuple[str, int], str] = {}
        requested_participants = requested_group.get("participants") or []
        participant_label_totals = Counter(
            _participant_key(participant.get("participant_label"))
            for participant in requested_participants
            if isinstance(participant, dict)
        )
        participant_labels: Counter[str] = Counter()
        for participant_position, requested_participant in enumerate(requested_participants):
            if not isinstance(requested_participant, dict):
                raise _mapping_error(
                    "PARTICIPANT_MAPPING_INVALID",
                    "玩家映射结构无效。",
                    group_index=group_position,
                    participant_index=participant_position,
                )
            label = _normalized_text(requested_participant.get("participant_label"))
            if not label:
                raise _mapping_error(
                    "PARTICIPANT_MAPPING_INVALID",
                    "玩家标识不能为空。",
                    group_index=group_position,
                    participant_index=participant_position,
                )
            requested_columns = requested_participant.get("columns") or []
            if not requested_columns:
                raise _mapping_error(
                    "PARTICIPANT_MAPPING_INVALID",
                    "每名玩家至少需要绑定一个玩家列。",
                    group_index=group_position,
                    participant_index=participant_position,
                )
            participant_labels[_participant_key(label)] += 1
            normalized_column_keys: list[tuple[str, int]] = []
            normalized_column_sources: list[dict[str, Any]] = []
            seen_sheets: set[str] = set()
            for requested_column in requested_columns:
                sheet_id = str(requested_column.get("sheet_id") or "")
                column_index = requested_column.get("column_index")
                if isinstance(column_index, bool) or not isinstance(column_index, int):
                    raise _mapping_error(
                        "PARTICIPANT_COLUMN_INVALID",
                        "玩家列索引无效。",
                        sheet_id=sheet_id,
                    )
                if sheet_id not in assigned_sheet_group:
                    if sheet_id in by_sheet_id:
                        raise _mapping_error(
                            "CROSS_GROUP_PARTICIPANT_MERGE_ATTEMPT",
                            "玩家列不能跨访谈组绑定。",
                            sheet_id=sheet_id,
                            column_index=column_index,
                        )
                    raise _mapping_error(
                        "PARTICIPANT_COLUMN_INVALID",
                        "玩家列引用了不存在的 Sheet。",
                        sheet_id=sheet_id,
                    )
                if assigned_sheet_group[sheet_id] != group_position:
                    raise _mapping_error(
                        "CROSS_GROUP_PARTICIPANT_MERGE_ATTEMPT",
                        "玩家列不能跨访谈组绑定。",
                        sheet_id=sheet_id,
                        column_index=column_index,
                    )
                if sheet_roles.get(sheet_id) != "record":
                    raise _mapping_error(
                        "PARTICIPANT_COLUMN_INVALID",
                        "只有记录 Sheet 的玩家候选列可以绑定玩家。",
                        sheet_id=sheet_id,
                        column_index=column_index,
                    )
                candidates = {
                    int(item["column_index"]): item
                    for item in _candidate_columns(by_sheet_id[sheet_id])
                }
                if column_index not in candidates:
                    raise _mapping_error(
                        "PARTICIPANT_COLUMN_INVALID",
                        "列不在该 Sheet 的玩家候选范围内。",
                        sheet_id=sheet_id,
                        column_index=column_index,
                    )
                key = (sheet_id, column_index)
                if key in bound_columns:
                    raise _mapping_error(
                        "PARTICIPANT_COLUMN_DUPLICATE",
                        "同一玩家列不能绑定给多名玩家。",
                        sheet_id=sheet_id,
                        column_index=column_index,
                    )
                if sheet_id in seen_sheets:
                    raise _mapping_error(
                        "PARTICIPANT_COLUMN_DUPLICATE",
                        "同一玩家在一个 Sheet 中最多绑定一列。",
                        sheet_id=sheet_id,
                    )
                seen_sheets.add(sheet_id)
                bound_columns[key] = label
                normalized_column_keys.append(key)
                normalized_column_sources.append(candidates[column_index])

            requested_participant_id_value = requested_participant.get("participant_id")
            requested_participant_id = (
                str(requested_participant_id_value).strip()
                if requested_participant_id_value is not None
                else ""
            )
            if requested_participant_id:
                if base_catalog is None or inherited_group_id is None:
                    raise _mapping_error(
                        "PARTICIPANT_ID_INVALID",
                        "玩家标识只能引用当前基线中同组的现有玩家。",
                        group_index=group_position,
                        participant_index=participant_position,
                    )
                owner = base_catalog["participant_owner"].get(
                    requested_participant_id
                )
                if owner != inherited_group_id:
                    raise _mapping_error(
                        "PARTICIPANT_ID_INVALID",
                        "玩家标识只能引用当前基线中同组的现有玩家。",
                        group_index=group_position,
                        participant_index=participant_position,
                    )
                if requested_participant_id in claimed_participant_ids:
                    raise _mapping_error(
                        "PARTICIPANT_ID_DUPLICATE",
                        "同一现有玩家标识不能在一次映射中重复使用。",
                        group_index=group_position,
                        participant_index=participant_position,
                    )
                participant_id = requested_participant_id
                claimed_participant_ids.add(participant_id)
            else:
                identity_parts: list[object] = [project_id, group_id]
                if base_catalog is not None:
                    identity_parts.extend(
                        [
                            "new_in_mapping_revision",
                            target_mapping_revision,
                            _participant_key(label),
                            *(
                                f"{sheet_id}:{column_index}"
                                for sheet_id, column_index in sorted(
                                    normalized_column_keys
                                )
                            ),
                        ]
                    )
                else:
                    identity_parts.append(_participant_key(label))
                    if participant_label_totals[_participant_key(label)] > 1:
                        identity_parts.append(f"duplicate:{participant_position}")
                participant_id = _stable_id("participant", *identity_parts)
                if (
                    participant_id in claimed_participant_ids
                    or (
                        base_catalog is not None
                        and participant_id in base_catalog["participant_owner"]
                    )
                ):
                    raise _mapping_error(
                        "PARTICIPANT_ID_DUPLICATE",
                        "新玩家标识与现有玩家冲突，请刷新后重试。",
                        group_index=group_position,
                        participant_index=participant_position,
                    )
                claimed_participant_ids.add(participant_id)
            bindings = []
            for source in sorted(
                normalized_column_sources,
                key=lambda item: (item["sheet_id"], item["column_index"]),
            ):
                bindings.append(
                    {
                        **source,
                        "binding_id": _stable_id(
                            "binding", source["sheet_id"], source["column_index"]
                        ),
                        "participant_id": participant_id,
                        "mapping_method": "user_confirmed",
                        "confidence": 1.0,
                        "decision_status": "confirmed",
                        "decision_source": "user_selection",
                    }
                )
            participants.append(
                {
                    "participant_id": participant_id,
                    "participant_label": label,
                    "decision_status": "confirmed",
                    "decision_source": "user_selection",
                    "columns": bindings,
                }
            )

        for label, count in participant_labels.items():
            if count > 1:
                issues.append(
                    _issue(
                        "PARTICIPANT_MAPPING_AMBIGUOUS",
                        "同一组内存在重复玩家标识，请重命名或合并。",
                        level="confirmation_required",
                        suggested_action="review_participant_mapping",
                        context={"group_id": group_id, "duplicate_count": count},
                    )
                )

        for sheet_id, role in sheet_roles.items():
            if role != "record":
                continue
            candidate_keys = {
                (sheet_id, int(column["column_index"]))
                for column in _candidate_columns(by_sheet_id[sheet_id])
            }
            if not candidate_keys:
                issues.append(
                    _issue(
                        "PARTICIPANT_COLUMN_MISSING",
                        "记录 Sheet 中没有可确认的玩家候选列。",
                        level="blocking",
                        suggested_action="review_sheet_role_or_source",
                        context={"sheet_id": sheet_id, "candidate_count": 0},
                    )
                )
                continue
            missing = sorted(candidate_keys - set(bound_columns))
            if missing:
                issues.append(
                    _issue(
                        "PARTICIPANT_COLUMN_MISSING",
                        "记录 Sheet 中仍有玩家候选列未绑定。",
                        level="blocking",
                        suggested_action="bind_all_participant_columns",
                        context={
                            "sheet_id": sheet_id,
                            "missing_column_indices": [item[1] for item in missing],
                            "missing_count": len(missing),
                        },
                    )
                )

        normalized_groups.append(
            {
                "group_id": group_id,
                "display_name": str(requested_group.get("display_name") or "").strip(),
                "decision_status": "confirmed",
                "decision_source": "user_selection",
                "sheets": sorted(
                    normalized_sheets,
                    key=lambda item: (item["index"], item["sheet_id"]),
                ),
                "participants": sorted(
                    participants,
                    key=lambda item: (
                        _participant_key(item["participant_label"]),
                        item["participant_id"],
                    ),
                ),
            }
        )

    unassigned = sorted(
        set(by_sheet_id) - set(assigned_sheet_group) - set(ignored_sheet_ids)
    )
    for sheet_id in unassigned:
        issues.append(
            _issue(
                "SHEET_ROLE_AMBIGUOUS",
                "Sheet 尚未归组或明确忽略。",
                level="confirmation_required",
                suggested_action="assign_sheet_role",
                context={"sheet_id": sheet_id},
            )
        )
    if not any_record_sheet:
        issues.append(
            _issue(
                "PARTICIPANT_COLUMN_MISSING",
                "至少需要保留一个包含玩家列的记录 Sheet。",
                level="blocking",
                suggested_action="assign_record_sheet",
                context={"record_sheet_count": 0},
            )
        )

    normalized_groups.sort(
        key=lambda item: (
            min(sheet["index"] for sheet in item["sheets"]),
            item["group_id"],
        )
    )
    normalized = {
        "mapping_schema_version": MAPPING_SCHEMA_VERSION,
        "base_snapshot_sha256": str(snapshot.get("snapshot_sha256") or ""),
        "project_id": project_id,
        "import_id": import_id,
        "workbook_revision_id": workbook_revision_id,
        "groups": normalized_groups,
        "ignored_sheet_ids": sorted(ignored_sheet_ids),
    }
    blocking_levels = {"blocking", "confirmation_required"}
    confirmation_ready = not any(
        issue.get("level") in blocking_levels for issue in issues
    )
    preview = _preview(normalized_groups)
    return {
        "mapping": normalized,
        "issues": issues,
        "confirmation_ready": confirmation_ready,
        "preview": preview,
        "final_participant_preview": preview,
    }
