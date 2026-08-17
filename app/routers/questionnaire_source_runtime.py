"""本地问卷来源运行时的聚合 HTTP 路由。"""

from fastapi import APIRouter, HTTPException, Request

from app.core.security import _owner_from_login
from app.routers.questionnaire_pdf_materials import (
    create_questionnaire_pdf_material_sources_router,
)
from app.routers.questionnaire_sources import (
    create_bested_questionnaire_sources_router,
    create_questionnaire_material_sources_router,
    create_questionnaire_sources_router,
)
from app.routers.questionnaire_snapshot_analysis import (
    create_questionnaire_snapshot_analysis_router,
)
from app.schemas.questionnaire_source_runtime import (
    QuestionnaireSourceCapabilities,
)
from app.services.auth import _require_feature
from app.services.questionnaire_source_runtime import QuestionnaireSourceRuntime


def _owner_key(login: dict | None) -> str:
    owner = str(_owner_from_login(login)["owner_key"]).strip()
    if not owner:
        raise HTTPException(status_code=401, detail="请先登录飞书")
    return owner


def create_questionnaire_source_runtime_router(
    runtime: QuestionnaireSourceRuntime,
) -> APIRouter:
    """聚合显式注入运行时支持的本地问卷来源接口。"""
    if not isinstance(runtime, QuestionnaireSourceRuntime):
        raise TypeError("runtime 必须是 QuestionnaireSourceRuntime")

    router = APIRouter()
    router.include_router(create_questionnaire_sources_router(
        runtime.snapshot_api,
    ))
    router.include_router(create_questionnaire_snapshot_analysis_router(
        runtime.snapshot_analysis_api,
    ))
    router.include_router(create_bested_questionnaire_sources_router(
        runtime.bested_api,
    ))
    router.include_router(create_questionnaire_material_sources_router(
        runtime.screenshot_material_api,
    ))
    router.include_router(create_questionnaire_pdf_material_sources_router(
        runtime.pdf_material_api,
    ))

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
