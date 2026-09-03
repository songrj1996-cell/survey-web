"""Deterministic, privacy-bounded exports for approved Interview Report V2.

This module deliberately stops at a canonical Markdown package.  The service
layer is responsible for rendering the Markdown to DOCX exactly once and for
persisting those bytes so later downloads never regenerate a different file.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import re
import unicodedata
from typing import Any, Mapping

from app.core.interview_v2_report import (
    REPORT_SECTION_SPECS,
    InterviewV2ReportValidationError,
    payload_sha256,
    validate_report_approval,
)


EXPORT_SCHEMA_VERSION = "interview-report-export/1.0"
EXPORT_PROFILE_VERSION = "approved-docx-evidence-redacted/1.0"
EXPORT_FORMAT = "docx"
EXPORT_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

_REPORT_RE = re.compile(r"^report_[0-9a-f]{32}$")
_PROJECT_RE = re.compile(r"^project_[0-9a-f]{32}$")
_EVIDENCE_RE = re.compile(r"^(?:ev|evidence)_[0-9a-f]{32}$")
_PARTICIPANT_RE = re.compile(r"^participant_[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CELL_ADDRESS_RE = re.compile(
    r"^(?:[A-Z]{1,4}[1-9][0-9]*|R[1-9][0-9]*C[1-9][0-9]*)$",
    re.IGNORECASE,
)
_REPORTABLE_IDENTITY_STATUSES = {
    "confirmed",
    "human_confirmed",
    "system_verified",
    "user_confirmed",
}
_EVIDENCE_TYPE_LABELS = {
    "participant_self_report": "玩家自述",
    "researcher_observation": "研究员观察",
}
_PRIVATE_HEX_ID_PREFIXES = (
    "analysis",
    "audit",
    "binding",
    "boundary",
    "case",
    "cell",
    "claim",
    "coverage",
    "dossier",
    "ev",
    "evaluation",
    "evidence",
    "export",
    "fact",
    "finding",
    "group",
    "import",
    "issue",
    "job",
    "label_scope",
    "label",
    "mapping",
    "module",
    "occurrence",
    "occ",
    "override",
    "participant",
    "project",
    "question",
    "report",
    "review",
    "scope",
    "section",
    "sheet",
    "stat",
    "structure",
    "trace",
    "upload",
    "workbook",
)
_PRIVATE_HEX_ID_PREFIX_PATTERN = "|".join(
    re.escape(prefix)
    for prefix in sorted(_PRIVATE_HEX_ID_PREFIXES, key=len, reverse=True)
)
_PRIVATE_ID_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?:"
    rf"(?:{_PRIVATE_HEX_ID_PREFIX_PATTERN})_[0-9a-f]{{32}}"
    r"|sheet_[0-9]{3,}"
    r"|fact_candidate_[1-9][0-9]*"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)


class InterviewV2ExportValidationError(ValueError):
    """The requested artifact cannot be built without weakening export rules."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.context = dict(context or {})


def _fail(
    code: str,
    message: str,
    *,
    context: Mapping[str, Any] | None = None,
) -> None:
    raise InterviewV2ExportValidationError(code, message, context=context)


def _text(value: object) -> str:
    if value is None:
        return ""
    return unicodedata.normalize(
        "NFC",
        str(value).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n"),
    ).strip()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_export_visible_text(value: str) -> None:
    """Reject internal identifiers from any user-visible export text layer."""

    if not isinstance(value, str):
        _fail(
            "EXPORT_PRIVACY_BOUNDARY_VIOLATED",
            "exported visible text is invalid",
        )
    if _PRIVATE_ID_RE.search(value):
        _fail(
            "EXPORT_PRIVACY_BOUNDARY_VIOLATED",
            "exported prose contains an internal identifier",
        )


def _approved_timestamp(value: object) -> tuple[str, str]:
    raw = _text(value)
    if not raw:
        _fail("EXPORT_APPROVAL_METADATA_INVALID", "approved_at is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InterviewV2ExportValidationError(
            "EXPORT_APPROVAL_METADATA_INVALID",
            "approved_at must be an ISO-8601 timestamp",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(
            "EXPORT_APPROVAL_METADATA_INVALID",
            "approved_at must include a timezone",
        )
    utc_value = parsed.astimezone(timezone.utc)
    canonical = utc_value.isoformat(timespec="seconds").replace("+00:00", "Z")
    display = utc_value.strftime("%Y-%m-%d %H:%M:%S UTC")
    return canonical, display


def _export_profile() -> dict[str, Any]:
    return {
        "profile_version": EXPORT_PROFILE_VERSION,
        "format": EXPORT_FORMAT,
        "include_evidence_appendix": True,
        "visible_evidence_fields": [
            "participant_label",
            "evidence_type",
            "normalized_content",
            "sheet_name",
            "cell_address",
        ],
        "omitted_evidence_fields": [
            "internal_ids",
            "raw_content",
            "display_content",
            "recorder_label",
            "owner",
        ],
    }


def _validate_report_identity(report: dict[str, Any], *, is_current_version: bool) -> None:
    report_version_id = _text(report.get("report_version_id"))
    project_id = _text(report.get("project_id"))
    version_number = report.get("version_number")
    revision_digest = _text(report.get("revision_payload_sha256"))
    if not _REPORT_RE.fullmatch(report_version_id):
        _fail("EXPORT_REPORT_INVALID", "report_version_id is invalid")
    if not _PROJECT_RE.fullmatch(project_id):
        _fail("EXPORT_REPORT_INVALID", "project_id is invalid")
    if (
        isinstance(version_number, bool)
        or not isinstance(version_number, int)
        or version_number < 1
    ):
        _fail("EXPORT_REPORT_INVALID", "report version_number is invalid")
    if not _SHA256_RE.fullmatch(revision_digest):
        _fail(
            "EXPORT_REPORT_INVALID",
            "report revision_payload_sha256 is invalid",
        )
    if is_current_version is not True:
        _fail(
            "EXPORT_REPORT_NOT_CURRENT",
            "only the current approved report version can be exported",
        )
    if _text(report.get("status")) != "approved":
        _fail(
            "EXPORT_REPORT_NOT_APPROVED",
            "only an approved report version can be exported",
        )
    if _text(report.get("audit_status")) not in {"audited", "audit_passed"}:
        _fail(
            "EXPORT_REPORT_AUDIT_BLOCKED",
            "the approved report must retain its audited status",
        )
    if not _text(report.get("approved_by")):
        _fail(
            "EXPORT_APPROVAL_METADATA_INVALID",
            "approved_by is required but is never rendered into the export",
        )


def _validate_evidence_entry(
    evidence_id: str,
    raw: object,
    *,
    participant_ids: set[str],
    allowed_types: set[str],
) -> dict[str, str]:
    if not isinstance(raw, dict):
        _fail(
            "EXPORT_EVIDENCE_INVALID",
            "referenced evidence must be an object",
            context={"evidence_id": evidence_id},
        )
    if _text(raw.get("evidence_id")) != evidence_id:
        _fail(
            "EXPORT_EVIDENCE_INVALID",
            "evidence map key and evidence identity do not match",
            context={"evidence_id": evidence_id},
        )
    participant_id = _text(raw.get("participant_id"))
    if (
        not _PARTICIPANT_RE.fullmatch(participant_id)
        or participant_id not in participant_ids
    ):
        _fail(
            "EXPORT_EVIDENCE_OWNERSHIP_INVALID",
            "evidence participant does not match the approved claim",
            context={"evidence_id": evidence_id},
        )
    if raw.get("inclusion_status") != "included":
        _fail(
            "EXPORT_EVIDENCE_NOT_REPORTABLE",
            "excluded evidence cannot enter a formal export",
            context={"evidence_id": evidence_id},
        )
    if raw.get("identity_decision_status") not in _REPORTABLE_IDENTITY_STATUSES:
        _fail(
            "EXPORT_EVIDENCE_NOT_REPORTABLE",
            "unconfirmed evidence identity cannot enter a formal export",
            context={"evidence_id": evidence_id},
        )
    evidence_type = _text(raw.get("evidence_type"))
    if evidence_type not in _EVIDENCE_TYPE_LABELS or evidence_type not in allowed_types:
        _fail(
            "EXPORT_EVIDENCE_TYPE_INVALID",
            "evidence type is not allowed by the approved claim",
            context={"evidence_id": evidence_id},
        )

    participant_label = _text(
        raw.get("participant_label") or raw.get("display_name")
    )
    normalized_content = _text(raw.get("normalized_content"))
    sheet_name = _text(raw.get("sheet_name"))
    cell_address = _text(raw.get("cell_address"))
    if (
        not participant_label
        or participant_label == participant_id
        or len(participant_label) > 200
        or "\n" in participant_label
    ):
        _fail(
            "EXPORT_EVIDENCE_DISPLAY_INVALID",
            "evidence requires a non-internal participant display name",
            context={"evidence_id": evidence_id},
        )
    if not normalized_content or len(normalized_content) > 30000:
        _fail(
            "EXPORT_EVIDENCE_DISPLAY_INVALID",
            "evidence requires a bounded normalized excerpt",
            context={"evidence_id": evidence_id},
        )
    if not sheet_name or len(sheet_name) > 200 or "\n" in sheet_name:
        _fail(
            "EXPORT_EVIDENCE_DISPLAY_INVALID",
            "evidence requires a source sheet name",
            context={"evidence_id": evidence_id},
        )
    if not _CELL_ADDRESS_RE.fullmatch(cell_address):
        _fail(
            "EXPORT_EVIDENCE_DISPLAY_INVALID",
            "evidence requires a valid source cell address",
            context={"evidence_id": evidence_id},
        )
    return {
        "participant_label": participant_label,
        "evidence_type": evidence_type,
        "evidence_type_label": _EVIDENCE_TYPE_LABELS[evidence_type],
        "normalized_content": normalized_content,
        "sheet_name": sheet_name,
        "cell_address": cell_address.upper(),
    }


def _quoted_lines(text: str) -> list[str]:
    return [f"> {line}" if line else "> " for line in text.split("\n")]


def _report_body(sections: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    rendered: list[str] = []
    manifest: list[dict[str, Any]] = []
    for (expected_key, expected_title), section in zip(
        REPORT_SECTION_SPECS, sections, strict=True
    ):
        content = section["content"]
        rendered.append(f"## {expected_title}\n\n{content}")
        manifest.append({
            "section_key": expected_key,
            "section_revision": section["section_revision"],
            "content_sha256": section["content_sha256"],
        })
    return "\n\n".join(rendered), manifest


def _evidence_appendix(
    report: dict[str, Any],
    evidence_by_id: Mapping[str, dict[str, Any]],
) -> tuple[str, int, set[str]]:
    claim_by_id = {
        _text(item.get("claim_id")): item
        for item in report["claims"]
        if isinstance(item, dict)
    }
    lines = [
        "# 证据附录",
        "",
        "> 本附录仅展示批准报告实际引用的规范化证据。原始值、记录员、owner 与内部标识均不导出。",
    ]
    appendix_entry_count = 0
    used_evidence_ids: set[str] = set()
    evidence_group_number = 0
    for (expected_key, expected_title), section in zip(
        REPORT_SECTION_SPECS, report["sections"], strict=True
    ):
        del expected_key
        for claim_id in section["claim_ids"]:
            claim = claim_by_id[claim_id]
            evidence_ids = claim.get("evidence_ids") or []
            if not evidence_ids:
                continue
            if (
                not isinstance(evidence_ids, list)
                or len(evidence_ids) != len(set(evidence_ids))
                or any(not _EVIDENCE_RE.fullmatch(_text(item)) for item in evidence_ids)
            ):
                _fail(
                    "EXPORT_EVIDENCE_INVALID",
                    "approved claim evidence identifiers are invalid",
                )
            participant_ids = {
                _text(item) for item in claim.get("participant_ids") or []
            }
            allowed_types = {
                _text(item) for item in claim.get("evidence_type_allowlist") or []
            }
            evidence_group_number += 1
            lines.extend([
                "",
                f"## {expected_title}｜证据组 {evidence_group_number}",
                "",
                "**对应结论：**",
                *_quoted_lines(_text(claim.get("text"))),
            ])
            for evidence_number, evidence_id in enumerate(
                sorted(_text(item) for item in evidence_ids), start=1
            ):
                if evidence_id not in evidence_by_id:
                    _fail(
                        "EXPORT_EVIDENCE_MISSING",
                        "approved claim references unavailable evidence",
                        context={"evidence_id": evidence_id},
                    )
                display = _validate_evidence_entry(
                    evidence_id,
                    evidence_by_id[evidence_id],
                    participant_ids=participant_ids,
                    allowed_types=allowed_types,
                )
                lines.extend([
                    "",
                    f"**证据 {evidence_number}**",
                    "",
                    f"- 玩家：{display['participant_label']}",
                    f"- 证据类型：{display['evidence_type_label']}",
                    f"- 来源 Sheet：{display['sheet_name']}",
                    f"- 单元格：{display['cell_address']}",
                    "- 规范化摘录：",
                    *_quoted_lines(display["normalized_content"]),
                ])
                appendix_entry_count += 1
                used_evidence_ids.add(evidence_id)
    if appendix_entry_count < 1:
        _fail(
            "EXPORT_EVIDENCE_MISSING",
            "an approved formal report must include cited evidence",
        )
    return "\n".join(lines), appendix_entry_count, used_evidence_ids


def build_export_package(
    report_revision: dict[str, Any],
    evidence_by_id: Mapping[str, dict[str, Any]],
    *,
    is_current_version: bool,
) -> dict[str, Any]:
    """Build a canonical approved-report body and redacted evidence appendix.

    The caller must provide evidence from the report's frozen evidence revision,
    keyed by evidence ID.  No raw value, recorder identity, owner identifier or
    other source fields are copied into the returned Markdown.
    """

    if not isinstance(report_revision, dict):
        _fail("EXPORT_REPORT_INVALID", "report revision must be an object")
    if not isinstance(evidence_by_id, Mapping):
        _fail("EXPORT_EVIDENCE_INVALID", "evidence_by_id must be a mapping")
    report = deepcopy(report_revision)
    _validate_report_identity(report, is_current_version=is_current_version)
    approved_at, approved_at_display = _approved_timestamp(report.get("approved_at"))
    try:
        approval = validate_report_approval(report)
    except InterviewV2ReportValidationError as exc:
        raise InterviewV2ExportValidationError(
            "EXPORT_REPORT_AUDIT_BLOCKED",
            "approved report failed deterministic revalidation",
            context={"reason": str(exc)},
        ) from exc

    report_body, section_manifest = _report_body(report["sections"])
    appendix, appendix_entry_count, used_evidence_ids = _evidence_appendix(
        report, evidence_by_id
    )
    version_number = report["version_number"]
    header = "\n".join([
        "# 访谈研究报告",
        "",
        "> 正式交付稿 · 已批准",
        "",
        f"- 报告版本：V{version_number}",
        f"- 批准时间：{approved_at_display}",
    ])
    markdown = f"{header}\n\n{report_body}\n\n---\n\n{appendix}\n"
    validate_export_visible_text(markdown)

    report_body_sha256 = payload_sha256(section_manifest)
    appendix_sha256 = _text_sha256(appendix)
    document_markdown_sha256 = _text_sha256(markdown)
    manifest = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "format": EXPORT_FORMAT,
        "export_profile_version": EXPORT_PROFILE_VERSION,
        "report_version_id": report["report_version_id"],
        "report_version_number": version_number,
        "report_revision_payload_sha256": report["revision_payload_sha256"],
        "approval_status": "approved",
        "approved_at": approved_at,
        "report_body_sha256": report_body_sha256,
        "section_manifest": section_manifest,
        "appendix_sha256": appendix_sha256,
        "document_markdown_sha256": document_markdown_sha256,
        "section_count": approval["section_count"],
        "claim_count": approval["claim_count"],
        "evidence_count": len(used_evidence_ids),
        "appendix_entry_count": appendix_entry_count,
    }
    return {
        "markdown": markdown,
        "filename": f"访谈研究报告_V{version_number}_已批准.docx",
        "export_profile": _export_profile(),
        "manifest": manifest,
        "manifest_sha256": payload_sha256(manifest),
        "report_revision_payload_sha256": report["revision_payload_sha256"],
    }


__all__ = [
    "EXPORT_FORMAT",
    "EXPORT_MEDIA_TYPE",
    "EXPORT_PROFILE_VERSION",
    "EXPORT_SCHEMA_VERSION",
    "InterviewV2ExportValidationError",
    "build_export_package",
    "validate_export_visible_text",
]
