"""调研分析平台 Web 后端 v2

新增：
- 本地题型推断 + 用户确认（Step 2）
- Prompt 管理（可编辑 + 版本历史）
- 历史记录（最近 20 条）
- Word 下载修复（RFC 5987）
"""

import asyncio
import base64
import csv
import hashlib
import html
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator
from urllib.parse import quote
from urllib.request import urlopen

import openpyxl
import markdown as markdown_lib
from docx import Document
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import secrets

import annotate
import comment_analysis
import crosstab_parser
import survey_plan
import survey_stats
from app.integrations import feishu_client as feishu_export
from app.integrations.dify_client import chat as dify_chat  # noqa: F401 (kept for compatibility)
from app.integrations.dify_client import call_dify_compatible, sse_dify_stream
from app.schemas.requests import (
    AdminUserPatch,
    AdminUserRequest,
    AnnotateConfirmAIRequest,
    AnnotateConfirmRequest,
    AppSettingsPatch,
    ColumnConfirmRequest,
    HistoryQARequest,
    HistoryTitleUpdateByIdRequest,
    HistoryTitleUpdateRequest,
    PlanConfirmRequest,
    PromptUpdateRequest,
    QARequest,
    UiTextUpdateRequest,
)
from app.storage.prompts import (
    DEFAULT_PROMPTS,
    _get_planner_extra,
    _get_writer_requirements,
    _load_prompts,
    _save_prompts,
)
from app.storage.settings import (
    DEFAULT_APP_SETTINGS,
    _load_app_settings,
    _save_app_settings,
)
from app.storage.audit_store import (
    _append_audit_log,
    _load_audit_logs,
    _save_audit_logs,
)
from app.storage.history import (
    _ensure_history_report_numbers,
    _history_no_value,
    _load_history,
    _next_history_report_no,
    _save_history,
)
from app.storage.whitelist import (
    _PERMS_SCHEMA_VERSION,
    _load_whitelist,
    _migrate_whitelist_perms,
    _save_whitelist,
)
from app.storage.ui_texts import (
    DEFAULT_UI_TEXTS,
    _load_ui_texts,
    _save_ui_texts,
)
from app.core.parsing import _parse_csv, _parse_excel, _parse_file
from app.core.responses import _make_download_response, sse_event
from app.core.text import _short_text
from app.services.report_history import save_annotate_to_history, save_to_history
from app.services.report_render import _inject_disclaimer
from app.services.question_detect import (
    ROLE_LABEL_MAP,
    _build_column_detect_query,
    _enrich_questions,
    _group_googleform_matrix,
    _heuristic_questions,
    _sanitize_choice_options,
)
from app.storage.sessions import (
    SESSION_TTL,
    _COMMENT_UPLOAD_DIR,
    _SESSION_DIR,
    _session_path,
    _sweep_old_sessions,
    _write_session,
    get_session,
    new_session,
    save_session,
)
from app.storage.logins import (
    _load_web_logins,
    _save_web_logins,
    _sync_web_logins_from_disk,
    web_logins,
)
from app.core.security import (
    ALL_PERMS,
    _admin_user_rows,
    _assign_session_owner,
    _current_login,
    _email,
    _find_history_for_login,
    _forbidden_response,
    _get_user_perms,
    _history_owner_key,
    _is_admin,
    _is_public_path,
    _login_allowed,
    _login_denied_reason,
    _login_url,
    _open_id,
    _owner_from_login,
    _require_admin,
    _safe_next_path,
    _trim_history_for_owner,
    _unauthorized_response,
    _visible_to_owner,
    _wants_api_response,
    _whitelist_match,
)
from app.core.audit import (
    AUDIT_FEATURES,
    AUDIT_FEATURE_LABELS,
    _audit_log_from_login,
    _audit_user,
    _client_ip,
    audit_log,
)

# 配置常量、Dify/飞书参数、数据路径、阈值、默认文案统一来自 app/core/config。
# 过渡期 server.py 仍以 import * 取回这些名字;step3 server.py 瘦身后此行移除。
from app.core.config import *  # noqa: F401,F403,E402










# ============================================================
# Session 管理（文件持久化，支持多 worker）
#
# 每个 session 存为 data/sessions/<uuid>.json。
# 写入走 tmp + os.replace 保证原子性，多进程安全。
# annotate_sessions 仍用内存（标注会话生命周期短，不跨请求保活）。
# ============================================================

# ============================================================
# 历史记录
# ============================================================











# ============================================================
# 文件解析
# ============================================================

# ============================================================
# 本地题型推断
# ============================================================



# ============================================================
# Planner / Writer 构建
# ============================================================

def _build_planner_sample(rows: list[list], sample_n: int = 5) -> str:
    if not rows:
        return ""
    headers = rows[0]
    sample = rows[1: 1 + sample_n]

    def esc(s):
        return ("" if s is None else str(s)).replace("|", "\\|").replace("\n", "<br>")

    md = "| " + " | ".join(esc(h) for h in headers) + " |\n"
    md += "| " + " | ".join(["---"] * len(headers)) + " |\n"
    for r in sample:
        cells = [r[i] if i < len(r) else "" for i in range(len(headers))]
        md += "| " + " | ".join(esc(c) for c in cells) + " |\n"

    total_data_rows = max(0, len(rows) - 1)
    return (
        f"<sample>\n"
        f"总数据行数（不含表头）: {total_data_rows}\n"
        f"以下展示表头 + 前 {len(sample)} 行样本：\n\n"
        f"{md}\n"
        f"</sample>"
    )


def _build_planner_query_with_confirmed(rows: list[list], confirmed_columns: list[dict]) -> str:
    """构建给 Planner 的完整 query，含用户确认的题型（逻辑题，矩阵题跨多列）。"""
    sample_md = _build_planner_sample(rows)

    confirmed_lines = []
    for q in confirmed_columns:
        # 兼容旧结构（confirmed_type/index）与新结构（role/name_zh/column_indexes）
        role = q.get("role") or q.get("confirmed_type") or "single_choice"
        name = q.get("name_zh") or q.get("name") or "?"
        cis = q.get("column_indexes") or ([q["index"]] if "index" in q else [])
        label = ROLE_LABEL_MAP.get(role, role)
        extra = ""
        if role in ("single_choice", "profile_dim", "multi_choice", "matrix_multi") and q.get("options"):
            opts = "、".join(str(o) for o in q["options"][:12])
            extra += f"，选项: {opts}"
        if role in ("multi_choice",) and q.get("delimiter"):
            extra += f"，分隔符: 「{q['delimiter']}」"
        if role in ("scale", "matrix_scale") and q.get("scale_min") is not None:
            extra += f"，量程: {q.get('scale_min')}–{q.get('scale_max')}"

        if role in ("matrix_scale", "matrix_multi"):
            rows_lbl = "、".join(str(r) for r in (q.get("rows") or []))
            confirmed_lines.append(
                f"- 矩阵题「{name}」({label})，子项行: {rows_lbl}；"
                f"对应列号 {cis}（这些列同属一道题，**必须整体归入同一个 part**）{extra}"
            )
        else:
            idx = cis[0] if cis else q.get("index", 0)
            confirmed_lines.append(f"- 列{idx}「{name}」: {label}{extra}")

    confirmed_block = "<confirmed_column_types>\n" + "\n".join(confirmed_lines) + "\n</confirmed_column_types>"
    extra_instructions = _get_planner_extra()

    # 检测是否存在画像维度列，生成对应的画像约束指令
    profile_dims = [q for q in confirmed_columns if (q.get("role") or q.get("confirmed_type")) == "profile_dim"]
    if not profile_dims:
        profile_constraint = (
            "\n⚠️ 画像约束（严格执行）：本问卷中用户**没有将任何题目标注为画像维度**。\n"
            "- cross_tabs 数组**必须为空** []\n"
            "- open_questions **不得**建议将任何题目用作用户画像或分组维度\n"
            "- 报告不应包含任何「用户画像」/「人群结构」分析章节\n"
        )
    else:
        dim_names = "、".join(
            f"「{q.get('name_zh') or q.get('name') or '?'}」" for q in profile_dims
        )
        profile_constraint = (
            f"\n画像维度约束：本问卷的画像维度列为 {dim_names}。"
            f"cross_tabs 的 profile_index **只能**使用上述列对应的列号，不得使用其他单选题做交叉分析。\n"
        )

    return (
        f"{sample_md}\n\n"
        f"{confirmed_block}\n\n"
        f"重要：以上题型和选项已由用户在界面中逐一确认；选择题选项必须以 <confirmed_column_types> 中的「选项」为权威，不得根据题干、表头或样本重新猜测选项，也不得围绕已确认选项再次提问。\n"
        f"注意：以上题型已由用户在界面中逐一确认，**不得**在 open_questions 中再次对题型进行发问。"
        f"选项的归并方式（哪个原始值归入哪个标准选项）同样已由用户在界面中逐一确认，**不得**在 open_questions 中就选项归并或分拆方式再次提问。"
        f"矩阵题的多个列号务必整体归入同一个 part。"
        f"{profile_constraint}\n"
        f"{extra_instructions}"
    )


def _build_plan_revision_query(plan: dict, headers: list[str], confirmed_columns: list[dict], user_text: str) -> str:
    header_lines = "\n".join(f"- 列{i}: {h}" for i, h in enumerate(headers))
    confirmed_json = json.dumps(confirmed_columns or [], ensure_ascii=False, indent=2)
    plan_json = json.dumps(plan or {}, ensure_ascii=False, indent=2)
    return (
        "你正在修订一份问卷分析方案。请根据用户的修改意见，在当前方案基础上输出一份完整的新 plan JSON。\n\n"
        "严格要求：\n"
        "1. 只能输出一个完整 JSON 对象，不要输出解释、确认语、Markdown 文本或 ```json 围栏外的内容。\n"
        "2. JSON 必须包含 columns、parts、cross_tabs、open_questions 字段，并通过既有 schema 校验。\n"
        "3. columns 必须保留用户已确认的题型、列号、选项、矩阵题分组等权威信息；不要重新猜测题型或选项。\n"
        "4. parts 必须使用实际存在的列号；矩阵题成员列必须整体归入同一个 part。\n"
        "5. 若用户意见只要求调整章节/分析重点，只改 parts、cross_tabs 或 open_questions，不要无故改 columns。\n\n"
        f"<headers>\n{header_lines}\n</headers>\n\n"
        f"<confirmed_columns_json>\n{confirmed_json}\n</confirmed_columns_json>\n\n"
        f"<current_plan_json>\n{plan_json}\n</current_plan_json>\n\n"
        f"<user_revision_request>\n{user_text.strip()}\n</user_revision_request>\n\n"
        "请现在返回修订后的完整 JSON 对象。"
    )


def _build_crosstab_planner_query(
    questionnaire_text: str,
    available_questions: list[str],
    open_question_names: list[str],
) -> str:
    """跑数表模式：给章节策划 planner 的初始 query（任务/输出格式由 Dify 应用 system prompt 定义）。"""
    q_text = (questionnaire_text or "").strip()
    if len(q_text) > 12000:
        q_text = q_text[:12000] + "\n…（问卷过长，已截断）"
    avail = "\n".join(f"- {q}" for q in available_questions) or "（无）"
    opens = "\n".join(f"- {q}" for q in open_question_names) or "（无）"
    return (
        f"<questionnaire>\n{q_text}\n</questionnaire>\n\n"
        f"<available_questions>\n{avail}\n</available_questions>\n\n"
        f"<open_questions_list>\n{opens}\n</open_questions_list>\n\n"
        "请基于以上规划报告章节大纲，按 system prompt 约定的 JSON 格式输出。"
    )


def _build_crosstab_plan_revision_query(
    questionnaire_text: str,
    current_parts: list[dict],
    user_text: str,
) -> str:
    """跑数表模式：章节大纲的修订 query。"""
    q_text = (questionnaire_text or "").strip()
    if len(q_text) > 12000:
        q_text = q_text[:12000] + "\n…（问卷过长，已截断）"
    outline = json.dumps(current_parts or [], ensure_ascii=False, indent=2)
    return (
        f"<questionnaire>\n{q_text}\n</questionnaire>\n\n"
        f"<current_outline>\n{outline}\n</current_outline>\n\n"
        f"<user_request>\n{user_text.strip()}\n</user_request>\n\n"
        "请在当前大纲基础上按用户意见调整，按 system prompt 约定的 JSON 格式输出。"
    )


def _render_crosstab_plan_card(plan: dict) -> str:
    """跑数表模式：把章节大纲 + 待确认问题渲染成给用户/历史看的 markdown。"""
    lines = ["## 报告章节大纲"]
    for i, p in enumerate(plan.get("parts", []), 1):
        scope = p.get("scope", "")
        lines.append(f"{i}. **{p['name']}**" + (f" — {scope}" if scope else ""))
    oqs = plan.get("open_questions") or []
    if oqs:
        lines.append("")
        lines.append("## 待确认问题")
        for q in oqs:
            lines.append(f"- {q}")
    return "\n".join(lines)


def _json_loads_loose(raw: str) -> tuple[dict | None, str]:
    raw = (raw or "").strip()
    if not raw:
        return None, "empty"
    candidates = [raw]
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S | re.I)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    brace_start = raw.find("{")
    brace_end = raw.rfind("}")
    if 0 <= brace_start < brace_end:
        candidates.append(raw[brace_start:brace_end + 1])

    for text in candidates:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj, ""
            return None, f"json root is {type(obj).__name__}"
        except Exception as e:
            last_err = str(e)
    return None, last_err[:180] if "last_err" in locals() else "invalid json"


def _cluster_diag_column(col_idx: int, col_name: str, total: int, batches: int) -> dict:
    return {
        "col_index": col_idx,
        "col_name": col_name,
        "total": total,
        "batches": batches,
        "phase_a": [],
        "phase_b": {},
        "phase_c": [],
        "status": "running",
        "reason": "",
        "themes": 0,
        "classifications": 0,
        "assignments": 0,
    }


async def _batch_qualitative_analysis(
    open_text: dict,
    plan: dict,
    headers: list,
    session_id: str,
):
    """大样本定性分析四阶段批处理。

    异步生成器，yield ("progress", msg) 或 ("result", clustered_themes)。
    clustered_themes 结构：
    {
        col_idx: {
            "col_name": str,
            "total": int,
            "themes": [{"id","name","description","count","percentage",
                        "positive_count","positive_pct","positive_summary",
                        "negative_count","negative_pct","negative_summary",
                        "quotes": [str]}],
            "other_themes": [{"name","count","percentage"}]
        }
    }
    """
    import json as _json
    from app.integrations.dify_client import workflow_run, STOP_SIGNAL

    clustered_themes: dict = {}
    diagnostics: dict[str, dict] = {}

    for col_idx, entries in open_text.items():
        col = next((c for c in plan["columns"] if c["index"] == col_idx), None)
        col_name = (col and col.get("name")) or (
            headers[col_idx] if col_idx < len(headers) else f"列{col_idx}"
        )
        total = len(entries)
        yield ("progress", f"【{col_name}】开始分析（共 {total} 条）")

        # ── Phase A：分批提取主题候选 ──────────────────────────────────────
        batches = [entries[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
        all_candidates: list[dict] = []
        diag = _cluster_diag_column(col_idx, col_name, total, len(batches))
        diagnostics[str(col_idx)] = diag

        for bi, batch in enumerate(batches, 1):
            yield ("progress", f"【{col_name}】提取主题（批次 {bi}/{len(batches)}）")
            responses_text = "\n".join(
                f"[{i}] {e.get('text', '')}" for i, e in enumerate(batch)
            )
            raw = await workflow_run(
                inputs={
                    "question": col_name,
                    "responses": responses_text,
                    "count": len(batch),
                },
                api_key=DIFY_THEME_EXTRACT_KEY,
                log_prefix=f"A col={col_idx} batch={bi}",
            )
            phase_a = {"batch": bi, "raw_len": len(raw or ""), "parsed": False, "themes": 0, "error": ""}
            if raw == STOP_SIGNAL:
                phase_a["error"] = "Dify returned 400 / STOP_SIGNAL"
                diag["phase_a"].append(phase_a)
                diag["status"] = "failed"
                diag["reason"] = phase_a["error"]
                yield ("progress", f"【{col_name}】主题提取遇到错误，跳过该列")
                break
            parsed, err = _json_loads_loose(raw)
            if parsed:
                themes = parsed.get("themes", [])
                if isinstance(themes, list):
                    all_candidates.extend(themes)
                    phase_a["parsed"] = True
                    phase_a["themes"] = len(themes)
                else:
                    phase_a["error"] = "`themes` is not list"
            else:
                phase_a["error"] = err
                yield ("progress", f"【{col_name}】主题提取结果解析失败（批次 {bi}），继续处理后续批次")
            diag["phase_a"].append(phase_a)

        if not all_candidates:
            diag["status"] = "failed"
            diag["reason"] = diag["reason"] or "主题提取未返回 themes"
            yield ("progress", f"【{col_name}】没有提取到主题，后续报告将尝试使用原文兜底")
            continue

        # ── Phase B：合并去重 ──────────────────────────────────────────────
        yield ("progress", f"【{col_name}】合并主题候选（共 {len(all_candidates)} 个）")
        candidates_text = "\n".join(
            f"- {t.get('name', '')}：{t.get('description', '')}"
            for t in all_candidates
        )
        raw_b = await workflow_run(
            inputs={
                "question": col_name,
                "theme_candidates": candidates_text,
                "total_responses": total,
            },
            api_key=DIFY_THEME_MERGE_KEY,
            log_prefix=f"B col={col_idx}",
        )
        diag["phase_b"] = {"raw_len": len(raw_b or ""), "parsed": False, "themes": 0, "error": ""}
        if raw_b == STOP_SIGNAL or not raw_b:
            diag["phase_b"]["error"] = "Dify returned STOP_SIGNAL" if raw_b == STOP_SIGNAL else "empty response"
            diag["status"] = "failed"
            diag["reason"] = f"主题合并失败：{diag['phase_b']['error']}"
            yield ("progress", f"【{col_name}】主题合并失败，跳过该列")
            continue
        merged, err = _json_loads_loose(raw_b)
        if not merged:
            diag["phase_b"]["error"] = err
            diag["status"] = "failed"
            diag["reason"] = f"主题合并结果解析失败：{err}"
            yield ("progress", f"【{col_name}】主题合并结果解析失败，跳过该列")
            continue
        final_themes = merged.get("themes", [])
        if isinstance(final_themes, list):
            diag["phase_b"]["parsed"] = True
            diag["phase_b"]["themes"] = len(final_themes)
            diag["themes"] = len(final_themes)
        else:
            final_themes = []
            diag["phase_b"]["error"] = "`themes` is not list"

        if not final_themes:
            diag["status"] = "failed"
            diag["reason"] = diag["reason"] or "主题合并未返回 themes"
            yield ("progress", f"【{col_name}】主题合并为空，后续报告将尝试使用原文兜底")
            continue

        theme_list_text = _json.dumps(
            [{"id": t["id"], "name": t["name"], "description": t["description"]}
             for t in final_themes],
            ensure_ascii=False,
        )

        # ── Phase C：回跑分类 ──────────────────────────────────────────────
        # counts[theme_id] = {"total": int, "pos": int, "neg": int, "neutral": int, "mixed": int}
        counts: dict[str, dict] = {t["id"]: {"total": 0, "pos": 0, "neg": 0, "neutral": 0, "mixed": 0}
                                    for t in final_themes}
        counts["other"] = {"total": 0, "pos": 0, "neg": 0, "neutral": 0, "mixed": 0}
        # quotes_pool[theme_id] = list of (sentiment, text)
        quotes_pool: dict[str, list] = {t["id"]: [] for t in final_themes}

        for bi, batch in enumerate(batches, 1):
            yield ("progress", f"【{col_name}】分类回复（批次 {bi}/{len(batches)}）")
            responses_text = "\n".join(
                f"[{i}] {e.get('text', '')}" for i, e in enumerate(batch)
            )
            raw_c = await workflow_run(
                inputs={
                    "question": col_name,
                    "theme_list": theme_list_text,
                    "responses": responses_text,
                },
                api_key=DIFY_CLASSIFY_KEY,
                log_prefix=f"C col={col_idx} batch={bi}",
            )
            phase_c = {"batch": bi, "raw_len": len(raw_c or ""), "parsed": False, "classifications": 0, "assignments": 0, "error": ""}
            if raw_c == STOP_SIGNAL:
                phase_c["error"] = "Dify returned 400 / STOP_SIGNAL"
                diag["phase_c"].append(phase_c)
                yield ("progress", f"【{col_name}】分类回复遇到错误（批次 {bi}），继续处理后续批次")
                continue
            cls_data, err = _json_loads_loose(raw_c)
            if not cls_data:
                phase_c["error"] = err
                diag["phase_c"].append(phase_c)
                yield ("progress", f"【{col_name}】分类结果解析失败（批次 {bi}），继续处理后续批次")
                continue

            classifications = cls_data.get("classifications", [])
            if not isinstance(classifications, list):
                phase_c["error"] = "`classifications` is not list"
                diag["phase_c"].append(phase_c)
                continue
            phase_c["parsed"] = True
            phase_c["classifications"] = len(classifications)
            diag["classifications"] += len(classifications)

            for item in classifications:
                try:
                    resp_idx = int(str(item.get("response_id", "")).strip("[]"))
                    original_text = batch[resp_idx].get("text", "") if resp_idx < len(batch) else ""
                except (ValueError, IndexError):
                    continue

                assignments = item.get("assignments", [])
                if isinstance(assignments, list):
                    phase_c["assignments"] += len(assignments)
                for assign in assignments:
                    tid = assign.get("theme_id", "other")
                    sentiment = assign.get("sentiment", "neutral")
                    if tid not in counts:
                        tid = "other"
                    counts[tid]["total"] += 1
                    if sentiment == "positive":
                        counts[tid]["pos"] += 1
                    elif sentiment == "negative":
                        counts[tid]["neg"] += 1
                    elif sentiment == "mixed":
                        counts[tid]["mixed"] += 1
                    else:
                        counts[tid]["neutral"] += 1
                    if tid != "other" and len(quotes_pool[tid]) < 10 and original_text:
                        quotes_pool[tid].append((sentiment, original_text))
            diag["assignments"] += phase_c["assignments"]
            diag["phase_c"].append(phase_c)

        # ── 统计汇总 ──────────────────────────────────────────────────────
        total_mentions = sum(v["total"] for v in counts.values())
        if total_mentions == 0:
            diag["status"] = "failed"
            diag["reason"] = "分类阶段未产生任何主题归属"
            yield ("progress", f"【{col_name}】分类未产生有效归属，后续报告将尝试使用原文兜底")
            continue

        themes_out = []
        other_themes_out = []

        for t in final_themes:
            tid = t["id"]
            c = counts[tid]
            cnt = c["total"]
            pct = round(cnt / total_mentions * 100, 1)
            pos_cnt = c["pos"]
            neg_cnt = c["neg"]
            pos_pct = round(pos_cnt / cnt * 100, 1) if cnt else 0.0
            neg_pct = round(neg_cnt / cnt * 100, 1) if cnt else 0.0

            # 代表性引用：每种情感最多取 1-2 条
            pool = quotes_pool.get(tid, [])
            pos_q = [txt for sent, txt in pool if sent == "positive"][:2]
            neg_q = [txt for sent, txt in pool if sent == "negative"][:2]
            neu_q = [txt for sent, txt in pool if sent not in ("positive", "negative")][:2]
            quotes = (pos_q + neg_q + neu_q)[:6]
            # 不足 3 条时从 pool 中补充未使用的原文，保证 Writer 有足够素材
            if len(quotes) < 3:
                used = set(quotes)
                extras = [txt for _, txt in pool if txt not in used]
                quotes = quotes + extras[:max(0, 3 - len(quotes))]

            entry = {
                "id": tid,
                "name": t["name"],
                "description": t.get("description", ""),
                "count": cnt,
                "percentage": pct,
                "positive_count": pos_cnt,
                "positive_pct": pos_pct,
                "positive_summary": t.get("positive_summary") or "",
                "negative_count": neg_cnt,
                "negative_pct": neg_pct,
                "negative_summary": t.get("negative_summary") or "",
                "quotes": quotes,
            }
            if pct < OTHER_THEME_PCT:
                other_themes_out.append({"name": t["name"], "count": cnt, "percentage": pct})
            else:
                themes_out.append(entry)

        themes_out.sort(key=lambda x: x["count"], reverse=True)
        other_themes_out.sort(key=lambda x: x["count"], reverse=True)

        clustered_themes[col_idx] = {
            "col_name": col_name,
            "total": total,
            "themes": themes_out,
            "other_themes": other_themes_out,
        }
        diag["status"] = "ok"
        diag["reason"] = ""
        yield ("progress", f"【{col_name}】分析完成，识别 {len(themes_out)} 个主要主题")

    yield ("diagnostics", diagnostics)
    yield ("result", clustered_themes)


def _extract_satisfaction_stats(stats_md: str) -> str:
    """Extract ## sections whose title contains '满意度' from stats markdown."""
    lines = stats_md.split("\n")
    sections: list[list[str]] = []
    current: list[str] = []
    capturing = False

    for line in lines:
        if line.startswith("## "):
            if capturing and current:
                sections.append(current)
            current = [line]
            capturing = "满意度" in line
        elif capturing:
            current.append(line)

    if capturing and current:
        sections.append(current)

    return "\n\n".join("\n".join(s) for s in sections)


def _entry_identity(entry: dict) -> str:
    parts = []
    ids = entry.get("ids") or {}
    profile = entry.get("profile") or {}
    for k, v in ids.items():
        if str(v).strip():
            parts.append(f"{k}={v}")
    for k, v in profile.items():
        if str(v).strip():
            parts.append(f"{k}={v}")
    return "；".join(parts)


def _clip_text(text: str, limit: int = 420) -> str:
    text = str(text or "").strip().replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _sample_open_entries(entries: list[dict], limit: int = 60) -> list[dict]:
    if len(entries) <= limit:
        return entries
    if limit <= 1:
        return entries[:1]
    step = (len(entries) - 1) / (limit - 1)
    idxs = []
    seen = set()
    for i in range(limit):
        idx = round(i * step)
        if idx not in seen:
            seen.add(idx)
            idxs.append(idx)
    return [entries[i] for i in idxs]


def _build_open_text_fallback_md(
    open_text: dict | None,
    clustered_themes: dict,
    plan: dict,
    headers: list[str],
) -> str:
    """Build a deterministic raw-text fallback for open columns without themes."""
    if not open_text:
        return ""

    clustered_keys = {str(k) for k in (clustered_themes or {}).keys()}
    blocks = []
    total_chars = 0
    max_chars = 45000

    for raw_idx, entries in open_text.items():
        idx_key = str(raw_idx)
        if idx_key in clustered_keys:
            continue
        if not entries:
            continue
        try:
            col_idx = int(raw_idx)
        except (TypeError, ValueError):
            col_idx = raw_idx
        col = next((c for c in plan.get("columns", []) if c.get("index") == col_idx), None)
        name = (col and col.get("name")) or (
            headers[col_idx] if isinstance(col_idx, int) and col_idx < len(headers) else f"列{raw_idx}"
        )
        lines = [f"### {name}（列 {raw_idx}，共 {len(entries)} 条非空回答；以下为抽样原文）"]
        for i, entry in enumerate(_sample_open_entries(entries), 1):
            ident = _entry_identity(entry)
            prefix = f"{i}. "
            if ident:
                prefix += f"[{ident}] "
            lines.append(prefix + _clip_text(entry.get("text", "")))
        block = "\n".join(lines)
        if total_chars + len(block) > max_chars:
            blocks.append("### 其余开放题\n（原文较多，已达到兜底上下文上限，未继续展开。）")
            break
        blocks.append(block)
        total_chars += len(block)

    if not blocks:
        return ""
    return (
        "<open_text_fallback>\n"
        "以下开放题未能产出稳定聚类结果。请仅基于这些真实原文做定性归纳和代表性引用；"
        "不要编造精确主题占比或人数。若内容明显属于年龄、性别、地区等画像补充项，不要当作体验观点展开。\n\n"
        + "\n\n".join(blocks)
        + "\n</open_text_fallback>"
    )


def _build_large_sample_writer_query(
    stats_md: str,
    clustered_themes: dict,
    plan: dict,
    headers: list[str],
    open_text: dict | None = None,
) -> str:
    parts_lines = ["  Part 1 受访者画像（固定）"]
    for i, p in enumerate(plan["parts"], 2):
        if "column_indexes" in p:
            col_names = []
            for idx in p["column_indexes"]:
                col = next((c for c in plan["columns"] if c["index"] == idx), None)
                nm = (col and col.get("name")) or (headers[idx] if idx < len(headers) else f"列{idx}")
                rl = col["role"] if col else "?"
                col_names.append(f"{nm}({rl})")
            parts_lines.append(f"  Part {i} {p['name']}: {'; '.join(col_names)}")
        else:
            scope = p.get("scope", "")
            parts_lines.append(f"  Part {i} {p['name']}" + (f": {scope}" if scope else ""))
    plan_summary = "<plan>\n报告结构：\n" + "\n".join(parts_lines) + "\n</plan>"

    theme_blocks = []
    for col_idx, data in clustered_themes.items():
        col_name = data["col_name"]
        total = data["total"]
        lines = [f"### 问题：{col_name}（共 {total:,} 条有效回答）\n"]

        for i, t in enumerate(data["themes"], 1):
            lines.append(f"**主题{i}：{t['name']}**（提及 {t['count']:,} 人次，占 {t['percentage']}%）")
            if t["positive_summary"] or t["positive_count"]:
                lines.append(f"- 正面（{t['positive_count']:,} / {t['positive_pct']}%）：{t['positive_summary']}")
            if t["negative_summary"] or t["negative_count"]:
                lines.append(f"- 负面（{t['negative_count']:,} / {t['negative_pct']}%）：{t['negative_summary']}")
            if t["quotes"]:
                lines.append(f"- 代表原文（请在报告中完整引用，并附中文翻译）：")
                for q in t["quotes"]:
                    lines.append(f'  > "{q}"')
            lines.append("")

        if data["other_themes"]:
            other_parts = "、".join(
                f"{o['name']}（{o['percentage']}%）" for o in data["other_themes"]
            )
            lines.append(f"**其他声音**（合计占比较低）：{other_parts}")

        theme_blocks.append("\n".join(lines))

    open_text_md = (
        "<open_text_themes>\n" + "\n\n".join(theme_blocks) + "\n</open_text_themes>"
        if theme_blocks else "<open_text_themes>（无开放题聚类结果）</open_text_themes>"
    )
    fallback_md = _build_open_text_fallback_md(open_text, clustered_themes, plan, headers)

    satisfaction_md = _extract_satisfaction_stats(stats_md)
    priority_block = (
        f"<priority_metrics>\n{satisfaction_md}\n</priority_metrics>\n\n"
        if satisfaction_md else ""
    )
    requirements = _get_large_sample_writer_requirements(has_satisfaction=bool(satisfaction_md))

    return (
        "**任务**：基于以下大样本问卷分析结果撰写调研报告。\n\n"
        f"{plan_summary}\n\n"
        f"<stats>\n{stats_md}\n</stats>\n\n"
        f"{priority_block}"
        f"{open_text_md}\n\n"
        + (f"{fallback_md}\n\n" if fallback_md else "")
        + f"**要求**：\n{requirements}"
    )


def _get_large_sample_writer_requirements(has_satisfaction: bool = False) -> str:
    satisfaction_rule = (
        "   - **满意度优先原则**：`<priority_metrics>` 中已提取满意度数据，必须将其作为核心结论中最靠前的 1-2 条展示，须包含具体数字"
        if has_satisfaction else
        "   - **满意度优先原则**：若报告中存在任何与满意度评分/评价相关的数据（如好评率、满意度评分、认可度等），必须将其作为核心结论中最靠前的 1-2 条展示，且须包含具体数字"
    )
    return f"""一、报告结构（严格按此顺序，不得调换）
1. **## 核心结论**（必须是第一个二级章节）
   - 列出整份报告中最重要的 5-8 条发现，每条一行，格式：「**结论标题**：具体说明（含数字）」
   - 覆盖所有 Part 的关键洞察，让读者读完此节即可掌握全部重点
{satisfaction_rule}
2. **## Part 1 受访者画像**（固定为第一个 Part，紧接核心结论之后）
   - 画像分布数据用 Markdown 表格呈现（列：维度 / 选项 / 人数占比），不要纯文字罗列
   - 表格之后用 1-2 句话解读画像特征
3. 其余 Part 按方案顺序逐章展开
4. **## 行动建议**（最后一节，3-5 条，每条必须有对应数据依据）

二、结论驱动
- 以"多少人持有什么观点"为核心叙事框架
- 每个结论必须附具体数字（人数或占比），禁止使用"部分用户""少数玩家"等模糊表述

三、主观题原文展示（关键）
- 每个主题/观点至少引用 3 条代表性玩家原文
- 展示格式：先展示原始语言原文（用引号括起），下方紧跟中文翻译（若原文已是中文则免翻译）
  示例：
  > "She's very outdated compared to other mage heroes."（该英雄与其他法师相比显得十分过时。）
  > "Modelnya kurang dipoles."（模型精致度不足。）
  > "模型感觉太老了，需要 revamp。"
- 引用的原文要能支撑该主题的核心论断，优先选择信息量最丰富的
- 若某主题可用原文不足 3 条，则展示全部可用原文，不要编造或重复引用

四、语言风格
- 简洁直接，去掉冗长铺垫和过渡句
- 报告语言为中文；玩家原文保留原语种并附中文翻译"""


def _writer_parts_meta(plan: dict, headers: list[str]) -> list[dict]:
    """返回 [{'i','name','col_desc'}]，供分轮生成时逐 Part 取标题与列说明。"""
    meta = []
    for i, p in enumerate(plan["parts"], 1):
        col_names = []
        for idx in p["column_indexes"]:
            col = next((c for c in plan["columns"] if c["index"] == idx), None)
            name = (col and col.get("name")) or (headers[idx] if idx < len(headers) else f"列{idx}")
            role = col["role"] if col else "?"
            col_names.append(f"{name}({role})")
        meta.append({"i": i, "name": p["name"], "col_desc": "; ".join(col_names)})
    return meta


def _build_writer_context(stats_md: str, open_text: dict, plan: dict, headers: list[str]) -> tuple[str, str, str]:
    """构造 Writer 的完整上下文：(plan_summary, open_text_md, requirements)。
    plan_summary/open_text/stats 仅在多轮生成的第 1 轮发送一次，后续轮次复用会话历史。"""
    parts_meta = _writer_parts_meta(plan, headers)
    parts_lines = [f"  Part {m['i']} {m['name']}: {m['col_desc']}" for m in parts_meta]
    plan_summary = "<plan>\n报告结构：\n" + "\n".join(parts_lines) + "\n</plan>"

    open_text_blocks = []
    for col_idx, texts in open_text.items():
        col = next((c for c in plan["columns"] if c["index"] == col_idx), None)
        name = (col and col.get("name")) or (headers[col_idx] if col_idx < len(headers) else f"列{col_idx}")
        joined_lines = []
        for entry in texts:
            ids = entry.get("ids", {})
            mlbb_vals = [str(v).strip() for k, v in ids.items() if "mlbb" in str(k).casefold() and str(v).strip()]
            player_vals = [str(v).strip() for k, v in ids.items() if "mlbb" not in str(k).casefold() and str(v).strip()]
            id_parts = []
            if player_vals:
                id_parts.append(f"玩家ID={' / '.join(player_vals)}")
            if mlbb_vals:
                id_parts.append(f"MLBBID={' / '.join(mlbb_vals)}")
            profile_str = " / ".join(f"{k}={v}" for k, v in entry.get("profile", {}).items())
            prefix = " | ".join(filter(None, [" | ".join(id_parts), f"画像={profile_str}" if profile_str else ""]))
            text_val = entry.get("text", "")
            joined_lines.append(f"- {f'[{prefix}] ' if prefix else ''}{text_val}")
        joined = "\n".join(joined_lines)
        open_text_blocks.append(f"### {name}（列 {col_idx}, 共 {len(texts)} 条非空回答）\n{joined}")

    open_text_md = (
        "<open_text>\n" + "\n\n".join(open_text_blocks) + "\n</open_text>"
        if open_text_blocks else "<open_text>（本问卷没有开放题）</open_text>"
    )

    requirements = _get_writer_requirements()
    requirements += (
        "\n\n补充：引用玩家原文时必须沿用 `<open_text>` 前缀里的玩家身份信息。"
        "`玩家ID=...` 和 `MLBBID=...` 是两个独立身份字段，报告表格中要拆成 `玩家ID`、`MLBBID` 两列，单元格只放值。"
        "如果出现 `MLBBID=123456(57001)`，括号内是区服编号，必须作为 MLBBID 的一部分展示，"
        "不得拆到「画像信息」或其它列；只有 `<open_text>` 前缀里真的存在 `画像=...` 时，才展示画像信息。"
    )
    return plan_summary, open_text_md, requirements


def _build_writer_first_query(stats_md: str, open_text: dict, plan: dict, headers: list[str]) -> str:
    """多轮生成第 1 轮：发送全部上下文 + 要求，但本轮只让模型输出一级标题。"""
    plan_summary, open_text_md, requirements = _build_writer_context(stats_md, open_text, plan, headers)
    return (
        "**协作方式**：本次报告将**分多轮**生成。下面先给你全部数据（<plan> 报告结构、<stats> 确定性统计、"
        "<open_text> 全部开放题原文）和完整的写作要求。请通读并牢记——后续每一轮我会指定你写其中**某一个章节**，"
        "你要从这些数据里取材，但**每轮只写我当轮指定的部分，绝不提前写其它章节**。\n\n"
        f"{plan_summary}\n\n"
        f"<stats>\n{stats_md}\n</stats>\n\n"
        f"{open_text_md}\n\n"
        f"<report_spec>\n以下是整篇报告最终要满足的写作要求（供你理解全局，后续逐轮执行）：\n{requirements}\n</report_spec>\n\n"
        "**本轮任务（第 1 轮）**：**只**输出报告的一级标题（`# 一级标题`）。"
        "如果 <stats> 或 metadata 中存在「被排除」样本依据，可在标题下另起一行用一句话说明依据。"
        "除此之外**什么都不要写**——不要写核心结论、不要写任何 Part、不要写 Bug 模块、不要写本节总结。"
        "确认你已读完全部数据，本轮输出仅一级标题。"
    )


def _build_writer_part_query(part: dict) -> str:
    """多轮生成中的某个 Part 轮：仅指示写这一个 Part。原文已在会话历史中。"""
    return (
        f"**本轮任务**：现在**只**写 `## Part {part['i']} {part['name']}` 这一个章节的完整内容"
        f"（涉及列：{part['col_desc']}）。\n"
        "严格按 <report_spec> 里对 Part 的写法：紧接 `## Part` 标题后先写一段详尽的「本节总结」段落（连贯文字、不用列表），"
        "再按题目逐一展开；开放题归纳必须用 `### 题目名` 三级标题，其下用 `#### 正面观点`/`#### 负面观点`/`#### 中立 / 建议` 分组，"
        "每个观点用 `**观点：短标题**` + `提及情况：` + `代表性原话：`（小表格）的固定结构，ID 展示规则照 <report_spec>。\n"
        "**约束**：① 只输出这一个 Part，不要写其它 Part；② 不要写核心结论、不要写 Bug 模块；"
        "③ 不要重复前面已经写过的标题或章节；④ 所有数字、百分比必须逐字取自 <stats>，禁止重算或编造。"
    )


def _build_writer_bug_query() -> str:
    """多轮生成的 Bug 模块轮：需要则只输出该模块，否则只回 NONE。"""
    return (
        "**本轮任务**：现在通览 <open_text> 里的**全部**开放反馈，按 <report_spec> 第 8 条判断是否需要 "
        "`## Bug 或待确认问题` 模块（仅当确有疑似功能 bug、体验异常、规则不明、玩家无法判断是否设计如此的问题时才需要）。\n"
        "- 若需要：**只**输出该模块——以 `## Bug 或待确认问题` 开头，下接 Markdown 表格，字段固定为 "
        "`问题类型`、`待确认问题`、`玩家信息`、`玩家原文翻译`，不要输出任何其它章节或解释。\n"
        "- 若不需要：**只**回复一个词 `NONE`，不要输出任何其它内容、不要解释。"
    )


def _build_writer_core_query(parts_meta: list[dict], has_bug: bool) -> str:
    """多轮生成的核心结论轮：基于已生成全部章节回写核心结论模块（放在报告顶部）。"""
    part_titles = "、".join(f"Part {m['i']} {m['name']}" for m in parts_meta)
    bug_clause = (
        "正文包含 `## Bug 或待确认问题` 模块，因此核心结论**最后必须**追加 `### 待确认问题概述`，只概述问题类型、不展开原文。"
        if has_bug else
        "正文没有 Bug 模块，因此核心结论**不要**写任何「待确认问题」相关小节。"
    )
    return (
        "**本轮任务（最后一轮）**：基于你前面已经生成的全部章节，撰写整篇报告的「核心结论」模块。"
        "这个模块最终会被放到报告**最顶部**（一级标题之后、各 Part 之前），所以请独立、完整地写出来。\n"
        "严格按 <report_spec> 里『核心结论』部分的格式：用 `<!--CORE_START-->` 和 `<!--CORE_END-->` 两个标记"
        "**各自独占一行**包裹整段，内部依次写：`## 核心结论`（首行写明样本总数）、`### 总体判断`、"
        f"逐个写 `### Part X 章节名：关键发现`（必须引用真实 Part 名：{part_titles}）、按需的 `### 高信号少数观点与风险`。\n"
        f"{bug_clause}\n"
        "**约束**：① 只输出从 `<!--CORE_START-->` 到 `<!--CORE_END-->` 的内容，不要重复正文章节、不要再写一级标题；"
        "② 核心结论里不使用百分比、不使用精确人数，改用量级描述（样本总数可引用）；"
        "③ 引用的绝对数值必须与 <stats> 一致。"
    )


def _format_rows_for_qa(rows: list[list], plan: dict) -> str:
    QA_MAX = 60000
    if not rows or len(rows) <= 1:
        return "（无数据）"
    headers = rows[0]
    body = rows[1:]
    total = len(body)
    col_names = [(h or "").strip() or f"col_{i}" for i, h in enumerate(headers)]

    def row_obj(row):
        return {col_names[i]: (row[i] if i < len(row) else "") for i in range(len(col_names))}

    dump = "\n".join(json.dumps(row_obj(r), ensure_ascii=False) for r in body)
    if len(dump) > QA_MAX:
        pidxs = [c["index"] for c in plan.get("columns", []) if c.get("role") == "profile_dim"]
        sampled = _stratified_sample(body, pidxs, 100)
        note = (
            f"# 原始数据共 {total} 行，超出上下文上限，已按画像维度分层抽样到 {len(sampled)} 行。\n\n"
        )
        dump = note + "\n".join(json.dumps(row_obj(r), ensure_ascii=False) for r in sampled)
    return dump


def _stratified_sample(body: list[list], profile_indexes: list[int], target: int = 100):
    if not profile_indexes or len(body) <= target:
        return body[:target]

    def key(row):
        return tuple(row[i] if i < len(row) else "" for i in profile_indexes)

    buckets: dict = {}
    for r in body:
        buckets.setdefault(key(r), []).append(r)

    out: list = []
    total = len(body)
    for items in buckets.values():
        share = max(1, round(len(items) / total * target))
        out.extend(items[:share])
        if len(out) >= target:
            break
    return out[:target]


# ============================================================
# Markdown → Word
# ============================================================







# ============================================================
# SSE 工具
# ============================================================











# ============================================================
# 帖子评论舆情分析（单 Dify Workflow，mode 路由 + Python 并发编排）
# ============================================================













# ============================================================
# FastAPI:app 实例 / 中间件 / 静态 / 首页·登录 均在 app/main.py。
# 过渡期 server.py 仍 import 该 app,并在下方继续注册尚未迁出的路由;
# 步骤3 迁完后本文件仅剩 `from app.main import app` 一行。
# ============================================================

from app.main import app  # noqa: E402

# ── 上传 ──────────────────────────────────────────────

@app.post("/api/upload")
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


# ── 评论舆情分析：上传 + 运行 ──────────────────────────────────────









# ── 跑数表模式上传（问卷 + 回答数据 + 跑数统计表）──────────────────────

# ── 题型识别（Step 2，LLM，SSE）──────────────────────

@app.get("/api/columns/{session_id}")
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


@app.post("/api/columns/{session_id}/confirm")
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


# ── Plan（SSE）────────────────────────────────────────

@app.get("/api/plan/{session_id}")
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


# ── Plan 确认/修改 ──────────────────────────────────

@app.post("/api/plan/confirm")
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


# ── 统计 ────────────────────────────────────────────

@app.post("/api/stats/{session_id}")
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


# ── 报告（SSE）──────────────────────────────────────

@app.get("/api/report/{session_id}")
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


# ── QA（SSE）────────────────────────────────────────

def _analyst_key_for_report(obj: dict) -> tuple[str, str]:
    """Return the Dify key that owns this report conversation."""
    analyst_app = obj.get("analyst_app") or ""
    mode = obj.get("mode") or obj.get("plan", {}).get("mode", "")
    if analyst_app == "large" or mode == "crosstab":
        return DIFY_LARGE_ANALYST_KEY, "DIFY_LARGE_ANALYST_KEY"
    return DIFY_ANALYST_KEY, "DIFY_ANALYST_KEY"


@app.post("/api/qa")
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


# ── 历史 QA（从历史条目恢复会话）────────────────────

@app.post("/api/history-qa")
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


# ── 导出 ────────────────────────────────────────────



































# ── 飞书 OAuth 登录 + 文档导出 ────────────────────────

# Web 登录态（内存 + 本地文件；服务热更新/重启后仍保留 7 天会话）














# ── Prompts 管理 ─────────────────────────────────────

# ── 历史记录 ──────────────────────────────────────────

# ============================================================
# 数据标注模块
# ============================================================









# ── 上传（标注专用）─────────────────────────────────────







# ── 确认列 + 任务选择 ────────────────────────────────────



# ── AI 作答识别（SSE）───────────────────────────────────



# 用户确认 AI 结果


# ── 质量打标（SSE）──────────────────────────────────────



# ── 下载标注 Excel ───────────────────────────────────────





# ── 启动 ────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
