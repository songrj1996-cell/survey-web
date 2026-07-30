"""访谈报告完整流程：多 Sheet 证据归并、分模块写作、审校、归档与恢复。"""
import asyncio
import json
import logging
import re
from pathlib import Path

from fastapi import HTTPException, Request

from app.core.config import (
    INTERVIEW_AUDIT_MODEL,
    INTERVIEW_AUDIT_REASONING,
    INTERVIEW_EXTRACT_MAX_TOKENS,
    INTERVIEW_EXTRACT_MODEL,
    INTERVIEW_EXTRACT_REASONING,
    INTERVIEW_FALLBACK_MODELS,
    INTERVIEW_MAX_INPUT_CHARS,
    INTERVIEW_MAX_REPAIR_ROUNDS,
    INTERVIEW_MAX_UPLOAD_BYTES,
    INTERVIEW_REPAIR_MODEL,
    INTERVIEW_REPAIR_REASONING,
    INTERVIEW_REPORT_MODEL,
    INTERVIEW_REPORT_REASONING,
    LLM_STREAM_HEARTBEAT_SECONDS,
)
from app.core.interview_parsing import (
    interview_source_refs,
    parse_interview_workbook,
    serialize_interview_workbook,
)
from app.core.responses import sse_event
from app.core.security import _assign_session_owner, _visible_to_owner
from app.integrations.llm_client import collect_chat_completion
from app.services.report_history import save_to_history
from app.storage.sessions import get_session, new_session, save_session


_INTERVIEW_LOCKS: dict[str, asyncio.Lock] = {}
_REQUIRED_MODULE_HEADINGS = ("### 模块判断", "### 主要发现", "### 产品建议")
logger = logging.getLogger(__name__)


def _stage_models(primary_model: str) -> tuple[str, ...]:
    """按主模型、备用模型顺序去重，供单个访谈阶段使用。"""
    models: list[str] = []
    for candidate in (primary_model, *INTERVIEW_FALLBACK_MODELS):
        model = str(candidate or "").strip()
        if model and model not in models:
            models.append(model)
    return tuple(models)


def _model_failure_reason(exc: Exception) -> str:
    detail = str(exc or "").lower()
    if (
        "max_output_tokens" in detail
        or "max_tokens" in detail
        or "finish_reason=length" in detail
    ):
        return "output_limit"
    return "request_failed"


def _stage_failure(exc: Exception, models: tuple[str, ...]) -> RuntimeError:
    if _model_failure_reason(exc) == "output_limit":
        attempted = "、".join(models)
        return RuntimeError(
            f"当前阶段生成内容超过输出上限，已尝试模型：{attempted}。"
            "已完成的进度会保留，请稍后继续生成"
        )
    return RuntimeError(str(exc))


def _parse_json_object(text: str, label: str) -> dict:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError(f"{label}没有返回有效 JSON")
    try:
        value = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label}返回的 JSON 无法解析：{exc.msg}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label}返回格式不正确")
    return value


def _sheet_summary(workbook: dict) -> list[dict]:
    return [
        {
            "name": sheet.get("name", ""),
            "nonempty_count": sheet.get("nonempty_count", 0),
            "max_row": sheet.get("max_row", 0),
            "max_column": sheet.get("max_column", 0),
        }
        for sheet in workbook.get("sheets", [])
    ]


def _filter_evidence_refs(extraction: dict, valid_refs: set[str]) -> tuple[dict, int]:
    """移除模型虚构的引用；无有效引用的证据不能进入报告。"""
    removed = 0
    clean_modules: list[dict] = []
    modules = extraction.get("modules")
    if not isinstance(modules, list):
        modules = []
    for module in modules:
        if not isinstance(module, dict):
            continue
        title = str(module.get("title") or "").strip()
        if not title:
            continue
        clean_evidence: list[dict] = []
        evidence = module.get("evidence")
        if not isinstance(evidence, list):
            evidence = []
        for item in evidence:
            if not isinstance(item, dict):
                continue
            raw_refs = item.get("source_refs")
            if not isinstance(raw_refs, list):
                raw_refs = []
            refs: list[str] = []
            for raw_ref in raw_refs:
                ref = str(raw_ref or "").strip().strip("[]")
                if ref in valid_refs and ref not in refs:
                    refs.append(ref)
                elif ref:
                    removed += 1
            if not refs:
                removed += 1
                continue
            clean_item = dict(item)
            clean_item["player_id"] = str(item.get("player_id") or "").strip()
            clean_item["source_refs"] = refs
            clean_evidence.append(clean_item)
        if clean_evidence:
            clean_module = dict(module)
            clean_module["title"] = title
            clean_module["evidence"] = clean_evidence
            clean_modules.append(clean_module)
    cleaned = dict(extraction)
    cleaned["modules"] = clean_modules
    return cleaned, removed


def _invalid_report_ref_markers(report_md: str, valid_refs: set[str]) -> list[str]:
    """检查报告中的 !A1 类引用是否确实对应输入工作簿。"""
    invalid: list[str] = []
    for match in re.finditer(r"![A-Z]{1,3}\d+", report_md, flags=re.IGNORECASE):
        end = match.end()
        if any(
            end >= len(ref) and report_md[end - len(ref):end].lower() == ref.lower()
            for ref in valid_refs
        ):
            continue
        marker = match.group(0)
        if marker not in invalid:
            invalid.append(marker)
    return invalid


def _module_structure_issues(
    module_md: str,
    title: str,
    valid_refs: set[str],
) -> list[str]:
    issues: list[str] = []
    if not re.search(rf"^##\s+{re.escape(title)}\s*$", module_md, re.MULTILINE):
        issues.append(f"必须以“## {title}”作为模块标题")
    for heading in _REQUIRED_MODULE_HEADINGS:
        if heading not in module_md:
            issues.append(f"缺少“{heading}”")
    if not re.search(r"^####\s+发现\s*\d+", module_md, re.MULTILINE):
        issues.append("“主要发现”下至少需要一个“#### 发现N”")
    invalid_refs = _invalid_report_ref_markers(module_md, valid_refs)
    if invalid_refs:
        issues.append("包含无法对应原始工作簿的引用")
    return issues


def _safe_report_title(filename: str) -> str:
    stem = re.sub(r"\s+", " ", Path(filename).stem).strip()
    stem = re.sub(r'[\\/:*?"<>|]', "_", stem)[:90] or "访谈研究"
    if stem.endswith(("访谈报告", "研究报告")):
        return stem
    return f"{stem}·访谈报告"


def _report_header(sess: dict, extraction: dict) -> str:
    workbook = sess.get("interview_workbook") or {}
    sheet_count = len(workbook.get("sheets") or [])
    player_count = len(extraction.get("players") or [])
    title = _safe_report_title(sess.get("filename") or "访谈研究.xlsx")
    scope = f"本报告归并了 {sheet_count} 个 Sheet"
    if player_count:
        scope += f"，识别出 {player_count} 位玩家"
    scope += "；模块判断与发现仅依据可追溯的访谈记录。"
    focus = str(sess.get("interview_research_focus") or "").strip()
    lines = [f"# {title}", "", f"> {scope}"]
    if focus:
        lines.extend(["", f"> 本次重点：{focus}"])
    return "\n".join(lines)


def _assemble_report(sess: dict, extraction: dict, module_reports: list[dict]) -> str:
    body = "\n\n".join(
        str(item.get("report_md") or "").strip()
        for item in module_reports
        if str(item.get("report_md") or "").strip()
    )
    return f"{_report_header(sess, extraction)}\n\n{body}".strip()


def _progress_event(
    *,
    stage: str,
    percent: int,
    message: str,
    module_index: int = 0,
    total_modules: int = 0,
    module_title: str = "",
) -> str:
    return sse_event(
        {
            "type": "progress",
            "stage": stage,
            "percent": max(0, min(100, int(percent))),
            "message": message,
            "module_index": module_index,
            "total_modules": total_modules,
            "module_title": module_title,
        }
    )


def _session_result(sess: dict) -> dict:
    workbook = sess.get("interview_workbook") or {}
    extraction = sess.get("interview_extraction") or {}
    return {
        "session_id": sess.get("session_id", ""),
        "status": sess.get("interview_status", "uploaded"),
        "stage": sess.get("interview_stage", "uploaded"),
        "percent": sess.get("interview_progress", 0),
        "message": sess.get("interview_progress_message", ""),
        "filename": sess.get("filename", ""),
        "sheets": _sheet_summary(workbook),
        "player_count": len(extraction.get("players") or []),
        "module_count": len(extraction.get("modules") or []),
        "completed_modules": len(sess.get("interview_module_reports") or []),
        "report_md": sess.get("report_md", ""),
        "partial_report_md": _assemble_report(
            sess,
            extraction,
            sess.get("interview_module_reports") or [],
        ) if extraction else "",
        "models_used": sess.get("interview_models_used") or {},
        "audit": sess.get("interview_audit") or {},
        "report_no": sess.get("interview_report_no", ""),
    }


def _require_owned_interview_session(session_id: str, login: dict | None) -> dict:
    sess = get_session(session_id)
    if sess.get("kind") != "interview" or not sess.get("interview_source_text"):
        raise HTTPException(status_code=400, detail="访谈会话无效，请重新上传文件")
    if not _visible_to_owner(sess, login):
        raise HTTPException(status_code=404, detail="访谈会话不存在")
    return sess


async def handle_interview_upload(
    filename: str,
    content: bytes,
    login: dict | None,
    research_focus: str = "",
) -> dict:
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(content) > INTERVIEW_MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="文件超过访谈报告上传大小限制")
    workbook = parse_interview_workbook(filename, content)
    serialized = serialize_interview_workbook(workbook)
    if len(serialized) > INTERVIEW_MAX_INPUT_CHARS:
        raise HTTPException(
            status_code=400,
            detail="访谈记录内容过多，超过当前单次报告处理上限",
        )

    session_id = new_session()
    sess = get_session(session_id)
    sess.update(
        {
            "session_id": session_id,
            "kind": "interview",
            "mode": "interview",
            "filename": filename,
            "interview_workbook": workbook,
            "interview_source_text": serialized,
            "interview_research_focus": str(research_focus or "").strip()[:2000],
            "interview_status": "uploaded",
            "interview_stage": "uploaded",
            "interview_progress": 5,
            "interview_progress_message": "文件解析完成，等待生成报告",
            "interview_module_reports": [],
            "interview_models_used": {},
        }
    )
    _assign_session_owner(sess, login)
    save_session(session_id, sess)
    return {
        "session_id": session_id,
        "filename": filename,
        "size": len(content),
        "sheets": _sheet_summary(workbook),
        "total_cells": workbook["total_cells"],
        "total_chars": workbook["total_chars"],
        "research_focus": sess["interview_research_focus"],
    }


def validate_interview_session(session_id: str, login: dict | None = None) -> None:
    _require_owned_interview_session(session_id, login)


def get_interview_status(session_id: str, login: dict | None) -> dict:
    return _session_result(_require_owned_interview_session(session_id, login))


async def _llm_with_heartbeats(messages: list[dict], **kwargs):
    task = asyncio.create_task(collect_chat_completion(messages, **kwargs))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=LLM_STREAM_HEARTBEAT_SECONDS)
            if task in done:
                yield await task
                return
            yield None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _extract_messages(source_text: str, research_focus: str) -> list[dict]:
    focus_line = f"\n本次研究重点：{research_focus}" if research_focus else ""
    return [
        {
            "role": "system",
            "content": (
                "你是资深游戏用户研究员。需要把同一访谈提纲、同一批玩家、不同"
                "记录者的多个 Sheet 归并为可追溯研究证据。先判断 Sheet 是互补记录"
                "还是不同玩家，不按固定行号机械对齐。追问必须回到父问题语境；"
                "“无记录”不能自动解释成“未询问”。只依据输入，不补写事实。"
            ),
        },
        {
            "role": "user",
            "content": (
                "输出严格 JSON，不要 Markdown。结构："
                '{"players":[{"player_id":"P01","aliases":[],"profile_summary":"",'
                '"behavior_logic":""}],"modules":[{"title":"","scope_summary":"",'
                '"evidence":[{"player_id":"P01","need":"","logic_reason":"",'
                '"finding":"","record_excerpt":"","source_refs":["Sheet名!A1"],'
                '"followup_context":"","coverage_state":"confirmed_answer"}]}],'
                '"limitations":[]}。coverage_state 只能使用 confirmed_answer、'
                "applicable_no_record、not_applicable、pending_assignment。模块以提纲中的"
                "产品/功能模块为准；同一玩家在不同 Sheet 的记录应补充或交叉核对，"
                "不要重复算成两位玩家。每条用于结论的 evidence 必须有真实单元格引用。"
                + focus_line
                + "\n\n访谈记录：\n"
                + source_text
            ),
        },
    ]


def _module_messages(
    module: dict,
    players: list,
    limitations: list,
    research_focus: str,
) -> list[dict]:
    focus_line = f"\n本次研究重点：{research_focus}" if research_focus else ""
    payload = {
        "players": players,
        "module": module,
        "limitations": limitations,
    }
    return [
        {
            "role": "system",
            "content": (
                "你是资深游戏用户研究报告作者。你的任务是解释玩家需求及其形成逻辑，"
                "而不是机械摘录。每个判断必须能被玩家记录支持；保留不同玩家之间的"
                "差异，不用多数意见覆盖少数但重要的场景。"
            ),
        },
        {
            "role": "user",
            "content": (
                "直接输出一个模块的中文 Markdown，不要代码围栏。严格使用：\n"
                "## 模块名\n\n"
                "### 模块判断\n\n"
                "先用一段话清楚陈述玩家真正需要什么。随后在适用时用项目符号拆解"
                "不同的需求逻辑（行为、场景、顾虑、决策原因），最后用一段“因此”"
                "说明该功能应解决的核心问题。\n\n"
                "### 主要发现\n\n"
                "#### 发现1：具体发现标题\n\n"
                "- P01：接近原记录的引用内容和必要语境。[来源：Sheet!A1]\n\n"
                "每个发现下逐一展开所有相关玩家；不能只写汇总，也不能为了凑人数"
                "加入无证据玩家。追问内容必须和父问题一起解释。\n\n"
                "### 产品建议\n\n"
                "- 给出一至六条简洁、可执行且与本模块证据直接对应的建议。\n\n"
                "不要增加其它一级或二级章节，不编造比例、玩家、原话或来源。"
                + focus_line
                + "\n\n研究证据 JSON：\n"
                + json.dumps(payload, ensure_ascii=False)
            ),
        },
    ]


def _module_repair_messages(
    module_md: str,
    module: dict,
    players: list,
    issues: list[str],
) -> list[dict]:
    return [
        {
            "role": "system",
            "content": "你负责修订证据型访谈报告模块，只能使用给定证据。",
        },
        {
            "role": "user",
            "content": (
                "按问题修订并直接输出完整模块 Markdown。必须保留“## 模块名”、"
                "“### 模块判断”、“### 主要发现”、“#### 发现N”和“### 产品建议”。"
                "不得新增证据中不存在的事实或引用。\n\n问题：\n"
                + json.dumps(issues, ensure_ascii=False)
                + "\n\n玩家信息：\n"
                + json.dumps(players, ensure_ascii=False)
                + "\n\n模块证据：\n"
                + json.dumps(module, ensure_ascii=False)
                + "\n\n原模块：\n"
                + module_md
            ),
        },
    ]


def _audit_messages(report_md: str, extraction: dict) -> list[dict]:
    return [
        {
            "role": "system",
            "content": "你是严格的游戏用户研究证据审校员，只审核，不直接重写。",
        },
        {
            "role": "user",
            "content": (
                "逐模块审核报告：模块判断是否明确说明需求和逻辑原因；主要发现是否"
                "按发现组织并逐玩家展开；引用与 source_refs 是否一致；产品建议是否"
                "简洁且由证据支持；是否夸大、遗漏关键分歧或把无记录误写成未询问。"
                "输出严格 JSON："
                '{"ok":true,"issues":[{"module_title":"","problem":"","suggestion":""}],'
                '"summary":""}。没有实质问题时 issues 必须为空。\n\n证据：\n'
                + json.dumps(extraction, ensure_ascii=False)
                + "\n\n报告：\n"
                + report_md
            ),
        },
    ]


async def _collect_stage(
    *,
    messages: list[dict],
    model: str,
    reasoning: str,
    request: Request,
    stage: str,
    percent: int,
    module_index: int = 0,
    total_modules: int = 0,
    module_title: str = "",
    max_tokens: int | None = None,
):
    models = _stage_models(model)
    last_error: Exception | None = None
    previous_model = ""
    for model_index, candidate in enumerate(models):
        is_fallback = model_index > 0
        yield (
            "heartbeat",
            sse_event(
                {
                    "type": "interview_model_status",
                    "stage": stage,
                    "percent": percent,
                    "model": candidate,
                    "primary_model": models[0],
                    "is_fallback": is_fallback,
                    "previous_model": previous_model,
                    "fallback_reason": (
                        _model_failure_reason(last_error)
                        if is_fallback and last_error is not None
                        else ""
                    ),
                    "module_index": module_index,
                    "total_modules": total_modules,
                    "module_title": module_title,
                }
            ),
        )
        try:
            async for result in _llm_with_heartbeats(
                messages,
                models=(candidate,),
                max_tokens=max_tokens,
                reasoning_effort=reasoning,
            ):
                if result is None:
                    yield (
                        "heartbeat",
                        sse_event(
                            {
                                "type": "heartbeat",
                                "stage": stage,
                                "percent": percent,
                                "model": candidate,
                                "is_fallback": is_fallback,
                                "module_index": module_index,
                                "total_modules": total_modules,
                                "module_title": module_title,
                            }
                        ),
                    )
                    if await request.is_disconnected():
                        return
                    continue
                yield ("result", result)
                return
        except Exception as exc:
            last_error = exc
            previous_model = candidate
            if model_index + 1 < len(models):
                continue
            raise _stage_failure(exc, models) from exc

    if last_error is not None:
        raise _stage_failure(last_error, models) from last_error


def _audit_issue_key(issue: dict) -> tuple[str, str]:
    return (
        str(issue.get("module_title") or "").strip(),
        str(issue.get("problem") or "").strip(),
    )


async def revise_interview_audit_issue_stream(
    session_id: str,
    issue_index: int,
    request: Request,
    login: dict | None,
):
    """按单条人工选中的审校意见修订模块，通过硬校验和复审后再覆盖报告。"""
    lock = _INTERVIEW_LOCKS.setdefault(session_id, asyncio.Lock())
    if lock.locked():
        yield sse_event({"type": "error", "message": "该访谈报告正在处理，请稍后再试"})
        return

    async with lock:
        sess = _require_owned_interview_session(session_id, login)
        original_report_md = str(sess.get("report_md") or "")
        try:
            if sess.get("interview_status") != "completed" or not original_report_md:
                raise RuntimeError("报告尚未生成完成，暂时无法按提醒修订")

            original_audit = dict(sess.get("interview_audit") or {})
            original_issues = original_audit.get("issues") or []
            if issue_index < 0 or issue_index >= len(original_issues):
                raise RuntimeError("未找到这条审校提醒，请刷新报告后重试")
            issue = original_issues[issue_index]
            if not isinstance(issue, dict):
                raise RuntimeError("这条审校提醒格式无效，请重新生成报告")
            if issue.get("review_status") == "confirmed":
                raise RuntimeError("这条提醒已确认，无需再次修订")

            module_title = str(issue.get("module_title") or "").strip()
            if not module_title:
                raise RuntimeError("审校提醒没有关联模块，无法安全修订")

            workbook = sess.get("interview_workbook") or {}
            extraction = sess.get("interview_extraction") or {}
            players = extraction.get("players") or []
            modules = extraction.get("modules") or []
            module_reports = [
                dict(item) for item in (sess.get("interview_module_reports") or [])
            ]
            target_module = next(
                (
                    module for module in modules
                    if str(module.get("title") or "").strip() == module_title
                ),
                None,
            )
            target_report = next(
                (
                    item for item in module_reports
                    if str(item.get("title") or "").strip() == module_title
                ),
                None,
            )
            if not target_module or not target_report:
                raise RuntimeError(f"无法定位提醒对应的模块“{module_title}”")

            issue_instructions = [
                text
                for text in (
                    str(issue.get("problem") or "").strip(),
                    str(issue.get("suggestion") or "").strip(),
                )
                if text
            ]
            if not issue_instructions:
                raise RuntimeError("这条审校提醒没有可执行的修改建议")

            yield sse_event({
                "type": "interview_review_progress",
                "message": f"正在按审校建议修订模块：{module_title}",
                "module_title": module_title,
            })
            repaired_md = ""
            repair_model = ""
            async for kind, result in _collect_stage(
                messages=_module_repair_messages(
                    str(target_report.get("report_md") or ""),
                    target_module,
                    players,
                    issue_instructions,
                ),
                model=INTERVIEW_REPAIR_MODEL,
                reasoning=INTERVIEW_REPAIR_REASONING,
                request=request,
                stage="review_repair",
                percent=96,
                module_title=module_title,
            ):
                if kind == "heartbeat":
                    yield result
                else:
                    repaired_md, repair_model = result
            if not repaired_md:
                return

            valid_refs = interview_source_refs(workbook)
            repaired_md = repaired_md.strip()
            hard_issues = _module_structure_issues(repaired_md, module_title, valid_refs)
            if hard_issues:
                raise RuntimeError(
                    "修订结果未通过结构与引用校验，当前报告未被修改："
                    + "；".join(hard_issues)
                )
            target_report["report_md"] = repaired_md
            candidate_report_md = _assemble_report(sess, extraction, module_reports)

            yield sse_event({
                "type": "interview_review_progress",
                "message": "修订完成，正在重新进行证据与质量复审",
                "module_title": module_title,
            })
            audit_text = ""
            audit_model = ""
            async for kind, result in _collect_stage(
                messages=_audit_messages(candidate_report_md, extraction),
                model=INTERVIEW_AUDIT_MODEL,
                reasoning=INTERVIEW_AUDIT_REASONING,
                request=request,
                stage="review_audit",
                percent=98,
                module_title=module_title,
            ):
                if kind == "heartbeat":
                    yield result
                else:
                    audit_text, audit_model = result
            if not audit_text:
                return

            audit = _parse_json_object(audit_text, "报告复审")
            issues = [
                dict(item) for item in (audit.get("issues") or [])
                if isinstance(item, dict)
            ]
            local_issues: list[str] = []
            for item in module_reports:
                local_issues.extend(
                    _module_structure_issues(
                        str(item.get("report_md") or ""),
                        str(item.get("title") or ""),
                        valid_refs,
                    )
                )
            if local_issues:
                raise RuntimeError(
                    "修订后的报告未通过结构与引用校验，当前报告未被修改"
                )
            if audit.get("ok") is not True and not issues:
                issues = [{
                    "module_title": module_title,
                    "problem": str(audit.get("summary") or "复审仍建议人工确认").strip(),
                    "suggestion": "请结合原始访谈证据确认当前表述。",
                }]

            confirmed = {
                _audit_issue_key(item): item
                for item in original_issues
                if isinstance(item, dict) and item.get("review_status") == "confirmed"
            }
            for item in issues:
                previous = confirmed.get(_audit_issue_key(item))
                if previous:
                    for key in ("review_status", "reviewed_at", "reviewed_by"):
                        if previous.get(key):
                            item[key] = previous[key]

            audit.update({
                "ok": not issues,
                "issues": issues,
                "local_issues": [],
                "status": "warning" if issues else "passed",
                "manual_review_round": int(
                    original_audit.get("manual_review_round") or 0
                ) + 1,
                "repair_exhausted": False,
            })
            models_used = dict(sess.get("interview_models_used") or {})
            models_used[f"manual_repair_{audit['manual_review_round']}"] = repair_model
            models_used[f"manual_audit_{audit['manual_review_round']}"] = audit_model
            sess.update({
                "interview_module_reports": module_reports,
                "interview_audit": audit,
                "interview_models_used": models_used,
                "interview_progress_message": (
                    "定向修订完成；仍有待确认提醒"
                    if issues else "定向修订完成并通过质量复审"
                ),
                "report_md": candidate_report_md,
            })
            save_session(session_id, sess)
            history_entry = save_to_history(session_id, sess)
            if history_entry:
                sess["interview_report_no"] = history_entry.get("report_no", "")
                save_session(session_id, sess)
            yield sse_event({
                "type": "interview_review_done",
                "message": sess["interview_progress_message"],
                **_session_result(sess),
            })
        except Exception as exc:
            logger.exception(
                "interview manual review failed session=%s issue_index=%s",
                session_id,
                issue_index,
            )
            yield sse_event({
                "type": "error",
                "message": str(exc),
                "report_unchanged": True,
            })


async def interview_report_stream(
    session_id: str,
    request: Request,
    login: dict | None = None,
):
    lock = _INTERVIEW_LOCKS.setdefault(session_id, asyncio.Lock())
    if lock.locked():
        yield sse_event({"type": "error", "message": "该访谈报告正在生成，请稍后恢复进度"})
        return

    async with lock:
        sess = _require_owned_interview_session(session_id, login)
        if sess.get("interview_status") == "completed" and sess.get("report_md"):
            yield sse_event({"type": "interview_done", **_session_result(sess)})
            return

        workbook = sess["interview_workbook"]
        valid_refs = interview_source_refs(workbook)
        models_used = dict(sess.get("interview_models_used") or {})
        try:
            sess["interview_status"] = "running"
            extraction = sess.get("interview_extraction")
            if not extraction:
                sess.update(
                    {
                        "interview_stage": "extract",
                        "interview_progress": 8,
                        "interview_progress_message": "正在归并玩家身份、提纲模块和多记录者证据",
                    }
                )
                save_session(session_id, sess)
                yield _progress_event(
                    stage="extract",
                    percent=8,
                    message=sess["interview_progress_message"],
                )
                extraction_text = ""
                async for kind, result in _collect_stage(
                    messages=_extract_messages(
                        sess["interview_source_text"],
                        sess.get("interview_research_focus", ""),
                    ),
                    model=INTERVIEW_EXTRACT_MODEL,
                    reasoning=INTERVIEW_EXTRACT_REASONING,
                    max_tokens=INTERVIEW_EXTRACT_MAX_TOKENS,
                    request=request,
                    stage="extract",
                    percent=8,
                ):
                    if kind == "heartbeat":
                        yield result
                    else:
                        extraction_text, models_used["extract"] = result
                if not extraction_text:
                    return
                extraction = _parse_json_object(extraction_text, "证据归并")
                extraction, removed_refs = _filter_evidence_refs(extraction, valid_refs)
                if not extraction.get("modules"):
                    raise RuntimeError("没有提取到带有效单元格引用的功能模块证据")
                sess.update(
                    {
                        "interview_extraction": extraction,
                        "interview_removed_invalid_refs": removed_refs,
                        "interview_models_used": models_used,
                        "interview_player_count": len(extraction.get("players") or []),
                        "interview_module_count": len(extraction.get("modules") or []),
                        "interview_progress": 18,
                        "interview_progress_message": (
                            f"证据归并完成：{len(extraction.get('players') or [])} 位玩家，"
                            f"{len(extraction.get('modules') or [])} 个模块"
                        ),
                    }
                )
                save_session(session_id, sess)
                yield _progress_event(
                    stage="extract_done",
                    percent=18,
                    message=sess["interview_progress_message"],
                )

            modules = extraction.get("modules") or []
            players = extraction.get("players") or []
            limitations = extraction.get("limitations") or []
            module_reports = list(sess.get("interview_module_reports") or [])
            completed = len(module_reports)
            total_modules = len(modules)
            for index in range(completed, total_modules):
                module = modules[index]
                title = str(module.get("title") or f"模块 {index + 1}").strip()
                start_percent = 18 + round(58 * index / max(1, total_modules))
                sess.update(
                    {
                        "interview_stage": "write",
                        "interview_progress": start_percent,
                        "interview_progress_message": (
                            f"正在撰写第 {index + 1}/{total_modules} 个模块：{title}"
                        ),
                    }
                )
                save_session(session_id, sess)
                yield _progress_event(
                    stage="write",
                    percent=start_percent,
                    message=sess["interview_progress_message"],
                    module_index=index + 1,
                    total_modules=total_modules,
                    module_title=title,
                )
                module_md = ""
                async for kind, result in _collect_stage(
                    messages=_module_messages(
                        module,
                        players,
                        limitations,
                        sess.get("interview_research_focus", ""),
                    ),
                    model=INTERVIEW_REPORT_MODEL,
                    reasoning=INTERVIEW_REPORT_REASONING,
                    request=request,
                    stage="write",
                    percent=start_percent,
                    module_index=index + 1,
                    total_modules=total_modules,
                    module_title=title,
                ):
                    if kind == "heartbeat":
                        yield result
                    else:
                        module_md, models_used[f"module_{index + 1}"] = result
                if not module_md:
                    return

                structure_issues = _module_structure_issues(module_md, title, valid_refs)
                if structure_issues:
                    repaired_md = ""
                    async for kind, result in _collect_stage(
                        messages=_module_repair_messages(
                            module_md,
                            module,
                            players,
                            structure_issues,
                        ),
                        model=INTERVIEW_REPAIR_MODEL,
                        reasoning=INTERVIEW_REPAIR_REASONING,
                        request=request,
                        stage="module_repair",
                        percent=start_percent,
                        module_index=index + 1,
                        total_modules=total_modules,
                        module_title=title,
                    ):
                        if kind == "heartbeat":
                            yield result
                        else:
                            repaired_md, models_used[f"module_{index + 1}_repair"] = result
                    if not repaired_md:
                        return
                    module_md = repaired_md
                    structure_issues = _module_structure_issues(module_md, title, valid_refs)
                    if structure_issues:
                        raise RuntimeError(f"模块“{title}”未通过结构与引用检查")

                module_reports.append({"title": title, "report_md": module_md.strip()})
                percent = 18 + round(58 * (index + 1) / max(1, total_modules))
                sess.update(
                    {
                        "interview_module_reports": module_reports,
                        "interview_models_used": models_used,
                        "interview_progress": percent,
                        "interview_progress_message": (
                            f"已完成第 {index + 1}/{total_modules} 个模块：{title}"
                        ),
                    }
                )
                save_session(session_id, sess)
                yield sse_event(
                    {
                        "type": "interview_module_done",
                        "stage": "write",
                        "percent": percent,
                        "message": sess["interview_progress_message"],
                        "module_index": index + 1,
                        "total_modules": total_modules,
                        "module_title": title,
                        "module_md": module_md.strip(),
                        "partial_report_md": _assemble_report(
                            sess,
                            extraction,
                            module_reports,
                        ),
                    }
                )
                if await request.is_disconnected():
                    return

            report_md = _assemble_report(sess, extraction, module_reports)
            audit: dict = {}
            for repair_round in range(INTERVIEW_MAX_REPAIR_ROUNDS + 1):
                audit_percent = 80 + repair_round * 7
                sess.update(
                    {
                        "interview_stage": "audit",
                        "interview_progress": audit_percent,
                        "interview_progress_message": (
                            "正在核对需求逻辑、逐玩家证据和产品建议"
                            if repair_round == 0
                            else f"正在进行第 {repair_round + 1} 次质量复审"
                        ),
                    }
                )
                save_session(session_id, sess)
                yield _progress_event(
                    stage="audit",
                    percent=audit_percent,
                    message=sess["interview_progress_message"],
                    total_modules=total_modules,
                )
                audit_text = ""
                async for kind, result in _collect_stage(
                    messages=_audit_messages(report_md, extraction),
                    model=INTERVIEW_AUDIT_MODEL,
                    reasoning=INTERVIEW_AUDIT_REASONING,
                    request=request,
                    stage="audit",
                    percent=audit_percent,
                    total_modules=total_modules,
                ):
                    if kind == "heartbeat":
                        yield result
                    else:
                        audit_text, models_used[f"audit_{repair_round + 1}"] = result
                if not audit_text:
                    return
                audit = _parse_json_object(audit_text, "报告审校")
                issues = audit.get("issues") if isinstance(audit.get("issues"), list) else []
                local_issues: list[str] = []
                for item in module_reports:
                    local_issues.extend(
                        _module_structure_issues(
                            item.get("report_md", ""),
                            item.get("title", ""),
                            valid_refs,
                        )
                    )
                audit["issues"] = issues
                audit["local_issues"] = local_issues
                audit["round"] = repair_round + 1
                sess["interview_audit"] = audit
                save_session(session_id, sess)
                if audit.get("ok") is True and not issues and not local_issues:
                    break
                logger.warning(
                    "interview audit needs attention session=%s round=%s "
                    "issue_count=%s local_issue_count=%s",
                    session_id,
                    repair_round + 1,
                    len(issues),
                    len(local_issues),
                )
                if repair_round >= INTERVIEW_MAX_REPAIR_ROUNDS:
                    if local_issues:
                        raise RuntimeError("报告经过自动修订后仍未通过结构与引用校验")
                    audit["status"] = "warning"
                    audit["repair_exhausted"] = True
                    sess["interview_audit"] = audit
                    save_session(session_id, sess)
                    break

                affected: dict[str, list[str]] = {}
                unmatched_problems: list[str] = []
                known_titles = {str(item.get("title") or "") for item in module_reports}
                for issue in issues:
                    if not isinstance(issue, dict):
                        continue
                    title = str(issue.get("module_title") or "").strip()
                    problem = str(issue.get("problem") or issue.get("suggestion") or "").strip()
                    if title in known_titles and problem:
                        affected.setdefault(title, []).append(problem)
                    elif problem:
                        unmatched_problems.append(problem)
                if unmatched_problems:
                    for item in module_reports:
                        affected.setdefault(item["title"], []).extend(unmatched_problems)
                if local_issues and not affected:
                    for item in module_reports:
                        affected.setdefault(item["title"], []).extend(local_issues)
                if not affected:
                    for item in module_reports:
                        affected.setdefault(item["title"], []).append("按审校意见加强证据链和需求逻辑")

                yield _progress_event(
                    stage="repair",
                    percent=min(94, audit_percent + 3),
                    message=f"审校发现 {len(issues) or len(local_issues)} 个问题，正在按模块修订",
                    total_modules=total_modules,
                )
                for index, item in enumerate(module_reports):
                    title = item["title"]
                    issue_list = affected.get(title)
                    if not issue_list:
                        continue
                    repaired_md = ""
                    async for kind, result in _collect_stage(
                        messages=_module_repair_messages(
                            item["report_md"],
                            modules[index],
                            players,
                            issue_list,
                        ),
                        model=INTERVIEW_REPAIR_MODEL,
                        reasoning=INTERVIEW_REPAIR_REASONING,
                        request=request,
                        stage="repair",
                        percent=min(94, audit_percent + 3),
                        module_index=index + 1,
                        total_modules=total_modules,
                        module_title=title,
                    ):
                        if kind == "heartbeat":
                            yield result
                        else:
                            repaired_md, models_used[
                                f"module_{index + 1}_audit_repair_{repair_round + 1}"
                            ] = result
                    if not repaired_md:
                        return
                    item["report_md"] = repaired_md.strip()
                    if _module_structure_issues(item["report_md"], title, valid_refs):
                        raise RuntimeError(f"模块“{title}”修订后仍未通过结构与引用检查")
                    sess["interview_module_reports"] = module_reports
                    save_session(session_id, sess)
                    yield sse_event(
                        {
                            "type": "interview_module_repaired",
                            "stage": "repair",
                            "percent": min(94, audit_percent + 3),
                            "message": f"已按审校意见修订模块：{title}",
                            "module_index": index + 1,
                            "total_modules": total_modules,
                            "module_title": title,
                            "module_md": item["report_md"],
                            "partial_report_md": _assemble_report(
                                sess,
                                extraction,
                                module_reports,
                            ),
                        }
                    )
                report_md = _assemble_report(sess, extraction, module_reports)

            invalid_refs = _invalid_report_ref_markers(report_md, valid_refs)
            if invalid_refs:
                raise RuntimeError("报告中仍有无法对应原始单元格的引用")
            audit_has_warnings = bool(audit.get("issues")) or audit.get("status") == "warning"
            writer_models = list(
                dict.fromkeys(
                    str(used_model)
                    for key, used_model in models_used.items()
                    if key.startswith("module_") and used_model
                )
            )
            sess.update(
                {
                    "interview_status": "completed",
                    "interview_stage": "completed",
                    "interview_progress": 100,
                    "interview_progress_message": (
                        "报告已完成并保存；自动审校仍有质量提醒"
                        if audit_has_warnings
                        else "报告已通过质量复审并保存"
                    ),
                    "interview_audit": audit,
                    "interview_models_used": models_used,
                    "report_writer_provider": "company_llm_gateway",
                    "report_writer_model": ",".join(writer_models) or INTERVIEW_REPORT_MODEL,
                    "report_md": report_md,
                }
            )
            save_session(session_id, sess)
            history_entry = save_to_history(session_id, sess)
            if history_entry:
                sess["interview_report_no"] = history_entry.get("report_no", "")
                save_session(session_id, sess)
            yield _progress_event(
                stage="completed",
                percent=100,
                message=sess["interview_progress_message"],
                total_modules=total_modules,
            )
            yield sse_event({"type": "interview_done", **_session_result(sess)})
        except Exception as exc:
            sess["interview_status"] = "failed"
            sess["interview_progress_message"] = str(exc)
            save_session(session_id, sess)
            logger.exception(
                "interview report failed session=%s stage=%s",
                session_id,
                sess.get("interview_stage", ""),
            )
            yield sse_event(
                {
                    "type": "error",
                    "message": str(exc),
                    "stage": sess.get("interview_stage", ""),
                    "percent": sess.get("interview_progress", 0),
                    "retryable": True,
                    "models_used": sess.get("interview_models_used") or {},
                }
            )
