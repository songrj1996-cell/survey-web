"""services/crosstab_service:跑数表模式上传业务——文件解析、列结构生成、session 创建。"""
import re

import crosstab_parser
from fastapi import HTTPException

from app.core.parsing import _parse_file
from app.core.security import _assign_session_owner
from app.services.questionnaire_import import parse_bested_qualitative_upload
from app.storage.sessions import get_session, new_session, save_session

_Q_TITLE_RE = re.compile(r"^Q(\d+)\[")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _questionnaire_title_map(q_rows: list[list]) -> dict[int, str]:
    """从问卷行里抽 Q号 → 题目文本（用于给清数列起可读名）。"""
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
    """跑数表模式：确定性构建列元数据（无需 AI 题型识别）。"""
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


async def handle_crosstab_upload(
    survey_content: bytes,
    survey_filename: str,
    data_content: bytes,
    data_filename: str,
    ct_content: bytes | None,
    ct_filename: str | None,
    login: dict | None,
) -> dict:
    """解析定量文件；跑数表缺省时改用原问卷结构和 Python 统计。"""
    parsed: dict | None = None
    crosstab_md = ""
    deterministic_questions: list[dict] | None = None
    if ct_content:
        try:
            rows = _parse_file(data_filename, data_content)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"回答数据解析失败：{e}")
        if not rows or len(rows) <= 1:
            raise HTTPException(status_code=400, detail="回答数据为空或只有表头")
        try:
            parsed = crosstab_parser.parse(ct_content)
            crosstab_md = crosstab_parser.render_to_markdown(parsed)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"跑数表解析失败：{e}")
    else:
        try:
            imported = parse_bested_qualitative_upload(
                data_content,
                survey_content,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=f"无法根据问卷结果自动计算统计：{e}",
            )
        rows = imported["rows"]
        deterministic_questions = imported["questions"]

    # 3) 问卷 → 纯文本 + Q号→题名映射
    if deterministic_questions is not None:
        questionnaire_text = imported["questionnaire_text"]
        q_title_map = {}
    else:
        try:
            q_rows = _parse_file(survey_filename, survey_content)
            questionnaire_text = "\n".join(
                " | ".join(str(c) for c in r if str(c).strip())
                for r in q_rows if any(str(c).strip() for c in r)
            )
            q_title_map = _questionnaire_title_map(q_rows)
        except Exception:
            questionnaire_text = ""
            q_title_map = {}

    # 4) 确定性建列 + 跑数表题目清单
    columns = deterministic_questions or _build_crosstab_columns(rows[0], q_title_map)
    crosstab_questions = crosstab_parser.question_names(parsed) if parsed else []
    stats_source = "external_crosstab" if parsed else "python"

    sid = new_session()
    sess = get_session(sid)
    sess["rows"] = rows
    sess["filename"] = data_filename
    sess["mode"] = "crosstab" if parsed else "quantitative"
    sess["analysis_mode"] = "quantitative"
    sess["stats_source"] = stats_source
    if parsed:
        sess["crosstab_md"] = crosstab_md
        sess["crosstab_parsed"] = parsed
    sess["questionnaire_text"] = questionnaire_text
    sess["crosstab_questions"] = crosstab_questions
    sess["columns_detected"] = columns
    if deterministic_questions is not None:
        sess["column_provider"] = "questionnaire"
    else:
        sess["confirmed_columns"] = columns
    _assign_session_owner(sess, login)
    save_session(sid, sess)

    return {
        "session_id": sid,
        "filename": data_filename,
        "total_rows": len(rows) - 1,
        "headers": rows[0],
        "preview": rows[1: min(6, len(rows))],
        "mode": sess["mode"],
        "analysis_mode": "quantitative",
        "stats_source": stats_source,
        "requires_column_confirmation": deterministic_questions is not None,
        "crosstab_questions": len((parsed or {}).get("questions", [])),
        "crosstab_segments": [s["label"] for s in (parsed or {}).get("segments", [])],
    }
