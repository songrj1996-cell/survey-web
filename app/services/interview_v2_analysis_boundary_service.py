"""Interview V2 analysis-boundary and evaluation-object orchestration."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from app.core.interview_v2_analysis_boundary import (
    InterviewV2AnalysisBoundaryError,
    build_analysis_boundary_proposal,
    build_coverage_preview,
    canonical_json_sha256,
    confirm_analysis_boundary as confirm_analysis_boundary_payload,
    validate_analysis_boundary,
)
from app.core.security import _owner_from_login
from app.services.interview_v2_import_service import InterviewV2ImportError
from app.services.interview_v2_mapping_service import (
    get_interview_import_with_mapping_status,
)
from app.storage import interview_v2_store as store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{canonical_json_sha256(value)[:32]}"


def _persistence_error(
    message: str = "分析边界保存失败，请稍后重试。",
) -> InterviewV2ImportError:
    return InterviewV2ImportError(
        status_code=500,
        code="ANALYSIS_BOUNDARY_PERSISTENCE_FAILED",
        message=message,
        retryable=True,
        suggested_action="retry_analysis_boundary_request",
    )


def _not_ready(status: str) -> InterviewV2ImportError:
    return InterviewV2ImportError(
        status_code=409,
        code="ANALYSIS_BOUNDARY_INPUT_NOT_READY",
        message="请先完成当前结构与证据复核。",
        suggested_action="review_structure_issues",
        context={"status": status},
    )


def _head_context(state: dict[str, Any] | None) -> dict[str, Any]:
    state = state or {}
    return {
        "current_structure_revision_id": state.get(
            "current_structure_revision_id"
        ),
        "current_evidence_revision_id": state.get(
            "current_evidence_revision_id"
        ),
        "current_boundary_revision_id": state.get(
            "current_boundary_revision_id"
        ),
        "current_coverage_revision_id": state.get(
            "current_coverage_revision_id"
        ),
    }


def _revision_conflict(
    state: dict[str, Any] | None,
) -> InterviewV2ImportError:
    return InterviewV2ImportError(
        status_code=409,
        code="ANALYSIS_BOUNDARY_REVISION_CONFLICT",
        message="分析边界或覆盖版本已更新，请刷新后合并更改。",
        suggested_action="refresh_analysis_boundary",
        context=_head_context(state),
    )


def _input_conflict(exc: Exception) -> InterviewV2ImportError:
    context = {
        "current_structure_revision_id": getattr(
            exc, "current_structure_revision_id", None
        ),
        "current_evidence_revision_id": getattr(
            exc, "current_evidence_revision_id", None
        ),
        "current_structure_status": getattr(
            exc, "current_structure_status", None
        ),
    }
    return InterviewV2ImportError(
        status_code=409,
        code="ANALYSIS_BOUNDARY_INPUT_CONFLICT",
        message="结构或证据已更新，请刷新后重新确认分析边界。",
        suggested_action="refresh_structure_review",
        context={key: value for key, value in context.items() if value},
    )


def _revision_content(revision: dict[str, Any], key: str) -> dict[str, Any]:
    content = revision.get(key)
    return deepcopy(content if isinstance(content, dict) else revision)


def _load_structure_input(
    import_id: str,
    login: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    public = get_interview_import_with_mapping_status(import_id, login)
    if str(public.get("status") or "") != "GROUP_MAPPING_CONFIRMED":
        raise _not_ready(str(public.get("status") or ""))
    try:
        bundle = store.load_current_structure_bundle(
            str(public.get("project_id") or ""),
            str(public.get("import_id") or ""),
        )
    except (OSError, TypeError, ValueError) as exc:
        raise _persistence_error(
            "分析边界所需的结构与证据读取失败，请稍后重试。"
        ) from exc
    if bundle is None:
        raise _not_ready("GROUP_MAPPING_CONFIRMED")
    state = bundle.get("state") or {}
    status = str(state.get("effective_status") or "")
    if bool(state.get("is_stale")) or state.get("artifact_status") == "STALE":
        raise _not_ready("STRUCTURE_INPUT_STALE")
    if status != "READY_FOR_DOSSIERS":
        raise _not_ready(status)
    return public, bundle


def _structure_parts(
    bundle: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    state = bundle.get("state") or {}
    structure_revision = bundle.get("structure_revision") or {}
    evidence_revision = bundle.get("evidence_revision") or {}
    heads = {
        "structure_revision_id": str(
            state.get("current_structure_revision_id") or ""
        ),
        "structure_payload_sha256": str(
            state.get("current_structure_payload_sha256") or ""
        ),
        "evidence_revision_id": str(
            state.get("current_evidence_revision_id") or ""
        ),
        "evidence_payload_sha256": str(
            state.get("current_evidence_payload_sha256") or ""
        ),
    }
    return (
        _revision_content(structure_revision, "structure"),
        _revision_content(evidence_revision, "evidence"),
        heads,
    )


def _confirmation_ready_for(
    *,
    public: dict[str, Any],
    structure: dict[str, Any],
    evidence: dict[str, Any],
    heads: dict[str, str],
    boundary: dict[str, Any],
) -> bool:
    try:
        confirm_analysis_boundary_payload(
            boundary,
            structure,
            evidence,
            project_id=str(public.get("project_id") or ""),
            import_id=str(public.get("import_id") or ""),
            structure_revision_id=heads["structure_revision_id"],
            evidence_revision_id=heads["evidence_revision_id"],
        )
    except InterviewV2AnalysisBoundaryError:
        return False
    return True


def _load_boundary_bundle_after_owner_check(
    public: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        return store.load_current_analysis_boundary_bundle(
            str(public.get("project_id") or ""),
            str(public.get("import_id") or ""),
        )
    except (OSError, TypeError, ValueError) as exc:
        raise _persistence_error(
            "分析边界状态校验失败，请稍后重试。"
        ) from exc


def _public_boundary(boundary: dict[str, Any]) -> dict[str, Any]:
    source = boundary.get("source") or {}
    public_source = {
        key: deepcopy(source[key])
        for key in {
            "project_id",
            "import_id",
            "structure_revision_id",
            "evidence_revision_id",
            "rules_version",
        }
        if key in source
    }
    object_fields = {
        "evaluation_object_id",
        "module_id",
        "parent_evaluation_object_id",
        "object_type",
        "display_name",
        "display_order",
        "main_question_ids",
        "occurrence_ids",
        "decision_status",
        "decision_source",
        "supersedes_evaluation_object_ids",
    }
    source_fields = {
        "source_scope_rule_id",
        "group_id",
        "sheet_id",
        "start_row",
        "end_row",
        "scope_type",
        "display_order",
        "allowed_split_rows",
        "decision_status",
        "decision_source",
    }
    label_fields = {
        "label_scope_rule_id",
        "label_key",
        "label_name",
        "scope_mode",
        "module_ids",
        "evaluation_object_ids",
        "decision_status",
        "decision_source",
    }
    return {
        "analysis_boundary_schema_version": boundary.get(
            "analysis_boundary_schema_version"
        ),
        "source": public_source,
        "evaluation_objects": [
            {key: deepcopy(item[key]) for key in object_fields if key in item}
            for item in boundary.get("evaluation_objects") or []
            if isinstance(item, dict)
        ],
        "source_scope_rules": [
            {key: deepcopy(item[key]) for key in source_fields if key in item}
            for item in boundary.get("source_scope_rules") or []
            if isinstance(item, dict)
        ],
        "label_scope_rules": [
            {key: deepcopy(item[key]) for key in label_fields if key in item}
            for item in boundary.get("label_scope_rules") or []
            if isinstance(item, dict)
        ],
        "status": boundary.get("status"),
    }


def _public_coverage(coverage: dict[str, Any]) -> dict[str, Any]:
    source = coverage.get("source") or {}
    public_source = {
        key: deepcopy(source[key])
        for key in {
            "project_id",
            "import_id",
            "structure_revision_id",
            "evidence_revision_id",
            "analysis_boundary_sha256",
            "rules_version",
        }
        if key in source
    }
    row_fields = {
        "coverage_id",
        "participant_id",
        "group_id",
        "evaluation_object_id",
        "module_id",
        "main_question_id",
        "source_presence",
        "asked_status",
        "applicability",
        "review_status",
        "derived_status",
        "self_report_count",
        "follow_up_count",
        "observation_count",
        "source_occurrence_ids",
        "self_report_evidence_ids",
        "observation_evidence_ids",
    }
    summary_fields = {
        "module_id",
        "evaluation_object_id",
        "main_question_id",
        "participant_count",
        "covered_participant_count",
        "observation_only_participant_count",
        "no_record_participant_count",
        "denominator_reliable",
        "denominator_participant_count",
        "proportion",
    }
    return {
        "coverage_schema_version": coverage.get("coverage_schema_version"),
        "source": public_source,
        "participant_count": coverage.get("participant_count", 0),
        "row_count": coverage.get("row_count", 0),
        "rows": [
            {key: deepcopy(item[key]) for key in row_fields if key in item}
            for item in coverage.get("rows") or []
            if isinstance(item, dict)
        ],
        "summaries": [
            {key: deepcopy(item[key]) for key in summary_fields if key in item}
            for item in coverage.get("summaries") or []
            if isinstance(item, dict)
        ],
    }


def _response_from_payloads(
    *,
    public: dict[str, Any],
    boundary: dict[str, Any],
    coverage: dict[str, Any],
    state: dict[str, Any] | None = None,
    boundary_revision: dict[str, Any] | None = None,
    coverage_revision: dict[str, Any] | None = None,
    confirmation_ready: bool | None = None,
) -> dict[str, Any]:
    state = state or {}
    boundary_revision = boundary_revision or {}
    coverage_revision = coverage_revision or {}
    if confirmation_ready is None:
        confirmation_ready = boundary.get("status") == "confirmed"
    return {
        "import_id": public.get("import_id"),
        "project_id": public.get("project_id"),
        "status": state.get("derived_status")
        or state.get("effective_status")
        or "ANALYSIS_BOUNDARY_REQUIRED",
        "structure_revision_id": (
            state.get("current_structure_revision_id")
            or (boundary.get("source") or {}).get("structure_revision_id")
        ),
        "evidence_revision_id": (
            state.get("current_evidence_revision_id")
            or (boundary.get("source") or {}).get("evidence_revision_id")
        ),
        "boundary_revision_id": state.get("current_boundary_revision_id"),
        "boundary_revision_number": state.get(
            "current_boundary_revision_number"
        ),
        "boundary_payload_sha256": (
            state.get("current_boundary_payload_sha256")
            or boundary_revision.get("revision_payload_sha256")
        ),
        "coverage_revision_id": state.get("current_coverage_revision_id"),
        "coverage_revision_number": state.get(
            "current_coverage_revision_number"
        ),
        "coverage_payload_sha256": (
            state.get("current_coverage_payload_sha256")
            or coverage_revision.get("revision_payload_sha256")
        ),
        "analysis_boundary": _public_boundary(boundary),
        "coverage_preview": _public_coverage(coverage),
        "open_issue_count": 0,
        "blocking_issue_count": 0,
        "confirmation_ready": bool(confirmation_ready),
        "is_stale": bool(state.get("is_stale")),
    }


def _response_from_bundle(
    public: dict[str, Any],
    bundle: dict[str, Any],
    *,
    confirmation_ready: bool | None = None,
) -> dict[str, Any]:
    boundary_revision = bundle.get("boundary_revision") or {}
    coverage_revision = bundle.get("coverage_revision") or {}
    return _response_from_payloads(
        public=public,
        boundary=_revision_content(boundary_revision, "analysis_boundary"),
        coverage=_revision_content(coverage_revision, "coverage_preview"),
        state=bundle.get("state") or {},
        boundary_revision=boundary_revision,
        coverage_revision=coverage_revision,
        confirmation_ready=confirmation_ready,
    )


def _proposal_response(
    public: dict[str, Any],
    structure_bundle: dict[str, Any],
    *,
    stale_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    structure, evidence, heads = _structure_parts(structure_bundle)
    try:
        proposal = build_analysis_boundary_proposal(
            structure,
            evidence,
            project_id=str(public.get("project_id") or ""),
            import_id=str(public.get("import_id") or ""),
            structure_revision_id=heads["structure_revision_id"],
            evidence_revision_id=heads["evidence_revision_id"],
        )
    except InterviewV2AnalysisBoundaryError as exc:
        raise InterviewV2ImportError(
            status_code=422,
            code=exc.code,
            message=exc.message,
            suggested_action="review_analysis_boundary",
            context=exc.context,
        ) from exc
    boundary = proposal.get("analysis_boundary") or {}
    proposal_state: dict[str, Any] = {}
    if stale_state:
        proposal_state = {
            "derived_status": "ANALYSIS_BOUNDARY_REQUIRED",
            "current_boundary_revision_id": stale_state.get(
                "current_boundary_revision_id"
            ),
            "current_boundary_revision_number": stale_state.get(
                "current_boundary_revision_number"
            ),
            "current_coverage_revision_id": stale_state.get(
                "current_coverage_revision_id"
            ),
            "current_coverage_revision_number": stale_state.get(
                "current_coverage_revision_number"
            ),
            "is_stale": True,
        }
    return _response_from_payloads(
        public=public,
        boundary=boundary,
        coverage=proposal.get("coverage_preview") or {},
        state=proposal_state,
        confirmation_ready=_confirmation_ready_for(
            public=public,
            structure=structure,
            evidence=evidence,
            heads=heads,
            boundary=boundary,
        ),
    )


def get_analysis_boundary(
    import_id: str, login: dict[str, Any] | None
) -> dict[str, Any]:
    public, structure_bundle = _load_structure_input(import_id, login)
    current = _load_boundary_bundle_after_owner_check(public)
    if current is not None and not bool((current.get("state") or {}).get("is_stale")):
        structure, evidence, heads = _structure_parts(structure_bundle)
        boundary = _revision_content(
            current.get("boundary_revision") or {}, "analysis_boundary"
        )
        return _response_from_bundle(
            public,
            current,
            confirmation_ready=_confirmation_ready_for(
                public=public,
                structure=structure,
                evidence=evidence,
                heads=heads,
                boundary=boundary,
            ),
        )
    return _proposal_response(
        public,
        structure_bundle,
        stale_state=(current or {}).get("state") or None,
    )


def get_coverage_preview(
    import_id: str, login: dict[str, Any] | None
) -> dict[str, Any]:
    response = get_analysis_boundary(import_id, login)
    return {
        key: response.get(key)
        for key in {
            "import_id",
            "project_id",
            "status",
            "structure_revision_id",
            "evidence_revision_id",
            "boundary_revision_id",
            "boundary_payload_sha256",
            "coverage_revision_id",
            "coverage_payload_sha256",
            "coverage_preview",
            "is_stale",
        }
    }


def _request_boundary(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "evaluation_objects": deepcopy(request.get("evaluation_objects") or []),
        "source_scope_rules": deepcopy(request.get("source_scope_rules") or []),
        "label_scope_rules": deepcopy(request.get("label_scope_rules") or []),
    }


def _build_revision_pair(
    *,
    public: dict[str, Any],
    heads: dict[str, str],
    boundary: dict[str, Any],
    coverage: dict[str, Any],
    revision_number: int,
    base_boundary_revision_id: str | None,
    base_coverage_revision_id: str | None,
    actor: str,
    change_reason: str,
    operation: str,
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    fingerprint_payload = {
        "operation": operation,
        "project_id": public.get("project_id"),
        "import_id": public.get("import_id"),
        "source": heads,
        "base_boundary_revision_id": base_boundary_revision_id,
        "base_coverage_revision_id": base_coverage_revision_id,
        "analysis_boundary": boundary,
        "change_reason": change_reason,
        "actor": actor,
    }
    request_fingerprint = canonical_json_sha256(fingerprint_payload)
    boundary_payload_sha = canonical_json_sha256(boundary)
    boundary_revision_id = _stable_id(
        "boundary",
        {
            "project_id": public.get("project_id"),
            "import_id": public.get("import_id"),
            "revision_number": revision_number,
            "boundary_payload_sha256": boundary_payload_sha,
            "request_fingerprint": request_fingerprint,
        },
    )
    coverage = deepcopy(coverage)
    coverage.setdefault("source", {})["boundary_revision_id"] = (
        boundary_revision_id
    )
    coverage_payload_sha = canonical_json_sha256(coverage)
    coverage_revision_id = _stable_id(
        "coverage",
        {
            "project_id": public.get("project_id"),
            "import_id": public.get("import_id"),
            "revision_number": revision_number,
            "coverage_payload_sha256": coverage_payload_sha,
            "request_fingerprint": request_fingerprint,
        },
    )
    created_at = _now()
    source = {
        "structure_revision_id": heads["structure_revision_id"],
        "structure_payload_sha256": heads["structure_payload_sha256"],
        "evidence_revision_id": heads["evidence_revision_id"],
        "evidence_payload_sha256": heads["evidence_payload_sha256"],
    }
    boundary_revision = {
        "schema_version": "interview-v2-analysis-boundary-revision/1.0",
        "project_id": public.get("project_id"),
        "import_id": public.get("import_id"),
        "revision_number": revision_number,
        "boundary_revision_id": boundary_revision_id,
        "request_fingerprint": request_fingerprint,
        "source": source,
        "analysis_boundary": deepcopy(boundary),
        "change_reason": change_reason,
        "created_at": created_at,
        "created_by": actor,
    }
    boundary_revision["revision_payload_sha256"] = (
        store.analysis_boundary_revision_payload_sha256(boundary_revision)
    )
    coverage_revision = {
        "schema_version": "interview-v2-coverage-revision/1.0",
        "project_id": public.get("project_id"),
        "import_id": public.get("import_id"),
        "revision_number": revision_number,
        "coverage_revision_id": coverage_revision_id,
        "boundary_revision_id": boundary_revision_id,
        "boundary_payload_sha256": boundary_revision[
            "revision_payload_sha256"
        ],
        "request_fingerprint": request_fingerprint,
        "source": source,
        "coverage_preview": coverage,
        "created_at": created_at,
    }
    coverage_revision["revision_payload_sha256"] = (
        store.coverage_revision_payload_sha256(coverage_revision)
    )
    return (
        boundary_revision,
        coverage_revision,
        request_fingerprint,
        created_at,
    )


def save_analysis_boundary(
    import_id: str,
    request: dict[str, Any],
    login: dict[str, Any] | None,
) -> dict[str, Any]:
    public, structure_bundle = _load_structure_input(import_id, login)
    structure, evidence, heads = _structure_parts(structure_bundle)
    for request_key, head_key in (
        ("base_structure_revision_id", "structure_revision_id"),
        ("base_evidence_revision_id", "evidence_revision_id"),
    ):
        if str(request.get(request_key) or "") != heads[head_key]:
            raise _revision_conflict(
                {
                    "current_structure_revision_id": heads[
                        "structure_revision_id"
                    ],
                    "current_evidence_revision_id": heads[
                        "evidence_revision_id"
                    ],
                }
            )

    current = _load_boundary_bundle_after_owner_check(public)
    current_state = (current or {}).get("state") or {}
    base_boundary_id = request.get("base_boundary_revision_id")
    base_coverage_id = request.get("base_coverage_revision_id")
    bases_match_current = (
        (current_state.get("current_boundary_revision_id") or None)
        == (base_boundary_id or None)
        and (current_state.get("current_coverage_revision_id") or None)
        == (base_coverage_id or None)
    )
    current_number = int(
        current_state.get("current_boundary_revision_number") or 0
    )

    try:
        if bases_match_current:
            revision_number = current_number + 1
            if current is not None and not bool(current_state.get("is_stale")):
                base_boundary = _revision_content(
                    current.get("boundary_revision") or {},
                    "analysis_boundary",
                )
            else:
                base_boundary = (
                    build_analysis_boundary_proposal(
                        structure,
                        evidence,
                        project_id=str(public.get("project_id") or ""),
                        import_id=str(public.get("import_id") or ""),
                        structure_revision_id=heads["structure_revision_id"],
                        evidence_revision_id=heads["evidence_revision_id"],
                    ).get("analysis_boundary")
                    or {}
                )
        else:
            if base_boundary_id is None and base_coverage_id is None:
                if current_number != 1:
                    raise _revision_conflict(current_state)
                base_revision_number = 0
                base_boundary = (
                    build_analysis_boundary_proposal(
                        structure,
                        evidence,
                        project_id=str(public.get("project_id") or ""),
                        import_id=str(public.get("import_id") or ""),
                        structure_revision_id=heads["structure_revision_id"],
                        evidence_revision_id=heads["evidence_revision_id"],
                    ).get("analysis_boundary")
                    or {}
                )
            else:
                matching_entries = [
                    entry
                    for entry in current_state.get("revision_history") or []
                    if isinstance(entry, dict)
                    and entry.get("boundary_revision_id") == base_boundary_id
                    and entry.get("coverage_revision_id") == base_coverage_id
                ]
                if len(matching_entries) != 1:
                    raise _revision_conflict(current_state)
                base_revision_number = int(
                    matching_entries[0].get("revision_number") or 0
                )
                if current_number != base_revision_number + 1:
                    raise _revision_conflict(current_state)
                try:
                    base_revision = store.load_analysis_boundary_revision(
                        str(public.get("project_id") or ""),
                        str(base_boundary_id or ""),
                    )
                except (OSError, TypeError, ValueError) as exc:
                    raise _persistence_error() from exc
                if base_revision is None:
                    raise _revision_conflict(current_state)
                base_source = base_revision.get("source") or {}
                if any(
                    str(base_source.get(key) or "") != heads[key]
                    for key in (
                        "structure_revision_id",
                        "structure_payload_sha256",
                        "evidence_revision_id",
                        "evidence_payload_sha256",
                    )
                ):
                    base_boundary = (
                        build_analysis_boundary_proposal(
                            structure,
                            evidence,
                            project_id=str(public.get("project_id") or ""),
                            import_id=str(public.get("import_id") or ""),
                            structure_revision_id=heads[
                                "structure_revision_id"
                            ],
                            evidence_revision_id=heads[
                                "evidence_revision_id"
                            ],
                        ).get("analysis_boundary")
                        or {}
                    )
                else:
                    base_boundary = _revision_content(
                        base_revision, "analysis_boundary"
                    )
            revision_number = base_revision_number + 1

        boundary = validate_analysis_boundary(
            _request_boundary(request),
            structure,
            evidence,
            project_id=str(public.get("project_id") or ""),
            import_id=str(public.get("import_id") or ""),
            structure_revision_id=heads["structure_revision_id"],
            evidence_revision_id=heads["evidence_revision_id"],
            base_boundary=base_boundary,
        )
        coverage = build_coverage_preview(structure, evidence, boundary)
    except InterviewV2AnalysisBoundaryError as exc:
        raise InterviewV2ImportError(
            status_code=422,
            code=exc.code,
            message=exc.message,
            suggested_action="review_analysis_boundary",
            context=exc.context,
        ) from exc

    actor = str(_owner_from_login(login).get("owner_key") or "")
    change_reason = str(request.get("change_reason") or "").strip()
    (
        boundary_revision,
        coverage_revision,
        request_fingerprint,
        created_at,
    ) = _build_revision_pair(
        public=public,
        heads=heads,
        boundary=boundary,
        coverage=coverage,
        revision_number=revision_number,
        base_boundary_revision_id=base_boundary_id,
        base_coverage_revision_id=base_coverage_id,
        actor=actor,
        change_reason=change_reason,
        operation="manual_edit",
    )
    if not bases_match_current:
        if (
            current is not None
            and current_state.get("current_request_fingerprint")
            == request_fingerprint
            and current_state.get("current_boundary_revision_id")
            == boundary_revision.get("boundary_revision_id")
            and current_state.get("current_coverage_revision_id")
            == coverage_revision.get("coverage_revision_id")
        ):
            current_boundary = _revision_content(
                current.get("boundary_revision") or {}, "analysis_boundary"
            )
            return _response_from_bundle(
                public,
                current,
                confirmation_ready=_confirmation_ready_for(
                    public=public,
                    structure=structure,
                    evidence=evidence,
                    heads=heads,
                    boundary=current_boundary,
                ),
            )
        raise _revision_conflict(current_state)
    try:
        saved_boundary, saved_coverage, saved_state = (
            store.save_analysis_boundary_bundle_cas(
                project_id=str(public.get("project_id") or ""),
                import_id=str(public.get("import_id") or ""),
                base_boundary_revision_id=base_boundary_id,
                base_coverage_revision_id=base_coverage_id,
                boundary_revision=boundary_revision,
                coverage_revision=coverage_revision,
                request_fingerprint=request_fingerprint,
                updated_at=created_at,
            )
        )
    except store.AnalysisBoundaryInputConflictError as exc:
        raise _input_conflict(exc) from exc
    except FileExistsError as exc:
        latest = _load_boundary_bundle_after_owner_check(public)
        latest_state = (latest or {}).get("state") or {}
        if (
            latest
            and latest_state.get("current_request_fingerprint")
            == request_fingerprint
            and latest_state.get("current_boundary_revision_id")
            == boundary_revision.get("boundary_revision_id")
            and latest_state.get("current_coverage_revision_id")
            == coverage_revision.get("coverage_revision_id")
        ):
            latest_boundary = _revision_content(
                latest.get("boundary_revision") or {}, "analysis_boundary"
            )
            return _response_from_bundle(
                public,
                latest,
                confirmation_ready=_confirmation_ready_for(
                    public=public,
                    structure=structure,
                    evidence=evidence,
                    heads=heads,
                    boundary=latest_boundary,
                ),
            )
        raise _revision_conflict(latest_state) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise _persistence_error() from exc

    return _response_from_payloads(
        public=public,
        boundary=_revision_content(saved_boundary, "analysis_boundary"),
        coverage=_revision_content(saved_coverage, "coverage_preview"),
        state=saved_state,
        boundary_revision=saved_boundary,
        coverage_revision=saved_coverage,
        confirmation_ready=_confirmation_ready_for(
            public=public,
            structure=structure,
            evidence=evidence,
            heads=heads,
            boundary=_revision_content(saved_boundary, "analysis_boundary"),
        ),
    )


def confirm_analysis_boundary(
    import_id: str,
    request: dict[str, Any],
    login: dict[str, Any] | None,
) -> dict[str, Any]:
    public, structure_bundle = _load_structure_input(import_id, login)
    structure, evidence, heads = _structure_parts(structure_bundle)
    actor = str(_owner_from_login(login).get("owner_key") or "")
    current = _load_boundary_bundle_after_owner_check(public)
    if current is None:
        raise _revision_conflict(None)
    current_state = current.get("state") or {}
    if bool(current_state.get("is_stale")) or any(
        str(current_state.get(state_key) or "") != heads[head_key]
        for state_key, head_key in (
            ("current_structure_revision_id", "structure_revision_id"),
            ("current_evidence_revision_id", "evidence_revision_id"),
        )
    ):
        raise InterviewV2ImportError(
            status_code=409,
            code="ANALYSIS_BOUNDARY_INPUT_CONFLICT",
            message="结构或证据已更新，请刷新后重新确认分析边界。",
            suggested_action="refresh_structure_review",
            context={
                "current_structure_revision_id": heads[
                    "structure_revision_id"
                ],
                "current_evidence_revision_id": heads[
                    "evidence_revision_id"
                ],
                "current_structure_status": "READY_FOR_DOSSIERS",
            },
        )
    requested_boundary_id = str(request.get("boundary_revision_id") or "")
    requested_coverage_id = str(request.get("coverage_revision_id") or "")
    requested_boundary_sha = str(
        request.get("boundary_payload_sha256") or ""
    )
    requested_coverage_sha = str(
        request.get("coverage_payload_sha256") or ""
    )
    current_matches_request = (
        current_state.get("current_boundary_revision_id")
        == requested_boundary_id
        and current_state.get("current_coverage_revision_id")
        == requested_coverage_id
        and current_state.get("current_boundary_payload_sha256")
        == requested_boundary_sha
        and current_state.get("current_coverage_payload_sha256")
        == requested_coverage_sha
    )
    current_boundary = _revision_content(
        current.get("boundary_revision") or {}, "analysis_boundary"
    )
    if (
        current_matches_request
        and current_state.get("effective_status") == "READY_FOR_DOSSIERS"
    ):
        return _response_from_bundle(
            public, current, confirmation_ready=True
        )
    if current_matches_request and current_boundary.get("status") == "confirmed":
        try:
            store.confirm_analysis_boundary_cas(
                project_id=str(public.get("project_id") or ""),
                import_id=str(public.get("import_id") or ""),
                boundary_revision_id=requested_boundary_id,
                coverage_revision_id=requested_coverage_id,
                boundary_payload_sha256=requested_boundary_sha,
                coverage_payload_sha256=requested_coverage_sha,
                confirmed_by=actor,
                confirmed_at=_now(),
            )
        except store.AnalysisBoundaryInputConflictError as exc:
            raise _input_conflict(exc) from exc
        except FileExistsError as exc:
            raise _revision_conflict(current_state) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise _persistence_error() from exc
        latest = _load_boundary_bundle_after_owner_check(public)
        if latest is None:
            raise _persistence_error()
        return _response_from_bundle(
            public, latest, confirmation_ready=True
        )

    if current_matches_request:
        base_entry = {
            "revision_number": current_state.get(
                "current_boundary_revision_number"
            ),
            "boundary_revision_id": requested_boundary_id,
            "coverage_revision_id": requested_coverage_id,
            "boundary_payload_sha256": requested_boundary_sha,
            "coverage_payload_sha256": requested_coverage_sha,
        }
        draft_boundary = current_boundary
    else:
        matching_entries = [
            entry
            for entry in current_state.get("revision_history") or []
            if isinstance(entry, dict)
            and entry.get("boundary_revision_id") == requested_boundary_id
            and entry.get("coverage_revision_id") == requested_coverage_id
            and entry.get("boundary_payload_sha256")
            == requested_boundary_sha
            and entry.get("coverage_payload_sha256")
            == requested_coverage_sha
        ]
        base_entry = matching_entries[0] if len(matching_entries) == 1 else None
        if (
            base_entry is None
            or int(current_state.get("current_boundary_revision_number") or 0)
            != int(base_entry.get("revision_number") or 0) + 1
        ):
            raise _revision_conflict(current_state)
        try:
            draft_revision = store.load_analysis_boundary_revision(
                str(public.get("project_id") or ""), requested_boundary_id
            )
            draft_coverage_revision = store.load_coverage_revision(
                str(public.get("project_id") or ""), requested_coverage_id
            )
        except (OSError, TypeError, ValueError) as exc:
            raise _persistence_error() from exc
        if (
            draft_revision is None
            or draft_coverage_revision is None
            or draft_revision.get("revision_payload_sha256")
            != requested_boundary_sha
            or draft_coverage_revision.get("revision_payload_sha256")
            != requested_coverage_sha
        ):
            raise _revision_conflict(current_state)
        draft_boundary = _revision_content(
            draft_revision, "analysis_boundary"
        )

    try:
        confirmed_boundary = confirm_analysis_boundary_payload(
            draft_boundary,
            structure,
            evidence,
            project_id=str(public.get("project_id") or ""),
            import_id=str(public.get("import_id") or ""),
            structure_revision_id=heads["structure_revision_id"],
            evidence_revision_id=heads["evidence_revision_id"],
        )
        confirmed_coverage = build_coverage_preview(
            structure, evidence, confirmed_boundary
        )
    except InterviewV2AnalysisBoundaryError as exc:
        raise InterviewV2ImportError(
            status_code=422,
            code=exc.code,
            message=exc.message,
            suggested_action="review_analysis_boundary",
            context=exc.context,
        ) from exc

    revision_number = int(base_entry.get("revision_number") or 0) + 1
    (
        confirmed_boundary_revision,
        confirmed_coverage_revision,
        request_fingerprint,
        created_at,
    ) = _build_revision_pair(
        public=public,
        heads=heads,
        boundary=confirmed_boundary,
        coverage=confirmed_coverage,
        revision_number=revision_number,
        base_boundary_revision_id=requested_boundary_id,
        base_coverage_revision_id=requested_coverage_id,
        actor=actor,
        change_reason="confirm_analysis_boundary",
        operation="confirm",
    )

    if not current_matches_request:
        if (
            current_state.get("current_request_fingerprint")
            != request_fingerprint
            or current_state.get("current_boundary_revision_id")
            != confirmed_boundary_revision.get("boundary_revision_id")
            or current_state.get("current_coverage_revision_id")
            != confirmed_coverage_revision.get("coverage_revision_id")
        ):
            raise _revision_conflict(current_state)
        saved_boundary = current.get("boundary_revision") or {}
        saved_coverage = current.get("coverage_revision") or {}
        saved_state = current_state
    else:
        try:
            saved_boundary, saved_coverage, saved_state = (
                store.save_analysis_boundary_bundle_cas(
                    project_id=str(public.get("project_id") or ""),
                    import_id=str(public.get("import_id") or ""),
                    base_boundary_revision_id=requested_boundary_id,
                    base_coverage_revision_id=requested_coverage_id,
                    boundary_revision=confirmed_boundary_revision,
                    coverage_revision=confirmed_coverage_revision,
                    request_fingerprint=request_fingerprint,
                    updated_at=created_at,
                )
            )
        except store.AnalysisBoundaryInputConflictError as exc:
            raise _input_conflict(exc) from exc
        except FileExistsError as exc:
            latest = _load_boundary_bundle_after_owner_check(public)
            latest_state = (latest or {}).get("state") or {}
            if (
                latest
                and latest_state.get("current_request_fingerprint")
                == request_fingerprint
                and latest_state.get("current_boundary_revision_id")
                == confirmed_boundary_revision.get("boundary_revision_id")
                and latest_state.get("current_coverage_revision_id")
                == confirmed_coverage_revision.get("coverage_revision_id")
            ):
                saved_boundary = latest.get("boundary_revision") or {}
                saved_coverage = latest.get("coverage_revision") or {}
                saved_state = latest_state
            else:
                raise _revision_conflict(latest_state) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise _persistence_error() from exc

    try:
        store.confirm_analysis_boundary_cas(
            project_id=str(public.get("project_id") or ""),
            import_id=str(public.get("import_id") or ""),
            boundary_revision_id=str(
                saved_boundary.get("boundary_revision_id") or ""
            ),
            coverage_revision_id=str(
                saved_coverage.get("coverage_revision_id") or ""
            ),
            boundary_payload_sha256=str(
                saved_boundary.get("revision_payload_sha256") or ""
            ),
            coverage_payload_sha256=str(
                saved_coverage.get("revision_payload_sha256") or ""
            ),
            confirmed_by=actor,
            confirmed_at=_now(),
        )
    except store.AnalysisBoundaryInputConflictError as exc:
        raise _input_conflict(exc) from exc
    except FileExistsError as exc:
        latest = _load_boundary_bundle_after_owner_check(public)
        raise _revision_conflict((latest or {}).get("state") or {}) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise _persistence_error() from exc
    latest = _load_boundary_bundle_after_owner_check(public)
    if latest is None:
        raise _persistence_error()
    return _response_from_bundle(
        public, latest, confirmation_ready=True
    )


__all__ = [
    "confirm_analysis_boundary",
    "get_analysis_boundary",
    "get_coverage_preview",
    "save_analysis_boundary",
]
