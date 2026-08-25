"""应用入口装配:创建 FastAPI、中间件、静态资源、登录门控、挂载各业务 router。

所有路由均在 app/routers/* 里定义,由本文件 include。
"""
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import COOKIE_NAME, FEISHU_LOGIN_REQUIRED
from app.core.config import (
    GOOGLE_FORMS_API_BASE,
    GOOGLE_FORMS_CONNECT_TIMEOUT,
    GOOGLE_FORMS_READ_TIMEOUT,
    GOOGLE_FORMS_SERVICE_ACCOUNT_ENABLED,
    GOOGLE_FORMS_SERVICE_ACCOUNT_FILE,
    QUESTIONNAIRE_LOCAL_SOURCE_PREVIEW_ENABLED,
    RESEARCH_ASSET_STORAGE_DIR,
)
from app.core.security import _forbidden_response, _is_public_path, _safe_next_path, _unauthorized_response
from app.services.auth import _current_login, _login_allowed
from app.storage.sessions import _sweep_old_sessions

app = FastAPI(title="调研分析平台")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
_sweep_old_sessions()  # 启动时清理过期 session 文件

static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(static_dir, "index.html"))


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(os.path.join(static_dir, "web-icon.jpg"), media_type="image/jpeg")


@app.get("/login")
async def login_page(request: Request, next: str = "/"):
    safe_next = _safe_next_path(next)
    login = await _current_login(request)
    if login and _login_allowed(login):
        return RedirectResponse(safe_next)
    return FileResponse(os.path.join(static_dir, "login.html"))


@app.middleware("http")
async def feishu_auth_middleware(request: Request, call_next):
    if not FEISHU_LOGIN_REQUIRED or _is_public_path(request.url.path):
        return await call_next(request)

    login = await _current_login(request)
    if not login:
        resp = _unauthorized_response(request)
        resp.delete_cookie(COOKIE_NAME)
        return resp
    if not _login_allowed(login):
        return _forbidden_response(request, login)
    return await call_next(request)


# ── 业务路由 ────────────────────────────────────────────────
from app.routers import (
    admin,
    annotate,
    comment_analysis,
    crosstab,
    export,
    feishu,
    history,
    interview,
    settings_api,
    survey,
)

if QUESTIONNAIRE_LOCAL_SOURCE_PREVIEW_ENABLED:
    from app.routers.questionnaire_source_runtime import (
        create_questionnaire_source_runtime_router,
    )
    from app.services.questionnaire_source_runtime import (
        create_questionnaire_source_runtime,
    )

    _google_forms_client = None
    if GOOGLE_FORMS_SERVICE_ACCOUNT_ENABLED:
        if GOOGLE_FORMS_SERVICE_ACCOUNT_FILE is None:
            raise RuntimeError(
                "已启用 Google Forms 服务账号，但未配置凭据文件"
            )
        from app.integrations.google_forms_service_account_client import (
            GoogleFormsServiceAccountClient,
        )

        _google_forms_client = GoogleFormsServiceAccountClient(
            GOOGLE_FORMS_SERVICE_ACCOUNT_FILE,
            forms_api_base=GOOGLE_FORMS_API_BASE,
            connect_timeout=GOOGLE_FORMS_CONNECT_TIMEOUT,
            read_timeout=GOOGLE_FORMS_READ_TIMEOUT,
        )

    _questionnaire_source_runtime = create_questionnaire_source_runtime(
        RESEARCH_ASSET_STORAGE_DIR,
        google_forms_client=_google_forms_client,
    )
    app.include_router(
        create_questionnaire_source_runtime_router(
            _questionnaire_source_runtime,
        ),
    )

app.include_router(survey.router)
app.include_router(settings_api.router)
app.include_router(admin.router)
app.include_router(feishu.router)
app.include_router(history.router)
app.include_router(crosstab.router)
app.include_router(export.router)
app.include_router(comment_analysis.router)
app.include_router(annotate.router)
app.include_router(interview.router)
