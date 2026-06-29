"""services/comment_preprocess:评论文件预处理 SSE 流程。"""
import asyncio
import time

import comment_analysis
from fastapi import Request

import os

from fastapi import HTTPException

from app.core.responses import sse_event
from app.services.audit import audit_log
from app.storage.sessions import get_session, save_session


def validate_comment_session_for_preprocess(session_id: str) -> None:
    """校验评论 session 是否具备预处理条件，不满足则 raise HTTPException。"""
    sess = get_session(session_id)
    if sess.get("kind") != "comment":
        raise HTTPException(status_code=400, detail="该会话不是评论分析任务")
    upload_path = sess.get("comment_upload_path")
    if not upload_path or not os.path.exists(upload_path):
        raise HTTPException(status_code=400, detail="评论文件不存在或已过期，请重新上传")


async def comment_preprocess_stream(session_id: str, request: Request):
    """评论预处理 SSE 流程（async generator）。"""
    sess = get_session(session_id)
    upload_path = sess.get("comment_upload_path")
    filename = sess.get("filename") or "upload.csv"
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
            request, "comment", "预处理评论数据",
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
