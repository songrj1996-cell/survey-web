"""请求级 LLM 凭据上下文。

ContextVar 会随 asyncio task 传播，同时在并发请求之间隔离，适合让一个
SSE 任务及其派生的并发子任务共享同一位用户的 API Key。
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any, Callable, Iterator


LLMAttemptObserver = Callable[[dict[str, Any]], Any]

# A/B only provides snapshot infrastructure. Business callers opt in during C.
_model_snapshot: ContextVar[dict | None] = ContextVar("llm_model_snapshot", default=None)


def current_llm_model_snapshot() -> dict | None:
    return deepcopy(_model_snapshot.get())


@contextmanager
def bind_llm_model_snapshot(snapshot: dict) -> Iterator[None]:
    token = _model_snapshot.set(deepcopy(snapshot))
    try:
        yield
    finally:
        _model_snapshot.reset(token)


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


_model_budget: ContextVar[dict | None] = ContextVar("llm_model_budget", default=None)


def current_llm_model_budget():
    # Intentionally shared across schema repairs within a single logical step.
    return _model_budget.get()


@contextmanager
def bind_llm_model_budget(budget):
    token = _model_budget.set(budget)
    try:
        yield
    finally:
        _model_budget.reset(token)
