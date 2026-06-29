"""services/comment_run:评论舆情分析 SSE 流程——流水线、精选原文、保存历史。"""
import asyncio

from fastapi import HTTPException, Request

from app.core.responses import sse_event
from app.services.comment_pipeline import (
    _comment_analysis_pipeline,
    _comment_append_selected_raw_comments,
    _comment_sample_note_md,
    _select_comment_raw_quotes,
)
from app.services.report_history import _comment_report_title, save_to_history
from app.storage.sessions import get_session, save_session


def validate_comment_session_for_run(session_id: str) -> None:
    """校验评论 session 是否具备运行分析的条件，不满足则 raise HTTPException。"""
    sess = get_session(session_id)
    if sess.get("kind") != "comment" or not sess.get("comment_preprocess_done") or not sess.get("comment_sample"):
        raise HTTPException(status_code=400, detail="该会话尚未完成评论预处理，请重新上传并等待预处理完成")


async def comment_run_stream(session_id: str, request: Request):
    """评论舆情分析 SSE 流程（async generator）。"""
    sess = get_session(session_id)
    quote_task: asyncio.Task | None = None
    try:
        if sess.get("comment_long_candidates"):
            quote_task = asyncio.create_task(_select_comment_raw_quotes(sess))
            yield sse_event({"type": "progress", "message": "已启动玩家评论原文精选，舆情报告将先生成…"})
        result = None
        async for item in _comment_analysis_pipeline(sess):
            if item[0] == "progress":
                yield sse_event({"type": "progress", "message": item[1]})
            elif item[0] == "result":
                result = item[1]
        if result is None:
            yield sse_event({"type": "error", "message": "分析未产出结果"})
            return
        sample_note = _comment_sample_note_md(sess)
        if sample_note:
            result["report_md"] = sample_note + "\n\n" + (result.get("report_md") or "*（未生成简报）*").strip()
        report_title = _comment_report_title(sess)
        sess["comment_report_title"] = report_title
        result["title"] = report_title
        result["post_title"] = sess.get("comment_post_title", "")
        sess["comment_result"] = result
        if result.get("report_md"):
            sess["report_md"] = result["report_md"]
        save_session(session_id, sess)
        save_to_history(session_id, sess)
        yield sse_event({"type": "comment_done", **result, "sample_meta": sess.get("comment_sample_meta", {})})
        if quote_task:
            try:
                selected_raw_comments = await quote_task
                current_report = sess.get("report_md") or result.get("report_md") or ""
                full_report = (
                    _comment_append_selected_raw_comments(current_report, selected_raw_comments)
                    if selected_raw_comments
                    else current_report
                )
                sess["comment_selected_raw_comments"] = selected_raw_comments
                sess["report_md"] = full_report
                result["selected_raw_comments"] = selected_raw_comments
                result["report_md"] = full_report
                result["title"] = sess.get("comment_report_title") or _comment_report_title(sess)
                result["post_title"] = sess.get("comment_post_title", "")
                sess["comment_result"] = result
                save_session(session_id, sess)
                save_to_history(session_id, sess)
                if selected_raw_comments:
                    yield sse_event({"type": "progress", "message": f"玩家评论原文精选已生成：{len(selected_raw_comments)} 条"})
                yield sse_event({
                    "type": "comment_quotes_done",
                    "selected_raw_comments": selected_raw_comments,
                    "report_md": full_report,
                })
            except Exception as exc:  # noqa: BLE001 - 精选失败不影响已生成主报告
                print(f"[comment quote_select] skipped due to error: {exc!r}", flush=True)
                yield sse_event({
                    "type": "comment_quotes_error",
                    "message": "玩家评论原文精选生成失败，舆情报告已完成",
                })
        else:
            yield sse_event({
                "type": "comment_quotes_done",
                "selected_raw_comments": [],
                "report_md": result.get("report_md") or "",
            })
    except Exception as e:
        if quote_task and not quote_task.done():
            quote_task.cancel()
            await asyncio.gather(quote_task, return_exceptions=True)
        yield sse_event({"type": "error", "message": str(e)})
