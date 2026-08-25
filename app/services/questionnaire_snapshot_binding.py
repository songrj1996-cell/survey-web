"""将完整问卷快照与本次回答文件确定性绑定到现有问卷分析合同。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.core.research_assets import content_sha256
from app.schemas.questionnaire import (
    CanonicalQuestion,
    CanonicalQuestionType,
    MappingStatus,
    ResponseColumnBinding,
    ResponseColumnMapping,
)
from app.schemas.research_assets import Provider
from app.services.questionnaire_import import (
    BestedResponseQuestionDefinition,
    _parse_same_column_multi_value,
    match_bested_response_workbook,
    parse_questionnaire_response_rows,
)
from app.storage.research_assets import (
    SnapshotPackage,
    SnapshotPackageError,
    build_snapshot_package,
)


_BESTED_QUESTION_ID_RE = re.compile(r"^Q([1-9]\d*)$")
_BESTED_ROW_ID_RE = re.compile(r"^Q([1-9]\d*):row:([1-9]\d*)$")
_MATRIX_TYPES = frozenset({
    CanonicalQuestionType.MATRIX_SINGLE,
    CanonicalQuestionType.MATRIX_MULTI,
    CanonicalQuestionType.MATRIX_SCALE,
})
_NON_RESPONSE_TYPES = frozenset({
    CanonicalQuestionType.SECTION,
    CanonicalQuestionType.STATIC_TEXT,
})


def _norm(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


@dataclass(frozen=True, slots=True)
class SnapshotDetectedColumn:
    """现有 ``columns_detected`` 单题合同的不可变表示。"""

    name_zh: str
    role: str
    column_indexes: tuple[int, ...]
    source_question_id: str | None = None
    delimiter: str | None = None
    options: tuple[str, ...] = ()
    rows: tuple[str, ...] = ()
    scale_min: int | None = None
    scale_max: int | None = None

    def to_session_value(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name_zh": self.name_zh,
            "role": self.role,
            "column_indexes": list(self.column_indexes),
        }
        if self.source_question_id is not None:
            value["source_question_id"] = self.source_question_id
        if self.delimiter is not None:
            value["delimiter"] = self.delimiter
        if self.options:
            value["options"] = list(self.options)
            value["options_original"] = list(self.options)
        if self.rows:
            value["rows"] = list(self.rows)
        if self.scale_min is not None:
            value["scale_min"] = self.scale_min
        if self.scale_max is not None:
            value["scale_max"] = self.scale_max
        return value


@dataclass(frozen=True, slots=True)
class SnapshotResponseBinding:
    """不含原始表头、owner 或路径的安全回答列绑定记录。"""

    question_id: str
    column_indexes: tuple[int, ...]
    mapping_method: str
    mapping_status: str
    confidence: float
    warning_codes: tuple[str, ...] = ()

    def to_session_value(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "column_indexes": list(self.column_indexes),
            "mapping_method": self.mapping_method,
            "mapping_status": self.mapping_status,
            "confidence": self.confidence,
            "warning_codes": list(self.warning_codes),
        }


@dataclass(frozen=True, slots=True)
class SnapshotProvenance:
    """可写入 session/history 的最小安全快照来源。"""

    snapshot_id: str
    package_sha256: str
    definition_sha256: str
    provider: str
    source_mode: str
    mapping_status: str
    question_count: int
    asset_count: int
    asset_reference_count: int
    schema_version: int = 1

    def to_session_value(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "package_sha256": self.package_sha256,
            "definition_sha256": self.definition_sha256,
            "provider": self.provider,
            "source_mode": self.source_mode,
            "mapping_status": self.mapping_status,
            "question_count": self.question_count,
            "asset_count": self.asset_count,
            "asset_reference_count": self.asset_reference_count,
        }


@dataclass(frozen=True, slots=True)
class SnapshotSurveyBinding:
    """可安全传入 survey service 的不可变快照回答绑定。"""

    rows: tuple[tuple[str, ...], ...]
    columns_detected: tuple[SnapshotDetectedColumn, ...]
    questionnaire_text: str
    matched_questions: int
    package_sha256: str
    provenance: SnapshotProvenance
    response_bindings: tuple[SnapshotResponseBinding, ...]
    provider: str
    source_type: str
    questionnaire_filename: str | None = None

    @property
    def snapshot_ref(self) -> SnapshotProvenance:
        return self.provenance

    def session_rows(self) -> list[list[str]]:
        return [list(row) for row in self.rows]

    def session_columns(self) -> list[dict[str, Any]]:
        return [column.to_session_value() for column in self.columns_detected]

    def session_snapshot_ref(self) -> dict[str, Any]:
        return self.provenance.to_session_value()

    def session_response_bindings(self) -> list[dict[str, Any]]:
        return [binding.to_session_value() for binding in self.response_bindings]


def _question_options(question: CanonicalQuestion) -> tuple[str, ...]:
    options: list[str] = []
    seen: set[str] = set()
    for option in question.options:
        value = str(option.label or option.value).strip()
        key = _norm(value)
        if not key or key in seen:
            raise ValueError(f"题目「{question.title}」包含空或重复选项")
        seen.add(key)
        options.append(value)
    return tuple(options)


def _question_rows(question: CanonicalQuestion) -> tuple[str, ...]:
    rows: list[str] = []
    seen_keys: set[str] = set()
    seen_labels: set[str] = set()
    for row in question.rows:
        key = str(row.row_key).strip()
        label = str(row.label).strip()
        if (
            not key
            or not label
            or key in seen_keys
            or _norm(label) in seen_labels
        ):
            raise ValueError(f"矩阵题「{question.title}」包含空或重复矩阵行")
        seen_keys.add(key)
        seen_labels.add(_norm(label))
        rows.append(label)
    return tuple(rows)


def _role_for_question(question: CanonicalQuestion) -> str:
    role = {
        CanonicalQuestionType.SINGLE_CHOICE: "single_choice",
        CanonicalQuestionType.DROPDOWN: "single_choice",
        CanonicalQuestionType.MULTI_CHOICE: "multi_choice",
        CanonicalQuestionType.OPEN_TEXT: "open_text",
        CanonicalQuestionType.SCALE: "scale",
        CanonicalQuestionType.RATING: "scale",
        CanonicalQuestionType.MATRIX_SINGLE: "matrix_single",
        CanonicalQuestionType.MATRIX_MULTI: "matrix_multi",
        CanonicalQuestionType.MATRIX_SCALE: "matrix_scale",
        CanonicalQuestionType.DATE: "ignore",
        CanonicalQuestionType.TIME: "ignore",
        CanonicalQuestionType.FILE_UPLOAD: "ignore",
    }.get(question.canonical_type)
    if role is None:
        raise ValueError(
            f"题目「{question.title}」的类型 {question.canonical_type.value} "
            "尚不能安全用于分析"
        )
    return role


def _substantive_questions(package: SnapshotPackage) -> list[CanonicalQuestion]:
    questions = [
        question
        for question in package.bundle.snapshot.canonical_questions
        if question.canonical_type not in _NON_RESPONSE_TYPES
    ]
    if not questions:
        raise ValueError("该快照没有可绑定的问卷结构，请先完成结构复核")
    ids: set[str] = set()
    titles: dict[str, CanonicalQuestion] = {}
    for question in questions:
        question_id = str(question.question_id).strip()
        title = str(question.title).strip()
        if not question_id or question_id in ids:
            raise ValueError("快照包含空或重复的 question_id")
        if not title:
            raise ValueError(f"题目 {question_id} 缺少题干")
        title_key = _norm(title)
        if title_key in titles:
            previous = titles[title_key]
            google_exact_duplicate = (
                package.bundle.snapshot.provider == Provider.GOOGLE_FORMS
                and previous.title.strip() == title
                and previous.canonical_type not in _MATRIX_TYPES
                and question.canonical_type not in _MATRIX_TYPES
            )
            if not google_exact_duplicate:
                raise ValueError(
                    f"快照包含重复题干「{title}」，不能确定性绑定回答列"
                )
        ids.add(question_id)
        titles.setdefault(title_key, question)
        if question.mapping_status not in {
            MappingStatus.EXACT,
            MappingStatus.NORMALIZED,
        }:
            raise ValueError(f"题目「{title}」的结构映射尚未完成复核")
        _role_for_question(question)
        if question.canonical_type in _MATRIX_TYPES and not question.rows:
            raise ValueError(f"矩阵题「{title}」缺少矩阵行")
        _question_rows(question)
        if question.canonical_type in {
            CanonicalQuestionType.SINGLE_CHOICE,
            CanonicalQuestionType.DROPDOWN,
            CanonicalQuestionType.MULTI_CHOICE,
            CanonicalQuestionType.MATRIX_SINGLE,
            CanonicalQuestionType.MATRIX_MULTI,
        } and not _question_options(question):
            raise ValueError(f"题目「{title}」缺少选项")
    return questions


def _questionnaire_text(
    package: SnapshotPackage,
    questions: list[CanonicalQuestion],
) -> str:
    snapshot = package.bundle.snapshot
    lines: list[str] = []
    if snapshot.title.strip():
        lines.append(f"问卷：{snapshot.title.strip()}")
    for question in questions:
        required = "必答" if question.required else "选答"
        lines.append(
            f"{question.question_id} [{question.canonical_type.value};{required}] "
            f"{question.title.strip()}"
        )
        options = _question_options(question)
        rows = _question_rows(question)
        if options:
            lines.append("选项：" + " | ".join(options))
        if rows:
            lines.append("矩阵行：" + " | ".join(rows))
    return "\n".join(lines)


def _row_value(row: list[str], index: int) -> str:
    return row[index] if index < len(row) else ""


def _matching_header_indexes(
    headers: list[str],
    candidates: tuple[str, ...],
    *,
    normalized: bool,
) -> set[int]:
    candidate_keys = {
        _norm(candidate) if normalized else candidate.strip()
        for candidate in candidates
        if str(candidate or "").strip()
    }
    if not candidate_keys:
        return set()
    return {
        index
        for index, header in enumerate(headers)
        if (_norm(header) if normalized else header.strip()) in candidate_keys
    }


def _header_candidates(
    question: CanonicalQuestion,
    binding: ResponseColumnBinding,
    *,
    row_label: str | None,
    row_key: str | None,
    exported_header: str | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    stable = tuple(dict.fromkeys(
        value
        for value in (
            binding.response_key,
            binding.provider_question_id,
            row_key,
            question.question_id if row_label is None else None,
        )
        if value
    ))
    structural: list[str] = []
    title = question.title.strip()
    if row_label is None:
        structural.append(exported_header or binding.column_header or title)
    else:
        if binding.column_header:
            structural.append(binding.column_header)
        structural.extend((
            f"{title} [{row_label}]",
            f"{title}__{row_label}",
        ))
    return stable, tuple(dict.fromkeys(structural))


def _select_google_source_index(
    headers: list[str],
    question: CanonicalQuestion,
    binding: ResponseColumnBinding,
    *,
    row_label: str | None,
    row_key: str | None,
    used_indexes: set[int],
    exported_header: str | None,
) -> tuple[int, str, str, float, tuple[str, ...]]:
    stable, structural = _header_candidates(
        question,
        binding,
        row_label=row_label,
        row_key=row_key,
        exported_header=exported_header,
    )

    exact_stable = _matching_header_indexes(headers, stable, normalized=False)
    if len(exact_stable) > 1:
        raise ValueError(f"题目「{question.title}」存在多个稳定回答键列")
    if exact_stable:
        index = next(iter(exact_stable))
        method = "provider_response_key"
        status = MappingStatus.EXACT.value
        confidence = 1.0
        warnings: tuple[str, ...] = ()
    else:
        index = -1
        declared_index = binding.column_index
        all_expected = (*stable, *structural)
        if declared_index is not None and declared_index < len(headers):
            actual_header = headers[declared_index].strip()
            exact_expected = {
                value.strip() for value in all_expected if value.strip()
            }
            normalized_expected = {_norm(value) for value in exact_expected}
            if actual_header in exact_expected:
                index = declared_index
                method = "declared_column_index"
                status = MappingStatus.EXACT.value
                confidence = 1.0
                warnings = ()
            elif _norm(actual_header) in normalized_expected:
                index = declared_index
                method = "declared_column_index"
                status = MappingStatus.NORMALIZED.value
                confidence = 0.9
                warnings = ("normalized_header_fallback",)
        if index < 0:
            exact_structural = _matching_header_indexes(
                headers, structural, normalized=False
            )
            if len(exact_structural) > 1:
                raise ValueError(f"题目「{question.title}」存在多个同名回答列")
            if exact_structural:
                index = next(iter(exact_structural))
                method = "declared_header"
                status = MappingStatus.EXACT.value
                confidence = 1.0
                warnings = ()
        if index < 0:
            normalized_matches = _matching_header_indexes(
                headers, (*stable, *structural), normalized=True
            )
            if len(normalized_matches) > 1:
                raise ValueError(f"题目「{question.title}」的规范化表头不唯一")
            if normalized_matches:
                index = next(iter(normalized_matches))
                method = "normalized_header_fallback"
                status = MappingStatus.NORMALIZED.value
                confidence = 0.85
                warnings = ("normalized_header_fallback",)
        if index < 0:
            label = row_label or question.title
            raise ValueError(f"未找到题目「{label}」对应的回答列")

    if index in used_indexes:
        raise ValueError(f"回答列不能同时绑定多个题目：{question.title}")
    return index, method, status, confidence, warnings


def _ordered_google_bindings(
    question: CanonicalQuestion,
    mapping: ResponseColumnMapping,
) -> list[tuple[ResponseColumnBinding, str | None, str | None]]:
    if mapping.mapping_status not in {MappingStatus.EXACT, MappingStatus.NORMALIZED}:
        raise ValueError(f"题目「{question.title}」的回答映射尚未完成复核")
    if question.canonical_type not in _MATRIX_TYPES:
        if len(mapping.bindings) != 1:
            raise ValueError(f"题目「{question.title}」必须且只能绑定一个回答列")
        binding = mapping.bindings[0]
        if (
            question.provider_question_id
            and binding.provider_question_id != question.provider_question_id
        ):
            raise ValueError(f"题目「{question.title}」的 Provider 题号不一致")
        return [(binding, None, None)]

    by_row_key: dict[str, ResponseColumnBinding] = {}
    for binding in mapping.bindings:
        if binding.row_key is None or binding.row_key in by_row_key:
            raise ValueError(f"矩阵题「{question.title}」的行绑定不完整或重复")
        by_row_key[binding.row_key] = binding
    expected_keys = {row.row_key for row in question.rows}
    if set(by_row_key) != expected_keys:
        raise ValueError(f"矩阵题「{question.title}」缺少或多出矩阵行回答列")
    ordered: list[tuple[ResponseColumnBinding, str, str]] = []
    for row in question.rows:
        binding = by_row_key[row.row_key]
        if (
            row.provider_question_id
            and binding.provider_question_id != row.provider_question_id
        ):
            raise ValueError(f"矩阵题「{question.title}」的行 Provider 题号不一致")
        ordered.append((binding, row.label, row.row_key))
    return ordered


def _scale_bounds(question: CanonicalQuestion) -> tuple[int, int]:
    numeric: list[int] = []
    for option in _question_options(question):
        try:
            number = int(option)
        except ValueError:
            continue
        numeric.append(number)
    if numeric:
        return min(numeric), max(numeric)
    return 1, 5


def _normalize_multi_values(
    body: list[list[str]],
    source_index: int,
    options: tuple[str, ...],
    title: str,
) -> list[str]:
    values: list[str] = []
    for row_number, row in enumerate(body, start=2):
        selected = _parse_same_column_multi_value(
            _row_value(row, source_index), list(options)
        )
        if selected is None:
            raise ValueError(
                f"题目「{title}」第 {row_number} 行的多选答案无法按快照选项匹配"
            )
        values.append("\n".join(selected))
    return values


def _bind_google(
    package: SnapshotPackage,
    questions: list[CanonicalQuestion],
    response_filename: str,
    response_content: bytes,
) -> tuple[
    tuple[tuple[str, ...], ...],
    tuple[SnapshotDetectedColumn, ...],
    tuple[SnapshotResponseBinding, ...],
]:
    rows = parse_questionnaire_response_rows(response_filename, response_content)
    headers = rows[0]
    body = rows[1:]
    mappings: dict[str, ResponseColumnMapping] = {}
    question_ids = {question.question_id for question in questions}
    for mapping in package.bundle.snapshot.response_column_mappings:
        if mapping.question_id not in question_ids:
            raise ValueError("快照包含无法定位到 Canonical 题目的回答映射")
        if mapping.question_id in mappings:
            raise ValueError("快照包含重复的回答映射")
        mappings[mapping.question_id] = mapping

    normalized_headers: list[str] = []
    normalized_columns: list[list[str]] = []
    detected: list[SnapshotDetectedColumn] = []
    safe_bindings: list[SnapshotResponseBinding] = []
    used_indexes: set[int] = set()
    title_totals: dict[str, int] = {}
    for question in questions:
        if question.canonical_type not in _MATRIX_TYPES:
            title = question.title.strip()
            title_totals[title] = title_totals.get(title, 0) + 1
    title_occurrences: dict[str, int] = {}
    exported_headers: dict[str, str] = {}
    used_exported_headers: set[str] = set()
    for question in questions:
        if question.canonical_type in _MATRIX_TYPES:
            continue
        title = question.title.strip()
        if title_totals[title] == 1:
            title_key = _norm(title)
            if title_key in used_exported_headers:
                raise ValueError(
                    f"Google Forms 题目「{title}」的导出列名无法唯一确定"
                )
            used_exported_headers.add(title_key)
            continue
        occurrence = title_occurrences.get(title, 0) + 1
        title_occurrences[title] = occurrence
        exported_header = (
            title
            if occurrence == 1
            else f"{title} {occurrence}"
        )
        exported_header_key = _norm(exported_header)
        if exported_header_key in used_exported_headers:
            raise ValueError(
                f"Google Forms 题目「{title}」的导出列名无法唯一确定"
            )
        used_exported_headers.add(exported_header_key)
        exported_headers[question.question_id] = exported_header

    for question in questions:
        mapping = mappings.get(question.question_id)
        if mapping is None:
            raise ValueError(f"题目「{question.title}」缺少回答列映射")
        ordered = _ordered_google_bindings(question, mapping)
        source_indexes: list[int] = []
        methods: list[str] = []
        statuses: list[str] = []
        confidences: list[float] = []
        warning_codes: set[str] = {
            warning.code for warning in (*question.warnings, *mapping.warnings)
        }
        for binding, row_label, row_key in ordered:
            index, method, status, confidence, warnings = (
                _select_google_source_index(
                    headers,
                    question,
                    binding,
                    row_label=row_label,
                    row_key=row_key,
                    used_indexes=used_indexes,
                    exported_header=exported_headers.get(question.question_id),
                )
            )
            source_indexes.append(index)
            methods.append(method)
            statuses.append(status)
            confidences.append(min(confidence, mapping.mapping_confidence))
            warning_codes.update(warnings)
            used_indexes.add(index)

        role = _role_for_question(question)
        options = _question_options(question)
        row_labels = _question_rows(question)
        target_indexes: list[int] = []
        for position, source_index in enumerate(source_indexes):
            target_index = len(normalized_headers)
            target_indexes.append(target_index)
            if question.canonical_type in _MATRIX_TYPES:
                normalized_headers.append(
                    f"{question.title.strip()} [{row_labels[position]}]"
                )
            else:
                normalized_headers.append(
                    exported_headers.get(question.question_id)
                    or question.title.strip()
                )
            if role in {"multi_choice", "matrix_multi"}:
                normalized_columns.append(_normalize_multi_values(
                    body, source_index, options, question.title
                ))
            else:
                normalized_columns.append([
                    _row_value(row, source_index) for row in body
                ])

        scale_min: int | None = None
        scale_max: int | None = None
        if role in {"scale", "matrix_scale"}:
            scale_min, scale_max = _scale_bounds(question)
        detected.append(SnapshotDetectedColumn(
            name_zh=(
                exported_headers.get(question.question_id)
                or question.title.strip()
            ),
            role=role,
            column_indexes=tuple(target_indexes),
            source_question_id=question.question_id,
            delimiter=("\n" if role in {"multi_choice", "matrix_multi"} else None),
            options=(
                options
                if role in {
                    "single_choice", "multi_choice", "matrix_single", "matrix_multi"
                }
                else ()
            ),
            rows=row_labels,
            scale_min=scale_min,
            scale_max=scale_max,
        ))
        method = "+".join(dict.fromkeys(methods))
        status = (
            MappingStatus.NORMALIZED.value
            if MappingStatus.NORMALIZED.value in statuses
            else MappingStatus.EXACT.value
        )
        safe_bindings.append(SnapshotResponseBinding(
            question_id=question.question_id,
            column_indexes=tuple(target_indexes),
            mapping_method=method,
            mapping_status=status,
            confidence=min(confidences),
            warning_codes=tuple(sorted(warning_codes)),
        ))

    for source_index, header in enumerate(headers):
        if source_index in used_indexes:
            continue
        target_index = len(normalized_headers)
        normalized_headers.append(header)
        normalized_columns.append([
            _row_value(row, source_index) for row in body
        ])
        header_key = _norm(header).replace("_", "")
        detected.append(SnapshotDetectedColumn(
            name_zh=header,
            role=("id" if "roleid" in header_key else "ignore"),
            column_indexes=(target_index,),
        ))

    normalized_rows: list[tuple[str, ...]] = [tuple(normalized_headers)]
    for row_index in range(len(body)):
        normalized_rows.append(tuple(
            column[row_index] for column in normalized_columns
        ))
    return tuple(normalized_rows), tuple(detected), tuple(safe_bindings)


def _bested_definition(question: CanonicalQuestion) -> BestedResponseQuestionDefinition:
    role = _role_for_question(question)
    qid: int | None = None
    if question.canonical_type in _MATRIX_TYPES:
        for position, row in enumerate(question.rows, start=1):
            match = _BESTED_ROW_ID_RE.fullmatch(row.provider_question_id or "")
            if match is None or int(match.group(2)) != position:
                raise ValueError(f"矩阵题「{question.title}」缺少稳定的倍市得行题号")
            row_qid = int(match.group(1))
            if qid is not None and row_qid != qid:
                raise ValueError(f"矩阵题「{question.title}」的倍市得行题号不一致")
            qid = row_qid
    else:
        match = _BESTED_QUESTION_ID_RE.fullmatch(
            question.provider_question_id or ""
        )
        if match is not None:
            qid = int(match.group(1))
    if qid is None:
        raise ValueError(f"题目「{question.title}」缺少稳定的倍市得 Q 号")
    return BestedResponseQuestionDefinition(
        question_id=question.question_id,
        qid=qid,
        title=question.title.strip(),
        role=role,
        options=_question_options(question),
        rows=_question_rows(question),
    )


def _detected_from_bested(
    value: dict[str, Any],
    question_id_by_source: dict[str, str],
) -> SnapshotDetectedColumn:
    source_id = value.get("source_question_id")
    canonical_id = question_id_by_source.get(str(source_id))
    return SnapshotDetectedColumn(
        name_zh=str(value.get("name_zh") or ""),
        role=str(value.get("role") or "ignore"),
        column_indexes=tuple(int(index) for index in value.get("column_indexes") or []),
        source_question_id=canonical_id,
        delimiter=(str(value["delimiter"]) if value.get("delimiter") else None),
        options=tuple(str(option) for option in value.get("options") or []),
        rows=tuple(str(row) for row in value.get("rows") or []),
        scale_min=(int(value["scale_min"]) if value.get("scale_min") is not None else None),
        scale_max=(int(value["scale_max"]) if value.get("scale_max") is not None else None),
    )


def _bind_bested(
    package: SnapshotPackage,
    questions: list[CanonicalQuestion],
    response_filename: str,
    response_content: bytes,
    questionnaire_text: str,
) -> tuple[
    tuple[tuple[str, ...], ...],
    tuple[SnapshotDetectedColumn, ...],
    tuple[SnapshotResponseBinding, ...],
]:
    if not response_filename.strip().casefold().endswith(".xlsx"):
        raise ValueError("倍市得回答绑定需要包含 data/code 工作表的 .xlsx 文件")
    definitions = [_bested_definition(question) for question in questions]
    if len({definition.qid for definition in definitions}) != len(definitions):
        raise ValueError("倍市得快照包含重复 Q 号")
    matched = match_bested_response_workbook(
        response_content,
        definitions,
        questionnaire_text=questionnaire_text,
        strict_snapshot_binding=True,
    )
    if matched["matched_questions"] != len(definitions):
        raise ValueError("倍市得回答文件缺少快照中的题目")
    matched_binding_ids = [
        str(value.get("question_id") or "")
        for value in matched.get("bindings") or []
    ]
    expected_binding_ids = {definition.question_id for definition in definitions}
    if (
        len(matched_binding_ids) != len(expected_binding_ids)
        or len(matched_binding_ids) != len(set(matched_binding_ids))
        or set(matched_binding_ids) != expected_binding_ids
    ):
        raise ValueError("倍市得回答绑定未完整且唯一覆盖快照题目")
    question_id_by_source = {
        f"Q{definition.qid}": definition.question_id
        for definition in definitions
    }
    columns = tuple(
        _detected_from_bested(value, question_id_by_source)
        for value in matched["questions"]
    )
    bindings = tuple(SnapshotResponseBinding(
        question_id=str(value["question_id"]),
        column_indexes=tuple(value["column_indexes"]),
        mapping_method=str(value["mapping_method"]),
        mapping_status=str(value["mapping_status"]),
        confidence=float(value["confidence"]),
        warning_codes=tuple(str(code) for code in value["warning_codes"]),
    ) for value in matched["bindings"])
    return (
        tuple(tuple(str(cell) for cell in row) for row in matched["rows"]),
        columns,
        bindings,
    )


def bind_snapshot_to_survey_responses(
    package: SnapshotPackage,
    *,
    owner_ref: str,
    response_filename: str,
    response_content: bytes,
) -> SnapshotSurveyBinding:
    """完整复核快照并将回答文件绑定为现有标准问卷分析输入。"""
    if not isinstance(package, SnapshotPackage):
        raise SnapshotPackageError("package 类型无效")
    if not isinstance(owner_ref, str) or not owner_ref.strip():
        raise ValueError("owner_ref 不能为空")
    if not isinstance(response_filename, str) or not response_filename.strip():
        raise ValueError("回答文件名不能为空")
    if not isinstance(response_content, bytes) or not response_content:
        raise ValueError("回答文件内容为空")

    package_bytes = build_snapshot_package(
        owner_ref.strip(), package.bundle, package.media
    )
    package_sha256 = content_sha256(package_bytes)
    snapshot = package.bundle.snapshot
    questions = _substantive_questions(package)
    questionnaire_text = _questionnaire_text(package, questions)

    if snapshot.provider == Provider.GOOGLE_FORMS:
        rows, columns, bindings = _bind_google(
            package,
            questions,
            response_filename,
            response_content,
        )
        source_type = "google"
    elif snapshot.provider == Provider.BESTED:
        rows, columns, bindings = _bind_bested(
            package,
            questions,
            response_filename,
            response_content,
            questionnaire_text,
        )
        source_type = "bested"
    else:
        raise ValueError(
            f"{snapshot.provider.value} 快照尚不能绑定标准问卷回答"
        )

    provenance = SnapshotProvenance(
        snapshot_id=snapshot.snapshot_id,
        package_sha256=package_sha256,
        definition_sha256=snapshot.content_hash,
        provider=snapshot.provider.value,
        source_mode=snapshot.source_mode.value,
        mapping_status=snapshot.mapping_status.value,
        question_count=snapshot.question_count,
        asset_count=snapshot.asset_count,
        asset_reference_count=snapshot.asset_reference_count,
    )
    return SnapshotSurveyBinding(
        rows=rows,
        columns_detected=columns,
        questionnaire_text=questionnaire_text,
        matched_questions=len(questions),
        package_sha256=package_sha256,
        provenance=provenance,
        response_bindings=bindings,
        provider=snapshot.provider.value,
        source_type=source_type,
    )
