"""services/annotate_workflow:数据标注的全部业务编排。

包含:内存会话管理、上传处理、列确认、AI 检测 SSE 流程、质量打标 SSE 流程、
结果 Excel 生成与落盘、历史保存、历史文件下载。
纯标注算法在 annotate 模块;HTTP 参数解析与响应包装在 routers/annotate。
"""
import asyncio
import json
import re
import time
import uuid
from pathlib import Path

import annotate
from fastapi import HTTPException, Request

from app.core.config import ANNOTATE_RESULT_DIR, DIFY_AI_DETECT_KEY, DIFY_QUALITY_KEY
from app.core.parsing import _parse_file
from app.core.responses import sse_event
from app.core.security import _assign_session_owner, _find_history_for_login
from app.integrations.dify_client import call_dify_compatible, sse_dify_stream
from app.services.audit import audit_log
from app.services.auth import _current_login
from app.services.question_detect import _detect_open_text_cols, _group_googleform_matrix
from app.services.report_history import save_annotate_to_history
from app.storage.history import _ensure_history_report_numbers, _load_history

# 标注会话用内存(生命周期短,不跨请求长期保活)
annotate_sessions: dict[str, dict] = {}


# ── 会话辅助 ────────────────────────────────────────────────────


def _annotate_download_filename(filename: str) -> str:
    stem = re.sub(r"\.(csv|xlsx|xls)$", "", filename or "annotated", flags=re.IGNORECASE)
    safe = re.sub(r'[\\/:*?"<>|]', "_", stem).strip() or "annotated"
    return f"{safe}_标注结果.xlsx"


def _annotate_result_path(sid: str) -> Path:
    safe_sid = re.sub(r"[^A-Za-z0-9_-]", "_", sid)
    return ANNOTATE_RESULT_DIR / f"{safe_sid}.xlsx"


def _annotate_incomplete_detail(sess: dict) -> str:
    missing_ai = sess.get("missing_ai_ids", []) or []
    missing_q = sess.get("missing_quality_ids", []) or []
    parts = []
    if missing_ai:
        ids_preview = ", ".join(missing_ai[:5]) + ("…" if len(missing_ai) > 5 else "")
        parts.append(f"AI 检测漏返 {len(missing_ai)} 行（ID：{ids_preview}）")
    if missing_q:
        ids_preview = ", ".join(missing_q[:5]) + ("…" if len(missing_q) > 5 else "")
        parts.append(f"质量打标漏返 {len(missing_q)} 行（ID：{ids_preview}）")
    return "；".join(parts)


def _build_annotate_excel_from_session(sess: dict) -> tuple[bytes, str]:
    rows = sess.get("rows")
    headers = sess.get("headers")
    if not rows:
        raise HTTPException(status_code=400, detail="会话中没有数据")
    incomplete = _annotate_incomplete_detail(sess)
    if incomplete:
        raise HTTPException(status_code=400, detail=f"结果不完整，无法下载：{incomplete}。请返回重试对应任务。")
    filename = sess.get("filename", "annotated")
    excel_bytes = annotate.generate_annotated_excel(
        rows,
        headers,
        sess.get("ai_results", []),
        set(sess.get("confirmed_ai_ids", [])),
        sess.get("quality_results", []),
        sess.get("open_text_cols", []),
        sess.get("id_col", 1),
        sess.get("tasks", {}),
    )
    return excel_bytes, _annotate_download_filename(filename)


async def _save_annotate_result_history(sid: str, sess: dict, request: Request) -> None:
    if _annotate_incomplete_detail(sess):
        return
    login = await _current_login(request)
    _assign_session_owner(sess, login)
    loop = asyncio.get_event_loop()
    excel_bytes, download_name = await loop.run_in_executor(
        None,
        _build_annotate_excel_from_session,
        sess,
    )
    ANNOTATE_RESULT_DIR.mkdir(parents=True, exist_ok=True)
    result_path = _annotate_result_path(sid)
    result_path.write_bytes(excel_bytes)
    save_annotate_to_history(sid, sess, str(result_path), download_name)


def _annotate_ai_log(message: str, **fields) -> None:
    payload = " ".join(f"{k}={v!r}" for k, v in fields.items())
    print(f"[annotate.ai_detect] {message}" + (f" {payload}" if payload else ""), flush=True)


def _get_annotate_session(sid: str) -> dict:
    sess = annotate_sessions.get(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="标注会话不存在或已过期，请重新上传文件")
    sess["ts"] = time.time()
    return sess


_ANNOTATE_SESSION_TTL = 7200  # 2 hours


def _clean_annotate_sessions() -> None:
    cutoff = time.time() - _ANNOTATE_SESSION_TTL
    expired = [k for k, v in annotate_sessions.items() if v.get("ts", 0) < cutoff]
    for k in expired:
        annotate_sessions.pop(k, None)


def _parse_string_array(text: str) -> list[str] | None:
    """从 LLM 输出中提取字符串数组，容忍值内部的裸双引号。"""
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if not m:
        return None
    raw = m.group()
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return [str(r) for r in result]
    except json.JSONDecodeError:
        pass
    items = []
    for line in raw.splitlines():
        line = line.strip().rstrip(',')
        if line.startswith('"') and line.endswith('"') and len(line) >= 2:
            items.append(line[1:-1])
    return items if items else None


def _new_annotate_session() -> str:
    _clean_annotate_sessions()
    sid = str(uuid.uuid4())
    annotate_sessions[sid] = {"ts": time.time()}
    return sid


async def _translate_headers(headers: list) -> list:
    """将表头翻译为中文简体，失败时原样返回。"""
    if not DIFY_AI_DETECT_KEY:
        return headers
    query = (
        "将以下问卷列名按顺序翻译为中文简体，只输出 JSON 数组，不加其他任何内容：\n"
        + json.dumps(headers, ensure_ascii=False)
    )
    full_text = ""
    try:
        async for chunk, _ in sse_dify_stream(query, "hdr-translate", "", DIFY_AI_DETECT_KEY):
            full_text += chunk
        result = _parse_string_array(full_text)
        if result and len(result) == len(headers):
            return result
    except Exception:
        pass
    return headers


# ── 上传 ────────────────────────────────────────────────────────


async def handle_annotate_upload(filename: str, content: bytes, login: dict | None) -> dict:
    """解析上传文件、检测列、翻译表头、创建会话，返回前端所需的 result dict。"""
    try:
        rows = _parse_file(filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not rows or len(rows) <= 1:
        raise HTTPException(status_code=400, detail="文件为空或只有表头")

    headers = rows[0]
    body = rows[1:]

    id_col = annotate.detect_id_column(headers, rows)
    open_text_cols = _detect_open_text_cols(rows, headers)

    matrix_col_idxs: list[int] = []
    for g in _group_googleform_matrix(headers):
        if g["type"] == "matrix":
            matrix_col_idxs.extend(g["member_indexes"])

    headers_zh = await _translate_headers(headers)

    sid = _new_annotate_session()
    sess = {
        "rows": rows,
        "headers": headers,
        "headers_zh": headers_zh,
        "filename": filename,
        "id_col": id_col,
        "open_text_cols": open_text_cols,
    }
    _assign_session_owner(sess, login)
    annotate_sessions[sid].update(sess)

    return {
        "session_id": sid,
        "filename": filename,
        "total_rows": len(body),
        "headers": headers,
        "headers_zh": headers_zh,
        "id_col": id_col,
        "open_text_cols": open_text_cols,
        "matrix_col_idxs": matrix_col_idxs,
        "preview": rows[1: min(4, len(rows))],
    }


# ── 列确认 ──────────────────────────────────────────────────────


def annotate_set_column_config(
    sid: str,
    id_col: int,
    open_text_cols: list[int],
    tasks: dict,
    background: str,
) -> list[str]:
    """更新会话的列配置，返回任务名称列表（用于审计）。"""
    sess = _get_annotate_session(sid)
    sess["id_col"] = id_col
    sess["open_text_cols"] = open_text_cols
    sess["tasks"] = tasks
    sess["background"] = background
    sess["ai_results"] = []
    sess["confirmed_ai_ids"] = []
    sess["quality_results"] = []
    sess.pop("missing_ai_ids", None)
    sess.pop("missing_quality_ids", None)
    task_names = []
    if tasks.get("ai_detect"):
        task_names.append("AI 作答识别")
    if tasks.get("quality"):
        task_names.append("回答质量打标")
    return task_names


# ── AI 检测 SSE ─────────────────────────────────────────────────


def _has_open_text(row: list, open_text_cols: list[int]) -> bool:
    return any((str(row[c]) if c < len(row) else "").strip() for c in open_text_cols)


def _row_id(row: list, id_col: int) -> str:
    return str(row[id_col]).strip() if id_col < len(row) else ""


def _chunks(items: list, size: int) -> list[list]:
    return [items[i: i + size] for i in range(0, len(items), size)]


def _open_text_originals(row: list, open_text_cols: list[int]) -> dict[str, str]:
    return {
        f"col_{c}": str(row[c]).strip()
        for c in open_text_cols
        if c < len(row) and str(row[c]).strip()
    }


def _empty_ai_result(row: list, id_col: int, open_text_cols: list[int], reason: str) -> dict:
    return {
        "id": _row_id(row, id_col),
        "ai_prob": 0,
        "is_polished": "low",
        "reason": reason,
        "evidence": "",
        "originals": _open_text_originals(row, open_text_cols),
        "translations": {},
    }


def _attach_originals(
    results: list[dict],
    batch_rows: list[list],
    id_col: int,
    open_text_cols: list[int],
) -> list[dict]:
    rows_by_id = {_row_id(row, id_col): row for row in batch_rows if _row_id(row, id_col)}
    for result in results:
        row = rows_by_id.get(str(result.get("id", "")).strip())
        if row is not None:
            result["originals"] = _open_text_originals(row, open_text_cols)
    return results


async def _run_ai_direct_batch(
    sid: str,
    batch_rows: list[list],
    headers: list,
    open_text_cols: list[int],
    id_col: int,
    background: str,
    api_key: str,
    label: str,
) -> tuple[list[dict], str]:
    """单个子批次的 Dify 调用 + 解析 + 一次重试。"""
    query = annotate.build_ai_detect_query(batch_rows, headers, open_text_cols, id_col, label, background)
    _annotate_ai_log("subbatch start", sid=sid, batch=label, rows=len(batch_rows), query_len=len(query))
    try:
        text, final_conv, mode, fallback_reason = await call_dify_compatible(
            query, f"{sid}-split-{label}", api_key
        )
        _annotate_ai_log("subbatch dify done", sid=sid, batch=label, mode=mode,
                         answer_len=len(text or ""), fallback=bool(fallback_reason))
        if not (text or "").strip():
            return [], "Dify 返回空内容"
        results, err = annotate.parse_ai_detect_result(text)
        if results:
            return _attach_originals(results, batch_rows, id_col, open_text_cols), ""
        retry_q = (
            f"上次输出无法解析（{err}）。请重新处理下面这批数据，并严格按 schema 用 ```json``` 围栏输出，"
            "不要附加任何解释文字。\n\n"
            f"{query}"
        )
        retry_text, _, retry_mode, retry_fallback = await call_dify_compatible(
            retry_q, f"{sid}-split-retry-{label}", api_key, final_conv
        )
        _annotate_ai_log("subbatch retry done", sid=sid, batch=label, mode=retry_mode,
                         answer_len=len(retry_text or ""), fallback=bool(retry_fallback))
        results, retry_err = annotate.parse_ai_detect_result(retry_text)
        if results:
            return _attach_originals(results, batch_rows, id_col, open_text_cols), ""
        return results, retry_err
    except Exception as exc:
        _annotate_ai_log("subbatch failed", sid=sid, batch=label, error=str(exc)[:1000])
        return [], str(exc)


async def ai_detect_stream(sid: str, request: Request):
    """AI 作答识别 SSE 流程（async generator）。"""
    sess = _get_annotate_session(sid)
    rows = sess.get("rows", [])
    headers = sess.get("headers", [])
    id_col = sess.get("id_col", 1)
    open_text_cols = sess.get("open_text_cols", [])
    background = sess.get("background", "")

    body = rows[1:]
    batch_size = annotate.AI_DETECT_BATCH
    batches = [body[i: i + batch_size] for i in range(0, len(body), batch_size)]
    total_batches = len(batches)

    all_results: list[dict] = []
    all_missing_ids: set[str] = set()
    try:
        yield sse_event({
            "type": "started",
            "rows": len(body),
            "total_batches": total_batches,
            "batch_size": batch_size,
            "msg": f"已连接，准备分析 {len(body)} 行，约 {total_batches} 批",
        })

        for batch_num, batch in enumerate(batches, 1):
            empty_rows = [r for r in batch if not _has_open_text(r, open_text_cols)]
            active_batch = [r for r in batch if _has_open_text(r, open_text_cols)]
            missing_ids = sum(1 for r in active_batch if not _row_id(r, id_col))
            yield sse_event({
                "type": "batch_started",
                "batch": batch_num,
                "done": batch_num - 1,
                "total": total_batches,
                "rows": len(active_batch),
                "skipped": len(empty_rows),
                "missing_ids": missing_ids,
                "msg": f"正在分析第 {batch_num}/{total_batches} 批（{len(active_batch)} 行，跳过 {len(empty_rows)} 行空主观题）",
            })
            if empty_rows:
                all_results.extend(
                    _empty_ai_result(r, id_col, open_text_cols, "主观题为空，系统自动判定为非 AI 作答")
                    for r in empty_rows
                )
            if missing_ids:
                msg = f"第 {batch_num} 批有 {missing_ids} 行缺少玩家唯一 ID，结果可能无法正确回填"
                _annotate_ai_log("missing ids", sid=sid, batch=batch_num, count=missing_ids)
                yield sse_event({"type": "warn", "msg": msg})
            if not active_batch:
                msg = f"第 {batch_num} 批没有可分析的主观题内容，已跳过 AI 调用"
                _annotate_ai_log("skip empty batch", sid=sid, batch=batch_num, rows=len(batch))
                yield sse_event({"type": "warn", "msg": msg})
                yield sse_event({
                    "type": "batch_done",
                    "batch": batch_num,
                    "done": batch_num,
                    "total": total_batches,
                    "count": len(empty_rows),
                    "msg": f"第 {batch_num}/{total_batches} 批完成，空主观题 {len(empty_rows)} 行已自动跳过",
                })
                continue

            query = annotate.build_ai_detect_query(
                active_batch, headers, open_text_cols, id_col, batch_num, background
            )
            _annotate_ai_log("batch start", sid=sid, batch=batch_num, rows=len(active_batch),
                             skipped=len(empty_rows), missing_ids=missing_ids, query_len=len(query))
            try:
                dify_task = asyncio.create_task(call_dify_compatible(query, sid, DIFY_AI_DETECT_KEY))
                while not dify_task.done():
                    yield sse_event({
                        "type": "dify_waiting", "batch": batch_num, "total": total_batches,
                        "msg": "正在等待 AI 返回，请勿关闭页面",
                    })
                    await asyncio.sleep(12)
                answer_text, final_conv, mode, fallback_reason = await dify_task
                _annotate_ai_log("dify done", sid=sid, batch=batch_num, mode=mode,
                                 answer_len=len(answer_text or ""), fallback=bool(fallback_reason))
                yield sse_event({
                    "type": "dify_done", "batch": batch_num, "mode": mode,
                    "answer_len": len(answer_text or ""),
                    "msg": f"第 {batch_num} 批 AI 返回完成（{mode}，{len(answer_text or '')} 字符）",
                })
                if fallback_reason:
                    _annotate_ai_log("fallback", sid=sid, batch=batch_num, reason=fallback_reason[:500])
                    yield sse_event({
                        "type": "warn",
                        "msg": f"第 {batch_num} 批 chat 调用不匹配，已自动改用 completion 调用：{fallback_reason[:240]}",
                    })
            except Exception as e:
                _annotate_ai_log("dify failed", sid=sid, batch=batch_num, error=str(e)[:1000])
                yield sse_event({"type": "warn", "msg": f"第 {batch_num} 批 Dify 调用失败：{e}"})
                split_results: list[dict] = []
                if len(active_batch) > 1:
                    yield sse_event({
                        "type": "warn",
                        "msg": f"第 {batch_num} 批已自动拆成更小子批次重试，避免 Dify 插件/模型超时",
                    })
                    for sub_idx, sub_batch in enumerate(_chunks(active_batch, 2), 1):
                        sub_label = f"{batch_num}.{sub_idx}"
                        sub_results, sub_err = await _run_ai_direct_batch(
                            sid, sub_batch, headers, open_text_cols, id_col, background,
                            DIFY_AI_DETECT_KEY, sub_label,
                        )
                        if sub_results:
                            split_results.extend(sub_results)
                            all_results.extend(sub_results)
                            yield sse_event({
                                "type": "warn",
                                "msg": f"第 {sub_label} 子批次重试成功，获得 {len(sub_results)} 条结果",
                            })
                        else:
                            yield sse_event({"type": "warn", "msg": f"第 {sub_label} 子批次仍失败：{sub_err}"})
                yield sse_event({
                    "type": "batch_done", "batch": batch_num, "done": batch_num,
                    "total": total_batches, "count": len(split_results),
                    "msg": f"第 {batch_num}/{total_batches} 批完成，拆分重试获得 {len(split_results)} 条结果，跳过 {len(empty_rows)} 行空主观题",
                })
                continue

            results, err = annotate.parse_ai_detect_result(answer_text)
            if not results:
                snippet = (answer_text or "")[:500].replace("\n", " ")
                _annotate_ai_log("parse failed", sid=sid, batch=batch_num,
                                 answer_len=len(answer_text or ""), error=err, snippet=snippet)
                retry_q = (
                    f"上次输出无法解析（{err}）。请重新处理下面这批数据，并严格按 schema 用 ```json``` 围栏输出，"
                    "不要附加任何解释文字。\n\n"
                    f"{query}"
                )
                yield sse_event({
                    "type": "warn",
                    "msg": f"第 {batch_num} 批首次解析失败，正在自动重试：{err}；返回长度 {len(answer_text or '')}；片段：{snippet[:180]}",
                })
                try:
                    retry_task = asyncio.create_task(call_dify_compatible(
                        retry_q, sid, DIFY_AI_DETECT_KEY, final_conv
                    ))
                    while not retry_task.done():
                        yield sse_event({
                            "type": "dify_waiting", "batch": batch_num, "total": total_batches,
                            "msg": "正在等待 AI 重试返回，请勿关闭页面",
                        })
                        await asyncio.sleep(12)
                    retry_text, _, retry_mode, retry_fallback = await retry_task
                    _annotate_ai_log("retry done", sid=sid, batch=batch_num, mode=retry_mode,
                                     answer_len=len(retry_text or ""), fallback=bool(retry_fallback))
                    results, err = annotate.parse_ai_detect_result(retry_text)
                except Exception as e:
                    _annotate_ai_log("retry failed", sid=sid, batch=batch_num, error=str(e)[:1000])
                    results, err = [], str(e)

            if results:
                results = _attach_originals(results, active_batch, id_col, open_text_cols)

            if not results:
                _annotate_ai_log("batch no results", sid=sid, batch=batch_num, error=err)
                yield sse_event({"type": "warn", "msg": f"第 {batch_num} 批解析失败：{err}"})
                split_results = []
                if len(active_batch) > 1:
                    yield sse_event({"type": "warn", "msg": f"第 {batch_num} 批将拆成更小子批次继续重试"})
                    for sub_idx, sub_batch in enumerate(_chunks(active_batch, 2), 1):
                        sub_label = f"{batch_num}.{sub_idx}"
                        sub_results, sub_err = await _run_ai_direct_batch(
                            sid, sub_batch, headers, open_text_cols, id_col, background,
                            DIFY_AI_DETECT_KEY, sub_label,
                        )
                        if sub_results:
                            split_results.extend(sub_results)
                            yield sse_event({
                                "type": "warn",
                                "msg": f"第 {sub_label} 子批次重试成功，获得 {len(sub_results)} 条结果",
                            })
                        else:
                            yield sse_event({"type": "warn", "msg": f"第 {sub_label} 子批次仍失败：{sub_err}"})
                if split_results:
                    all_results.extend(split_results)
                    results = split_results
                else:
                    batch_ids = {_row_id(r, id_col) for r in active_batch if _row_id(r, id_col)}
                    if batch_ids:
                        all_missing_ids.update(batch_ids)
                        yield sse_event({
                            "type": "warn",
                            "msg": f"第 {batch_num} 批所有重试均失败，{len(batch_ids)} 行计入缺失，完成后将阻断下载",
                        })
            else:
                all_results.extend(results)
                _annotate_ai_log("batch parsed", sid=sid, batch=batch_num, count=len(results))

            if results:
                expected_ids = {_row_id(r, id_col) for r in active_batch if _row_id(r, id_col)}
                returned_ids = {r["id"] for r in results if r.get("id")}
                missing = expected_ids - returned_ids
                if missing:
                    yield sse_event({"type": "warn", "msg": f"第 {batch_num} 批漏返 {len(missing)} 行，正在补发重试…"})
                    missing_rows = [r for r in active_batch if _row_id(r, id_col) in missing]
                    retry_results, _ = await _run_ai_direct_batch(
                        sid, missing_rows, headers, open_text_cols, id_col, background,
                        DIFY_AI_DETECT_KEY, f"{batch_num}.miss",
                    )
                    if retry_results:
                        results = list(results) + retry_results
                        all_results.extend(retry_results)
                        missing -= {r["id"] for r in retry_results if r.get("id")}
                    if missing:
                        all_missing_ids.update(missing)
                        yield sse_event({
                            "type": "warn",
                            "msg": (
                                f"第 {batch_num} 批重试后仍漏返 {len(missing)} 行"
                                f"（ID：{', '.join(sorted(missing)[:5])}{'…' if len(missing) > 5 else ''}），完成后将阻断下载"
                            ),
                        })

            yield sse_event({
                "type": "batch_done", "batch": batch_num, "done": batch_num,
                "total": total_batches, "count": len(results),
                "msg": f"第 {batch_num}/{total_batches} 批完成，获得 {len(results)} 条 AI 结果，跳过 {len(empty_rows)} 行空主观题",
            })

        sess["ai_results"] = all_results
        sess.pop("missing_ai_ids", None)
        if all_missing_ids:
            sess["missing_ai_ids"] = sorted(all_missing_ids)
        high_prob = [r for r in all_results if r.get("ai_prob", 0) >= 80]
        if not all_missing_ids and not high_prob and not (sess.get("tasks") or {}).get("quality"):
            await _save_annotate_result_history(sid, sess, request)
        await audit_log(
            request, "annotate", "完成 AI 作答识别",
            f"会话：{sid}；结果数：{len(all_results)}；高概率数：{len(high_prob)}；未回填：{len(all_missing_ids)}",
            metadata={"session_id": sid, "results": len(all_results),
                      "high_prob": len(high_prob), "missing": len(all_missing_ids)},
        )
        yield sse_event({
            "type": "ai_detect_done",
            "results": all_results,
            "high_prob": high_prob,
            "missing_ids": sorted(all_missing_ids),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        yield sse_event({"type": "error", "message": str(e)})


# ── confirm-ai ──────────────────────────────────────────────────


async def annotate_set_confirmed_ai(sid: str, confirmed_ai_ids: list[str], request: Request) -> None:
    """存储用户确认的 AI 作答 ID，必要时自动保存历史。"""
    sess = _get_annotate_session(sid)
    sess["confirmed_ai_ids"] = confirmed_ai_ids
    if not (sess.get("tasks") or {}).get("quality"):
        await _save_annotate_result_history(sid, sess, request)


# ── 质量打标 SSE ────────────────────────────────────────────────


async def _run_one_quality_batch(
    sid: str,
    batch_num: int,
    batch: list,
    headers: list,
    open_text_cols: list[int],
    id_col: int,
    api_key: str,
) -> tuple[int, list[dict], set[str], str]:
    """运行单批质量打标，含解析重试和覆盖重试。永远返回结构化结果，不向外抛异常。"""
    batch_ids = {str(r[id_col]).strip() if id_col < len(r) else "" for r in batch}
    batch_ids.discard("")
    try:
        query = annotate.build_quality_label_query(batch, headers, open_text_cols, id_col, batch_num)
        chunks: list[str] = []
        final_conv = ""
        async for chunk, conv_id in sse_dify_stream(query, sid, "", api_key):
            if chunk:
                chunks.append(chunk)
            if conv_id:
                final_conv = conv_id

        results, err = annotate.parse_quality_result("".join(chunks))
        if not results:
            retry_q = (
                f"上次输出无法解析（{err}）。请严格按 schema 用 ```json``` 围栏重新输出，"
                "不要附加任何解释文字。"
            )
            retry_chunks: list[str] = []
            async for chunk, _ in sse_dify_stream(retry_q, sid, final_conv, api_key):
                if chunk:
                    retry_chunks.append(chunk)
            results, err = annotate.parse_quality_result("".join(retry_chunks))

        if not results:
            return batch_num, [], batch_ids, err

        expected = set(batch_ids)
        still_missing: set[str] = set()
        if expected:
            returned = {r["id"] for r in results if r.get("id")}
            missing = expected - returned
            if missing:
                missing_rows = [
                    r for r in batch
                    if (str(r[id_col]).strip() if id_col < len(r) else "") in missing
                ]
                retry_miss_q = annotate.build_quality_label_query(
                    missing_rows, headers, open_text_cols, id_col, f"{batch_num}.miss"
                )
                miss_chunks: list[str] = []
                async for chunk, _ in sse_dify_stream(retry_miss_q, sid, "", api_key):
                    if chunk:
                        miss_chunks.append(chunk)
                miss_results, _ = annotate.parse_quality_result("".join(miss_chunks))
                if miss_results:
                    results.extend(miss_results)
                    missing -= {r["id"] for r in miss_results if r.get("id")}
                still_missing = missing

        return batch_num, results, still_missing, ""
    except Exception as exc:
        return batch_num, [], batch_ids, str(exc)


async def quality_stream(sid: str, request: Request):
    """质量打标 SSE 流程（async generator）。"""
    sess = _get_annotate_session(sid)
    rows = sess.get("rows", [])
    headers = sess.get("headers", [])
    id_col = sess.get("id_col", 1)
    open_text_cols = sess.get("open_text_cols", [])
    confirmed_ai_ids = set(sess.get("confirmed_ai_ids", []))

    body = rows[1:]
    non_ai_body = (
        [r for r in body if str(r[id_col]).strip() not in confirmed_ai_ids]
        if body and id_col < len(headers)
        else body
    )
    batch_size = annotate.QUALITY_BATCH
    batches = [non_ai_body[i: i + batch_size] for i in range(0, len(non_ai_body), batch_size)]
    total_batches = len(batches)
    QUALITY_CONCURRENCY = 3

    all_results: list[dict] = []
    all_missing_ids_q: set[str] = set()
    pending: set[asyncio.Task] = set()
    try:
        sem = asyncio.Semaphore(QUALITY_CONCURRENCY)

        async def run_with_sem(batch_num: int, batch: list):
            async with sem:
                return await _run_one_quality_batch(
                    sid, batch_num, batch, headers, open_text_cols, id_col, DIFY_QUALITY_KEY
                )

        pending = {asyncio.create_task(run_with_sem(i, b)) for i, b in enumerate(batches, 1)}
        done_count = 0
        yield sse_event({
            "type": "progress", "done": 0, "total": total_batches,
            "msg": f"已连接，{len(non_ai_body)} 行分 {total_batches} 批，最多 {QUALITY_CONCURRENCY} 批并行",
        })

        while pending:
            finished, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                batch_num, results, still_missing, err = await task
                done_count += 1
                if not results:
                    all_missing_ids_q.update(still_missing)
                    yield sse_event({
                        "type": "warn",
                        "msg": f"第 {batch_num} 批解析失败：{err}；{len(still_missing)} 行计入缺失，完成后将阻断下载",
                    })
                else:
                    all_results.extend(results)
                    if still_missing:
                        all_missing_ids_q.update(still_missing)
                        yield sse_event({
                            "type": "warn",
                            "msg": (
                                f"第 {batch_num} 批重试后仍漏返 {len(still_missing)} 行"
                                f"（ID：{', '.join(sorted(still_missing)[:5])}{'…' if len(still_missing) > 5 else ''}），完成后将阻断下载"
                            ),
                        })
                yield sse_event({
                    "type": "progress", "done": done_count, "total": total_batches,
                    "msg": f"第 {batch_num} 批完成，获得 {len(results)} 条结果（{done_count}/{total_batches} 批已完成）",
                })

        sess["quality_results"] = all_results
        sess.pop("missing_quality_ids", None)
        if all_missing_ids_q:
            sess["missing_quality_ids"] = sorted(all_missing_ids_q)
        if not all_missing_ids_q:
            await _save_annotate_result_history(sid, sess, request)
        await audit_log(
            request, "annotate", "完成回答质量打标",
            f"会话：{sid}；结果数：{len(all_results)}；未回填：{len(all_missing_ids_q)}",
            metadata={"session_id": sid, "results": len(all_results), "missing": len(all_missing_ids_q)},
        )
        yield sse_event({
            "type": "quality_done",
            "count": len(all_results),
            "missing_ids": sorted(all_missing_ids_q),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        yield sse_event({"type": "error", "message": str(e)})


# ── 下载 ────────────────────────────────────────────────────────


async def build_and_save_annotate_download(sid: str, request: Request) -> tuple[bytes, str]:
    """生成标注 Excel、落盘历史、返回 (bytes, download_name)。"""
    sess = _get_annotate_session(sid)
    loop = asyncio.get_event_loop()
    excel_bytes, download_name = await loop.run_in_executor(
        None, _build_annotate_excel_from_session, sess
    )
    await _save_annotate_result_history(sid, sess, request)
    return excel_bytes, download_name


def get_annotate_history_file(history_id: str, login: dict | None) -> tuple[bytes, str]:
    """从历史记录获取标注文件内容，返回 (bytes, download_name)，找不到则抛 HTTPException。"""
    history = _load_history()
    history = _ensure_history_report_numbers(history)
    entry = _find_history_for_login(history, history_id, login)
    if not entry or entry.get("mode") != "annotate":
        raise HTTPException(status_code=404, detail="标注历史记录不存在")

    raw_path = str(entry.get("annotate_result_path") or "")
    if not raw_path:
        raise HTTPException(status_code=404, detail="这条历史记录没有可下载的标注文件")
    result_path = Path(raw_path)
    if not result_path.is_absolute():
        result_path = ANNOTATE_RESULT_DIR / result_path.name
    try:
        result_resolved = result_path.resolve(strict=True)
        root_resolved = ANNOTATE_RESULT_DIR.resolve()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="标注结果文件已不存在")
    if root_resolved not in result_resolved.parents and result_resolved != root_resolved:
        raise HTTPException(status_code=400, detail="标注结果路径无效")

    download_name = (
        entry.get("annotate_download_name")
        or _annotate_download_filename(entry.get("filename", "annotated"))
    )
    return result_resolved.read_bytes(), download_name
