"""定性问卷来源适配：解析倍市得原问卷并与可读回答数据确定性对齐。"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Sequence

import openpyxl

from app.integrations.bested_questionnaire_client import (
    BestedQuestionnaireHyperlink,
    BestedQuestionnaireImage,
    BestedQuestionnaireParseResult,
    BestedQuestionnaireQuestion,
    _XLSX_MAX_CELLS_PER_SHEET,
    _XLSX_MAX_COLUMNS_PER_SHEET,
    _XLSX_MAX_ROWS_PER_SHEET,
    _validate_xlsx_package,
    parse_bested_questionnaire,
)


_CODE_QUESTION_RE = re.compile(r"^Q(\d+)\.(.*)$", re.DOTALL)
_CONTACT_RE = re.compile(r"whatsapp|手机号|手机号码|联系电话|联系方式", re.IGNORECASE)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


@dataclass(frozen=True, slots=True)
class BestedResponseQuestionDefinition:
    """匹配倍市得回答导出所需的最小、不可变题目定义。"""

    question_id: str
    qid: int
    title: str
    role: str
    options: tuple[str, ...] = ()
    rows: tuple[str, ...] = ()


def _cell_text(value) -> str:
    return "" if value is None else str(value).strip()


def _load_workbook(content: bytes):
    _validate_xlsx_package(content)
    try:
        return openpyxl.load_workbook(
            io.BytesIO(content),
            read_only=True,
            data_only=True,
        )
    except Exception as exc:
        raise ValueError("无法读取 Excel 内容，请确认文件来自倍市得且未损坏") from exc


def _worksheet_rows(ws) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in ws.iter_rows(values_only=True):
        rows.append([_cell_text(value) for value in row])
    while rows and not any(cell for cell in rows[-1]):
        rows.pop()
    return rows


def _parse_bested_questionnaire(
    content: bytes,
) -> tuple[list[BestedQuestionnaireQuestion], str]:
    parsed = parse_bested_questionnaire(content, discover_media=False)
    return list(parsed.questions), parsed.questionnaire_text


def _parse_response_workbook(content: bytes) -> tuple[list[list[str]], list[tuple[int, str]]]:
    workbook = _load_workbook(content)
    try:
        if "data" not in workbook.sheetnames or "code" not in workbook.sheetnames:
            raise ValueError("回答文件必须同时包含 data 和 code 工作表")
        data_rows = _worksheet_rows(workbook["data"])
        code_rows = _worksheet_rows(workbook["code"])
    finally:
        workbook.close()
    if len(data_rows) <= 1:
        raise ValueError("data 工作表为空或只有表头")

    code_questions: list[tuple[int, str]] = []
    for row in code_rows:
        value = row[1] if len(row) > 1 else ""
        match = _CODE_QUESTION_RE.match(value)
        if match:
            code_questions.append((int(match.group(1)), match.group(2).strip()))
    if not code_questions:
        raise ValueError("code 工作表中未识别到 Q号.题干")
    return data_rows, code_questions


def parse_questionnaire_response_rows(
    filename: str,
    content: bytes,
) -> list[list[str]]:
    """安全解析快照绑定入口的 CSV/XLSX 回答表。"""
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("回答文件名不能为空")
    if not isinstance(content, bytes) or not content:
        raise ValueError("回答文件内容为空")
    normalized_name = filename.strip().casefold()
    if normalized_name.endswith(".csv"):
        rows = _parse_bounded_response_csv(content)
    elif normalized_name.endswith(".xlsx"):
        workbook = _load_workbook(content)
        try:
            rows = _worksheet_rows(workbook.active)
        finally:
            workbook.close()
    else:
        raise ValueError("回答文件仅支持 .csv 或 .xlsx")
    if len(rows) <= 1:
        raise ValueError("回答文件为空或只有表头")
    if not any(header for header in rows[0]):
        raise ValueError("回答文件缺少有效表头")
    return rows


def _parse_bounded_response_csv(content: bytes) -> list[list[str]]:
    text: str | None = None
    for encoding in ("utf-8-sig", "utf-8", "gbk", "gb2312"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("无法解析 CSV 文件，请确认文件编码为 UTF-8 或 GBK")

    rows: list[list[str]] = []
    total_cells = 0
    try:
        for row_number, row in enumerate(csv.reader(io.StringIO(text)), start=1):
            if row_number > _XLSX_MAX_ROWS_PER_SHEET:
                raise ValueError("CSV 回答行数超过安全上限")
            if len(row) > _XLSX_MAX_COLUMNS_PER_SHEET:
                raise ValueError("CSV 回答列数超过安全上限")
            total_cells += len(row)
            if total_cells > _XLSX_MAX_CELLS_PER_SHEET:
                raise ValueError("CSV 回答单元格数量超过安全上限")
            rows.append([_cell_text(cell) for cell in row])
    except csv.Error as exc:
        raise ValueError("无法解析 CSV 文件，请确认文件格式正确") from exc
    while rows and not any(rows[-1]):
        rows.pop()
    return rows


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _find_exact_header(
    headers: list[str],
    title: str,
    used: set[int],
    cursor: int,
    *,
    strict_unique_headers: bool,
) -> int | None:
    wanted = _norm(title)
    if not strict_unique_headers:
        for index in range(cursor, len(headers)):
            if index not in used and _norm(headers[index]) == wanted:
                return index
    matches = [
        index for index, header in enumerate(headers)
        if index not in used and _norm(header) == wanted
    ]
    if strict_unique_headers and len(matches) > 1:
        raise ValueError(f"题目「{title}」存在多个规范化同名回答列")
    return matches[0] if len(matches) == 1 else None


def _group_headers(
    headers: list[str],
    title: str,
    expected_labels: list[str],
    used: set[int],
) -> list[int]:
    prefix = f"{title}__"
    candidates = [
        index for index, header in enumerate(headers)
        if index not in used and header.startswith(prefix)
    ]
    suffix_to_index = {
        _norm(headers[index][len(prefix):]): index
        for index in candidates
    }
    indexes: list[int] = []
    missing: list[str] = []
    for label in expected_labels:
        index = suffix_to_index.get(_norm(label))
        if index is None:
            missing.append(label)
        else:
            indexes.append(index)
    if missing or len(indexes) != len(candidates):
        detail = "、".join(missing[:3]) if missing else "存在原问卷之外的拆分列"
        raise ValueError(f"题目「{title}」与回答列无法完整匹配：{detail}")
    return indexes


def _row_value(row: list[str], index: int) -> str:
    return row[index] if index < len(row) else ""


def _parse_same_column_multi_value(
    value: str,
    options: list[str],
) -> list[str] | None:
    """按完整选项文本解析倍市得同列多选，避免误切选项内的逗号。"""
    text = str(value or "").strip()
    if not text:
        return []

    ordered_options = sorted(options, key=len, reverse=True)
    memo: dict[int, list[str] | None] = {}

    def walk(position: int) -> list[str] | None:
        if position == len(text):
            return []
        if position in memo:
            return memo[position]

        for option in ordered_options:
            if not text.startswith(option, position):
                continue
            end = position + len(option)
            if end == len(text):
                memo[position] = [option]
                return memo[position]
            if text[end] != ",":
                continue
            next_position = end + 1
            while next_position < len(text) and text[next_position].isspace():
                next_position += 1
            remaining = walk(next_position)
            if remaining is not None:
                memo[position] = [option, *remaining]
                return memo[position]

        memo[position] = None
        return None

    return walk(0)


def _translation_sources(questions: list[dict]) -> list[dict]:
    sources: list[dict] = []
    for question in questions:
        question_id = str(question.get("source_question_id") or "").strip()
        if not question_id:
            continue
        sources.append({
            "question_id": question_id,
            "name": str(question.get("name_zh") or "").strip(),
            "options": [
                str(value).strip() for value in question.get("options") or []
            ],
            "rows": [
                str(value).strip() for value in question.get("rows") or []
            ],
        })
    return sources


def build_questionnaire_translation_query(questions: list[dict]) -> str:
    """构造只包含可翻译文本的任务，不向模型开放题型与列结构。"""
    payload = {"questions": _translation_sources(questions)}
    return (
        "请逐题翻译以下 JSON 数据。question_id、数组长度和数组顺序必须原样保持。\n"
        "只输出一个 JSON 对象，不要解释，不要 Markdown。\n"
        "输出结构："
        '{"translations":[{"question_id":"Q1","name_zh":"中文题干",'
        '"options_zh":["中文选项1"],"rows_zh":["中文矩阵行1"]}]}\n'
        "待翻译数据：\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _translation_json(answer: str) -> dict:
    text = str(answer or "").strip()
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"翻译结果不是有效 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("翻译结果顶层必须是 JSON 对象")
    return parsed


def _translated_list(
    item: dict,
    key: str,
    source_values: list[str],
    question_id: str,
) -> list[str]:
    values = item.get(key)
    if not isinstance(values, list) or len(values) != len(source_values):
        raise ValueError(
            f"{question_id} 的 {key} 数量与原问卷不一致"
        )
    translated = [str(value or "").strip() for value in values]
    if any(not value for value in translated):
        raise ValueError(f"{question_id} 的 {key} 存在空翻译")
    normalized = [_norm(value) for value in translated]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{question_id} 的 {key} 出现重复翻译")
    return translated


def parse_questionnaire_translations(
    answer: str,
    questions: list[dict],
) -> dict[str, dict]:
    """严格校验翻译覆盖率与数组长度，避免翻译结果改变问卷结构。"""
    sources = _translation_sources(questions)
    expected = {source["question_id"]: source for source in sources}
    parsed = _translation_json(answer)
    items = parsed.get("translations")
    if not isinstance(items, list):
        raise ValueError("翻译结果缺少 translations 数组")

    translations: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("translations 中存在非对象元素")
        question_id = str(item.get("question_id") or "").strip()
        if question_id not in expected:
            raise ValueError(f"翻译结果包含未知题号：{question_id or '空'}")
        if question_id in translations:
            raise ValueError(f"翻译结果中 {question_id} 重复")
        source = expected[question_id]
        name_zh = str(item.get("name_zh") or "").strip()
        if not name_zh:
            raise ValueError(f"{question_id} 缺少中文题干")
        if _LATIN_RE.search(source["name"]) and not _CHINESE_RE.search(name_zh):
            raise ValueError(f"{question_id} 的题干未翻译为中文")
        translations[question_id] = {
            "name_zh": name_zh,
            "options_zh": _translated_list(
                item, "options_zh", source["options"], question_id,
            ),
            "rows_zh": _translated_list(
                item, "rows_zh", source["rows"], question_id,
            ),
        }

    missing = [question_id for question_id in expected if question_id not in translations]
    if missing:
        raise ValueError(f"翻译结果缺少题目：{'、'.join(missing[:5])}")
    return translations


def apply_questionnaire_translations(
    questions: list[dict],
    translations: dict[str, dict],
) -> list[dict]:
    """只替换展示文本，并为中文选项保留到原始回答值的确定性映射。"""
    translated_questions: list[dict] = []
    for question in questions:
        updated = dict(question)
        question_id = str(question.get("source_question_id") or "").strip()
        translated = translations.get(question_id)
        if not translated:
            translated_questions.append(updated)
            continue

        original_name = str(question.get("name_zh") or "").strip()
        updated["name_original"] = original_name
        updated["name_zh"] = translated["name_zh"]

        original_options = [
            str(value).strip() for value in question.get("options") or []
        ]
        if original_options:
            options_zh = list(translated["options_zh"])
            updated["options"] = options_zh
            updated["options_original"] = original_options
            aliases: dict[str, list[str]] = {}
            for canonical, original in zip(options_zh, original_options):
                if _norm(canonical) != _norm(original):
                    aliases[canonical] = [original]
            if aliases:
                updated["value_aliases"] = aliases
            else:
                updated.pop("value_aliases", None)

        original_rows = [
            str(value).strip() for value in question.get("rows") or []
        ]
        if original_rows:
            updated["rows_original"] = original_rows
            updated["rows"] = list(translated["rows_zh"])
        translated_questions.append(updated)
    return translated_questions


def match_bested_response_workbook(
    response_content: bytes,
    questions: Sequence[BestedResponseQuestionDefinition],
    *,
    questionnaire_text: str,
    strict_snapshot_binding: bool = False,
) -> dict:
    """匹配倍市得回答导出；严格全集模式仅供新快照入口使用。"""
    source_questions = list(questions)
    if not source_questions:
        raise ValueError("问卷中没有可匹配的题目")
    if any(
        not isinstance(question, BestedResponseQuestionDefinition)
        for question in source_questions
    ):
        raise TypeError("questions 必须是 BestedResponseQuestionDefinition 序列")
    qids = [question.qid for question in source_questions]
    if any(qid < 1 for qid in qids) or len(qids) != len(set(qids)):
        raise ValueError("问卷题号必须是互不重复的正整数")
    if any(not question.question_id.strip() for question in source_questions):
        raise ValueError("问卷 question_id 不能为空")
    if len({question.question_id for question in source_questions}) != len(
        source_questions
    ):
        raise ValueError("问卷 question_id 不能重复")

    data_rows, code_questions = _parse_response_workbook(response_content)
    code_qids = [qid for qid, _title in code_questions]
    if strict_snapshot_binding:
        if len(code_qids) != len(set(code_qids)):
            raise ValueError("code 工作表中的 Q 号不能重复")
        expected_qids = set(qids)
        actual_qids = set(code_qids)
        if actual_qids != expected_qids:
            missing = sorted(expected_qids - actual_qids)
            extra = sorted(actual_qids - expected_qids)
            details: list[str] = []
            if missing:
                details.append("缺少 " + "、".join(f"Q{qid}" for qid in missing))
            if extra:
                details.append("多出 " + "、".join(f"Q{qid}" for qid in extra))
            raise ValueError("code 工作表题号与问卷不一致：" + "；".join(details))
    source_by_qid = {question.qid: question for question in source_questions}
    headers = data_rows[0]
    body = data_rows[1:]

    normalized_headers: list[str] = []
    normalized_columns: list[list[str]] = []
    detected_questions: list[dict] = []
    response_bindings: list[dict] = []
    used_indexes: set[int] = set()
    cursor = 0

    for qid, code_title in code_questions:
        question = source_by_qid.get(qid)
        if question is None:
            raise ValueError(f"回答文件中的 Q{qid} 在原问卷中不存在")
        if _norm(question.title) != _norm(code_title):
            raise ValueError(f"Q{qid} 的题干在原问卷与回答文件中不一致")

        role = question.role
        if _CONTACT_RE.search(question.title):
            role = "ignore"

        if question.role == "multi_choice":
            split_prefix = f"{code_title}__"
            split_candidates = [
                index for index, header in enumerate(headers)
                if index not in used_indexes and header.startswith(split_prefix)
            ]
            same_column_index = _find_exact_header(
                headers, code_title, used_indexes, cursor,
                strict_unique_headers=strict_snapshot_binding,
            )
            if split_candidates and same_column_index is not None:
                raise ValueError(
                    f"题目「{code_title}」同时存在同列与拆列多选回答"
                )

            combined: list[str] = []
            if split_candidates:
                source_indexes = _group_headers(
                    headers, code_title, question.options, used_indexes,
                )
                for row in body:
                    selected: list[str] = []
                    for source_index, option in zip(
                        source_indexes, question.options,
                    ):
                        value = _row_value(row, source_index).strip()
                        if value:
                            selected.append(value if value != option else option)
                    combined.append("\n".join(selected))
            elif same_column_index is not None:
                source_indexes = [same_column_index]
                for row_number, row in enumerate(body, start=2):
                    value = _row_value(row, same_column_index)
                    selected = _parse_same_column_multi_value(
                        value, question.options,
                    )
                    if selected is None:
                        raise ValueError(
                            f"题目「{code_title}」第 {row_number} 行的同列多选答案"
                            "无法按原问卷选项匹配"
                        )
                    combined.append("\n".join(selected))
            else:
                _group_headers(
                    headers, code_title, question.options, used_indexes,
                )
                raise AssertionError("多选题列匹配未返回结果")

            target_index = len(normalized_headers)
            normalized_headers.append(question.title)
            normalized_columns.append(combined)
            detected_questions.append({
                "name_zh": question.title,
                "role": role,
                "column_indexes": [target_index],
                "delimiter": "\n",
                "options": list(question.options),
                "options_original": list(question.options),
                "source_question_id": f"Q{qid}",
            })
            response_bindings.append({
                "question_id": question.question_id,
                "column_indexes": [target_index],
                "source_column_indexes": list(source_indexes),
                "mapping_method": "bested_code_and_header",
                "mapping_status": "normalized",
                "confidence": 0.95,
                "warning_codes": [],
            })
            used_indexes.update(source_indexes)
            cursor = max(cursor, max(source_indexes) + 1)
            continue

        if question.role in {"matrix_single", "matrix_multi", "matrix_scale"}:
            source_indexes = _group_headers(
                headers, code_title, question.rows, used_indexes,
            )
            target_indexes: list[int] = []
            for source_index, row_label in zip(source_indexes, question.rows):
                target_index = len(normalized_headers)
                target_indexes.append(target_index)
                normalized_headers.append(f"{question.title} [{row_label}]")
                if question.role == "matrix_multi":
                    values: list[str] = []
                    for row_number, row in enumerate(body, start=2):
                        selected = _parse_same_column_multi_value(
                            _row_value(row, source_index),
                            list(question.options),
                        )
                        if selected is None:
                            raise ValueError(
                                f"题目「{code_title}」矩阵行「{row_label}」"
                                f"第 {row_number} 行的多选答案无法按原问卷选项匹配"
                            )
                        values.append("\n".join(selected))
                    normalized_columns.append(values)
                else:
                    normalized_columns.append([
                        _row_value(row, source_index) for row in body
                    ])
            detected = {
                "name_zh": question.title,
                "role": role,
                "column_indexes": target_indexes,
                "rows": list(question.rows),
                "source_question_id": f"Q{qid}",
            }
            if role in {"matrix_single", "matrix_multi"}:
                detected["options"] = list(question.options)
                detected["options_original"] = list(question.options)
            if role == "matrix_multi":
                detected["delimiter"] = "\n"
            if role == "matrix_scale":
                detected["scale_min"] = 1
                detected["scale_max"] = max(5, len(question.options))
            detected_questions.append(detected)
            response_bindings.append({
                "question_id": question.question_id,
                "column_indexes": list(target_indexes),
                "source_column_indexes": list(source_indexes),
                "mapping_method": "bested_code_and_matrix_headers",
                "mapping_status": "normalized",
                "confidence": 0.95,
                "warning_codes": [],
            })
            used_indexes.update(source_indexes)
            cursor = max(cursor, max(source_indexes) + 1)
            continue

        source_index = _find_exact_header(
            headers,
            code_title,
            used_indexes,
            cursor,
            strict_unique_headers=strict_snapshot_binding,
        )
        if source_index is None:
            raise ValueError(f"未找到 Q{qid}「{code_title}」对应的回答列")
        target_index = len(normalized_headers)
        normalized_headers.append(question.title)
        normalized_columns.append([
            _row_value(row, source_index) for row in body
        ])
        detected = {
            "name_zh": question.title,
            "role": role,
            "column_indexes": [target_index],
            "source_question_id": f"Q{qid}",
        }
        if role == "single_choice":
            detected["options"] = list(question.options)
            detected["options_original"] = list(question.options)
        detected_questions.append(detected)
        response_bindings.append({
            "question_id": question.question_id,
            "column_indexes": [target_index],
            "source_column_indexes": [source_index],
            "mapping_method": "bested_code_and_header",
            "mapping_status": "normalized",
            "confidence": 0.95,
            "warning_codes": [],
        })
        used_indexes.add(source_index)
        cursor = source_index + 1

    for source_index, header in enumerate(headers):
        if source_index in used_indexes:
            continue
        low = header.casefold()
        if "role_id" in low or "roleid" in low:
            role = "id"
        else:
            role = "ignore"
        target_index = len(normalized_headers)
        normalized_headers.append(header)
        normalized_columns.append([
            _row_value(row, source_index) for row in body
        ])
        detected_questions.append({
            "name_zh": header,
            "role": role,
            "column_indexes": [target_index],
        })

    normalized_rows = [normalized_headers]
    for row_index in range(len(body)):
        normalized_rows.append([
            column[row_index] for column in normalized_columns
        ])

    return {
        "rows": normalized_rows,
        "questions": detected_questions,
        "questionnaire_text": questionnaire_text,
        "matched_questions": len(code_questions),
        "bindings": response_bindings,
    }


def parse_bested_qualitative_upload(
    response_content: bytes,
    questionnaire_content: bytes,
) -> dict:
    """返回标准化 rows 与确定性题型，保持旧上传路径的返回协议。"""
    source_questions, questionnaire_text = _parse_bested_questionnaire(
        questionnaire_content
    )
    definitions = [
        BestedResponseQuestionDefinition(
            question_id=f"Q{question.qid}",
            qid=question.qid,
            title=question.title,
            role=question.role,
            options=tuple(question.options),
            rows=tuple(question.rows),
        )
        for question in source_questions
    ]
    matched = match_bested_response_workbook(
        response_content,
        definitions,
        questionnaire_text=questionnaire_text,
    )
    return {
        "rows": matched["rows"],
        "questions": matched["questions"],
        "questionnaire_text": matched["questionnaire_text"],
        "matched_questions": matched["matched_questions"],
    }
