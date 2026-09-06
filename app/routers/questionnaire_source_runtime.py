"""Aggregate exactly the capability and five Google family routes."""

from fastapi import APIRouter, HTTPException, Request

from app.core.security import _owner_from_login
from app.routers.google_forms_families import create_google_forms_families_router
from app.schemas.questionnaire_source_runtime import QuestionnaireSourceCapabilities
from app.services.auth import _require_feature
from app.services.questionnaire_source_runtime import QuestionnaireSourceRuntime


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


def create_questionnaire_source_runtime_router(
    runtime: QuestionnaireSourceRuntime,
) -> APIRouter:
    if not isinstance(runtime, QuestionnaireSourceRuntime):
        raise TypeError("runtime 必须是 QuestionnaireSourceRuntime")

    router = APIRouter()
    router.include_router(
        create_google_forms_families_router(runtime.google_forms_family_api)
    )

    @router.get(
        "/api/questionnaire-sources/capabilities",
        response_model=QuestionnaireSourceCapabilities,
    )
    async def get_questionnaire_source_capabilities(
        request: Request,
    ) -> QuestionnaireSourceCapabilities:
        login = await _require_feature(request, "survey")
        _owner_key(login)
        return runtime.capabilities

    return router


__all__ = ["create_questionnaire_source_runtime_router"]
