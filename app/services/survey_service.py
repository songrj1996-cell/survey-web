"""services/survey_service:问卷分析全部业务编排。

包含:上传处理、列题型识别 SSE、方案生成 SSE、方案修订 SSE、统计计算、
报告生成 SSE（大样本/标准两路）、当前会话 QA SSE、历史报告 QA SSE。
HTTP 参数解析与响应包装在 routers/survey。
"""
import asyncio
from datetime import datetime

import survey_plan
import survey_stats


def is_survey_plan_approval(user_text: str) -> bool:
    """用户意见是否表示直接确认方案（不修订）。"""
    return survey_plan.is_user_approval(user_text)
from fastapi import HTTPException, Request

from app.core.config import (
    DIFY_ANALYST_KEY,
    DIFY_COLUMN_KEY,
    DIFY_CROSSTAB_PLANNER_KEY,
    DIFY_LARGE_ANALYST_KEY,
    DIFY_PLANNER_KEY,
    LARGE_SAMPLE_THRESHOLD,
)
from app.core.parsing import _parse_file
from app.core.responses import sse_event
from app.core.security import _assign_session_owner, _find_history_for_login
from app.core.text import _short_text
from app.integrations.dify_client import sse_dify_stream
from app.schemas.requests import QualitativeContextRequest
from app.services.audit import audit_log
from app.services.auth import _current_login
from app.services.branch_logic import infer_branch_rules
from app.services.question_detect import (
    _build_column_detect_query,
    _enrich_questions,
    _group_googleform_matrix,
    _heuristic_questions,
    _reconcile_question_roles,
    _sanitize_choice_options,
)
from app.services.report_engine import (
    _analyst_key_for_report,
    _batch_qualitative_analysis,
    _build_crosstab_plan_revision_query,
    _build_crosstab_planner_query,
    _build_large_sample_writer_query,
    _build_plan_revision_query,
    _build_planner_query_with_confirmed,
    _build_planner_sample,
    _build_writer_action_query,
    _build_writer_bug_query,
    _build_writer_core_query,
    _build_writer_first_query,
    _build_writer_part_query,
    _format_rows_for_qa,
    _render_crosstab_plan_card,
    _writer_parts_meta,
)
from app.services.report_history import save_to_history
from app.services.report_render import _inject_disclaimer
from app.storage.history import _load_history, _save_history
from app.storage.prompts import _get_planner_extra
from app.storage.sessions import get_session, new_session, save_session


# ── 上传 ────────────────────────────────────────────────────────


async def handle_survey_upload(filename: str, content: bytes, login: dict | None) -> dict:
    """解析上传文件，创建 session，返回前端所需的 result dict。"""
    try:
        rows = _parse_file(filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not rows:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(rows) <= 1:
        raise HTTPException(status_code=400, detail="文件只有表头没有数据行")

    sid = new_session()
    sess = get_session(sid)
    sess["rows"] = rows
    sess["filename"] = filename
    _assign_session_owner(sess, login)
    save_session(sid, sess)

    return {
        "session_id": sid,
        "filename": filename,
        "total_rows": len(rows) - 1,
        "headers": rows[0],
        "preview": rows[1: min(6, len(rows))],
    }


# ── 列题型识别 SSE ───────────────────────────────────────────────


async def columns_stream(session_id: str, request: Request):
    """LLM 列题型识别 SSE 流程（async generator）。"""
    sess = get_session(session_id)
    rows = sess.get("rows")
    try:
        groups = _group_googleform_matrix(rows[0])
        query = _build_column_detect_query(rows, groups)
        header_count = len(rows[0])

        answer_chunks: list[str] = []
        final_conv = ""
        async for chunk, conv_id in sse_dify_stream(query, session_id, "", DIFY_COLUMN_KEY):
            if chunk:
                answer_chunks.append(chunk)
                yield sse_event({"type": "chunk", "content": chunk})
            if conv_id:
                final_conv = conv_id

        questions, err = survey_plan.parse_columns_from_llm("".join(answer_chunks), header_count)

        if not questions:
            retry_q = (
                f"上次输出无法解析: {err}。请严格按 schema 用 ```json``` 围栏重新输出，"
                "不要附加任何解释文字。"
            )
            retry_chunks: list[str] = []
            async for chunk, conv_id in sse_dify_stream(retry_q, session_id, final_conv, DIFY_COLUMN_KEY):
                if chunk:
                    retry_chunks.append(chunk)
            questions, err = survey_plan.parse_columns_from_llm("".join(retry_chunks), header_count)

        if not questions:
            print(f"[columns] LLM 解析失败，回退本地启发式：{err}")
            questions = _heuristic_questions(rows, groups)
            yield sse_event({"type": "chunk", "content": "\n（题型识别解析失败，已回退本地推断，请仔细核对）\n"})

        questions = _enrich_questions(questions, rows[0], groups)
        questions = _reconcile_question_roles(rows, questions)
        questions = _sanitize_choice_options(rows, questions)
        if sess.get("mode") == "crosstab":
            hdrs = rows[0]
            for q in questions:
                idx = q.get("index")
                if isinstance(idx, int) and 0 <= idx < len(hdrs) \
                        and str(hdrs[idx]).strip().endswith("__open"):
                    q["role"] = "open_text"
        sess["columns_detected"] = questions
        save_session(session_id, sess)
        await audit_log(
            request, "survey", "识别题型",
            f"会话：{session_id}；识别列数：{len(questions)}",
            metadata={"session_id": session_id, "columns": len(questions)},
        )
        yield sse_event({"type": "columns_ready", "columns": questions})
    except Exception as e:
        import traceback; traceback.print_exc()
        yield sse_event({"type": "error", "message": str(e)})


# ── 列确认 ──────────────────────────────────────────────────────


def set_survey_columns(session_id: str, columns: list) -> None:
    """存储用户确认后的列题型配置。"""
    sess = get_session(session_id)
    sess["confirmed_columns"] = columns
    sess["branch_rules"] = infer_branch_rules(sess.get("rows") or [], columns)
    save_session(session_id, sess)


def _ensure_branch_rules(sess: dict) -> list[dict]:
    """兼容功能上线前已创建的 session，并确保 plan 始终携带确定性跳转关系。"""
    if sess.get("mode") == "crosstab":
        return []
    branch_rules = sess.get("branch_rules")
    if not isinstance(branch_rules, list):
        branch_rules = infer_branch_rules(
            sess.get("rows") or [],
            sess.get("confirmed_columns") or [],
        )
        sess["branch_rules"] = branch_rules
    plan = sess.get("plan")
    if isinstance(plan, dict):
        plan["branch_rules"] = branch_rules
    return branch_rules


def save_qualitative_context(session_id: str, ctx: QualitativeContextRequest) -> None:
    """存储用户可选填写的定性报告业务上下文。"""
    sess = get_session(session_id)
    sess["qualitative_context"] = ctx.model_dump() if hasattr(ctx, "model_dump") else ctx.dict()
    save_session(session_id, sess)


# ── 方案生成 SSE ────────────────────────────────────────────────


async def plan_stream(session_id: str, request: Request):
    """分析方案生成 SSE 流程（async generator）。"""
    sess = get_session(session_id)
    rows = sess.get("rows")
    confirmed_columns = sess.get("confirmed_columns")
    branch_rules = _ensure_branch_rules(sess)
    is_crosstab = sess.get("mode") == "crosstab"
    try:
        if is_crosstab:
            cols = confirmed_columns or []
            open_names = [c["name"] for c in cols if c.get("role") == "open_text"]
            avail = sess.get("crosstab_questions", [])
            query = _build_crosstab_planner_query(
                sess.get("questionnaire_text", ""), avail, open_names
            )
            ans_chunks: list[str] = []
            conv = ""
            async for chunk, cid in sse_dify_stream(query, session_id, "", DIFY_CROSSTAB_PLANNER_KEY):
                if chunk:
                    ans_chunks.append(chunk)
                    yield sse_event({"type": "chunk", "content": chunk})
                if cid:
                    conv = cid
            ctp, err = survey_plan.parse_crosstab_plan("".join(ans_chunks))
            if not ctp:
                yield sse_event({"type": "progress", "message": "方案格式校验中，正在修订输出…"})
                retry_q = (
                    f"上次输出无法解析: {err}。请只输出一个 JSON 对象"
                    "(含 parts 和 open_questions)，用 ```json``` 围栏，不要解释文字。"
                )
                retry_chunks: list[str] = []
                async for chunk, cid in sse_dify_stream(retry_q, session_id, conv, DIFY_CROSSTAB_PLANNER_KEY):
                    if chunk:
                        retry_chunks.append(chunk)
                    if cid:
                        conv = cid
                ctp, err = survey_plan.parse_crosstab_plan("".join(retry_chunks))
            if not ctp:
                yield sse_event({"type": "error", "message": f"章节大纲解析失败：{err}"}); return
            plan = {
                "mode": "crosstab",
                "columns": cols,
                "parts": ctp["parts"],
                "open_questions": ctp["open_questions"],
                "cross_tabs": [],
            }
            sess["plan"] = plan
            sess["planner_conv_id"] = conv
            save_session(session_id, sess)
            card_text = _render_crosstab_plan_card(plan)
            await audit_log(
                request, "survey", "生成章节大纲",
                f"会话：{session_id}；章节数：{len(plan['parts'])}",
                metadata={"session_id": session_id, "parts": len(plan["parts"]), "mode": "crosstab"},
            )
            yield sse_event({"type": "plan_ready", "plan": plan, "card_text": card_text, "headers": rows[0]})
            return

        if confirmed_columns:
            planner_query = _build_planner_query_with_confirmed(
                rows,
                confirmed_columns,
                branch_rules=branch_rules,
            )
        else:
            planner_query = _build_planner_sample(rows) + "\n\n" + _get_planner_extra()

        answer_chunks: list[str] = []
        final_conv_id = ""
        async for chunk, conv_id in sse_dify_stream(planner_query, session_id, "", DIFY_PLANNER_KEY):
            if chunk:
                answer_chunks.append(chunk)
                yield sse_event({"type": "chunk", "content": chunk})
            if conv_id:
                final_conv_id = conv_id

        full_answer = "".join(answer_chunks)
        headers = rows[0]
        plan, err = survey_plan.parse_plan_from_llm(full_answer, len(headers))

        if not plan:
            yield sse_event({"type": "progress", "message": "方案格式校验中，正在修订输出…"})
            retry_q = (
                f"上次输出无法解析: {err}。请严格按 JSON schema 重新输出，"
                "用 ```json ``` 围栏，不要附加解释文字。"
            )
            retry_chunks: list[str] = []
            async for chunk, conv_id in sse_dify_stream(retry_q, session_id, final_conv_id, DIFY_PLANNER_KEY):
                if chunk: retry_chunks.append(chunk)
                if conv_id: final_conv_id = conv_id
            plan, err = survey_plan.parse_plan_from_llm("".join(retry_chunks), len(headers))

        if not plan:
            yield sse_event({"type": "error", "message": f"方案解析失败：{err}"}); return

        if confirmed_columns:
            plan = survey_plan.merge_confirmed_into_plan(plan, confirmed_columns)
        plan["branch_rules"] = branch_rules

        sess["plan"] = plan
        sess["planner_conv_id"] = final_conv_id
        save_session(session_id, sess)
        card_text = survey_plan.render_plan_for_user(plan, headers)
        await audit_log(
            request, "survey", "生成分析方案",
            f"会话：{session_id}；Part 数：{len(plan.get('parts', []))}",
            metadata={"session_id": session_id, "parts": len(plan.get("parts", []))},
        )
        yield sse_event({"type": "plan_ready", "plan": plan, "card_text": card_text, "headers": headers})
    except Exception as e:
        import traceback; traceback.print_exc()
        yield sse_event({"type": "error", "message": str(e)})


# ── 方案修订 SSE ────────────────────────────────────────────────


async def plan_revision_stream(session_id: str, user_text: str, request: Request):
    """方案修订 SSE 流程（async generator）。"""
    sess = get_session(session_id)
    branch_rules = _ensure_branch_rules(sess)
    plan = sess.get("plan")
    rows = sess.get("rows")
    planner_conv_id = sess.get("planner_conv_id", "")
    try:
        if sess.get("mode") == "crosstab":
            conv = planner_conv_id
            rev_q = _build_crosstab_plan_revision_query(
                sess.get("questionnaire_text", ""), plan.get("parts", []), user_text
            )
            rchunks: list[str] = []
            async for chunk, cid in sse_dify_stream(rev_q, session_id, "", DIFY_CROSSTAB_PLANNER_KEY):
                if chunk:
                    rchunks.append(chunk)
                    yield sse_event({"type": "chunk", "content": chunk})
                if cid:
                    conv = cid
            ctp, err = survey_plan.parse_crosstab_plan("".join(rchunks))
            if not ctp:
                yield sse_event({"type": "error", "message": f"修订章节大纲解析失败：{err}"}); return
            new_plan = dict(plan)
            new_plan["parts"] = ctp["parts"]
            new_plan["open_questions"] = ctp["open_questions"]
            sess["plan"] = new_plan
            sess["planner_conv_id"] = conv
            save_session(session_id, sess)
            card_text = _render_crosstab_plan_card(new_plan)
            await audit_log(
                request, "survey", "修订章节大纲",
                f"会话：{session_id}；修改意见：{_short_text(user_text)}",
                metadata={"session_id": session_id, "mode": "crosstab"},
            )
            yield sse_event({"type": "plan_ready", "plan": new_plan, "card_text": card_text, "headers": rows[0]})
            return

        new_conv_id = planner_conv_id
        answer_chunks: list[str] = []
        headers = rows[0]
        revision_query = _build_plan_revision_query(
            plan,
            headers,
            sess.get("confirmed_columns", []),
            user_text,
            branch_rules=branch_rules,
        )
        async for chunk, conv_id in sse_dify_stream(revision_query, session_id, "", DIFY_PLANNER_KEY):
            if chunk:
                answer_chunks.append(chunk)
                yield sse_event({"type": "chunk", "content": chunk})
            if conv_id:
                new_conv_id = conv_id

        full_answer = "".join(answer_chunks)
        new_plan, err = survey_plan.parse_plan_from_llm(full_answer, len(headers))
        if not new_plan:
            retry_query = (
                f"{revision_query}\n\n"
                "上一次输出无法解析为 JSON。请修正并只返回完整 JSON 对象。\n"
                f"解析错误：{err}\n"
                f"<previous_output>\n{full_answer[:4000]}\n</previous_output>"
            )
            retry_chunks: list[str] = []
            retry_conv_id = new_conv_id
            yield sse_event({"type": "progress", "message": "方案格式校验中，正在修订输出…"})
            yield sse_event({"type": "chunk", "content": "\n\n正在按严格 JSON 格式重新修订方案...\n"})
            async for chunk, conv_id in sse_dify_stream(retry_query, session_id, "", DIFY_PLANNER_KEY):
                if chunk:
                    retry_chunks.append(chunk)
                    yield sse_event({"type": "chunk", "content": chunk})
                if conv_id:
                    retry_conv_id = conv_id
            new_plan, err = survey_plan.parse_plan_from_llm("".join(retry_chunks), len(headers))
            if new_plan:
                new_conv_id = retry_conv_id
        if not new_plan:
            yield sse_event({"type": "error", "message": f"修订方案解析失败：{err}"}); return

        if sess.get("confirmed_columns"):
            new_plan = survey_plan.merge_confirmed_into_plan(new_plan, sess["confirmed_columns"])
        new_plan["branch_rules"] = branch_rules

        sess["plan"] = new_plan
        sess["planner_conv_id"] = new_conv_id
        save_session(session_id, sess)
        card_text = survey_plan.render_plan_for_user(new_plan, headers)
        await audit_log(
            request, "survey", "修订分析方案",
            f"会话：{session_id}；修改意见：{_short_text(user_text)}",
            metadata={"session_id": session_id},
        )
        yield sse_event({"type": "plan_ready", "plan": new_plan, "card_text": card_text, "headers": headers})
    except Exception as e:
        import traceback; traceback.print_exc()
        yield sse_event({"type": "error", "message": str(e)})


# ── 统计计算 ────────────────────────────────────────────────────


async def compute_survey_stats(session_id: str, request: Request) -> str:
    """计算统计数据，写入 session，返回 stats_md。"""
    sess = get_session(session_id)
    _ensure_branch_rules(sess)
    plan = sess.get("plan")
    rows = sess.get("rows")
    if not plan or not rows:
        raise HTTPException(status_code=400, detail="会话状态丢失")
    loop = asyncio.get_event_loop()
    if sess.get("mode") == "crosstab":
        stats_md = sess.get("crosstab_md", "")
        open_text = await loop.run_in_executor(None, survey_stats.collect_open_text, rows, plan)
    else:
        stats_md, open_text = await loop.run_in_executor(None, survey_stats.compute, rows, plan)
    sess["stats_md"] = stats_md
    sess["open_text"] = open_text
    sess["rows_fed"] = False
    save_session(session_id, sess)
    await audit_log(
        request, "survey", "计算统计",
        f"会话：{session_id}；样本行数：{max(0, len(rows) - 1)}",
        metadata={"session_id": session_id, "rows": max(0, len(rows) - 1)},
    )
    return stats_md


# ── 报告生成 SSE ────────────────────────────────────────────────


async def report_stream(session_id: str, request: Request):
    """报告生成 SSE 流程（大样本/标准两路，async generator）。"""
    sess = get_session(session_id)
    _assign_session_owner(sess, await _current_login(request))
    _ensure_branch_rules(sess)
    plan = sess.get("plan")
    rows = sess.get("rows")
    stats_md = sess.get("stats_md")
    open_text = sess.get("open_text", {})
    is_crosstab = sess.get("mode") == "crosstab"
    qualitative_context = None if is_crosstab else sess.get("qualitative_context")
    use_large_mode = is_crosstab or any(len(v) > LARGE_SAMPLE_THRESHOLD for v in open_text.values())

    try:
        if use_large_mode:
            total_open_text = sum(len(v) for v in open_text.values())
            start_msg = (
                f"跑数表模式：数字取自跑数表，开始对 {total_open_text} 条主观题回复做聚类"
                if is_crosstab
                else "检测到超过500条回复，启用批量处理模式"
            )
            yield sse_event({"type": "progress", "message": start_msg})
            clustered_themes: dict = {}
            cluster_diagnostics: dict = {}
            async for item in _batch_qualitative_analysis(open_text, plan, rows[0], session_id):
                if item[0] == "progress":
                    yield sse_event({"type": "progress", "message": item[1]})
                elif item[0] == "diagnostics":
                    cluster_diagnostics = item[1]
                elif item[0] == "result":
                    clustered_themes = item[1]

            failed_cols = [
                d.get("col_name", f"列{k}") for k, d in (cluster_diagnostics or {}).items()
                if d.get("status") != "ok"
            ]
            sess["open_text_cluster_diagnostics"] = cluster_diagnostics
            save_session(session_id, sess)
            if failed_cols:
                msg = "部分主观题聚类未完成，报告将使用原文兜底：" + "、".join(failed_cols[:4])
                if len(failed_cols) > 4:
                    msg += f"等 {len(failed_cols)} 列"
                yield sse_event({"type": "progress", "message": msg})

            yield sse_event({"type": "progress", "message": "主题分析完成，开始生成报告..."})
            writer_query = _build_large_sample_writer_query(
                stats_md, clustered_themes, plan, rows[0], open_text,
                qualitative_context=qualitative_context,
            )
            if is_crosstab:
                q_text = (sess.get("questionnaire_text") or "").strip()
                if q_text:
                    if len(q_text) > 8000:
                        q_text = q_text[:8000] + "\n…（问卷过长，已截断）"
                    writer_query = (
                        f"<questionnaire>\n以下是问卷原文（仅供理解题目意图与背景，"
                        f"不要直接搬运）：\n{q_text}\n</questionnaire>\n\n" + writer_query
                    )
            analyst_key = DIFY_LARGE_ANALYST_KEY
            answer_chunks: list[str] = []
            final_conv_id = ""
            async for chunk, conv_id in sse_dify_stream(
                writer_query, session_id, "", analyst_key, max_attempts=4
            ):
                if chunk:
                    answer_chunks.append(chunk)
                    yield sse_event({"type": "chunk", "content": chunk})
                if conv_id:
                    final_conv_id = conv_id
            full_report = "".join(answer_chunks)
        else:
            analyst_key = DIFY_ANALYST_KEY
            parts_meta = _writer_parts_meta(plan, rows[0])
            final_conv_id = ""

            async def _round(query: str, conv_id: str):
                buf: list[str] = []
                cid = conv_id
                async for ch, c in sse_dify_stream(
                    query, session_id, conv_id, analyst_key, max_attempts=4
                ):
                    if ch:
                        buf.append(ch)
                        yield sse_event({"type": "chunk", "content": ch})
                    if c:
                        cid = c
                _round.out = ("".join(buf), cid)

            total_rounds = len(parts_meta) + 4
            yield sse_event({"type": "progress",
                             "message": f"分章生成 1/{total_rounds}：准备数据并生成标题…"})
            first_q = _build_writer_first_query(
                stats_md, open_text, plan, rows[0], qualitative_context=qualitative_context
            )
            async for ev in _round(first_q, ""):
                yield ev
            title_text, final_conv_id = _round.out
            title_lines = []
            for ln in title_text.split("\n"):
                if ln.lstrip().startswith("## "):
                    break
                title_lines.append(ln)
            title_block = "\n".join(title_lines).strip() or title_text.strip()

            part_sections: list[str] = []
            for m in parts_meta:
                rnd = m["i"] + 1
                yield sse_event({"type": "progress",
                                 "message": f"分章生成 {rnd}/{total_rounds}：Part {m['i']} {m['name']}…"})
                yield sse_event({"type": "chunk", "content": "\n\n"})
                async for ev in _round(_build_writer_part_query(m), final_conv_id):
                    yield ev
                sec, final_conv_id = _round.out
                part_sections.append(sec.strip())

            yield sse_event({"type": "progress",
                             "message": f"分章生成 {total_rounds - 2}/{total_rounds}：核查待确认问题…"})
            async for ev in _round(_build_writer_bug_query(), final_conv_id):
                yield ev
            bug_text, final_conv_id = _round.out
            bug_clean = bug_text.strip()
            has_bug = bool(bug_clean) and bug_clean.upper().strip(" .。`*") != "NONE" and "## Bug" in bug_clean
            bug_section = bug_clean if has_bug else ""

            yield sse_event({"type": "progress",
                             "message": f"分章生成 {total_rounds - 1}/{total_rounds}：汇总核心结论…"})
            yield sse_event({"type": "chunk", "content": "\n\n"})
            async for ev in _round(_build_writer_core_query(parts_meta, has_bug, qualitative_context), final_conv_id):
                yield ev
            core_text, final_conv_id = _round.out
            core_block = core_text.strip()

            yield sse_event({"type": "progress",
                             "message": f"分章生成 {total_rounds}/{total_rounds}：生成行动建议…"})
            yield sse_event({"type": "chunk", "content": "\n\n"})
            async for ev in _round(_build_writer_action_query(parts_meta, has_bug, qualitative_context), final_conv_id):
                yield ev
            action_text, final_conv_id = _round.out
            action_clean = action_text.strip()
            has_action = bool(action_clean) and "## 行动建议" in action_clean
            action_section = action_clean if has_action else ""

            details_divider = "---------------- 以下为详细信息，各位可以按需查看 ----------------"
            assembled = [title_block, core_block, details_divider, *part_sections]
            if bug_section:
                assembled.append(bug_section)
            if action_section:
                assembled.append(action_section)
            full_report = "\n\n".join(b for b in assembled if b)

        drifted = survey_stats.find_numbers_not_in_stats(full_report, stats_md)
        if drifted:
            print(f"[stats] WARN drifted numbers: {drifted[:20]}")

        full_report = _inject_disclaimer(full_report, mode=sess.get("mode") or "")
        sess["report_md"] = full_report
        sess["analyst_conv_id"] = final_conv_id
        sess["analyst_app"] = "large" if use_large_mode else "standard"
        sess["rows_fed"] = False
        save_session(session_id, sess)
        save_to_history(session_id, sess)
        await audit_log(
            request, "survey", "生成报告",
            f"会话：{session_id}；文件：{sess.get('filename', 'unknown')}；模式：{'大样本' if use_large_mode else '标准'}",
            metadata={"session_id": session_id, "filename": sess.get("filename", "unknown"),
                      "large_mode": use_large_mode},
        )
        yield sse_event({"type": "report_done", "report_md": full_report})
    except Exception as e:
        import traceback; traceback.print_exc()
        yield sse_event({"type": "error", "message": str(e)})


# ── 当前会话 QA SSE ─────────────────────────────────────────────


async def qa_stream(session_id: str, question: str, request: Request):
    """当前会话 QA SSE 流程（async generator）。"""
    sess = get_session(session_id)
    _assign_session_owner(sess, await _current_login(request))
    analyst_conv_id = sess.get("analyst_conv_id", "")
    rows = sess.get("rows", [])
    plan = sess.get("plan", {})
    rows_fed = sess.get("rows_fed", False)
    analyst_key, analyst_key_name = _analyst_key_for_report(sess)
    try:
        if not rows_fed and rows:
            rows_block = _format_rows_for_qa(rows, plan)
            qa_query = f"<rows>\n{rows_block}\n</rows>\n\n用户问题: {question}"
        else:
            qa_query = question

        answer_chunks: list[str] = []
        new_conv_id = analyst_conv_id
        async for chunk, conv_id in sse_dify_stream(qa_query, session_id, analyst_conv_id, analyst_key):
            if chunk:
                answer_chunks.append(chunk)
                yield sse_event({"type": "chunk", "content": chunk})
            if conv_id:
                new_conv_id = conv_id

        answer_text = "".join(answer_chunks)
        sess["analyst_conv_id"] = new_conv_id or analyst_conv_id
        sess["analyst_app"] = "large" if analyst_key_name == "DIFY_LARGE_ANALYST_KEY" else "standard"
        sess["rows_fed"] = True
        sess.setdefault("qa_messages", []).extend([
            {"role": "user", "content": question, "ts": datetime.now().isoformat()},
            {"role": "ai", "content": answer_text, "ts": datetime.now().isoformat()},
        ])
        save_session(session_id, sess)
        save_to_history(session_id, sess)
        await audit_log(
            request, "report", "追问当前报告",
            f"会话：{session_id}；问题：{_short_text(question)}",
            metadata={"session_id": session_id},
        )
        yield sse_event({"type": "qa_done", "answer": answer_text})
    except Exception as e:
        import traceback; traceback.print_exc()
        yield sse_event({"type": "error", "message": str(e)})


# ── 历史报告 QA SSE ─────────────────────────────────────────────


async def history_qa_stream(
    history_id: str,
    question: str,
    history: list,
    analyst_conv_id: str,
    analyst_key: str,
    analyst_key_name: str,
    request: Request,
):
    """历史报告续聊 QA SSE 流程（async generator）。"""
    try:
        answer_chunks: list[str] = []
        new_conv_id = analyst_conv_id
        async for chunk, conv_id in sse_dify_stream(question, history_id, analyst_conv_id, analyst_key):
            if chunk:
                answer_chunks.append(chunk)
                yield sse_event({"type": "chunk", "content": chunk})
            if conv_id:
                new_conv_id = conv_id

        answer_text = "".join(answer_chunks)
        for h in history:
            if h["id"] == history_id:
                h["analyst_conv_id"] = new_conv_id or analyst_conv_id
                h["analyst_app"] = "large" if analyst_key_name == "DIFY_LARGE_ANALYST_KEY" else "standard"
                h.setdefault("qa_messages", []).extend([
                    {"role": "user", "content": question, "ts": datetime.now().isoformat()},
                    {"role": "ai", "content": answer_text, "ts": datetime.now().isoformat()},
                ])
                break
        _save_history(history)
        await audit_log(
            request, "report", "追问历史报告",
            f"历史报告：{history_id}；问题：{_short_text(question)}",
            metadata={"history_id": history_id},
        )
        yield sse_event({"type": "qa_done", "answer": answer_text})
    except Exception as e:
        import traceback; traceback.print_exc()
        yield sse_event({"type": "error", "message": str(e)})


# ── Router 前置校验函数 ──────────────────────────────────────────


def validate_columns_ready(session_id: str) -> None:
    """校验列识别前置条件（rows 存在），不满足则 raise HTTPException。"""
    sess = get_session(session_id)
    if not sess.get("rows"):
        raise HTTPException(status_code=400, detail="会话中没有数据")


def validate_plan_ready(session_id: str) -> str:
    """校验方案生成前置条件，返回 mode 供 router 选择正确的 planner key。"""
    sess = get_session(session_id)
    if not sess.get("rows"):
        raise HTTPException(status_code=400, detail="会话中没有数据，请先上传文件")
    return sess.get("mode", "")


def validate_plan_confirm_ready(session_id: str) -> None:
    """校验方案确认/修订前置条件，不满足则 raise HTTPException。"""
    sess = get_session(session_id)
    if not sess.get("plan") or not sess.get("rows"):
        raise HTTPException(status_code=400, detail="会话状态丢失，请重新上传文件")


def validate_report_ready(session_id: str) -> bool:
    """校验报告生成前置条件，返回 use_large_mode 供 router 选择正确的 analyst key。"""
    sess = get_session(session_id)
    if not all([sess.get("plan"), sess.get("rows"), sess.get("stats_md")]):
        raise HTTPException(status_code=400, detail="请先完成统计计算")
    return sess.get("mode") == "crosstab" or any(
        len(v) > LARGE_SAMPLE_THRESHOLD for v in sess.get("open_text", {}).values()
    )


def validate_qa_ready(session_id: str) -> None:
    """校验 QA 前置条件（analyst_conv_id + analyst_key），不满足则 raise HTTPException。"""
    sess = get_session(session_id)
    if not sess.get("analyst_conv_id"):
        raise HTTPException(status_code=400, detail="请先生成报告")
    analyst_key, analyst_key_name = _analyst_key_for_report(sess)
    if not analyst_key:
        raise HTTPException(status_code=500, detail=f"未配置 {analyst_key_name}")


def prepare_history_qa_context(
    history_id: str, login: dict | None
) -> tuple[list, str, str, str]:
    """加载历史记录，校验续聊前置条件。
    返回 (history, analyst_conv_id, analyst_key, analyst_key_name)。
    """
    history = _load_history()
    entry = _find_history_for_login(history, history_id, login)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    analyst_conv_id = entry.get("analyst_conv_id", "")
    if not analyst_conv_id:
        raise HTTPException(status_code=400, detail="该历史记录没有可续聊的对话")
    analyst_key, analyst_key_name = _analyst_key_for_report(entry)
    if not analyst_key:
        raise HTTPException(status_code=500, detail=f"未配置 {analyst_key_name}")
    return history, analyst_conv_id, analyst_key, analyst_key_name
