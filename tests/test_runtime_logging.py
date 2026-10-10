import ast
import io
import logging
from contextlib import redirect_stdout
from pathlib import Path
import unittest
from unittest.mock import patch

from app.core import runtime_logging


class RuntimeLoggingTests(unittest.TestCase):
    def test_runtime_log_is_timestamped_and_plain_language(self):
        output = io.StringIO()
        with patch.object(
            runtime_logging,
            "_timestamp",
            return_value="2026-10-10 15:02:41.238+08:00",
        ), redirect_stdout(output):
            runtime_logging.runtime_log(
                "数据标注",
                "success",
                "模型调用完成",
                model="gpt-5.6-terra",
                attempt=1,
                response_chars=3669,
            )

        self.assertEqual(
            output.getvalue().strip(),
            "[2026-10-10 15:02:41.238+08:00] [成功] [数据标注] 模型调用完成"
            "｜模型：gpt-5.6-terra｜尝试次数：1｜返回字符数：3669",
        )

    def test_sensitive_fields_are_masked_or_shortened(self):
        output = io.StringIO()
        with patch.object(runtime_logging, "_timestamp", return_value="TIME"), redirect_stdout(output):
            runtime_logging.runtime_log(
                "飞书登录",
                "info",
                "已取得用户信息",
                open_id="ou_1234567890abcdef",
                email="researcher@example.com",
                path=r"C:\private\survey.xlsx",
                url="https://example.com/private/document-token?secret=value",
            )

        rendered = output.getvalue()
        self.assertIn("***90abcdef", rendered)
        self.assertIn("r***@example.com", rendered)
        self.assertIn("survey.xlsx", rendered)
        self.assertIn("https://example.com/…", rendered)
        self.assertNotIn("document-token", rendered)
        self.assertNotIn("C:\\private", rendered)

    def test_internal_stage_and_status_values_are_translated(self):
        output = io.StringIO()
        with patch.object(runtime_logging, "_timestamp", return_value="TIME"), redirect_stdout(output):
            runtime_logging.runtime_log(
                "问卷报告",
                "info",
                "观点证据覆盖检查完成",
                phase="writing",
                stage="audit",
                status="writer_omission",
            )

        rendered = output.getvalue()
        self.assertIn("流程阶段：撰写报告", rendered)
        self.assertIn("阶段：审校报告", rendered)
        self.assertIn("状态：报告遗漏了有证据支持的观点", rendered)
        self.assertNotIn("writer_omission", rendered)

    def test_closed_output_does_not_interrupt_business_code(self):
        with patch("builtins.print", side_effect=OSError("output closed")):
            runtime_logging.runtime_log("问卷报告", "error", "生成失败")

    def test_history_404_explains_likely_causes(self):
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "",
            0,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:50000", "GET", "/api/history/example-id", "1.1", 404),
            None,
        )
        with patch.object(runtime_logging, "_timestamp", return_value="TIME"):
            rendered = runtime_logging.HumanAccessFormatter().format(record)

        self.assertIn("[提醒] [历史记录]", rendered)
        self.assertIn("没有找到这条记录", rendered)
        self.assertIn("可能尚未保存", rendered)
        self.assertIn("HTTP 404", rendered)

    def test_success_and_server_error_access_logs_are_distinct(self):
        formatter = runtime_logging.HumanAccessFormatter()
        cases = (
            (200, "[成功]", "读取历史记录列表完成"),
            (500, "[失败]", "服务器处理时出错"),
        )
        with patch.object(runtime_logging, "_timestamp", return_value="TIME"):
            for status, level, phrase in cases:
                with self.subTest(status=status):
                    record = logging.LogRecord(
                        "uvicorn.access",
                        logging.INFO,
                        "",
                        0,
                        '%s - "%s %s HTTP/%s" %d',
                        ("127.0.0.1:50000", "GET", "/api/history", "1.1", status),
                        None,
                    )
                    rendered = formatter.format(record)
                    self.assertIn(level, rendered)
                    self.assertIn(phrase, rendered)

    def test_common_uvicorn_startup_message_is_translated(self):
        record = logging.LogRecord(
            "uvicorn.error",
            logging.INFO,
            "",
            0,
            "Application startup complete.",
            (),
            None,
        )
        with patch.object(runtime_logging, "_timestamp", return_value="TIME"):
            rendered = runtime_logging.HumanSystemFormatter().format(record)
        self.assertEqual(rendered, "[TIME] [信息] [服务] 应用启动完成，可以开始访问")

    def test_unknown_system_message_keeps_original_details(self):
        record = logging.LogRecord(
            "uvicorn.error",
            logging.WARNING,
            "",
            0,
            "Future runtime message: %s",
            ("detail",),
            None,
        )
        with patch.object(runtime_logging, "_timestamp", return_value="TIME"):
            rendered = runtime_logging.HumanSystemFormatter().format(record)
        self.assertIn("[提醒] [服务]", rendered)
        self.assertIn("服务运行信息：", rendered)
        self.assertIn("Future runtime message: detail", rendered)

    def test_backend_runtime_code_has_no_direct_print_or_logger_calls(self):
        root = Path(__file__).resolve().parents[1]
        violations = []
        logger_methods = {"debug", "info", "warning", "error", "exception", "critical"}
        for path in (root / "app").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id == "print":
                    if path.name != "runtime_logging.py":
                        violations.append(f"{path.relative_to(root)}:{node.lineno}: print")
                is_named_logger = (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in logger_methods
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in {"logger", "logging"}
                )
                is_inline_logger = (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in logger_methods
                    and isinstance(node.func.value, ast.Call)
                    and isinstance(node.func.value.func, ast.Attribute)
                    and node.func.value.func.attr == "getLogger"
                )
                if is_named_logger or is_inline_logger:
                    violations.append(
                        f"{path.relative_to(root)}:{node.lineno}: logger.{node.func.attr}"
                    )
                is_traceback_print = (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "print_exc"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "traceback"
                )
                if is_traceback_print:
                    violations.append(
                        f"{path.relative_to(root)}:{node.lineno}: traceback.print_exc"
                    )
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
