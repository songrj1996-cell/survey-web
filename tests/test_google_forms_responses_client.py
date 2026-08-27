from __future__ import annotations

import json
import unittest

import httpx

from app.integrations.google_forms_responses_client import (
    GoogleFormsResponsesClient,
    GoogleFormsResponsesClientError,
    GoogleFormsResponsesErrorCode,
)


FORM_ID = "FORM_SYNTHETIC_001"
BASE = "https://forms.googleapis.com/v1"


def _response(response_id: str, *, value: str = "原始回答") -> dict:
    return {
        "responseId": response_id,
        "createTime": "2026-08-24T01:02:03Z",
        "lastSubmittedTime": "2026-08-24T01:02:04Z",
        "answers": {
            "question-open": {
                "questionId": "question-open",
                "textAnswers": {"answers": [{"value": value}]},
            },
            "question-files": {
                "questionId": "question-files",
                "fileUploadAnswers": {
                    "answers": [{
                        "fileId": "file-synthetic",
                        "fileName": "synthetic.png",
                        "mimeType": "image/png",
                    }],
                },
            },
        },
    }


class GoogleFormsResponsesClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_all_pages_and_preserves_text_and_file_metadata(self):
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            token = request.url.params.get("pageToken")
            payload = (
                {"responses": [_response("response-1")], "nextPageToken": "next-1"}
                if token is None
                else {"responses": [_response("response-2", value="jawaban asli")]}
            )
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            client = GoogleFormsResponsesClient(
                http_client,
                lambda: {"Authorization": "Bearer synthetic-token"},
                forms_api_base=BASE,
            )
            capture = await client.fetch_all(FORM_ID)

        self.assertEqual(capture.form_id, FORM_ID)
        self.assertEqual(capture.page_count, 2)
        self.assertEqual([item.response_id for item in capture.responses], [
            "response-1", "response-2",
        ])
        self.assertEqual(capture.responses[1].answers[1].text_values, ("jawaban asli",))
        self.assertEqual(
            capture.responses[0].answers[0].file_uploads[0].file_id,
            "file-synthetic",
        )
        self.assertEqual(requests[0].url.params["pageSize"], "5000")
        self.assertEqual(requests[1].url.params["pageToken"], "next-1")
        self.assertEqual(
            requests[0].headers["authorization"],
            "Bearer synthetic-token",
        )

    async def test_repeated_page_token_fails_closed(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "responses": [],
                "nextPageToken": "same-token",
            })

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            client = GoogleFormsResponsesClient(
                http_client,
                lambda: {"Authorization": "Bearer synthetic-token"},
                forms_api_base=BASE,
            )
            with self.assertRaises(GoogleFormsResponsesClientError) as caught:
                await client.fetch_all(FORM_ID)
        self.assertEqual(
            caught.exception.code,
            GoogleFormsResponsesErrorCode.PAGINATION_LOOP,
        )

    async def test_provider_body_and_pii_never_enter_error_text(self):
        secret = "respondent-secret@example.test"

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, content=secret.encode())

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            client = GoogleFormsResponsesClient(
                http_client,
                lambda: {"Authorization": "Bearer synthetic-token"},
                forms_api_base=BASE,
            )
            with self.assertRaises(GoogleFormsResponsesClientError) as caught:
                await client.fetch_all(FORM_ID)
        self.assertEqual(
            caught.exception.code,
            GoogleFormsResponsesErrorCode.PERMISSION_DENIED,
        )
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(FORM_ID, str(caught.exception))

    async def test_mismatched_answer_question_id_is_invalid(self):
        payload = {"responses": [_response("response-1")]}
        payload["responses"][0]["answers"]["question-open"]["questionId"] = "other"

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps(payload).encode())

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            client = GoogleFormsResponsesClient(
                http_client,
                lambda: {"Authorization": "Bearer synthetic-token"},
                forms_api_base=BASE,
            )
            with self.assertRaises(GoogleFormsResponsesClientError) as caught:
                await client.fetch_all(FORM_ID)
        self.assertEqual(
            caught.exception.code,
            GoogleFormsResponsesErrorCode.INVALID_RESPONSE,
        )

    def test_rejects_non_official_base_and_invalid_limits(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        for kwargs in (
            {"forms_api_base": "https://attacker.example/v1"},
            {"forms_api_base": BASE, "page_size": 5001},
            {"forms_api_base": BASE, "max_responses": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(GoogleFormsResponsesClientError):
                    GoogleFormsResponsesClient(
                        client,
                        lambda: {"Authorization": "Bearer token"},
                        **kwargs,
                    )


if __name__ == "__main__":
    unittest.main()
