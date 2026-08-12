"""Google Forms 与倍市得问卷到统一快照/素材聚合的确定性映射。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePath
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.core.research_assets import (
    content_sha256,
    provider_definition_sha256,
    sanitize_provider_payload,
    structured_sha256,
    validate_research_contract,
)
from app.integrations.bested_questionnaire_client import (
    BestedQuestionnaireMediaIssue,
    BestedQuestionnaireParseResult,
    BestedQuestionnaireQuestion,
    worksheet_image_use_count,
)
from app.integrations.google_forms_client import (
    GoogleFormCapture,
    GoogleFormImageCapture,
    GoogleFormImageFailure,
    GoogleFormsErrorCode,
    GoogleFormsStage,
    GoogleImageContext,
)
from app.schemas.questionnaire import (
    BranchAction,
    BranchRule,
    CanonicalOption,
    CanonicalQuestion,
    CanonicalQuestionType,
    CanonicalRow,
    CollectionState,
    MappingStatus,
    ProviderItemDefinition,
    QuestionnaireSnapshot,
    QuestionnaireSourceMode,
    ResponseColumnBinding,
    ResponseColumnMapping,
)
from app.schemas.research_assets import (
    AccessStatus,
    AssetContextType,
    AssetReference,
    AssetRole,
    BindingStatus,
    DocumentType,
    ExportPolicy,
    ImportWarning,
    MediaType,
    ProcessingStatus,
    Provider,
    ResearchAsset,
    ResearchAssetCollection,
    ResearchDocument,
    ResearchSource,
    SensitivityStatus,
    SnapshotPolicy,
    SourceKind,
    SourceLocator,
)
from app.storage.research_assets import ResearchAssetBundle


_DRIVE_FILE_RE = re.compile(r"/file/d/([A-Za-z0-9_-]+)")
_EXCEL_CELL_RE = re.compile(r"^([A-Z]{1,3})([1-9]\d*)$")
_GOOGLE_ITEM_KEYS = (
    "questionItem",
    "questionGroupItem",
    "pageBreakItem",
    "textItem",
    "imageItem",
    "videoItem",
)
_BESTED_TYPE_MAP = {
    "single_choice": CanonicalQuestionType.SINGLE_CHOICE,
    "multi_choice": CanonicalQuestionType.MULTI_CHOICE,
    "matrix_single": CanonicalQuestionType.MATRIX_SINGLE,
    "matrix_multi": CanonicalQuestionType.MATRIX_MULTI,
    "matrix_scale": CanonicalQuestionType.MATRIX_SCALE,
    "scale": CanonicalQuestionType.SCALE,
    "open_text": CanonicalQuestionType.OPEN_TEXT,
}
_GOOGLE_IMAGE_FAILURE_MESSAGES = {
    GoogleFormsErrorCode.IMAGE_URL_REJECTED:
        "图片临时地址未通过安全下载校验，已跳过该素材",
    GoogleFormsErrorCode.IMAGE_REDIRECT_LIMIT:
        "图片临时地址重定向次数超过限制，已跳过该素材",
    GoogleFormsErrorCode.IMAGE_HTTP_ERROR:
        "图片服务返回失败状态，已保留问卷结构并跳过该素材",
    GoogleFormsErrorCode.IMAGE_INVALID_RESPONSE:
        "图片服务响应无效，已保留问卷结构并跳过该素材",
    GoogleFormsErrorCode.IMAGE_CONTENT_TYPE_REJECTED:
        "图片类型不在安全支持范围内，已跳过该素材",
    GoogleFormsErrorCode.IMAGE_TOO_LARGE:
        "图片超过安全大小限制，已跳过该素材",
    GoogleFormsErrorCode.IMAGE_SIGNATURE_MISMATCH:
        "图片内容与声明格式不一致，已跳过该素材",
    GoogleFormsErrorCode.TRANSPORT_ERROR:
        "图片下载连接失败，已保留问卷结构并跳过该素材",
}
_BESTED_MEDIA_ISSUE_MESSAGES = {
    "image_loading_failed":
        "一张 OOXML 声明的内嵌图片无法加载；问卷文字结构和其他素材已保留",
    "image_extraction_failed":
        "一张内嵌图片无法读取；问卷文字结构和其他素材已保留",
    "media_workbook_load_failed":
        "问卷文字结构已解析，但素材工作簿无法加载；内嵌素材未完整恢复",
}


@dataclass(frozen=True, slots=True)
class QuestionnaireMappingResult:
    """可原子保存的领域聚合，以及按内容哈希索引的图片字节。"""

    bundle: ResearchAssetBundle
    media: dict[str, bytes]


def _require_owner(owner_ref: str) -> str:
    if not isinstance(owner_ref, str) or not owner_ref.strip():
        raise ValueError("owner_ref 不能为空")
    return owner_ref.strip()


def _require_retrieved_at(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("retrieved_at 必须是带时区的 datetime")
    return value


def _stable_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}_{structured_sha256(list(parts))[:24]}"


def _safe_filename(filename: str) -> str:
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("filename 不能为空")
    normalized = filename.strip().replace("\\", "/")
    name = PurePath(normalized).name
    if not name or name in {".", ".."}:
        raise ValueError("filename 无效")
    return name


def _question_ids(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        question_id = value.get("questionId")
        if isinstance(question_id, str) and question_id:
            found.append(question_id)
        for child in value.values():
            found.extend(_question_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_question_ids(child))
    return list(dict.fromkeys(found))


def _google_image_paths(value: Any, path: tuple[Any, ...] = ()) -> set[tuple[Any, ...]]:
    paths: set[tuple[Any, ...]] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = (*path, key)
            if key == "image" and isinstance(child, dict):
                paths.add(child_path)
            paths.update(_google_image_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.update(_google_image_paths(child, (*path, index)))
    return paths


def _google_option_index(path: tuple[Any, ...]) -> int | None:
    indexes = [
        path[position + 1]
        for position, part in enumerate(path[:-1])
        if part == "options" and isinstance(path[position + 1], int)
    ]
    if len(indexes) > 1:
        raise ValueError("Google 图片捕获的 JSON 路径包含多个选项位置")
    return indexes[0] if indexes else None


def _value_at_json_path(
    value: Any,
    path: tuple[str | int, ...],
) -> Any:
    current = value
    for part in path:
        if isinstance(part, int):
            if not isinstance(current, list) or part >= len(current):
                raise ValueError("Google 图片证据的 JSON 路径不存在")
            current = current[part]
        else:
            if not isinstance(current, dict) or part not in current:
                raise ValueError("Google 图片证据的 JSON 路径不存在")
            current = current[part]
    return current


def _google_question_context_at_path(
    raw_item: dict[str, Any],
    relative_path: tuple[str | int, ...],
) -> tuple[str | None, tuple[str, ...]]:
    current: Any = raw_item
    inherited_question_id: str | None = None
    shared_question_ids: tuple[str, ...] = ()

    def update_context(value: Any) -> None:
        nonlocal inherited_question_id, shared_question_ids
        if isinstance(value, dict):
            own_question_id = value.get("questionId")
            if isinstance(own_question_id, str) and own_question_id:
                inherited_question_id = own_question_id
            nested_question = value.get("question")
            if isinstance(nested_question, dict):
                nested_question_id = nested_question.get("questionId")
                if isinstance(nested_question_id, str) and nested_question_id:
                    inherited_question_id = nested_question_id
            group = value.get("questionGroupItem")
            if isinstance(group, dict):
                raw_questions = group.get("questions")
                if isinstance(raw_questions, list):
                    shared_question_ids = tuple(
                        question_id
                        for question in raw_questions
                        if isinstance(question, dict)
                        and isinstance(
                            question_id := question.get("questionId"),
                            str,
                        )
                        and question_id
                    )

    for part in relative_path:
        update_context(current)
        try:
            current = current[part]
        except (KeyError, IndexError, TypeError):
            raise ValueError("Google 图片证据的 JSON 路径不存在") from None
    update_context(current)
    return (
        inherited_question_id,
        (
            (inherited_question_id,)
            if inherited_question_id is not None
            else shared_question_ids
        ),
    )


def _validate_google_image_evidence(
    json_path: tuple[str | int, ...],
    context: GoogleImageContext,
    raw_items: list[Any],
) -> None:
    if (
        not isinstance(json_path, tuple)
        or any(
            not isinstance(part, (str, int))
            or isinstance(part, bool)
            or (isinstance(part, str) and not part)
            or (isinstance(part, int) and part < 0)
            for part in json_path
        )
    ):
        raise ValueError("Google 图片证据的 JSON 路径无效")
    if not isinstance(context, GoogleImageContext):
        raise ValueError("Google 图片证据的上下文无效")
    if (
        not isinstance(context.item_position, int)
        or isinstance(context.item_position, bool)
    ):
        raise ValueError("Google 图片证据的 Item 位置无效")
    if context.item_id is not None and not isinstance(context.item_id, str):
        raise ValueError("Google 图片证据的 itemId 无效")
    if (
        context.question_id is not None
        and not isinstance(context.question_id, str)
    ):
        raise ValueError("Google 图片证据的 questionId 无效")
    if (
        not isinstance(context.question_ids, tuple)
        or any(
            not isinstance(question_id, str) or not question_id
            for question_id in context.question_ids
        )
        or len(context.question_ids) != len(set(context.question_ids))
    ):
        raise ValueError("Google 图片证据的 question_ids 无效")
    if context.option_index is not None and (
        not isinstance(context.option_index, int)
        or isinstance(context.option_index, bool)
        or context.option_index < 0
    ):
        raise ValueError("Google 图片证据的 option_index 无效")
    position = context.item_position
    if (
        position < 0
        or position >= len(raw_items)
        or len(json_path) < 3
        or json_path[0] != "items"
        or json_path[1] != position
        or json_path[-1] != "image"
    ):
        raise ValueError("Google 图片捕获的 JSON 路径与 Item 位置不一致")
    raw_item = raw_items[position]
    if not isinstance(raw_item, dict):
        raise ValueError("Google 图片捕获指向的 Item 不是对象")
    if raw_item.get("itemId") != context.item_id:
        raise ValueError("Google 图片捕获的 itemId 与原始 Item 不一致")
    relative_path = json_path[2:]
    if not isinstance(_value_at_json_path(raw_item, relative_path), dict):
        raise ValueError("Google 图片证据的 JSON 路径未指向图片对象")
    expected_question_id, expected_question_ids = (
        _google_question_context_at_path(raw_item, relative_path)
    )
    if context.question_id != expected_question_id:
        raise ValueError("Google 图片捕获的 questionId 与 JSON 路径不一致")
    if context.question_ids != expected_question_ids:
        raise ValueError("Google 图片捕获的 question_ids 与 JSON 路径不一致")
    if context.option_index != _google_option_index(json_path):
        raise ValueError("Google 图片捕获的 option_index 与 JSON 路径不一致")


def _validate_google_image_capture(
    image: GoogleFormImageCapture,
    raw_items: list[Any],
) -> None:
    _validate_google_image_evidence(
        image.json_path,
        image.context,
        raw_items,
    )


def _google_image_failure_warning(
    failure: GoogleFormImageFailure,
    *,
    form_id: str,
) -> ImportWarning:
    if not isinstance(failure.code, GoogleFormsErrorCode):
        raise ValueError("Google 图片失败记录的错误代码无效")
    if (
        not isinstance(failure.stage, GoogleFormsStage)
        or failure.stage != GoogleFormsStage.IMAGE_DOWNLOAD
    ):
        raise ValueError("Google 图片失败记录的 stage 必须是 image_download")
    message = _GOOGLE_IMAGE_FAILURE_MESSAGES.get(failure.code)
    if message is None:
        raise ValueError("Google 图片失败记录包含不可降级的错误代码")
    if not isinstance(failure.retryable, bool):
        raise ValueError("Google 图片失败记录的 retryable 必须是布尔值")
    if (
        failure.status_code is not None
        and (
            not isinstance(failure.status_code, int)
            or isinstance(failure.status_code, bool)
            or not 100 <= failure.status_code <= 599
        )
    ):
        raise ValueError("Google 图片失败记录的 HTTP 状态码无效")
    if failure.code == GoogleFormsErrorCode.IMAGE_HTTP_ERROR:
        if (
            failure.status_code is None
            or failure.status_code == 200
            or failure.status_code in {301, 302, 303, 307, 308}
        ):
            raise ValueError("Google 图片 HTTP 失败记录缺少非成功状态码")
        expected_retryable = (
            failure.status_code in {408, 409, 425, 429}
            or failure.status_code >= 500
        )
        if failure.retryable != expected_retryable:
            raise ValueError("Google 图片 HTTP 失败记录的重试语义不一致")
    elif failure.code == GoogleFormsErrorCode.TRANSPORT_ERROR:
        if failure.status_code is not None or not failure.retryable:
            raise ValueError("Google 图片传输失败记录的状态或重试语义不一致")
    elif failure.code == GoogleFormsErrorCode.IMAGE_REDIRECT_LIMIT:
        if (
            failure.status_code not in {301, 302, 303, 307, 308}
            or failure.retryable
        ):
            raise ValueError("Google 图片重定向失败记录的状态语义不一致")
    elif failure.code in {
        GoogleFormsErrorCode.IMAGE_CONTENT_TYPE_REJECTED,
        GoogleFormsErrorCode.IMAGE_SIGNATURE_MISMATCH,
    }:
        if failure.status_code != 200 or failure.retryable:
            raise ValueError("Google 图片响应失败记录的状态语义不一致")
    elif failure.code == GoogleFormsErrorCode.IMAGE_INVALID_RESPONSE:
        if (
            failure.status_code not in {200, 301, 302, 303, 307, 308}
            or failure.retryable
        ):
            raise ValueError("Google 图片无效响应记录的状态语义不一致")
    elif failure.code == GoogleFormsErrorCode.IMAGE_URL_REJECTED:
        if failure.retryable or failure.status_code is not None:
            raise ValueError("Google 图片地址失败记录的状态语义不一致")
    elif failure.code == GoogleFormsErrorCode.IMAGE_TOO_LARGE:
        if failure.retryable or failure.status_code not in {None, 200}:
            raise ValueError("Google 图片超限失败记录的状态语义不一致")
    retry_guidance = (
        "；可稍后重试该素材"
        if failure.retryable
        else "；该结果无需自动重试"
    )
    return ImportWarning(
        code=f"google_forms_{failure.code.value}",
        message=message + retry_guidance,
        blocking=False,
        source_locator=SourceLocator(
            provider=Provider.GOOGLE_FORMS,
            provider_form_id=form_id,
            provider_question_id=failure.context.question_id,
            provider_item_id=failure.context.item_id,
            question_position=failure.context.item_position,
            json_path=list(failure.json_path),
        ),
    )


def _bested_media_issue_warning(
    issue: BestedQuestionnaireMediaIssue,
    *,
    filename: str,
    sheet_name: str,
) -> ImportWarning:
    if not isinstance(issue.code, str):
        raise ValueError("倍市得素材问题的错误代码无效")
    message = _BESTED_MEDIA_ISSUE_MESSAGES.get(issue.code)
    if message is None:
        raise ValueError("倍市得素材问题包含未知错误代码")
    if issue.sheet_name is not None and (
        not isinstance(issue.sheet_name, str)
        or issue.sheet_name != sheet_name
    ):
        raise ValueError("倍市得素材问题指向其他工作表")
    if issue.source_row is not None and (
        not isinstance(issue.source_row, int)
        or isinstance(issue.source_row, bool)
        or issue.source_row < 1
    ):
        raise ValueError("倍市得素材问题的来源行无效")
    if issue.source_cell is not None:
        if not isinstance(issue.source_cell, str):
            raise ValueError("倍市得素材问题的来源单元格无效")
        cell_match = _EXCEL_CELL_RE.fullmatch(issue.source_cell)
        if cell_match is None:
            raise ValueError("倍市得素材问题的来源单元格无效")
        if (
            issue.source_row is None
            or int(cell_match.group(2)) != issue.source_row
        ):
            raise ValueError("倍市得素材问题的单元格与来源行不一致")
    elif issue.source_row is not None:
        raise ValueError("倍市得素材问题的来源行缺少单元格定位")
    if issue.code == "media_workbook_load_failed" and (
        issue.source_cell is not None or issue.source_row is not None
    ):
        raise ValueError("倍市得素材工作簿加载失败不能声明单图位置")
    return ImportWarning(
        code=f"bested_{issue.code}",
        message=message,
        blocking=False,
        source_locator=SourceLocator(
            provider=Provider.BESTED,
            file_id=filename,
            sheet_name=issue.sheet_name or sheet_name,
            cell=issue.source_cell,
        ),
    )


def _bested_question_qid_for_row(
    questions: tuple[BestedQuestionnaireQuestion, ...],
    source_row: int | None,
) -> int | None:
    if source_row is None:
        return None
    matches = [
        question.qid
        for question in questions
        if question.source_row
        <= source_row
        <= question.source_row + len(question.raw_rows) - 1
    ]
    if len(matches) > 1:
        raise ValueError("倍市得素材来源行同时落入多个题目区间")
    return matches[0] if matches else None


def _validate_bested_cell_row(
    *,
    source_cell: str | None,
    source_row: int | None,
    label: str,
) -> None:
    if source_cell is None:
        if source_row is not None:
            raise ValueError(f"{label}来源行缺少单元格定位")
        return
    if not isinstance(source_cell, str):
        raise ValueError(f"{label}来源单元格无效")
    match = _EXCEL_CELL_RE.fullmatch(source_cell)
    if match is None:
        raise ValueError(f"{label}来源单元格无效")
    if (
        not isinstance(source_row, int)
        or isinstance(source_row, bool)
        or int(match.group(2)) != source_row
    ):
        raise ValueError(f"{label}来源单元格与来源行不一致")


def _bested_coverage_end_row(
    coverage: str | None,
    *,
    source_cell: str | None,
    source_row: int | None,
) -> int | None:
    if source_cell is None:
        if coverage is not None:
            raise ValueError("倍市得图片缺少锚点却声明 coverage")
        return source_row
    if not isinstance(coverage, str):
        raise ValueError("倍市得图片 coverage 无效")
    parts = coverage.split(":")
    if len(parts) not in {1, 2} or parts[0] != source_cell:
        raise ValueError("倍市得图片 coverage 与来源单元格不一致")
    end_match = _EXCEL_CELL_RE.fullmatch(parts[-1])
    if end_match is None:
        raise ValueError("倍市得图片 coverage 无效")
    end_row = int(end_match.group(2))
    if source_row is None or end_row < source_row:
        raise ValueError("倍市得图片 coverage 范围无效")
    return end_row


def _validate_bested_media_closure(
    parsed: BestedQuestionnaireParseResult,
    content: bytes,
) -> None:
    workbook_failure_count = sum(
        issue.code == "media_workbook_load_failed"
        for issue in parsed.media_issues
    )
    if workbook_failure_count:
        if (
            workbook_failure_count != 1
            or len(parsed.media_issues) != 1
            or parsed.images
            or parsed.hyperlinks
        ):
            raise ValueError("倍市得素材工作簿加载失败记录与解析结果矛盾")
        return
    expected_image_uses = worksheet_image_use_count(content, parsed.sheet_name)
    per_image_failures = sum(
        issue.code in {"image_loading_failed", "image_extraction_failed"}
        for issue in parsed.media_issues
    )
    if len(parsed.images) + per_image_failures != expected_image_uses:
        raise ValueError("倍市得图片成功/失败集合与 OOXML 声明数量不一致")

    for image in parsed.images:
        if image.sheet_name != parsed.sheet_name:
            raise ValueError("倍市得图片指向其他工作表")
        _validate_bested_cell_row(
            source_cell=image.source_cell,
            source_row=image.source_row,
            label="倍市得图片",
        )
        start_qid = _bested_question_qid_for_row(
            parsed.questions,
            image.source_row,
        )
        end_qid = _bested_question_qid_for_row(
            parsed.questions,
            _bested_coverage_end_row(
                image.coverage,
                source_cell=image.source_cell,
                source_row=image.source_row,
            ),
        )
        expected_qid = (
            start_qid
            if start_qid is not None and start_qid == end_qid
            else None
        )
        if image.question_qid != expected_qid:
            raise ValueError("倍市得图片的题目归属与来源行不一致")
    for hyperlink in parsed.hyperlinks:
        if hyperlink.sheet_name != parsed.sheet_name:
            raise ValueError("倍市得超链接指向其他工作表")
        _validate_bested_cell_row(
            source_cell=hyperlink.source_cell,
            source_row=hyperlink.source_row,
            label="倍市得超链接",
        )
        expected_qid = _bested_question_qid_for_row(
            parsed.questions,
            hyperlink.source_row,
        )
        if hyperlink.question_qid != expected_qid:
            raise ValueError("倍市得超链接的题目归属与来源行不一致")


def _google_item_type(item: dict[str, Any]) -> str:
    if "questionGroupItem" in item:
        return "questionGroupItem.grid"
    if "questionItem" in item:
        question = item.get("questionItem", {}).get("question", {})
        if isinstance(question, dict):
            for key in (
                "choiceQuestion",
                "textQuestion",
                "scaleQuestion",
                "ratingQuestion",
                "dateQuestion",
                "timeQuestion",
                "fileUploadQuestion",
            ):
                if key in question:
                    return f"questionItem.{key}"
        return "questionItem.unknown"
    for key in _GOOGLE_ITEM_KEYS[2:]:
        if key in item:
            return key
    return "unknownItem"


def _option_key(index: int) -> str:
    return f"option-{index + 1}"


def _google_choice_type(raw_type: Any) -> CanonicalQuestionType:
    return {
        "RADIO": CanonicalQuestionType.SINGLE_CHOICE,
        "CHECKBOX": CanonicalQuestionType.MULTI_CHOICE,
        "DROP_DOWN": CanonicalQuestionType.DROPDOWN,
    }.get(raw_type, CanonicalQuestionType.UNKNOWN)


def _google_options(value: Any) -> list[CanonicalOption]:
    if not isinstance(value, list):
        return []
    options: list[CanonicalOption] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            continue
        text = raw.get("value")
        if not isinstance(text, str) or not text:
            text = f"未命名选项 {index + 1}"
        options.append(CanonicalOption(
            option_key=_option_key(index),
            value=text,
            label=text,
        ))
    return options


def _google_branch_rules(
    options: Any,
    section_by_item_id: dict[str, str],
) -> tuple[list[BranchRule], list[ImportWarning]]:
    rules: list[BranchRule] = []
    warnings: list[ImportWarning] = []
    if not isinstance(options, list):
        return rules, warnings
    for index, option in enumerate(options):
        if not isinstance(option, dict):
            continue
        option_key = _option_key(index)
        target = option.get("goToSectionId")
        if isinstance(target, str) and target:
            canonical_target = section_by_item_id.get(target)
            if canonical_target is None:
                warnings.append(ImportWarning(
                    code="missing_branch_target",
                    message="Google 选项跳转目标未出现在当前问卷定义中",
                    field_path=f"options[{index}].goToSectionId",
                    blocking=True,
                ))
                continue
            rules.append(BranchRule(
                option_key=option_key,
                action=BranchAction.GO_TO_SECTION,
                target_section_id=canonical_target,
                provider_raw_action=target,
            ))
            continue
        action = option.get("goToAction")
        canonical_action = {
            "NEXT_SECTION": BranchAction.NEXT_SECTION,
            "RESTART_FORM": BranchAction.RESTART_FORM,
            "SUBMIT_FORM": BranchAction.SUBMIT_FORM,
        }.get(action)
        if canonical_action is not None:
            rules.append(BranchRule(
                option_key=option_key,
                action=canonical_action,
                provider_raw_action=action,
            ))
    return rules, warnings


def _google_question(
    item: dict[str, Any],
    *,
    item_id: str,
    canonical_id: str,
    section_by_item_id: dict[str, str],
) -> tuple[CanonicalQuestion, bool]:
    item_type = _google_item_type(item)
    title = item.get("title") if isinstance(item.get("title"), str) else ""
    description = (
        item.get("description")
        if isinstance(item.get("description"), str)
        else ""
    )
    if item_type == "pageBreakItem":
        return CanonicalQuestion(
            question_id=canonical_id,
            provider_item_id=item_id,
            canonical_type=CanonicalQuestionType.SECTION,
            title=title,
            description=description,
            mapping_status=MappingStatus.EXACT,
            mapping_confidence=1.0,
        ), True
    if item_type in {"textItem", "imageItem", "videoItem"}:
        if item_type == "videoItem":
            caption = item.get("videoItem", {}).get("caption")
            if isinstance(caption, str) and caption:
                description = caption
        return CanonicalQuestion(
            question_id=canonical_id,
            provider_item_id=item_id,
            canonical_type=CanonicalQuestionType.STATIC_TEXT,
            title=title,
            description=description,
            mapping_status=MappingStatus.EXACT,
            mapping_confidence=1.0,
        ), True

    if item_type == "questionGroupItem.grid":
        group = item.get("questionGroupItem", {})
        questions = group.get("questions", []) if isinstance(group, dict) else []
        rows: list[CanonicalRow] = []
        required: list[bool] = []
        if isinstance(questions, list):
            for index, raw_row in enumerate(questions):
                if not isinstance(raw_row, dict):
                    continue
                question_id = raw_row.get("questionId")
                if not isinstance(question_id, str) or not question_id:
                    continue
                row_title = raw_row.get("rowQuestion", {}).get("title")
                if not isinstance(row_title, str) or not row_title:
                    row_title = f"未命名矩阵行 {index + 1}"
                rows.append(CanonicalRow(
                    row_key=f"row-{index + 1}",
                    label=row_title,
                    provider_question_id=question_id,
                ))
                required.append(bool(raw_row.get("required", False)))
        grid = group.get("grid", {}) if isinstance(group, dict) else {}
        columns = grid.get("columns", {}) if isinstance(grid, dict) else {}
        column_type = columns.get("type") if isinstance(columns, dict) else None
        canonical_type = {
            "RADIO": CanonicalQuestionType.MATRIX_SINGLE,
            "CHECKBOX": CanonicalQuestionType.MATRIX_MULTI,
        }.get(column_type, CanonicalQuestionType.UNKNOWN)
        supported = canonical_type != CanonicalQuestionType.UNKNOWN and bool(rows)
        warnings = [] if supported else [ImportWarning(
            code="unsupported_google_grid",
            message="Google 题组结构无法完整映射，需要人工确认",
            field_path="questionGroupItem",
            blocking=False,
        )]
        return CanonicalQuestion(
            question_id=canonical_id,
            provider_item_id=item_id,
            canonical_type=canonical_type,
            title=title,
            description=description,
            required=bool(required) and all(required),
            rows=rows,
            options=_google_options(
                columns.get("options") if isinstance(columns, dict) else None
            ),
            mapping_status=(MappingStatus.EXACT if supported else MappingStatus.PARTIAL),
            mapping_confidence=(1.0 if supported else 0.5),
            warnings=warnings,
        ), supported

    question_item = item.get("questionItem", {})
    question = (
        question_item.get("question", {})
        if isinstance(question_item, dict)
        else {}
    )
    if not isinstance(question, dict):
        question = {}
    provider_question_id = question.get("questionId")
    if not isinstance(provider_question_id, str) or not provider_question_id:
        provider_question_id = None
    canonical_type = CanonicalQuestionType.UNKNOWN
    options: list[CanonicalOption] = []
    branching: list[BranchRule] = []
    warnings: list[ImportWarning] = []
    supported = provider_question_id is not None
    if "choiceQuestion" in question:
        choice = question.get("choiceQuestion", {})
        if not isinstance(choice, dict):
            choice = {}
        canonical_type = _google_choice_type(choice.get("type"))
        options = _google_options(choice.get("options"))
        branching, branch_warnings = _google_branch_rules(
            choice.get("options"), section_by_item_id
        )
        warnings.extend(branch_warnings)
        supported = supported and canonical_type != CanonicalQuestionType.UNKNOWN
    elif "textQuestion" in question:
        canonical_type = CanonicalQuestionType.OPEN_TEXT
    elif "scaleQuestion" in question:
        canonical_type = CanonicalQuestionType.SCALE
    elif "ratingQuestion" in question:
        canonical_type = CanonicalQuestionType.RATING
    elif "dateQuestion" in question:
        canonical_type = CanonicalQuestionType.DATE
    elif "timeQuestion" in question:
        canonical_type = CanonicalQuestionType.TIME
    elif "fileUploadQuestion" in question:
        canonical_type = CanonicalQuestionType.FILE_UPLOAD
        warnings.append(ImportWarning(
            code="drive_response_access_required",
            message="文件上传回答仍需按当前用户的 Drive 权限单独读取",
            field_path="questionItem.question.fileUploadQuestion",
            blocking=False,
        ))
    else:
        supported = False
    if not supported:
        warnings.append(ImportWarning(
            code="unsupported_google_question",
            message="Google 题型无法完整映射，需要人工确认",
            field_path="questionItem.question",
            blocking=False,
        ))
    return CanonicalQuestion(
        question_id=canonical_id,
        provider_question_id=provider_question_id,
        provider_item_id=item_id,
        canonical_type=canonical_type,
        title=title,
        description=description,
        required=bool(question.get("required", False)),
        options=options,
        branching=branching,
        mapping_status=(MappingStatus.EXACT if supported else MappingStatus.PARTIAL),
        mapping_confidence=(1.0 if supported else 0.5),
        warnings=warnings,
    ), supported


def _google_response_mapping(
    question: CanonicalQuestion,
    *,
    form_id: str,
) -> ResponseColumnMapping | None:
    provider_ids: list[tuple[str, str | None]] = []
    if question.rows:
        provider_ids.extend(
            (row.provider_question_id, row.row_key)
            for row in question.rows
            if row.provider_question_id is not None
        )
    elif question.provider_question_id is not None:
        provider_ids.append((question.provider_question_id, None))
    if not provider_ids:
        return None
    bindings = [
        ResponseColumnBinding(
            provider_question_id=provider_id,
            row_key=row_key,
            response_key=provider_id,
            column_header=(
                next(row.label for row in question.rows if row.row_key == row_key)
                if row_key is not None
                else question.title or None
            ),
            source_locator=SourceLocator(
                provider=Provider.GOOGLE_FORMS,
                provider_form_id=form_id,
                provider_question_id=provider_id,
                provider_item_id=question.provider_item_id,
            ),
        )
        for provider_id, row_key in provider_ids
    ]
    return ResponseColumnMapping(
        question_id=question.question_id,
        bindings=bindings,
        mapping_status=MappingStatus.EXACT,
        mapping_confidence=1.0,
    )


def _image_extension(mime_type: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }.get(mime_type, ".bin")


def _youtube_id(value: str) -> str | None:
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        unsafe_authority = (
            parts.username is not None
            or parts.password is not None
            or parts.port is not None
        )
    except ValueError:
        return None
    if parts.scheme != "https" or unsafe_authority:
        return None
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        candidate = parse_qs(parts.query).get("v", [None])[0]
    elif host == "youtu.be":
        candidate = parts.path.strip("/").split("/", 1)[0]
    else:
        return None
    if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_-]{6,64}", candidate):
        return candidate
    return None


def _drive_file_id(value: str) -> str | None:
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        unsafe_authority = (
            parts.username is not None
            or parts.password is not None
            or parts.port is not None
        )
    except ValueError:
        return None
    if parts.scheme != "https" or unsafe_authority:
        return None
    if host not in {"drive.google.com", "docs.google.com"}:
        return None
    match = _DRIVE_FILE_RE.search(parts.path)
    if match:
        return match.group(1)
    query_id = parse_qs(parts.query).get("id", [None])[0]
    if isinstance(query_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", query_id):
        return query_id
    return None


def _append_external_reference(
    *,
    url: str,
    title: str,
    provider_item_id: str,
    canonical_question: CanonicalQuestion,
    form_id: str | None,
    owner_ref: str,
    retrieved_at: datetime,
    sources: list[ResearchSource],
    documents: list[ResearchDocument],
    assets: list[ResearchAsset],
    references: list[AssetReference],
    file_id: str | None = None,
    sheet_name: str | None = None,
    cell: str | None = None,
) -> str | None:
    video_id = _youtube_id(url)
    drive_id = _drive_file_id(url)
    if video_id is None and drive_id is None:
        return None
    if video_id is not None:
        provider = Provider.YOUTUBE
        identity = video_id
        document_type = DocumentType.VIDEO
        media_type = MediaType.VIDEO
        mime_type = "video/youtube"
        access_status = AccessStatus.UNKNOWN
        locator = SourceLocator(provider=provider, video_id=video_id)
    else:
        provider = Provider.GOOGLE_DRIVE
        identity = drive_id or ""
        document_type = DocumentType.EXTERNAL_RESOURCE
        media_type = MediaType.EXTERNAL_LINK
        mime_type = "application/octet-stream"
        access_status = AccessStatus.UNKNOWN
        locator = SourceLocator(provider=provider, drive_file_id=identity)
    source_id = _stable_id("src", provider.value, identity, owner_ref)
    document_id = _stable_id("doc", source_id, identity)
    asset_id = _stable_id("asset", document_id, identity)
    reference_id = _stable_id(
        "aref",
        asset_id,
        canonical_question.question_id,
        provider_item_id,
        sheet_name,
        cell,
    )
    if source_id not in {source.source_id for source in sources}:
        sources.append(ResearchSource(
            source_id=source_id,
            source_kind=SourceKind.REMOTE_URL,
            provider=provider,
            original_name=title or provider.value,
            original_url=sanitize_provider_payload(url),
            owner_ref=owner_ref,
            created_at=retrieved_at,
            acquisition_status=ProcessingStatus.COMPLETED,
            access_status=access_status,
        ))
        documents.append(ResearchDocument(
            document_id=document_id,
            source_id=source_id,
            document_type=document_type,
            title=title,
            mime_type=mime_type,
            retrieved_at=retrieved_at,
            snapshot_policy=SnapshotPolicy.REFERENCE_ONLY,
            parse_status=ProcessingStatus.COMPLETED,
            source_locator=locator.model_copy(update={
                "source_id": source_id,
                "document_id": document_id,
            }),
        ))
        assets.append(ResearchAsset(
            asset_id=asset_id,
            document_id=document_id,
            media_type=media_type,
            mime_type=mime_type,
            display_name=title,
            provider=provider,
            provider_resource_id=identity,
            access_status=access_status,
            processing_status=ProcessingStatus.COMPLETED,
            sensitivity_status=SensitivityStatus.UNKNOWN,
            export_policy=ExportPolicy.MANUAL_CONFIRMATION,
            source_locator=locator.model_copy(update={
                "source_id": source_id,
                "document_id": document_id,
            }),
        ))
    reference_locator = SourceLocator(
        provider=(Provider.GOOGLE_FORMS if form_id else Provider.BESTED),
        provider_form_id=form_id,
        provider_item_id=provider_item_id,
        file_id=file_id,
        sheet_name=sheet_name,
        cell=cell,
    )
    if reference_id in {item.reference_id for item in references}:
        return reference_id
    references.append(AssetReference(
        reference_id=reference_id,
        asset_id=asset_id,
        context_type=AssetContextType.SURVEY_QUESTION,
        context_id=canonical_question.question_id,
        role=AssetRole.RESEARCHER_MATERIAL,
        source_locator=reference_locator,
        binding_status=BindingStatus.CONFIRMED,
        binding_confidence=1.0,
    ))
    canonical_question.asset_reference_ids.append(reference_id)
    return reference_id


def map_google_form_capture(
    capture: GoogleFormCapture,
    *,
    owner_ref: str,
    retrieved_at: datetime,
) -> QuestionnaireMappingResult:
    """将已授权获取的 Forms 定义和即时图片捕获映射为完整 Bundle。"""
    owner = _require_owner(owner_ref)
    retrieved = _require_retrieved_at(retrieved_at)
    raw_form = sanitize_provider_payload(capture.raw_form)
    if not isinstance(raw_form, dict) or raw_form.get("formId") != capture.form_id:
        raise ValueError("Google capture 的 formId 与原始定义不一致")
    raw_items = raw_form.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("Google Forms 定义缺少 items")
    definition_hash = provider_definition_sha256(raw_form)
    source_id = _stable_id("src", "google_forms", owner, capture.form_id)
    document_id = _stable_id("doc", source_id, definition_hash)
    snapshot_id = _stable_id("qsn", document_id, definition_hash)
    collection_id = _stable_id("rac", owner, snapshot_id)
    info = raw_form.get("info") if isinstance(raw_form.get("info"), dict) else {}
    title = info.get("title") if isinstance(info.get("title"), str) else ""
    revision = (
        raw_form.get("revisionId")
        if isinstance(raw_form.get("revisionId"), str)
        else None
    )

    provider_items: list[ProviderItemDefinition] = []
    item_ids: list[str] = []
    exact = True
    snapshot_warnings: list[ImportWarning] = []
    for position, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            raise ValueError(f"Google items[{position}] 不是对象")
        provider_item_id = raw_item.get("itemId")
        if not isinstance(provider_item_id, str) or not provider_item_id:
            exact = False
            provider_item_id = _stable_id("missing_item", position, raw_item)
            snapshot_warnings.append(ImportWarning(
                code="missing_google_item_id",
                message="Google Item 缺少稳定 itemId，已使用快照内临时定位",
                field_path=f"items[{position}]",
                blocking=True,
            ))
        item_ids.append(provider_item_id)
        provider_items.append(ProviderItemDefinition(
            provider=Provider.GOOGLE_FORMS,
            provider_item_id=provider_item_id,
            provider_item_type=_google_item_type(raw_item),
            provider_position=position,
            provider_question_ids=_question_ids(raw_item),
            raw_definition=raw_item,
            source_locator=SourceLocator(
                provider=Provider.GOOGLE_FORMS,
                provider_form_id=capture.form_id,
                provider_item_id=provider_item_id,
                question_position=position,
            ),
        ))

    canonical_ids = {
        item_id: _stable_id("q", capture.form_id, item_id)
        for item_id in item_ids
    }
    section_by_item_id = {
        item_id: canonical_ids[item_id]
        for item_id, item in zip(item_ids, raw_items, strict=True)
        if isinstance(item, dict) and "pageBreakItem" in item
    }
    canonical_questions: list[CanonicalQuestion] = []
    supported_by_item: dict[str, bool] = {}
    for item_id, raw_item in zip(item_ids, raw_items, strict=True):
        question, supported = _google_question(
            raw_item,
            item_id=item_id,
            canonical_id=canonical_ids[item_id],
            section_by_item_id=section_by_item_id,
        )
        canonical_questions.append(question)
        supported_by_item[item_id] = supported
        exact = exact and supported
    canonical_by_item = {
        question.provider_item_id: question
        for question in canonical_questions
        if question.provider_item_id is not None
    }

    expected_image_paths = _google_image_paths(raw_items, ("items",))
    successful_image_paths: set[tuple[str | int, ...]] = set()
    for image in capture.images:
        _validate_google_image_capture(image, raw_items)
        if image.json_path in successful_image_paths:
            raise ValueError("Google capture 包含重复图片 JSON 路径")
        successful_image_paths.add(image.json_path)
    failed_image_paths: set[tuple[str | int, ...]] = set()
    image_failure_warnings: list[ImportWarning] = []
    for failure in capture.image_failures:
        _validate_google_image_evidence(
            failure.json_path,
            failure.context,
            raw_items,
        )
        if failure.json_path in failed_image_paths:
            raise ValueError("Google capture 包含重复图片失败 JSON 路径")
        if failure.json_path in successful_image_paths:
            raise ValueError("Google 图片路径不能同时标记为成功和失败")
        failed_image_paths.add(failure.json_path)
        image_failure_warnings.append(_google_image_failure_warning(
            failure,
            form_id=capture.form_id,
        ))
    if successful_image_paths | failed_image_paths != expected_image_paths:
        raise ValueError("Google 图片成功/失败集合与原始表单图片位置不一致")
    if image_failure_warnings:
        exact = False
        snapshot_warnings.extend(image_failure_warnings)

    sources = [ResearchSource(
        source_id=source_id,
        source_kind=SourceKind.PROVIDER_CONNECTION,
        provider=Provider.GOOGLE_FORMS,
        original_name=title,
        original_url=(
            raw_form.get("responderUri")
            if isinstance(raw_form.get("responderUri"), str)
            else None
        ),
        owner_ref=owner,
        created_at=retrieved,
        acquisition_status=(
            ProcessingStatus.PARTIAL
            if image_failure_warnings
            else ProcessingStatus.COMPLETED
        ),
        access_status=AccessStatus.ACCESSIBLE,
        warnings=list(image_failure_warnings),
    )]
    documents = [ResearchDocument(
        document_id=document_id,
        source_id=source_id,
        document_type=DocumentType.QUESTIONNAIRE,
        title=title,
        mime_type="application/vnd.google-apps.form",
        content_hash=definition_hash,
        retrieved_at=retrieved,
        snapshot_policy=SnapshotPolicy.FULL_COPY,
        parse_status=(
            ProcessingStatus.PARTIAL
            if image_failure_warnings
            else ProcessingStatus.COMPLETED
        ),
        warnings=list(image_failure_warnings),
        source_locator=SourceLocator(
            source_id=source_id,
            document_id=document_id,
            provider=Provider.GOOGLE_FORMS,
            provider_form_id=capture.form_id,
            provider_revision=revision,
        ),
    )]
    assets: list[ResearchAsset] = []
    references: list[AssetReference] = []
    media: dict[str, bytes] = {}
    for image in capture.images:
        if content_sha256(image.content) != image.sha256:
            raise ValueError("Google 图片捕获内容与 sha256 不一致")
        if image.context.item_id not in canonical_by_item:
            raise ValueError("Google 图片无法定位到 Provider Item")
        question = canonical_by_item[image.context.item_id]
        image_id = _stable_id("asset", capture.form_id, list(image.json_path))
        reference_id = _stable_id("aref", image_id, question.question_id)
        option: CanonicalOption | None = None
        if image.context.option_index is not None:
            if image.context.option_index >= len(question.options):
                raise ValueError("Google 选项图片索引超出 Canonical 选项范围")
            option = question.options[image.context.option_index]
            reference_id = _stable_id(
                "aref", image_id, question.question_id, option.option_key
            )
        locator = SourceLocator(
            provider=Provider.GOOGLE_FORMS,
            provider_form_id=capture.form_id,
            provider_question_id=image.context.question_id,
            provider_item_id=image.context.item_id,
            question_position=image.context.item_position,
            json_path=list(image.json_path),
        )
        assets.append(ResearchAsset(
            asset_id=image_id,
            document_id=document_id,
            media_type=MediaType.IMAGE,
            mime_type=image.mime_type,
            filename=f"{image.sha256}{_image_extension(image.mime_type)}",
            display_name=(option.label if option else question.title),
            size_bytes=len(image.content),
            content_hash=image.sha256,
            provider=Provider.GOOGLE_FORMS,
            provider_version=revision,
            access_status=AccessStatus.ACCESSIBLE,
            processing_status=ProcessingStatus.COMPLETED,
            sensitivity_status=SensitivityStatus.UNKNOWN,
            export_policy=ExportPolicy.MANUAL_CONFIRMATION,
            source_locator=locator,
        ))
        if option is not None:
            option.asset_reference_ids.append(reference_id)
            context_type = AssetContextType.SURVEY_OPTION
            role = AssetRole.OPTION_STIMULUS
            option_key = option.option_key
        else:
            question.asset_reference_ids.append(reference_id)
            context_type = AssetContextType.SURVEY_QUESTION
            role = (
                AssetRole.QUESTION_STIMULUS
                if _google_item_type(raw_items[image.context.item_position])
                == "imageItem"
                else AssetRole.QUESTION_INSTRUCTION
            )
            option_key = None
        references.append(AssetReference(
            reference_id=reference_id,
            asset_id=image_id,
            context_type=context_type,
            context_id=question.question_id,
            role=role,
            option_key=option_key,
            source_locator=locator,
            binding_status=BindingStatus.CONFIRMED,
            binding_confidence=1.0,
        ))
        media.setdefault(image.sha256, image.content)

    for item_id, raw_item in zip(item_ids, raw_items, strict=True):
        question = canonical_by_item[item_id]
        if "videoItem" in raw_item:
            url = raw_item.get("videoItem", {}).get("video", {}).get("youtubeUri")
            if isinstance(url, str):
                _append_external_reference(
                    url=url,
                    title=question.title,
                    provider_item_id=item_id,
                    canonical_question=question,
                    form_id=capture.form_id,
                    owner_ref=owner,
                    retrieved_at=retrieved,
                    sources=sources,
                    documents=documents,
                    assets=assets,
                    references=references,
                )
        for field in ("title", "description"):
            value = raw_item.get(field)
            if isinstance(value, str):
                for url in re.findall(r"https://[^\s<>'\"]+", value):
                    if _drive_file_id(url):
                        _append_external_reference(
                            url=url,
                            title=question.title or "Google Drive 参考材料",
                            provider_item_id=item_id,
                            canonical_question=question,
                            form_id=capture.form_id,
                            owner_ref=owner,
                            retrieved_at=retrieved,
                            sources=sources,
                            documents=documents,
                            assets=assets,
                            references=references,
                        )

    response_mappings = [
        mapping
        for question in canonical_questions
        if (mapping := _google_response_mapping(question, form_id=capture.form_id))
        is not None
    ]
    publish_state = raw_form.get("publishSettings", {})
    if isinstance(publish_state, dict):
        publish_state = publish_state.get("publishState")
    if not isinstance(publish_state, dict):
        collection_state = CollectionState.UNKNOWN
    elif publish_state.get("isPublished") is True:
        collection_state = (
            CollectionState.OPEN
            if publish_state.get("isAcceptingResponses") is True
            else CollectionState.CLOSED
        )
    elif publish_state.get("isPublished") is False:
        collection_state = CollectionState.CLOSED
    else:
        collection_state = CollectionState.UNKNOWN
    snapshot_status = MappingStatus.EXACT if exact else MappingStatus.PARTIAL
    snapshot = QuestionnaireSnapshot(
        snapshot_id=snapshot_id,
        document_id=document_id,
        provider=Provider.GOOGLE_FORMS,
        provider_form_id=capture.form_id,
        source_mode=QuestionnaireSourceMode.OFFICIAL_API,
        title=title,
        retrieved_at=retrieved,
        provider_revision=revision,
        content_hash=definition_hash,
        collection_state=collection_state,
        item_count=len(provider_items),
        question_count=sum(
            question.canonical_type not in {
                CanonicalQuestionType.SECTION,
                CanonicalQuestionType.STATIC_TEXT,
            }
            for question in canonical_questions
        ),
        asset_count=len(assets),
        mapping_status=snapshot_status,
        provider_raw_definition=raw_form,
        provider_items=provider_items,
        canonical_questions=canonical_questions,
        response_column_mappings=response_mappings,
        warnings=snapshot_warnings,
    )
    collection = ResearchAssetCollection(
        collection_id=collection_id,
        owner_ref=owner,
        sources=sources,
        documents=documents,
        assets=assets,
        references=references,
    )
    validate_research_contract(snapshot, collection)
    return QuestionnaireMappingResult(
        bundle=ResearchAssetBundle(snapshot, collection),
        media=media,
    )


def _bested_provider_ids(
    question: BestedQuestionnaireQuestion,
) -> tuple[str | None, list[tuple[str, str]]]:
    if question.role.startswith("matrix_"):
        rows = [
            (f"Q{question.qid}:row:{index + 1}", label)
            for index, label in enumerate(question.rows)
        ]
        return None, rows
    return f"Q{question.qid}", []


def map_bested_questionnaire_upload(
    parsed: BestedQuestionnaireParseResult,
    *,
    owner_ref: str,
    filename: str,
    questionnaire_content: bytes,
    retrieved_at: datetime,
) -> QuestionnaireMappingResult:
    """将倍市得原问卷上传映射为不依赖发布页的可复现快照。"""
    owner = _require_owner(owner_ref)
    retrieved = _require_retrieved_at(retrieved_at)
    safe_name = _safe_filename(filename)
    if not isinstance(questionnaire_content, bytes) or not questionnaire_content:
        raise ValueError("questionnaire_content 必须是非空 bytes")
    file_hash = content_sha256(questionnaire_content)
    if parsed.content_sha256 != file_hash:
        raise ValueError("倍市得解析结果与原问卷文件内容不一致")
    source_id = _stable_id("src", "bested_upload", owner, file_hash)
    document_id = _stable_id("doc", source_id, file_hash)
    snapshot_id = _stable_id("qsn", document_id, file_hash)
    collection_id = _stable_id("rac", owner, snapshot_id)
    provider_raw = sanitize_provider_payload({
        "workbook_filename": safe_name,
        "sheet_name": parsed.sheet_name,
        "rows": [list(row) for row in parsed.provider_rows],
    })
    media_issue_warnings = [
        _bested_media_issue_warning(
            issue,
            filename=safe_name,
            sheet_name=parsed.sheet_name,
        )
        for issue in parsed.media_issues
    ]
    _validate_bested_media_closure(parsed, questionnaire_content)

    provider_items: list[ProviderItemDefinition] = []
    canonical_questions: list[CanonicalQuestion] = []
    question_by_qid: dict[int, CanonicalQuestion] = {}
    item_id_by_qid: dict[int, str] = {}
    for position, source_question in enumerate(parsed.questions):
        provider_question_id, provider_rows = _bested_provider_ids(source_question)
        provider_ids = (
            [provider_question_id]
            if provider_question_id is not None
            else [row_id for row_id, _ in provider_rows]
        )
        item_id = _stable_id("bested_item", file_hash, source_question.qid)
        canonical_id = _stable_id("q", file_hash, source_question.qid)
        item_id_by_qid[source_question.qid] = item_id
        provider_items.append(ProviderItemDefinition(
            provider=Provider.BESTED,
            provider_item_id=item_id,
            provider_item_type=source_question.source_type,
            provider_position=position,
            provider_question_ids=provider_ids,
            raw_definition={
                "source_cell": source_question.source_cell,
                "raw_heading": source_question.raw_heading,
                "raw_title": source_question.title,
                "raw_rows": [list(row) for row in source_question.raw_rows],
                "raw_options": list(source_question.options),
                "raw_matrix_rows": list(source_question.rows),
            },
            source_locator=SourceLocator(
                provider=Provider.BESTED,
                provider_item_id=item_id,
                file_id=safe_name,
                sheet_name=source_question.sheet_name,
                cell=source_question.source_cell,
                question_position=position,
            ),
        ))
        canonical_type = _BESTED_TYPE_MAP.get(
            source_question.role,
            CanonicalQuestionType.UNKNOWN,
        )
        options = [
            CanonicalOption(
                option_key=_option_key(index),
                value=value,
                label=value,
            )
            for index, value in enumerate(source_question.options)
        ]
        rows = [
            CanonicalRow(
                row_key=f"row-{index + 1}",
                label=label,
                provider_question_id=row_id,
            )
            for index, (row_id, label) in enumerate(provider_rows)
        ]
        question = CanonicalQuestion(
            question_id=canonical_id,
            provider_question_id=provider_question_id,
            provider_item_id=item_id,
            canonical_type=canonical_type,
            title=source_question.title,
            rows=rows,
            options=options,
            mapping_status=(
                MappingStatus.NORMALIZED
                if canonical_type != CanonicalQuestionType.UNKNOWN
                else MappingStatus.UNSUPPORTED
            ),
            mapping_confidence=(
                0.95 if canonical_type != CanonicalQuestionType.UNKNOWN else 0.0
            ),
        )
        canonical_questions.append(question)
        question_by_qid[source_question.qid] = question

    sources = [ResearchSource(
        source_id=source_id,
        source_kind=SourceKind.LOCAL_UPLOAD,
        provider=Provider.BESTED,
        original_name=safe_name,
        owner_ref=owner,
        created_at=retrieved,
        acquisition_status=(
            ProcessingStatus.PARTIAL
            if media_issue_warnings
            else ProcessingStatus.COMPLETED
        ),
        access_status=AccessStatus.ACCESSIBLE,
        warnings=list(media_issue_warnings),
    )]
    documents = [ResearchDocument(
        document_id=document_id,
        source_id=source_id,
        document_type=DocumentType.QUESTIONNAIRE,
        title=(canonical_questions[0].title if canonical_questions else safe_name),
        filename=safe_name,
        mime_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if safe_name.casefold().endswith(".xlsx")
            else "application/vnd.ms-excel"
        ),
        size_bytes=len(questionnaire_content),
        content_hash=file_hash,
        retrieved_at=retrieved,
        snapshot_policy=SnapshotPolicy.FULL_COPY,
        parse_status=(
            ProcessingStatus.PARTIAL
            if media_issue_warnings
            else ProcessingStatus.COMPLETED
        ),
        warnings=list(media_issue_warnings),
        source_locator=SourceLocator(
            source_id=source_id,
            document_id=document_id,
            provider=Provider.BESTED,
            file_id=safe_name,
            sheet_name=parsed.sheet_name,
        ),
    )]
    assets: list[ResearchAsset] = []
    references: list[AssetReference] = []
    media: dict[str, bytes] = {}
    for index, image in enumerate(parsed.images):
        image_hash = content_sha256(image.content)
        asset_id = _stable_id(
            "asset", document_id, image.source_cell, index, image_hash
        )
        item_id = (
            item_id_by_qid.get(image.question_qid)
            if image.question_qid is not None
            else None
        )
        locator = SourceLocator(
            provider=Provider.BESTED,
            provider_item_id=item_id,
            file_id=safe_name,
            sheet_name=image.sheet_name,
            cell=image.source_cell,
            anchor=image.source_cell,
            coverage=image.coverage,
        )
        assets.append(ResearchAsset(
            asset_id=asset_id,
            document_id=document_id,
            media_type=MediaType.IMAGE,
            mime_type=image.mime_type,
            filename=f"{image_hash}{_image_extension(image.mime_type)}",
            display_name=(
                question_by_qid[image.question_qid].title
                if image.question_qid in question_by_qid
                else f"问卷图片 {index + 1}"
            ),
            size_bytes=len(image.content),
            content_hash=image_hash,
            provider=Provider.BESTED,
            access_status=AccessStatus.ACCESSIBLE,
            processing_status=ProcessingStatus.COMPLETED,
            sensitivity_status=SensitivityStatus.UNKNOWN,
            export_policy=ExportPolicy.MANUAL_CONFIRMATION,
            source_locator=locator,
        ))
        if image.question_qid in question_by_qid:
            question = question_by_qid[image.question_qid]
            reference_id = _stable_id("aref", asset_id, question.question_id)
            question.asset_reference_ids.append(reference_id)
            references.append(AssetReference(
                reference_id=reference_id,
                asset_id=asset_id,
                context_type=AssetContextType.SURVEY_QUESTION,
                context_id=question.question_id,
                role=AssetRole.QUESTION_STIMULUS,
                source_locator=locator,
                binding_status=BindingStatus.NEEDS_REVIEW,
                binding_confidence=0.7,
                warnings=[ImportWarning(
                    code="bested_image_question_scope_only",
                    message="图片仅按原问卷题目行区间关联，具体题干/选项位置需确认",
                    blocking=False,
                    source_locator=locator,
                )],
            ))
        else:
            references.append(AssetReference(
                reference_id=_stable_id("aref", asset_id, document_id),
                asset_id=asset_id,
                context_type=AssetContextType.RESEARCH_DOCUMENT,
                context_id=document_id,
                role=AssetRole.RESEARCHER_MATERIAL,
                source_locator=locator,
                binding_status=BindingStatus.NEEDS_REVIEW,
                binding_confidence=0.0,
            ))
        media.setdefault(image_hash, image.content)

    for hyperlink in parsed.hyperlinks:
        if hyperlink.question_qid not in question_by_qid:
            continue
        question = question_by_qid[hyperlink.question_qid]
        _append_external_reference(
            url=hyperlink.url,
            title=hyperlink.display_text or question.title,
            provider_item_id=item_id_by_qid[hyperlink.question_qid],
            canonical_question=question,
            form_id=None,
            owner_ref=owner,
            retrieved_at=retrieved,
            sources=sources,
            documents=documents,
            assets=assets,
            references=references,
            file_id=safe_name,
            sheet_name=hyperlink.sheet_name,
            cell=hyperlink.source_cell,
        )

    response_mappings = [
        ResponseColumnMapping(
            question_id=question.question_id,
            mapping_status=MappingStatus.NEEDS_REVIEW,
            mapping_confidence=0.0,
            warnings=[ImportWarning(
                code="response_export_required",
                message="需结合本次倍市得回答导出后确认回答列映射",
                blocking=False,
            )],
        )
        for question in canonical_questions
    ]
    snapshot = QuestionnaireSnapshot(
        snapshot_id=snapshot_id,
        document_id=document_id,
        provider=Provider.BESTED,
        source_mode=QuestionnaireSourceMode.ORIGINAL_QUESTIONNAIRE_UPLOAD,
        title=(canonical_questions[0].title if canonical_questions else safe_name),
        retrieved_at=retrieved,
        content_hash=file_hash,
        collection_state=CollectionState.UNKNOWN,
        item_count=len(provider_items),
        question_count=len(canonical_questions),
        asset_count=len(assets),
        mapping_status=MappingStatus.PARTIAL,
        provider_raw_definition=provider_raw,
        provider_items=provider_items,
        canonical_questions=canonical_questions,
        response_column_mappings=response_mappings,
        warnings=[
            ImportWarning(
                code="bested_file_local_ids",
                message="题目身份仅在本次原问卷文件快照内稳定，不代表倍市得在线 ID",
                blocking=False,
            ),
            *media_issue_warnings,
        ],
    )
    collection = ResearchAssetCollection(
        collection_id=collection_id,
        owner_ref=owner,
        sources=sources,
        documents=documents,
        assets=assets,
        references=references,
    )
    validate_research_contract(snapshot, collection)
    return QuestionnaireMappingResult(
        bundle=ResearchAssetBundle(snapshot, collection),
        media=media,
    )
