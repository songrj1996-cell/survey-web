"""Compatibility accessors for survey column type and profile usage."""

from __future__ import annotations


PROFILE_SCOPES = frozenset({"analysis", "label"})
PROFILE_LABEL_ROLES = frozenset({
    "single_choice",
    "multi_choice",
    "scale",
    "matrix_scale",
    "matrix_single",
    "matrix_multi",
    "open_text",
})
PROFILE_GROUPING_ROLES = PROFILE_LABEL_ROLES - {"open_text"}


def question_type(col: dict) -> str:
    """Return the question type; legacy profile columns are single choice."""
    role = col.get("role")
    return "single_choice" if role == "profile_dim" else role


def is_profile_dim(col: dict) -> bool:
    """Return whether a column is attached to quoted respondent evidence."""
    if col.get("role") == "profile_dim":
        return True
    return bool(col.get("use_as_profile")) and question_type(col) in PROFILE_LABEL_ROLES


def profile_scope(col: dict) -> str:
    """Return the effective profile scope, normalizing legacy and invalid values."""
    if col.get("role") == "profile_dim":
        return "analysis"
    scope = col.get("profile_scope")
    if scope not in PROFILE_SCOPES:
        scope = "analysis"
    if question_type(col) == "open_text":
        return "label"
    return scope


def is_profile_grouping(col: dict) -> bool:
    """Return whether a profile participates in overview, grouping and sampling."""
    return (
        is_profile_dim(col)
        and question_type(col) in PROFILE_GROUPING_ROLES
        and profile_scope(col) == "analysis"
    )


def normalize_profile_fields(col: dict) -> dict:
    """Normalize profile fields in place without rejecting stale/model output."""
    legacy = col.get("role") == "profile_dim"
    role = question_type(col) or "single_choice"
    enabled = is_profile_dim(col) and role in PROFILE_LABEL_ROLES
    scope = "analysis" if legacy else profile_scope(col)
    col["role"] = role
    col["use_as_profile"] = enabled
    if enabled:
        col["profile_scope"] = scope
    else:
        col.pop("profile_scope", None)
    return col
