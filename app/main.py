"""应用入口装配:创建 FastAPI、中间件、静态资源、登录门控、挂载各业务 router。

过渡期(步骤3进行中):尚未迁出的路由仍在 server.py 中通过 `from app.main import app`
注册到这里创建的同一个 app 上。迁移完成后,所有路由都在 app/routers/* 里、由本文件 include。
"""
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import COOKIE_NAME, FEISHU_LOGIN_REQUIRED
from app.core.security import (
    _current_login,
    _forbidden_response,
    _is_public_path,
    _login_allowed,
    _safe_next_path,
    _unauthorized_response,
)
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


# ── 业务路由(随步骤3逐组迁入后在此 include)───────────────────
from app.routers import admin, crosstab, export, feishu, history, settings_api

app.include_router(settings_api.router)
app.include_router(admin.router)
app.include_router(feishu.router)
app.include_router(history.router)
app.include_router(crosstab.router)
app.include_router(export.router)
