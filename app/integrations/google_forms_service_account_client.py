"""Deployment service-account authorization for read-only Google Forms imports.

The adapter owns only the short-lived authorization lifecycle.  It loads one
deployment credential, refreshes access tokens without blocking the event
loop, and delegates Forms/image acquisition to :class:`GoogleFormsClient`.
Credential contents, access tokens, owner references, and form identifiers are
never logged or included in connector errors.
"""

from __future__ import annotations

import asyncio
import math
import os
from collections.abc import Mapping
from typing import Any

import httpx
from google.auth import exceptions as google_auth_exceptions
from google.auth.transport import Request as GoogleAuthRequest
from google.auth.transport import Response as GoogleAuthResponse
from google.oauth2 import service_account

from app.integrations.google_forms_client import (
    GoogleFormCapture,
    GoogleFormsClient,
    GoogleFormsConnectorError,
    GoogleFormsErrorCode,
    GoogleFormsStage,
    GoogleImageDownloadPolicy,
)


__all__ = [
    "GOOGLE_FORMS_BODY_READONLY_SCOPE",
    "GoogleFormsServiceAccountClient",
]


GOOGLE_FORMS_BODY_READONLY_SCOPE = (
    "https://www.googleapis.com/auth/forms.body.readonly"
)
_OFFICIAL_FORMS_API_BASE = "https://forms.googleapis.com/v1"
_MAX_SERVICE_ACCOUNT_EMAIL_LENGTH = 320
_MAX_ACCESS_TOKEN_LENGTH = 8192
_MAX_TOKEN_RESPONSE_BYTES = 1024 * 1024
_TOKEN_READ_CHUNK_BYTES = 64 * 1024
_ALLOWED_TOKEN_ENDPOINTS = frozenset({
    "https://accounts.google.com/o/oauth2/token",
    "https://oauth2.googleapis.com/token",
})


def _configuration_error(message: str) -> GoogleFormsConnectorError:
    return GoogleFormsConnectorError(
        GoogleFormsErrorCode.INVALID_CONFIGURATION,
        message,
        stage=GoogleFormsStage.CONFIGURATION,
    )


def _authorization_error() -> GoogleFormsConnectorError:
    return GoogleFormsConnectorError(
        GoogleFormsErrorCode.AUTHORIZATION_FAILED,
        "Google Forms service-account authorization could not be prepared",
        stage=GoogleFormsStage.AUTHORIZATION,
    )


def _positive_timeout(value: float, *, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise _configuration_error(f"{label} must be a positive finite number")
    return float(value)


def _credential_path(value: str | os.PathLike[str]) -> str:
    try:
        path = os.fspath(value)
    except TypeError:
        raise _configuration_error(
            "Google Forms service-account credential path is invalid"
        ) from None
    if not isinstance(path, str) or not path.strip():
        raise _configuration_error(
            "Google Forms service-account credential path is invalid"
        )
    return path


def _forms_api_base(value: str) -> str:
    if value != _OFFICIAL_FORMS_API_BASE:
        raise _configuration_error(
            "Google Forms API base must use the official endpoint"
        )
    return value


def _service_account_email(credentials: Any) -> str:
    try:
        email = credentials.service_account_email
    except Exception:
        raise _configuration_error(
            "Google Forms service-account identity is invalid"
        ) from None
    if (
        not isinstance(email, str)
        or not email
        or email != email.strip()
        or len(email) > _MAX_SERVICE_ACCOUNT_EMAIL_LENGTH
        or "@" not in email
        or any(ord(character) < 33 or ord(character) > 126 for character in email)
    ):
        raise _configuration_error(
            "Google Forms service-account identity is invalid"
        )
    return email


class _BufferedGoogleAuthResponse(GoogleAuthResponse):
    """Small in-memory token response implementing google-auth's protocol."""

    def __init__(
        self,
        status: int,
        headers: Mapping[str, str],
        data: bytes,
    ) -> None:
        self._status = status
        self._headers = dict(headers)
        self._data = data

    @property
    def status(self) -> int:
        return self._status

    @property
    def headers(self) -> Mapping[str, str]:
        return self._headers

    @property
    def data(self) -> bytes:
        return self._data


class _BoundedTokenRequest(GoogleAuthRequest):
    """Synchronous google-auth transport with strict redirect/size limits."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        connect_timeout: float,
        read_timeout: float,
    ) -> None:
        self._client = client
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=read_timeout,
            pool=connect_timeout,
        )

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: object = None,
        **kwargs: object,
    ) -> GoogleAuthResponse:
        del timeout, kwargs
        if url not in _ALLOWED_TOKEN_ENDPOINTS:
            raise google_auth_exceptions.TransportError(
                "Google authorization endpoint is invalid"
            )
        try:
            parsed_url = httpx.URL(url)
        except (TypeError, ValueError):
            raise google_auth_exceptions.TransportError(
                "Google authorization endpoint is invalid"
            ) from None
        if (
            parsed_url.scheme != "https"
            or not parsed_url.host
            or parsed_url.userinfo
            or parsed_url.port is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise google_auth_exceptions.TransportError(
                "Google authorization endpoint is invalid"
            ) from None

        try:
            with self._client.stream(
                method,
                str(parsed_url),
                content=body,
                headers=dict(headers or {}),
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                declared_length = response.headers.get("content-length")
                if declared_length is not None:
                    try:
                        parsed_length = int(declared_length)
                    except (TypeError, ValueError):
                        raise google_auth_exceptions.TransportError(
                            "Google authorization response is invalid"
                        ) from None
                    if parsed_length < 0 or parsed_length > _MAX_TOKEN_RESPONSE_BYTES:
                        raise google_auth_exceptions.TransportError(
                            "Google authorization response is invalid"
                        )

                content = bytearray()
                for chunk in response.iter_bytes(_TOKEN_READ_CHUNK_BYTES):
                    content.extend(chunk)
                    if len(content) > _MAX_TOKEN_RESPONSE_BYTES:
                        raise google_auth_exceptions.TransportError(
                            "Google authorization response is too large"
                        )
                return _BufferedGoogleAuthResponse(
                    response.status_code,
                    response.headers,
                    bytes(content),
                )
        except google_auth_exceptions.TransportError:
            raise
        except httpx.HTTPError:
            raise google_auth_exceptions.TransportError(
                "Google authorization request failed"
            ) from None


def _consume_refresh_exception(task: asyncio.Task[None]) -> None:
    """Retrieve abandoned refresh failures without exposing their details."""

    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        pass


class GoogleFormsServiceAccountClient:
    """Owner-compatible Forms client backed by one deployment identity."""

    def __init__(
        self,
        service_account_file: str | os.PathLike[str],
        *,
        forms_api_base: str,
        connect_timeout: float = 15.0,
        read_timeout: float = 60.0,
        image_policy: GoogleImageDownloadPolicy | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        token_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._connect_timeout = _positive_timeout(
            connect_timeout,
            label="connect_timeout",
        )
        self._read_timeout = _positive_timeout(
            read_timeout,
            label="read_timeout",
        )
        normalized_forms_api_base = _forms_api_base(forms_api_base)
        if http_transport is not None and not isinstance(
            http_transport,
            httpx.AsyncBaseTransport,
        ):
            raise _configuration_error("Google Forms HTTP transport is invalid")
        if token_transport is not None and not isinstance(
            token_transport,
            httpx.BaseTransport,
        ):
            raise _configuration_error(
                "Google authorization HTTP transport is invalid"
            )

        credential_path = _credential_path(service_account_file)
        try:
            credentials = service_account.Credentials.from_service_account_file(
                credential_path,
                scopes=(GOOGLE_FORMS_BODY_READONLY_SCOPE,),
            )
        except Exception:
            raise _configuration_error(
                "Google Forms service-account credential could not be loaded"
            ) from None

        self._credentials = credentials
        self._service_account_email = _service_account_email(credentials)
        self._forms_api_base = normalized_forms_api_base
        self._image_policy = image_policy
        self._http_transport = http_transport
        self._token_transport = token_transport
        self._authorization_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None

    @property
    def service_account_email(self) -> str:
        """Validated non-secret identity suitable for an authorization hint."""

        return self._service_account_email

    async def fetch_form(
        self,
        owner_ref: str,
        form_id: str,
    ) -> GoogleFormCapture:
        """Fetch one form without retaining its owner or provider identifier."""

        if not isinstance(owner_ref, str) or not owner_ref.strip():
            raise _configuration_error("Google Forms owner context is invalid")

        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=self._read_timeout,
            pool=self._connect_timeout,
        )
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._http_transport,
            ) as http_client:
                connector = GoogleFormsClient(
                    http_client,
                    self._authorization_headers,
                    forms_api_base=self._forms_api_base,
                    image_policy=self._image_policy,
                )
                return await connector.fetch_form(form_id)
        except asyncio.CancelledError:
            raise
        except GoogleFormsConnectorError:
            raise
        except Exception:
            raise GoogleFormsConnectorError(
                GoogleFormsErrorCode.TRANSPORT_ERROR,
                "Google Forms request failed before receiving a response",
                stage=GoogleFormsStage.FORMS_GET,
                retryable=True,
            ) from None

    async def _authorization_headers(self) -> dict[str, str]:
        task: asyncio.Task[None] | None
        async with self._authorization_lock:
            if self._refresh_task is not None and not self._refresh_task.done():
                task = self._refresh_task
            else:
                self._refresh_task = None
                try:
                    valid = bool(self._credentials.valid)
                except Exception:
                    raise _authorization_error() from None
                if valid:
                    return self._token_header()
                task = asyncio.create_task(asyncio.to_thread(
                    self._refresh_credentials,
                ))
                task.add_done_callback(_consume_refresh_exception)
                self._refresh_task = task

        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._authorization_lock:
                if self._refresh_task is task:
                    self._refresh_task = None
            raise _authorization_error() from None

        async with self._authorization_lock:
            if self._refresh_task is task:
                self._refresh_task = None
            return self._token_header()

    def _refresh_credentials(self) -> None:
        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=self._read_timeout,
            pool=self._connect_timeout,
        )
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self._token_transport,
        ) as client:
            request = _BoundedTokenRequest(
                client,
                connect_timeout=self._connect_timeout,
                read_timeout=self._read_timeout,
            )
            self._credentials.refresh(request)

    def _token_header(self) -> dict[str, str]:
        try:
            token = self._credentials.token
        except Exception:
            raise _authorization_error() from None
        if (
            not isinstance(token, str)
            or not token
            or token != token.strip()
            or len(token) > _MAX_ACCESS_TOKEN_LENGTH
            or any(ord(character) < 33 or ord(character) > 126 for character in token)
        ):
            raise _authorization_error()
        return {"Authorization": f"Bearer {token}"}
