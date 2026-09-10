"""Reruns inherit their base version's mode; all persistence uses synthetic data."""
from contextlib import ExitStack
from copy import deepcopy
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.core.config import DEFAULT_QUICK_WRITER_REQUIREMENTS
from app.services import report_history, report_versions, survey_service
from app.storage import history as history_storage, sessions as session_storage
from tests.test_duplicate_report_flow import (
    LOGIN, TemporaryDuplicateRuntimeMixin, _fingerprint_session, _snapshot,
)
from tests.test_report_quick_mode_flow import _draft
from tests.test_survey_report_versions import _base_session, _event_payloads, _isolated_report_runtime, _writer


class ReportRerunStyleTests(TemporaryDuplicateRuntimeMixin, unittest.IsolatedAsyncioTestCase):
    def _archive_modes(self, *styles):
        history_id = str(uuid.uuid4())
        source = _fingerprint_session(include_plan=True)
        for index, style in enumerate(styles, 1):
            snapshot = {**_snapshot(f"原报告 V{index}"), "report_style": style}
            report_versions.append_report_version(
                source, snapshot, kind="initial" if index == 1 else "regenerate",
                base_version=None if index == 1 else index - 1,
            )
        entry = report_history.save_to_history(history_id, source)
        return history_id, entry

    def _prepare(self, history_id, *, base_version=None, stale_style=None, enabled=True):
        fresh = _fingerprint_session()
        if stale_style is not None:
            fresh["pending_report_style"] = stale_style
        session_id = self._new_session(fresh)
        with patch.object(survey_service, "is_quick_report_enabled", return_value=enabled):
            prepared = survey_service.prepare_duplicate_report_rerun(
                session_id, LOGIN, history_id=history_id, base_version=base_version,
            )
        return session_id, prepared

    def _entry(self, history_id):
        return next(entry for entry in history_storage._load_history() if entry["id"] == history_id)

    async def _generate(self, session_id, *, expected_style, enabled=True, stale_style=None):
        ready = session_storage.get_session(session_id)
        ready["stats_md"] = "有效样本(总计):总体=1"
        ready["open_text"] = {}
        if stale_style is not None:
            ready["pending_report_style"] = stale_style
        session_storage.save_session(session_id, ready)
        writer = AsyncMock(return_value=(_draft(), "mock-model")) if expected_style == "quick" else _writer("重生成完整报告")
        with _isolated_report_runtime(ready, writer), ExitStack() as stack:
            # Keep the model/rendering substitutes, but exercise real isolated
            # session persistence and the atomic append to the same history card.
            stack.enter_context(patch.object(survey_service, "get_session", session_storage.get_session))
            stack.enter_context(patch.object(survey_service, "save_session", session_storage.save_session))
            stack.enter_context(patch.object(survey_service, "is_quick_report_enabled", return_value=enabled))
            stack.enter_context(patch.object(survey_service, "_get_prompt_text", return_value=DEFAULT_QUICK_WRITER_REQUIREMENTS))
            events = _event_payloads([
                event async for event in survey_service.report_stream(session_id, None, generation_kind="initial")
            ])
        return events, writer

    async def test_prepare_inherits_selected_or_active_version_over_new_session_default(self):
        cases = [
            (("quick",), None, None, "quick"),
            (("quick",), None, "full", "quick"),
            (("full",), None, "quick", "full"),
            (("quick", "full"), 1, "full", "quick"),
            (("full", "quick"), 1, "quick", "full"),
            (("full", "quick"), None, "full", "quick"),
        ]
        for styles, base, stale, expected in cases:
            with self.subTest(styles=styles, base=base, stale=stale):
                history_id, entry = self._archive_modes(*styles)
                before = deepcopy(entry)
                session_id, prepared = self._prepare(history_id, base_version=base, stale_style=stale)
                self.assertTrue(prepared["skip_plan"])
                self.assertEqual(prepared["base_version"], base or len(styles))
                self.assertEqual(session_storage.get_session(session_id)["pending_report_style"], expected)
                self.assertEqual(self._entry(history_id), before)

    async def test_quick_base_generates_quick_version_even_if_session_mode_was_reset(self):
        history_id, before = self._archive_modes("quick", "full")
        previous = report_versions.normalize_report_versions(before)
        session_id, _ = self._prepare(history_id, base_version=1)
        events, writer = await self._generate(session_id, expected_style="quick", stale_style="full")
        self.assertFalse([event for event in events if event["type"] == "error"], events)
        done = next(event for event in events if event["type"] == "report_done")
        self.assertEqual(done["report_style"], "quick")
        self.assertEqual(done["history_id"], history_id)
        self.assertEqual(writer.await_count, 1)
        after = self._entry(history_id)
        created = report_versions.resolve_report_version(after)
        self.assertEqual((created["version"], created["base_version"], created["report_style"]), (3, 1, "quick"))
        self.assertIn("发现与证据附录", created["report_md"])
        for version in previous:
            self.assertEqual(report_versions.resolve_report_version(after, version["version"]), version)
        self.assertEqual(session_storage.get_session(session_id)["report_style"], "quick")

    async def test_full_base_stays_full_despite_quick_active_version_and_closed_switch(self):
        history_id, before = self._archive_modes("full", "quick")
        previous = report_versions.normalize_report_versions(before)
        session_id, _ = self._prepare(history_id, base_version=1, stale_style="quick", enabled=False)
        events, writer = await self._generate(session_id, expected_style="full", enabled=False, stale_style="quick")
        self.assertFalse([event for event in events if event["type"] == "error"], events)
        done = next(event for event in events if event["type"] == "report_done")
        self.assertEqual(done["report_style"], "full")
        self.assertEqual(writer.await_count, 5)
        after = self._entry(history_id)
        created = report_versions.resolve_report_version(after)
        self.assertEqual((created["version"], created["base_version"], created["report_style"]), (3, 1, "full"))
        self.assertNotIn("quick_report_diagnostics", created)
        for version in previous:
            self.assertEqual(report_versions.resolve_report_version(after, version["version"]), version)

    async def test_closed_switch_rejects_quick_prepare_without_saving_rerun(self):
        history_id, before = self._archive_modes("quick")
        session_id = self._new_session(_fingerprint_session())
        stored = session_storage.get_session(session_id)
        with patch.object(survey_service, "is_quick_report_enabled", return_value=False), patch.object(
            survey_service, "save_session"
        ) as save:
            with self.assertRaises(HTTPException) as caught:
                survey_service.prepare_duplicate_report_rerun(session_id, LOGIN, history_id=history_id)
            self.assertEqual(caught.exception.status_code, 400)
            save.assert_not_called()
        self.assertNotIn("pending_report_style", stored)
        self.assertEqual(self._entry(history_id), before)

    async def test_closed_after_prepare_blocks_quick_without_fallback_and_retry_keeps_mode(self):
        history_id, before = self._archive_modes("quick")
        session_id, _ = self._prepare(history_id)
        events, writer = await self._generate(session_id, expected_style="quick", enabled=False, stale_style="full")
        writer.assert_not_awaited()
        self.assertTrue(any(event["type"] == "error" for event in events))
        self.assertFalse(any(event["type"] == "report_done" for event in events))
        self.assertEqual(self._entry(history_id), before)
        events, writer = await self._generate(session_id, expected_style="quick", enabled=True)
        self.assertTrue(any(event["type"] == "report_done" and event["report_style"] == "quick" for event in events), events)
        self.assertEqual(writer.await_count, 1)

    async def test_legacy_report_without_mode_inherits_full_and_preserves_v1(self):
        history_id, _ = self._archive_modes("full")

        def make_legacy(history):
            entry = next(item for item in history if item["id"] == history_id)
            for field in ("report_versions", "report_style", "active_report_version", "next_report_version"):
                entry.pop(field, None)

        history_storage.mutate_history(make_legacy)
        original = report_versions.resolve_report_version(self._entry(history_id), 1)
        session_id, _ = self._prepare(history_id, stale_style="quick", enabled=False)
        self.assertEqual(session_storage.get_session(session_id)["pending_report_style"], "full")
        events, _ = await self._generate(session_id, expected_style="full", enabled=False)
        self.assertFalse([event for event in events if event["type"] == "error"], events)
        after = self._entry(history_id)
        self.assertEqual(report_versions.resolve_report_version(after, 1), original)
        self.assertEqual(report_versions.resolve_report_version(after, 2)["report_style"], "full")

    async def test_internal_regeneration_also_uses_selected_base_mode(self):
        for base_style, other_style in (("quick", "full"), ("full", "quick")):
            with self.subTest(base=base_style):
                session = _base_session()
                report_versions.append_report_version(session, {**_snapshot("V1"), "report_style": base_style})
                report_versions.append_report_version(session, {**_snapshot("V2"), "report_style": other_style}, kind="regenerate", base_version=1)
                session["pending_report_style"] = other_style
                before = report_versions.normalize_report_versions(session)
                writer = AsyncMock(return_value=(_draft(), "mock-model")) if base_style == "quick" else _writer("完整 V3")
                with _isolated_report_runtime(session, writer), patch.object(
                    survey_service, "is_quick_report_enabled", return_value=base_style == "quick"
                ), patch.object(survey_service, "_get_prompt_text", return_value=DEFAULT_QUICK_WRITER_REQUIREMENTS):
                    events = _event_payloads([
                        event async for event in survey_service.report_stream("internal-rerun", None, generation_kind="regenerate", base_version=1)
                    ])
                self.assertFalse([event for event in events if event["type"] == "error"], events)
                self.assertEqual(report_versions.resolve_report_version(session, 3)["report_style"], base_style)
                for version in before:
                    self.assertEqual(report_versions.resolve_report_version(session, version["version"]), version)


if __name__ == "__main__":
    unittest.main()
