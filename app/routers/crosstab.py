"""routers/crosstab:倍市得「跑数表模式」上传(问卷 + 回答数据 + 跑数统计表)。

仅含跑数表专属的上传与确定性建列逻辑;后续方案/统计/报告复用 survey 的共享流程。
"""
import re

import crosstab_parser
from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from app.core.audit import audit_log
from app.core.parsing import _parse_file
from app.core.security import _assign_session_owner, _current_login
from app.storage.sessions import get_session, new_session, save_session

router = APIRouter()

_Q_TITLE_RE = re.compile(r"^Q(\d+)\[")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _questionnaire_title_map(q_rows: list[list]) -> dict[int, str]:
    """从问卷行里抽 Q号 → 题目文本(用于给清数列起可读名)。"""
    m: dict[int, str] = {}
    for r in q_rows:
        if not r:
            continue
        c0 = str(r[0]).strip() if len(r) > 0 else ""
        mt = _Q_TITLE_RE.match(c0)
        if mt:
            text = str(r[1]).strip() if len(r) > 1 else ""
            text = _HTML_TAG_RE.sub("", text).strip()
            if text:
                m[int(mt.group(1))] = text
    return m


def _build_crosstab_columns(headers: list[str], q_title_map: dict[int, str]) -> list[dict]:
    """跑数表模式：确定性构建列元数据（无需 AI 题型识别）。

    只关心三类：open_text(供聚类)、id/profile(原文署名)，其余 ignore
    （数字来自跑数表，清数闭合列不参与统计）。
    """
    cols: list[dict] = []
    for i, h in enumerate(headers):
        hs = str(h).strip()
        low = hs.lower()
        role = "ignore"
        name = hs
        if hs.endswith("__open"):
            role = "open_text"
            mt = re.match(r"Q(\d+)", hs)
            if mt and int(mt.group(1)) in q_title_map:
                name = q_title_map[int(mt.group(1))]
        elif "zone_id" in low or "zoneid" in low:
            role = "ignore"
        elif "role_id" in low or "roleid" in low:
            role = "id"
        elif low in ("response id", "responseid"):
            role = "id"
        elif hs in ("段位", "等级", "性别", "年龄", "区服", "国家", "地区", "服务器"):
            role = "profile_dim"
        cols.append({
            "index": i,
            "name": name,
            "role": role,
            "source": "crosstab",
            "column_indexes": [i],
        })
    return cols


@router.post("/api/upload/crosstab")
async def upload_crosstab(
    request: Request,
    survey_file: UploadFile = File(...),    # 问卷（题目意图上下文）
    data_file: UploadFile = File(...),      # 清数：清洗后的问卷回答（用于主观题原文）
    crosstab_file: UploadFile = File(...),  # 跑数表：倍市得交叉统计表（数字来源）
):
    """倍市得「跑数表模式」上传：平台不再自算统计，数字直接取自跑数表，
    只对主观题做聚类。三文件均必需。"""
    # 1) 清数（回答数据）→ rows
    data_content = await data_file.read()
    try:
        rows = _parse_file(data_file.filename or "data.xlsx", data_content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"回答数据解析失败：{e}")
    if not rows or len(rows) <= 1:
        raise HTTPException(status_code=400, detail="回答数据为空或只有表头")

    # 2) 跑数表 → 结构化 → stats markdown
    ct_content = await crosstab_file.read()
    try:
        parsed = crosstab_parser.parse(ct_content)
        crosstab_md = crosstab_parser.render_to_markdown(parsed)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"跑数表解析失败：{e}")

    # 3) 问卷 → 纯文本（题目意图上下文）+ Q号→题名映射（给清数列起可读名）
    survey_content = await survey_file.read()
    try:
        q_rows = _parse_file(survey_file.filename or "survey.xlsx", survey_content)
        questionnaire_text = "\n".join(
            " | ".join(str(c) for c in r if str(c).strip())
            for r in q_rows if any(str(c).strip() for c in r)
        )
        q_title_map = _questionnaire_title_map(q_rows)
    except Exception:
        questionnaire_text = ""
        q_title_map = {}

    # 4) 确定性建列（跳过 AI 题型识别）+ 跑数表题目清单（给 planner）
    columns = _build_crosstab_columns(rows[0], q_title_map)
    crosstab_questions = crosstab_parser.question_names(parsed)

    sid = new_session()
    sess = get_session(sid)
    sess["rows"] = rows
    sess["filename"] = data_file.filename or "data.xlsx"
    sess["mode"] = "crosstab"
    sess["crosstab_md"] = crosstab_md
    sess["questionnaire_text"] = questionnaire_text
    sess["crosstab_questions"] = crosstab_questions
    # 列已确定性建好，直接作为"已确认"，跳过题型确认步骤
    sess["columns_detected"] = columns
    sess["confirmed_columns"] = columns
    _assign_session_owner(sess, await _current_login(request))
    save_session(sid, sess)

    result = {
        "session_id": sid,
        "filename": data_file.filename,
        "total_rows": len(rows) - 1,
        "headers": rows[0],
        "preview": rows[1: min(6, len(rows))],
        "mode": "crosstab",
        "crosstab_questions": len(parsed.get("questions", [])),
        "crosstab_segments": [s["label"] for s in parsed.get("segments", [])],
    }
    await audit_log(
        request,
        "survey",
        "上传跑数表数据",
        f"问卷：{survey_file.filename or '?'}；数据：{data_file.filename or '?'}；"
        f"跑数表：{crosstab_file.filename or '?'}；样本行数：{len(rows) - 1}",
        metadata={"session_id": sid, "rows": len(rows) - 1, "mode": "crosstab"},
    )
    return result
