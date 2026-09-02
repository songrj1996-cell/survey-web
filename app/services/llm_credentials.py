"""飞书用户个人 LLM Key 的验证、加密保存与请求绑定。"""
from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request

from app.core.config import (
    FEISHU_LOGIN_REQUIRED,
    LLM_API_KEY,
    LLM_COLUMN_MODEL,
    USER_LLM_KEY_ENCRYPTION_KEY,
)
from app.core.llm_context import bind_llm_api_key, bind_llm_attempt_observer
from app.integrations.llm_client import collect_chat_completion
from app.services.auth import _current_login
from app.services.llm_usage import LLMUsageRecorder, start_llm_usage_task
from app.storage.llm_credentials import (
    load_llm_credentials,
    mutate_llm_credentials,
)


def _owner_id(login: dict | None) -> str:
    login = login or {}
    open_id = str(login.get("open_id") or "").strip()
    email = str(login.get("email") or "").strip().lower()
    identity = f"open_id:{open_id}" if open_id else (f"email:{email}" if email else "")
    if not identity:
        raise HTTPException(status_code=401, detail="请先登录飞书")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _fernet() -> Fernet:
    if not USER_LLM_KEY_ENCRYPTION_KEY:
        raise HTTPException(
            status_code=503,
            detail="服务端尚未配置个人 LLM Key 加密主密钥，请联系管理员",
        )
    try:
        return Fernet(USER_LLM_KEY_ENCRYPTION_KEY.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="服务端个人 LLM Key 加密主密钥格式无效，请联系管理员",
        ) from exc


def _decrypt_record(record: dict) -> str:
    token = str(record.get("ciphertext") or "").encode("ascii", errors="ignore")
    if not token:
        return ""
    try:
        return _fernet().decrypt(token).decode("utf-8").strip()
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="个人 LLM Key 无法解密，请联系管理员检查加密主密钥",
        ) from exc


def get_user_llm_key_status(login: dict | None) -> dict:
    if not FEISHU_LOGIN_REQUIRED and not login:
        return {
            "required": False,
            "personal_key_supported": False,
            "configured": bool(LLM_API_KEY),
            "storage_ready": bool(USER_LLM_KEY_ENCRYPTION_KEY),
            "updated_at": "",
        }
    owner_id = _owner_id(login)
    record = load_llm_credentials().get(owner_id)
    configured = False
    storage_ready = bool(USER_LLM_KEY_ENCRYPTION_KEY)
    if record and storage_ready:
        try:
            configured = bool(_decrypt_record(record))
        except HTTPException:
            storage_ready = False
    return {
        "required": FEISHU_LOGIN_REQUIRED,
        "personal_key_supported": True,
        "configured": configured,
        "storage_ready": storage_ready,
        "updated_at": str((record or {}).get("updated_at") or ""),
    }


def get_user_llm_api_key(login: dict | None) -> str:
    owner_id = _owner_id(login)
    record = load_llm_credentials().get(owner_id)
    api_key = _decrypt_record(record) if record else ""
    if not api_key:
        raise HTTPException(
            status_code=428,
            detail={
                "code": "USER_LLM_KEY_REQUIRED",
                "message": "请先在个人中心填写 LLM API Key",
            },
        )
    return api_key


async def require_request_llm_api_key(request: Request) -> str:
    login = await _current_login(request)
    if not FEISHU_LOGIN_REQUIRED:
        if login:
            owner_id = _owner_id(login)
            record = load_llm_credentials().get(owner_id)
            if record:
                personal_key = _decrypt_record(record)
                if personal_key:
                    return personal_key
        if LLM_API_KEY:
            return LLM_API_KEY
        raise HTTPException(status_code=500, detail="本地开发环境未配置 LLM_API_KEY")
    return get_user_llm_api_key(login)


async def validate_and_save_user_llm_api_key(
    login: dict | None,
    api_key: str,
) -> dict:
    key = str(api_key or "").strip()
    if len(key) < 8:
        raise HTTPException(status_code=422, detail="LLM API Key 格式无效")
    cipher = _fernet()
    recorder = None
    try:
        recorder = start_llm_usage_task(
            login,
            category="other",
            action="API Key 连接验证",
        )
    except Exception:
        recorder = None
    try:
        with bind_llm_attempt_observer(
            recorder.on_attempt_event if recorder else None
        ):
            await collect_chat_completion(
                [{"role": "user", "content": "连接验证：请只回复 OK。"}],
                models=(LLM_COLUMN_MODEL,),
                max_tokens=1024,
                api_key=key,
            )
    except Exception as exc:
        _finish_usage_recorder(recorder, "failed")
        message = str(exc).strip() or type(exc).__name__
        message = message.replace(key, "***")
        raise HTTPException(
            status_code=400,
            detail=f"LLM API Key 验证失败：{message[:500]}",
        ) from exc
    _finish_usage_recorder(recorder, "completed")

    owner_id = _owner_id(login)
    updated_at = datetime.now(timezone.utc).isoformat()
    ciphertext = cipher.encrypt(key.encode("utf-8")).decode("ascii")

    def _save(records: dict[str, dict]) -> None:
        records[owner_id] = {
            "version": 1,
            "ciphertext": ciphertext,
            "updated_at": updated_at,
        }

    mutate_llm_credentials(_save)
    return {"configured": True, "updated_at": updated_at}


def delete_user_llm_api_key(login: dict | None) -> bool:
    owner_id = _owner_id(login)
    deleted = False

    def _delete(records: dict[str, dict]) -> None:
        nonlocal deleted
        deleted = records.pop(owner_id, None) is not None

    mutate_llm_credentials(_delete)
    return deleted


async def stream_with_llm_api_key(
    stream: AsyncIterator[str],
    api_key: str,
    *,
    request: Request | None = None,
    category: str = "other",
    action: str = "AI 任务",
    reference_id: str = "",
    title: str = "",
    history_id: str = "",
) -> AsyncIterator[str]:
    recorder = await _usage_recorder_for_request(
        request,
        category=category,
        action=action,
        reference_id=reference_id,
        title=title,
        history_id=history_id,
    )
    try:
        with bind_llm_api_key(api_key), bind_llm_attempt_observer(
            recorder.on_attempt_event if recorder else None
        ):
            async for chunk in stream:
                yield chunk
    except (asyncio.CancelledError, GeneratorExit):
        _finish_usage_recorder(recorder, "cancelled")
        raise
    except BaseException:
        _finish_usage_recorder(recorder, "failed")
        raise
    else:
        _finish_usage_recorder(recorder, "completed")


async def run_with_llm_api_key(
    awaitable,
    api_key: str,
    *,
    request: Request | None = None,
    category: str = "other",
    action: str = "AI 任务",
    reference_id: str = "",
    title: str = "",
    history_id: str = "",
):
    recorder = await _usage_recorder_for_request(
        request,
        category=category,
        action=action,
        reference_id=reference_id,
        title=title,
        history_id=history_id,
    )
    try:
        with bind_llm_api_key(api_key), bind_llm_attempt_observer(
            recorder.on_attempt_event if recorder else None
        ):
            result = await awaitable
    except asyncio.CancelledError:
        _finish_usage_recorder(recorder, "cancelled")
        raise
    except BaseException:
        _finish_usage_recorder(recorder, "failed")
        raise
    _finish_usage_recorder(recorder, "completed")
    return result


async def _usage_recorder_for_request(
    request: Request | None,
    *,
    category: str,
    action: str,
    reference_id: str,
    title: str,
    history_id: str,
) -> LLMUsageRecorder | None:
    if request is None:
        return None
    try:
        login = await _current_login(request)
        return start_llm_usage_task(
            login,
            category=category,
            action=action,
            reference_id=reference_id,
            title=title,
            history_id=history_id,
        )
    except Exception:
        # Usage instrumentation must never prevent an otherwise valid AI task.
        return None


def _finish_usage_recorder(
    recorder: LLMUsageRecorder | None,
    status: str,
) -> None:
    if recorder is None:
        return
    try:
        recorder.finish(status)
    except Exception:
        # Usage persistence must not replace the task's real success/failure result.
        return
