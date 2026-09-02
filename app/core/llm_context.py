"""请求级 LLM 凭据上下文。

ContextVar 会随 asyncio task 传播，同时在并发请求之间隔离，适合让一个
SSE 任务及其派生的并发子任务共享同一位用户的 API Key。
"""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator


LLMAttemptObserver = Callable[[dict[str, Any]], Any]


_request_llm_api_key: ContextVar[str] = ContextVar(
    "request_llm_api_key",
    default="",
)
_request_llm_attempt_observer: ContextVar[LLMAttemptObserver | None] = ContextVar(
    "request_llm_attempt_observer",
    default=None,
)


def current_llm_api_key() -> str:
    return _request_llm_api_key.get().strip()


def current_llm_attempt_observer() -> LLMAttemptObserver | None:
    return _request_llm_attempt_observer.get()


@contextmanager
def bind_llm_api_key(api_key: str) -> Iterator[None]:
    key = str(api_key or "").strip()
    if not key:
        raise RuntimeError("未提供 LLM API Key")
    token = _request_llm_api_key.set(key)
    try:
        yield
    finally:
        _request_llm_api_key.reset(token)


@contextmanager
def bind_llm_attempt_observer(
    observer: LLMAttemptObserver | None,
) -> Iterator[None]:
    token = _request_llm_attempt_observer.set(observer)
    try:
        yield
    finally:
        _request_llm_attempt_observer.reset(token)
