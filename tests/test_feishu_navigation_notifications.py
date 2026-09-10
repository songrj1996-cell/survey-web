"""Isolated progress-message acceptance; no real messages or document writes."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1] / ".test-tmp"
ROOT.mkdir(exist_ok=True)
os.environ.setdefault("DATA_DIR", str(ROOT / "notification-data"))
os.environ.setdefault("RESEARCH_ASSET_STORAGE_DIR", str(ROOT / "notification-assets"))

import httpx
from app.core import config
from app.integrations import feishu_client as feishu
from app.services import feishu_navigation_auto_update as service
from app.storage import feishu_navigation as storage
from tests.test_feishu_evidence_navigation import sample_report, FULL, apply_block_updates
from tests.test_feishu_wiki_navigation_auto_update import live_blocks, signed_event, DOC, ORIGIN, WIKI, TOKEN, KEY, APP

REAL_CLIENT = httpx.AsyncClient


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        settings = [patch.object(storage, "FEISHU_NAVIGATION_DATA_DIR", Path(self.temp.name)),
                    patch.object(config, "FEISHU_WIKI_AUTO_UPDATE_ENABLED", True),
                    patch.object(config, "FEISHU_NAVIGATION_NOTIFICATIONS_ENABLED", True),
                    patch.object(config, "FEISHU_EVENT_VERIFICATION_TOKEN", TOKEN),
                    patch.object(config, "FEISHU_EVENT_ENCRYPT_KEY", KEY),
                    patch.object(feishu, "FEISHU_APP_ID", APP),
                    patch.object(feishu, "FEISHU_APP_SECRET", "synthetic-secret"),
                    patch.object(feishu, "_app_access_token", AsyncMock(return_value="synthetic-access-token"))]
        for setting in settings:
            setting.start()
            self.addCleanup(setting.stop)

    def register(self, recipient="ou_exporter", *, now=None):
        return storage.register_document(DOC, ORIGIN, recipient_open_id=recipient, title="测试报告", now=now)

    def claim(self, *, now=None):
        return storage.claim_job(lease_seconds=60, max_attempts=3, notify=True, now=now)

    def claim_notice(self, *, now=None):
        return storage.claim_notification(lease_seconds=15, max_attempts=3, ttl_seconds=86400, now=now)

    def started(self, *, now=None):
        self.register(now=now)
        job = self.claim(now=now)
        storage.mark_navigation_started(DOC, job["claim_id"], WIKI, notify=True, now=now)
        return job

    def finish(self, job, status="completed", *, now=None):
        storage.finish_job(DOC, job["claim_id"], {"status": status, "wiki_url": WIKI, "skipped_links": 2}, notify=True, now=now)

    async def repair(self, job, *, partial=False):
        blocks = live_blocks()
        if partial:
            source = next(block for block in blocks if "ordered" in block)
            run = next(e["text_run"] for e in source["ordered"]["elements"] if e["text_run"].get("text_element_style", {}).get("link"))
            run["text_element_style"]["link"]["url"] = ORIGIN + "#missing"
        async def snapshot(*args):
            return {"revision_id": 4, "blocks": deepcopy(blocks)}
        async def update(token, revision, updates):
            apply_block_updates(blocks, updates)
            return len(updates)
        with patch.object(feishu, "subscribe_navigation_events", AsyncMock()), \
                patch.object(feishu, "resolve_navigation_wiki_url", AsyncMock(return_value=WIKI)), \
                patch.object(feishu, "get_navigation_snapshot", side_effect=snapshot), \
                patch.object(feishu, "update_navigation_blocks", side_effect=update):
            await service.process_navigation_job(job)

    async def test_export_registers_authenticated_exporter_and_title_only_for_quick_report(self):
        from app.services import export_service
        for report in (sample_report(), FULL):
            with patch.object(feishu, "create_doc_via_bot", AsyncMock(return_value=(ORIGIN, DOC, "docx"))), \
                    patch.object(feishu, "send_message_to_user", AsyncMock()), \
                    patch.object(export_service, "register_exported_navigation", AsyncMock()) as register:
                await export_service._export_to_feishu(report, {"open_id": "ou_exporter"}, title="测试报告")
                if report == FULL:
                    register.assert_not_awaited()
                else:
                    register.assert_awaited_once_with(DOC, ORIGIN, recipient_open_id="ou_exporter", title="测试报告")

    async def test_duplicate_registration_and_read_event_cannot_change_recipient(self):
        self.register()
        self.assertFalse(storage.register_document(DOC, ORIGIN, recipient_open_id="ou_reader", title="other"))
        job = self.claim()
        storage.finish_job(DOC, job["claim_id"], {"status": "watching"})
        body, headers = signed_event()
        await service.accept_navigation_event(body, headers)
        record = storage.list_records()[0]
        self.assertEqual((record["recipient_open_id"], record["title"]), ("ou_exporter", "测试报告"))

    async def test_seed_and_old_record_never_guess_a_recipient(self):
        await service.register_existing_document(ORIGIN)
        with storage._registry() as documents:
            documents[DOC].pop("notifications")
            documents[DOC].pop("recipient_open_id")
        job = self.claim()
        await self.repair(job)
        self.assertEqual(storage.list_records()[0]["status"], "completed")
        self.assertIsNone(self.claim_notice())

    async def test_no_started_notice_until_wiki_is_resolved(self):
        self.register()
        with patch.object(feishu, "subscribe_navigation_events", AsyncMock()), \
                patch.object(feishu, "resolve_navigation_wiki_url", AsyncMock(return_value=None)):
            await service.process_navigation_job(self.claim())
        self.assertEqual(storage.list_records()[0]["status"], "watching")
        self.assertIsNone(self.claim_notice())

    async def test_fast_completion_coalesces_stale_start_and_sends_one_result(self):
        self.register()
        await self.repair(self.claim())
        job = self.claim_notice()
        self.assertEqual(job["stage"], "completed")
        sender = AsyncMock(return_value="om_result")
        with patch.object(feishu, "send_navigation_notification", sender):
            await service.process_navigation_notification(job)
        args = sender.await_args.args
        self.assertEqual(args[0], "ou_exporter")
        self.assertIn(WIKI, args[1])
        self.assertIn("已更新", args[1])
        self.assertIsNone(self.claim_notice())
        self.assertFalse(storage.enqueue_event(DOC, "self-update", cooldown=30))

    async def test_start_then_result_order_and_slow_message_does_not_block_repair(self):
        repair_job = self.started()
        started_notice = self.claim_notice()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return "om_start"
        with patch.object(feishu, "send_navigation_notification", side_effect=slow):
            delivery = asyncio.create_task(service.process_navigation_notification(started_notice))
            try:
                await entered.wait()
                await asyncio.wait_for(self.repair(repair_job), timeout=1)
                self.assertEqual(storage.list_records()[0]["status"], "completed")
                self.assertIsNone(self.claim_notice())  # Another process cannot overtake in-flight start.
            finally:
                release.set()
                await delivery
        result = self.claim_notice()
        self.assertEqual(result["stage"], "completed")
        self.assertIn("正在", service._notification_text(started_notice))

    async def test_partial_completion_has_honest_result_and_link(self):
        self.register()
        await self.repair(self.claim(), partial=True)
        notice = self.claim_notice()
        self.assertEqual(notice["stage"], "completed_with_skips")
        self.assertGreater(notice["skipped_links"], 0)
        self.assertIn("部分完成", service._notification_text(notice))
        self.assertIn(WIKI, service._notification_text(notice))

    async def test_failed_repair_notifies_after_budget_and_later_success_supersedes_pending_failure(self):
        self.register()
        with patch.object(feishu, "subscribe_navigation_events", AsyncMock(side_effect=TimeoutError)):
            for attempt in range(3):
                await service.process_navigation_job(self.claim(now=time.time() + 100 * attempt))
                if attempt < 2:
                    self.assertIsNone(self.claim_notice())
        notice = self.claim_notice()
        self.assertEqual(notice["stage"], "failed")
        self.assertIn("暂时失败", service._notification_text(notice))
        storage.finish_notification(notice, sent=False, retryable=True, max_attempts=3, delay=30)
        storage.enqueue_event(DOC, "permission-restored", cooldown=30, now=time.time() + 500)
        await self.repair(self.claim(now=time.time() + 501))
        self.assertEqual(self.claim_notice()["stage"], "completed")

    async def test_message_retry_preserves_uuid_and_respects_retry_after(self):
        self.finish(self.started())
        first = self.claim_notice()
        error = feishu.FeishuNavigationAPIError("request", 99991400, retry_after=120)
        with patch.object(feishu, "send_navigation_notification", AsyncMock(side_effect=error)):
            await service.process_navigation_notification(first)
        notice = storage.list_records()[0]["notifications"]["completed"]
        self.assertGreater(notice["next_at"], time.time() + 115)
        second = self.claim_notice(now=notice["next_at"] + 1)
        self.assertEqual(second["id"], first["id"])
        with patch.object(feishu, "send_navigation_notification", AsyncMock(return_value="om_done")):
            await service.process_navigation_notification(second)
        self.assertIsNone(self.claim_notice(now=notice["next_at"] + 10))

    async def test_message_permission_failure_does_not_change_completed_repair(self):
        self.finish(self.started())
        notice = self.claim_notice()
        with patch.object(feishu, "send_navigation_notification", AsyncMock(side_effect=feishu.FeishuNavigationAPIError("request", 403, retryable=False))):
            await service.process_navigation_notification(notice)
        record = storage.list_records()[0]
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["notifications"]["completed"]["state"], "failed")

    def test_notification_claim_restart_expiry_stale_claim_budget_and_ttl(self):
        self.finish(self.started(now=100), now=101)
        first = self.claim_notice(now=101)
        self.assertIsNone(self.claim_notice(now=102))
        second = self.claim_notice(now=117)
        self.assertEqual(second["id"], first["id"])
        self.assertFalse(storage.finish_notification(first, sent=True, retryable=False, max_attempts=3, delay=0))
        self.claim_notice(now=133)
        self.assertIsNone(self.claim_notice(now=150))
        self.assertEqual(storage.list_records()[0]["notifications"]["completed"]["state"], "failed")

    def test_expired_unsent_notification_and_crashed_repair_have_bounded_behavior(self):
        self.started(now=100)
        self.assertIsNone(self.claim_notice(now=100 + 86401))
        self.claim(now=161)
        self.claim(now=222)
        self.assertIsNone(self.claim(now=283))
        self.assertEqual(self.claim_notice(now=284)["stage"], "failed")

    async def test_notification_switch_off_keeps_repair_and_starts_no_notification_worker(self):
        with patch.object(config, "FEISHU_NAVIGATION_NOTIFICATIONS_ENABLED", False):
            await service.register_exported_navigation(DOC, ORIGIN, recipient_open_id="ou_exporter", title="test")
            await self.repair(self.claim())
            self.assertIsNone(self.claim_notice())
            async def wait(stop):
                await stop.wait()
            with patch.object(service, "_worker", side_effect=wait), patch.object(service, "_notification_worker", AsyncMock()) as worker:
                async with service.navigation_lifespan(None):
                    await asyncio.sleep(0)
                worker.assert_not_awaited()

    async def test_checked_message_api_uuid_private_recipient_and_error_handling(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_notice"}})
        with patch.object(feishu.httpx, "AsyncClient", side_effect=lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler))):
            for _ in range(2):
                result = await feishu.send_navigation_notification("ou_exporter", "测试", "a" * 32, timeout=1)
                self.assertEqual(result, "om_notice")
        self.assertEqual(json.loads(calls[0].content), json.loads(calls[1].content))
        payload = json.loads(calls[0].content)
        self.assertEqual((payload["uuid"], payload["receive_id"]), ("a" * 32, "ou_exporter"))
        self.assertEqual(calls[0].url.params["receive_id_type"], "open_id")
        for response in (httpx.Response(403, json={"code": 99991679}), httpx.Response(200, json={"code": 0, "data": {}})):
            with patch.object(feishu.httpx, "AsyncClient", side_effect=lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(lambda request: response))):
                with self.assertRaises(feishu.FeishuNavigationAPIError):
                    await feishu.send_navigation_notification("ou_exporter", "test", "a" * 32, timeout=1)
        with self.assertRaises(ValueError):
            await feishu.send_navigation_notification("oc_group", "test", "a" * 32, timeout=1)

    async def test_message_api_total_timeout_includes_authentication(self):
        async def slow():
            await asyncio.sleep(10)
        with patch.object(feishu, "_app_access_token", side_effect=slow):
            with self.assertRaises(TimeoutError):
                await feishu.send_navigation_notification("ou_exporter", "test", "a" * 32, timeout=0.02)
