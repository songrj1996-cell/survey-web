"""Feishu Sheets read-only transport. URLs supplied by users are never fetched."""
from __future__ import annotations

import re
import time
from urllib.parse import quote, urlsplit

import httpx

from app.core import config
from app.integrations import feishu_client

_tenant_cache = {"token": "", "expires": 0}


class SheetsError(RuntimeError):
    pass


class RangeValues(list):
    def __init__(self, values, revision):
        super().__init__(values)
        self.revision = revision


def parse_reference(value: str) -> tuple[str, str]:
    text = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{10,128}", text):
        return "sheets", text
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower()
    allowed = any(host == domain or host.endswith("." + domain) for domain in config.FEISHU_SHEETS_ALLOWED_HOSTS)
    match = re.fullmatch(r"/(wiki|sheets)/([A-Za-z0-9_-]{10,128})/?", parsed.path)
    if parsed.scheme != "https" or not allowed or not match or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise SheetsError("请填写飞书电子表格或知识库表格链接，或电子表格标识")
    return match[1], match[2]


async def application_token() -> str:
    if _tenant_cache["expires"] > time.time() + 60:
        return _tenant_cache["token"]
    if not feishu_client.FEISHU_APP_ID or not feishu_client.FEISHU_APP_SECRET:
        raise SheetsError("应用身份尚未配置，请联系管理员；也可使用有表格权限的飞书登录身份")
    async with httpx.AsyncClient(timeout=config.FEISHU_SHEETS_READ_TIMEOUT, follow_redirects=False) as client:
        try:
            response = await client.post(config.FEISHU_API_BASE + "/auth/v3/tenant_access_token/internal",
                                         json={"app_id": feishu_client.FEISHU_APP_ID, "app_secret": feishu_client.FEISHU_APP_SECRET})
            body = response.json()
        except (httpx.HTTPError, ValueError):
            raise SheetsError("获取应用访问凭据失败，请检查飞书连接配置") from None
    if not isinstance(body, dict):
        raise SheetsError("飞书凭据响应格式异常，请重试")
    if response.status_code != 200 or body.get("code") != 0 or not body.get("tenant_access_token"):
        raise SheetsError(f"获取应用访问凭据失败（HTTP {response.status_code}，飞书码 {body.get('code', '未知')}）")
    _tenant_cache.update(token=body["tenant_access_token"], expires=time.time() + int(body.get("expire", 0)))
    return _tenant_cache["token"]


async def _get(path: str, token: str, params=None) -> dict:
    async with httpx.AsyncClient(timeout=config.FEISHU_SHEETS_READ_TIMEOUT, follow_redirects=False) as client:
        try:
            response = await client.get(config.FEISHU_API_BASE + path, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=utf-8"}, params=params)
            body = response.json()
        except httpx.TimeoutException:
            raise SheetsError("飞书读取超时，请重试；不会使用旧名单代替此次读取") from None
        except (httpx.HTTPError, ValueError):
            raise SheetsError("飞书连接或响应异常，请重试") from None
    if not isinstance(body, dict):
        raise SheetsError("飞书响应格式异常，已停止读取")
    if response.status_code != 200 or body.get("code") != 0:
        raise SheetsError(f"飞书读取失败（HTTP {response.status_code}，飞书码 {body.get('code', '未知')}）；请检查登录账号或应用的表格访问权限及开放平台读取权限")
    result = body.get("data")
    if not isinstance(result, dict):
        raise SheetsError("飞书未返回完整数据，已停止读取")
    return result


async def resolve_spreadsheet(reference: str, token: str) -> str:
    kind, identifier = parse_reference(reference)
    if kind == "wiki":
        data = await _get("/wiki/v2/spaces/get_node", token, {"token": identifier})
        node = data.get("node", {})
        if node.get("obj_type") != "sheet" or not re.fullmatch(r"[A-Za-z0-9_-]{10,128}", node.get("obj_token", "")):
            raise SheetsError("该知识库节点不是电子表格，或返回的表格标识无效")
        identifier = node["obj_token"]
    return identifier


async def list_sheets(spreadsheet: str, token: str) -> list[dict]:
    data = await _get(f"/sheets/v3/spreadsheets/{quote(spreadsheet, safe='')}/sheets/query", token)
    sheets = data.get("sheets")
    if not isinstance(sheets, list) or data.get("has_more"):
        raise SheetsError("飞书未返回完整工作表元数据，已停止读取")
    return sheets


async def read_range(spreadsheet: str, sheet_id: str, first: int, last: int, last_column: str, token: str) -> list[list]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sheet_id):
        raise SheetsError("工作表标识无效")
    address = f"{sheet_id}!A{first}:{last_column}{last}"
    data = await _get(f"/sheets/v2/spreadsheets/{quote(spreadsheet, safe='')}/values/{quote(address, safe='')}",
                      token, {"valueRenderOption": "ToString", "dateTimeRenderOption": "FormattedString"})
    value_range = data.get("valueRange")
    if not isinstance(value_range, dict) or not isinstance(value_range.get("values", []), list):
        raise SheetsError("飞书单元格范围响应不完整")
    values = value_range.get("values", [])
    if len(values) > last - first + 1 or any(not isinstance(row, list) for row in values):
        raise SheetsError("飞书单元格范围响应不符合请求范围")
    revision = data.get("revision", value_range.get("revision"))
    if revision is None:
        raise SheetsError("飞书未返回范围版本，无法确认完整读取一致性")
    return RangeValues(values + [[] for _ in range(last - first + 1 - len(values))], revision)


async def read_sheet(reference: str, sheet_id: str, token: str, *, max_column: int, header_row: int = 1, preview=False) -> dict:
    # Inclusive ranges read hidden and filtered rows; UI filter state is irrelevant.
    spreadsheet = await resolve_spreadsheet(reference, token)
    sheets = await list_sheets(spreadsheet, token)
    sheet = next((item for item in sheets if item.get("sheet_id") == sheet_id), None)
    if not sheet or sheet.get("resource_type", "sheet") != "sheet":
        raise SheetsError("配置的工作表不存在或不属于普通电子表格")
    grid = sheet.get("grid_properties", {})
    row_count, column_count = int(grid.get("row_count", 0)), int(grid.get("column_count", 0))
    if not row_count or max_column >= column_count or header_row > row_count:
        raise SheetsError("表头或列映射超出当前工作表范围，请重新确认映射")
    if row_count > config.FEISHU_SHEETS_MAX_ROWS and not preview:
        raise SheetsError("工作表超过完整读取上限，请管理员调整读取配置；不会截取部分名单")
    number, last_column = max_column + 1, ""
    while number:
        number, remainder = divmod(number - 1, 26)
        last_column = chr(65 + remainder) + last_column
    end = min(row_count, header_row + 5) if preview else row_count
    values = []
    revision = None
    for first in range(header_row, end + 1, config.FEISHU_SHEETS_RANGE_ROWS):
        last = min(first + config.FEISHU_SHEETS_RANGE_ROWS - 1, end)
        segment = await read_range(spreadsheet, sheet_id, first, last, last_column, token)
        current_revision = getattr(segment, "revision", None)
        if revision is not None and current_revision != revision:
            raise SheetsError("读取期间名单内容发生变化，请重试；未使用混合版本名单")
        revision = current_revision
        values.extend(segment)
    # Detect layout changes during a multi-range read instead of combining two layouts.
    after = next((item for item in await list_sheets(spreadsheet, token) if item.get("sheet_id") == sheet_id), None)
    if not after or after.get("grid_properties") != grid or after.get("title") != sheet.get("title"):
        raise SheetsError("读取期间工作表结构发生变化，请重试")
    if revision is not None:
        probe = await read_range(spreadsheet, sheet_id, header_row, header_row, last_column, token)
        if probe.revision != revision:
            raise SheetsError("读取期间名单内容发生变化，请重试；未使用混合版本名单")
    return {"spreadsheet": spreadsheet, "sheet": sheet, "header_row": header_row, "values": values, "revision": revision}
