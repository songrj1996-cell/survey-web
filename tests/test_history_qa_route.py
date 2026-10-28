import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.routers import survey


class HistoryQARouteTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(survey.router)
        self.app.dependency_overrides[survey.legacy_route_access] = lambda: None
        self.client = TestClient(self.app)

    def test_history_qa_streams_and_records_selected_report_title(self):
        for title in ("目标历史报告", "", None):
            with self.subTest(title=title):
                history = [
                    {"id": "other", "title": "其他报告"},
                    {"id": "target", "title": title},
                ]

                async def answer(*args):
                    yield 'data: {"type":"qa_done","answer":"合成回答"}\n\n'

                async def wrap(stream, api_key, **metadata):
                    async for event in stream:
                        yield event

                with (
                    patch.object(survey, "_current_login", new=AsyncMock(return_value=None)),
                    patch.object(survey, "prepare_history_qa_context", return_value=history) as prepare,
                    patch.object(survey, "require_request_llm_api_key", new=AsyncMock(return_value="synthetic-key")),
                    patch.object(survey, "history_qa_stream", side_effect=answer) as stream,
                    patch.object(survey, "stream_with_llm_api_key", side_effect=wrap) as wrapper,
                ):
                    response = self.client.post("/api/history-qa", json={
                        "history_id": "target", "question": "合成追问", "version": 2,
                    })

                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
                self.assertIn("合成回答", response.text)
                prepare.assert_called_once_with("target", None, 2)
                self.assertEqual(stream.call_args.args[:3], ("target", "合成追问", history))
                self.assertEqual(stream.call_args.args[4], 2)
                metadata = wrapper.call_args.kwargs
                self.assertEqual(metadata["title"], title or "")
                self.assertEqual(metadata["action"], "历史报告追问")
                self.assertEqual(metadata["reference_id"], "target")
                self.assertEqual(metadata["history_id"], "target")

    def test_history_qa_preserves_preparation_errors_without_starting_model_stream(self):
        for status, detail in ((404, "历史记录不存在"), (400, "该历史记录没有可追问的报告")):
            with (
                self.subTest(status=status),
                patch.object(survey, "_current_login", new=AsyncMock(return_value=None)),
                patch.object(survey, "prepare_history_qa_context", side_effect=HTTPException(status, detail)),
                patch.object(survey, "require_request_llm_api_key", new=AsyncMock()) as key,
                patch.object(survey, "history_qa_stream") as stream,
            ):
                response = self.client.post("/api/history-qa", json={
                    "history_id": "missing", "question": "合成追问",
                })
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["detail"], detail)
                key.assert_not_awaited()
                stream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
