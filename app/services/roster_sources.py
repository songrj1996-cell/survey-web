"""Shared roster configuration and fresh, complete business snapshots."""
from collections import Counter
from copy import deepcopy
import re
import uuid

from fastapi import HTTPException

from app.integrations import feishu_sheets as sheets
from app.services.feature_access import owner_identity, require_permission
from app.services.group_tasks import fingerprint, now
from app.services.task_records import column_letter
from app.storage import group_workflow as store


async def _token(mode, login):
    if mode == "application":
        return await sheets.application_token()
    token = (login or {}).get("token", "")
    if not token:
        raise sheets.SheetsError("请先使用有此表格权限的飞书账号登录；名单读取不会借用其他成员的登录凭据")
    return token


def _business_access(login):
    for feature in ("annotate", "reward", "roster"):
        try:
            require_permission(login, feature)
            return
        except HTTPException as exc:
            if exc.status_code not in (401, 403):
                raise
    raise HTTPException(403, "没有名单维护或相关业务功能权限")


def list_sources(login):
    require_permission(login, "roster")
    return store.list_records("sources")


async def inspect_source(request, login):
    require_permission(login, "roster")
    try:
        token = await _token(request.auth_mode, login)
        spreadsheet = await sheets.resolve_spreadsheet(request.spreadsheet_url, token)
        metadata = await sheets.list_sheets(spreadsheet, token)
        result = {"spreadsheet": spreadsheet, "sheets": metadata}
        if request.sheet_id:
            sheet = next((s for s in metadata if s.get("sheet_id") == request.sheet_id), None)
            if not sheet:
                raise sheets.SheetsError("请选择该电子表格内的工作表")
            columns = int(sheet.get("grid_properties", {}).get("column_count", 0))
            data = await sheets.read_sheet(spreadsheet, request.sheet_id, token, max_column=min(columns, 256) - 1,
                                           header_row=request.header_row, preview=True)
            result["headers"] = data["values"][0] if data["values"] else []
            result["sample"] = data["values"][1:]
        return result
    except sheets.SheetsError as exc:
        raise HTTPException(422, str(exc)) from None


def _cell(row, index):
    value = row[index] if index is not None and index < len(row) else ""
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise sheets.SheetsError("映射列包含非文本单元格，请转换成纯文本后重试")
    return str(value).strip()


def status_eligibility(status, source):
    """Honor explicit mappings first, then the two equivalent in-group labels."""
    if status in source["eligible_statuses"]:
        return True
    if status in source["ineligible_statuses"]:
        return False
    synonyms = {"群内人员", "群内成员"}
    if status in synonyms and synonyms.intersection(source["eligible_statuses"]):
        return True
    return None


async def _read_source(source: dict, login) -> dict:
    token = await _token(source["auth_mode"], login)
    columns = [source[key] for key in ("player_col", "ori_col", "status_col", "nickname_col") if source.get(key) is not None]
    data = await sheets.read_sheet(source["spreadsheet_url"], source["sheet_id"], token,
                                   max_column=max(columns), header_row=source["header_row"])
    values = data["values"]
    headers = values[0] if values else []
    for column, expected in source.get("expected_headers", {}).items():
        if _cell(headers, int(column)) != expected:
            raise sheets.SheetsError(f"{column_letter(int(column))} 列表头已变化，请在名单来源重新确认映射")
    entries = []
    for row_number, row in enumerate(values[1:], source["header_row"] + 1):
        if not any(_cell(row, index) for index in columns):
            continue
        player, ori, status = (_cell(row, source[key]) for key in ("player_col", "ori_col", "status_col"))
        if not player:
            raise sheets.SheetsError(f"第 {row_number} 行缺少 playerid，请补齐或移走非名单内容")
        error = ""
        raw_id = row[source["player_col"]] if source["player_col"] < len(row) else ""
        if isinstance(raw_id, (int, float)) and abs(raw_id) >= 10**15 or re.fullmatch(r"[+-]?\d+(?:\.\d+)?[eE][+-]?\d+", player):
            error = "playerid 为长数字数值或科学计数法，可能已丢失精度；请在飞书中恢复原始文本 ID"
        match = re.fullmatch(r"([0-9]+)\s*[（(]\s*([0-9]+)\s*[）)]", ori)
        if not match:
            error = (error + "；" if error else "") + "ori 为空或格式错误，应为 uid(zoneid)"
        else:
            ori = f"{match[1]}({match[2]})"
        eligible = status_eligibility(status, source)
        entries.append({"key": f"{source['id']}_{row_number}", "source_id": source["id"], "group_name": source["name"],
                        "source_revision": source["revision"], "row_number": row_number,
                        "player_id": player, "ori": ori, "status": status, "eligible": eligible,
                        "nickname": _cell(row, source.get("nickname_col")), "identity_error": error,
                        "status_error": "" if eligible is not None else f"未识别的群内状态：{status or '空白'}，请维护来源状态映射"})
    return {"entries": entries, "spreadsheet": data["spreadsheet"], "sheet_title": data["sheet"].get("title", ""),
            "headers": {str(c): _cell(headers, c) for c in columns}, "read_at": now()}


async def save_source(request, login, source_id=None):
    require_permission(login, "roster", write=True)
    payload = request.model_dump(exclude={"expected_revision"})
    columns = [payload[key] for key in ("player_col", "ori_col", "status_col", "nickname_col") if payload.get(key) is not None]
    if len(columns) != len(set(columns)):
        raise HTTPException(422, "名单身份、账号、状态和昵称不能映射到同一列")
    if set(payload["eligible_statuses"]) & set(payload["ineligible_statuses"]) or any(not value.strip() for value in payload["eligible_statuses"] + payload["ineligible_statuses"]):
        raise HTTPException(422, "在群和退群状态必须明确且不能重叠")
    previous = store.read("sources", source_id) if source_id else None
    if source_id and (not previous or previous["revision"] != request.expected_revision):
        raise HTTPException(409, "来源已变化，请刷新后确认")
    source = {**payload, "id": source_id or uuid.uuid4().hex, "revision": (previous or {}).get("revision", 0) + 1}
    if previous and not source["enabled"]:
        # An inaccessible source must remain possible to disable. Re-enabling always validates live.
        data = {"headers": previous.get("expected_headers", {}), "spreadsheet": previous["spreadsheet"],
                "sheet_title": previous["sheet_title"], "read_at": previous.get("last_read_at", ""), "entries": []}
    else:
        try:
            data = await _read_source(source, login)
        except sheets.SheetsError as exc:
            raise HTTPException(422, f"{source['name']}：{exc}") from None
    source.update(expected_headers=data["headers"], spreadsheet=data["spreadsheet"], sheet_title=data["sheet_title"],
                  last_read_at=data["read_at"], last_read_status="成功", last_read_error="", row_count=len(data["entries"]), updated_at=now())
    if previous and not source["enabled"]:
        source.update(last_read_status="已停用", row_count=previous.get("row_count", 0))
    formula = source["formula"]
    if formula["mode"] == "same_workbook":
        formula.update(target_spreadsheet=data["spreadsheet"], sheet_title=data["sheet_title"],
                       player_col=source["player_col"], ori_col=source["ori_col"], status_col=source["status_col"],
                       first_data_row=source["header_row"] + 1)
    elif not formula["target_spreadsheet"] or not formula["sheet_title"] or not formula["synchronization_note"].strip():
        raise HTTPException(422, "跨工作簿请填写已同步的目标表格、工作表和同步说明，复制前还会核对内容")
    source["events"] = deepcopy((previous or {}).get("events", [])) + [{"at": now(), **owner_identity(login), "action": "保存名单来源", "revision": source["revision"]}]
    if previous:
        def update(current):
            if current["revision"] != request.expected_revision:
                raise HTTPException(409, "来源已被其他操作者更新，请刷新")
            current.clear()
            current.update(source)
            return current
        return store.mutate("sources", source["id"], update)
    return store.create("sources", source["id"], source)


def _read_status(source, error="", count=None):
    def update(current):
        if current["revision"] == source["revision"]:
            current.update(last_read_at=now(), last_read_status="失败" if error else "成功", last_read_error=error)
            if count is not None:
                current["row_count"] = count
    store.mutate("sources", source["id"], update)


async def read_roster_snapshot(login):
    _business_access(login)
    sources = [s for s in store.list_records("sources") if s["enabled"]]
    if not sources:
        raise HTTPException(409, "尚未配置可用名单来源，请由有名单维护权限的成员配置")
    entries, metadata, errors = [], [], []
    for source in sources:
        try:
            data = await _read_source(source, login)
            _read_status(source, count=len(data["entries"]))
            entries.extend(data["entries"])
            metadata.append({"id": source["id"], "name": source["name"], "revision": source["revision"],
                             "spreadsheet": data["spreadsheet"], "sheet_id": source["sheet_id"],
                             "sheet_title": data["sheet_title"], "read_at": data["read_at"], "row_count": len(data["entries"])})
        except sheets.SheetsError as exc:
            message = f"{source['name']}：{exc}"
            _read_status(source, error=message)
            errors.append(message)
    if errors:
        raise HTTPException(409, "；".join(errors) + "。本次完整名单读取未成功，请处理后重试")
    current_versions = {s["id"]: s["revision"] for s in store.list_records("sources") if s["enabled"]}
    if current_versions != {s["id"]: s["revision"] for s in sources}:
        raise HTTPException(409, "读取期间名单来源配置发生变化，请重试")
    snapshot = {"id": uuid.uuid4().hex, "read_at": now(), "sources": metadata, "entries": entries,
                "fingerprint": fingerprint({"sources": current_versions, "entries": entries})}
    store.create("snapshots", snapshot["id"], snapshot)
    return snapshot


async def validate_formula_reference(source_id, login):
    _business_access(login)
    source = store.read("sources", source_id)
    if not source or not source["enabled"]:
        raise HTTPException(409, "记录使用的名单来源已停用，请先处理")
    try:
        data = await _read_source(source, login)
        reference = deepcopy(source["formula"])
        if reference["mode"] == "same_workbook":
            if reference["sheet_title"] != data["sheet_title"]:
                raise sheets.SheetsError("名单工作表已改名，请重新保存来源映射")
        else:
            token = await _token(source["auth_mode"], login)
            target = await sheets.resolve_spreadsheet(reference["target_spreadsheet"], token)
            metadata = await sheets.list_sheets(target, token)
            matching = [s for s in metadata if s.get("title") == reference["sheet_title"]]
            if len(matching) != 1 or reference["first_data_row"] < 2:
                raise sheets.SheetsError("同步名单工作表或首行配置无效")
            mirror = {**source, "spreadsheet_url": target, "sheet_id": matching[0]["sheet_id"],
                      "header_row": reference["first_data_row"] - 1, "nickname_col": None, "expected_headers": {},
                      **{key: reference[key] for key in ("player_col", "ori_col", "status_col")}}
            synced = await _read_source(mirror, login)
            def signatures(items):
                return Counter((row["player_id"], row["ori"], row["status"]) for row in items)
            if signatures(synced["entries"]) != signatures(data["entries"]):
                raise sheets.SheetsError("目标同步名单与来源不一致，请先同步再复制，不能生成错误查找公式")
            reference["target_spreadsheet"] = target
        duplicates = Counter(row["player_id"] for row in data["entries"])
        if any(count > 1 for count in duplicates.values()):
            raise sheets.SheetsError("名单中有重复 playerid，查找公式不能唯一定位；请先在名单中处理重复")
        reference["group_name"] = source["name"]
        return reference
    except sheets.SheetsError as exc:
        raise HTTPException(409, f"{source['name']}：{exc}") from None
