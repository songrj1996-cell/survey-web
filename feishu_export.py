"""飞书导出：OAuth 网页登录 + 用「登录用户」身份把 markdown 报告导成飞书云文档。

设计：
- 用户走 OAuth 授权 → 拿到 user_access_token（归属本人）。
- 用 user_access_token 调 drive 导入接口创建 docx → 文档天然归该用户所有（无需转移 owner）。
- 「核心结论」高亮块（callout）为 best-effort：失败则核心结论以普通段落留在正文，不影响主流程。

所有配置来自环境变量：
  FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_BASE / FEISHU_REDIRECT_URI [/ FEISHU_SCOPE]
"""

from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import urlencode

import httpx

FEISHU_APP_ID       = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET   = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_BASE         = os.getenv("FEISHU_BASE", "https://open.feishu.cn/open-apis").rstrip("/")
FEISHU_REDIRECT_URI = os.getenv("FEISHU_REDIRECT_URI", "")
FEISHU_SCOPE        = os.getenv("FEISHU_SCOPE", "")  # 可选；留空用应用默认权限


def is_configured() -> bool:
    return bool(FEISHU_APP_ID and FEISHU_APP_SECRET and FEISHU_REDIRECT_URI)


# ── app_access_token（OIDC 换 token 时作鉴权头）──────────────
_app_token_cache: dict = {"value": "", "exp": 0.0}


async def _app_access_token() -> str:
    now = time.time()
    if _app_token_cache["value"] and _app_token_cache["exp"] > now + 60:
        return _app_token_cache["value"]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{FEISHU_BASE}/auth/v3/app_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 app_access_token 失败: {data}")
        _app_token_cache["value"] = data["app_access_token"]
        _app_token_cache["exp"] = now + data.get("expire", 7000)
        return _app_token_cache["value"]


# ── OAuth ────────────────────────────────────────────────────

def build_authorize_url(state: str) -> str:
    params = {
        "app_id": FEISHU_APP_ID,
        "redirect_uri": FEISHU_REDIRECT_URI,
        "state": state,
    }
    if FEISHU_SCOPE:
        params["scope"] = FEISHU_SCOPE
    return f"{FEISHU_BASE}/authen/v1/authorize?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """authorization_code → user_access_token，并取用户信息。返回登录态字典。"""
    app_token = await _app_access_token()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{FEISHU_BASE}/authen/v1/oidc/access_token",
            headers={"Authorization": f"Bearer {app_token}",
                     "Content-Type": "application/json; charset=utf-8"},
            json={"grant_type": "authorization_code", "code": code},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"换取 user_access_token 失败: {data}")
        d = data["data"]
        token = d["access_token"]
        info = await _get_user_info(token)
        return {
            "open_id": info.get("open_id", ""),
            "name": info.get("name", ""),
            "token": token,
            "refresh": d.get("refresh_token", ""),
            "exp": time.time() + int(d.get("expires_in", 7000)),
        }


async def refresh_token(refresh: str) -> dict:
    app_token = await _app_access_token()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{FEISHU_BASE}/authen/v1/oidc/refresh_access_token",
            headers={"Authorization": f"Bearer {app_token}",
                     "Content-Type": "application/json; charset=utf-8"},
            json={"grant_type": "refresh_token", "refresh_token": refresh},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"刷新 token 失败: {data}")
        d = data["data"]
        return {
            "token": d["access_token"],
            "refresh": d.get("refresh_token", refresh),
            "exp": time.time() + int(d.get("expires_in", 7000)),
        }


async def _get_user_info(user_token: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{FEISHU_BASE}/authen/v1/user_info",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取用户信息失败: {data}")
        return data["data"]


# ── 用用户身份创建文档 ────────────────────────────────────────

async def _upload_md(user_token: str, filename: str, content: bytes) -> str:
    files = {"file": (filename, content, "text/markdown")}
    data = {"file_name": filename, "parent_type": "explorer", "parent_node": "", "size": str(len(content))}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{FEISHU_BASE}/drive/v1/files/upload_all",
            headers={"Authorization": f"Bearer {user_token}"},
            files=files, data=data,
        )
        r = resp.json()
        if r.get("code") != 0:
            raise RuntimeError(f"上传 md 失败: {r}")
        return r["data"]["file_token"]


async def _import_task(user_token: str, file_token: str, name: str) -> str:
    body = {
        "file_extension": "md", "file_token": file_token, "type": "docx",
        "file_name": name.removesuffix(".md"),
        "point": {"mount_type": 1, "mount_key": ""},
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{FEISHU_BASE}/drive/v1/import_tasks",
            headers={"Authorization": f"Bearer {user_token}"}, json=body,
        )
        r = resp.json()
        if r.get("code") != 0:
            raise RuntimeError(f"创建导入任务失败: {r}")
        return r["data"]["ticket"]


async def _poll_import(user_token: str, ticket: str, timeout_seconds: int = 180) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            resp = await client.get(
                f"{FEISHU_BASE}/drive/v1/import_tasks/{ticket}",
                headers={"Authorization": f"Bearer {user_token}"},
            )
            r = resp.json()
            if r.get("code") != 0:
                raise RuntimeError(f"查询导入任务失败: {r}")
            task = r["data"]["result"]
            # 注意：job_status 0=成功、1/2=处理中、其他=错误（与直觉相反）
            status = task.get("job_status")
            if status in (1, 2):
                if asyncio.get_event_loop().time() > deadline:
                    raise RuntimeError("导入任务超时")
                await asyncio.sleep(1.5)
                continue
            if status == 0 and task.get("token"):
                return {"token": task["token"], "type": task.get("type", "docx"), "url": task.get("url")}
            raise RuntimeError(f"导入任务失败: status={status}, msg={task.get('job_error_msg')}")


async def create_doc_as_user(title: str, content: str, user_token: str) -> tuple[str, str]:
    """以登录用户身份导入 markdown → docx，返回 (url, doc_token)。文档归该用户所有。"""
    fname = f"{title}.md"
    file_token = await _upload_md(user_token, fname, content.encode("utf-8"))
    ticket = await _import_task(user_token, file_token, fname)
    res = await _poll_import(user_token, ticket)
    doc_token = res["token"]
    doc_type = res.get("type") or "docx"
    url = res.get("url") or f"https://feishu.cn/{doc_type}/{doc_token}"
    return url, doc_token


# ── 核心结论高亮块（best-effort）──────────────────────────────
# Feishu docx block_type: 4=heading2, 14=callout（高亮块）, 2=text, 12=bullet
_BT_HEADING2 = 4
_BT_CALLOUT = 14


def _text_block(content: str, block_type: int = 2) -> dict:
    key = {2: "text", 12: "bullet"}.get(block_type, "text")
    return {
        "block_type": block_type,
        key: {"elements": [{"text_run": {"content": content}}], "style": {}},
    }


async def apply_core_callout(doc_token: str, user_token: str, core_lines: list[str]) -> bool:
    """把「核心结论」做成飞书高亮块（callout）。best-effort：任何失败都吞掉返回 False，
    此时正文里已有普通的「## 核心结论」段落，不影响可用性。
    """
    if not core_lines:
        return False
    try:
        headers = {"Authorization": f"Bearer {user_token}",
                   "Content-Type": "application/json; charset=utf-8"}
        async with httpx.AsyncClient(timeout=15) as client:
            # 1. 列出文档块，定位「核心结论」heading2 及其后续块（到下一个 heading2 / 末尾）
            blocks = []
            page_token = ""
            while True:
                params = {"page_size": 500}
                if page_token:
                    params["page_token"] = page_token
                resp = await client.get(
                    f"{FEISHU_BASE}/docx/v1/documents/{doc_token}/blocks",
                    headers=headers, params=params,
                )
                r = resp.json()
                if r.get("code") != 0:
                    return False
                blocks.extend(r["data"]["items"])
                page_token = r["data"].get("page_token") or ""
                if not r["data"].get("has_more"):
                    break

            # 文档根块 id（page 块，通常 == doc_token）
            root_id = doc_token
            # 找核心结论 heading2
            start = -1
            for i, b in enumerate(blocks):
                if b.get("block_type") == _BT_HEADING2:
                    h = b.get("heading2", {})
                    txt = "".join(e.get("text_run", {}).get("content", "") for e in h.get("elements", []))
                    if txt.strip() == "核心结论":
                        start = i
                        break
            if start < 0:
                return False
            # 收集到下一个 heading2 之前
            end = len(blocks)
            for j in range(start + 1, len(blocks)):
                if blocks[j].get("block_type") == _BT_HEADING2:
                    end = j
                    break
            section = blocks[start:end]
            section_ids = [b["block_id"] for b in section]

            # 该段块在 root children 里的起止索引（导入文档通常是扁平结构）
            root_children = [b["block_id"] for b in blocks]
            try:
                insert_index = root_children.index(section_ids[0])
                last_index = root_children.index(section_ids[-1])
            except ValueError:
                return False
            # 仅当核心结论段在根层连续时才安全删除，否则只插不删（接受重复，绝不误删）
            contiguous = (last_index - insert_index + 1) == len(section_ids)

            # 2. 在核心结论位置插入一个 callout 块
            resp = await client.post(
                f"{FEISHU_BASE}/docx/v1/documents/{doc_token}/blocks/{root_id}/children",
                headers=headers,
                json={"index": insert_index, "children": [{"block_type": _BT_CALLOUT, "callout": {}}]},
            )
            r = resp.json()
            if r.get("code") != 0:
                return False
            callout_id = r["data"]["children"][0]["block_id"]

            # 3. 往 callout 里塞核心结论文本（标题 + 各要点）
            children = [_text_block("核心结论")]
            for ln in core_lines:
                s = ln.strip()
                if not s or s.startswith("#"):
                    continue
                if s.startswith(("- ", "* ", "•")):
                    children.append(_text_block(s.lstrip("-*• ").strip(), 12))
                else:
                    children.append(_text_block(s))
            await client.post(
                f"{FEISHU_BASE}/docx/v1/documents/{doc_token}/blocks/{callout_id}/children",
                headers=headers, json={"children": children},
            )

            # 4. 删除原普通核心结论段（callout 已插在前，原段整体后移 1）
            if contiguous:
                await client.request(
                    "DELETE",
                    f"{FEISHU_BASE}/docx/v1/documents/{doc_token}/blocks/{root_id}/children/batch_delete",
                    headers=headers,
                    json={"start_index": insert_index + 1, "end_index": last_index + 2},
                )
            return True
    except Exception:
        return False
