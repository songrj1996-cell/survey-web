"""Synthetic translation/cache checks; never call a real model or touch reports."""
import asyncio
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
import httpx

from app.core import config
from app.core import security
from app.core.llm_context import current_llm_api_key
from app.routers import survey as survey_router
from app.services import history_service, survey_service
from app.services import report_versions
from app.services import report_source_translation as service
from app.storage import report_source_translations as storage


def source(number=1, text="The reward did not arrive.", question_key="5"):
    return {"response_id": f"{question_key}/r{number}", "question_key": question_key,
            "question": "奖励体验", "text": text}


def answer(messages):
    items = json.loads(messages[1]["content"])["sources"]
    return {"schema_version": 1, "translations": [
        {**{key: item[key] for key in ("response_id", "question_key", "offset", "end_offset")},
         "translation_zh": "完整中文译文：" + item["response_id"]} for item in items]}


def frozen_report():
    versions = []
    for version in (1, 2):
        questions = [{"question_key": key, "question": f"Q{key} 冻结题目 V{version}",
                      "sources": [{"response_id": f"{key}/r1", "text": f"Original answer V{version}, question {key}."}]}
                     for key in ("5", "8")]
        versions.append({"version": version, "kind": "initial" if version == 1 else "regenerate",
                         "report_style": "quick", "report_mode": "quick",
                         "report_md": f"# 合成报告 V{version}", "input_snapshot": {"source_questions": questions}})
    return {"id": "history-report", "owner_key": "email:owner@example.com", "mode": "standard",
            "report_md": "# 合成报告 V2", "active_report_version": 2, "report_versions": versions,
            "input_snapshot": {"source_questions": [{"question_key": "5", "question": "界面上的修改题目",
                                "sources": [{"response_id": "5/r1", "text": "Unsaved source must not leak."}]}]}}


class ReportSourceTranslationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="source-translation-")
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "cache"
        directory = patch.object(config, "REPORT_SOURCE_TRANSLATIONS_DIR", self.cache)
        directory.start()
        self.addCleanup(directory.stop)
        prompt = patch.object(service, "_get_prompt_text", return_value=config.DEFAULT_REPORT_SOURCE_TRANSLATION_SYSTEM)
        self.prompt = prompt.start()
        self.addCleanup(prompt.stop)
        self.calls = []

    async def asyncTearDown(self):
        state = service._LOOP_STATES.get(asyncio.get_running_loop())
        if state and state["jobs"]:
            await asyncio.gather(*state["jobs"], return_exceptions=True)
        service._LOOP_STATES.pop(asyncio.get_running_loop(), None)

    async def collect(self, messages, **kwargs):
        self.calls.append(deepcopy(messages))
        self.assertLessEqual(sum(len(message["content"]) for message in messages), config.REPORT_SOURCE_TRANSLATION_INPUT_CHARS)
        self.assertEqual(kwargs["max_http_attempts"], config.LLM_QUICK_REPORT_HTTP_ATTEMPT_CAP)
        return json.dumps(answer(messages), ensure_ascii=False), "synthetic"

    async def translate(self, items, *, namespace="owner/report/v1", collect=None):
        return await service.translate_report_source_items(items, namespace=namespace, collect=collect or self.collect)

    async def test_original_order_and_text_are_preserved_and_verified_cache_avoids_calls(self):
        items = [source(2), source(1)]
        items[0]["translation_zh"] = "不能信任这份输入译文"
        before = deepcopy(items)
        async def reverse(messages, **kwargs):
            self.calls.append(deepcopy(messages))
            result = answer(messages)
            result["translations"].reverse()
            return json.dumps(result), "fake"
        first = await self.translate(items, collect=reverse)
        second = await self.translate(items)
        self.assertEqual(first, second)
        self.assertEqual(items, before)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([item["response_id"] for item in first], ["5/r2", "5/r1"])
        self.assertEqual([item["translation_zh"] for item in first], ["完整中文译文：5/r2", "完整中文译文：5/r1"])
        self.assertTrue(all(item["text"] == before[index]["text"] for index, item in enumerate(first)))
        for path in self.cache.glob("*.json"):
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(stored), {"schema_version", "fingerprint", "translation_zh", "translation_sha256"})
            self.assertEqual(stored["fingerprint"], path.stem)
            self.assertNotIn("The reward", path.read_text(encoding="utf-8"))

    async def test_pure_chinese_has_no_model_call_but_mixed_foreign_text_is_translated(self):
        items = [source(1, "奖励还没收到。\n请核实，金额 123。"), source(2, "123 😀"), source(3, "奖励 not received")]
        result = await self.translate(items)
        self.assertEqual(len(self.calls), 1)
        sent = json.loads(self.calls[0][1]["content"])["sources"]
        self.assertEqual([item["response_id"] for item in sent], ["5/r3"])
        self.assertEqual(result[0]["translation_zh"], items[0]["text"])
        self.assertEqual(result[1]["translation_zh"], items[1]["text"])
        self.assertTrue(all(item["translation_status"] == "complete" for item in result))

    async def test_unknown_id_never_borrows_translation_and_only_failed_source_retries(self):
        async def invalid(messages, **kwargs):
            self.calls.append(deepcopy(messages))
            result = answer(messages)
            for item in result["translations"]:
                if item["response_id"] == "5/r2":
                    item["response_id"] = "unknown/r999"
            return json.dumps(result), "fake"
        items = [source(1), source(2)]
        first = await self.translate(items, collect=invalid)
        self.assertEqual([item["translation_status"] for item in first], ["complete", "failed"])
        self.assertEqual(first[1]["translation_error"], "missing_translation")
        self.assertEqual(first[1]["translation_zh"], "")
        self.assertEqual(len(self.calls), 2)
        retry_inputs = json.loads(self.calls[1][1]["content"])["sources"]
        self.assertEqual([item["response_id"] for item in retry_inputs], ["5/r2"])
        second = await self.translate(items)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual([item["response_id"] for item in json.loads(self.calls[2][1]["content"])["sources"]], ["5/r2"])
        self.assertTrue(all(item["translation_status"] == "complete" for item in second))

    async def test_invalid_content_and_duplicate_mapping_have_one_repair_then_fail(self):
        async def invalid(messages, **kwargs):
            self.calls.append(deepcopy(messages))
            result = answer(messages)
            result["translations"] += deepcopy(result["translations"])
            return json.dumps(result), "fake"
        result = await self.translate([source()], collect=invalid)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result[0]["translation_error"], "invalid_translation")
        self.assertEqual(list(self.cache.glob("*.json")), [])
        malformed = AsyncMock(return_value=("private raw error <token>", "fake"))
        result = await self.translate([source()], collect=malformed)
        self.assertEqual(malformed.await_count, 2)
        self.assertEqual(result[0]["translation_error"], "invalid_structure")
        self.assertNotIn("private", json.dumps(result))

    async def test_cache_separates_versions_owners_source_text_and_prompt_configuration(self):
        for namespace in ("owner/report/v1", "owner/report/v2", "other-owner/report/v1"):
            await self.translate([source()], namespace=namespace)
        self.assertEqual(len(self.calls), 3)
        await self.translate([source(text="A changed answer.")])
        self.assertEqual(len(self.calls), 4)
        self.prompt.return_value += "\n保留细节。"
        await self.translate([source()])
        self.assertEqual(len(self.calls), 5)
        with patch.object(config, "LLM_QUICK_REPORT_MODEL", "different-synthetic-model"):
            await self.translate([source()])
        self.assertEqual(len(self.calls), 6)

    async def test_corrupt_cache_hash_is_not_reused_and_paths_are_only_fingerprints(self):
        await self.translate([source()], namespace="../../owner/report/v1")
        path = next(self.cache.glob("*.json"))
        cached = json.loads(path.read_text(encoding="utf-8"))
        cached["translation_zh"] = "被修改的译文"
        path.write_text(json.dumps(cached), encoding="utf-8")
        await self.translate([source()], namespace="../../owner/report/v1")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(list(self.cache.glob("*.json"))), 1)
        with self.assertRaises(ValueError):
            storage.load_source_translation("../../outside")

    async def test_overlapping_concurrent_pages_share_each_inflight_source(self):
        async def slow(messages, **kwargs):
            self.calls.append(deepcopy(messages))
            await asyncio.sleep(0.04)
            return json.dumps(answer(messages)), "fake"
        first, second = await asyncio.gather(self.translate([source(1), source(2)], collect=slow),
                                             self.translate([source(2), source(3)], collect=slow))
        seen = Counter(item["response_id"] for messages in self.calls
                       for item in json.loads(messages[1]["content"])["sources"])
        self.assertEqual(seen, {"5/r1": 1, "5/r2": 1, "5/r3": 1})
        self.assertTrue(all(item["translation_status"] == "complete" for item in first + second))

    async def test_cancelling_one_viewer_does_not_cancel_shared_translation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(messages, **kwargs):
            self.calls.append(deepcopy(messages))
            entered.set()
            await release.wait()
            return json.dumps(answer(messages)), "fake"
        first = asyncio.create_task(self.translate([source()], collect=blocked))
        await entered.wait()
        second = asyncio.create_task(self.translate([source()], collect=blocked))
        await asyncio.sleep(0.02)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        result = await second
        self.assertEqual(result[0]["translation_status"], "complete")
        self.assertEqual(len(self.calls), 1)

    async def test_call_timeout_is_safe_and_manual_retry_is_not_permanently_cached(self):
        cancelled = asyncio.Event()
        async def slow(messages, **kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        with patch.object(config, "REPORT_SOURCE_TRANSLATION_CALL_TIMEOUT_SECONDS", 0.02):
            result = await self.translate([source()], collect=slow)
            self.assertEqual(result[0]["translation_error"], "call_timeout")
            self.assertTrue(cancelled.is_set())
            recovered = await self.translate([source()])
        self.assertEqual(recovered[0]["translation_status"], "complete")
        self.assertEqual(len(self.calls), 1)

    async def test_page_timeout_keeps_completed_sources_and_cancels_remaining_calls(self):
        cancelled = asyncio.Event()
        async def mixed(messages, **kwargs):
            items = json.loads(messages[1]["content"])["sources"]
            if any(item["response_id"] == "5/r2" for item in items):
                try:
                    await asyncio.sleep(10)
                finally:
                    cancelled.set()
            return json.dumps(answer(messages)), "fake"
        items = [source(1, "English feedback " * 100), source(2, "Another feedback " * 100)]
        with patch.object(config, "REPORT_SOURCE_TRANSLATION_INPUT_CHARS", 3000), \
                patch.object(config, "REPORT_SOURCE_TRANSLATION_PAGE_TIMEOUT_SECONDS", 0.08):
            result = await self.translate(items, collect=mixed)
        self.assertEqual([item["translation_status"] for item in result], ["complete", "failed"])
        self.assertEqual(result[1]["translation_error"], "page_timeout")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(list(self.cache.glob("*.json"))), 1)

    async def test_page_timeout_during_repair_keeps_other_valid_item_in_same_batch(self):
        calls = 0
        async def missing_then_slow(messages, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                await asyncio.sleep(10)
            value = answer(messages)
            value["translations"] = value["translations"][:1]
            return json.dumps(value), "fake"
        with patch.object(config, "REPORT_SOURCE_TRANSLATION_PAGE_TIMEOUT_SECONDS", 0.08):
            result = await self.translate([source(1), source(2)], collect=missing_then_slow)
        self.assertEqual([item["translation_status"] for item in result], ["complete", "failed"])
        self.assertEqual(result[1]["translation_error"], "page_timeout")
        self.assertEqual(len(list(self.cache.glob("*.json"))), 1)

    async def test_long_sources_split_without_losing_tail_and_partial_translation_is_not_complete(self):
        original = 'Start "\\😀 ' * 350 + " THE_FINAL_SENTENCE"
        fragments = []
        async def translate_fragments(messages, **kwargs):
            fragments.extend(json.loads(messages[1]["content"])["sources"])
            return await self.collect(messages, **kwargs)
        with patch.object(config, "REPORT_SOURCE_TRANSLATION_INPUT_CHARS", 2500):
            result = await self.translate([source(text=original)], collect=translate_fragments)
        ordered = sorted(fragments, key=lambda item: item["offset"])
        self.assertGreater(len(ordered), 1)
        self.assertEqual("".join(item["text"] for item in ordered), original)
        self.assertEqual(ordered[-1]["end_offset"], len(original))
        self.assertEqual(result[0]["text"], original)
        self.assertEqual(result[0]["translation_status"], "complete")
        async def missing_tail(messages, **kwargs):
            value = answer(messages)
            value["translations"] = [item for item in value["translations"] if item["end_offset"] != len(original)]
            return json.dumps(value), "fake"
        with patch.object(config, "REPORT_SOURCE_TRANSLATION_INPUT_CHARS", 2500):
            failed = await self.translate([source(text=original)], namespace="owner/report/v2", collect=missing_tail)
        self.assertEqual(failed[0]["translation_status"], "failed")
        self.assertEqual(failed[0]["translation_zh"], "")
        self.assertEqual(failed[0]["text"], original)

    async def test_item_and_batch_budgets_reject_without_truncation(self):
        with self.assertRaises(ValueError):
            await self.translate([source(index) for index in range(51)])
        item = source(text="Large answer " * 1000)
        with patch.object(config, "REPORT_SOURCE_TRANSLATION_MAX_BATCHES", 1), \
                patch.object(config, "REPORT_SOURCE_TRANSLATION_INPUT_CHARS", 2500):
            result = await self.translate([item])
        self.assertEqual(result[0]["translation_error"], "input_budget_exceeded")
        self.assertEqual(result[0]["text"], item["text"])
        self.assertEqual(self.calls, [])

    def test_cache_write_is_atomic_and_failed_replace_cleans_only_its_temporary_file(self):
        fingerprint = "a" * 64
        storage.save_source_translation(fingerprint, "原来的完整译文")
        with patch.object(storage.os, "replace", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(OSError):
                storage.save_source_translation(fingerprint, "新的译文")
        self.assertEqual(storage.load_source_translation(fingerprint), "原来的完整译文")
        self.assertEqual(len(list(self.cache.iterdir())), 1)

    async def test_service_uses_explicit_frozen_version_and_authenticated_owner_namespace(self):
        report = frozen_report()
        before = deepcopy(report)
        namespaces = []
        translate = service.translate_report_source_items
        async def capture(items, *, namespace):
            namespaces.append(json.loads(namespace))
            return await translate(items, namespace=namespace, collect=self.collect)
        with patch.object(config, "FEISHU_LOGIN_REQUIRED", True), \
                patch.object(survey_service, "get_session", return_value=report), \
                patch.object(service, "translate_report_source_items", side_effect=capture), \
                patch.object(survey_service, "save_session") as save:
            result = await survey_service.translate_report_sources("session-report", version=1,
                response_ids=["8/r1", "5/r1"], login={"email": "owner@example.com"})
        self.assertEqual(result["version"], 1)
        self.assertEqual([item["question_key"] for item in result["items"]], ["8", "5"])
        self.assertEqual([item["question"] for item in result["items"]], ["Q8 冻结题目 V1", "Q5 冻结题目 V1"])
        self.assertEqual([item["text"] for item in result["items"]],
                         ["Original answer V1, question 8.", "Original answer V1, question 5."])
        self.assertEqual([item["translation_zh"] for item in result["items"]],
                         ["完整中文译文：8/r1", "完整中文译文：5/r1"])
        self.assertEqual(namespaces, [{"report": "session-report", "version": 1, "owner": "email:owner@example.com"}])
        self.assertEqual(report, before)
        save.assert_not_called()

    async def test_metadata_is_returned_with_translation_but_never_sent_to_model(self):
        item = source()
        item.update(profile={"段位": "PRIVATE_RANK", "场次": 0}, ids={"玩家编号": "PRIVATE_PLAYER_ID"},
                    source_id="PRIVATE_SOURCE_ID")
        result = await self.translate([item])
        self.assertEqual(result[0]["profile"], item["profile"])
        self.assertEqual(result[0]["ids"], item["ids"])
        self.assertEqual(result[0]["source_id"], item["source_id"])
        sent = json.dumps(self.calls)
        for value in ("PRIVATE_RANK", "PRIVATE_PLAYER_ID", "PRIVATE_SOURCE_ID", "profile"):
            self.assertNotIn(value, sent)

    async def test_legacy_session_source_page_and_translation_enrich_matching_frozen_metadata(self):
        from tests.test_report_modes_lifecycle import profile_source, legacy_profile_snapshot
        report = profile_source()
        report_versions.append_report_version(report, legacy_profile_snapshot(report), kind="initial")
        before = deepcopy(report)
        with patch.object(config, "FEISHU_LOGIN_REQUIRED", True), \
                patch.object(survey_service, "get_session", return_value=report), \
                patch.object(service, "collect_chat_completion", side_effect=self.collect), \
                patch.object(survey_service, "save_session") as save:
            page = survey_service.get_report_sources("legacy-session", version=1, question_key="4",
                login={"email": "owner@example.com"})
            result = await survey_service.translate_report_sources("legacy-session", version=1,
                response_ids=["4/r2", "4/r1"], login={"email": "owner@example.com"})
        self.assertEqual([item["profile"]["段位"] for item in page["items"]], ["Gold", "Silver"])
        self.assertEqual([item["profile"]["段位"] for item in result["items"]], ["Silver", "Gold"])
        self.assertEqual(result["items"][1]["ids"]["记录编号"], "0")
        self.assertNotIn("private-second-id", json.dumps(self.calls))
        self.assertNotIn("Gold", json.dumps(self.calls))
        self.assertEqual(report, before)
        save.assert_not_called()

    async def test_legacy_history_only_enriches_from_matching_owned_original_session(self):
        from tests.test_report_modes_lifecycle import profile_source, legacy_profile_snapshot
        original = profile_source()
        report_versions.append_report_version(original, legacy_profile_snapshot(original), kind="initial")
        historical = deepcopy(original)
        historical["id"] = "legacy-history"
        historical.pop("rows")
        before = deepcopy(historical)
        with patch.object(config, "FEISHU_LOGIN_REQUIRED", True), \
                patch.object(security, "FEISHU_LOGIN_REQUIRED", True), \
                patch.object(history_service, "_load_history_with_report_numbers", return_value=[historical]), \
                patch.object(survey_service, "get_session", return_value=original) as loader:
            page = survey_service.get_report_sources("unrelated-session", history_id="legacy-history", version=1,
                login={"email": "owner@example.com"})
            self.assertEqual(page["items"][0]["profile"]["段位"], "Gold")
            loader.assert_called_once_with("legacy-history")
            original["owner_key"] = "email:someone-else@example.com"
            denied = survey_service.get_report_sources("unrelated-session", history_id="legacy-history", version=1,
                login={"email": "owner@example.com"})
            self.assertEqual(denied["items"][0]["profile"], {})
            loader.side_effect = HTTPException(status_code=404, detail="expired")
            expired = survey_service.get_report_sources("unrelated-session", history_id="legacy-history", version=1,
                login={"email": "owner@example.com"})
            self.assertEqual(expired["items"][0]["profile"], {})
        self.assertEqual(historical, before)

    async def test_service_session_permission_checks_precede_translation(self):
        translator = AsyncMock(side_effect=AssertionError("unauthorized source must not reach translation"))
        with patch.object(config, "FEISHU_LOGIN_REQUIRED", True), \
                patch.object(survey_service, "get_session", return_value=frozen_report()) as loader, \
                patch.object(service, "translate_report_source_items", new=translator):
            for login, status in ((None, 401), ({"email": "other@example.com"}, 404)):
                with self.subTest(login=login), self.assertRaises(HTTPException) as caught:
                    await survey_service.translate_report_sources("session-report", version=1,
                        response_ids=["5/r1"], login=login)
                self.assertEqual(caught.exception.status_code, status)
            self.assertEqual(loader.call_count, 1)
        translator.assert_not_awaited()

    async def test_service_history_access_and_version_select_frozen_originals(self):
        report = frozen_report()
        before = deepcopy(report)
        namespaces = []
        translate = service.translate_report_source_items
        async def capture(items, *, namespace):
            namespaces.append(json.loads(namespace))
            return await translate(items, namespace=namespace, collect=self.collect)
        with patch.object(security, "FEISHU_LOGIN_REQUIRED", True), \
                patch.object(history_service, "_load_history_with_report_numbers", return_value=[report]), \
                patch.object(survey_service, "get_session", side_effect=AssertionError("history must use history ownership")), \
                patch.object(service, "translate_report_source_items", side_effect=capture) as translator:
            result = await survey_service.translate_report_sources("unused-session", history_id="history-report",
                version=1, response_ids=["5/r1"], login={"email": "owner@example.com"})
            for login in (None, {"email": "other@example.com"}):
                with self.subTest(login=login), self.assertRaises(HTTPException) as caught:
                    await survey_service.translate_report_sources("unused-session", history_id="history-report",
                        version=1, response_ids=["5/r1"], login=login)
                self.assertEqual(caught.exception.status_code, 404)
            self.assertEqual(translator.await_count, 1)
        self.assertEqual(result["items"][0]["text"], "Original answer V1, question 5.")
        self.assertEqual(result["items"][0]["question"], "Q5 冻结题目 V1")
        self.assertEqual(namespaces, [{"report": "history-report", "version": 1, "owner": "email:owner@example.com"}])
        self.assertEqual(report, before)

    async def test_service_unknown_source_or_version_never_starts_translation(self):
        report = frozen_report()
        translator = AsyncMock(side_effect=AssertionError("invalid source must not reach translation"))
        with patch.object(config, "FEISHU_LOGIN_REQUIRED", True), \
                patch.object(security, "FEISHU_LOGIN_REQUIRED", True), \
                patch.object(survey_service, "get_session", return_value=report), \
                patch.object(history_service, "_load_history_with_report_numbers", return_value=[report]), \
                patch.object(service, "translate_report_source_items", new=translator):
            for history_id in ("", "history-report"):
                for version, ids, status in ((99, ["5/r1"], 404), (1, ["missing/r1"], 400), (1, [], 400)):
                    with self.subTest(history=history_id, version=version, ids=ids), self.assertRaises(HTTPException) as caught:
                        await survey_service.translate_report_sources("session-report", history_id=history_id,
                            version=version, response_ids=ids, login={"email": "owner@example.com"})
                    self.assertEqual(caught.exception.status_code, status)
        translator.assert_not_awaited()

    async def test_route_binds_request_credentials_and_forwards_version_history_parameters(self):
        app = FastAPI()
        app.include_router(survey_router.router)
        report = frozen_report()
        seen_keys = []
        async def collector(messages, **kwargs):
            seen_keys.append(current_llm_api_key())
            return await self.collect(messages, **kwargs)
        with patch.object(config, "FEISHU_LOGIN_REQUIRED", True), \
                patch.object(security, "FEISHU_LOGIN_REQUIRED", True), \
                patch.object(survey_router, "_current_login", new=AsyncMock(return_value={"email": "owner@example.com"})), \
                patch.object(survey_router, "require_request_llm_api_key", new=AsyncMock(return_value="synthetic-request-key")), \
                patch.object(survey_service, "get_session", return_value=report), \
                patch.object(history_service, "_load_history_with_report_numbers", return_value=[report]), \
                patch.object(service, "collect_chat_completion", side_effect=collector), \
                patch.object(survey_router, "translate_report_sources", wraps=survey_service.translate_report_sources) as integration:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                current = await client.post("/api/report/session-report/sources/translate", json={
                    "version": 2, "response_ids": ["8/r1"]})
                historical = await client.post("/api/report/unused-session/sources/translate", json={
                    "version": 1, "history_id": "history-report", "response_ids": ["5/r1"]})
        self.assertEqual((current.status_code, historical.status_code), (200, 200))
        self.assertEqual(current.json()["items"][0]["text"], "Original answer V2, question 8.")
        self.assertEqual(historical.json()["items"][0]["text"], "Original answer V1, question 5.")
        self.assertEqual(integration.await_args_list[0].kwargs, {"version": 2, "history_id": "",
                         "response_ids": ["8/r1"], "login": {"email": "owner@example.com"}})
        self.assertEqual(integration.await_args_list[1].kwargs, {"version": 1, "history_id": "history-report",
                         "response_ids": ["5/r1"], "login": {"email": "owner@example.com"}})
        self.assertEqual(seen_keys, ["synthetic-request-key", "synthetic-request-key"])
        self.assertEqual(current_llm_api_key(), "")

    async def test_route_rejects_invalid_parameters_and_missing_credentials_without_translation(self):
        app = FastAPI()
        app.include_router(survey_router.router)
        denied = AsyncMock(side_effect=HTTPException(status_code=403, detail="synthetic missing credentials"))
        translator = AsyncMock(side_effect=AssertionError("invalid request must not reach service"))
        with patch.object(survey_router, "_current_login", new=AsyncMock(return_value={"email": "owner@example.com"})), \
                patch.object(survey_router, "require_request_llm_api_key", new=denied), \
                patch.object(survey_router, "translate_report_sources", new=translator):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                for payload in ({"version": 0, "response_ids": ["5/r1"]},
                                {"version": 1, "response_ids": []},
                                {"version": 1, "response_ids": [f"5/r{i}" for i in range(51)]}):
                    response = await client.post("/api/report/session/sources/translate", json=payload)
                    self.assertEqual(response.status_code, 422)
                denied.assert_not_awaited()
                response = await client.post("/api/report/session/sources/translate", json={"version": 1, "response_ids": ["5/r1"]})
                self.assertEqual(response.status_code, 403)
        translator.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
