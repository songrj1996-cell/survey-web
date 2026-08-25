"""Strict parsing for Google Forms editor links.

Only links whose path exposes the Forms API ``formId`` are accepted.  The
parser never resolves redirects and never includes the submitted link in an
exception message, so callers can safely map stable error codes to UI hints.
"""

from __future__ import annotations

import re
from enum import Enum
from urllib.parse import parse_qsl, urlsplit


__all__ = [
    "GoogleFormsLinkError",
    "GoogleFormsLinkErrorCode",
    "parse_google_forms_edit_link",
]


class GoogleFormsLinkErrorCode(str, Enum):
    """Stable reasons why a submitted Google Forms link was rejected."""

    INVALID_URL = "invalid_url"
    HTTPS_REQUIRED = "https_required"
    UNSUPPORTED_HOST = "unsupported_host"
    SHORT_LINK_UNSUPPORTED = "short_link_unsupported"
    PUBLISHED_LINK_UNSUPPORTED = "published_link_unsupported"
    EDIT_LINK_REQUIRED = "edit_link_required"
    INVALID_FORM_ID = "invalid_form_id"
    UNSAFE_URL_COMPONENT = "unsafe_url_component"


_SAFE_ERROR_MESSAGES = {
    GoogleFormsLinkErrorCode.INVALID_URL: "Google Forms link is invalid",
    GoogleFormsLinkErrorCode.HTTPS_REQUIRED: (
        "Google Forms link must use HTTPS"
    ),
    GoogleFormsLinkErrorCode.UNSUPPORTED_HOST: (
        "Google Forms link host is not supported"
    ),
    GoogleFormsLinkErrorCode.SHORT_LINK_UNSUPPORTED: (
        "Google Forms short links are not supported"
    ),
    GoogleFormsLinkErrorCode.PUBLISHED_LINK_UNSUPPORTED: (
        "Google Forms published links do not expose an API form ID"
    ),
    GoogleFormsLinkErrorCode.EDIT_LINK_REQUIRED: (
        "A Google Forms editor link is required"
    ),
    GoogleFormsLinkErrorCode.INVALID_FORM_ID: (
        "Google Forms link contains an invalid form ID"
    ),
    GoogleFormsLinkErrorCode.UNSAFE_URL_COMPONENT: (
        "Google Forms link contains unsupported URL components"
    ),
}


class GoogleFormsLinkError(ValueError):
    """A safe parsing failure that never retains or echoes the source link."""

    def __init__(self, code: GoogleFormsLinkErrorCode) -> None:
        super().__init__(_SAFE_ERROR_MESSAGES[code])
        self.code = code


_MAX_LINK_LENGTH = 2048
_FORM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_EDITOR_PATH_RE = re.compile(r"^/forms/d/([^/]*)/edit/?$")
_PUBLISHED_PATH_RE = re.compile(r"^/forms/d/e(?:/|$)")
_ALLOWED_QUERY_VALUES = {
    "usp": frozenset({"drive_link", "sf_link", "sharing"}),
}


def _raise(code: GoogleFormsLinkErrorCode) -> None:
    raise GoogleFormsLinkError(code)


def _validate_query(raw_link: str, query: str) -> None:
    if "?" not in raw_link:
        return
    if not query:
        _raise(GoogleFormsLinkErrorCode.UNSAFE_URL_COMPONENT)
    try:
        pairs = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=4,
            separator="&",
        )
    except (ValueError, TypeError):
        _raise(GoogleFormsLinkErrorCode.UNSAFE_URL_COMPONENT)

    seen: set[str] = set()
    for key, value in pairs:
        allowed_values = _ALLOWED_QUERY_VALUES.get(key)
        if key in seen or allowed_values is None or value not in allowed_values:
            _raise(GoogleFormsLinkErrorCode.UNSAFE_URL_COMPONENT)
        seen.add(key)


def parse_google_forms_edit_link(value: str) -> str:
    """Return the API ``formId`` from a strict Google Forms editor link.

    Accepted links use ``https://docs.google.com/forms/d/{FORM_ID}/edit``.
    A trailing slash and Google's non-sensitive ``usp`` share suffixes are
    tolerated.  Short/published links are rejected because their identifiers
    cannot be passed reliably to ``forms.get``.
    """

    if not isinstance(value, str):
        _raise(GoogleFormsLinkErrorCode.INVALID_URL)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _raise(GoogleFormsLinkErrorCode.INVALID_URL)

    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_LINK_LENGTH:
        _raise(GoogleFormsLinkErrorCode.INVALID_URL)

    try:
        parts = urlsplit(normalized)
        hostname = parts.hostname
        port = parts.port
    except (TypeError, ValueError):
        _raise(GoogleFormsLinkErrorCode.INVALID_URL)

    if parts.scheme.casefold() != "https":
        _raise(GoogleFormsLinkErrorCode.HTTPS_REQUIRED)
    if parts.username is not None or parts.password is not None or port is not None:
        _raise(GoogleFormsLinkErrorCode.UNSAFE_URL_COMPONENT)

    normalized_host = hostname.casefold() if hostname else ""
    if normalized_host == "forms.gle":
        _raise(GoogleFormsLinkErrorCode.SHORT_LINK_UNSUPPORTED)
    if normalized_host != "docs.google.com":
        _raise(GoogleFormsLinkErrorCode.UNSUPPORTED_HOST)
    if "#" in normalized:
        _raise(GoogleFormsLinkErrorCode.UNSAFE_URL_COMPONENT)

    if _PUBLISHED_PATH_RE.match(parts.path):
        _raise(GoogleFormsLinkErrorCode.PUBLISHED_LINK_UNSUPPORTED)

    path_match = _EDITOR_PATH_RE.fullmatch(parts.path)
    if path_match is None:
        _raise(GoogleFormsLinkErrorCode.EDIT_LINK_REQUIRED)
    form_id = path_match.group(1)
    if _FORM_ID_RE.fullmatch(form_id) is None:
        _raise(GoogleFormsLinkErrorCode.INVALID_FORM_ID)

    _validate_query(normalized, parts.query)
    return form_id
