"""routers/comment_analysis:帖子评论舆情分析(上传 / 预处理 / 运行)。"""
import asyncio
import hashlib
import os
import time
from pathlib import Path

import comment_analysis
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.core.responses import sse_event
from app.core.security import _assign_session_owner
from app.services.audit import audit_log
from app.services.auth import _current_login
from app.services.comment_pipeline import (
    _comment_analysis_pipeline,
    _comment_append_selected_raw_comments,
    _comment_sample_note_md,
    _select_comment_raw_quotes,
)
from app.services.report_history import (
    _comment_report_title,
    _find_comment_duplicate_report,
    save_to_history,
)
from app.storage.sessions import _COMMENT_UPLOAD_DIR, get_session, new_session, save_session
from app.storage.settings import _load_app_settings

router = APIRouter()


@router.post("/api/comment-analysis/upload")
async def comment_analysis_upload(
    request: Request,
    file: UploadFile = File(...),
    post_title: str = Form(""),
    post_content: str = Form(""),
):
    """上传评论文件 → 保存临时文件 → 创建 session。重型预处理走 SSE。"""
    content = await file.read()
    filename = file.filename or "upload.csv"
    suffix = Path(filename).suffix.lower()
    if suffix not in {".csv", ".xlsx", ".xls"}:
        raise HTTPException(status_code=400, detail="仅支持 CSV / Excel（.csv / .xlsx / .xls）文件")
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")
    post_title = (post_title or "").strip()
    post_content = (post_content or "").strip()
    if not post_title:
        raise HTTPException(status_code=400, detail="请填写帖子标题")
    if not post_content:
        raise HTTPException(status_code=400, detail="请填写帖子原文")
    file_hash = hashlib.sha256(content).hexdigest()
    login = await _current_login(request)
    duplicate_report = None
    if _load_app_settings().get("comment_duplicate_reminder_enabled", True):
        duplicate_report = _find_comment_duplicate_report(file_hash, login)

    sid = new_session()
    _COMMENT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_path = _COMMENT_UPLOAD_DIR / f"{sid}{suffix}"
    with open(upload_path, "wb") as f:
        f.write(content)

    print(
        f"[comment-upload] saved sid={sid} file={filename} size={len(content)} path={upload_path}",
        flush=True,
    )

    sess = get_session(sid)
    sess["kind"] = "comment"
    sess["mode"] = "comment"  # 导出时据此插评论分析专用免责声明，不插问卷定性声明
    sess["filename"] = filename
    sess["comment_file_hash"] = file_hash
    sess["comment_upload_path"] = str(upload_path)
    sess["comment_upload_size"] = len(content)
    sess["comment_post_title"] = post_title
    sess["comment_post_content"] = post_content
    sess["comment_preprocess_done"] = False
    _assign_session_owner(sess, login)
    save_session(sid, sess)
    await audit_log(
        request,
        "comment",
        "上传评论文件",
        f"文件：{filename}；大小：{len(content)} bytes",
        metadata={"session_id": sid, "size": len(content)},
    )
    return {
        "session_id": sid,
        "filename": filename,
        "size": len(content),
        "preprocess_required": True,
        "duplicate_report": duplicate_report,
    }


@router.get("/api/comment-analysis/preprocess/{session_id}")
async def comment_analysis_preprocess(session_id: str, request: Request):
    """SSE：流式解析、清洗、抽样评论文件，并保存抽样结果到 session。"""
    sess = get_session(session_id)
    if sess.get("kind") != "comment":
        raise HTTPException(status_code=400, detail="该会话不是评论分析任务")
    upload_path = sess.get("comment_upload_path")
    filename = sess.get("filename") or "upload.csv"
    if not upload_path or not os.path.exists(upload_path):
        raise HTTPException(status_code=400, detail="评论文件不存在或已过期，请重新上传")

    async def generate():
        try:
            if sess.get("comment_preprocess_done") and sess.get("comment_sample"):
                yield sse_event({
                    "type": "comment_preprocess_done",
                    "message": "预处理已完成",
                    "scan_rows": sess.get("comment_scan_rows", 0),
                    "nonempty_count": sess.get("comment_nonempty_count", 0),
                    "valid_count": sess.get("comment_valid_count", 0),
                    "sample_count": sess.get("comment_sample_count", 0),
                    "scan_capped": sess.get("comment_scan_capped", False),
                    "warning": sess.get("comment_scan_warning", ""),
                    "sample_meta": sess.get("comment_sample_meta", {}),
                })
                return

            start = time.time()
            print(f"[comment-preprocess] start sid={session_id} file={filename}", flush=True)
            yield sse_event({"type": "progress", "message": "上传完成，开始预处理评论文件…"})
            final_result = None
            for item in comment_analysis.preprocess_comment_file(upload_path, filename):
                if item.get("kind") == "progress":
                    payload = {"type": "progress", **{k: v for k, v in item.items() if k != "kind"}}
                    yield sse_event(payload)
                    await asyncio.sleep(0)
                elif item.get("kind") == "done":
                    final_result = item["result"]

            if not final_result:
                yield sse_event({"type": "error", "message": "预处理未产出结果"})
                return

            sample = final_result.pop("sample")
            long_candidates = final_result.pop("long_candidates", [])
            sess.update({
                "comment_preprocess_done": True,
                "comment_col_name": final_result.get("comment_column", ""),
                "comment_column_name": final_result.get("comment_column", ""),
                "comment_column_index": final_result.get("comment_column_index", 0),
                "comment_column_detection": final_result.get("comment_column_detection", {}),
                "comment_scan_rows": final_result.get("scan_rows", 0),
                "comment_nonempty_count": final_result.get("nonempty_count", 0),
                "comment_valid_count": final_result.get("valid_count", 0),
                "comment_sample_count": final_result.get("sample_count", 0),
                "comment_sample_pool_count": final_result.get("sample_pool_count", final_result.get("sample_count", 0)),
                "comment_scan_capped": final_result.get("scan_capped", False),
                "comment_scan_warning": final_result.get("warning", ""),
                "comment_filter_stats": final_result.get("filter_stats", {}),
                "comment_sample": sample,
                "comment_long_candidates": long_candidates,
                "comment_long_candidate_count": final_result.get("long_candidate_count", len(long_candidates)),
                "comment_sample_meta": {
                    "scan_rows": final_result.get("scan_rows", 0),
                    "nonempty_count": final_result.get("nonempty_count", 0),
                    "valid_count": final_result.get("valid_count", 0),
                    "sample_count": final_result.get("sample_count", 0),
                    "sample_pool_count": final_result.get("sample_pool_count", final_result.get("sample_count", 0)),
                    "initial_sample_max_n": final_result.get("initial_sample_max_n", 0),
                    "sample_pool_max_n": final_result.get("sample_pool_max_n", 0),
                    "long_candidate_count": final_result.get("long_candidate_count", len(long_candidates)),
                    "long_candidate_max_n": final_result.get("long_candidate_max_n", 0),
                    "scan_capped": final_result.get("scan_capped", False),
                    "warning": final_result.get("warning", ""),
                    "column_name": final_result.get("comment_column", ""),
                    "column_index": final_result.get("comment_column_index", 0),
                    "column_detection": final_result.get("comment_column_detection", {}),
                },
            })
            save_session(session_id, sess)
            print(
                "[comment-preprocess] done "
                f"sid={session_id} scan={sess['comment_scan_rows']} valid={sess['comment_valid_count']} "
                f"sample={sess['comment_sample_count']} elapsed={time.time() - start:.2f}s",
                flush=True,
            )
            await audit_log(
                request,
                "comment",
                "预处理评论数据",
                f"文件：{filename}；扫描 {sess['comment_scan_rows']} 行；有效 {sess['comment_valid_count']} 条；抽样 {sess['comment_sample_count']} 条",
                metadata={
                    "session_id": session_id,
                    "scan_rows": sess["comment_scan_rows"],
                    "valid": sess["comment_valid_count"],
                    "sample": sess["comment_sample_count"],
                    "scan_capped": sess["comment_scan_capped"],
                },
            )
            client_payload = {k: v for k, v in final_result.items() if k not in {"preview_comments"}}
            yield sse_event({
                "type": "comment_preprocess_done",
                "message": "评论预处理完成，开始 AI 分析…",
                **client_payload,
                "sample_meta": sess["comment_sample_meta"],
            })
        except Exception as e:
            print(f"[comment-preprocess] failed sid={session_id} error={e!r}", flush=True)
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.get("/api/comment-analysis/run/{session_id}")
async def comment_analysis_run(session_id: str, request: Request):
    """SSE：执行评论舆情分析流水线，流式回传进度与最终结果。"""
    sess = get_session(session_id)
    if sess.get("kind") != "comment" or not sess.get("comment_preprocess_done") or not sess.get("comment_sample"):
        raise HTTPException(status_code=400, detail="该会话尚未完成评论预处理，请重新上传并等待预处理完成")

    async def generate():
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
            # 同步写入 report_md，复用现有 PDF / 飞书导出端点
            if result.get("report_md"):
                sess["report_md"] = result["report_md"]
            save_session(session_id, sess)
            save_to_history(session_id, sess)
            yield sse_event({"type": "comment_done", **result, "sample_meta": sess.get("comment_sample_meta", {})})
            if quote_task:
                try:
                    selected_raw_comments = await quote_task
                    current_report = sess.get("report_md") or result.get("report_md") or ""
                    if selected_raw_comments:
                        full_report = _comment_append_selected_raw_comments(current_report, selected_raw_comments)
                    else:
                        full_report = current_report
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

    return StreamingResponse(generate(), media_type="text/event-stream")
