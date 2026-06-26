"""routers/survey:问卷分析主流程(上传/题型/方案/统计/报告/问答)。

跑数表(crosstab)模式复用本组的 plan/stats/report/qa 流程,仅上传入口在 routers/crosstab。
"""
import asyncio
from datetime import datetime

import survey_plan
import survey_stats
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.audit import audit_log
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
from app.core.security import _assign_session_owner, _current_login, _find_history_for_login
from app.core.text import _short_text
from app.integrations.dify_client import sse_dify_stream
from app.schemas.requests import (
    ColumnConfirmRequest,
    HistoryQARequest,
    PlanConfirmRequest,
    QARequest,
)
from app.services.question_detect import (
    _build_column_detect_query,
    _enrich_questions,
    _group_googleform_matrix,
    _heuristic_questions,
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

router = APIRouter()


@router.post("/api/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    content = await file.read()
    try:
        rows = _parse_file(file.filename or "upload.csv", content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not rows:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(rows) <= 1:
        raise HTTPException(status_code=400, detail="文件只有表头没有数据行")

    sid = new_session()
    sess = get_session(sid)
    sess["rows"] = rows
    sess["filename"] = file.filename or "upload.csv"
    _assign_session_owner(sess, await _current_login(request))
    save_session(sid, sess)

    result = {
        "session_id": sid,
        "filename": file.filename,
        "total_rows": len(rows) - 1,
        "headers": rows[0],
        "preview": rows[1: min(6, len(rows))],
    }
    await audit_log(
        request,
        "survey",
        "上传数据",
        f"文件：{file.filename or 'unknown'}；样本行数：{len(rows) - 1}",
        metadata={"session_id": sid, "rows": len(rows) - 1},
    )
    return result


@router.get("/api/columns/{session_id}")
async def get_columns(session_id: str, request: Request):
    """LLM 识别列题型（含 Google Form 矩阵题分组、中文题名、多选选项清单）。

    流式返回；最终发 `columns_ready`，columns 为「逻辑题」列表（矩阵题跨多列）。
    LLM 解析失败时回退本地启发式。
    """
    sess = get_session(session_id)
    rows = sess.get("rows")
    if not rows:
        raise HTTPException(status_code=400, detail="会话中没有数据")
    if not DIFY_COLUMN_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_COLUMN_KEY（题型识别应用）")

    async def generate():
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
            questions = _sanitize_choice_options(rows, questions)
            # 跑数表模式安全网：倍市得清数中以 __open 结尾的列必是开放题，
            # 强制 role=open_text，避免 AI 误判导致主观题漏聚类。
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
                request,
                "survey",
                "识别题型",
                f"会话：{session_id}；识别列数：{len(questions)}",
                metadata={"session_id": session_id, "columns": len(questions)},
            )
            yield sse_event({"type": "columns_ready", "columns": questions})
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/api/columns/{session_id}/confirm")
async def confirm_columns(session_id: str, req: ColumnConfirmRequest, request: Request):
    """存储用户确认（或修改）后的列题型。"""
    sess = get_session(session_id)
    sess["confirmed_columns"] = req.columns
    save_session(session_id, sess)
    await audit_log(
        request,
        "survey",
        "确认数据列",
        f"会话：{session_id}；确认列数：{len(req.columns)}",
        metadata={"session_id": session_id, "columns": len(req.columns)},
    )
    return {"ok": True}


@router.get("/api/plan/{session_id}")
async def get_plan(session_id: str, request: Request):
    sess = get_session(session_id)
    rows = sess.get("rows")
    confirmed_columns = sess.get("confirmed_columns")

    is_crosstab = sess.get("mode") == "crosstab"

    if not rows:
        raise HTTPException(status_code=400, detail="会话中没有数据，请先上传文件")
    if is_crosstab:
        if not DIFY_CROSSTAB_PLANNER_KEY:
            raise HTTPException(status_code=500, detail="未配置 DIFY_CROSSTAB_PLANNER_KEY")
    elif not DIFY_PLANNER_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_PLANNER_KEY")

    async def generate():
        try:
            if is_crosstab:
                # ── 跑数表模式：AI 读问卷原文规划章节大纲 ──────────────────
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
                planner_query = _build_planner_query_with_confirmed(rows, confirmed_columns)
            else:
                planner_query = (
                    _build_planner_sample(rows)
                    + "\n\n" + _get_planner_extra()
                )

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

            # 用用户确认的题型覆盖 planner 的列分类（权威），并修补 parts（矩阵题整体入同一 part）
            if confirmed_columns:
                plan = survey_plan.merge_confirmed_into_plan(plan, confirmed_columns)

            sess["plan"] = plan
            sess["planner_conv_id"] = final_conv_id
            save_session(session_id, sess)
            card_text = survey_plan.render_plan_for_user(plan, headers)
            await audit_log(
                request,
                "survey",
                "生成分析方案",
                f"会话：{session_id}；Part 数：{len(plan.get('parts', []))}",
                metadata={"session_id": session_id, "parts": len(plan.get("parts", []))},
            )
            yield sse_event({"type": "plan_ready", "plan": plan, "card_text": card_text, "headers": headers})

        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/api/plan/confirm")
async def confirm_plan(req: PlanConfirmRequest, request: Request):
    sess = get_session(req.session_id)
    plan = sess.get("plan")
    rows = sess.get("rows")
    planner_conv_id = sess.get("planner_conv_id", "")

    if not plan or not rows:
        raise HTTPException(status_code=400, detail="会话状态丢失，请重新上传文件")

    if survey_plan.is_user_approval(req.user_text):
        await audit_log(
            request,
            "survey",
            "确认分析方案",
            f"会话：{req.session_id}",
            metadata={"session_id": req.session_id},
        )
        return JSONResponse({"approved": True})

    async def generate():
        try:
            # ── 跑数表模式：章节大纲修订 ──────────────────────────────
            if sess.get("mode") == "crosstab":
                conv = planner_conv_id
                rev_q = _build_crosstab_plan_revision_query(
                    sess.get("questionnaire_text", ""), plan.get("parts", []), req.user_text
                )
                rchunks: list[str] = []
                async for chunk, cid in sse_dify_stream(rev_q, req.session_id, "", DIFY_CROSSTAB_PLANNER_KEY):
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
                save_session(req.session_id, sess)
                card_text = _render_crosstab_plan_card(new_plan)
                await audit_log(
                    request, "survey", "修订章节大纲",
                    f"会话：{req.session_id}；修改意见：{_short_text(req.user_text)}",
                    metadata={"session_id": req.session_id, "mode": "crosstab"},
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
                req.user_text,
            )
            async for chunk, conv_id in sse_dify_stream(
                revision_query, req.session_id, "", DIFY_PLANNER_KEY
            ):
                if chunk:
                    answer_chunks.append(chunk)
                    yield sse_event({"type": "chunk", "content": chunk})
                if conv_id:
                    new_conv_id = conv_id

            full_answer = "".join(answer_chunks)
            new_plan, err = survey_plan.parse_plan_from_llm(
                full_answer,
                len(headers)
            )
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
                async for chunk, conv_id in sse_dify_stream(
                    retry_query, req.session_id, "", DIFY_PLANNER_KEY
                ):
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

            sess["plan"] = new_plan
            sess["planner_conv_id"] = new_conv_id
            save_session(req.session_id, sess)
            card_text = survey_plan.render_plan_for_user(new_plan, headers)
            await audit_log(
                request,
                "survey",
                "修订分析方案",
                f"会话：{req.session_id}；修改意见：{_short_text(req.user_text)}",
                metadata={"session_id": req.session_id},
            )
            yield sse_event({"type": "plan_ready", "plan": new_plan, "card_text": card_text, "headers": headers})
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/api/stats/{session_id}")
async def compute_stats(session_id: str, request: Request):
    sess = get_session(session_id)
    plan = sess.get("plan")
    rows = sess.get("rows")
    if not plan or not rows:
        raise HTTPException(status_code=400, detail="会话状态丢失")
    loop = asyncio.get_event_loop()
    if sess.get("mode") == "crosstab":
        # 跑数表模式：数字直接取自跑数表，平台不自算统计；只收集开放题原文供聚类。
        stats_md = sess.get("crosstab_md", "")
        open_text = await loop.run_in_executor(
            None, survey_stats.collect_open_text, rows, plan
        )
    else:
        stats_md, open_text = await loop.run_in_executor(
            None, survey_stats.compute, rows, plan
        )
    sess["stats_md"] = stats_md
    sess["open_text"] = open_text
    sess["rows_fed"] = False
    save_session(session_id, sess)
    await audit_log(
        request,
        "survey",
        "计算统计",
        f"会话：{session_id}；样本行数：{max(0, len(rows) - 1)}",
        metadata={"session_id": session_id, "rows": max(0, len(rows) - 1)},
    )
    return {"stats_md": stats_md}


@router.get("/api/report/{session_id}")
async def generate_report(session_id: str, request: Request):
    sess = get_session(session_id)
    _assign_session_owner(sess, await _current_login(request))
    plan = sess.get("plan")
    rows = sess.get("rows")
    stats_md = sess.get("stats_md")
    open_text = sess.get("open_text", {})

    if not all([plan, rows, stats_md]):
        raise HTTPException(status_code=400, detail="请先完成统计计算")

    is_crosstab = sess.get("mode") == "crosstab"
    # 跑数表模式恒定走聚类；定性模式以单列最大回答数判断（避免多开放题列加总误触发）
    use_large_mode = is_crosstab or any(len(v) > LARGE_SAMPLE_THRESHOLD for v in open_text.values())

    if use_large_mode:
        if not DIFY_LARGE_ANALYST_KEY:
            raise HTTPException(status_code=500, detail="未配置 DIFY_LARGE_ANALYST_KEY")
    else:
        if not DIFY_ANALYST_KEY:
            raise HTTPException(status_code=500, detail="未配置 DIFY_ANALYST_KEY")

    async def generate():
        try:
            if use_large_mode:
                # ── 大样本 / 跑数表模式：主观题四阶段批处理聚类 ──────────────
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
                writer_query = _build_large_sample_writer_query(stats_md, clustered_themes, plan, rows[0], open_text)
                # 跑数表模式：把问卷原文作为题目意图上下文一并喂给 Writer
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
                async for chunk, conv_id in sse_dify_stream(writer_query, session_id, "", analyst_key):
                    if chunk:
                        answer_chunks.append(chunk)
                        yield sse_event({"type": "chunk", "content": chunk})
                    if conv_id:
                        final_conv_id = conv_id
                full_report = "".join(answer_chunks)
            else:
                # ── 标准模式：同会话分章多轮生成（规避 Dify 单次 10 分钟超时）──
                # 第 1 轮喂全部原文+要求、只写标题并建立会话；之后复用 conversation_id，
                # 每轮只写一个 Part / Bug 模块 / 核心结论，原文不再重发。每轮输出短，单次不超时。
                analyst_key = DIFY_ANALYST_KEY
                parts_meta = _writer_parts_meta(plan, rows[0])
                final_conv_id = ""

                async def _round(query: str, conv_id: str):
                    """跑一轮：流式 yield chunk 事件，结束后把整段文本与 conv_id 存入 _round.out。"""
                    buf: list[str] = []
                    cid = conv_id
                    async for ch, c in sse_dify_stream(query, session_id, conv_id, analyst_key):
                        if ch:
                            buf.append(ch)
                            yield sse_event({"type": "chunk", "content": ch})
                        if c:
                            cid = c
                    _round.out = ("".join(buf), cid)

                total_rounds = len(parts_meta) + 3  # 标题 + N 个 Part + Bug + 核心结论

                # 第 1 轮：全部上下文 + 只写标题
                yield sse_event({"type": "progress",
                                 "message": f"分章生成 1/{total_rounds}：准备数据并生成标题…"})
                first_q = _build_writer_first_query(stats_md, open_text, plan, rows[0])
                async for ev in _round(first_q, ""):
                    yield ev
                title_text, final_conv_id = _round.out
                # 只保留标题段（首个 `## ` 之前），防止模型抢跑后续章节
                title_lines = []
                for ln in title_text.split("\n"):
                    if ln.lstrip().startswith("## "):
                        break
                    title_lines.append(ln)
                title_block = "\n".join(title_lines).strip() or title_text.strip()

                # 第 2..N+1 轮：逐 Part
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

                # 倒数第 2 轮：Bug 模块（需要才写，否则模型回 NONE）
                yield sse_event({"type": "progress",
                                 "message": f"分章生成 {total_rounds - 1}/{total_rounds}：核查待确认问题…"})
                async for ev in _round(_build_writer_bug_query(), final_conv_id):
                    yield ev
                bug_text, final_conv_id = _round.out
                bug_clean = bug_text.strip()
                has_bug = bool(bug_clean) and bug_clean.upper().strip(" .。`*") != "NONE" and "## Bug" in bug_clean
                bug_section = bug_clean if has_bug else ""

                # 最后一轮：核心结论（汇总全部章节，放报告顶部）
                yield sse_event({"type": "progress",
                                 "message": f"分章生成 {total_rounds}/{total_rounds}：汇总核心结论…"})
                yield sse_event({"type": "chunk", "content": "\n\n"})
                async for ev in _round(_build_writer_core_query(parts_meta, has_bug), final_conv_id):
                    yield ev
                core_text, final_conv_id = _round.out
                core_block = core_text.strip()

                # 组装最终报告：标题 → 核心结论 → 各 Part → Bug 模块
                assembled = [title_block, core_block, *part_sections]
                if bug_section:
                    assembled.append(bug_section)
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
                request,
                "survey",
                "生成报告",
                f"会话：{session_id}；文件：{sess.get('filename', 'unknown')}；模式：{'大样本' if use_large_mode else '标准'}",
                metadata={"session_id": session_id, "filename": sess.get("filename", "unknown"), "large_mode": use_large_mode},
            )

            yield sse_event({"type": "report_done", "report_md": full_report})
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/api/qa")
async def qa(req: QARequest, request: Request):
    sess = get_session(req.session_id)
    _assign_session_owner(sess, await _current_login(request))
    analyst_conv_id = sess.get("analyst_conv_id", "")
    rows = sess.get("rows", [])
    plan = sess.get("plan", {})
    rows_fed = sess.get("rows_fed", False)

    if not analyst_conv_id:
        raise HTTPException(status_code=400, detail="请先生成报告")
    analyst_key, analyst_key_name = _analyst_key_for_report(sess)
    if not analyst_key:
        raise HTTPException(status_code=500, detail=f"未配置 {analyst_key_name}")

    async def generate():
        try:
            if not rows_fed and rows:
                rows_block = _format_rows_for_qa(rows, plan)
                qa_query = f"<rows>\n{rows_block}\n</rows>\n\n用户问题: {req.question}"
            else:
                qa_query = req.question

            answer_chunks: list[str] = []
            new_conv_id = analyst_conv_id

            async for chunk, conv_id in sse_dify_stream(
                qa_query, req.session_id, analyst_conv_id, analyst_key
            ):
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
                {"role": "user", "content": req.question, "ts": datetime.now().isoformat()},
                {"role": "ai", "content": answer_text, "ts": datetime.now().isoformat()},
            ])
            save_session(req.session_id, sess)
            save_to_history(req.session_id, sess)
            await audit_log(
                request,
                "report",
                "追问当前报告",
                f"会话：{req.session_id}；问题：{_short_text(req.question)}",
                metadata={"session_id": req.session_id},
            )
            yield sse_event({"type": "qa_done", "answer": answer_text})
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/api/history-qa")
async def history_qa(req: HistoryQARequest, request: Request):
    """从历史记录中续聊 QA（无行数据，直接使用 analyst conv_id）。"""
    login = await _current_login(request)
    history = _load_history()
    entry = _find_history_for_login(history, req.history_id, login)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")

    analyst_conv_id = entry.get("analyst_conv_id", "")
    if not analyst_conv_id:
        raise HTTPException(status_code=400, detail="该历史记录没有可续聊的对话")
    analyst_key, analyst_key_name = _analyst_key_for_report(entry)
    if not analyst_key:
        raise HTTPException(status_code=500, detail=f"未配置 {analyst_key_name}")

    async def generate():
        try:
            answer_chunks: list[str] = []
            new_conv_id = analyst_conv_id
            async for chunk, conv_id in sse_dify_stream(
                req.question, req.history_id, analyst_conv_id, analyst_key
            ):
                if chunk:
                    answer_chunks.append(chunk)
                    yield sse_event({"type": "chunk", "content": chunk})
                if conv_id:
                    new_conv_id = conv_id

            # 更新历史中的 conv_id
            answer_text = "".join(answer_chunks)
            for h in history:
                if h["id"] == req.history_id:
                    h["analyst_conv_id"] = new_conv_id or analyst_conv_id
                    h["analyst_app"] = "large" if analyst_key_name == "DIFY_LARGE_ANALYST_KEY" else "standard"
                    h.setdefault("qa_messages", []).extend([
                        {"role": "user", "content": req.question, "ts": datetime.now().isoformat()},
                        {"role": "ai", "content": answer_text, "ts": datetime.now().isoformat()},
                    ])
                    break
            _save_history(history)
            await audit_log(
                request,
                "report",
                "追问历史报告",
                f"历史报告：{entry.get('title', req.history_id)}；问题：{_short_text(req.question)}",
                metadata={"history_id": req.history_id},
            )

            yield sse_event({"type": "qa_done", "answer": answer_text})
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")
