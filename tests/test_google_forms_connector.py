from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

import httpx

from app.integrations.google_forms_client import (
    GoogleFormsClient,
    GoogleFormsConnectorError,
    GoogleFormsErrorCode,
    GoogleFormsStage,
    GoogleImageDownloadPolicy,
)


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "questionnaire_sources"
    / "google_forms_api.json"
)
FORM_ID = "FORM_SYNTHETIC_001"
FORMS_API_BASE = "https://forms.googleapis.test/v1"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

IMAGE_PATHS = (
    ("items", 0, "imageItem", "image"),
    (
        "items",
        1,
        "questionItem",
        "question",
        "choiceQuestion",
        "options",
        0,
        "image",
    ),
    ("items", 1, "questionItem", "image"),
    ("items", 2, "questionGroupItem", "image"),
)


def _fixture() -> dict:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as source:
        return json.load(source)


def _at_path(payload: dict, path: tuple[str | int, ...]):
    current = payload
    for part in path:
        current = current[part]
    return current


def _payload_with_image_urls(urls: tuple[str, ...]) -> dict:
    payload = _fixture()
    for path, url in zip(IMAGE_PATHS, urls, strict=True):
        _at_path(payload, path)["contentUri"] = url
    return payload


def _payload_with_one_image(url: str | int) -> dict:
    payload = _fixture()
    _at_path(payload, IMAGE_PATHS[0])["contentUri"] = url
    return payload


def _png(label: str) -> bytes:
    return PNG_SIGNATURE + label.encode("ascii")


class _AsyncChunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


class GoogleFormsConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_form_and_captures_all_images_in_memory(self):
        image_urls = tuple(
            f"https://lh{index}.googleusercontent.com/image-{index}?temporary=secret-{index}"
            for index in range(1, 5)
        )
        payload = _payload_with_image_urls(image_urls)
        image_content = {
            f"/image-{index}": _png(f"image-{index}")
            for index in range(1, 5)
        }
        requests: list[dict[str, object]] = []
        authorization_calls = 0

        async def authorization() -> dict[str, str]:
            nonlocal authorization_calls
            authorization_calls += 1
            return {
                "Authorization": "Bearer synthetic-secret-token",
                "X-Goog-User-Project": "synthetic-project",
            }

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append({
                "url": request.url,
                "authorization": request.headers.get("authorization"),
                "cookie": request.headers.get("cookie"),
            })
            if request.url.host == "forms.googleapis.test":
                self.assertEqual(request.method, "GET")
                self.assertEqual(
                    request.url.path,
                    f"/v1/forms/{FORM_ID}",
                )
                self.assertEqual(
                    request.headers["authorization"],
                    "Bearer synthetic-secret-token",
                )
                return httpx.Response(200, json=payload)
            return httpx.Response(
                200,
                headers={"Content-Type": "image/png; charset=binary"},
                content=image_content[request.url.path],
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            transport=transport,
            headers={"Authorization": "Bearer client-default-must-not-leak"},
            cookies={"session": "client-cookie-must-not-leak"},
        ) as http_client:
            connector = GoogleFormsClient(
                http_client,
                authorization,
                forms_api_base=FORMS_API_BASE,
            )
            capture = await connector.fetch_form(FORM_ID)

        self.assertEqual(authorization_calls, 1)
        self.assertEqual(capture.form_id, FORM_ID)
        self.assertEqual(len(capture.images), 4)
        self.assertTrue(capture.raw_form["syntheticProviderField"]["mustSurvive"])
        serialized = json.dumps(capture.raw_form, ensure_ascii=False)
        self.assertNotIn("contentUri", serialized)
        self.assertNotIn("googleusercontent.com", serialized)
        self.assertNotIn("temporary=", serialized)

        captures_by_path = {image.json_path: image for image in capture.images}
        self.assertEqual(set(captures_by_path), set(IMAGE_PATHS))
        for index, path in enumerate(IMAGE_PATHS, start=1):
            image = captures_by_path[path]
            expected = _png(f"image-{index}")
            self.assertEqual(image.content, expected)
            self.assertEqual(image.mime_type, "image/png")
            self.assertEqual(image.sha256, hashlib.sha256(expected).hexdigest())

        standalone = captures_by_path[IMAGE_PATHS[0]].context
        self.assertEqual(standalone.item_id, "item-standalone-image")
        self.assertIsNone(standalone.question_id)
        self.assertEqual(standalone.question_ids, ())
        self.assertIsNone(standalone.option_index)

        option = captures_by_path[IMAGE_PATHS[1]].context
        self.assertEqual(option.question_id, "question-choice")
        self.assertEqual(option.question_ids, ("question-choice",))
        self.assertEqual(option.option_index, 0)

        prompt = captures_by_path[IMAGE_PATHS[2]].context
        self.assertEqual(prompt.question_id, "question-choice")
        self.assertIsNone(prompt.option_index)

        group = captures_by_path[IMAGE_PATHS[3]].context
        self.assertIsNone(group.question_id)
        self.assertEqual(group.question_ids, (
            "question-grid-usability",
            "question-grid-art",
        ))

        self.assertEqual(len(requests), 5)
        self.assertEqual(
            {request["url"].host for request in requests[1:]},
            {"lh1.googleusercontent.com", "lh2.googleusercontent.com",
             "lh3.googleusercontent.com", "lh4.googleusercontent.com"},
        )
        for request in requests[1:]:
            self.assertIsNone(request["authorization"])
            self.assertIsNone(request["cookie"])

    async def test_fixture_contains_no_persisted_temporary_image_url(self):
        raw_fixture = FIXTURE_PATH.read_text(encoding="utf-8")

        self.assertNotIn("contentUri", raw_fixture)
        self.assertNotIn("googleusercontent.com", raw_fixture)
        self.assertNotIn("access_token", raw_fixture)

    async def test_rejects_non_allowlisted_or_credentialed_image_urls(self):
        rejected_urls = (
            "http://lh3.googleusercontent.com/image?token=secret-query-value",
            "https://evilgoogleusercontent.com/image?token=secret-query-value",
            "https://googleusercontent.com.evil.invalid/image?token=secret-query-value",
            "https://user:password@lh3.googleusercontent.com/image?token=secret-query-value",
            "https://127.0.0.1/image?token=secret-query-value",
            "https://lh3.googleusercontent.com:8443/image?token=secret-query-value",
            "https://lh3.googleusercontent.com/image?token=secret-query-value#fragment",
        )

        for rejected_url in rejected_urls:
            with self.subTest(rejected_url=rejected_url):
                requests: list[httpx.Request] = []
                payload = _payload_with_one_image(rejected_url)

                async def handler(request: httpx.Request) -> httpx.Response:
                    requests.append(request)
                    return httpx.Response(200, json=payload)

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ) as http_client:
                    connector = GoogleFormsClient(
                        http_client,
                        lambda: {"Authorization": "Bearer hidden-token"},
                        forms_api_base=FORMS_API_BASE,
                    )
                    with self.assertRaises(GoogleFormsConnectorError) as caught:
                        await connector.fetch_form(FORM_ID)

                error = caught.exception
                self.assertEqual(error.code, GoogleFormsErrorCode.IMAGE_URL_REJECTED)
                self.assertEqual(error.stage, GoogleFormsStage.IMAGE_DOWNLOAD)
                self.assertFalse(error.retryable)
                self.assertEqual(len(requests), 1)
                safe_error = f"{error} {error.safe_context!r}"
                self.assertNotIn("secret-query-value", safe_error)
                self.assertNotIn("hidden-token", safe_error)

    async def test_validates_every_redirect_and_never_follows_an_evil_target(self):
        initial_url = "https://lh3.googleusercontent.com/start?token=initial-secret"
        rejected_redirect = (
            "https://googleusercontent.com.evil.invalid/steal?token=redirect-secret"
        )
        payload = _payload_with_one_image(initial_url)
        requested_urls: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requested_urls.append(str(request.url))
            if request.url.host == "forms.googleapis.test":
                return httpx.Response(200, json=payload)
            return httpx.Response(302, headers={"Location": rejected_redirect})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            connector = GoogleFormsClient(
                http_client,
                lambda: {"Authorization": "Bearer hidden-token"},
                forms_api_base=FORMS_API_BASE,
            )
            with self.assertRaises(GoogleFormsConnectorError) as caught:
                await connector.fetch_form(FORM_ID)

        self.assertEqual(
            caught.exception.code,
            GoogleFormsErrorCode.IMAGE_URL_REJECTED,
        )
        self.assertEqual(len(requested_urls), 2)
        self.assertNotIn("redirect-secret", str(caught.exception))
        self.assertFalse(any("evil.invalid" in url for url in requested_urls))

    async def test_follows_a_bounded_safe_redirect_without_authorization(self):
        payload = _payload_with_one_image(
            "https://lh3.googleusercontent.com/start?temporary=one"
        )
        image_requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "forms.googleapis.test":
                return httpx.Response(200, json=payload)
            image_requests.append(request)
            if request.url.path == "/start":
                return httpx.Response(
                    307,
                    headers={
                        "Location": (
                            "https://lh4.googleusercontent.com/final?temporary=two"
                        )
                    },
                )
            return httpx.Response(
                200,
                headers={"Content-Type": "image/png"},
                content=_png("redirected"),
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer client-default"},
        ) as http_client:
            connector = GoogleFormsClient(
                http_client,
                lambda: {"Authorization": "Bearer forms-only"},
                forms_api_base=FORMS_API_BASE,
            )
            capture = await connector.fetch_form(FORM_ID)

        self.assertEqual(capture.images[0].content, _png("redirected"))
        self.assertEqual(len(image_requests), 2)
        self.assertTrue(all(
            request.headers.get("authorization") is None
            for request in image_requests
        ))

    async def test_maps_forms_http_failures_without_echoing_response_body(self):
        scenarios = (
            (401, GoogleFormsErrorCode.AUTHENTICATION_REQUIRED, False),
            (403, GoogleFormsErrorCode.PERMISSION_DENIED, False),
            (404, GoogleFormsErrorCode.FORM_NOT_FOUND, False),
            (429, GoogleFormsErrorCode.RATE_LIMITED, True),
            (503, GoogleFormsErrorCode.PROVIDER_UNAVAILABLE, True),
        )

        for status, expected_code, retryable in scenarios:
            with self.subTest(status=status):
                async def handler(request: httpx.Request) -> httpx.Response:
                    return httpx.Response(
                        status,
                        json={"error": "provider-body-secret"},
                    )

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ) as http_client:
                    connector = GoogleFormsClient(
                        http_client,
                        lambda: {"Authorization": "Bearer auth-secret"},
                        forms_api_base=FORMS_API_BASE,
                    )
                    with self.assertRaises(GoogleFormsConnectorError) as caught:
                        await connector.fetch_form(FORM_ID)

                error = caught.exception
                self.assertEqual(error.code, expected_code)
                self.assertEqual(error.status_code, status)
                self.assertEqual(error.retryable, retryable)
                self.assertNotIn("provider-body-secret", str(error))
                self.assertNotIn("auth-secret", str(error))

    async def test_rejects_bad_mime_signature_and_declared_size(self):
        scenarios = (
            (
                {"Content-Type": "text/html"},
                b"<html>not an image</html>",
                GoogleFormsErrorCode.IMAGE_CONTENT_TYPE_REJECTED,
            ),
            (
                {"Content-Type": "image/png"},
                b"not-a-png",
                GoogleFormsErrorCode.IMAGE_SIGNATURE_MISMATCH,
            ),
            (
                {"Content-Type": "image/png"},
                PNG_SIGNATURE + (b"x" * 20),
                GoogleFormsErrorCode.IMAGE_TOO_LARGE,
            ),
        )

        for headers, content, expected_code in scenarios:
            with self.subTest(expected_code=expected_code):
                payload = _payload_with_one_image(
                    "https://lh3.googleusercontent.com/image?temporary=secret"
                )

                async def handler(request: httpx.Request) -> httpx.Response:
                    if request.url.host == "forms.googleapis.test":
                        return httpx.Response(200, json=payload)
                    return httpx.Response(200, headers=headers, content=content)

                policy = GoogleImageDownloadPolicy(
                    max_image_bytes=16,
                    max_total_bytes=64,
                )
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ) as http_client:
                    connector = GoogleFormsClient(
                        http_client,
                        lambda: {"Authorization": "Bearer auth-secret"},
                        forms_api_base=FORMS_API_BASE,
                        image_policy=policy,
                    )
                    with self.assertRaises(GoogleFormsConnectorError) as caught:
                        await connector.fetch_form(FORM_ID)

                self.assertEqual(caught.exception.code, expected_code)
                self.assertNotIn("temporary=secret", str(caught.exception))

    async def test_enforces_actual_streamed_size_without_content_length(self):
        payload = _payload_with_one_image(
            "https://lh3.googleusercontent.com/chunked?temporary=secret"
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "forms.googleapis.test":
                return httpx.Response(200, json=payload)
            return httpx.Response(
                200,
                headers={"Content-Type": "image/png"},
                stream=_AsyncChunks(PNG_SIGNATURE, b"x" * 9),
            )

        policy = GoogleImageDownloadPolicy(
            max_image_bytes=16,
            max_total_bytes=64,
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            connector = GoogleFormsClient(
                http_client,
                lambda: {"Authorization": "Bearer auth-secret"},
                forms_api_base=FORMS_API_BASE,
                image_policy=policy,
            )
            with self.assertRaises(GoogleFormsConnectorError) as caught:
                await connector.fetch_form(FORM_ID)

        self.assertEqual(
            caught.exception.code,
            GoogleFormsErrorCode.IMAGE_TOO_LARGE,
        )

    async def test_image_http_and_transport_errors_are_structured(self):
        payload = _payload_with_one_image(
            "https://lh3.googleusercontent.com/image?temporary=secret"
        )

        async def status_handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "forms.googleapis.test":
                return httpx.Response(200, json=payload)
            return httpx.Response(503, content=b"provider-image-error-secret")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(status_handler)
        ) as http_client:
            connector = GoogleFormsClient(
                http_client,
                lambda: {"Authorization": "Bearer auth-secret"},
                forms_api_base=FORMS_API_BASE,
            )
            with self.assertRaises(GoogleFormsConnectorError) as caught:
                await connector.fetch_form(FORM_ID)

        error = caught.exception
        self.assertEqual(error.code, GoogleFormsErrorCode.IMAGE_HTTP_ERROR)
        self.assertEqual(error.status_code, 503)
        self.assertTrue(error.retryable)
        self.assertNotIn("provider-image-error-secret", str(error))

        async def transport_handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "forms.googleapis.test":
                return httpx.Response(200, json=payload)
            raise httpx.ReadTimeout("transport-secret", request=request)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport_handler)
        ) as http_client:
            connector = GoogleFormsClient(
                http_client,
                lambda: {"Authorization": "Bearer auth-secret"},
                forms_api_base=FORMS_API_BASE,
            )
            with self.assertRaises(GoogleFormsConnectorError) as caught:
                await connector.fetch_form(FORM_ID)

        error = caught.exception
        self.assertEqual(error.code, GoogleFormsErrorCode.TRANSPORT_ERROR)
        self.assertTrue(error.retryable)
        self.assertNotIn("transport-secret", str(error))

    async def test_invalid_json_identity_and_content_uri_are_structured(self):
        scenarios = []

        async def invalid_json_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"{not-json provider-secret")

        scenarios.append((
            invalid_json_handler,
            GoogleFormsErrorCode.FORMS_INVALID_JSON,
        ))

        mismatched = _fixture()
        mismatched["formId"] = "DIFFERENT_SYNTHETIC_FORM"

        async def mismatch_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=mismatched)

        scenarios.append((
            mismatch_handler,
            GoogleFormsErrorCode.FORMS_INVALID_RESPONSE,
        ))

        malformed_image = _payload_with_one_image(42)

        async def malformed_image_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=malformed_image)

        scenarios.append((
            malformed_image_handler,
            GoogleFormsErrorCode.FORMS_INVALID_RESPONSE,
        ))

        for handler, expected_code in scenarios:
            with self.subTest(expected_code=expected_code):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ) as http_client:
                    connector = GoogleFormsClient(
                        http_client,
                        lambda: {"Authorization": "Bearer auth-secret"},
                        forms_api_base=FORMS_API_BASE,
                    )
                    with self.assertRaises(GoogleFormsConnectorError) as caught:
                        await connector.fetch_form(FORM_ID)
                self.assertEqual(caught.exception.code, expected_code)
                self.assertNotIn("provider-secret", str(caught.exception))
                self.assertNotIn("auth-secret", str(caught.exception))

    async def test_invalid_form_id_and_authorization_fail_before_network(self):
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(500)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            connector = GoogleFormsClient(
                http_client,
                lambda: {"Authorization": "Bearer auth-secret"},
                forms_api_base=FORMS_API_BASE,
            )
            with self.assertRaises(GoogleFormsConnectorError) as caught:
                await connector.fetch_form("../../unsafe")

        self.assertEqual(caught.exception.code, GoogleFormsErrorCode.INVALID_FORM_ID)
        self.assertEqual(requests, [])

        def broken_authorization() -> dict[str, str]:
            raise RuntimeError("authorization-provider-secret")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            connector = GoogleFormsClient(
                http_client,
                broken_authorization,
                forms_api_base=FORMS_API_BASE,
            )
            with self.assertRaises(GoogleFormsConnectorError) as caught:
                await connector.fetch_form(FORM_ID)

        self.assertEqual(
            caught.exception.code,
            GoogleFormsErrorCode.AUTHORIZATION_FAILED,
        )
        self.assertNotIn("authorization-provider-secret", str(caught.exception))
        self.assertEqual(requests, [])

    async def test_input_fixture_is_not_mutated_by_runtime_uri_injection(self):
        original = _fixture()
        injected = deepcopy(original)
        _at_path(injected, IMAGE_PATHS[0])["contentUri"] = (
            "https://lh3.googleusercontent.com/image?temporary=secret"
        )

        self.assertEqual(original, _fixture())
        self.assertNotEqual(original, injected)


if __name__ == "__main__":
    unittest.main()
