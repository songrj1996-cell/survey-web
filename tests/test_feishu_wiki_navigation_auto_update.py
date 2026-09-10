"""Offline event/queue/API acceptance; all filesystem writes are isolated."""
import asyncio
import base64
from copy import deepcopy
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import quote, unquote

TEST_ROOT = Path(__file__).resolve().parents[1] / ".test-tmp"
TEST_ROOT.mkdir(exist_ok=True)
os.environ.setdefault("DATA_DIR", str(TEST_ROOT / "default-data"))

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import FastAPI

from app.core import config
from app.integrations import feishu_client as feishu
from app.routers import feishu as routes
from app.services import feishu_navigation_auto_update as service
from app.services.feishu_evidence_navigation import prepare_wiki_navigation_updates
from app.storage import feishu_navigation as storage
from tests.test_feishu_evidence_navigation import DOC, wiki_fixture, apply_block_updates, sample_report, FULL

ORIGIN = f"https://tenant.feishu.cn/docx/{DOC}"
WIKI = "https://tenant.feishu.cn/wiki/wiki-test"
APP = "cli_test"
KEY = "synthetic-encrypt-key"
TOKEN = "synthetic-verification-token"
REAL_CLIENT = httpx.AsyncClient


def signed_event(*, event_id="event-1", doc=DOC, encrypted=False, event_type="drive.file.read_v1"):
    timestamp = str(int(time.time()))
    payload = {
        "schema": "2.0",
        "header": {"event_id": event_id, "event_type": event_type, "app_id": APP, "token": TOKEN},
        "event": {"file_type": "docx", "file_token": doc},
    }
    body = json.dumps(payload).encode()
    if encrypted:
        padder = padding.PKCS7(128).padder()
        padded = padder.update(body) + padder.finalize()
        iv = b"0123456789abcdef"
        encryptor = Cipher(algorithms.AES(hashlib.sha256(KEY.encode()).digest()), modes.CBC(iv)).encryptor()
        body = json.dumps({"encrypt": base64.b64encode(iv + encryptor.update(padded) + encryptor.finalize()).decode()}).encode()
    signature = hashlib.sha256((timestamp + "nonce" + KEY).encode() + body).hexdigest()
    return body, {"x-lark-request-timestamp": timestamp, "x-lark-request-nonce": "nonce", "x-lark-signature": signature}


def live_blocks():
    blocks = wiki_fixture()
    for block in blocks:
        for kind in ("text", "bullet", "ordered"):
            for element in block.get(kind, {}).get("elements", []):
                link = element["text_run"].get("text_element_style", {}).get("link")
                if link:
                    link["url"] = quote(unquote(link["url"]).replace(
                        "https://example.invalid/docx/doc-test?from=export", ORIGIN,
                    ), safe="")
    return blocks


def claim_in_process(directory, queue):
    storage.FEISHU_NAVIGATION_DATA_DIR = Path(directory)
    job = storage.claim_job(lease_seconds=60, max_attempts=3, now=100)
    queue.put(job["claim_id"] if job else None)


class EventProtocolTests(unittest.TestCase):
    def decode(self, body, headers, **kwargs):
        return feishu.decode_navigation_event(
            body, headers, verification_token=TOKEN, encrypt_key=KEY,
            app_id=APP, max_age_seconds=300, **kwargs,
        )

    def test_plain_and_encrypted_events(self):
        for encrypted in (False, True):
            body, headers = signed_event(encrypted=encrypted)
            self.assertEqual(self.decode(body, headers)["event"]["file_token"], DOC)

    def test_tampering_expiry_missing_token_and_wrong_application_fail(self):
        body, headers = signed_event()
        for candidate, supplied, now in (
            (body + b" ", headers, None),
            (body, {}, None),
            (body, headers, time.time() + 1000),
            (body.replace(TOKEN.encode(), b"incorrect"), headers, None),
            (body.replace(APP.encode(), b"foreign"), headers, None),
            (b'{"encrypt":"not base64"}', headers, None),
            (b"[]", headers, None),
        ):
            with self.assertRaises(feishu.FeishuEventValidationError):
                self.decode(candidate, supplied, now=now)

    def test_challenge_requires_verification_token(self):
        body = json.dumps({"type": "url_verification", "token": TOKEN, "challenge": "test-challenge"}).encode()
        self.assertEqual(self.decode(body, {})["challenge"], "test-challenge")
        with self.assertRaises(feishu.FeishuEventValidationError):
            self.decode(body.replace(TOKEN.encode(), b"wrong"), {})


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.directory.cleanup)
        self.patch = patch.object(storage, "FEISHU_NAVIGATION_DATA_DIR", Path(self.directory.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_duplicate_event_restart_and_expired_claim(self):
        self.assertTrue(storage.register_document(DOC, ORIGIN, now=100))
        self.assertFalse(storage.register_document(DOC, ORIGIN, now=100))
        first = storage.claim_job(lease_seconds=60, max_attempts=3, now=100)
        self.assertIsNone(storage.claim_job(lease_seconds=60, max_attempts=3, now=120))
        # The next process reads disk, not a process-local queue.
        recovered = storage.claim_job(lease_seconds=60, max_attempts=3, now=161)
        self.assertNotEqual(first["claim_id"], recovered["claim_id"])
        self.assertFalse(storage.finish_job(DOC, first["claim_id"], {"status": "completed"}))
        self.assertTrue(storage.finish_job(DOC, recovered["claim_id"], {"status": "watching"}, now=162))
        self.assertTrue(storage.enqueue_event(DOC, "event-1", cooldown=30, now=163))
        self.assertFalse(storage.enqueue_event(DOC, "event-1", cooldown=30, now=200))
        self.assertIsNone(storage.claim_job(lease_seconds=60, max_attempts=3, now=180))
        self.assertIsNotNone(storage.claim_job(lease_seconds=60, max_attempts=3, now=192))

    def test_retry_budget_does_not_reset_during_own_write_events(self):
        storage.register_document(DOC, ORIGIN, now=100)
        job = storage.claim_job(lease_seconds=60, max_attempts=3, now=100)
        for index in range(20):
            self.assertFalse(storage.enqueue_event(DOC, f"own-{index}", cooldown=30, now=101))
        self.assertEqual(storage.list_records()[0]["attempts"], 1)
        storage.finish_job(DOC, job["claim_id"], {"status": "completed"}, now=102)
        self.assertFalse(storage.enqueue_event(DOC, "late-own", cooldown=30, now=200))

    def test_crashed_tasks_exhaust_budget_instead_of_retrying_forever(self):
        storage.register_document(DOC, ORIGIN, now=100)
        for now in (100, 161, 222):
            self.assertIsNotNone(storage.claim_job(lease_seconds=60, max_attempts=3, now=now))
        self.assertIsNone(storage.claim_job(lease_seconds=60, max_attempts=3, now=283))
        self.assertEqual(storage.list_records()[0]["status"], "failed")

    def test_move_event_racing_lookup_schedules_one_followup(self):
        storage.register_document(DOC, ORIGIN, now=100)
        job = storage.claim_job(lease_seconds=60, max_attempts=3, now=100)
        storage.enqueue_event(DOC, "move-race", cooldown=30, now=101)
        storage.finish_job(DOC, job["claim_id"], {"status": "watching"}, now=102, followup_delay=30)
        self.assertIsNone(storage.claim_job(lease_seconds=60, max_attempts=3, now=120))
        followup = storage.claim_job(lease_seconds=60, max_attempts=3, now=130)
        self.assertEqual(followup["attempts"], 1)
        storage.finish_job(DOC, followup["claim_id"], {"status": "watching"}, now=131)
        self.assertIsNone(storage.claim_job(lease_seconds=60, max_attempts=3, now=200))

    def test_unknown_document_and_corrupt_registry_do_not_modify_records(self):
        self.assertFalse(storage.enqueue_event("unknown", "event-1", cooldown=30))
        storage.register_document(DOC, ORIGIN)
        path = Path(self.directory.name) / "registry.json"
        path.write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            storage.register_document("other", ORIGIN)
        self.assertEqual(path.read_text(encoding="utf-8"), "broken")

    def test_two_processes_cannot_claim_the_same_job(self):
        storage.register_document(DOC, ORIGIN, now=100)
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        workers = [context.Process(target=claim_in_process, args=(self.directory.name, queue)) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(15)
            if worker.is_alive():
                worker.terminate()
                worker.join()
                self.fail("isolated process-lock test timed out")
            self.assertEqual(worker.exitcode, 0)
        results = [queue.get(timeout=2) for _ in workers]
        queue.close()
        queue.join_thread()
        self.assertEqual(sum(item is not None for item in results), 1)


class AutoUpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.directory.cleanup)
        self.patches = [
            patch.object(storage, "FEISHU_NAVIGATION_DATA_DIR", Path(self.directory.name)),
            patch.object(config, "FEISHU_WIKI_AUTO_UPDATE_ENABLED", True),
            patch.object(config, "FEISHU_EVENT_ENCRYPT_KEY", KEY),
            patch.object(config, "FEISHU_EVENT_VERIFICATION_TOKEN", TOKEN),
            patch.object(feishu, "FEISHU_APP_ID", APP),
            patch.object(feishu, "FEISHU_APP_SECRET", "synthetic-secret"),
            patch.object(feishu, "_app_access_token", AsyncMock(return_value="synthetic-app-token")),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def claim(self, now=None):
        return storage.claim_job(lease_seconds=60, max_attempts=3, now=now)

    async def test_export_registration_is_local_and_full_report_is_not_registered(self):
        from app.services import export_service
        for report in (sample_report(), FULL):
            with patch.object(feishu, "create_doc_via_bot", AsyncMock(return_value=(ORIGIN, DOC, "docx"))), \
                    patch.object(export_service, "register_exported_navigation", AsyncMock()) as register:
                await export_service._export_to_feishu(report, {})
                self.assertEqual(register.await_count, int(report != FULL))
        with patch.object(feishu, "subscribe_navigation_events", AsyncMock()) as subscribe:
            self.assertTrue(await service.register_exported_navigation(DOC, ORIGIN))
            subscribe.assert_not_awaited()
        self.assertFalse(await service.register_exported_navigation("different", ORIGIN))

    async def test_end_to_end_registration_event_wiki_move_and_readback(self):
        state = {"wiki": False, "subscribed": False, "revision": 4, "blocks": live_blocks()}
        calls = []
        def handler(request):
            calls.append((request.method, request.url.path))
            path = request.url.path
            if path.endswith("get_subscribe"):
                data = {"is_subscribe": state["subscribed"]}
            elif path.endswith("/subscribe"):
                state["subscribed"] = True
                data = {}
            elif path.endswith("get_node"):
                if not state["wiki"]:
                    return httpx.Response(200, json={"code": 131014})
                data = {"node": {"node_token": "wiki-test", "obj_token": DOC, "obj_type": "docx"}}
            elif request.method == "PATCH":
                self.assertEqual(int(request.url.params["document_revision_id"]), state["revision"])
                self.assertTrue(request.url.params["client_token"])
                apply_block_updates(state["blocks"], json.loads(request.content)["requests"])
                state["revision"] += 1
                data = {"document_revision_id": state["revision"]}
            elif path.endswith("/blocks"):
                self.assertEqual(int(request.url.params["document_revision_id"]), state["revision"])
                data = {"items": deepcopy(state["blocks"]), "has_more": False}
            else:
                data = {"document": {"document_id": DOC, "revision_id": state["revision"]}}
            return httpx.Response(200, json={"code": 0, "data": data})
        with patch.object(feishu.httpx, "AsyncClient", side_effect=lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler))):
            await service.register_existing_document(ORIGIN)
            await service.process_navigation_job(await self.claim())
            self.assertEqual(storage.list_records()[0]["status"], "watching")
            state["wiki"] = True
            body, headers = signed_event(encrypted=True)
            self.assertEqual(await service.accept_navigation_event(body, headers), {"code": 0})
            await service.process_navigation_job(await self.claim(now=time.time() + 31))
            record = storage.list_records()[0]
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["wiki_url"], WIKI)
            self.assertGreater(record["updated_blocks"], 0)
            self.assertEqual(prepare_wiki_navigation_updates(state["blocks"], DOC, ORIGIN, WIKI)["updates"], [])
            count = len(calls)
            await service.accept_navigation_event(*signed_event(event_id="self-write", event_type="drive.file.edit_v1"))
            self.assertIsNone(await self.claim(now=time.time() + 100))
            self.assertEqual(len(calls), count)

    async def test_failed_permission_and_timeout_preserve_document_and_retry_limits(self):
        for failure in (feishu.FeishuNavigationAPIError("request", 403, retryable=False), TimeoutError()):
            doc = DOC + ("-auth" if isinstance(failure, feishu.FeishuNavigationAPIError) else "-timeout")
            storage.register_document(doc, ORIGIN.replace(DOC, doc))
            job = await self.claim()
            with patch.object(feishu, "subscribe_navigation_events", AsyncMock(side_effect=failure)), \
                    patch.object(feishu, "update_navigation_blocks", AsyncMock()) as write:
                await service.process_navigation_job(job)
                write.assert_not_awaited()
            record = next(r for r in storage.list_records() if r["doc_token"] == doc)
            self.assertEqual(record["status"], "failed" if not isinstance(failure, TimeoutError) else "pending")

    async def test_router_auth_body_limit_challenge_and_unsupported_event(self):
        app = FastAPI()
        app.include_router(routes.router)
        async with REAL_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            body, headers = signed_event(doc="unknown")
            self.assertEqual((await client.post("/api/feishu/events", content=body, headers=headers)).status_code, 200)
            self.assertEqual(storage.list_records(), [])
            self.assertEqual((await client.post("/api/feishu/events", content=body)).status_code, 403)
            with patch.object(routes, "FEISHU_EVENT_MAX_BYTES", 10):
                self.assertEqual((await client.post("/api/feishu/events", content=body)).status_code, 413)
            response = await client.post("/api/feishu/events", json={"type": "url_verification", "token": TOKEN, "challenge": "ready"})
            self.assertEqual(response.json(), {"challenge": "ready"})
            with patch.object(config, "FEISHU_EVENT_ENCRYPT_KEY", ""):
                self.assertEqual((await client.post("/api/feishu/events", content=body, headers=headers)).status_code, 503)

    async def test_disabled_mode_has_no_storage_network_or_background_work(self):
        with patch.object(config, "FEISHU_WIKI_AUTO_UPDATE_ENABLED", False), \
                patch.object(storage, "register_document") as register, \
                patch.object(service, "_worker", AsyncMock()) as worker:
            self.assertFalse(await service.register_exported_navigation(DOC, ORIGIN))
            async with service.navigation_lifespan(None):
                pass
            register.assert_not_called()
            worker.assert_not_awaited()

    async def test_lifespan_starts_worker_and_cancels_it_on_shutdown(self):
        started = asyncio.Event()
        stopped = asyncio.Event()
        async def worker(stop):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        with patch.object(service, "_worker", side_effect=worker):
            async with service.navigation_lifespan(None):
                await asyncio.wait_for(started.wait(), timeout=1)
            self.assertTrue(stopped.is_set())

    async def test_main_mounts_verified_callback_without_browser_cookie(self):
        # DATA_DIR is set to the approved isolated tree before importing main.
        from app import main
        with patch.object(main, "FEISHU_LOGIN_REQUIRED", True), \
                patch.object(config, "FEISHU_WIKI_AUTO_UPDATE_ENABLED", False):
            async with REAL_CLIENT(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                body, headers = signed_event(doc="unknown")
                response = await client.post("/api/feishu/events", content=body, headers=headers)
                self.assertEqual(response.status_code, 200)
                response = await client.post("/api/feishu/events", content=body)
                self.assertEqual(response.status_code, 403)

    async def test_job_total_timeout_cancels_slow_subscription(self):
        storage.register_document(DOC, ORIGIN)
        job = await self.claim()
        async def slow(*args):
            await asyncio.sleep(10)
        start = time.monotonic()
        with patch.object(config, "FEISHU_NAVIGATION_JOB_TIMEOUT_SECONDS", 0.02), \
                patch.object(feishu, "subscribe_navigation_events", side_effect=slow), \
                patch.object(feishu, "update_navigation_blocks", AsyncMock()) as write:
            await service.process_navigation_job(job)
            write.assert_not_awaited()
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(storage.list_records()[0]["status"], "pending")

    async def test_rate_limit_honors_retry_after_without_immediate_loop(self):
        storage.register_document(DOC, ORIGIN)
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, headers={"Retry-After": "120"}, json={"code": 99991400})
        start = time.time()
        with patch.object(feishu.httpx, "AsyncClient", side_effect=lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler))):
            await service.process_navigation_job(await self.claim())
        record = storage.list_records()[0]
        self.assertEqual(record["status"], "pending")
        self.assertGreaterEqual(record["next_at"], start + 120)
        self.assertEqual(len(calls), 1)

    async def test_readback_failure_never_reports_completed(self):
        storage.register_document(DOC, ORIGIN)
        snapshot = {"revision_id": 4, "blocks": live_blocks()}
        with patch.object(feishu, "subscribe_navigation_events", AsyncMock()), \
                patch.object(feishu, "resolve_navigation_wiki_url", AsyncMock(return_value=WIKI)), \
                patch.object(feishu, "get_navigation_snapshot", AsyncMock(return_value=snapshot)), \
                patch.object(feishu, "update_navigation_blocks", AsyncMock(return_value=4)):
            await service.process_navigation_job(await self.claim())
        self.assertEqual(storage.list_records()[0]["status"], "pending")
        self.assertEqual(storage.list_records()[0]["last_error"], "verification:links_remaining")

    async def test_existing_subscription_and_pre_moved_old_document(self):
        calls = []
        def handler(request):
            calls.append(request.method)
            return httpx.Response(200, json={"code": 0, "data": {"is_subscribe": True}})
        with patch.object(feishu.httpx, "AsyncClient", side_effect=lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler))):
            await feishu.subscribe_navigation_events(DOC)
        self.assertEqual(calls, ["GET"])
        self.assertTrue(await service.register_existing_document(ORIGIN))
        self.assertFalse(await service.register_existing_document(ORIGIN))

    async def test_partial_unresolvable_links_are_reported_as_skipped(self):
        blocks = live_blocks()
        source = next(block for block in blocks if "ordered" in block)
        run = next(e["text_run"] for e in source["ordered"]["elements"] if e["text_run"].get("text_element_style", {}).get("link"))
        run["text_element_style"]["link"]["url"] = ORIGIN + "#missing"
        storage.register_document(DOC, ORIGIN)
        async def snapshot(*args):
            return {"revision_id": 4, "blocks": deepcopy(blocks)}
        async def update(token, revision, updates):
            apply_block_updates(blocks, updates)
            return len(updates)
        with patch.object(feishu, "subscribe_navigation_events", AsyncMock()), \
                patch.object(feishu, "resolve_navigation_wiki_url", AsyncMock(return_value=WIKI)), \
                patch.object(feishu, "get_navigation_snapshot", side_effect=snapshot), \
                patch.object(feishu, "update_navigation_blocks", side_effect=update):
            await service.process_navigation_job(await self.claim())
        self.assertEqual(storage.list_records()[0]["status"], "completed_with_skips")
        self.assertGreater(storage.list_records()[0]["skipped_links"], 0)

    async def test_version_conflict_and_partial_batch_never_fall_back_to_latest(self):
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, json={"code": 0, "data": {"document_revision_id": 11}})
            return httpx.Response(400, json={"code": 1770032})
        updates = [{"block_id": str(i), "update_text_elements": {"elements": []}} for i in range(205)]
        with patch.object(feishu.httpx, "AsyncClient", side_effect=lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler))):
            with self.assertRaises(feishu.FeishuNavigationAPIError) as raised:
                await feishu.update_navigation_blocks(DOC, 10, updates)
        self.assertEqual(raised.exception.updated_blocks, 100)
        self.assertEqual([r.url.params["document_revision_id"] for r in calls], ["10", "11"])
        self.assertEqual([len(json.loads(r.content)["requests"]) for r in calls], [100, 100])

    async def test_wiki_identity_mismatch_and_broken_pagination_are_rejected(self):
        for scenario in ("foreign-wiki", "pagination"):
            def handler(request):
                if scenario == "foreign-wiki":
                    data = {"node": {"node_token": "wiki-test", "obj_token": "other", "obj_type": "docx"}}
                elif request.url.path.endswith("/blocks"):
                    data = {"items": [{"block_id": "one"}], "has_more": True, "page_token": "repeated"}
                else:
                    data = {"document": {"document_id": DOC, "revision_id": 5}}
                return httpx.Response(200, json={"code": 0, "data": data})
            with patch.object(feishu.httpx, "AsyncClient", side_effect=lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler))):
                with self.assertRaises(feishu.FeishuNavigationAPIError):
                    if scenario == "foreign-wiki":
                        await feishu.resolve_navigation_wiki_url(DOC, ORIGIN)
                    else:
                        await feishu.get_navigation_snapshot(DOC)


if __name__ == "__main__":
    unittest.main()
