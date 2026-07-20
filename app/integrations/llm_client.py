"""定性报告写作使用的直连 LLM 网关客户端。

每次调用会先在服务端完整缓冲一轮输出。只有整轮成功后，上层才会把内容
推给前端，因此连接中途断开时可以安全重试，不会把重复或残缺章节混入报告。

网关同时暴露多种协议：Claude 优先使用 Anthropic Messages，其他模型优先
使用 OpenAI Responses；若网关明确报告协议不兼容，会自动尝试下一种协议。
"""
import asyncio
import json
from dataclasses import dataclass
from typing import Literal

import httpx

from app.core.config import (
    LLM_API_BASE,
    LLM_API_KEY,
    LLM_CONNECT_TIMEOUT,
    LLM_READ_TIMEOUT,
    LLM_REPORT_FALLBACK_MODELS,
    LLM_REPORT_MAX_ATTEMPTS,
    LLM_REPORT_MAX_TOKENS,
    LLM_REPORT_MODEL,
)


_Protocol = Literal["messages", "responses", "chat"]


@dataclass
class _LLMRequestError(RuntimeError):
    message: str
    retryable: bool = False
    status_code: int | None = None
    endpoint_incompatible: bool = False

    def __str__(self) -> str:
        return self.message


def _configured_models() -> list[str]:
    models = []
    for model in (LLM_REPORT_MODEL, *LLM_REPORT_FALLBACK_MODELS):
        model = str(model or "").strip()
        if model and model not in models:
            models.append(model)
    return models


def _protocol_order(model: str) -> tuple[_Protocol, ...]:
    if "claude" in model.lower():
        return ("messages", "responses", "chat")
    return ("responses", "chat", "messages")


def _safe_error_text(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw or "")
    if LLM_API_KEY:
        text = text.replace(LLM_API_KEY, "***")
    return " ".join(text.split())[:800]


def _content_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def _split_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    system_parts: list[str] = []
    conversation: list[dict] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = _content_text(message.get("content"))
        if role in {"system", "developer"}:
            if content:
                system_parts.append(content)
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        conversation.append({"role": role, "content": content})
    return "\n\n".join(system_parts), conversation


def _endpoint_incompatible(status: int, body: str) -> bool:
    lowered = body.lower()
    markers = (
        "no model_info.compatible",
        "upstream configured for endpoint",
        "not supported for this endpoint",
        "unsupported endpoint",
        "method not allowed",
    )
    return status in {404, 405} or any(marker in lowered for marker in markers)


def _http_error(model: str, protocol: _Protocol, status: int, body: str) -> _LLMRequestError:
    incompatible = _endpoint_incompatible(status, body)
    retryable = not incompatible and (status in {408, 409, 425, 429} or status >= 500)
    return _LLMRequestError(
        f"LLM HTTP {status} model={model} protocol={protocol}: {body or 'empty response'}",
        retryable=retryable,
        status_code=status,
        endpoint_incompatible=incompatible,
    )


def _chat_chunk_text(data: dict) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
    text = _content_text(delta.get("content"))
    if text:
        return text
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    return _content_text(message.get("content"))


def _responses_text(response: dict) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str):
        return direct
    parts: list[str] = []
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


async def _request_chat(
    client: httpx.AsyncClient,
    messages: list[dict],
    model: str,
) -> str:
    protocol: _Protocol = "chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": LLM_REPORT_MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{LLM_API_BASE}/chat/completions"

    async with client.stream("POST", url, headers=headers, json=payload) as response:
        if response.status_code >= 400:
            body = _safe_error_text(await response.aread())
            raise _http_error(model, protocol, response.status_code, body)

        chunks: list[str] = []
        finish_reason = ""
        async for raw_line in response.aiter_lines():
            line = raw_line.strip()
            if not line or line.startswith(":") or line.startswith("event:"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("error"):
                error_text = _safe_error_text(json.dumps(data["error"], ensure_ascii=False))
                raise _LLMRequestError(
                    f"LLM stream error model={model} protocol={protocol}: {error_text}",
                    retryable=True,
                )
            choices = data.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                reason = choices[0].get("finish_reason")
                if reason:
                    finish_reason = str(reason)
            text = _chat_chunk_text(data)
            if text:
                chunks.append(text)

    answer = "".join(chunks).strip()
    if finish_reason in {"length", "content_filter"}:
        raise _LLMRequestError(
            f"LLM stopped before completing output model={model} protocol={protocol}; "
            f"finish_reason={finish_reason}",
            status_code=400,
        )
    if not answer:
        raise _LLMRequestError(
            f"LLM returned empty output model={model} protocol={protocol}",
            retryable=True,
        )
    return answer


async def _request_messages(
    client: httpx.AsyncClient,
    messages: list[dict],
    model: str,
) -> str:
    protocol: _Protocol = "messages"
    system, conversation = _split_messages(messages)
    payload = {
        "model": model,
        "messages": conversation,
        "stream": True,
        "max_tokens": LLM_REPORT_MAX_TOKENS,
    }
    if system:
        payload["system"] = system
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "x-api-key": LLM_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    url = f"{LLM_API_BASE}/messages"

    async with client.stream("POST", url, headers=headers, json=payload) as response:
        if response.status_code >= 400:
            body = _safe_error_text(await response.aread())
            raise _http_error(model, protocol, response.status_code, body)

        chunks: list[str] = []
        stop_reason = ""
        async for raw_line in response.aiter_lines():
            line = raw_line.strip()
            if not line or line.startswith(":") or line.startswith("event:"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            event_type = str(data.get("type") or "")
            if event_type == "error" or data.get("error"):
                error = data.get("error") or data
                error_text = _safe_error_text(json.dumps(error, ensure_ascii=False))
                raise _LLMRequestError(
                    f"LLM stream error model={model} protocol={protocol}: {error_text}",
                    retryable=True,
                )
            if event_type == "content_block_delta":
                delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
                text = delta.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            elif event_type == "message_delta":
                delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
                if delta.get("stop_reason"):
                    stop_reason = str(delta["stop_reason"])
            elif not event_type and isinstance(data.get("content"), list):
                text = _content_text(data["content"])
                if text:
                    chunks.append(text)
                if data.get("stop_reason"):
                    stop_reason = str(data["stop_reason"])

    answer = "".join(chunks).strip()
    if stop_reason in {"max_tokens", "refusal"}:
        raise _LLMRequestError(
            f"LLM stopped before completing output model={model} protocol={protocol}; "
            f"stop_reason={stop_reason}",
            status_code=400,
        )
    if not answer:
        raise _LLMRequestError(
            f"LLM returned empty output model={model} protocol={protocol}",
            retryable=True,
        )
    return answer


async def _request_responses(
    client: httpx.AsyncClient,
    messages: list[dict],
    model: str,
) -> str:
    protocol: _Protocol = "responses"
    instructions, conversation = _split_messages(messages)
    payload = {
        "model": model,
        "input": conversation,
        "stream": True,
        "max_output_tokens": LLM_REPORT_MAX_TOKENS,
    }
    if instructions:
        payload["instructions"] = instructions
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{LLM_API_BASE}/responses"

    async with client.stream("POST", url, headers=headers, json=payload) as response:
        if response.status_code >= 400:
            body = _safe_error_text(await response.aread())
            raise _http_error(model, protocol, response.status_code, body)

        chunks: list[str] = []
        final_text = ""
        incomplete_reason = ""
        async for raw_line in response.aiter_lines():
            line = raw_line.strip()
            if not line or line.startswith(":") or line.startswith("event:"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            event_type = str(data.get("type") or "")
            if event_type == "response.output_text.delta":
                delta = data.get("delta")
                if isinstance(delta, str):
                    chunks.append(delta)
            elif event_type in {"response.failed", "error"} or data.get("error"):
                error = data.get("error") or data.get("response") or data
                error_text = _safe_error_text(json.dumps(error, ensure_ascii=False))
                raise _LLMRequestError(
                    f"LLM stream error model={model} protocol={protocol}: {error_text}",
                    retryable=True,
                )
            elif event_type in {"response.completed", "response.incomplete"}:
                completed = data.get("response") if isinstance(data.get("response"), dict) else {}
                if not chunks:
                    final_text = _responses_text(completed)
                if event_type == "response.incomplete" or completed.get("status") == "incomplete":
                    details = completed.get("incomplete_details")
                    if isinstance(details, dict):
                        incomplete_reason = str(details.get("reason") or "incomplete")
                    else:
                        incomplete_reason = "incomplete"
            elif not event_type:
                if data.get("status") == "incomplete":
                    details = data.get("incomplete_details")
                    if isinstance(details, dict):
                        incomplete_reason = str(details.get("reason") or "incomplete")
                    else:
                        incomplete_reason = "incomplete"
                if not chunks:
                    final_text = _responses_text(data)

    answer = ("".join(chunks) or final_text).strip()
    if incomplete_reason:
        raise _LLMRequestError(
            f"LLM stopped before completing output model={model} protocol={protocol}; "
            f"reason={incomplete_reason}",
            status_code=400,
        )
    if not answer:
        raise _LLMRequestError(
            f"LLM returned empty output model={model} protocol={protocol}",
            retryable=True,
        )
    return answer


async def _request_once(
    client: httpx.AsyncClient,
    messages: list[dict],
    model: str,
    protocol: _Protocol,
) -> str:
    if protocol == "messages":
        return await _request_messages(client, messages, model)
    if protocol == "responses":
        return await _request_responses(client, messages, model)
    return await _request_chat(client, messages, model)


async def collect_chat_completion(messages: list[dict]) -> tuple[str, str]:
    """返回完整回答和实际模型；失败轮次不会向调用方暴露半截文本。"""
    if not LLM_API_BASE:
        raise RuntimeError("未配置 LLM_API_BASE")
    if not LLM_API_KEY:
        raise RuntimeError("未配置 LLM_API_KEY")
    models = _configured_models()
    if not models:
        raise RuntimeError("未配置 LLM_REPORT_MODEL")

    timeout = httpx.Timeout(
        connect=LLM_CONNECT_TIMEOUT,
        read=LLM_READ_TIMEOUT,
        write=60.0,
        pool=LLM_CONNECT_TIMEOUT,
    )
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for model in models:
            for protocol in _protocol_order(model):
                switch_protocol = False
                for attempt in range(1, LLM_REPORT_MAX_ATTEMPTS + 1):
                    try:
                        answer = await _request_once(client, messages, model, protocol)
                        return answer, model
                    except _LLMRequestError as exc:
                        last_error = exc
                        if exc.endpoint_incompatible:
                            switch_protocol = True
                            break
                        if not exc.retryable:
                            break
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        last_error = exc

                    if attempt < LLM_REPORT_MAX_ATTEMPTS:
                        await asyncio.sleep(min(8, 2 ** (attempt - 1)))

                if not switch_protocol:
                    break

    detail = _safe_error_text(str(last_error or "unknown error"))
    raise RuntimeError(
        f"LLM report generation failed after retries; models={','.join(models)}; last_error={detail}"
    ) from last_error
