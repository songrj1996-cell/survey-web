"""Read-only Google Forms acquisition with ephemeral image capture.

Authentication and HTTP lifecycle are owned by callers.  This module only
translates the ``forms.get`` protocol into an in-memory result and downloads
short-lived Google image URLs before returning.  Individual image failures are
reported without discarding the form definition or other images.  The client
never persists credentials, temporary URLs, form definitions, or image bytes.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx


__all__ = [
    "AuthorizationProvider",
    "GoogleFormCapture",
    "GoogleFormImageCapture",
    "GoogleFormImageFailure",
    "GoogleFormsClient",
    "GoogleFormsConnectorError",
    "GoogleFormsErrorCode",
    "GoogleFormsStage",
    "GoogleImageContext",
    "GoogleImageDownloadPolicy",
    "JsonPathPart",
]


AuthorizationProvider = Callable[
    [],
    Mapping[str, str] | Awaitable[Mapping[str, str]],
]
JsonPathPart = str | int


class GoogleFormsErrorCode(str, Enum):
    """Stable connector errors that services can map to import issues."""

    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_FORM_ID = "invalid_form_id"
    AUTHORIZATION_FAILED = "authorization_failed"
    AUTHENTICATION_REQUIRED = "authentication_required"
    PERMISSION_DENIED = "permission_denied"
    FORM_NOT_FOUND = "form_not_found"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    FORMS_HTTP_ERROR = "forms_http_error"
    FORMS_INVALID_JSON = "forms_invalid_json"
    FORMS_INVALID_RESPONSE = "forms_invalid_response"
    TOO_MANY_IMAGES = "too_many_images"
    IMAGE_URL_REJECTED = "image_url_rejected"
    IMAGE_REDIRECT_LIMIT = "image_redirect_limit"
    IMAGE_HTTP_ERROR = "image_http_error"
    IMAGE_INVALID_RESPONSE = "image_invalid_response"
    IMAGE_CONTENT_TYPE_REJECTED = "image_content_type_rejected"
    IMAGE_TOO_LARGE = "image_too_large"
    IMAGE_SIGNATURE_MISMATCH = "image_signature_mismatch"
    TRANSPORT_ERROR = "transport_error"


class GoogleFormsStage(str, Enum):
    CONFIGURATION = "configuration"
    AUTHORIZATION = "authorization"
    FORMS_GET = "forms_get"
    IMAGE_DISCOVERY = "image_discovery"
    IMAGE_DOWNLOAD = "image_download"


class GoogleFormsConnectorError(RuntimeError):
    """A structured error whose message and context are safe to log.

    Response bodies, authorization values, form IDs, and temporary image URLs
    are intentionally excluded from both the message and ``safe_context``.
    """

    def __init__(
        self,
        code: GoogleFormsErrorCode,
        message: str,
        *,
        stage: GoogleFormsStage,
        retryable: bool = False,
        status_code: int | None = None,
        safe_context: Mapping[str, str | int] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.retryable = retryable
        self.status_code = status_code
        self.safe_context = dict(safe_context or {})


@dataclass(frozen=True, slots=True)
class GoogleImageDownloadPolicy:
    """Limits for short-lived Forms image downloads.

    A suffix matches either the exact host or a subdomain separated by ``.``;
    for example, ``googleusercontent.com`` does not match
    ``notgoogleusercontent.com``.
    """

    allowed_hosts: tuple[str, ...] = ()
    allowed_host_suffixes: tuple[str, ...] = ("googleusercontent.com",)
    allowed_mime_types: tuple[str, ...] = (
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    )
    max_image_bytes: int = 10 * 1024 * 1024
    max_total_bytes: int = 50 * 1024 * 1024
    max_images: int = 100
    max_redirects: int = 3

    def __post_init__(self) -> None:
        _validate_policy(self)


@dataclass(frozen=True, slots=True)
class GoogleImageContext:
    """Provider coordinates needed to bind an image during later mapping."""

    item_position: int
    item_id: str | None
    question_id: str | None
    question_ids: tuple[str, ...]
    option_index: int | None


@dataclass(frozen=True, slots=True)
class GoogleFormImageCapture:
    """One validated image held only in memory; no temporary URL is retained."""

    json_path: tuple[JsonPathPart, ...]
    context: GoogleImageContext
    content: bytes
    mime_type: str
    sha256: str


@dataclass(frozen=True, slots=True)
class GoogleFormImageFailure:
    """One image acquisition failure safe for later mapping and persistence."""

    json_path: tuple[JsonPathPart, ...]
    context: GoogleImageContext
    code: GoogleFormsErrorCode
    stage: GoogleFormsStage
    retryable: bool
    status_code: int | None


@dataclass(frozen=True, slots=True)
class GoogleFormCapture:
    """A Forms definition plus captured images and path-complete failures."""

    form_id: str
    raw_form: dict[str, Any]
    images: tuple[GoogleFormImageCapture, ...]
    image_failures: tuple[GoogleFormImageFailure, ...] = ()


@dataclass(frozen=True, slots=True)
class _DiscoveredImage:
    content_uri: str
    json_path: tuple[JsonPathPart, ...]
    context: GoogleImageContext


_FORM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_DNS_NAME_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
    r"[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
    r"[A-Za-z0-9])?\.?$"
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429})
_FORBIDDEN_AUTH_HEADERS = frozenset({
    "connection",
    "content-length",
    "cookie",
    "host",
    "proxy-authorization",
    "transfer-encoding",
})


def _raise_configuration_error(message: str) -> None:
    raise GoogleFormsConnectorError(
        GoogleFormsErrorCode.INVALID_CONFIGURATION,
        message,
        stage=GoogleFormsStage.CONFIGURATION,
    )


def _normalize_host_rule(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        _raise_configuration_error(f"{label} contains a non-string host rule")
    normalized = value.strip().lower().rstrip(".")
    if (
        not normalized
        or not _DNS_NAME_RE.fullmatch(normalized)
    ):
        _raise_configuration_error(f"{label} contains an invalid host rule")
    try:
        parsed = httpx.URL(f"https://{normalized}")
    except (TypeError, ValueError):
        _raise_configuration_error(f"{label} contains an invalid host rule")
    if parsed.host != normalized or parsed.raw_path != b"/":
        _raise_configuration_error(f"{label} contains an invalid host rule")
    return normalized


def _validate_policy(policy: GoogleImageDownloadPolicy) -> None:
    if not policy.allowed_hosts and not policy.allowed_host_suffixes:
        _raise_configuration_error("image host allowlist cannot be empty")
    for host in policy.allowed_hosts:
        _normalize_host_rule(host, label="allowed_hosts")
    for suffix in policy.allowed_host_suffixes:
        _normalize_host_rule(suffix, label="allowed_host_suffixes")
    if not policy.allowed_mime_types:
        _raise_configuration_error("image MIME allowlist cannot be empty")
    if any(
        not isinstance(mime, str)
        or not mime.startswith("image/")
        or mime != mime.strip().lower()
        for mime in policy.allowed_mime_types
    ):
        _raise_configuration_error("image MIME allowlist is invalid")
    if (
        not isinstance(policy.max_image_bytes, int)
        or isinstance(policy.max_image_bytes, bool)
        or policy.max_image_bytes <= 0
    ):
        _raise_configuration_error("max_image_bytes must be positive")
    if (
        not isinstance(policy.max_total_bytes, int)
        or isinstance(policy.max_total_bytes, bool)
        or policy.max_total_bytes <= 0
    ):
        _raise_configuration_error("max_total_bytes must be positive")
    if policy.max_total_bytes < policy.max_image_bytes:
        _raise_configuration_error(
            "max_total_bytes cannot be smaller than max_image_bytes"
        )
    if (
        not isinstance(policy.max_images, int)
        or isinstance(policy.max_images, bool)
        or policy.max_images <= 0
    ):
        _raise_configuration_error("max_images must be positive")
    if (
        not isinstance(policy.max_redirects, int)
        or isinstance(policy.max_redirects, bool)
        or policy.max_redirects < 0
    ):
        _raise_configuration_error("max_redirects cannot be negative")


def _validate_forms_api_base(value: str) -> httpx.URL:
    try:
        base = httpx.URL(value)
    except (TypeError, ValueError):
        raise GoogleFormsConnectorError(
            GoogleFormsErrorCode.INVALID_CONFIGURATION,
            "Forms API base URL is invalid",
            stage=GoogleFormsStage.CONFIGURATION,
        ) from None
    if (
        base.scheme != "https"
        or not base.host
        or base.userinfo
        or base.query
        or base.fragment
        or base.port is not None
    ):
        _raise_configuration_error(
            "Forms API base URL must be an HTTPS origin/path without credentials"
        )
    return base


def _forms_url(base: httpx.URL, form_id: str) -> httpx.URL:
    if not _FORM_ID_RE.fullmatch(form_id):
        raise GoogleFormsConnectorError(
            GoogleFormsErrorCode.INVALID_FORM_ID,
            "Google form ID has an invalid format",
            stage=GoogleFormsStage.FORMS_GET,
        )
    path = f"{base.path.rstrip('/')}/forms/{form_id}"
    return base.copy_with(path=path)


def _image_safe_context(
    image: _DiscoveredImage,
    *,
    host: str | None = None,
) -> dict[str, str | int]:
    context: dict[str, str | int] = {
        "json_path": _format_json_path(image.json_path),
        "item_position": image.context.item_position,
    }
    if image.context.item_id:
        context["item_id"] = image.context.item_id
    if host:
        context["host"] = host
    return context


def _format_json_path(path: tuple[JsonPathPart, ...]) -> str:
    rendered = "$"
    for part in path:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered += f".{part}"
    return rendered


def _question_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    found: list[str] = []
    for question in value:
        if not isinstance(question, dict):
            continue
        question_id = question.get("questionId")
        if isinstance(question_id, str) and question_id:
            found.append(question_id)
    return tuple(found)


def _option_index(path: tuple[JsonPathPart, ...]) -> int | None:
    for index, part in enumerate(path[:-1]):
        if part == "options" and isinstance(path[index + 1], int):
            return path[index + 1]
    return None


def _discover_images(raw_form: dict[str, Any]) -> tuple[_DiscoveredImage, ...]:
    items = raw_form.get("items", [])
    if not isinstance(items, list):
        raise GoogleFormsConnectorError(
            GoogleFormsErrorCode.FORMS_INVALID_RESPONSE,
            "Forms response items field is not an array",
            stage=GoogleFormsStage.IMAGE_DISCOVERY,
        )

    discovered: list[_DiscoveredImage] = []

    def visit(
        value: Any,
        path: tuple[JsonPathPart, ...],
        *,
        item_position: int,
        item_id: str | None,
        inherited_question_id: str | None,
        shared_question_ids: tuple[str, ...],
    ) -> None:
        if isinstance(value, dict):
            current_question_id = inherited_question_id
            own_question_id = value.get("questionId")
            if isinstance(own_question_id, str) and own_question_id:
                current_question_id = own_question_id
            nested_question = value.get("question")
            if isinstance(nested_question, dict):
                nested_question_id = nested_question.get("questionId")
                if isinstance(nested_question_id, str) and nested_question_id:
                    current_question_id = nested_question_id

            if "contentUri" in value:
                content_uri = value["contentUri"]
                context = GoogleImageContext(
                    item_position=item_position,
                    item_id=item_id,
                    question_id=current_question_id,
                    question_ids=(
                        (current_question_id,)
                        if current_question_id
                        else shared_question_ids
                    ),
                    option_index=_option_index(path),
                )
                if not isinstance(content_uri, str) or not content_uri.strip():
                    placeholder = _DiscoveredImage("", path, context)
                    raise GoogleFormsConnectorError(
                        GoogleFormsErrorCode.FORMS_INVALID_RESPONSE,
                        "Image contentUri is not a non-empty string",
                        stage=GoogleFormsStage.IMAGE_DISCOVERY,
                        safe_context=_image_safe_context(placeholder),
                    )
                discovered.append(_DiscoveredImage(
                    content_uri=content_uri,
                    json_path=path,
                    context=context,
                ))

            nested_shared_ids = shared_question_ids
            group = value.get("questionGroupItem")
            if isinstance(group, dict):
                nested_shared_ids = _question_ids(group.get("questions"))
            for key, child in value.items():
                if key == "contentUri":
                    continue
                visit(
                    child,
                    (*path, key),
                    item_position=item_position,
                    item_id=item_id,
                    inherited_question_id=current_question_id,
                    shared_question_ids=nested_shared_ids,
                )
            return

        if isinstance(value, list):
            for index, child in enumerate(value):
                visit(
                    child,
                    (*path, index),
                    item_position=item_position,
                    item_id=item_id,
                    inherited_question_id=inherited_question_id,
                    shared_question_ids=shared_question_ids,
                )

    for position, item in enumerate(items):
        if not isinstance(item, dict):
            raise GoogleFormsConnectorError(
                GoogleFormsErrorCode.FORMS_INVALID_RESPONSE,
                "Forms response contains a non-object item",
                stage=GoogleFormsStage.IMAGE_DISCOVERY,
                safe_context={"item_position": position},
            )
        item_id = item.get("itemId")
        safe_item_id = item_id if isinstance(item_id, str) and item_id else None
        visit(
            item,
            ("items", position),
            item_position=position,
            item_id=safe_item_id,
            inherited_question_id=None,
            shared_question_ids=(),
        )
    return tuple(discovered)


def _strip_content_uris(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_content_uris(child)
            for key, child in value.items()
            if key != "contentUri"
        }
    if isinstance(value, list):
        return [_strip_content_uris(child) for child in value]
    return value


def _host_is_allowed(host: str, policy: GoogleImageDownloadPolicy) -> bool:
    normalized = host.lower().rstrip(".")
    exact_hosts = {
        _normalize_host_rule(item, label="allowed_hosts")
        for item in policy.allowed_hosts
    }
    if normalized in exact_hosts:
        return True
    for raw_suffix in policy.allowed_host_suffixes:
        suffix = _normalize_host_rule(
            raw_suffix,
            label="allowed_host_suffixes",
        )
        if normalized == suffix or normalized.endswith(f".{suffix}"):
            return True
    return False


def _validate_image_url(
    raw_url: str,
    image: _DiscoveredImage,
    policy: GoogleImageDownloadPolicy,
) -> httpx.URL:
    try:
        url = httpx.URL(raw_url)
    except (TypeError, ValueError):
        raise GoogleFormsConnectorError(
            GoogleFormsErrorCode.IMAGE_URL_REJECTED,
            "Temporary image URL is invalid",
            stage=GoogleFormsStage.IMAGE_DOWNLOAD,
            safe_context=_image_safe_context(image),
        ) from None
    host = url.host or ""
    if (
        url.scheme != "https"
        or not host
        or url.userinfo
        or url.fragment
        or url.port is not None
        or not _host_is_allowed(host, policy)
    ):
        raise GoogleFormsConnectorError(
            GoogleFormsErrorCode.IMAGE_URL_REJECTED,
            "Temporary image URL is outside the Google image allowlist",
            stage=GoogleFormsStage.IMAGE_DOWNLOAD,
            safe_context=_image_safe_context(image, host=host or None),
        )
    return url


def _content_length(
    response: httpx.Response,
    image: _DiscoveredImage,
) -> int | None:
    raw_length = response.headers.get("content-length")
    if raw_length is None:
        return None
    try:
        length = int(raw_length)
    except ValueError:
        raise GoogleFormsConnectorError(
            GoogleFormsErrorCode.IMAGE_INVALID_RESPONSE,
            "Image response has an invalid Content-Length header",
            stage=GoogleFormsStage.IMAGE_DOWNLOAD,
            status_code=response.status_code,
            safe_context=_image_safe_context(image, host=response.request.url.host),
        ) from None
    if length < 0:
        raise GoogleFormsConnectorError(
            GoogleFormsErrorCode.IMAGE_INVALID_RESPONSE,
            "Image response has an invalid Content-Length header",
            stage=GoogleFormsStage.IMAGE_DOWNLOAD,
            status_code=response.status_code,
            safe_context=_image_safe_context(image, host=response.request.url.host),
        )
    return length


def _detected_mime_type(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _forms_http_error(status_code: int) -> GoogleFormsConnectorError:
    if status_code == 401:
        code = GoogleFormsErrorCode.AUTHENTICATION_REQUIRED
        message = "Google Forms authorization is missing or expired"
    elif status_code == 403:
        code = GoogleFormsErrorCode.PERMISSION_DENIED
        message = "Google Forms access was denied"
    elif status_code == 404:
        code = GoogleFormsErrorCode.FORM_NOT_FOUND
        message = "Google form was not found"
    elif status_code == 429:
        code = GoogleFormsErrorCode.RATE_LIMITED
        message = "Google Forms rate limit was reached"
    elif status_code >= 500:
        code = GoogleFormsErrorCode.PROVIDER_UNAVAILABLE
        message = "Google Forms is temporarily unavailable"
    else:
        code = GoogleFormsErrorCode.FORMS_HTTP_ERROR
        message = "Google Forms returned an unexpected HTTP status"
    return GoogleFormsConnectorError(
        code,
        message,
        stage=GoogleFormsStage.FORMS_GET,
        retryable=(status_code in _RETRYABLE_STATUSES or status_code >= 500),
        status_code=status_code,
    )


def _image_failure(
    image: _DiscoveredImage,
    error: GoogleFormsConnectorError,
) -> GoogleFormImageFailure:
    if error.stage != GoogleFormsStage.IMAGE_DOWNLOAD:
        raise error
    return GoogleFormImageFailure(
        json_path=image.json_path,
        context=image.context,
        code=error.code,
        stage=error.stage,
        retryable=error.retryable,
        status_code=error.status_code,
    )


def _total_size_failure(image: _DiscoveredImage) -> GoogleFormImageFailure:
    return GoogleFormImageFailure(
        json_path=image.json_path,
        context=image.context,
        code=GoogleFormsErrorCode.IMAGE_TOO_LARGE,
        stage=GoogleFormsStage.IMAGE_DOWNLOAD,
        retryable=False,
        status_code=None,
    )


class GoogleFormsClient:
    """Minimal read-only client for one authorized ``forms.get`` call."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        authorization: AuthorizationProvider,
        *,
        forms_api_base: str,
        image_policy: GoogleImageDownloadPolicy | None = None,
    ) -> None:
        self._http_client = http_client
        self._authorization = authorization
        self._forms_api_base = _validate_forms_api_base(forms_api_base)
        self._image_policy = image_policy or GoogleImageDownloadPolicy()
        _validate_policy(self._image_policy)

    def _get_request(
        self,
        url: httpx.URL,
        *,
        headers: Mapping[str, str],
    ) -> httpx.Request:
        """Build a timed request without inheriting client credentials/cookies."""

        template = self._http_client.build_request("GET", url)
        return httpx.Request(
            "GET",
            url,
            headers=headers,
            extensions=dict(template.extensions),
        )

    async def fetch_form(self, form_id: str) -> GoogleFormCapture:
        """Fetch one form and capture each independently usable image."""

        request_url = _forms_url(self._forms_api_base, form_id)
        headers = await self._authorization_headers()
        request = self._get_request(
            request_url,
            headers={**headers, "Accept": "application/json"},
        )
        try:
            response = await self._http_client.send(
                request,
                auth=None,
                follow_redirects=False,
            )
        except httpx.TransportError:
            raise GoogleFormsConnectorError(
                GoogleFormsErrorCode.TRANSPORT_ERROR,
                "Google Forms request failed before receiving a response",
                stage=GoogleFormsStage.FORMS_GET,
                retryable=True,
            ) from None

        try:
            if response.status_code != 200:
                raise _forms_http_error(response.status_code)
            try:
                payload = response.json()
            except (UnicodeDecodeError, ValueError):
                raise GoogleFormsConnectorError(
                    GoogleFormsErrorCode.FORMS_INVALID_JSON,
                    "Google Forms returned invalid JSON",
                    stage=GoogleFormsStage.FORMS_GET,
                    status_code=response.status_code,
                ) from None
        finally:
            await response.aclose()

        if not isinstance(payload, dict):
            raise GoogleFormsConnectorError(
                GoogleFormsErrorCode.FORMS_INVALID_RESPONSE,
                "Google Forms response root is not an object",
                stage=GoogleFormsStage.FORMS_GET,
            )
        response_form_id = payload.get("formId")
        if response_form_id != form_id:
            raise GoogleFormsConnectorError(
                GoogleFormsErrorCode.FORMS_INVALID_RESPONSE,
                "Google Forms response identity does not match the request",
                stage=GoogleFormsStage.FORMS_GET,
            )

        discovered = _discover_images(payload)
        if len(discovered) > self._image_policy.max_images:
            raise GoogleFormsConnectorError(
                GoogleFormsErrorCode.TOO_MANY_IMAGES,
                "Google form contains more images than the configured limit",
                stage=GoogleFormsStage.IMAGE_DISCOVERY,
                safe_context={"image_count": len(discovered)},
            )

        sanitized_payload = _strip_content_uris(payload)
        captures: list[GoogleFormImageCapture] = []
        failures: list[GoogleFormImageFailure] = []
        total_bytes = 0
        for index, image in enumerate(discovered):
            remaining_total_bytes = self._image_policy.max_total_bytes - total_bytes
            if remaining_total_bytes <= 0:
                failures.extend(
                    _total_size_failure(unresolved)
                    for unresolved in discovered[index:]
                )
                break
            try:
                capture = await self._download_image(
                    image,
                    remaining_total_bytes=remaining_total_bytes,
                )
            except GoogleFormsConnectorError as error:
                failures.append(_image_failure(image, error))
                received = error.safe_context.get("bytes_received", 0)
                if (
                    not isinstance(received, int)
                    or isinstance(received, bool)
                    or received < 0
                ):
                    raise GoogleFormsConnectorError(
                        GoogleFormsErrorCode.IMAGE_INVALID_RESPONSE,
                        "Image download accounting is invalid",
                        stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                    ) from None
                total_bytes += received
                if (
                    error.safe_context.get("total_limit_reached") == 1
                    or total_bytes >= self._image_policy.max_total_bytes
                ):
                    failures.extend(
                        _total_size_failure(unresolved)
                        for unresolved in discovered[index + 1:]
                    )
                    break
                continue

            total_bytes += len(capture.content)
            captures.append(capture)

        return GoogleFormCapture(
            form_id=form_id,
            raw_form=sanitized_payload,
            images=tuple(captures),
            image_failures=tuple(failures),
        )

    async def _authorization_headers(self) -> dict[str, str]:
        try:
            provided = self._authorization()
            if inspect.isawaitable(provided):
                provided = await provided
        except Exception:
            raise GoogleFormsConnectorError(
                GoogleFormsErrorCode.AUTHORIZATION_FAILED,
                "Google Forms authorization could not be prepared",
                stage=GoogleFormsStage.AUTHORIZATION,
            ) from None
        if not isinstance(provided, Mapping):
            raise GoogleFormsConnectorError(
                GoogleFormsErrorCode.AUTHORIZATION_FAILED,
                "Google Forms authorization provider returned invalid headers",
                stage=GoogleFormsStage.AUTHORIZATION,
            )

        headers: dict[str, str] = {}
        for name, value in provided.items():
            normalized_name = name.lower() if isinstance(name, str) else ""
            if (
                not isinstance(name, str)
                or not isinstance(value, str)
                or not _HEADER_NAME_RE.fullmatch(name)
                or not value.strip()
                or any(
                    ord(character) < 32 or ord(character) > 126
                    for character in value
                )
                or normalized_name in _FORBIDDEN_AUTH_HEADERS
            ):
                raise GoogleFormsConnectorError(
                    GoogleFormsErrorCode.AUTHORIZATION_FAILED,
                    "Google Forms authorization provider returned invalid headers",
                    stage=GoogleFormsStage.AUTHORIZATION,
                )
            headers[normalized_name] = value
        if not headers:
            raise GoogleFormsConnectorError(
                GoogleFormsErrorCode.AUTHORIZATION_FAILED,
                "Google Forms authorization provider returned no headers",
                stage=GoogleFormsStage.AUTHORIZATION,
            )
        return headers

    async def _download_image(
        self,
        image: _DiscoveredImage,
        *,
        remaining_total_bytes: int,
    ) -> GoogleFormImageCapture:
        current_url = _validate_image_url(
            image.content_uri,
            image,
            self._image_policy,
        )
        redirects = 0

        while True:
            request = self._get_request(
                current_url,
                headers={
                    "Accept": ", ".join(
                        self._image_policy.allowed_mime_types
                    ),
                    "Accept-Encoding": "identity",
                },
            )
            try:
                response = await self._http_client.send(
                    request,
                    stream=True,
                    auth=None,
                    follow_redirects=False,
                )
            except httpx.RequestError:
                raise GoogleFormsConnectorError(
                    GoogleFormsErrorCode.TRANSPORT_ERROR,
                    "Temporary image download failed before receiving a response",
                    stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                    retryable=True,
                    safe_context=_image_safe_context(
                        image,
                        host=current_url.host,
                    ),
                ) from None

            try:
                if response.status_code in _REDIRECT_STATUSES:
                    if redirects >= self._image_policy.max_redirects:
                        raise GoogleFormsConnectorError(
                            GoogleFormsErrorCode.IMAGE_REDIRECT_LIMIT,
                            "Temporary image exceeded the redirect limit",
                            stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                            status_code=response.status_code,
                            safe_context=_image_safe_context(
                                image,
                                host=current_url.host,
                            ),
                        )
                    location = response.headers.get("location")
                    if not location:
                        raise GoogleFormsConnectorError(
                            GoogleFormsErrorCode.IMAGE_INVALID_RESPONSE,
                            "Temporary image redirect omitted its destination",
                            stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                            status_code=response.status_code,
                            safe_context=_image_safe_context(
                                image,
                                host=current_url.host,
                            ),
                        )
                    try:
                        redirected_url = current_url.join(location)
                    except (TypeError, ValueError):
                        raise GoogleFormsConnectorError(
                            GoogleFormsErrorCode.IMAGE_URL_REJECTED,
                            "Temporary image redirect destination is invalid",
                            stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                            safe_context=_image_safe_context(
                                image,
                                host=current_url.host,
                            ),
                        ) from None
                    current_url = _validate_image_url(
                        str(redirected_url),
                        image,
                        self._image_policy,
                    )
                    redirects += 1
                    continue

                if response.status_code != 200:
                    raise GoogleFormsConnectorError(
                        GoogleFormsErrorCode.IMAGE_HTTP_ERROR,
                        "Temporary image returned an unexpected HTTP status",
                        stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                        retryable=(
                            response.status_code in _RETRYABLE_STATUSES
                            or response.status_code >= 500
                        ),
                        status_code=response.status_code,
                        safe_context=_image_safe_context(
                            image,
                            host=current_url.host,
                        ),
                    )

                mime_type = response.headers.get("content-type", "").split(
                    ";", 1
                )[0].strip().lower()
                if mime_type not in self._image_policy.allowed_mime_types:
                    raise GoogleFormsConnectorError(
                        GoogleFormsErrorCode.IMAGE_CONTENT_TYPE_REJECTED,
                        "Temporary image has a disallowed Content-Type",
                        stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                        status_code=response.status_code,
                        safe_context=_image_safe_context(
                            image,
                            host=current_url.host,
                        ),
                    )

                content_encoding = response.headers.get(
                    "content-encoding",
                    "",
                ).strip().lower()
                if content_encoding not in {"", "identity"}:
                    raise GoogleFormsConnectorError(
                        GoogleFormsErrorCode.IMAGE_INVALID_RESPONSE,
                        "Temporary image used an unsupported Content-Encoding",
                        stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                        status_code=response.status_code,
                        safe_context=_image_safe_context(
                            image,
                            host=current_url.host,
                        ),
                    )

                declared_length = _content_length(response, image)
                if (
                    declared_length is not None
                    and declared_length > self._image_policy.max_image_bytes
                ):
                    raise GoogleFormsConnectorError(
                        GoogleFormsErrorCode.IMAGE_TOO_LARGE,
                        "Temporary image exceeds the configured size limit",
                        stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                        status_code=response.status_code,
                        safe_context=_image_safe_context(
                            image,
                            host=current_url.host,
                        ),
                    )
                if (
                    declared_length is not None
                    and declared_length > remaining_total_bytes
                ):
                    raise GoogleFormsConnectorError(
                        GoogleFormsErrorCode.IMAGE_TOO_LARGE,
                        "Temporary image exceeds the remaining total size limit",
                        stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                        status_code=response.status_code,
                        safe_context={
                            **_image_safe_context(
                                image,
                                host=current_url.host,
                            ),
                            "total_limit_reached": 1,
                        },
                    )

                content = bytearray()
                try:
                    if hasattr(response, "_content"):
                        stream = response.aiter_bytes()
                    else:
                        stream = response.aiter_raw()
                    async for chunk in stream:
                        next_size = len(content) + len(chunk)
                        if next_size > remaining_total_bytes:
                            raise GoogleFormsConnectorError(
                                GoogleFormsErrorCode.IMAGE_TOO_LARGE,
                                "Temporary image exceeds the remaining total size limit",
                                stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                                status_code=response.status_code,
                                safe_context={
                                    **_image_safe_context(
                                        image,
                                        host=current_url.host,
                                    ),
                                    "bytes_received": next_size,
                                    "total_limit_reached": 1,
                                },
                            )
                        if next_size > self._image_policy.max_image_bytes:
                            raise GoogleFormsConnectorError(
                                GoogleFormsErrorCode.IMAGE_TOO_LARGE,
                                "Temporary image exceeds the configured size limit",
                                stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                                status_code=response.status_code,
                                safe_context=_image_safe_context(
                                    image,
                                    host=current_url.host,
                                ) | {"bytes_received": next_size},
                            )
                        content.extend(chunk)
                except httpx.RequestError:
                    raise GoogleFormsConnectorError(
                        GoogleFormsErrorCode.TRANSPORT_ERROR,
                        "Temporary image download was interrupted",
                        stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                        retryable=True,
                        safe_context=_image_safe_context(
                            image,
                            host=current_url.host,
                        ) | {"bytes_received": len(content)},
                    ) from None

                immutable_content = bytes(content)
                if _detected_mime_type(immutable_content) != mime_type:
                    raise GoogleFormsConnectorError(
                        GoogleFormsErrorCode.IMAGE_SIGNATURE_MISMATCH,
                        "Temporary image signature does not match its Content-Type",
                        stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                        status_code=response.status_code,
                        safe_context=_image_safe_context(
                            image,
                            host=current_url.host,
                        ) | {"bytes_received": len(immutable_content)},
                    )
                return GoogleFormImageCapture(
                    json_path=image.json_path,
                    context=image.context,
                    content=immutable_content,
                    mime_type=mime_type,
                    sha256=hashlib.sha256(immutable_content).hexdigest(),
                )
            finally:
                await response.aclose()
