"""HTTP routes for multi-language Google Forms questionnaire families."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from app.core.google_forms_links import GoogleFormsLinkError, parse_google_forms_edit_link
from app.core.security import _owner_from_login
from app.schemas.questionnaire_families import (
    QuestionnaireFamilyAnalysisSessionResponse,
    QuestionnaireFamilyCatalogResponse,
    QuestionnaireFamilyCreateRequest,
    QuestionnaireFamilySummary,
)
from app.services.auth import _require_feature
from app.services.google_forms_family_api import (
    GoogleFormsFamilyApi,
    GoogleFormsFamilyInternalError,
    GoogleFormsFamilyInvalidError,
    GoogleFormsFamilyMappingUnavailableError,
    GoogleFormsFamilyNeedsReviewError,
    GoogleFormsFamilyNoResponsesError,
    GoogleFormsFamilyNotFoundError,
    GoogleFormsFamilyProviderError,
)
from app.services.llm_credentials import require_request_llm_api_key


_MAX_REQUEST_BYTES = 32 * 1024
_DEFAULT_CATALOG_LIMIT = 20
_MAX_CATALOG_LIMIT = 50


def _owner_key(login: dict | None) -> str:
    owner = str(_owner_from_login(login)["owner_key"]).strip()
    if not owner:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "google_forms_family_authentication_required",
                "message": "请先登录飞书",
            },
        )
    return owner


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


async def _parse_create_request(request: Request) -> QuestionnaireFamilyCreateRequest:
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if media_type.strip().casefold() != "application/json":
        raise HTTPException(
            status_code=415,
            detail={
                "code": "google_forms_family_unsupported_media_type",
                "message": "问卷项目请求必须使用 JSON",
            },
        )
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_REQUEST_BYTES:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "google_forms_family_request_too_large",
                    "message": "问卷项目请求超过大小限制",
                },
            )
    try:
        value = json.loads(
            bytes(body).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
        return QuestionnaireFamilyCreateRequest.model_validate(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as error:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "google_forms_family_invalid_request",
                "message": "问卷项目请求无效",
            },
        ) from error


def _catalog_query_error() -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "code": "google_forms_family_invalid_catalog_query",
            "message": "调研项目目录查询参数无效",
        },
    )


def _parse_catalog_query(request: Request) -> tuple[str | None, int]:
    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in {"cursor", "limit"} or key in values:
            raise _catalog_query_error()
        values[key] = value
    cursor = values.get("cursor")
    if (
        cursor is not None
        and (
            not cursor
            or len(cursor) > 512
            or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                for character in cursor
            )
        )
    ):
        raise _catalog_query_error()
    limit_value = values.get("limit")
    if limit_value is None:
        limit = _DEFAULT_CATALOG_LIMIT
    elif not limit_value.isascii() or not limit_value.isdigit():
        raise _catalog_query_error()
    else:
        limit = int(limit_value)
        if limit < 1 or limit > _MAX_CATALOG_LIMIT:
            raise _catalog_query_error()
    return cursor, limit


def _raise_family_error(error: Exception) -> None:
    if isinstance(error, GoogleFormsFamilyInvalidError):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "google_forms_family_invalid",
                "message": "问卷项目或回答数据无效",
            },
        ) from error
    if isinstance(error, GoogleFormsFamilyNotFoundError):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "google_forms_family_not_found",
                "message": "问卷项目不存在或当前账号无权访问",
            },
        ) from error
    if isinstance(error, GoogleFormsFamilyMappingUnavailableError):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "google_forms_family_mapping_unavailable",
                "message": "自动语义匹配暂时失败，请重试；这不代表问卷版本存在差异",
            },
        ) from error
    if isinstance(error, GoogleFormsFamilyNeedsReviewError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "google_forms_family_needs_review",
                "message": (
                    "问卷结构存在阻断差异。系统当前不能直接修改映射；"
                    "请根据下方问题修改原 Form，然后重新检查结构。"
                ),
                "summary": {
                    "family_id": error.family.family_id,
                    "status": error.family.status.value,
                    "blocking_issue_count": sum(
                        item.affected_count
                        for item in error.family.diagnostics
                        if item.blocking
                    ),
                    "diagnostics": [
                        item.model_dump(mode="json")
                        for item in error.family.diagnostics
                    ],
                },
            },
        ) from error
    if isinstance(error, GoogleFormsFamilyNoResponsesError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "google_forms_family_no_responses",
                "message": "当前项目还没有可分析的回答，请确认 Form 已收到至少一条回答后重试",
            },
        ) from error
    if isinstance(error, GoogleFormsFamilyProviderError):
        contracts = {
            "google_forms_provider_authentication_failed": (
                401,
                "Google 服务账号授权无效，请联系管理员检查只读凭据",
            ),
            "google_forms_provider_permission_denied": (
                403,
                "服务账号无权读取某份 Form 的问卷或回答，请将编辑权限共享给部署服务账号后重试",
            ),
            "google_forms_provider_form_not_found": (
                404,
                "某份 Google Form 不存在或服务账号当前不可见，请检查编辑链接和共享权限",
            ),
            "google_forms_provider_rate_limited": (
                429,
                "Google Forms 请求过于频繁，请稍后重试",
            ),
            "google_forms_provider_unavailable": (
                503,
                "Google Forms 暂时不可用，请稍后重试",
            ),
            "google_forms_provider_error": (
                502,
                "Google Forms 暂时无法返回可用数据",
            ),
        }
        code = error.code if error.code in contracts else "google_forms_provider_error"
        status_code, message = contracts[code]
        raise HTTPException(
            status_code=status_code,
            detail={"code": code, "message": message},
        ) from error
    if isinstance(error, GoogleFormsFamilyInternalError):
        raise HTTPException(
            status_code=500,
            detail={
                "code": "google_forms_family_internal_error",
                "message": "问卷项目服务暂时不可用",
            },
        ) from error
    raise HTTPException(
        status_code=500,
        detail={
            "code": "google_forms_family_internal_error",
            "message": "问卷项目服务暂时不可用",
        },
    ) from error


def create_google_forms_families_router(api: GoogleFormsFamilyApi) -> APIRouter:
    if not isinstance(api, GoogleFormsFamilyApi):
        raise TypeError("api 必须是 GoogleFormsFamilyApi")
    router = APIRouter()

    @router.get(
        "/api/questionnaire-sources/google-forms/families",
        response_model=QuestionnaireFamilyCatalogResponse,
    )
    async def list_families(request: Request) -> QuestionnaireFamilyCatalogResponse:
        login = await _require_feature(request, "survey")
        cursor, limit = _parse_catalog_query(request)
        try:
            return await api.list_families(
                _owner_key(login),
                cursor=cursor,
                limit=limit,
            )
        except Exception as error:
            _raise_family_error(error)
            raise AssertionError("unreachable")

    @router.post(
        "/api/questionnaire-sources/google-forms/families",
        response_model=QuestionnaireFamilySummary,
    )
    async def create_family(request: Request) -> QuestionnaireFamilySummary:
        login = await _require_feature(request, "survey")
        owner_ref = _owner_key(login)
        payload = await _parse_create_request(request)
        variants = []
        try:
            for item in payload.variants:
                variants.append((item.language, parse_google_forms_edit_link(item.form_url)))
            llm_api_key = (
                await require_request_llm_api_key(request)
                if len(variants) > 1
                else None
            )
            return await api.create_family(
                owner_ref,
                payload.title,
                variants,
                llm_api_key=llm_api_key,
            )
        except GoogleFormsLinkError as error:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "google_forms_family_invalid_edit_link",
                    "message": "请为每个版本填写 /forms/d/.../edit Google Forms 编辑链接",
                },
            ) from error
        except HTTPException:
            raise
        except Exception as error:
            _raise_family_error(error)
            raise AssertionError("unreachable")

    @router.get(
        "/api/questionnaire-sources/google-forms/families/{family_id}",
        response_model=QuestionnaireFamilySummary,
    )
    async def get_family(family_id: str, request: Request) -> QuestionnaireFamilySummary:
        login = await _require_feature(request, "survey")
        try:
            return await api.get_family(_owner_key(login), family_id)
        except Exception as error:
            _raise_family_error(error)
            raise AssertionError("unreachable")

    @router.post(
        "/api/questionnaire-sources/google-forms/families/{family_id}/refresh",
        response_model=QuestionnaireFamilySummary,
    )
    async def refresh_family(
        family_id: str,
        request: Request,
    ) -> QuestionnaireFamilySummary:
        login = await _require_feature(request, "survey")
        try:
            owner_ref = _owner_key(login)
            existing = await api.get_family(owner_ref, family_id)
            llm_api_key = (
                await require_request_llm_api_key(request)
                if existing.variant_count > 1
                else None
            )
            return await api.refresh_family(
                owner_ref,
                family_id,
                llm_api_key=llm_api_key,
            )
        except HTTPException:
            raise
        except Exception as error:
            _raise_family_error(error)
            raise AssertionError("unreachable")

    @router.post(
        "/api/questionnaire-sources/google-forms/families/{family_id}/analysis-sessions",
        response_model=QuestionnaireFamilyAnalysisSessionResponse,
    )
    async def create_family_analysis_session(
        family_id: str,
        request: Request,
    ) -> QuestionnaireFamilyAnalysisSessionResponse:
        login = await _require_feature(request, "survey")
        try:
            return await api.create_analysis_session(
                _owner_key(login),
                family_id,
                login,
            )
        except Exception as error:
            _raise_family_error(error)
            raise AssertionError("unreachable")

    return router


__all__ = ["create_google_forms_families_router"]
