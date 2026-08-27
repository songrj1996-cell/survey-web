"""Bounded read-only acquisition for Google Forms responses.

The integration owns only the ``forms.responses.list`` protocol.  It never
logs or persists response bodies, authorization headers, form identifiers, or
respondent data.  Pagination is completed before a validated immutable
capture is returned to the service layer.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum

import httpx


AuthorizationProvider = Callable[
    [],
    Mapping[str, str] | Awaitable[Mapping[str, str]],
]


class GoogleFormsResponsesErrorCode(str, Enum):
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_FORM_ID = "invalid_form_id"
    AUTHORIZATION_FAILED = "authorization_failed"
    AUTHENTICATION_REQUIRED = "authentication_required"
    PERMISSION_DENIED = "permission_denied"
    FORM_NOT_FOUND = "form_not_found"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    HTTP_ERROR = "http_error"
    INVALID_JSON = "invalid_json"
    INVALID_RESPONSE = "invalid_response"
    PAGINATION_LOOP = "pagination_loop"
    TOO_MANY_RESPONSES = "too_many_responses"
    RESPONSE_TOO_LARGE = "response_too_large"
    TRANSPORT_ERROR = "transport_error"


class GoogleFormsResponsesClientError(RuntimeError):
    """Safe error without provider bodies, tokens, form IDs, or PII."""

    def __init__(
        self,
        code: GoogleFormsResponsesErrorCode,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class GoogleFileUploadAnswer:
    file_id: str
    file_name: str
    mime_type: str


@dataclass(frozen=True, slots=True)
class GoogleResponseAnswer:
    question_id: str
    text_values: tuple[str, ...] = ()
    file_uploads: tuple[GoogleFileUploadAnswer, ...] = ()


@dataclass(frozen=True, slots=True)
class GoogleFormResponse:
    response_id: str
    create_time: str
    last_submitted_time: str
    respondent_email: str | None
    answers: tuple[GoogleResponseAnswer, ...]


@dataclass(frozen=True, slots=True)
class GoogleFormResponsesCapture:
    form_id: str
    responses: tuple[GoogleFormResponse, ...]
    page_count: int


_FORM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_FORBIDDEN_AUTH_HEADERS = frozenset({
    "connection",
    "content-length",
    "cookie",
    "host",
    "proxy-authorization",
    "transfer-encoding",
})
_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429})
_MAX_PAGE_TOKEN_LENGTH = 4096
_MAX_RESPONSE_ID_LENGTH = 1024
_MAX_QUESTION_ID_LENGTH = 1024
_MAX_TIMESTAMP_LENGTH = 128
_MAX_TEXT_VALUE_LENGTH = 2 * 1024 * 1024
_MAX_FILE_FIELD_LENGTH = 4096
_READ_CHUNK_BYTES = 64 * 1024


def _client_error(
    code: GoogleFormsResponsesErrorCode,
    message: str,
    *,
    retryable: bool = False,
    status_code: int | None = None,
) -> GoogleFormsResponsesClientError:
    return GoogleFormsResponsesClientError(
        code,
        message,
        retryable=retryable,
        status_code=status_code,
    )


def _forms_api_base(value: str) -> httpx.URL:
    try:
        base = httpx.URL(value)
    except (TypeError, ValueError):
        raise _client_error(
            GoogleFormsResponsesErrorCode.INVALID_CONFIGURATION,
            "Google Forms API base is invalid",
        ) from None
    if (
        base.scheme != "https"
        or base.host != "forms.googleapis.com"
        or base.userinfo
        or base.port is not None
        or base.query
        or base.fragment
        or base.path.rstrip("/") != "/v1"
    ):
        raise _client_error(
            GoogleFormsResponsesErrorCode.INVALID_CONFIGURATION,
            "Google Forms API base must use the official v1 endpoint",
        )
    return base


def _positive_int(value: int, *, label: str, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
        or value > maximum
    ):
        raise _client_error(
            GoogleFormsResponsesErrorCode.INVALID_CONFIGURATION,
            f"{label} is outside the supported range",
        )
    return value


def _required_string(value: object, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError
    return value


def _optional_string(value: object, *, maximum: int) -> str | None:
    if value is None or value == "":
        return None
    return _required_string(value, maximum=maximum)


def _http_error(status_code: int) -> GoogleFormsResponsesClientError:
    if status_code == 401:
        code = GoogleFormsResponsesErrorCode.AUTHENTICATION_REQUIRED
        message = "Google Forms responses authorization is missing or expired"
    elif status_code == 403:
        code = GoogleFormsResponsesErrorCode.PERMISSION_DENIED
        message = "Google Forms responses access was denied"
    elif status_code == 404:
        code = GoogleFormsResponsesErrorCode.FORM_NOT_FOUND
        message = "Google form responses were not found"
    elif status_code == 429:
        code = GoogleFormsResponsesErrorCode.RATE_LIMITED
        message = "Google Forms responses rate limit was reached"
    elif status_code >= 500:
        code = GoogleFormsResponsesErrorCode.PROVIDER_UNAVAILABLE
        message = "Google Forms responses service is unavailable"
    else:
        code = GoogleFormsResponsesErrorCode.HTTP_ERROR
        message = "Google Forms responses returned an unexpected status"
    return _client_error(
        code,
        message,
        retryable=(status_code in _RETRYABLE_STATUSES or status_code >= 500),
        status_code=status_code,
    )


def _answer(value: object, map_question_id: str) -> GoogleResponseAnswer:
    if not isinstance(value, dict):
        raise ValueError
    question_id = _required_string(
        value.get("questionId"),
        maximum=_MAX_QUESTION_ID_LENGTH,
    )
    if question_id != map_question_id:
        raise ValueError
    text_payload = value.get("textAnswers")
    file_payload = value.get("fileUploadAnswers")
    if (text_payload is None) == (file_payload is None):
        raise ValueError

    if text_payload is not None:
        if not isinstance(text_payload, dict):
            raise ValueError
        raw_answers = text_payload.get("answers")
        if not isinstance(raw_answers, list):
            raise ValueError
        texts: list[str] = []
        for raw in raw_answers:
            if not isinstance(raw, dict):
                raise ValueError
            text = raw.get("value")
            if not isinstance(text, str) or len(text) > _MAX_TEXT_VALUE_LENGTH:
                raise ValueError
            texts.append(text)
        return GoogleResponseAnswer(question_id=question_id, text_values=tuple(texts))

    assert file_payload is not None
    if not isinstance(file_payload, dict):
        raise ValueError
    raw_files = file_payload.get("answers")
    if not isinstance(raw_files, list):
        raise ValueError
    files: list[GoogleFileUploadAnswer] = []
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise ValueError
        files.append(GoogleFileUploadAnswer(
            file_id=_required_string(
                raw.get("fileId"), maximum=_MAX_FILE_FIELD_LENGTH
            ),
            file_name=_required_string(
                raw.get("fileName"), maximum=_MAX_FILE_FIELD_LENGTH
            ),
            mime_type=_required_string(
                raw.get("mimeType"), maximum=_MAX_FILE_FIELD_LENGTH
            ),
        ))
    return GoogleResponseAnswer(question_id=question_id, file_uploads=tuple(files))


def _form_response(value: object) -> GoogleFormResponse:
    if not isinstance(value, dict):
        raise ValueError
    response_id = _required_string(
        value.get("responseId"), maximum=_MAX_RESPONSE_ID_LENGTH
    )
    create_time = _required_string(
        value.get("createTime"), maximum=_MAX_TIMESTAMP_LENGTH
    )
    last_submitted_time = _required_string(
        value.get("lastSubmittedTime"), maximum=_MAX_TIMESTAMP_LENGTH
    )
    respondent_email = _optional_string(
        value.get("respondentEmail"), maximum=320
    )
    raw_answers = value.get("answers", {})
    if not isinstance(raw_answers, dict):
        raise ValueError
    answers: list[GoogleResponseAnswer] = []
    for map_question_id, raw_answer in raw_answers.items():
        validated_map_id = _required_string(
            map_question_id, maximum=_MAX_QUESTION_ID_LENGTH
        )
        answers.append(_answer(raw_answer, validated_map_id))
    answers.sort(key=lambda item: item.question_id)
    return GoogleFormResponse(
        response_id=response_id,
        create_time=create_time,
        last_submitted_time=last_submitted_time,
        respondent_email=respondent_email,
        answers=tuple(answers),
    )


class GoogleFormsResponsesClient:
    """Read and validate every response page for one authorized form."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        authorization: AuthorizationProvider,
        *,
        forms_api_base: str,
        page_size: int = 5000,
        max_responses: int = 100_000,
        max_page_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if not isinstance(http_client, httpx.AsyncClient):
            raise _client_error(
                GoogleFormsResponsesErrorCode.INVALID_CONFIGURATION,
                "Google Forms responses HTTP client is invalid",
            )
        if not callable(authorization):
            raise _client_error(
                GoogleFormsResponsesErrorCode.INVALID_CONFIGURATION,
                "Google Forms responses authorization provider is invalid",
            )
        self._http_client = http_client
        self._authorization = authorization
        self._forms_api_base = _forms_api_base(forms_api_base)
        self._page_size = _positive_int(page_size, label="page_size", maximum=5000)
        self._max_responses = _positive_int(
            max_responses, label="max_responses", maximum=1_000_000
        )
        self._max_page_bytes = _positive_int(
            max_page_bytes,
            label="max_page_bytes",
            maximum=256 * 1024 * 1024,
        )

    async def fetch_all(self, form_id: str) -> GoogleFormResponsesCapture:
        if not isinstance(form_id, str) or not _FORM_ID_RE.fullmatch(form_id):
            raise _client_error(
                GoogleFormsResponsesErrorCode.INVALID_FORM_ID,
                "Google form ID has an invalid format",
            )
        headers = await self._authorization_headers()
        responses: list[GoogleFormResponse] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        page_count = 0

        while True:
            page_count += 1
            params: dict[str, str | int] = {"pageSize": self._page_size}
            if page_token is not None:
                params["pageToken"] = page_token
            url = self._forms_api_base.copy_with(
                path=f"/v1/forms/{form_id}/responses",
                params=params,
            )
            payload = await self._request_page(url, headers)
            raw_responses = payload.get("responses", [])
            if not isinstance(raw_responses, list):
                raise _client_error(
                    GoogleFormsResponsesErrorCode.INVALID_RESPONSE,
                    "Google Forms responses list is invalid",
                )
            try:
                parsed = [_form_response(item) for item in raw_responses]
            except (TypeError, ValueError):
                raise _client_error(
                    GoogleFormsResponsesErrorCode.INVALID_RESPONSE,
                    "Google Forms returned an invalid response record",
                ) from None
            responses.extend(parsed)
            if len(responses) > self._max_responses:
                raise _client_error(
                    GoogleFormsResponsesErrorCode.TOO_MANY_RESPONSES,
                    "Google Forms response count exceeds the configured limit",
                )

            raw_next = payload.get("nextPageToken")
            if raw_next in {None, ""}:
                break
            try:
                next_token = _required_string(
                    raw_next,
                    maximum=_MAX_PAGE_TOKEN_LENGTH,
                )
            except ValueError:
                raise _client_error(
                    GoogleFormsResponsesErrorCode.INVALID_RESPONSE,
                    "Google Forms returned an invalid page token",
                ) from None
            if next_token in seen_tokens:
                raise _client_error(
                    GoogleFormsResponsesErrorCode.PAGINATION_LOOP,
                    "Google Forms repeated a response page token",
                )
            seen_tokens.add(next_token)
            page_token = next_token

        return GoogleFormResponsesCapture(
            form_id=form_id,
            responses=tuple(responses),
            page_count=page_count,
        )

    async def _authorization_headers(self) -> dict[str, str]:
        try:
            provided = self._authorization()
            if inspect.isawaitable(provided):
                provided = await provided
        except Exception:
            raise _client_error(
                GoogleFormsResponsesErrorCode.AUTHORIZATION_FAILED,
                "Google Forms responses authorization could not be prepared",
            ) from None
        if not isinstance(provided, Mapping):
            raise _client_error(
                GoogleFormsResponsesErrorCode.AUTHORIZATION_FAILED,
                "Google Forms responses authorization is invalid",
            )
        headers: dict[str, str] = {}
        for name, value in provided.items():
            lowered = name.lower() if isinstance(name, str) else ""
            if (
                not isinstance(name, str)
                or not _HEADER_NAME_RE.fullmatch(name)
                or not isinstance(value, str)
                or not value
                or any(ord(character) < 32 or ord(character) > 126 for character in value)
                or lowered in _FORBIDDEN_AUTH_HEADERS
            ):
                raise _client_error(
                    GoogleFormsResponsesErrorCode.AUTHORIZATION_FAILED,
                    "Google Forms responses authorization is invalid",
                )
            headers[lowered] = value
        if not headers:
            raise _client_error(
                GoogleFormsResponsesErrorCode.AUTHORIZATION_FAILED,
                "Google Forms responses authorization is empty",
            )
        return headers

    async def _request_page(
        self,
        url: httpx.URL,
        authorization_headers: Mapping[str, str],
    ) -> dict[str, object]:
        request = self._http_client.build_request(
            "GET",
            url,
            headers={**authorization_headers, "Accept": "application/json"},
        )
        try:
            response = await self._http_client.send(
                request,
                auth=None,
                follow_redirects=False,
                stream=True,
            )
        except httpx.RequestError:
            raise _client_error(
                GoogleFormsResponsesErrorCode.TRANSPORT_ERROR,
                "Google Forms responses request failed",
                retryable=True,
            ) from None
        try:
            if response.status_code != 200:
                raise _http_error(response.status_code)
            declared_length = response.headers.get("content-length")
            if declared_length is not None:
                try:
                    parsed_length = int(declared_length)
                except ValueError:
                    raise _client_error(
                        GoogleFormsResponsesErrorCode.INVALID_RESPONSE,
                        "Google Forms responses Content-Length is invalid",
                    ) from None
                if parsed_length < 0 or parsed_length > self._max_page_bytes:
                    raise _client_error(
                        GoogleFormsResponsesErrorCode.RESPONSE_TOO_LARGE,
                        "Google Forms response page is too large",
                    )
            content = bytearray()
            async for chunk in response.aiter_bytes(_READ_CHUNK_BYTES):
                content.extend(chunk)
                if len(content) > self._max_page_bytes:
                    raise _client_error(
                        GoogleFormsResponsesErrorCode.RESPONSE_TOO_LARGE,
                        "Google Forms response page is too large",
                    )
        finally:
            await response.aclose()
        try:
            payload = json.loads(bytes(content))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _client_error(
                GoogleFormsResponsesErrorCode.INVALID_JSON,
                "Google Forms responses returned invalid JSON",
            ) from None
        if not isinstance(payload, dict):
            raise _client_error(
                GoogleFormsResponsesErrorCode.INVALID_RESPONSE,
                "Google Forms responses root is invalid",
            )
        return payload


__all__ = [
    "GoogleFileUploadAnswer",
    "GoogleFormResponse",
    "GoogleFormResponsesCapture",
    "GoogleFormsResponsesClient",
    "GoogleFormsResponsesClientError",
    "GoogleFormsResponsesErrorCode",
    "GoogleResponseAnswer",
]
