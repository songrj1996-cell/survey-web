import asyncio
import json
from typing import AsyncGenerator

import httpx

from app.core.config import DIFY_API_BASE, DIFY_API_KEY


async def chat(
    query: str,
    user: str,
    conversation_id: str = "",
    api_key: str | None = None,
) -> tuple[str, str]:
    """调用 Dify chat-messages（streaming）。返回 (answer, conversation_id)。

    api_key 不传时用 .env 里默认的 DIFY_API_KEY。多业务时由 main 传入对应应用的 key。
    """
    api_key = api_key or DIFY_API_KEY
    if not api_key:
        raise RuntimeError("缺少 Dify API Key")

    answer_parts: list[str] = []
    new_conv_id = conversation_id

    payload = {
        "inputs": {},
        "query": query,
        "response_mode": "streaming",
        "user": user,
        "conversation_id": conversation_id,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    print(
        f"[dify] -> POST | conv_id={conversation_id or '(new)'} | "
        f"query_len={len(query)}"
    )

    async with httpx.AsyncClient(timeout=1800, follow_redirects=True) as client:
        async with client.stream(
            "POST",
            f"{DIFY_API_BASE}/chat-messages",
            headers=headers,
            json=payload,
        ) as resp:
            if resp.status_code >= 400:
                err_body = await resp.aread()
                raise RuntimeError(
                    f"Dify {resp.status_code}: {err_body.decode('utf-8', errors='replace')}"
                )
            msg_chunks = 0
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                event = data.get("event")
                if event in ("message", "agent_message"):
                    answer_parts.append(data.get("answer", ""))
                    msg_chunks += 1
                if data.get("conversation_id"):
                    new_conv_id = data["conversation_id"]
                if event == "error":
                    raise RuntimeError(
                        f"Dify error: {data.get('code')} {data.get('message')}"
                    )
                if event in ("message_end", "workflow_finished", "agent_message_end"):
                    break

    total_chars = sum(len(p) for p in answer_parts)
    print(f"[dify] <- done | chunks={msg_chunks} | answer_len={total_chars}")
    return "".join(answer_parts), new_conv_id


# 熔断信号：Dify 返回 400 时，表示请求结构 / 变量类型有问题，重试也救不了，
# 让上层立即停整个批量任务，避免空跑几百次。
STOP_SIGNAL = "STOP_SIGNAL"


def _stringify_workflow_output(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _pick_workflow_output(outputs: dict) -> tuple[str, str]:
    """Return (text, source_key) from Dify workflow outputs.

    Older configuration expects `output`, but workflow apps are often published
    with variables such as `result`, `answer`, or `text`. Treat a single
    arbitrary output variable as valid so renaming in Dify does not silently
    erase the result.
    """
    if not isinstance(outputs, dict) or not outputs:
        return "", ""

    first_present: tuple[str, str] | None = None
    for key in ("output", "result", "answer", "text"):
        if key in outputs:
            text = _stringify_workflow_output(outputs.get(key)).strip()
            if first_present is None:
                first_present = (text, key)
            if text:
                return text, key

    if len(outputs) == 1:
        key, value = next(iter(outputs.items()))
        return _stringify_workflow_output(value).strip(), str(key)

    for key, value in outputs.items():
        text = _stringify_workflow_output(value).strip()
        if text:
            return text, str(key)
    return first_present or ("", "")


async def complete(
    inputs: dict,
    query: str,
    api_key: str,
    user: str = "bot",
    max_retries: int = 3,
    log_prefix: str = "",
) -> str:
    """调用 Dify completion-messages（一次性请求，仍走流式收响应）。

    返回完整的 answer 字符串。400 → 返回 STOP_SIGNAL（上层应该立刻停整批任务）；
    429 / 5xx / 网络错误 → 指数退避重试；重试耗尽返回空串。
    """
    if not api_key:
        raise RuntimeError("缺少 Dify API Key")

    payload = {
        "inputs": inputs,
        "query": query,
        "response_mode": "streaming",
        "user": user,
    }
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }
    url = f"{DIFY_API_BASE}/completion-messages"

    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        for attempt in range(max_retries):
            try:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code == 400:
                        body = await resp.aread()
                        snippet = body.decode("utf-8", errors="replace")[:300]
                        print(f"[dify] {log_prefix} 400 STOP_SIGNAL: {snippet}")
                        return STOP_SIGNAL
                    if resp.status_code == 429 or resp.status_code >= 500:
                        wait = min(2 ** attempt, 8)
                        print(
                            f"[dify] {log_prefix} {resp.status_code}; retry in {wait}s"
                        )
                        await asyncio.sleep(wait)
                        continue
                    if resp.status_code != 200:
                        body = await resp.aread()
                        snippet = body.decode("utf-8", errors="replace")[:200]
                        print(f"[dify] {log_prefix} {resp.status_code} give up: {snippet}")
                        return ""

                    full_ans = ""
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            chunk = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("event") in ("message", "agent_message"):
                            full_ans += chunk.get("answer") or chunk.get("text") or ""
                    return full_ans.strip()
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                wait = min(2 ** attempt, 8)
                print(f"[dify] {log_prefix} network err: {e}; retry in {wait}s")
                await asyncio.sleep(wait)
            except Exception as e:
                if "client has been closed" in str(e):
                    return ""
                wait = min(2 ** attempt, 8)
                print(f"[dify] {log_prefix} exc: {e}; retry in {wait}s")
                await asyncio.sleep(wait)

    print(f"[dify] {log_prefix} retry exhausted")
    return ""


async def workflow_run(
    inputs: dict,
    api_key: str,
    user: str = "batch",
    max_retries: int = 3,
    log_prefix: str = "",
) -> str:
    """调用 Dify Workflow 类型应用（/workflows/run，blocking 模式）。

    返回 outputs.output 字段的字符串。400 → 返回 STOP_SIGNAL；
    429 / 5xx / 网络错误 → 指数退避重试；重试耗尽返回空串。
    """
    if not api_key:
        raise RuntimeError("缺少 Dify API Key")

    payload = {
        "inputs": inputs,
        "response_mode": "blocking",
        "user": user,
    }
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }
    url = f"{DIFY_API_BASE}/workflows/run"

    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        for attempt in range(max_retries):
            try:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 400:
                    snippet = resp.text[:300]
                    print(f"[dify] {log_prefix} 400 STOP_SIGNAL: {snippet}")
                    return STOP_SIGNAL
                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = min(2 ** attempt, 8)
                    print(f"[dify] {log_prefix} {resp.status_code}; retry in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                if resp.status_code != 200:
                    snippet = resp.text[:200]
                    print(f"[dify] {log_prefix} {resp.status_code} give up: {snippet}")
                    return ""

                data = resp.json()
                outputs = data.get("data", {}).get("outputs", {}) or {}
                output, output_key = _pick_workflow_output(outputs)
                keys = ",".join(str(k) for k in outputs.keys()) if isinstance(outputs, dict) else ""
                print(
                    f"[dify] {log_prefix} workflow done | "
                    f"output_key={output_key or '(none)'} | outputs=[{keys}] | "
                    f"output_len={len(output)}"
                )
                return output.strip()

            except (httpx.TimeoutException, httpx.NetworkError) as e:
                wait = min(2 ** attempt, 8)
                print(f"[dify] {log_prefix} network err: {e}; retry in {wait}s")
                await asyncio.sleep(wait)
            except Exception as e:
                wait = min(2 ** attempt, 8)
                print(f"[dify] {log_prefix} exc: {e}; retry in {wait}s")
                await asyncio.sleep(wait)

    print(f"[dify] {log_prefix} retry exhausted")
    return ""


async def sse_dify_stream(
    query: str,
    user_id: str,
    conversation_id: str,
    api_key: str,
) -> AsyncGenerator[tuple[str, str], None]:
    import httpx

    payload = {
        "inputs": {},
        "query": query,
        "response_mode": "streaming",
        "user": user_id,
        "conversation_id": conversation_id,
    }
    req_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    final_conv_id = conversation_id

    async with httpx.AsyncClient(timeout=1800, follow_redirects=True) as client:
        async with client.stream(
            "POST", f"{DIFY_API_BASE}/chat-messages",
            headers=req_headers, json=payload,
        ) as resp:
            if resp.status_code >= 400:
                err = await resp.aread()
                raise RuntimeError(f"Dify {resp.status_code}: {err.decode('utf-8', errors='replace')}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event = data.get("event")
                if event in ("message", "agent_message"):
                    yield data.get("answer", ""), ""
                if data.get("conversation_id"):
                    final_conv_id = data["conversation_id"]
                if event == "error":
                    raise RuntimeError(f"Dify error: {data.get('code')} {data.get('message')}")
                if event in ("message_end", "workflow_finished", "agent_message_end"):
                    break

    yield "", final_conv_id


async def sse_dify_completion_stream(
    query: str,
    user_id: str,
    api_key: str,
    input_var: str = "survey_batch",
) -> AsyncGenerator[str, None]:
    """调用 Dify completion-messages 端点（文本生成应用）。"""
    import httpx

    inputs = {input_var: query}
    for fallback_key in ("query", "text", "content"):
        inputs.setdefault(fallback_key, query)

    payload = {
        "inputs": inputs,
        "response_mode": "streaming",
        "user": user_id,
    }
    req_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=1800, follow_redirects=True) as client:
        async with client.stream(
            "POST", f"{DIFY_API_BASE}/completion-messages",
            headers=req_headers, json=payload,
        ) as resp:
            if resp.status_code >= 400:
                err = await resp.aread()
                raise RuntimeError(f"Dify {resp.status_code}: {err.decode('utf-8', errors='replace')}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event = data.get("event")
                if event == "message":
                    yield data.get("answer", "")
                if event == "error":
                    raise RuntimeError(f"Dify error: {data.get('code')} {data.get('message')}")
                if event in ("message_end", "workflow_finished"):
                    break


def _looks_like_dify_endpoint_mismatch(err: Exception) -> bool:
    text = str(err).lower()
    return any(k in text for k in [
        "completion-messages",
        "chat-messages",
        "app mode",
        "app_mode",
        "not support",
        "not supported",
        "invalid_param",
        "endpoint",
        "completion app",
        "chat app",
    ])


async def call_dify_compatible(
    query: str,
    user_id: str,
    api_key: str,
    conversation_id: str = "",
) -> tuple[str, str, str, str]:
    """优先按 chat 应用调用；若疑似端点/应用类型不匹配，自动改用 completion 应用。"""
    try:
        chunks: list[str] = []
        final_conv = conversation_id
        async for chunk, conv_id in sse_dify_stream(query, user_id, conversation_id, api_key):
            if chunk:
                chunks.append(chunk)
            if conv_id:
                final_conv = conv_id
        return "".join(chunks), final_conv, "chat", ""
    except Exception as e:
        if not _looks_like_dify_endpoint_mismatch(e):
            raise
        fallback_reason = str(e)

    chunks: list[str] = []
    async for chunk in sse_dify_completion_stream(query, user_id, api_key):
        if chunk:
            chunks.append(chunk)
    return "".join(chunks), "", "completion", fallback_reason
