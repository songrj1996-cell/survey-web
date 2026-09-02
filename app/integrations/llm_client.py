"""定性报告写作使用的直连 LLM 网关客户端。

每次调用会先在服务端完整缓冲一轮输出。只有整轮成功后，上层才会把内容
推给前端，因此连接中途断开时可以安全重试，不会把重复或残缺章节混入报告。

网关同时暴露多种协议：Claude 优先使用 Anthropic Messages，其他模型优先
使用 OpenAI Responses；若网关明确报告协议不兼容，会自动尝试下一种协议。
"""
import asyncio
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from app.core.config import (
    FEISHU_LOGIN_REQUIRED,
    LLM_API_BASE,
    LLM_API_KEY,
    LLM_CONNECT_TIMEOUT,
    LLM_READ_TIMEOUT,
    LLM_REPORT_FALLBACK_MODELS,
    LLM_REPORT_MAX_ATTEMPTS,
    LLM_REPORT_MAX_TOKENS,
    LLM_REPORT_MODEL,
)
from app.core.llm_context import current_llm_api_key, current_llm_attempt_observer


_Protocol = Literal["messages", "responses", "chat"]
_AttemptEventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass
class _LLMRequestError(RuntimeError):
    message: str
    retryable: bool = False
    status_code: int | None = None
    endpoint_incompatible: bool = False
    chat_usage_option_incompatible: bool = False

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class _LLMResult:
    answer: str
    response_model: str | None = None
    usage: dict[str, int] | None = None
    usage_complete: bool = False


@dataclass
class _AttemptObservation:
    """Upstream facts observed before one HTTP attempt reaches a terminal state."""

    response_model: str | None = None
    usage: dict[str, int] | None = None
    usage_complete: bool = False


def _configured_models(model_overrides=None) -> list[str]:
    models = []
    candidates = model_overrides if model_overrides is not None else (
        LLM_REPORT_MODEL,
        *LLM_REPORT_FALLBACK_MODELS,
    )
    for model in candidates:
        model = str(model or "").strip()
        if model and model not in models:
            models.append(model)
    return models


def _protocol_order(model: str) -> tuple[_Protocol, ...]:
    if "claude" in model.lower():
        return ("messages", "responses", "chat")
    return ("responses", "chat", "messages")


def _safe_error_text(raw: bytes | str, api_key: str = "") -> str:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw or "")
    for secret in {str(api_key or "").strip(), LLM_API_KEY}:
        if secret:
            text = text.replace(secret, "***")
    return " ".join(text.split())[:800]


def _token_count(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _normalized_usage(value) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    input_tokens = _token_count(value.get("input_tokens"))
    if input_tokens is None:
        input_tokens = _token_count(value.get("prompt_tokens"))
    output_tokens = _token_count(value.get("output_tokens"))
    if output_tokens is None:
        output_tokens = _token_count(value.get("completion_tokens"))
    if input_tokens is None or output_tokens is None:
        return None
    total_tokens = _token_count(value.get("total_tokens"))
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


async def _emit_attempt_event(
    callback: _AttemptEventCallback | None,
    event: dict[str, Any],
) -> None:
    if callback is None:
        return
    try:
        result = callback(dict(event))
        if inspect.isawaitable(result):
            await result
    except Exception:
        # Usage/progress instrumentation must never make a successful LLM call fail.
        return


async def _emit_attempt_events(
    callback: _AttemptEventCallback | None,
    event: dict[str, Any],
) -> None:
    context_observer = current_llm_attempt_observer()
    await _emit_attempt_event(context_observer, event)
    if callback is not context_observer:
        await _emit_attempt_event(callback, event)


def _chat_usage_option_incompatible(status: int, body: str) -> bool:
    if status not in {400, 422}:
        return False
    lowered = body.lower()
    field_named = "stream_options" in lowered or "include_usage" in lowered
    rejection_markers = (
        "unsupported",
        "not supported",
        "does not support",
        "unknown parameter",
        "unknown field",
        "unrecognized",
        "unexpected",
        "extra inputs",
        "extra fields",
        "not permitted",
        "not allowed",
        "invalid parameter",
    )
    return field_named and any(marker in lowered for marker in rejection_markers)


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
    max_tokens: int,
    observation: _AttemptObservation,
    *,
    api_key: str,
    include_usage: bool = True,
) -> _LLMResult:
    protocol: _Protocol = "chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
    }
    if include_usage:
        payload["stream_options"] = {"include_usage": True}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = f"{LLM_API_BASE}/chat/completions"

    async with client.stream(
        "POST",
        url,
        headers=headers,
        json=payload,
    ) as response:
        if response.status_code >= 400:
            body = _safe_error_text(await response.aread(), api_key)
            if include_usage and _chat_usage_option_incompatible(
                response.status_code,
                body,
            ):
                raise _LLMRequestError(
                    f"LLM HTTP {response.status_code} model={model} "
                    f"protocol={protocol}: {body or 'empty response'}",
                    status_code=response.status_code,
                    chat_usage_option_incompatible=True,
                )
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
                error_text = _safe_error_text(
                    json.dumps(data["error"], ensure_ascii=False),
                    api_key,
                )
                raise _LLMRequestError(
                    f"LLM stream error model={model} protocol={protocol}: {error_text}",
                    retryable=True,
                )
            if isinstance(data.get("model"), str) and data["model"].strip():
                observation.response_model = data["model"].strip()
            choices = data.get("choices")
            chunk_usage = _normalized_usage(data.get("usage"))
            if chunk_usage is not None:
                observation.usage = chunk_usage
                # With stream_options.include_usage, only the trailing chunk with
                # an empty choices list is the final usage snapshot.
                observation.usage_complete = (
                    isinstance(choices, list) and not choices
                )
            if (
                isinstance(choices, list)
                and choices
                and isinstance(choices[0], dict)
            ):
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
    return _LLMResult(
        answer=answer,
        response_model=observation.response_model,
        usage=observation.usage if include_usage else None,
        usage_complete=bool(include_usage and observation.usage_complete),
    )


async def _request_messages(
    client: httpx.AsyncClient,
    messages: list[dict],
    model: str,
    max_tokens: int,
    reasoning_effort: str | None,
    observation: _AttemptObservation,
    *,
    api_key: str,
) -> _LLMResult:
    protocol: _Protocol = "messages"
    system, conversation = _split_messages(messages)
    payload = {
        "model": model,
        "messages": conversation,
        "stream": True,
        "max_tokens": max_tokens,
    }
    if system:
        payload["system"] = system
    if reasoning_effort and model.lower().startswith(
        ("claude-sonnet-5", "claude-opus-5", "claude-fable-5")
    ):
        payload["thinking"] = {"type": "adaptive"}
        payload["output_config"] = {"effort": reasoning_effort}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    url = f"{LLM_API_BASE}/messages"

    async with client.stream("POST", url, headers=headers, json=payload) as response:
        if response.status_code >= 400:
            body = _safe_error_text(await response.aread(), api_key)
            raise _http_error(model, protocol, response.status_code, body)

        chunks: list[str] = []
        stop_reason = ""
        input_tokens: int | None = None
        output_tokens: int | None = None
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
                error_text = _safe_error_text(
                    json.dumps(error, ensure_ascii=False),
                    api_key,
                )
                raise _LLMRequestError(
                    f"LLM stream error model={model} protocol={protocol}: {error_text}",
                    retryable=True,
                )
            if event_type == "message_start":
                message = (
                    data.get("message")
                    if isinstance(data.get("message"), dict)
                    else {}
                )
                if isinstance(message.get("model"), str) and message["model"].strip():
                    observation.response_model = message["model"].strip()
                start_usage = message.get("usage")
                if isinstance(start_usage, dict):
                    parsed_input = _token_count(start_usage.get("input_tokens"))
                    parsed_output = _token_count(start_usage.get("output_tokens"))
                    if parsed_input is not None:
                        input_tokens = parsed_input
                    if parsed_output is not None:
                        output_tokens = parsed_output
                    observed_usage = _normalized_usage({
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    })
                    if observed_usage is not None:
                        observation.usage = observed_usage
                        observation.usage_complete = False
            elif event_type == "content_block_delta":
                delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
                text = delta.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            elif event_type == "message_delta":
                delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
                if delta.get("stop_reason"):
                    stop_reason = str(delta["stop_reason"])
                delta_usage = data.get("usage")
                if isinstance(delta_usage, dict):
                    parsed_input = _token_count(delta_usage.get("input_tokens"))
                    parsed_output = _token_count(delta_usage.get("output_tokens"))
                    if parsed_input is not None:
                        input_tokens = parsed_input
                    if parsed_output is not None:
                        # Anthropic message_delta output usage is cumulative.
                        output_tokens = parsed_output
                    observed_usage = _normalized_usage({
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    })
                    if observed_usage is not None:
                        observation.usage = observed_usage
                        observation.usage_complete = bool(delta.get("stop_reason"))
            elif not event_type and isinstance(data.get("content"), list):
                text = _content_text(data["content"])
                if text:
                    chunks.append(text)
                if data.get("stop_reason"):
                    stop_reason = str(data["stop_reason"])
                if isinstance(data.get("model"), str) and data["model"].strip():
                    observation.response_model = data["model"].strip()
                raw_usage = data.get("usage")
                if isinstance(raw_usage, dict):
                    parsed_input = _token_count(raw_usage.get("input_tokens"))
                    parsed_output = _token_count(raw_usage.get("output_tokens"))
                    if parsed_input is not None:
                        input_tokens = parsed_input
                    if parsed_output is not None:
                        output_tokens = parsed_output
                    observed_usage = _normalized_usage({
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    })
                    if observed_usage is not None:
                        observation.usage = observed_usage
                        observation.usage_complete = True

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
    usage = _normalized_usage(
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    )
    return _LLMResult(
        answer=answer,
        response_model=observation.response_model,
        usage=usage,
        usage_complete=observation.usage_complete,
    )


async def _request_responses(
    client: httpx.AsyncClient,
    messages: list[dict],
    model: str,
    max_tokens: int,
    reasoning_effort: str | None,
    observation: _AttemptObservation,
    *,
    api_key: str,
) -> _LLMResult:
    protocol: _Protocol = "responses"
    instructions, conversation = _split_messages(messages)
    payload = {
        "model": model,
        "input": conversation,
        "stream": True,
        "max_output_tokens": max_tokens,
    }
    if instructions:
        payload["instructions"] = instructions
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = f"{LLM_API_BASE}/responses"

    async with client.stream("POST", url, headers=headers, json=payload) as response:
        if response.status_code >= 400:
            body = _safe_error_text(await response.aread(), api_key)
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
            response_data = (
                data.get("response")
                if isinstance(data.get("response"), dict)
                else data
            )
            if (
                isinstance(response_data.get("model"), str)
                and response_data["model"].strip()
            ):
                observation.response_model = response_data["model"].strip()
            response_usage = _normalized_usage(response_data.get("usage"))
            if response_usage is not None:
                observation.usage = response_usage
                observation.usage_complete = (
                    event_type in {
                        "response.completed",
                        "response.incomplete",
                        "response.failed",
                    }
                    or str(response_data.get("status") or "").strip().lower()
                    in {"completed", "incomplete", "failed"}
                )
            if event_type == "response.output_text.delta":
                delta = data.get("delta")
                if isinstance(delta, str):
                    chunks.append(delta)
            elif event_type in {"response.failed", "error"} or data.get("error"):
                error = data.get("error") or data.get("response") or data
                error_text = _safe_error_text(
                    json.dumps(error, ensure_ascii=False),
                    api_key,
                )
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
    return _LLMResult(
        answer=answer,
        response_model=observation.response_model,
        usage=observation.usage,
        usage_complete=observation.usage_complete,
    )


async def _request_once(
    client: httpx.AsyncClient,
    messages: list[dict],
    model: str,
    protocol: _Protocol,
    max_tokens: int,
    reasoning_effort: str | None,
    observation: _AttemptObservation,
    *,
    api_key: str,
    chat_include_usage: bool = True,
) -> _LLMResult:
    if protocol == "messages":
        return await _request_messages(
            client,
            messages,
            model,
            max_tokens,
            reasoning_effort,
            observation,
            api_key=api_key,
        )
    if protocol == "responses":
        return await _request_responses(
            client,
            messages,
            model,
            max_tokens,
            reasoning_effort,
            observation,
            api_key=api_key,
        )
    return await _request_chat(
        client,
        messages,
        model,
        max_tokens,
        observation,
        api_key=api_key,
        include_usage=chat_include_usage,
    )


async def collect_chat_completion(
    messages: list[dict],
    *,
    models=None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
    on_attempt_event: _AttemptEventCallback | None = None,
    api_key: str | None = None,
) -> tuple[str, str]:
    """返回完整回答和实际请求模型；失败轮次不会暴露半截文本。

    若提供 ``on_attempt_event``，每次真实 HTTP 请求都会发送一条 ``started``
    和一条 ``completed``/``failed``；同一对事件共享 ``call_id``，``attempt``
    则在本次 collect 调用内按真实请求顺序递增。
    """
    if not LLM_API_BASE:
        raise RuntimeError("未配置 LLM_API_BASE")
    request_api_key = str(api_key or current_llm_api_key()).strip()
    if not request_api_key and not FEISHU_LOGIN_REQUIRED:
        request_api_key = LLM_API_KEY
    if not request_api_key:
        raise RuntimeError("未提供当前用户的 LLM API Key")
    configured_models = _configured_models(models)
    if not configured_models:
        raise RuntimeError("未配置 LLM_REPORT_MODEL")
    request_max_tokens = max(1024, int(max_tokens or LLM_REPORT_MAX_TOKENS))

    timeout = httpx.Timeout(
        connect=LLM_CONNECT_TIMEOUT,
        read=LLM_READ_TIMEOUT,
        write=60.0,
        pool=LLM_CONNECT_TIMEOUT,
    )
    last_error: Exception | None = None
    requested_model = configured_models[0]
    attempt_number = 0

    def _terminal_event(
        status: str,
        event_base: dict[str, Any],
        observation: _AttemptObservation,
        result: _LLMResult | None = None,
    ) -> dict[str, Any]:
        event = {
            "status": status,
            **event_base,
            "usage": result.usage if result is not None else observation.usage,
            "usage_complete": (
                result.usage_complete
                if result is not None
                else observation.usage_complete
            ),
        }
        response_model = (
            result.response_model if result is not None else observation.response_model
        )
        if response_model:
            event["response_model"] = response_model
        return event

    async with httpx.AsyncClient(timeout=timeout) as client:
        async def _run_http_attempt(
            model: str,
            model_index: int,
            protocol: _Protocol,
            *,
            chat_include_usage: bool = True,
        ) -> _LLMResult:
            nonlocal attempt_number
            attempt_number += 1
            event_base = {
                "call_id": uuid.uuid4().hex,
                "model": model,
                "requested_model": requested_model,
                "protocol": protocol,
                "fallback": model_index > 0,
                "attempt": attempt_number,
            }
            observation = _AttemptObservation()
            await _emit_attempt_events(
                on_attempt_event,
                {"status": "started", **event_base},
            )
            try:
                result = await _request_once(
                    client,
                    messages,
                    model,
                    protocol,
                    request_max_tokens,
                    reasoning_effort,
                    observation,
                    api_key=request_api_key,
                    chat_include_usage=chat_include_usage,
                )
            except asyncio.CancelledError:
                await _emit_attempt_events(
                    on_attempt_event,
                    _terminal_event("failed", event_base, observation),
                )
                raise
            except Exception:
                await _emit_attempt_events(
                    on_attempt_event,
                    _terminal_event("failed", event_base, observation),
                )
                raise

            await _emit_attempt_events(
                on_attempt_event,
                _terminal_event("completed", event_base, observation, result),
            )
            return result

        async def _request_with_chat_compatibility(
            model: str,
            model_index: int,
            protocol: _Protocol,
        ) -> _LLMResult:
            chat_include_usage = True
            while True:
                try:
                    return await _run_http_attempt(
                        model,
                        model_index,
                        protocol,
                        chat_include_usage=chat_include_usage,
                    )
                except _LLMRequestError as exc:
                    if (
                        protocol == "chat"
                        and chat_include_usage
                        and exc.chat_usage_option_incompatible
                    ):
                        chat_include_usage = False
                        continue
                    raise

        for model_index, model in enumerate(configured_models):
            for protocol in _protocol_order(model):
                switch_protocol = False
                for attempt in range(1, LLM_REPORT_MAX_ATTEMPTS + 1):
                    try:
                        result = await _request_with_chat_compatibility(
                            model,
                            model_index,
                            protocol,
                        )
                        return result.answer, model
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

    detail = _safe_error_text(
        str(last_error or "unknown error"),
        request_api_key,
    )
    raise RuntimeError(
        "LLM generation failed after retries; "
        f"models={','.join(configured_models)}; last_error={detail}"
    ) from last_error
