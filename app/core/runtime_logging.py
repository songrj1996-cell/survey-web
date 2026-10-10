"""后端终端日志的统一白话格式。

这里只负责展示运行状态，不参与任何业务判断。日志格式化失败时必须回退，
不能让报告、导出或审计流程因为“写日志”而中断。
"""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
import logging
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlsplit


_OUTPUT_LOCK = threading.Lock()
_ACCESS_OPERATOR_LOCK = threading.Lock()
_ACCESS_OPERATOR_TTL_SECONDS = 120.0
_ACCESS_OPERATORS: dict[str, tuple[str, float]] = {}
_RUNTIME_OPERATOR: ContextVar[str] = ContextVar(
    "runtime_log_operator",
    default="系统任务",
)

_LEVEL_LABELS = {
    "success": "成功",
    "progress": "进行中",
    "info": "信息",
    "warning": "提醒",
    "error": "失败",
}

_FIELD_LABELS = {
    "attempt": "尝试次数",
    "batch": "批次",
    "batches": "批次数",
    "browser_mode": "浏览器启动方式",
    "bytes": "文件大小",
    "candidates": "候选数",
    "catalog_entries": "证据目录条目数",
    "chunk": "分段",
    "code": "错误代码",
    "call_id": "模型调用编号",
    "context_included": "已提供完整上下文",
    "elapsed_seconds": "耗时",
    "email": "邮箱",
    "error": "原因",
    "examples": "示例",
    "expected": "预期数",
    "fallback": "使用备用方案",
    "filename": "文件",
    "first_batch_rows": "第一小批数据行数",
    "has_name": "已取得姓名",
    "history_id": "历史记录",
    "http_status": "HTTP 状态",
    "input_chars": "输入字符数",
    "input_tokens": "输入 Token",
    "issue_indexes": "问题序号",
    "local_issues": "本地校验问题数",
    "long_candidates": "长评论候选数",
    "mention_blocks": "观点提及块数",
    "model": "模型",
    "model_issues": "模型审校问题数",
    "open_id": "飞书用户标识",
    "output_tokens": "输出 Token",
    "parsed": "解析数",
    "pages": "页数",
    "part_index": "章节序号",
    "path": "位置",
    "phase": "流程阶段",
    "repair_round": "自动修订轮次",
    "repaired": "经过格式修复",
    "returned_fields": "返回字段",
    "response_chars": "返回字符数",
    "rows": "数据行数",
    "run_id": "本轮生成编号",
    "sample_rows": "抽样行数",
    "scanned_rows": "扫描行数",
    "second_batch_rows": "第二小批数据行数",
    "selected": "选中数",
    "session_id": "任务编号",
    "stage": "阶段",
    "status": "状态",
    "step": "处理步骤",
    "target_height_inches": "目标页面高度（英寸）",
    "task": "任务",
    "title": "标题",
    "total_tokens": "Token 总数",
    "translated": "已翻译数",
    "translation_round": "翻译轮次",
    "unmatched_count": "无法对应的数字数",
    "url": "地址",
    "valid_rows": "有效行数",
    "viewpoint_blocks": "观点块数",
}

_FIELD_VALUE_LABELS = {
    "phase": {
        "planning": "制定分析方案",
        "writing": "撰写报告",
        "review": "审校报告",
        "finalize": "整理最终结果",
    },
    "stage": {
        "extract": "提取访谈材料",
        "extract_done": "访谈材料提取完成",
        "write": "撰写报告",
        "module_repair": "修订报告模块",
        "audit": "审校报告",
        "repair": "自动修订",
        "review_repair": "根据审校结果修订",
        "review_audit": "复核修订结果",
        "report_generation": "生成报告",
        "partial_rerun": "局部重做",
        "completed": "已完成",
    },
    "status": {
        "started": "已开始",
        "pending": "等待重试",
        "processing": "正在处理",
        "completed": "已完成",
        "success": "成功",
        "failed": "失败",
        "writer_no_viewpoints": "报告没有生成观点段落",
        "writer_omission": "报告遗漏了有证据支持的观点",
        "not_applicable": "本次无需检查",
    },
}


def _timestamp(created: float | None = None) -> str:
    moment = (
        datetime.fromtimestamp(created).astimezone()
        if created is not None
        else datetime.now().astimezone()
    )
    return moment.isoformat(sep=" ", timespec="milliseconds")


def _mask_identifier(value: object) -> str:
    text = str(value or "").strip()
    if len(text) <= 8:
        return text
    return f"***{text[-8:]}"


def _mask_email(value: object) -> str:
    text = str(value or "").strip()
    if "@" not in text:
        return _mask_identifier(text)
    name, domain = text.split("@", 1)
    visible = name[:1] if name else ""
    return f"{visible}***@{domain}"


def _safe_error(value: object) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(
        r"(?i)(token|authorization|cookie|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+",
        r"\1=***",
        text,
    )
    return text[:500]


def _safe_operator_text(value: object) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:80]


def runtime_operator_from_login(
    login: dict | None,
    *,
    anonymous_label: str = "未登录访客",
) -> str:
    """把登录态转换成可辨认但不过度暴露隐私的操作人名称。"""
    if not login:
        return anonymous_label
    name = _safe_operator_text(login.get("name"))
    if name:
        return name
    return "已登录用户"


def bind_runtime_operator(operator: str):
    """为当前异步请求绑定操作人，并返回稍后恢复上下文所需的 token。"""
    safe_operator = _safe_operator_text(operator) or "身份无法识别"
    return _RUNTIME_OPERATOR.set(safe_operator)


def reset_runtime_operator(token) -> None:
    _RUNTIME_OPERATOR.reset(token)


def current_runtime_operator() -> str:
    return _RUNTIME_OPERATOR.get()


def _client_key(client: object) -> str:
    if isinstance(client, (tuple, list)) and len(client) >= 2:
        return f"{client[0]}:{client[1]}"
    return str(client or "").strip()


def register_access_operator(client: object, operator: str) -> None:
    """暂存一次 HTTP 访问日志的操作人；formatter 读取后立即删除。"""
    key = _client_key(client)
    if not key:
        return
    now = time.monotonic()
    with _ACCESS_OPERATOR_LOCK:
        if len(_ACCESS_OPERATORS) >= 2000:
            expired_before = now - _ACCESS_OPERATOR_TTL_SECONDS
            expired = [
                item_key for item_key, (_value, created) in _ACCESS_OPERATORS.items()
                if created < expired_before
            ]
            for item_key in expired:
                _ACCESS_OPERATORS.pop(item_key, None)
            if len(_ACCESS_OPERATORS) >= 2000:
                oldest = min(_ACCESS_OPERATORS, key=lambda item_key: _ACCESS_OPERATORS[item_key][1])
                _ACCESS_OPERATORS.pop(oldest, None)
        _ACCESS_OPERATORS[key] = (_safe_operator_text(operator), now)


def _take_access_operator(client: object) -> str:
    key = _client_key(client)
    with _ACCESS_OPERATOR_LOCK:
        item = _ACCESS_OPERATORS.pop(key, None)
    if item and time.monotonic() - item[1] <= _ACCESS_OPERATOR_TTL_SECONDS:
        return item[0] or "身份无法识别"
    return current_runtime_operator()


def _operator_suffix(operator: str | None = None) -> str:
    return f"｜操作人：{operator or current_runtime_operator()}"


def _safe_url(value: object) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return "已生成（详细地址已隐藏）"
    if not parsed.scheme or not parsed.netloc:
        return "已生成（详细地址已隐藏）"
    return f"{parsed.scheme}://{parsed.netloc}/…"


def _format_value(key: str, value: object) -> str:
    translated = _FIELD_VALUE_LABELS.get(key, {}).get(str(value))
    if translated:
        return translated
    if key in {
        "open_id", "user_id", "session_id", "history_id", "doc_token",
        "run_id", "call_id", "request_id",
    }:
        return _mask_identifier(value)
    if key == "email":
        return _mask_email(value)
    if key == "path":
        return Path(str(value or "")).name
    if key == "url":
        return _safe_url(value)
    if key == "error":
        return _safe_error(value)
    if isinstance(value, bool):
        return "是" if value else "否"
    if key == "elapsed_seconds":
        try:
            return f"{float(value):.2f} 秒"
        except (TypeError, ValueError):
            pass
    return str(value)


def runtime_log(area: str, level: str, message: str, **fields: object) -> None:
    """输出一条带本地时区时间戳的单行日志；任何格式错误均不得向外抛出。"""
    try:
        label = _LEVEL_LABELS.get(level, _LEVEL_LABELS["info"])
        parts = [
            f"{_FIELD_LABELS.get(key, key)}：{_format_value(key, value)}"
            for key, value in fields.items()
            if value not in (None, "")
        ]
        suffix = f"｜{'｜'.join(parts)}" if parts else ""
        rendered = (
            f"[{_timestamp()}] [{label}] [{area}] {message}"
            f"{_operator_suffix()}{suffix}"
        )
    except Exception as exc:  # pragma: no cover - 最终防线，不能影响业务
        rendered = (
            f"[{_timestamp()}] [提醒] [系统日志] 日志格式化失败，已保留原始说明"
            f"{_operator_suffix()}｜说明：{str(message)[:300]}"
            f"｜技术信息：{type(exc).__name__}"
        )
    try:
        with _OUTPUT_LOCK:
            print(rendered, file=sys.stdout, flush=True)
    except Exception:  # pragma: no cover - 输出端被关闭时也不能影响业务
        return


def _http_action(method: str, raw_path: str) -> tuple[str, str]:
    path = raw_path.split("?", 1)[0]
    if path == "/":
        return "页面", "打开调研分析平台"
    if path.startswith("/static/") or path == "/favicon.ico":
        return "页面", "加载页面资源"
    if path == "/api/history":
        return "历史记录", "读取历史记录列表"
    if re.fullmatch(r"/api/history/[^/]+", path):
        return "历史记录", "打开一条历史记录"
    if path.startswith("/api/history/"):
        return "历史记录", "操作历史记录"
    if path.startswith("/api/annotate"):
        return "数据标注", "处理数据标注任务"
    if path.startswith("/api/interview") or path.startswith("/api/v1/interview"):
        return "访谈报告", "处理访谈报告任务"
    if path.startswith("/api/comment-analysis"):
        return "评论分析", "处理评论分析任务"
    if path.startswith("/api/export"):
        return "报告导出", "导出报告"
    if path.startswith("/api/feishu"):
        return "飞书", "处理飞书功能"
    if path.startswith("/api/profile"):
        return "个人中心", "读取当前账号和个人配置"
    if path.startswith("/api/ui-texts"):
        return "页面", "读取页面文案"
    if path.startswith("/api/upload-guide"):
        return "页面", "读取上传说明"
    if path.startswith("/api/report") or path.startswith("/api/qa"):
        return "问卷报告", "处理报告任务"
    if path.startswith("/api/"):
        return "接口请求", "处理系统请求"
    return "页面", "访问系统页面"


def _http_result(status: int, *, history_detail: bool) -> tuple[str, str]:
    if 200 <= status < 300:
        return "success", "完成"
    if status == 304:
        return "success", "使用浏览器缓存完成"
    if status == 400:
        return "warning", "未完成：提交内容不符合当前要求"
    if status == 401:
        return "warning", "未完成：尚未登录或登录已经失效"
    if status == 403:
        return "warning", "未完成：当前账号没有操作权限"
    if status == 404 and history_detail:
        return "warning", "未完成：没有找到这条记录，可能尚未保存、已删除、超过保留上限或当前账号不可见"
    if status == 404:
        return "warning", "未完成：没有找到请求的内容"
    if status == 409:
        return "warning", "未完成：当前状态与这次操作冲突，可能是重复操作"
    if status == 422:
        return "warning", "未完成：提交内容的格式不符合要求"
    if status == 429:
        return "warning", "未完成：操作过于频繁，请稍后再试"
    if 400 <= status < 500:
        return "warning", "未完成：请求被服务器拒绝"
    if status >= 500:
        return "error", "服务器处理时出错，请查看同一时间附近的失败日志"
    return "info", "已处理"


class HumanAccessFormatter(logging.Formatter):
    """把 Uvicorn 的 method/path/status 访问日志翻译成白话。"""

    def format(self, record: logging.LogRecord) -> str:
        operator = current_runtime_operator()
        try:
            if isinstance(record.args, (tuple, list)) and record.args:
                operator = _take_access_operator(record.args[0])
        except Exception:
            pass
        try:
            client, method, path, _http_version, status = record.args[:5]
            status = int(status)
            method = str(method).upper()
            path = str(path)
            area, action = _http_action(method, path)
            history_detail = bool(re.fullmatch(r"/api/history/[^/?]+", path.split("?", 1)[0]))
            level, result = _http_result(status, history_detail=history_detail)
            label = _LEVEL_LABELS[level]
            return (
                f"[{_timestamp(record.created)}] [{label}] [{area}] {action}{result}"
                f"{_operator_suffix(operator)}｜HTTP {status}｜请求：{method} {path}"
            )
        except Exception:
            return (
                f"[{_timestamp(record.created)}] [信息] [接口请求] "
                f"{_safe_error(record.getMessage())}{_operator_suffix(operator)}"
            )


_SYSTEM_MESSAGES = (
    (re.compile(r"^Started server process \[(\d+)\]$"), lambda m: f"服务进程已启动｜进程号：{m.group(1)}"),
    (re.compile(r"^Started reloader process \[(\d+)\] using (.+)$"), lambda m: f"自动重载监控已启动｜进程号：{m.group(1)}｜方式：{m.group(2)}"),
    (re.compile(r"^Finished server process \[(\d+)\]$"), lambda m: f"服务进程已结束｜进程号：{m.group(1)}"),
    (re.compile(r"^Stopping reloader process \[(\d+)\]$"), lambda m: f"自动重载监控已停止｜进程号：{m.group(1)}"),
    (re.compile(r"^Uvicorn running on (.+) \(Press CTRL\+C to quit\)$"), lambda m: f"服务已开始监听｜地址：{m.group(1)}｜按 Ctrl+C 可停止"),
    (re.compile(r"^Waiting for application startup\.$"), lambda _m: "正在启动应用"),
    (re.compile(r"^Application startup complete\.$"), lambda _m: "应用启动完成，可以开始访问"),
    (re.compile(r"^Shutting down$"), lambda _m: "正在关闭服务"),
    (re.compile(r"^Waiting for application shutdown\.$"), lambda _m: "正在等待应用安全关闭"),
    (re.compile(r"^Application shutdown complete\.$"), lambda _m: "应用已经安全关闭"),
    (re.compile(r"^(?:WatchFiles|StatReload) detected changes in (.+)\. Reloading\.\.\.$"), lambda m: f"检测到代码文件变化，正在自动重启｜文件：{m.group(1)}"),
)


def _translate_system_message(message: str) -> str:
    for pattern, render in _SYSTEM_MESSAGES:
        match = pattern.match(message)
        if match:
            return render(match)
    if message.startswith("Error loading ASGI app"):
        return f"应用加载失败：{message.removeprefix('Error loading ASGI app.').strip()}"
    return f"服务运行信息：{message}"


class HumanSystemFormatter(logging.Formatter):
    """给 Uvicorn 的启动、重载和关闭日志添加时间并翻译常见状态。"""

    def format(self, record: logging.LogRecord) -> str:
        raw = record.getMessage()
        if record.levelno >= logging.ERROR:
            level = "error"
        elif record.levelno >= logging.WARNING:
            level = "warning"
        else:
            level = "info"
        message = _translate_system_message(raw)
        if record.exc_info:
            exc_type, exc, _tb = record.exc_info
            if exc_type is not None:
                message += f"｜技术信息：{exc_type.__name__}：{_safe_error(exc)}"
        return (
            f"[{_timestamp(record.created)}] [{_LEVEL_LABELS[level]}] [服务] {message}"
            f"{_operator_suffix()}"
        )


def configure_runtime_logging() -> None:
    """配置当前已存在的 Uvicorn handler；可重复调用以覆盖后配置的 handler。"""
    access_formatter = HumanAccessFormatter()
    system_formatter = HumanSystemFormatter()
    for handler in logging.getLogger("uvicorn.access").handlers:
        handler.setFormatter(access_formatter)
    for logger_name in ("uvicorn", "uvicorn.error"):
        for handler in logging.getLogger(logger_name).handlers:
            handler.setFormatter(system_formatter)
