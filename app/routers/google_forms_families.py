"""HTTP routes for multi-language Google Forms questionnaire families."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from app.core.google_forms_links import GoogleFormsLinkError, parse_google_forms_edit_link
from app.core.security import _owner_from_login
from app.schemas.questionnaire_families import (
    QuestionnaireFamilyAnalysisSessionResponse,
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
    GoogleFormsFamilyNotFoundError,
    GoogleFormsFamilyProviderError,
)


_MAX_REQUEST_BYTES = 32 * 1024


def _owner_key(login: dict | None) -> str:
    owner = str(_owner_from_login(login)["owner_key"]).strip()
    if not owner:
        raise HTTPException(status_code=401, detail="请先登录飞书")
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
        raise HTTPException(status_code=415, detail="问卷家族请求必须使用 JSON")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="问卷家族请求超过大小限制")
    try:
        value = json.loads(
            bytes(body).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
        return QuestionnaireFamilyCreateRequest.model_validate(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as error:
        raise HTTPException(status_code=422, detail="问卷家族请求无效") from error


def _raise_family_error(error: Exception) -> None:
    if isinstance(error, GoogleFormsFamilyInvalidError):
        raise HTTPException(status_code=422, detail="问卷家族或回答数据无效") from error
    if isinstance(error, GoogleFormsFamilyNotFoundError):
        raise HTTPException(status_code=404, detail="问卷家族不存在") from error
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
                "message": str(error),
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
    if isinstance(error, GoogleFormsFamilyProviderError):
        status_code = error.status_code
        if status_code in {401, 403, 404, 429, 503}:
            messages = {
                401: "Google 服务账号授权无效，请联系管理员",
                403: "服务账号无权读取某份 Form 的问卷或回答，请共享编辑权限",
                404: "某份 Google Form 不存在或服务账号不可见",
                429: "Google Forms 请求过于频繁，请稍后重试",
                503: "Google Forms 暂时不可用，请稍后重试",
            }
            raise HTTPException(status_code=status_code, detail=messages[status_code]) from error
        raise HTTPException(status_code=502, detail="Google Forms 暂时无法返回可用数据") from error
    if isinstance(error, GoogleFormsFamilyInternalError):
        raise HTTPException(status_code=500, detail="问卷家族服务暂时不可用") from error
    raise HTTPException(status_code=500, detail="问卷家族服务暂时不可用") from error


def create_google_forms_families_router(api: GoogleFormsFamilyApi) -> APIRouter:
    if not isinstance(api, GoogleFormsFamilyApi):
        raise TypeError("api 必须是 GoogleFormsFamilyApi")
    router = APIRouter()

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
            return await api.create_family(owner_ref, payload.title, variants)
        except GoogleFormsLinkError as error:
            raise HTTPException(
                status_code=422,
                detail="请为每种语言填写 /forms/d/.../edit Google Forms 编辑链接",
            ) from error
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
