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
import re
import time
from urllib.parse import urlencode

import httpx

FEISHU_APP_ID       = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET   = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_BASE         = os.getenv("FEISHU_BASE", "https://open.feishu.cn/open-apis").rstrip("/")
FEISHU_REDIRECT_URI = os.getenv("FEISHU_REDIRECT_URI", "")
FEISHU_SCOPE        = ""  # 留空用应用默认权限；不要用单一 drive:file:upload 覆盖授权范围


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


async def _get_email_via_contact(open_id: str) -> str:
    """用 app_access_token + Contact API 查用户邮箱（需 contact:user.base:readonly）。"""
    try:
        app_token = await _app_access_token()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{FEISHU_BASE}/contact/v3/users/{open_id}",
                headers={"Authorization": f"Bearer {app_token}"},
                params={"user_id_type": "open_id"},
            )
            data = resp.json()
            if data.get("code") != 0:
                print(f"[auth] contact API 查邮箱失败 open_id={open_id}: {data}")
                return ""
            user = data.get("data", {}).get("user", {})
            email = (user.get("email") or user.get("enterprise_email") or "").strip().lower()
            if email:
                print(f"[auth] contact API 成功获取邮箱 open_id={open_id} email={email}")
            else:
                print(f"[auth] contact API 未返回邮箱 open_id={open_id} user_fields={list(user.keys())}")
            return email
    except Exception as e:
        print(f"[auth] contact API 异常: {e}")
        return ""


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
        open_id = info.get("open_id", "")
        email = (info.get("email") or info.get("enterprise_email") or "").strip().lower()
        print(f"[auth] user_info open_id={open_id} name={info.get('name','')} email_from_authen={email!r}")
        # 若 /authen/v1/user_info 未返回邮箱，用 app_access_token + Contact API 补查
        if not email and open_id:
            email = await _get_email_via_contact(open_id)
        return {
            "open_id": open_id,
            "union_id": info.get("union_id", ""),
            "user_id": info.get("user_id", ""),
            "name": info.get("name", ""),
            "email": email,
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


# ── 用机器人（app_access_token）创建文档 + 发消息 ──────────────

async def create_doc_via_bot(
    title: str,
    content: str,
    open_id: str | None = None,
    callout_sections: list[dict] | None = None,
) -> tuple[str, str, str]:
    """用 tenant_access_token 创建文档，返回 (URL, doc_token, doc_type)。
    若提供 open_id，文档创建后转移所有权给该用户（文档移入用户我的空间）。
    """
    fname = f"{title}.md"
    md_bytes = content.encode("utf-8")
    app_token = await _app_access_token()

    # 1. 上传 md 文件
    files = {"file": (fname, md_bytes, "text/markdown")}
    fdata = {"file_name": fname, "parent_type": "explorer", "parent_node": "", "size": str(len(md_bytes))}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{FEISHU_BASE}/drive/v1/files/upload_all",
            headers={"Authorization": f"Bearer {app_token}"},
            files=files, data=fdata,
        )
        res = r.json()
        if res.get("code") != 0:
            raise RuntimeError(f"上传 md 失败 code={res.get('code')} {res.get('msg','')} — 请确认机器人已获取 drive 写入权限")
        file_token = res["data"]["file_token"]

    # 2. 创建导入任务
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{FEISHU_BASE}/drive/v1/import_tasks",
            headers={"Authorization": f"Bearer {app_token}"},
            json={"file_extension": "md", "file_token": file_token, "type": "docx",
                  "file_name": title, "point": {"mount_type": 1, "mount_key": ""}},
        )
        res = r.json()
        if res.get("code") != 0:
            raise RuntimeError(f"创建导入任务失败: {res}")
        ticket = res["data"]["ticket"]

    # 3. 轮询任务结果
    deadline = asyncio.get_event_loop().time() + 180
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            r = await client.get(
                f"{FEISHU_BASE}/drive/v1/import_tasks/{ticket}",
                headers={"Authorization": f"Bearer {app_token}"},
            )
            res = r.json()
            if res.get("code") != 0:
                raise RuntimeError(f"查询导入任务失败: {res}")
            task = res["data"]["result"]
            status = task.get("job_status")
            if status in (1, 2):
                if asyncio.get_event_loop().time() > deadline:
                    raise RuntimeError("导入任务超时")
                await asyncio.sleep(1.5)
                continue
            if status == 0 and task.get("token"):
                doc_token = task["token"]
                doc_type = task.get("type") or "docx"
                doc_url = task.get("url") or f"https://feishu.cn/{doc_type}/{doc_token}"
                break
            raise RuntimeError(f"导入失败: status={status} {task.get('job_error_msg','')}")

    # 4. 高亮块处理（best-effort，必须在转移所有权前做，确保机器人仍有编辑权限）
    if callout_sections:
        await apply_callout_sections(doc_token, callout_sections)

    # 5. 转移所有权给用户（best-effort）
    if open_id:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{FEISHU_BASE}/drive/v1/permissions/{doc_token}/members/transfer_owner",
                    headers={"Authorization": f"Bearer {app_token}",
                             "Content-Type": "application/json; charset=utf-8"},
                    params={"type": doc_type},
                    json={"member_type": "openid", "member_id": open_id},
                )
        except Exception:
            pass

    return doc_url, doc_token, doc_type


async def upload_pdf_via_bot(
    title: str,
    content: bytes,
    open_id: str | None = None,
) -> tuple[str, str, str]:
    """Upload a generated PDF to Feishu Drive and return (URL, file_token, type)."""
    safe_title = re.sub(r'[\\/:*?"<>|]', "_", (title or "调研报告").strip())[:120] or "调研报告"
    fname = f"{safe_title}.pdf"
    app_token = await _app_access_token()

    files = {"file": (fname, content, "application/pdf")}
    fdata = {"file_name": fname, "parent_type": "explorer", "parent_node": "", "size": str(len(content))}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"{FEISHU_BASE}/drive/v1/files/upload_all",
            headers={"Authorization": f"Bearer {app_token}"},
            files=files,
            data=fdata,
        )
        res = r.json()
        print(f"[feishu] upload_pdf response={res}")
        if res.get("code") != 0:
            raise RuntimeError(
                f"上传 PDF 失败 code={res.get('code')} {res.get('msg', '')}，请确认应用已开通 drive 文件写入权限"
            )
        data = res.get("data") or {}
        file_token = data.get("file_token")
        if not file_token:
            raise RuntimeError(f"上传 PDF 后未返回 file_token: {res}")

    file_url = data.get("url") or data.get("file_url") or f"https://feishu.cn/file/{file_token}"

    if open_id:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{FEISHU_BASE}/drive/v1/permissions/{file_token}/members/transfer_owner",
                    headers={
                        "Authorization": f"Bearer {app_token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    params={"type": "file"},
                    json={"member_type": "openid", "member_id": open_id},
                )
        except Exception as e:
            print(f"[feishu] transfer PDF owner failed: {e}")

    return file_url, file_token, "file"


async def send_message_to_user(open_id: str, text: str) -> None:
    """用机器人发送文本消息给指定用户。"""
    import json as _json
    try:
        app_token = await _app_access_token()
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{FEISHU_BASE}/im/v1/messages",
                params={"receive_id_type": "open_id"},
                headers={"Authorization": f"Bearer {app_token}",
                         "Content-Type": "application/json; charset=utf-8"},
                json={"receive_id": open_id, "msg_type": "text",
                      "content": _json.dumps({"text": text}, ensure_ascii=False)},
            )
    except Exception as e:
        print(f"[feishu] send message failed: {e}")


# ── 用用户身份创建文档（旧方案，保留备用）────────────────────────

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
            code = r.get("code")
            msg = r.get("msg", "")
            hint = ""
            if code in (99991663, 99991664, 230003, 99991400):
                hint = "（可能原因：drive 文件权限未开通，请在飞书开放平台为应用申请 drive:drive:write 权限并审批）"
            raise RuntimeError(f"上传 md 到飞书云空间失败 code={code} msg={msg}{hint}")
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
            code = r.get("code")
            msg = r.get("msg", "")
            raise RuntimeError(
                "创建飞书云文档导入任务失败 "
                f"code={code} msg={msg}；请确认应用已开通“查看、创建云文档导入任务”权限，"
                "且当前用户已退出后重新登录授权"
            )
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
                code = r.get("code")
                msg = r.get("msg", "")
                raise RuntimeError(
                    "查询飞书云文档导入任务失败 "
                    f"code={code} msg={msg}；请确认应用已开通“查看、创建云文档导入任务”权限，"
                    "且当前用户已退出后重新登录授权"
                )
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
_HEADING_KEYS = ("heading1", "heading2", "heading3", "heading4", "heading5", "heading6")


def _text_block(content: str, block_type: int = 2) -> dict:
    key = {2: "text", 12: "bullet"}.get(block_type, "text")
    return {
        "block_type": block_type,
        key: {"elements": [{"text_run": {"content": content}}], "style": {}},
    }


def _block_text(block: dict) -> str:
    for key in (*_HEADING_KEYS, "text", "bullet", "ordered"):
        data = block.get(key)
        if isinstance(data, dict):
            return "".join(e.get("text_run", {}).get("content", "") for e in data.get("elements", []))
    return ""


def _heading_level(block: dict) -> int:
    for i, key in enumerate(_HEADING_KEYS, 1):
        if key in block:
            return i
    return 0


async def _list_doc_blocks(client: httpx.AsyncClient, headers: dict, doc_token: str) -> list[dict]:
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
            return []
        blocks.extend(r["data"]["items"])
        page_token = r["data"].get("page_token") or ""
        if not r["data"].get("has_more"):
            break
    return blocks


async def _replace_section_with_callout(
    client: httpx.AsyncClient,
    headers: dict,
    doc_token: str,
    title: str,
    lines: list[str],
    occurrence: int = 1,
) -> bool:
    blocks = await _list_doc_blocks(client, headers, doc_token)
    if not blocks:
        return False

    start = -1
    start_level = 0
    seen = 0
    for i, block in enumerate(blocks):
        level = _heading_level(block)
        if level and _block_text(block).strip() == title:
            seen += 1
            if seen == occurrence:
                start = i
                start_level = level
                break
    if start < 0:
        return False

    end = len(blocks)
    for j in range(start + 1, len(blocks)):
        level = _heading_level(blocks[j])
        if level and level <= start_level:
            end = j
            break

    section = blocks[start:end]
    section_ids = [b["block_id"] for b in section]
    root_id = doc_token
    root_children = [b["block_id"] for b in blocks]
    try:
        insert_index = root_children.index(section_ids[0])
        last_index = root_children.index(section_ids[-1])
    except ValueError:
        return False
    contiguous = (last_index - insert_index + 1) == len(section_ids)

    resp = await client.post(
        f"{FEISHU_BASE}/docx/v1/documents/{doc_token}/blocks/{root_id}/children",
        headers=headers,
        json={"index": insert_index, "children": [{"block_type": _BT_CALLOUT, "callout": {}}]},
    )
    r = resp.json()
    if r.get("code") != 0:
        return False
    callout_id = r["data"]["children"][0]["block_id"]

    children = [_text_block(title)]
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith(("- ", "* ", "•")):
            children.append(_text_block(s.lstrip("-*• ").strip(), 12))
        else:
            children.append(_text_block(s))
    if len(children) == 1:
        for block in section[1:]:
            text = _block_text(block).strip()
            if text:
                children.append(_text_block(text, 12 if block.get("bullet") else 2))

    await client.post(
        f"{FEISHU_BASE}/docx/v1/documents/{doc_token}/blocks/{callout_id}/children",
        headers=headers,
        json={"children": children},
    )

    if contiguous:
        await client.request(
            "DELETE",
            f"{FEISHU_BASE}/docx/v1/documents/{doc_token}/blocks/{root_id}/children/batch_delete",
            headers=headers,
            json={"start_index": insert_index + 1, "end_index": last_index + 2},
        )
    return True


async def apply_callout_sections(doc_token: str, sections: list[dict]) -> bool:
    """把指定标题段落转为飞书高亮块。best-effort，失败不影响文档可用。"""
    if not sections:
        return False
    try:
        app_token = await _app_access_token()
        headers = {"Authorization": f"Bearer {app_token}",
                   "Content-Type": "application/json; charset=utf-8"}
        changed = False
        async with httpx.AsyncClient(timeout=15) as client:
            for section in reversed(sections):
                changed = await _replace_section_with_callout(
                    client,
                    headers,
                    doc_token,
                    section.get("title", ""),
                    section.get("lines", []),
                    max(1, int(section.get("occurrence") or 1)),
                ) or changed
        return changed
    except Exception as e:
        print(f"[feishu] apply callout failed: {e}")
        return False


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
