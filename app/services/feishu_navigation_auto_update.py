"""Event-driven repair of registered quick-report navigation after Wiki moves."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import logging
import time

from app.core import config
from app.integrations import feishu_client
from app.services.feishu_evidence_navigation import (
    navigation_document_identity,
    prepare_wiki_navigation_updates,
)
from app.storage import feishu_navigation as storage

logger = logging.getLogger(__name__)
_EVENTS = {"drive.file.read_v1", "drive.file.edit_v1", "drive.file.title_updated_v1"}


class NavigationEventUnavailable(RuntimeError):
    pass


def verification_ready() -> bool:
    return bool(
        config.FEISHU_EVENT_VERIFICATION_TOKEN and config.FEISHU_EVENT_ENCRYPT_KEY
        and feishu_client.FEISHU_APP_ID and feishu_client.FEISHU_APP_SECRET
    )


async def register_exported_navigation(doc_token: str, doc_url: str, *, recipient_open_id: str = "", title: str = "") -> bool:
    """Registration adds local metadata only; subscription runs off the export path."""
    if not config.FEISHU_WIKI_AUTO_UPDATE_ENABLED:
        return False
    try:
        if not verification_ready():
            raise NavigationEventUnavailable("event_verification_not_configured")
        parsed_token, canonical = navigation_document_identity(doc_url)
        if parsed_token != doc_token:
            raise ValueError("exported document identity mismatch")
        return await asyncio.to_thread(
            storage.register_document, doc_token, canonical,
            recipient_open_id=recipient_open_id if config.FEISHU_NAVIGATION_NOTIFICATIONS_ENABLED else "",
            title=title,
        )
    except Exception as exc:
        logger.error("Feishu navigation registration failed: %s", type(exc).__name__)
        return False


async def register_existing_document(doc_url: str) -> bool:
    """Explicit admin bootstrap; never discovers/scans the user's other documents."""
    token, canonical = navigation_document_identity(doc_url)
    if not config.FEISHU_WIKI_AUTO_UPDATE_ENABLED or not verification_ready():
        raise NavigationEventUnavailable("automatic_navigation_not_configured")
    return await asyncio.to_thread(storage.register_document, token, canonical)


async def accept_navigation_event(body: bytes, headers: dict) -> dict:
    if not verification_ready():
        raise NavigationEventUnavailable("event_verification_not_configured")
    payload = feishu_client.decode_navigation_event(
        body, headers, verification_token=config.FEISHU_EVENT_VERIFICATION_TOKEN,
        encrypt_key=config.FEISHU_EVENT_ENCRYPT_KEY, app_id=feishu_client.FEISHU_APP_ID,
        max_age_seconds=config.FEISHU_EVENT_MAX_AGE_SECONDS,
    )
    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}
    if not config.FEISHU_WIKI_AUTO_UPDATE_ENABLED:
        return {"code": 0}
    header, event = payload["header"], payload.get("event", {})
    if not isinstance(event, dict):
        raise ValueError("invalid event data")
    if header.get("event_type") not in _EVENTS or event.get("file_type") != "docx":
        return {"code": 0}
    # Persist the event before acknowledging it; unknown documents do no API work.
    await asyncio.to_thread(
        storage.enqueue_event, event.get("file_token"), header.get("event_id"),
        cooldown=config.FEISHU_NAVIGATION_COOLDOWN_SECONDS,
    )
    return {"code": 0}


async def process_navigation_job(job: dict) -> None:
    token = job["doc_token"]
    subscribed, changed = job["subscribed"], 0
    result = {}
    try:
        async with asyncio.timeout(config.FEISHU_NAVIGATION_JOB_TIMEOUT_SECONDS):
            if not subscribed:
                await feishu_client.subscribe_navigation_events(token)
                subscribed = True
            wiki_url = await feishu_client.resolve_navigation_wiki_url(token, job["doc_url"])
            if wiki_url is None:
                result = {"status": "watching", "last_error": ""}
            else:
                await asyncio.to_thread(
                    storage.mark_navigation_started, token, job["claim_id"], wiki_url,
                    notify=config.FEISHU_NAVIGATION_NOTIFICATIONS_ENABLED,
                )
                snapshot = await feishu_client.get_navigation_snapshot(token)
                prepared = prepare_wiki_navigation_updates(
                    snapshot["blocks"], token, job["doc_url"], wiki_url,
                )
                if prepared["updates"]:
                    changed = await feishu_client.update_navigation_blocks(
                        token, snapshot["revision_id"], prepared["updates"],
                    )
                    # Read back real block IDs and link targets before reporting completion.
                    verified = await feishu_client.get_navigation_snapshot(token)
                    remaining = prepare_wiki_navigation_updates(
                        verified["blocks"], token, job["doc_url"], wiki_url,
                    )
                    if remaining["updates"]:
                        raise feishu_client.FeishuNavigationAPIError("verification", "links_remaining")
                    prepared = remaining
                skipped = prepared["skipped_links"]
                result = {
                    "status": "completed_with_skips" if skipped else "completed",
                    "wiki_url": wiki_url, "last_error": "",
                    "skipped_links": skipped,
                }
    except Exception as exc:
        retryable = not isinstance(exc, ValueError)
        retry_after = 0
        if isinstance(exc, feishu_client.FeishuNavigationAPIError):
            changed += exc.updated_blocks
            retryable = exc.retryable
            reason = f"{exc.operation}:{exc.code}"
            retry_after = exc.retry_after
        else:
            reason = type(exc).__name__
        retry = retryable and job["attempts"] < config.FEISHU_NAVIGATION_MAX_ATTEMPTS
        result = {
            "status": "pending" if retry else "failed",
            "next_at": time.time() + max(
                retry_after, config.FEISHU_NAVIGATION_COOLDOWN_SECONDS * job["attempts"],
            ),
            "last_error": reason,
        }
        logger.warning("Feishu navigation job %s: %s", result["status"], reason)
    result.update(subscribed=subscribed, updated_blocks=job["updated_blocks"] + changed)
    await asyncio.to_thread(
        storage.finish_job, token, job["claim_id"], result,
        followup_delay=config.FEISHU_NAVIGATION_COOLDOWN_SECONDS,
        notify=config.FEISHU_NAVIGATION_NOTIFICATIONS_ENABLED,
    )


def _notification_text(job: dict) -> str:
    title = job.get("title") or "调研报告"
    stage = job["stage"]
    if stage == "started":
        text = f"正在检查并更新《{title}》的知识库证据链接。"
    elif stage == "completed":
        text = f"《{title}》的证据和返回链接已更新，可在知识库文档内跳转。"
    elif stage == "completed_with_skips":
        text = f"《{title}》的链接更新部分完成：已处理能确认的链接，还有 {job.get('skipped_links', 0)} 处未处理，请检查文档中的相关链接。"
    elif stage == "failed":
        text = f"《{title}》的证据链接自动更新暂时失败，本轮自动重试已结束。部分链接可能仍是原地址；请检查机器人文档权限，或联系平台维护者查看处理状态。"
    else:
        raise ValueError("invalid notification stage")
    return f"{text}\n文档：{job['url']}"


async def process_navigation_notification(job: dict) -> None:
    sent, retryable, error, retry_after = False, True, "", 0
    try:
        await feishu_client.send_navigation_notification(
            job["recipient_open_id"], _notification_text(job), job["id"],
            timeout=config.FEISHU_NAVIGATION_NOTIFICATION_TIMEOUT_SECONDS,
        )
        sent = True
    except Exception as exc:
        error = type(exc).__name__
        if isinstance(exc, feishu_client.FeishuNavigationAPIError):
            retryable, retry_after = exc.retryable, exc.retry_after
            error = f"{exc.operation}:{exc.code}"
        elif isinstance(exc, ValueError):
            retryable = False
        logger.warning("Feishu navigation notification failed: %s", error)
    await asyncio.to_thread(
        storage.finish_notification, job, sent=sent, retryable=retryable,
        max_attempts=config.FEISHU_NAVIGATION_NOTIFICATION_MAX_ATTEMPTS,
        delay=max(retry_after, config.FEISHU_NAVIGATION_NOTIFICATION_RETRY_SECONDS * job["attempts"]),
        error=error,
    )


async def _notification_worker(stop: asyncio.Event) -> None:
    # Independent from document processing: a slow/failing message never blocks a repair.
    while not stop.is_set():
        try:
            job = await asyncio.to_thread(
                storage.claim_notification,
                lease_seconds=config.FEISHU_NAVIGATION_NOTIFICATION_TIMEOUT_SECONDS + 10,
                max_attempts=config.FEISHU_NAVIGATION_NOTIFICATION_MAX_ATTEMPTS,
                ttl_seconds=config.FEISHU_NAVIGATION_NOTIFICATION_TTL_SECONDS,
            )
            if job:
                await process_navigation_notification(job)
                continue
        except Exception as exc:
            logger.warning("Feishu navigation notification worker: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.FEISHU_NAVIGATION_WORKER_INTERVAL_SECONDS)
        except TimeoutError:
            pass


async def _worker(stop: asyncio.Event) -> None:
    for url in config.FEISHU_NAVIGATION_SEED_DOC_URLS:
        try:
            await register_existing_document(url)
        except Exception as exc:
            logger.error("Feishu navigation seed registration failed: %s", type(exc).__name__)
    while not stop.is_set():
        try:
            job = await asyncio.to_thread(
                storage.claim_job,
                lease_seconds=config.FEISHU_NAVIGATION_JOB_TIMEOUT_SECONDS + 15,
                max_attempts=config.FEISHU_NAVIGATION_MAX_ATTEMPTS,
                notify=config.FEISHU_NAVIGATION_NOTIFICATIONS_ENABLED,
            )
            if job:
                await process_navigation_job(job)
                continue
        except Exception as exc:
            logger.error("Feishu navigation worker failed: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.FEISHU_NAVIGATION_WORKER_INTERVAL_SECONDS)
        except TimeoutError:
            pass


@asynccontextmanager
async def navigation_lifespan(app):
    """A disabled installation performs no registry or network operations."""
    if not config.FEISHU_WIKI_AUTO_UPDATE_ENABLED:
        yield
        return
    if not verification_ready():
        logger.error("Feishu automatic navigation disabled: event verification is not configured")
        yield
        return
    stop = asyncio.Event()
    task = asyncio.create_task(_worker(stop), name="feishu-navigation")
    notifications = (asyncio.create_task(_notification_worker(stop), name="feishu-navigation-notifications")
                     if config.FEISHU_NAVIGATION_NOTIFICATIONS_ENABLED else None)
    try:
        yield
    finally:
        stop.set()
        tasks = [task] + ([notifications] if notifications is not None else [])
        for running in tasks:
            running.cancel()
        for running in tasks:
            with suppress(asyncio.CancelledError):
                await running
