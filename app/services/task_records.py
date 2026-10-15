"""One row per player, computed from individual retained feedback, and safe clipboard cells."""
from collections import Counter, defaultdict
from html import escape
import re

from fastapi import HTTPException


def aggregate_players(task: dict) -> list[dict]:
    from app.services.group_tasks import effective_result
    groups = defaultdict(list)
    for row in task.get("feedback", []):
        if not row.get("excluded"):
            groups[row.get("player_id", "")].append(row)
    output = []
    for player_id, rows in groups.items():
        outcomes = [effective_result(task, row) for row in rows]
        identities = {((row.get("identity") or {}).get("source_id", ""),
                       (row.get("identity") or {}).get("ori", "")) for row in rows}
        advs = sorted({row.get("adv", "") for row in rows if row.get("adv")})
        decision = task.get("player_adv", {}).get(player_id)
        issues = []
        if len(identities) != 1 or not all(source and ori for source, ori in identities):
            issues.append("同一玩家的群或 ori 不唯一，请修正反馈身份")
        if len(advs) > 1 and decision is None:
            issues.append("同一玩家有不同 ADV，请明确选择记录账号")
        identity = rows[0].get("identity") or {}
        complete = all(item["complete"] for item in outcomes)
        base = sum(item["base"] or 0 for item in outcomes) if complete else None
        excellent = sum(item["excellent"] or 0 for item in outcomes) if complete else None
        composition = Counter(item["label"] or "待补齐" for item in outcomes)
        rules = task["rules"]
        output.append({
            "player_id": player_id, "ori": identity.get("ori", ""),
            "source_id": identity.get("source_id", ""), "group_name": identity.get("group_name", ""),
            "identity": identity, "adv": decision["value"] if decision else (advs[0] if len(advs) == 1 else ""),
            "adv_choices": advs, "response_ids": [row["id"] for row in rows],
            "feedback_count": len(rows), "composition": dict(composition), "issues": issues,
            "complete": complete, "base": base, "excellent": excellent,
            "total": base + excellent if complete else None,
            "form_valid": any(item["label"] in ("优秀反馈", "有效反馈") for item in outcomes),
            "data_value": len(rows) if rules["allow_repeat"] else rows[0].get("count") if rules["requires_count"] else "",
            "data_pass": any(row.get("gate", {}).get("status") == "pass" for row in rows) if rules["requires_count"] else None,
        })
    return output


def column_letter(index: int) -> str:
    if not 0 <= index <= 18277:
        raise ValueError("列号超出范围")
    value, text = index + 1, ""
    while value:
        value, remainder = divmod(value - 1, 26)
        text = chr(65 + remainder) + text
    return text


def lookup_formula(reference: dict, column: int) -> str:
    title = str(reference.get("sheet_title", ""))
    if not title or re.search(r"[\x00-\x1f\[\]]", title):
        raise HTTPException(409, "请在名单来源中确认公式引用的工作表名称")
    sheet = "'" + title.replace("'", "''") + "'"
    player, value = column_letter(reference["player_col"]), column_letter(column)
    # Current-row reference remains valid when pasted into any target row.
    return f'=INDEX({sheet}!${value}:${value},MATCH(INDEX($C:$C,ROW()),{sheet}!${player}:${player},0))'


def _text(value, *, force_text=False) -> str:
    clean = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    if force_text or clean.lstrip().startswith(("=", "+", "-", "@")):
        # Feishu preserves a pasted apostrophe as part of the ID. A quoted
        # string formula evaluates to the exact text in both clipboard formats;
        # doubled quotes keep every user-supplied character inside that literal.
        return '="' + clean.replace('"', '""') + '"'
    return clean


async def clipboard_records(task_id: str, version: int, login, source_id: str = "") -> dict:
    from app.services.group_tasks import load_task, readiness_issues
    from app.services.roster_sources import validate_formula_reference
    task = load_task(task_id, login)
    if task["version"] != version or task.get("confirmed_version") != version or readiness_issues(task, settlement=True):
        raise HTTPException(409, "请先确认当前版本的完整任务结果")
    players = [p for p in aggregate_players(task) if not source_id or p["source_id"] == source_id]
    if not players:
        raise HTTPException(409, "当前复制范围没有记录")
    references = {}
    for source in {p["source_id"] for p in players}:
        references[source] = await validate_formula_reference(source, login)
    books = {r["target_spreadsheet"] for r in references.values()}
    if len(books) != 1:
        raise HTTPException(409, "各群公式目标工作簿不同，请按群分别复制")
    rows, html_rows = [], []
    for player in players:
        reference = references[player["source_id"]]
        cells = [task["rules"]["payout_date"], _text(task["rules"]["name"]), _text(player["player_id"], force_text=True),
                 lookup_formula(reference, reference["ori_col"]), _text(player["adv"], force_text=bool(player["adv"])),
                 lookup_formula(reference, reference["status_col"]), "是" if player["form_valid"] else "否",
                 player["data_value"], "不适用" if player["data_pass"] is None else "是" if player["data_pass"] else "否",
                 player["base"], player["excellent"], 0]
        rows.append("\t".join(str(cell) if cell is not None else "" for cell in cells))
        html_cells = []
        for cell in cells:
            html_cells.append(f"<td>{escape(str(cell) if cell is not None else '')}</td>")
        html_rows.append("<tr>" + "".join(html_cells) + "</tr>")
    return {"text": "\r\n".join(rows), "html": "<table>" + "".join(html_rows) + "</table>",
            "count": len(rows), "columns": 12, "source_id": source_id,
            "scope": references[source_id]["group_name"] if source_id else "本任务全部群",
            "target_spreadsheet": next(iter(books))}
