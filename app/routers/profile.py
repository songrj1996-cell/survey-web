"""个人中心：当前账号信息与个人 LLM Key 管理。"""
from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas.requests import UserLlmKeyUpdateRequest
from app.services.audit import audit_log
from app.services.auth import _current_login
from app.services.llm_credentials import (
    delete_user_llm_api_key,
    get_user_llm_key_status,
    validate_and_save_user_llm_api_key,
)
from app.services.llm_usage import get_user_llm_usage


router = APIRouter()


async def _profile_login(request: Request) -> dict:
    login = await _current_login(request)
    if not login:
        raise HTTPException(status_code=401, detail="请先登录飞书")
    return login


@router.get("/api/profile")
async def get_profile(request: Request):
    login = await _current_login(request)
    status = get_user_llm_key_status(login)
    return {
        "name": str((login or {}).get("name") or ""),
        "email": str((login or {}).get("email") or ""),
        **status,
    }


@router.get("/api/profile/llm-usage")
async def get_profile_llm_usage(
    request: Request,
    period: str = Query("30d", pattern="^(7d|30d|all)$"),
    category: str = Query("", pattern="^(|survey|comment|interview|annotate|other)$"),
    status: str = Query("", pattern="^(|running|completed|failed|cancelled)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    login = await _profile_login(request)
    return get_user_llm_usage(
        login,
        period=period,
        category=category,
        status=status,
        offset=offset,
        limit=limit,
    )


@router.put("/api/profile/llm-key")
async def put_profile_llm_key(
    body: UserLlmKeyUpdateRequest,
    request: Request,
):
    login = await _profile_login(request)
    result = await validate_and_save_user_llm_api_key(login, body.api_key)
    await audit_log(
        request,
        "auth",
        "配置个人 LLM Key",
        "用户验证并保存了个人 LLM Key",
    )
    return {"ok": True, **result}


@router.delete("/api/profile/llm-key")
async def delete_profile_llm_key(request: Request):
    login = await _profile_login(request)
    deleted = delete_user_llm_api_key(login)
    await audit_log(
        request,
        "auth",
        "删除个人 LLM Key",
        "用户删除了个人 LLM Key",
    )
    return {"ok": True, "deleted": deleted, "configured": False}
