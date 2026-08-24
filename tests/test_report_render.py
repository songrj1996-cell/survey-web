import base64
import json
import unittest
from unittest.mock import MagicMock, patch

from app.core.config import QUALITATIVE_DISCLAIMER, REPORT_DISCLAIMER
from app.services import report_render


SAMPLE_REPORT_WITH_STALE_DISCLAIMER = f"""# 测试报告
{REPORT_DISCLAIMER}
{QUALITATIVE_DISCLAIMER}

## 核心结论
正文。
"""


class _FakeCdpWebSocket:
    def __init__(self, height_px: int, pdf_bytes: bytes):
        self.height_px = height_px
        self.pdf_bytes = pdf_bytes
        self.requests: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def send(self, payload: str):
        self.requests.append(json.loads(payload))

    def recv(self) -> str:
        request = self.requests[-1]
        method = request["method"]
        expression = request.get("params", {}).get("expression", "")
        if method == "Runtime.evaluate" and "document.body.scrollHeight" in expression:
            result = {"result": {"value": self.height_px}}
        elif method == "Page.printToPDF":
            result = {"data": base64.b64encode(self.pdf_bytes).decode("ascii")}
        else:
            result = {}
        return json.dumps({"id": request["id"], "result": result})


class BrowserLongPageTests(unittest.TestCase):
    def test_browser_pdf_uses_matching_dynamic_page_height(self):
        fake_ws = _FakeCdpWebSocket(height_px=9600, pdf_bytes=b"%PDF-test")
        fake_process = MagicMock()
        fake_process.wait.return_value = 0

        with (
            patch.object(report_render, "_free_local_port", return_value=9222),
            patch.object(report_render, "_wait_for_cdp_target", return_value="ws://test"),
            patch.object(report_render.subprocess, "Popen", return_value=fake_process),
            patch("websockets.sync.client.connect", return_value=fake_ws),
        ):
            rendered = report_render._html_to_pdf_with_browser_cmd(
                "<html><head><style>@page { size: 10in 14in; }</style></head><body>报告</body></html>",
                "chrome",
                "--headless=new",
            )

        self.assertEqual(rendered, b"%PDF-test")
        style_request = next(
            request
            for request in fake_ws.requests
            if request["method"] == "Runtime.evaluate"
            and "survey-pdf-long-page" in request["params"].get("expression", "")
        )
        print_request = next(
            request for request in fake_ws.requests if request["method"] == "Page.printToPDF"
        )
        self.assertLess(fake_ws.requests.index(style_request), fake_ws.requests.index(print_request))
        self.assertIn(
            "@page { size: 10in 100.25in; margin: 0; }",
            style_request["params"]["expression"],
        )
        params = print_request["params"]
        self.assertEqual(params["paperWidth"], 10.0)
        self.assertEqual(params["paperHeight"], 100.25)
        self.assertIs(params["preferCSSPageSize"], True)

    def test_set_pdf_page_height_replaces_static_rule(self):
        doc = f"<style>{report_render.PDF_PAGE_RULE}</style>"

        adjusted = report_render._set_pdf_page_height(doc, 123.456)

        self.assertIn("@page { size: 10in 123.46in; margin: 0; }", adjusted)
        self.assertNotIn(report_render.PDF_PAGE_RULE, adjusted)


class QuantitativeDisclaimerExportTests(unittest.TestCase):
    def test_quantitative_pdf_removes_stale_qualitative_disclaimer(self):
        for mode in ("quantitative", "crosstab"):
            with self.subTest(mode=mode):
                with (
                    patch.object(
                        report_render.markdown_lib,
                        "markdown",
                        return_value="<p>正文</p>",
                    ) as markdown_mock,
                    patch.object(report_render, "html_to_pdf_bytes", return_value=b"%PDF-test"),
                ):
                    rendered = report_render.report_markdown_to_pdf(
                        SAMPLE_REPORT_WITH_STALE_DISCLAIMER,
                        mode=mode,
                    )

                prepared = markdown_mock.call_args.args[0]
                self.assertEqual(rendered, b"%PDF-test")
                self.assertEqual(prepared.count(REPORT_DISCLAIMER), 1)
                self.assertNotIn(QUALITATIVE_DISCLAIMER, prepared)

    def test_quantitative_feishu_removes_stale_qualitative_disclaimer(self):
        report_text = REPORT_DISCLAIMER.removeprefix("> ").strip()
        qualitative_text = QUALITATIVE_DISCLAIMER.removeprefix("> ").strip()

        for mode in ("quantitative", "crosstab"):
            with self.subTest(mode=mode):
                prepared = report_render._prep_feishu_export_md(
                    SAMPLE_REPORT_WITH_STALE_DISCLAIMER,
                    mode=mode,
                )

            self.assertIn(report_text, prepared)
            self.assertNotIn(qualitative_text, prepared)


if __name__ == "__main__":
    unittest.main()
