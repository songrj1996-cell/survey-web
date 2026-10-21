"""Observe survey progress without changing generation, stored results or retries."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from functools import wraps
import json
import re
import time

from app.core import config
from app.core.runtime_logging import runtime_log
from app.integrations.llm_client import _attempt_error_category


_CURRENT = ContextVar("survey_runtime_progress", default=None)
_STAGES = {
    "prepare": "准备生成报告",
    "columns": "识别问卷题型",
    "plan": "制定分析方案",
    "plan_revision": "调整分析方案",
    "themes": "整理回答主题",
    "synthesis": "综合分析各项发现",
    "writing": "撰写报告",
    "writer": "撰写初稿",
    "registry": "整理报告中需要核对的内容",
    "meaning": "核对原意和引用",
    "quantity": "核对人数范围",
    "quick_statistics": "整理客观题统计",
    "quick_questions": "逐题整理主观回复",
    "finalize": "整理最终结果",
    "saving": "保存报告结果",
}


def _logging_only(method):
    @wraps(method)
    def guarded(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception:
            return None
    return guarded


def _failure_explanation(category, http_status=None):
    if category == "already_running":
        return "同一任务已经在生成报告", "请回到当前任务查看进度，不要重复启动"
    if category == "source_unavailable":
        return "原报告不存在或当前账号无权访问", "请检查所选历史记录和账号权限"
    if category == "output_truncated":
        return "模型输出达到本次长度上限，内容被截断", "请联系维护人员检查输出上限和本轮记录，再决定是否重新生成"
    if category == "authentication_error":
        return "模型服务拒绝了认证", "请检查个人模型凭据，必要时联系管理员"
    if category == "rate_limited" or http_status == 429:
        return "模型服务限制了本次请求", "请稍后再试；持续出现时联系管理员检查服务额度或并发限制"
    if category in {"timeout", "connect_timeout", "read_timeout", "write_timeout", "pool_timeout"}:
        return "在本次等待时限内未取得完整结果", "请检查页面结果和本轮记录后再决定是否重试；超时不代表没有产生模型费用"
    if category in {"transport_error", "server_error", "http_error"}:
        return "与模型服务通信未成功，具体故障原因尚未确认", "请稍后检查服务连接和本轮记录，必要时联系管理员"
    if category in {"empty_output", "incomplete_output", "protocol_incompatible"}:
        return "本次没有取得符合处理要求的完整结果", "请联系维护人员检查模型返回格式和本轮记录后再决定是否重试"
    return "具体原因尚未确认", "请提供同一任务编号、阶段和时间给维护人员排查，先检查页面结果，避免反复点击生成"


class SurveyProgressLog:
    """One request's last known stage. No report text or raw vendor errors are logged."""

    def __init__(self, session_id, kind="report", *, retry_failed=False, generation_kind=None):
        self.session_id = session_id
        self.run_id = None
        self.kind = kind
        self.stage = _STAGES.get(kind, _STAGES["prepare"])
        self.started = self.changed = self.last_wait = time.monotonic()
        self.ended = False
        self.last_progress = None
        self.last_question_counts = None
        self.retry_failed = retry_failed
        self.generation_kind = generation_kind

    @_logging_only
    def emit(self, level, message, **fields):
        runtime_log(
            "问卷报告", level, message, session_id=self.session_id, run_id=self.run_id,
            stage=self.stage, elapsed_seconds=max(0, time.monotonic() - self.started), **fields,
        )

    @_logging_only
    def start(self):
        action = "开始补全报告未完成项" if self.retry_failed else (
            "开始重新生成报告" if self.generation_kind == "regenerate" else "开始处理报告生成请求"
        )
        if self.kind != "report":
            action = "开始" + self.stage
        self.emit("progress", action)

    @_logging_only
    def enter(self, stage):
        name = _STAGES.get(stage)
        if not name or self.ended or name == self.stage:
            return
        self.stage = name
        self.changed = self.last_wait = time.monotonic()
        self.emit("progress", "正在" + name)

    @_logging_only
    def wait(self):
        now = time.monotonic()
        if self.ended or now - self.last_wait < config.LLM_STREAM_HEARTBEAT_SECONDS:
            return
        self.last_wait = now
        self.emit("info", f"仍在等待当前阶段的后续结果，本阶段已等待 {int(now - self.changed)} 秒；尚未收到完成结果，请勿重复提交")

    @_logging_only
    def fail(self, *, failure=None, error=None, prefix="当前处理未完成", **fields):
        if self.ended:
            return
        self.ended = True
        failure = failure if isinstance(failure, dict) else {}
        if self.stage == _STAGES["saving"]:
            message = "报告结果未能确认保存成功；请先检查历史版本，再联系维护人员排查保存步骤，避免直接重复生成"
        else:
            category = failure.get("error_category")
            if not category and error is not None:
                # Read structured exception facts, never parse a vendor's error body.
                cause = error
                for _ in range(8):
                    candidate = "timeout" if isinstance(cause, TimeoutError) else _attempt_error_category(cause)
                    if candidate != "unexpected_error":
                        category = candidate
                        failure = {"http_status": getattr(cause, "status_code", None)}
                        break
                    cause = cause.__cause__
                    if cause is None:
                        break
            reason, advice = _failure_explanation(category, failure.get("http_status"))
            message = f"{prefix}：{reason}。本次处理已结束，未安排后续自动重试。{advice}"
        error_type = type(error).__name__ if error is not None else failure.get("error_type")
        if isinstance(error_type, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,80}", error_type):
            fields.setdefault("error_type", error_type)
        if type(failure.get("http_status")) is int:
            fields.setdefault("http_status", failure["http_status"])
        if type(failure.get("output_cap")) is int and failure["output_cap"] > 0:
            fields.setdefault("output_limit", failure["output_cap"])
        self.emit("error", message, **fields)

    @_logging_only
    def cancelled(self):
        if not self.ended:
            self.ended = True
            self.emit("warning", "本次处理已停止；是否保留了可用结果请检查页面和历史版本，尚在途的模型请求可能仍产生费用")

    @_logging_only
    def observe(self, raw):
        if not isinstance(raw, str) or not raw.startswith("data: "):
            return
        # Content chunks can contain private answers/report text. Never inspect or print them.
        if raw.startswith('data: {"type": "chunk",'):
            return
        payload = json.loads(raw[6:].strip())
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "heartbeat":
            self.wait()
        elif kind == "analysis_progress":
            if self.stage == _STAGES["saving"]:
                return  # Earlier queued progress cannot undo a real persistence boundary.
            stage = payload.get("stage") if payload.get("phase") == "postwrite" else payload.get("phase")
            key = (stage, payload.get("status"))
            if key != self.last_progress:
                self.last_progress = key
                self.enter(stage)
            if stage == "quick_questions":
                completed, total = payload.get("run_completed"), payload.get("run_total")
                counts = (completed, total)
                if (type(completed) is int and type(total) is int and 0 <= completed <= total
                        and counts != self.last_question_counts):
                    self.last_question_counts = counts
                    self.emit("progress", f"本轮已完成 {completed}/{total} 道题目；最终结果以保存状态为准")
        elif kind in {"columns_ready", "plan_ready", "report_done"}:
            self.ended = True
            if kind == "columns_ready":
                self.emit("success", "问卷题型结果已准备好，请在页面核对并确认数据列")
            elif kind == "plan_ready":
                self.emit("success", "分析方案已生成并保存，请在页面核对并确认")
            else:
                audit = payload.get("report_audit") or {}
                status = payload.get("report_status") or audit.get("status")
                if payload.get("audit_updated"):
                    text = ("原版本核验结果已更新，尚有部分核验未完成；正文已保留，未新增报告版本"
                            if status == "partial" else "原版本核验结果已更新；未新增报告版本，提示仍需人工审阅")
                    self.emit("warning" if status == "partial" else "success", text, version=payload.get("version"))
                elif status == "partial":
                    text = "报告已保存，但部分核验或题目尚未完成；请在页面查看未完成项，并使用页面提供的补全操作"
                    self.emit("warning", text, version=payload.get("version"))
                else:
                    self.emit("success", "报告结果已生成并保存；请在页面查看报告和核验提示，此记录不代表所有内容已经人工确认", version=payload.get("version"))
        elif kind == "error":
            failure = payload.get("failure")
            if not failure:
                category = {
                    "当前任务正在生成报告": "already_running",
                    "当前任务正在生成报告，请等待本次生成完成后再试。": "already_running",
                    "原报告不存在或无权访问": "source_unavailable",
                }.get(payload.get("message"))
                failure = {"error_category": category}
            self.fail(failure=failure)
        elif kind == "cancelled":
            self.cancelled()
        else:
            self.wait()

    @_logging_only
    def finish(self):
        if not self.ended:
            self.ended = True
            self.emit("warning", "结果连接已结束，但尚未收到任务完成记录；请检查页面和历史版本，不能据此认定生成成功")


@_logging_only
def report_log_phase(stage):
    current = _CURRENT.get()
    if current is not None:
        current.enter(stage)


@_logging_only
def report_log_run(run_id):
    current = _CURRENT.get()
    if current is not None and isinstance(run_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        current.run_id = run_id


@_logging_only
def report_log_failure(session_id, **kwargs):
    current = _CURRENT.get() or SurveyProgressLog(session_id)
    current.fail(**kwargs)


def log_survey_stream(kind):
    """Keep every original SSE byte, exception and cancellation visible to its caller."""
    def decorate(func):
        @wraps(func)
        async def observed(session_id, *args, **kwargs):
            progress = SurveyProgressLog(session_id, kind, retry_failed=kwargs.get("retry_failed", False),
                                         generation_kind=kwargs.get("generation_kind"))
            source = func(session_id, *args, **kwargs)
            progress.start()
            try:
                while True:
                    # Restore context before yielding to the caller. Streams may be
                    # interleaved in one task or closed by another task/finalizer.
                    token = _CURRENT.set(progress)
                    try:
                        event = await anext(source)
                    except StopAsyncIteration:
                        break
                    finally:
                        _CURRENT.reset(token)
                    progress.observe(event)
                    yield event
            except asyncio.CancelledError:
                progress.cancelled()
                raise
            except Exception as exc:
                progress.fail(error=exc)
                raise
            finally:
                token = _CURRENT.set(progress)
                try:
                    await source.aclose()
                finally:
                    progress.finish()
                    _CURRENT.reset(token)
        return observed
    return decorate
