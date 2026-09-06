from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch

import httpx

from app.integrations import google_forms_service_account_client as module
from app.integrations.google_forms_client import (
    GoogleFormsConnectorError,
    GoogleFormsErrorCode,
    GoogleFormsStage,
)
from app.integrations.google_forms_service_account_client import (
    GOOGLE_FORMS_BODY_READONLY_SCOPE,
    GOOGLE_FORMS_RESPONSES_READONLY_SCOPE,
    GoogleFormsServiceAccountClient,
)


OWNER_REF = "owner-secret@example.test"
FORM_ID = "FORM_SYNTHETIC_001"
FORMS_API_BASE = "https://forms.googleapis.com/v1"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
CREDENTIAL_PATH = Path("C:/outside-workspace/service-account-secret.json")
SERVICE_ACCOUNT_EMAIL = "forms-reader@example-project.iam.gserviceaccount.com"
ACCESS_TOKEN = "synthetic-access-token-secret"


class _FakeCredentials:
    def __init__(
        self,
        *,
        service_account_email: str = SERVICE_ACCOUNT_EMAIL,
        valid: bool = False,
        token: str | None = None,
        refresh_error: Exception | None = None,
        token_endpoint: str = TOKEN_ENDPOINT,
    ) -> None:
        self.service_account_email = service_account_email
        self._valid = valid
        self.token = token
        self.refresh_error = refresh_error
        self.token_endpoint = token_endpoint
        self.refresh_calls = 0

    @property
    def valid(self) -> bool:
        return self._valid

    def refresh(self, request) -> None:
        self.refresh_calls += 1
        if self.refresh_error is not None:
            raise self.refresh_error
        response = request(
            self.token_endpoint,
            method="POST",
            body=b"assertion=credential-material-secret",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=999,
            allow_redirects=True,
        )
        if response.status != 200:
            raise RuntimeError(
                "token response secret: " + response.data.decode("utf-8")
            )
        payload = json.loads(response.data.decode("utf-8"))
        self.token = payload["access_token"]
        self._valid = True


class _FakeSigner:
    key_id = "synthetic-key-id"

    def sign(self, message: bytes) -> bytes:
        return b"synthetic-signature"


def _forms_payload(*, form_id: str = FORM_ID) -> dict[str, object]:
    return {
        "formId": form_id,
        "info": {"title": "Synthetic form"},
        "items": [],
    }


def _build_client(
    credentials: Any,
    **kwargs,
) -> tuple[GoogleFormsServiceAccountClient, object]:
    with patch.object(
        module.service_account.Credentials,
        "from_service_account_file",
        return_value=credentials,
    ) as factory:
        client = GoogleFormsServiceAccountClient(
            CREDENTIAL_PATH,
            forms_api_base=FORMS_API_BASE,
            **kwargs,
        )
    return client, factory


class GoogleFormsServiceAccountClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_responses_uses_scoped_token_and_responses_endpoint(self):
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={
                "responses": [{
                    "responseId": "response-1",
                    "createTime": "2026-08-25T00:00:00Z",
                    "lastSubmittedTime": "2026-08-25T00:01:00Z",
                    "answers": {},
                }],
            })

        client, factory = _build_client(
            _FakeCredentials(valid=True, token=ACCESS_TOKEN),
            http_transport=httpx.MockTransport(handler),
        )
        capture = await client.fetch_responses(OWNER_REF, FORM_ID)

        self.assertEqual(len(capture.responses), 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, f"/v1/forms/{FORM_ID}/responses")
        self.assertEqual(requests[0].url.params["pageSize"], "5000")
        self.assertEqual(requests[0].headers["authorization"], f"Bearer {ACCESS_TOKEN}")
        factory.assert_called_once_with(
            str(CREDENTIAL_PATH),
            scopes=(
                GOOGLE_FORMS_BODY_READONLY_SCOPE,
                GOOGLE_FORMS_RESPONSES_READONLY_SCOPE,
            ),
        )

    async def test_google_credentials_refresh_protocol_is_compatible(self):
        credentials = module.service_account.Credentials(
            _FakeSigner(),
            service_account_email=SERVICE_ACCOUNT_EMAIL,
            token_uri=TOKEN_ENDPOINT,
            scopes=(
                GOOGLE_FORMS_BODY_READONLY_SCOPE,
                GOOGLE_FORMS_RESPONSES_READONLY_SCOPE,
            ),
        )

        def token_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "access_token": ACCESS_TOKEN,
                "expires_in": 3600,
                "token_type": "Bearer",
            })

        async def forms_handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                request.headers["authorization"],
                f"Bearer {ACCESS_TOKEN}",
            )
            return httpx.Response(200, json=_forms_payload())

        client, _ = _build_client(
            credentials,
            token_transport=httpx.MockTransport(token_handler),
            http_transport=httpx.MockTransport(forms_handler),
        )

        capture = await client.fetch_form(OWNER_REF, FORM_ID)

        self.assertTrue(credentials.valid)
        self.assertEqual(capture.form_id, FORM_ID)

    async def test_refreshes_with_readonly_scope_and_bounded_http(self):
        token_requests: list[httpx.Request] = []
        forms_requests: list[httpx.Request] = []

        def token_handler(request: httpx.Request) -> httpx.Response:
            token_requests.append(request)
            return httpx.Response(
                200,
                json={"access_token": ACCESS_TOKEN},
            )

        async def forms_handler(request: httpx.Request) -> httpx.Response:
            forms_requests.append(request)
            return httpx.Response(200, json=_forms_payload())

        credentials = _FakeCredentials()
        client, factory = _build_client(
            credentials,
            connect_timeout=1.25,
            read_timeout=2.5,
            token_transport=httpx.MockTransport(token_handler),
            http_transport=httpx.MockTransport(forms_handler),
        )

        capture = await client.fetch_form(OWNER_REF, FORM_ID)

        self.assertEqual(capture.form_id, FORM_ID)
        self.assertEqual(client.service_account_email, SERVICE_ACCOUNT_EMAIL)
        factory.assert_called_once_with(
            str(CREDENTIAL_PATH),
            scopes=(
                GOOGLE_FORMS_BODY_READONLY_SCOPE,
                GOOGLE_FORMS_RESPONSES_READONLY_SCOPE,
            ),
        )
        self.assertEqual(credentials.refresh_calls, 1)
        self.assertEqual(len(token_requests), 1)
        token_request = token_requests[0]
        self.assertEqual(token_request.method, "POST")
        self.assertEqual(str(token_request.url), TOKEN_ENDPOINT)
        self.assertEqual(
            token_request.extensions["timeout"],
            {
                "connect": 1.25,
                "read": 2.5,
                "write": 2.5,
                "pool": 1.25,
            },
        )

        self.assertEqual(len(forms_requests), 1)
        forms_request = forms_requests[0]
        self.assertEqual(
            str(forms_request.url),
            f"{FORMS_API_BASE}/forms/{FORM_ID}",
        )
        self.assertEqual(
            forms_request.headers["authorization"],
            f"Bearer {ACCESS_TOKEN}",
        )
        self.assertEqual(
            forms_request.extensions["timeout"],
            {
                "connect": 1.25,
                "read": 2.5,
                "write": 2.5,
                "pool": 1.25,
            },
        )

    async def test_reuses_valid_short_lived_token(self):
        token_request_count = 0
        forms_request_count = 0

        def token_handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_request_count
            token_request_count += 1
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN})

        async def forms_handler(request: httpx.Request) -> httpx.Response:
            nonlocal forms_request_count
            forms_request_count += 1
            return httpx.Response(200, json=_forms_payload())

        credentials = _FakeCredentials()
        client, _ = _build_client(
            credentials,
            token_transport=httpx.MockTransport(token_handler),
            http_transport=httpx.MockTransport(forms_handler),
        )

        await client.fetch_form(OWNER_REF, FORM_ID)
        await client.fetch_form(OWNER_REF, FORM_ID)

        self.assertEqual(credentials.refresh_calls, 1)
        self.assertEqual(token_request_count, 1)
        self.assertEqual(forms_request_count, 2)

    def test_credential_load_failure_is_safe_invalid_configuration(self):
        leaked_detail = "private-key-material-secret"
        with patch.object(
            module.service_account.Credentials,
            "from_service_account_file",
            side_effect=ValueError(leaked_detail),
        ):
            with self.assertRaises(GoogleFormsConnectorError) as caught:
                GoogleFormsServiceAccountClient(
                    CREDENTIAL_PATH,
                    forms_api_base=FORMS_API_BASE,
                )

        error = caught.exception
        self.assertEqual(error.code, GoogleFormsErrorCode.INVALID_CONFIGURATION)
        self.assertEqual(error.stage, GoogleFormsStage.CONFIGURATION)
        self.assertIsNone(error.__cause__)
        rendered = str(error)
        self.assertNotIn(leaked_detail, rendered)
        self.assertNotIn(str(CREDENTIAL_PATH), rendered)

    def test_service_account_email_is_validated_and_read_only(self):
        credentials = _FakeCredentials()
        client, _ = _build_client(credentials)

        with self.assertRaises(AttributeError):
            client.service_account_email = "replacement@example.test"

        invalid_values = (
            "",
            " leading@example.test",
            "control\n@example.test",
            "x" * 321,
        )
        for invalid_value in invalid_values:
            with self.subTest(invalid_value_length=len(invalid_value)):
                with patch.object(
                    module.service_account.Credentials,
                    "from_service_account_file",
                    return_value=_FakeCredentials(
                        service_account_email=invalid_value,
                    ),
                ):
                    with self.assertRaises(GoogleFormsConnectorError) as caught:
                        GoogleFormsServiceAccountClient(
                            CREDENTIAL_PATH,
                            forms_api_base=FORMS_API_BASE,
                        )
                self.assertEqual(
                    caught.exception.code,
                    GoogleFormsErrorCode.INVALID_CONFIGURATION,
                )
                if invalid_value:
                    self.assertNotIn(invalid_value, str(caught.exception))

    async def test_refresh_failure_is_safe_authorization_failed(self):
        leaked_detail = "credential-refresh-token-secret"
        credentials = _FakeCredentials(
            refresh_error=RuntimeError(
                f"{leaked_detail}:{OWNER_REF}:{FORM_ID}:{CREDENTIAL_PATH}"
            )
        )

        async def unexpected_forms_request(
            request: httpx.Request,
        ) -> httpx.Response:
            self.fail("Forms API must not be called after refresh failure")

        client, _ = _build_client(
            credentials,
            http_transport=httpx.MockTransport(unexpected_forms_request),
        )

        with self.assertRaises(GoogleFormsConnectorError) as caught:
            await client.fetch_form(OWNER_REF, FORM_ID)

        error = caught.exception
        self.assertEqual(error.code, GoogleFormsErrorCode.AUTHORIZATION_FAILED)
        self.assertEqual(error.stage, GoogleFormsStage.AUTHORIZATION)
        self.assertIsNone(error.__cause__)
        rendered = str(error)
        for secret in (
            leaked_detail,
            OWNER_REF,
            FORM_ID,
            str(CREDENTIAL_PATH),
        ):
            self.assertNotIn(secret, rendered)

    async def test_token_redirect_is_not_followed_and_error_is_safe(self):
        token_requests: list[httpx.Request] = []

        def token_handler(request: httpx.Request) -> httpx.Response:
            token_requests.append(request)
            return httpx.Response(
                302,
                headers={"Location": "https://attacker.example/token-secret"},
                content=b"provider-response-secret",
            )

        credentials = _FakeCredentials()
        client, _ = _build_client(
            credentials,
            token_transport=httpx.MockTransport(token_handler),
        )

        with self.assertRaises(GoogleFormsConnectorError) as caught:
            await client.fetch_form(OWNER_REF, FORM_ID)

        self.assertEqual(len(token_requests), 1)
        self.assertEqual(
            caught.exception.code,
            GoogleFormsErrorCode.AUTHORIZATION_FAILED,
        )
        rendered = str(caught.exception)
        self.assertNotIn("attacker.example", rendered)
        self.assertNotIn("provider-response-secret", rendered)

    async def test_only_exact_official_token_endpoints_are_allowed(self):
        rejected_endpoints = (
            "http://oauth2.googleapis.com/token",
            "https://oauth2.googleapis.com:443/token",
            "https://oauth2.googleapis.com/token?redirect=secret",
            "https://oauth2.googleapis.com/token#secret",
            "https://user@oauth2.googleapis.com/token",
            "https://oauth2.googleapis.com/other",
            "https://attacker.example/token",
        )

        token_request_count = 0

        def unexpected_token_request(request: httpx.Request) -> httpx.Response:
            nonlocal token_request_count
            token_request_count += 1
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN})

        with httpx.Client(
            transport=httpx.MockTransport(unexpected_token_request)
        ) as sync_client:
            token_request = module._BoundedTokenRequest(
                sync_client,
                connect_timeout=1.0,
                read_timeout=2.0,
            )
            for endpoint in rejected_endpoints:
                with self.subTest(endpoint_shape=endpoint.split(":", 1)[0]):
                    with self.assertRaises(
                        module.google_auth_exceptions.TransportError
                    ) as caught:
                        token_request(endpoint, method="POST")
                    self.assertNotIn(endpoint, str(caught.exception))
        self.assertEqual(token_request_count, 0)

        legacy_requests = 0

        def legacy_token_handler(request: httpx.Request) -> httpx.Response:
            nonlocal legacy_requests
            legacy_requests += 1
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN})

        async def forms_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_forms_payload())

        legacy_credentials = _FakeCredentials(
            token_endpoint="https://accounts.google.com/o/oauth2/token"
        )
        legacy_client, _ = _build_client(
            legacy_credentials,
            token_transport=httpx.MockTransport(legacy_token_handler),
            http_transport=httpx.MockTransport(forms_handler),
        )
        await legacy_client.fetch_form(OWNER_REF, FORM_ID)
        self.assertEqual(legacy_requests, 1)

    async def test_invalid_access_tokens_never_enter_request_headers(self):
        invalid_tokens = (
            "",
            " leading-token",
            "trailing-token ",
            "token\nheader-injection",
            "x" * 8193,
        )

        for invalid_token in invalid_tokens:
            with self.subTest(token_length=len(invalid_token)):
                forms_request_count = 0

                async def unexpected_forms_request(
                    request: httpx.Request,
                ) -> httpx.Response:
                    nonlocal forms_request_count
                    forms_request_count += 1
                    return httpx.Response(200, json=_forms_payload())

                credentials = _FakeCredentials(
                    valid=True,
                    token=invalid_token,
                )
                client, _ = _build_client(
                    credentials,
                    http_transport=httpx.MockTransport(
                        unexpected_forms_request
                    ),
                )

                with self.assertRaises(GoogleFormsConnectorError) as caught:
                    await client.fetch_form(OWNER_REF, FORM_ID)

                self.assertEqual(forms_request_count, 0)
                self.assertEqual(
                    caught.exception.code,
                    GoogleFormsErrorCode.AUTHORIZATION_FAILED,
                )
                if invalid_token:
                    self.assertNotIn(invalid_token, str(caught.exception))

    async def test_forms_redirect_is_not_followed(self):
        forms_requests: list[httpx.Request] = []

        async def forms_handler(request: httpx.Request) -> httpx.Response:
            forms_requests.append(request)
            return httpx.Response(
                302,
                headers={
                    "Location": "https://attacker.example/forms/redirect-secret"
                },
            )

        credentials = _FakeCredentials(valid=True, token=ACCESS_TOKEN)
        client, _ = _build_client(
            credentials,
            http_transport=httpx.MockTransport(forms_handler),
        )

        with self.assertRaises(GoogleFormsConnectorError) as caught:
            await client.fetch_form(OWNER_REF, FORM_ID)

        error = caught.exception
        self.assertEqual(len(forms_requests), 1)
        self.assertEqual(error.code, GoogleFormsErrorCode.FORMS_HTTP_ERROR)
        self.assertEqual(error.status_code, 302)
        self.assertNotIn("attacker.example", str(error))
        self.assertNotIn(FORM_ID, str(error))

    async def test_default_image_policy_rejects_non_google_host(self):
        forms_requests: list[httpx.Request] = []
        payload = _forms_payload()
        payload["items"] = [{
            "itemId": "image-item",
            "imageItem": {
                "image": {
                    "contentUri": "https://attacker.example/image-secret",
                }
            },
        }]

        async def forms_handler(request: httpx.Request) -> httpx.Response:
            forms_requests.append(request)
            if request.url.host != "forms.googleapis.com":
                self.fail("Rejected image host must not receive a request")
            return httpx.Response(200, json=payload)

        credentials = _FakeCredentials(valid=True, token=ACCESS_TOKEN)
        client, _ = _build_client(
            credentials,
            http_transport=httpx.MockTransport(forms_handler),
        )

        capture = await client.fetch_form(OWNER_REF, FORM_ID)

        self.assertEqual(len(forms_requests), 1)
        self.assertEqual(capture.images, ())
        self.assertEqual(len(capture.image_failures), 1)
        self.assertEqual(
            capture.image_failures[0].code,
            GoogleFormsErrorCode.IMAGE_URL_REJECTED,
        )
        self.assertNotIn("image-secret", repr(capture.raw_form))

    def test_invalid_constructor_values_fail_before_loading_credentials(self):
        invalid_cases = (
            {"connect_timeout": 0},
            {"connect_timeout": float("inf")},
            {"read_timeout": True},
            {"forms_api_base": "https://attacker.example/v1"},
            {"http_transport": object()},
            {"token_transport": object()},
        )
        with patch.object(
            module.service_account.Credentials,
            "from_service_account_file",
        ) as factory:
            for invalid_kwargs in invalid_cases:
                with self.subTest(kwargs=invalid_kwargs):
                    kwargs = dict(invalid_kwargs)
                    with self.assertRaises(GoogleFormsConnectorError) as caught:
                        forms_api_base = kwargs.pop(
                            "forms_api_base",
                            FORMS_API_BASE,
                        )
                        GoogleFormsServiceAccountClient(
                            CREDENTIAL_PATH,
                            forms_api_base=forms_api_base,
                            **kwargs,
                        )
                    self.assertEqual(
                        caught.exception.code,
                        GoogleFormsErrorCode.INVALID_CONFIGURATION,
                    )
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
