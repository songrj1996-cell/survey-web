"""Offline lifecycle tests; synthetic data and mock collectors only."""
import asyncio
from contextlib import ExitStack, contextmanager, redirect_stdout
from copy import deepcopy
import io
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from app.core import config
from app.services import survey_service, report_versions
from app.services.report_modes import prepare_report_markdown
from app.services.report_partial_rerun import partial_rerun_capability
from app.services.report_postwrite_inputs import freeze_inputs
from app.services.report_postwrite_pipeline import run_pipeline, BoundedCaller, BudgetStop, initial_draft_state, reuse_registry_state
from app.storage.report_postwrite_runs import RunStore
from app.storage.prompts import DEFAULT_PROMPTS
from tests.test_report_postwrite_audit import session, response, note_review

PROMPTS = {"writer": config.DEFAULT_POSTWRITE_WRITER, "registry": config.DEFAULT_POSTWRITE_REGISTRY, "audit": config.DEFAULT_POSTWRITE_AUDITOR}
REPORT = "# 合成报告\n\n熟悉规则可能降低门槛 [q1/r4]"


def legacy_meaning_fixture(total=31):
    import hashlib
    from app.services.report_postwrite_inputs import fingerprint
    from app.services.report_postwrite_audit import validate_registry, meaning_payload, messages, LEGACY_SOURCE_UNIT_FORMAT
    from app.services.report_postwrite_pipeline import run_identity, audit_summary
    frozen = freeze_inputs(session())
    claims = [{"id": f"C{i:03d}", "original": REPORT.split("\n\n")[1], "block_ids": ["B0002"],
               "kind": "inference", "review_refs": ["q1/r4"]} for i in range(1, total + 1)]
    registry = {"claims": claims, "tasks": [], "not_checked": [f"待核验备注{i}" for i in range(5)],
                "coverage": [{"block_id": "B0001", "no_claim": True}, {"block_id": "B0002", "claim_ids": [c["id"] for c in claims]}]}
    raw_registry = json.dumps(registry, ensure_ascii=False)
    state = initial_draft_state(frozen, prompts=PROMPTS, draft=REPORT)
    state["identity"] = run_identity(frozen, PROMPTS, source_protocol=LEGACY_SOURCE_UNIT_FORMAT)
    state["registry_raw"] = raw_registry
    state["registry"] = validate_registry(registry, REPORT, frozen["postwrite_packet"], raw_registry=raw_registry,
                                          legacy_note_dispositions=note_review(raw_registry))
    state["registry_reuse"] = {"source_response_sha256": hashlib.sha256(raw_registry.encode()).hexdigest()}
    payload = meaning_payload(REPORT, frozen["postwrite_packet"], state["registry"]["claims"][:30])
    key = "meaning:" + fingerprint([c["id"] for c in claims[:30]])[:16]
    obj = {"checks": [response(c) for c in claims[:30]], "not_checked": ["开放题人数另批核验", "targets以外的报告段落"]}
    obj["checks"][0]["sources"][0]["meaning"] = "旧释义原样保留，不在迁移时改判"
    raw = json.dumps(obj, ensure_ascii=False)
    state["steps"] = {key: {"raw": raw, "complete": False, "checks": [{**c, "complete": False} for c in obj["checks"]]}}
    state["responses"] = {"registry": raw_registry, key: raw}
    state["calls"] = [{"stage": stage, "state": "complete", "usage_complete": True,
        "actual_model": config.LLM_POSTWRITE_MODEL, "charged_or_reserved_tokens": 140, "elapsed_seconds": 1}
        for stage in ("registry", key)]
    state["usage"] = {"requests": 2, "tokens": 280}
    state["audit"] = audit_summary(state)
    return frozen, state, {key: messages(PROMPTS["audit"], payload, LEGACY_SOURCE_UNIT_FORMAT)}


def partial_meaning_fixture():
    from app.services.report_postwrite_inputs import fingerprint
    from app.services.report_postwrite_audit import meaning_payload, messages, bind_meanings
    from app.services.report_postwrite_pipeline import reuse_meaning_state, audit_summary
    frozen, legacy, requests = legacy_meaning_fixture(155)
    state = reuse_meaning_state(frozen, prompts=PROMPTS, previous=legacy, requests_by_stage=requests)
    for start in (30, 60):
        batch = state["registry"]["claims"][start:start + 30]
        payload = meaning_payload(REPORT, frozen["postwrite_packet"], batch)
        key = "meaning:" + fingerprint([c["id"] for c in batch])[:16]
        obj = {"checks": [response(c) for c in batch if c["id"] != "C074"], "not_checked": [], "scope_notes": []}
        raw = json.dumps(obj, ensure_ascii=False)
        state["steps"][key] = {**bind_meanings(obj, payload), "raw": raw}
        state["responses"][key] = raw
        state["calls"].append({"stage": key, "state": "complete", "usage_complete": True,
            "actual_model": config.LLM_POSTWRITE_MODEL, "charged_or_reserved_tokens": 140, "elapsed_seconds": 1})
        requests[key] = messages(PROMPTS["audit"], payload)
    state["usage"] = {"requests": 4, "tokens": 560}
    state["audit"] = audit_summary(state)
    return frozen, legacy, state, requests


async def fake_collect(messages, **kwargs):
    payload = json.loads(messages[-1]["content"])
    assert kwargs["max_http_attempts"] == 1
    assert len(kwargs["models"]) == 1
    callback = kwargs.get("on_attempt_event")
    if callback:
        await callback({"status": "completed", "usage": {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140}, "usage_complete": True, "finish_reason": "stop"})
    if "blocks" in payload:
        answer = {"claims": [{"id": "C1", "original": REPORT.split("\n\n")[1], "block_ids": ["B0002"], "kind": "inference", "review_refs": ["q1/r4"]}],
                  "coverage": [{"block_id": "B0001", "no_claim": True}, {"block_id": "B0002", "claim_ids": ["C1"]}], "tasks": []}
    elif "targets" in payload:
        answer = {"checks": [response(c) for c in payload["targets"]]}
    elif "task" in payload:
        answer = {"members": [{"player": p, "count": False, "sources": []} for p in payload["players"]]}
    else:
        return REPORT, "fake"
    return json.dumps(answer, ensure_ascii=False), "fake"


@contextmanager
def runtime(sess, collect=fake_collect):
    with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
        stack.enter_context(patch.object(config, "REPORT_POSTWRITE_ENABLED", True))
        stack.enter_context(patch.object(survey_service, "is_postwrite_report_enabled", return_value=True))
        stack.enter_context(patch.object(survey_service, "PostwriteRunStore", side_effect=lambda: RunStore(folder)))
        stack.enter_context(patch.object(survey_service, "get_session", side_effect=lambda sid: deepcopy(sess)))
        stack.enter_context(patch.object(survey_service, "_current_login", new=AsyncMock(return_value=None)))
        stack.enter_context(patch.object(survey_service, "require_session_access", side_effect=lambda sid, login, loader: deepcopy(sess)))
        stack.enter_context(patch.object(survey_service, "_assign_session_owner"))
        stack.enter_context(patch.object(survey_service, "_get_prompt_text", side_effect=lambda k: DEFAULT_PROMPTS[k]["current"]))
        stack.enter_context(patch.object(survey_service, "collect_chat_completion", new=collect))
        def save(sid, value):
            saved = deepcopy(value)
            sess.clear()
            sess.update(saved)
        stack.enter_context(patch.object(survey_service, "save_session", side_effect=save))
        stack.enter_context(patch.object(survey_service, "save_to_history"))
        yield


def events(raw):
    return [json.loads(e[6:].strip()) for e in raw if e.startswith("data: ")]


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_business_entry_loads_selected_writer_and_keeps_r_case_sources(self):
        from pathlib import Path
        from app.storage import prompts as prompt_storage

        narrative = "# 合成案例报告\n\nR2玩家认为卡牌有趣，同时希望模式常驻。"
        for selected_writer in (config.DEFAULT_POSTWRITE_WRITER, "CUSTOM_WRITER_KEPT_BY_SETTINGS"):
            with self.subTest(custom=selected_writer != config.DEFAULT_POSTWRITE_WRITER):
                sess = session()
                writer_requests = []

                async def collect(messages, **kwargs):
                    payload = json.loads(messages[-1]["content"])
                    raw, model = await fake_collect(messages, **kwargs)
                    if "blocks" in payload:
                        return json.dumps({"claims": [{"id": "C1", "original": narrative.split("\n\n")[1],
                            "block_ids": ["B0002"], "kind": "fact", "review_refs": ["q1/r2", "q2/r2"]}],
                            "coverage": [{"block_id": "B0001", "no_claim": True},
                                         {"block_id": "B0002", "claim_ids": ["C1"]}], "tasks": []}, ensure_ascii=False), model
                    if "targets" not in payload and "task" not in payload:
                        writer_requests.append(deepcopy(messages))
                        return narrative, model
                    return raw, model

                with tempfile.TemporaryDirectory(prefix="postwrite-entry-prompts-") as folder:
                    prompt_file = Path(folder) / "prompts.json"
                    stored = deepcopy(DEFAULT_PROMPTS)
                    stored["postwrite_writer_system"]["current"] = selected_writer
                    prompt_file.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
                    with runtime(sess, collect), patch.object(prompt_storage, "PROMPTS_FILE", str(prompt_file)), patch.object(
                        survey_service, "_get_prompt_text", side_effect=prompt_storage._get_prompt_text
                    ):
                        output = events([e async for e in survey_service.report_stream("writer-entry", None)])
                self.assertTrue(any(e["type"] == "report_done" for e in output), output)
                saved = report_versions.resolve_report_version(sess, 1)
                self.assertEqual(saved["report_md"], narrative)
                self.assertEqual(len(writer_requests), 1)
                effective_writer = selected_writer + "\n\n" + config.POSTWRITE_OPENING_REQUIREMENTS
                self.assertEqual(writer_requests[0][0], {"role": "system", "content": effective_writer})
                self.assertEqual(json.loads(writer_requests[0][-1]["content"]), {
                    **saved["input_snapshot"]["postwrite_packet"], "instruction": ""})
                state = saved["report_audit"]["checkpoint"]
                self.assertEqual(state["prompts"], {**PROMPTS, "writer": effective_writer})
                sources = [item["source"] for check in saved["report_audit"]["checks"] for item in check["sources"]]
                self.assertEqual([(s["ref"], s["source_row"], s["original"]) for s in sources],
                                 [("q1/r2", 2, "卡牌有趣"), ("q2/r2", 2, "希望常驻")])
                self.assertTrue(all(s["player"] == "P00002" for s in sources))

    async def test_old_checkpoint_keeps_writer_when_catalog_has_upgraded(self):
        sess = session()

        async def partial(messages, **kwargs):
            raw, model = await fake_collect(messages, **kwargs)
            return ('{"checks":[]}', model) if "targets" in json.loads(messages[-1]["content"]) else (raw, model)

        def old_catalog(key):
            return config.DEFAULT_POSTWRITE_WRITER_V1 if key == "postwrite_writer_system" else DEFAULT_PROMPTS[key]["current"]

        with runtime(sess, partial), patch.object(survey_service, "_get_prompt_text", side_effect=old_catalog), \
             patch.object(config, "POSTWRITE_OPENING_REQUIREMENTS", ""):
            _ = [e async for e in survey_service.report_stream("old-writer", None)]
        before = deepcopy(report_versions.resolve_report_version(sess, 1))
        self.assertEqual(before["report_audit"]["checkpoint"]["prompts"]["writer"], config.DEFAULT_POSTWRITE_WRITER_V1)
        self.assertEqual(before["report_audit"]["status"], "partial")
        requests = []

        async def resume(messages, **kwargs):
            requests.append(deepcopy(messages))
            self.assertIn("targets", json.loads(messages[-1]["content"]))
            return await fake_collect(messages, **kwargs)

        with runtime(sess, resume):
            output = events([e async for e in survey_service.report_stream("old-writer", None, retry_failed=True, base_version=1)])
        self.assertTrue(any(e["type"] == "report_done" for e in output), output)
        after = report_versions.resolve_report_version(sess, 2)
        self.assertTrue(requests)
        self.assertEqual(after["report_audit"]["status"], "complete")
        self.assertEqual(after["report_audit"]["checkpoint"]["prompts"], before["report_audit"]["checkpoint"]["prompts"])
        self.assertEqual(after["report_md"], before["report_md"])
        self.assertEqual(after["input_snapshot"], before["input_snapshot"])
        self.assertEqual(report_versions.resolve_report_version(sess, 1), before)

    async def test_list_alias_resume_reuses_meanings_and_reviewed_members(self):
        from app.services.report_postwrite_audit import bind_members
        from app.services.report_postwrite_inputs import fingerprint
        s = session()
        s["rows"][1][3], s["rows"][2][3], s["rows"][3][3] = "Good", "Bad", "Baik"
        s["confirmed_columns"][3].update(options=["好", "差"], value_aliases={"好": ["Good", "Baik"], "差": ["Bad"]})
        frozen = freeze_inputs(s)
        state = await run_pipeline(frozen, prompts=PROMPTS, collect=fake_collect)
        original_steps = deepcopy(state["steps"])
        group = {"question_key": "3", "values": ["好"]}
        common = {"mode": "subjective", "question_keys": ["1"], "population": "all", "group": group}
        tasks = [{**common, "id": "done", "predicate": "有明确反馈"},
                 {**common, "id": "pending", "predicate": "有明确反馈"},
                 {**common, "id": "objective", "mode": "objective", "question_keys": ["3"], "exact_values": ["好"]}]
        state["registry"]["tasks"] = tasks
        players = ["P00002", "P00004"]
        key = "members:done:" + fingerprint(players)[:12]
        saved = bind_members({"members": [{"player": players[0], "count": None}, {"player": players[1], "count": False}]}, tasks[0], players, [])
        state["steps"][key] = deepcopy(saved)
        requests = []
        async def quantity_only(messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            self.assertEqual([t["id"] for t in payload["tasks"]], ["pending"])
            self.assertEqual(payload["tasks"][0]["players"], players)
            requests.append(payload)
            return json.dumps({"tasks": [{"id": "pending", "members": [{"player": p, "count": False} for p in players]}]}), "fake"
        result = await run_pipeline(frozen, prompts=PROMPTS, previous=state, collect=quantity_only)
        self.assertEqual(result["audit"]["status"], "complete", result["stop_reason"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["steps"][key], saved)
        self.assertEqual(result["steps"]["count:objective"]["count"], 2)
        self.assertEqual(result["steps"]["count:done"]["review_status"], "ambiguous")
        self.assertEqual(result["draft"], state["draft"])
        for k, v in original_steps.items():
            self.assertEqual(result["steps"][k], v)

    async def test_reviewed_ambiguous_counts_resume_without_model_and_save_range(self):
        frozen = freeze_inputs(session())
        state = await run_pipeline(frozen, prompts=PROMPTS, collect=fake_collect)
        state["registry"]["tasks"] = [
            {"id": "T1", "predicate": "明确说卡牌有趣", "mode": "subjective", "question_keys": ["1"], "population": "all"},
            {"id": "T2", "predicate": "填写了非空回答", "mode": "subjective", "question_keys": ["1"], "population": "all"}]
        async def count_collect(messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            self.assertEqual([t["id"] for t in payload["tasks"]], ["T1"])
            await fake_collect(messages, **kwargs)
            return json.dumps({"tasks": [{"id": "T1", "members": [
                {"player": "P00002", "count": True, "sources": [{"ref": "q1/r2", "meaning": "卡牌有趣", "relation": "direct"}]},
                {"player": "P00003", "count": False, "sources": []},
                {"player": "P00004", "count": None, "sources": [{"ref": "q1/r4", "meaning": "规则熟悉", "relation": "ambiguous"}]}]}]}), "fake"
        result = await run_pipeline(frozen, prompts=PROMPTS, previous=state, collect=count_collect)
        self.assertEqual(result["audit"]["status"], "complete", result["stop_reason"])
        self.assertEqual((result["audit"]["counts_reviewed"], result["audit"]["counts_exact"], result["audit"]["counts_ambiguous"]), (2, 1, 1))
        self.assertEqual(result["steps"]["count:T2"]["count"], 2)
        forbidden = AsyncMock(side_effect=AssertionError("model forbidden"))
        again = await run_pipeline(frozen, prompts=PROMPTS, previous=result, collect=forbidden)
        forbidden.assert_not_called()
        self.assertEqual(again["audit"]["counts"], result["audit"]["counts"])
        snapshot = {"report_md": REPORT, "report_mode": "insight", "report_audit": again["audit"]}
        exported = prepare_report_markdown(snapshot, "evidence")
        self.assertEqual(exported, REPORT)
        self.assertNotIn("含歧义待人工确认", exported)
        self.assertNotIn("可能1—2/3人", exported)
        self.assertEqual(again["audit"]["counts_ambiguous"], 1)
        from app.services.report_postwrite_pipeline import audit_summary
        del again["steps"]["count:T1"]
        incomplete = audit_summary(again)
        self.assertEqual(incomplete["status"], "partial")
        self.assertEqual(incomplete["counts_unreviewed"], 1)

    async def test_partial_batch_reuses_89_and_repairs_only_gap_before_untouched(self):
        from app.services.report_postwrite_pipeline import reuse_meaning_state
        frozen, _, old, requests = partial_meaning_fixture()
        before = deepcopy(old)
        state = reuse_meaning_state(frozen, prompts=PROMPTS, previous=old, requests_by_stage=requests, allow_partial=True)
        self.assertEqual(state["audit"]["checked_claims"], 89)
        self.assertEqual(state["steps"], old["steps"])
        self.assertEqual(state["responses"], old["responses"])
        called = []
        async def collect(messages, **kwargs):
            called.append([c["id"] for c in json.loads(messages[-1]["content"])["targets"]])
            return await fake_collect(messages, **kwargs)
        result = await run_pipeline(frozen, prompts=PROMPTS, previous=state, collect=collect)
        self.assertEqual(called, [["C074"], [f"C{i:03d}" for i in range(91,121)],
                                  [f"C{i:03d}" for i in range(121,151)], [f"C{i:03d}" for i in range(151,156)]])
        self.assertEqual(result["audit"]["checked_claims"], 155)
        self.assertEqual(result["audit"]["status"], "complete")
        for key, step in old["steps"].items():
            self.assertEqual(result["steps"][key], step)
        self.assertEqual(result["registry"], old["registry"])
        self.assertEqual(old, before)

    async def test_gap_failure_stops_without_repeating_any_completed_claim(self):
        frozen, _, state, _ = partial_meaning_fixture()
        called = []
        async def collect(messages, **kwargs):
            called.append([c["id"] for c in json.loads(messages[-1]["content"])["targets"]])
            await fake_collect(messages, **kwargs)
            return '{"checks":[]}', "fake"
        result = await run_pipeline(frozen, prompts=PROMPTS, previous=state, collect=collect)
        self.assertEqual(called, [["C074"]])
        self.assertEqual(result["audit"]["checked_claims"], 89)
        self.assertEqual(result["audit"]["status"], "partial")
        for key, step in state["steps"].items():
            self.assertEqual(result["steps"][key], step)

    async def test_partial_reuse_rejects_modified_judgment_or_invalid_returned_item(self):
        from app.services.report_postwrite_pipeline import reuse_meaning_state
        from app.services.report_postwrite_audit import bind_meanings
        frozen, _, old, requests = partial_meaning_fixture()
        key = list(old["steps"])[-1]
        changed = deepcopy(old)
        changed["steps"][key]["checks"][0]["verdict"] = "issue"
        with self.assertRaisesRegex(ValueError, "禁止改判"):
            reuse_meaning_state(frozen, prompts=PROMPTS, previous=changed, requests_by_stage=requests, allow_partial=True)
        changed = deepcopy(old)
        obj = json.loads(changed["responses"][key]); obj["checks"][0]["sources"] = []
        raw = json.dumps(obj, ensure_ascii=False)
        changed["responses"][key] = raw
        changed["steps"][key] = {**bind_meanings(obj, json.loads(requests[key][-1]["content"])), "raw": raw}
        with self.assertRaisesRegex(ValueError, "无效判断"):
            reuse_meaning_state(frozen, prompts=PROMPTS, previous=changed, requests_by_stage=requests, allow_partial=True)

    async def test_reused_thirty_meanings_continue_at_31_without_rewriting_judgments(self):
        from app.services.report_postwrite_pipeline import reuse_meaning_state
        frozen, old, requests = legacy_meaning_fixture()
        before = deepcopy(old)
        state = reuse_meaning_state(frozen, prompts=PROMPTS, previous=old, requests_by_stage=requests)
        self.assertEqual(old, before)
        self.assertEqual(state["audit"]["checked_claims"], 30)
        self.assertEqual(state["responses"], old["responses"])
        self.assertEqual(len(state["registry"]["legacy_not_checked"]), 5)
        key = next(iter(requests))
        for raw, bound in zip(json.loads(old["responses"][key])["checks"], state["steps"][key]["checks"]):
            self.assertEqual(bound["verdict"], raw["verdict"])
            self.assertEqual(bound["sources"][0]["meaning"], raw["sources"][0]["meaning"])
        self.assertEqual(len(state["steps"][key]["scope_notes"]), 2)
        called = []
        async def collect(messages, **kwargs):
            called.extend(c["id"] for c in json.loads(messages[-1]["content"])["targets"])
            return await fake_collect(messages, **kwargs)
        result = await run_pipeline(frozen, prompts=PROMPTS, previous=state, collect=collect)
        self.assertEqual(called, ["C031"])
        self.assertEqual(result["audit"]["status"], "complete")
        self.assertEqual(result["usage"]["requests"], 3)

    async def test_meaning_reuse_refuses_changed_request_or_real_missing_check(self):
        from app.services.report_postwrite_pipeline import reuse_meaning_state
        frozen, old, requests = legacy_meaning_fixture()
        key = next(iter(requests))
        changed = deepcopy(requests); changed[key][-1]["content"] = "{}"
        with self.assertRaises((ValueError, KeyError)):
            reuse_meaning_state(frozen, prompts=PROMPTS, previous=old, requests_by_stage=changed)
        missing = deepcopy(old); obj = json.loads(missing["responses"][key]); obj["checks"].pop()
        missing["responses"][key] = missing["steps"][key]["raw"] = json.dumps(obj)
        with self.assertRaisesRegex(ValueError, "实际缺项"):
            reuse_meaning_state(frozen, prompts=PROMPTS, previous=missing, requests_by_stage=requests)

    async def test_resumed_objective_alias_counts_without_model_count_or_registration(self):
        from app.services.report_postwrite_inputs import fingerprint
        from app.services.report_postwrite_audit import validate_registry
        frozen = freeze_inputs(session())
        packet = frozen["postwrite_packet"]
        next(q for q in packet["questions"] if q["key"] == "3")["definition"]["options"] = {"Good": "好", "Baik": "好", "Bad": "差"}
        duplicate = deepcopy(next(u for u in packet["source_units"] if u["ref"] == "q3/r2"))
        duplicate["ref"] = "q3/r2-duplicate"
        packet["source_units"].append(duplicate)
        frozen["packet_fingerprint"] = fingerprint(packet)
        state = initial_draft_state(frozen, prompts=PROMPTS, draft=REPORT)
        raw, _ = await fake_collect([{"content": '{"blocks":[]}'}], models=["fake"], max_http_attempts=1)
        registry = json.loads(raw)
        registry["claims"][0]["count_task_ids"] = ["T1"]
        registry["tasks"] = [{"id": "T1", "mode": "objective", "question_keys": ["3"], "population": "all",
            "group": {"question_key": "3", "values": ["Good"]}, "exact_values": ["Good", "Baik", "好"],
            "reported_count": 2, "reported_denominator": 2, "reported_percent": 100}]
        state["registry"] = validate_registry(registry, REPORT, packet)
        self.assertTrue(state["registry"]["complete"])
        async def meaning_only(messages, **kwargs):
            self.assertIn("targets", json.loads(messages[-1]["content"]))
            return await fake_collect(messages, **kwargs)
        result = await run_pipeline(frozen, prompts=PROMPTS, previous=state, collect=meaning_only)
        self.assertEqual(result["audit"]["status"], "complete", result["stop_reason"])
        self.assertEqual(result["usage"]["requests"], 1)
        count = result["steps"]["count:T1"]
        self.assertEqual((count["count"], count["denominator"], count["percent"]), (2, 2, 100))
        self.assertEqual(count["issues"], [])

    async def saved_registry_with_note(self):
        frozen = freeze_inputs(session())
        async def collect(messages, **kwargs):
            raw, _ = await fake_collect(messages, **kwargs)
            obj = json.loads(raw); obj["not_checked"] = ["合理解释已登记，原意尚未核验"]
            return json.dumps(obj, ensure_ascii=False), config.LLM_POSTWRITE_MODEL
        previous = await run_pipeline(frozen, prompts=PROMPTS, collect=collect, stop_after="registry",
                                     previous=initial_draft_state(frozen, prompts=PROMPTS, draft=REPORT))
        self.assertFalse(previous["registry"]["complete"])
        return frozen, previous

    async def test_reused_registration_preserves_notes_and_history_without_calls(self):
        frozen, previous = await self.saved_registry_with_note()
        before = deepcopy(previous)
        state = reuse_registry_state(frozen, prompts=PROMPTS, previous=previous,
                                     legacy_note_dispositions=note_review(previous["registry_raw"]))
        forbidden = AsyncMock(side_effect=AssertionError("model forbidden"))
        result = await run_pipeline(frozen, prompts=PROMPTS, previous=state, stop_after="registry", collect=forbidden)
        forbidden.assert_not_called()
        self.assertEqual(previous, before)
        self.assertEqual(result["calls"], previous["calls"])
        self.assertEqual(result["registry_raw"], previous["registry_raw"])
        self.assertEqual(result["audit"]["registration_status"], "complete")
        self.assertEqual(result["audit"]["status"], "partial")
        self.assertEqual(result["audit"]["checked_claims"], 0)
        self.assertEqual(result["usage"]["tokens"], previous["usage"]["tokens"])
        self.assertEqual(len(result["audit"]["verification_notes"]), 1)

    async def test_reused_registration_continues_directly_into_meaning(self):
        frozen, previous = await self.saved_registry_with_note()
        state = reuse_registry_state(frozen, prompts=PROMPTS, previous=previous,
                                     legacy_note_dispositions=note_review(previous["registry_raw"]))
        state["registration_only"] = True
        seen = []
        async def collect(messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            self.assertIn("targets", payload)
            seen.extend(c["id"] for c in payload["targets"])
            return await fake_collect(messages, **kwargs)
        result = await run_pipeline(frozen, prompts=PROMPTS, previous=state, collect=collect)
        self.assertEqual(seen, ["C1"])
        self.assertEqual(result["audit"]["status"], "complete")
        self.assertNotIn("registration_only", result)
        self.assertEqual(result["usage"]["requests"], 2)  # one inherited + one mock audit
        self.assertEqual(len(result["audit"]["verification_notes"]), 1)

    async def test_unclassified_or_damaged_reuse_never_reregisters(self):
        frozen, previous = await self.saved_registry_with_note()
        unresolved = reuse_registry_state(frozen, prompts=PROMPTS, previous=previous)
        valid = reuse_registry_state(frozen, prompts=PROMPTS, previous=previous,
                                     legacy_note_dispositions=note_review(previous["registry_raw"]))
        valid["registry_raw"] += " "
        for state in (unresolved, valid):
            forbidden = AsyncMock(side_effect=AssertionError("no auto registration"))
            result = await run_pipeline(frozen, prompts=PROMPTS, previous=state, collect=forbidden)
            forbidden.assert_not_called()
            self.assertEqual(result["audit"]["status"], "partial")

    async def test_reuse_does_not_import_other_audit_or_changed_policy(self):
        frozen, previous = await self.saved_registry_with_note()
        with self.assertRaises(ValueError):
            reuse_registry_state(frozen, prompts={**PROMPTS, "audit": "changed"}, previous=previous)
        previous["steps"] = {"meaning:old": {"checks": []}}
        with self.assertRaises(ValueError):
            reuse_registry_state(frozen, prompts=PROMPTS, previous=previous)

    async def test_registry_only_uses_64k_and_never_calls_auditor(self):
        frozen = freeze_inputs(session())
        seed = initial_draft_state(frozen, prompts=PROMPTS, draft=REPORT)
        seen = []
        async def collect(messages, **kwargs):
            self.assertIn("blocks", json.loads(messages[-1]["content"]))
            self.assertEqual(kwargs["max_tokens"], 64000)
            seen.append(kwargs)
            return await fake_collect(messages, **kwargs)
        result = await run_pipeline(frozen, prompts=PROMPTS, previous=seed, collect=collect,
                                    stop_after="registry", caller_options={"max_calls": 1, "max_tokens": 200000})
        self.assertEqual(len(seen), 1)
        self.assertTrue(result["registry"]["complete"])
        self.assertTrue(result["registration_only"])
        self.assertEqual(result["audit"]["status"], "partial")
        self.assertEqual(result["audit"]["checked_claims"], 0)
        self.assertEqual(result["steps"], {})
        self.assertEqual(result["draft"], REPORT)
        self.assertIn("登记阶段已完成", result["stop_reason"])

    async def test_stage_cap_does_not_change_writer_meaning_or_count(self):
        seen = []
        async def collect(messages, **kwargs):
            seen.append(kwargs["max_tokens"])
            return "ok", "fake"
        caller = BoundedCaller(collect=collect, output_tokens=12000)
        for stage in ("writer", "registry", "meaning:1", "members:1"):
            await caller.call(stage, [{"content": "small"}])
        self.assertEqual(seen, [12000, 64000, 12000, 12000])
        self.assertEqual([r["output_cap"] for r in caller.ledger], seen)

    async def test_registry_reserves_64k_before_network(self):
        collect = AsyncMock()
        caller = BoundedCaller(collect=collect, output_tokens=12000, max_tokens=50000)
        with self.assertRaises(BudgetStop):
            await caller.call("registry", [{"content": "small"}])
        collect.assert_not_called()

    async def test_registry_only_requires_draft_and_valid_stop(self):
        collect = AsyncMock()
        for stop in ("registry", "meaning"):
            with self.assertRaises(ValueError):
                await run_pipeline(freeze_inputs(session()), prompts=PROMPTS, collect=collect, stop_after=stop)
        collect.assert_not_called()

    async def test_registry_only_does_not_accept_truncation(self):
        frozen = freeze_inputs(session())
        async def collect(messages, **kwargs):
            await kwargs["on_attempt_event"]({"finish_reason": "max_tokens", "usage_complete": True,
                                               "usage": {"total_tokens": 65000}})
            return '{"claims":[]}', "fake"
        result = await run_pipeline(frozen, prompts=PROMPTS, collect=collect, stop_after="registry",
                                    previous=initial_draft_state(frozen, prompts=PROMPTS, draft=REPORT))
        self.assertIsNone(result["registry"])
        self.assertEqual(result["audit"]["status"], "partial")
        self.assertEqual(result["usage"]["requests"], 1)
        self.assertIn("截断", result["stop_reason"])

    async def test_registry_only_does_not_accept_invalid_coverage(self):
        frozen = freeze_inputs(session())
        async def collect(messages, **kwargs):
            return '{"claims":[],"tasks":[],"coverage":[]}', "fake"
        result = await run_pipeline(frozen, prompts=PROMPTS, collect=collect, stop_after="registry",
                                    previous=initial_draft_state(frozen, prompts=PROMPTS, draft=REPORT))
        self.assertFalse(result["registry"]["complete"])
        self.assertEqual(result["audit"]["status"], "partial")
        self.assertEqual(result["usage"]["requests"], 1)

    async def test_registry_only_no_claim_report_still_not_audited(self):
        frozen = freeze_inputs(session())
        async def collect(messages, **kwargs):
            return json.dumps({"claims": [], "tasks": [], "coverage": [
                {"block_id": "B0001", "no_claim": True}]}), "fake"
        result = await run_pipeline(frozen, prompts=PROMPTS, collect=collect, stop_after="registry",
                                    previous=initial_draft_state(frozen, prompts=PROMPTS, draft="# 标题"))
        self.assertTrue(result["registry"]["complete"])
        self.assertEqual(result["audit"]["status"], "partial")

    async def test_reused_draft_starts_at_registry_without_prior_answers(self):
        frozen = freeze_inputs(session())
        seed = initial_draft_state(frozen, prompts=PROMPTS, draft=REPORT)
        seen = []
        async def collect(messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            self.assertTrue("blocks" in payload or "targets" in payload)
            seen.append(payload)
            return await fake_collect(messages, **kwargs)
        result = await run_pipeline(frozen, prompts=PROMPTS, previous=seed, collect=collect)
        self.assertEqual(result["draft"], REPORT)
        self.assertEqual(result["audit"]["status"], "complete")
        self.assertEqual(len(seen), 2)
        self.assertIsNone(seed["registry"])
        self.assertEqual(seed["steps"], {})
        seed["draft"] += "tampered"
        failed = await run_pipeline(frozen, prompts=PROMPTS, previous=seed, collect=collect)
        self.assertEqual(failed["usage"]["requests"], 0)
        self.assertEqual(len(seen), 2)

    async def test_incomplete_first_batch_stops_further_spend(self):
        frozen = freeze_inputs(session())
        seed = initial_draft_state(frozen, prompts=PROMPTS, draft=REPORT)
        async def collect(messages, **kwargs):
            raw, model = await fake_collect(messages, **kwargs)
            if "blocks" in json.loads(messages[-1]["content"]):
                obj = json.loads(raw)
                obj["claims"].append({**obj["claims"][0], "id": "C2"})
                obj["coverage"][1]["claim_ids"].append("C2")
                return json.dumps(obj), model
            return '{"checks":[]}', model
        with patch.object(config, "POSTWRITE_CLAIMS_PER_BATCH", 1):
            seed = initial_draft_state(frozen, prompts=PROMPTS, draft=REPORT)
            result = await run_pipeline(frozen, prompts=PROMPTS, previous=seed, collect=collect)
        self.assertEqual(result["usage"]["requests"], 2)
        self.assertEqual(result["audit"]["status"], "partial")
        self.assertIn("停止后续调用", result["stop_reason"])

    async def test_valid_issue_verdict_does_not_stop_other_claims(self):
        async def collect(messages, **kwargs):
            raw, model = await fake_collect(messages, **kwargs)
            if "targets" in json.loads(messages[-1]["content"]):
                obj = json.loads(raw)
                obj["checks"][0]["verdict"] = "issue"
                return json.dumps(obj), model
            return raw, model
        result = await run_pipeline(freeze_inputs(session()), prompts=PROMPTS, collect=collect)
        self.assertEqual(result["audit"]["status"], "complete")
        self.assertEqual(result["audit"]["checks"][0]["verdict"], "issue")

    async def test_count_tasks_share_one_request_but_keep_question_scope(self):
        count_calls = []
        async def collect(messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            raw, model = await fake_collect(messages, **kwargs)
            if "blocks" in payload:
                reg = json.loads(raw)
                reg["tasks"] = [{"id": "T1", "question_keys": ["1"], "mode": "subjective", "population": "all", "predicate": "有趣"},
                                {"id": "T2", "question_keys": ["2"], "mode": "subjective", "population": "all", "predicate": "希望常驻"},
                                {"id": "T3", "question_keys": ["3"], "mode": "objective", "population": "answered", "exact_values": ["好"], "reported_count": 2}]
                return json.dumps(reg), model
            if "tasks" in payload:
                count_calls.append(payload)
                answers = []
                for task in payload["tasks"]:
                    self.assertTrue(all(ref.startswith("q1/") if task["id"] == "T1" else ref.startswith("q2/") for ref in task["allowed_refs"]))
                    members = [{"player": p, "count": False, "sources": []} for p in task["players"]]
                    answers.append({"id": task["id"], "members": members})
                return json.dumps({"tasks": answers}), model
            return raw, model
        result = await run_pipeline(freeze_inputs(session()), prompts=PROMPTS, collect=collect)
        self.assertEqual(result["audit"]["status"], "complete", result)
        self.assertEqual(len(count_calls), 1)
        counts = {c["id"]: c for c in result["audit"]["counts"]}
        self.assertEqual(counts["T3"]["count"], 2)
        self.assertEqual(counts["T3"]["denominator"], 3)
        self.assertEqual(result["usage"]["requests"], 4)

    async def test_not_checked_marker_cannot_be_hidden_by_complete_checks(self):
        async def collect(messages, **kwargs):
            raw, model = await fake_collect(messages, **kwargs)
            if "targets" in json.loads(messages[-1]["content"]):
                answer = json.loads(raw)
                answer["not_checked"] = ["C1尚未核实"]
                return json.dumps(answer), model
            return raw, model
        result = await run_pipeline(freeze_inputs(session()), prompts=PROMPTS, collect=collect)
        self.assertEqual(result["audit"]["status"], "partial")

    async def test_history_resume_from_frozen_input_without_live_rows(self):
        sess = session()
        async def partial(messages, **kwargs):
            raw, model = await fake_collect(messages, **kwargs)
            return ('{"checks":[]}', model) if "targets" in json.loads(messages[-1]["content"]) else (raw, model)
        with runtime(sess, partial):
            _ = [e async for e in survey_service.report_stream("history-fixture", None)]
        archive = deepcopy(sess)
        archive["id"] = "archive-one"
        base = report_versions.resolve_report_version(archive, 1)
        sess.pop("rows")
        sess.update(rerun_target_history_id="archive-one", postwrite_history_rerun=True)
        def mutate(callback):
            return callback([archive])
        with runtime(sess), patch.object(survey_service, "_load_history", return_value=[archive]), patch.object(survey_service, "_find_history_for_login", side_effect=lambda history, ident, login: next((x for x in history if x.get("id") == ident), None)), patch("app.storage.history.mutate_history", side_effect=mutate):
            result = events([e async for e in survey_service.report_stream("restore", None, retry_failed=True, base_version=1)])
        self.assertTrue(any(e["type"] == "report_done" and e.get("history_id") == "archive-one" for e in result), result)
        self.assertEqual(report_versions.resolve_report_version(archive, 1), base)
        self.assertEqual(report_versions.resolve_report_version(archive, 2)["report_audit"]["status"], "complete")

    async def test_truncated_response_not_certified_and_raw_diagnostic_retained(self):
        async def collect(messages, **kwargs):
            raw, model = await fake_collect(messages, **kwargs)
            if "blocks" in json.loads(messages[-1]["content"]):
                return '{"claims":[', model
            return raw, model
        result = await run_pipeline(freeze_inputs(session()), prompts=PROMPTS, collect=collect)
        self.assertEqual(result["audit"]["status"], "partial")
        self.assertEqual(result["responses"]["registry"], '{"claims":[')
        self.assertEqual(result["usage"]["requests"], 2)

    async def test_full_offline_pipeline_and_usage(self):
        result = await run_pipeline(freeze_inputs(session()), prompts=PROMPTS, collect=fake_collect)
        self.assertEqual(result["audit"]["status"], "complete", result)
        self.assertEqual(result["draft"], REPORT)
        self.assertEqual(result["usage"]["requests"], 3)
        self.assertEqual(result["usage"]["tokens"], 420)

    async def test_partial_meaning_resume_without_writer_or_registry(self):
        calls = []
        async def partial(messages, **kwargs):
            p = json.loads(messages[-1]["content"])
            if "targets" in p:
                await fake_collect(messages, **kwargs)
                return '{"checks":[]}', "fake"
            return await fake_collect(messages, **kwargs)
        frozen = freeze_inputs(session())
        result = await run_pipeline(frozen, prompts=PROMPTS, collect=partial)
        self.assertEqual(result["audit"]["status"], "partial")
        async def resume(messages, **kwargs):
            p = json.loads(messages[-1]["content"])
            self.assertIn("targets", p)
            calls.append(p)
            return await fake_collect(messages, **kwargs)
        finished = await run_pipeline(frozen, prompts=PROMPTS, collect=resume, previous=result)
        self.assertEqual(finished["audit"]["status"], "complete", finished)
        self.assertEqual(len(calls), 1)
        self.assertEqual(finished["draft"], result["draft"])

    async def test_completed_resume_makes_no_requests(self):
        frozen = freeze_inputs(session())
        first = await run_pipeline(frozen, prompts=PROMPTS, collect=fake_collect)
        collector = AsyncMock(side_effect=AssertionError("unexpected request"))
        again = await run_pipeline(frozen, prompts=PROMPTS, collect=collector, previous=first)
        self.assertEqual(again["audit"]["status"], "complete")
        collector.assert_not_called()

    async def test_budget_stops_preserving_draft(self):
        result = await run_pipeline(freeze_inputs(session()), prompts=PROMPTS, collect=fake_collect, caller_options={"max_calls": 1})
        self.assertEqual(result["draft"], REPORT)
        self.assertEqual(result["audit"]["status"], "partial")
        self.assertEqual(result["usage"]["requests"], 1)

    async def test_small_token_budget_stops_before_request(self):
        collector = AsyncMock()
        caller = BoundedCaller(collect=collector, max_tokens=1000)
        with self.assertRaises(BudgetStop):
            await caller.call("x", [{"content": "abc"}])
        collector.assert_not_called()

    async def test_rules_change_does_not_reuse_checkpoint(self):
        frozen = freeze_inputs(session())
        first = await run_pipeline(frozen, prompts=PROMPTS, collect=fake_collect)
        with self.assertRaises(ValueError):
            await run_pipeline(frozen, prompts={**PROMPTS, "audit": "changed"}, previous=first, collect=fake_collect)

    async def test_cancel_preserves_draft_and_incomplete_state(self):
        reached = asyncio.Event()
        async def collect(messages, **kwargs):
            if "blocks" in json.loads(messages[-1]["content"]):
                reached.set()
                await asyncio.Event().wait()
            return await fake_collect(messages, **kwargs)
        task = asyncio.create_task(run_pipeline(freeze_inputs(session()), prompts=PROMPTS, collect=collect))
        await reached.wait()
        task.cancel()
        result = await task
        self.assertEqual(result["draft"], REPORT)
        self.assertEqual(result["audit"]["status"], "partial")
        self.assertEqual(result["stop_reason"], "cancelled")

    async def test_sse_save_new_version_resume_and_original_immutable(self):
        sess = session()
        async def partial(messages, **kwargs):
            result = await fake_collect(messages, **kwargs)
            return ('{"checks":[]}', "fake") if "targets" in json.loads(messages[-1]["content"]) else result
        with runtime(sess, partial):
            output = events([e async for e in survey_service.report_stream("postwrite-test", None)])
        self.assertTrue(any(e["type"] == "report_done" for e in output), output)
        base = report_versions.resolve_report_version(sess, 1)
        self.assertEqual(base["report_audit"]["status"], "partial")
        with runtime(sess):
            output = events([e async for e in survey_service.report_stream("postwrite-test", None, retry_failed=True, base_version=1)])
        self.assertTrue(any(e["type"] == "report_done" for e in output), output)
        self.assertEqual(report_versions.resolve_report_version(sess, 1), base)
        second = report_versions.resolve_report_version(sess, 2)
        self.assertEqual(second["report_audit"]["status"], "complete")
        self.assertEqual(second["report_md"], base["report_md"])
        self.assertEqual(second["input_snapshot"], base["input_snapshot"])
        self.assertIn("待人工", second["report_audit"]["notice"].replace("供人工审阅", "待人工"))
        self.assertFalse(partial_rerun_capability(sess, second)["available"])
        self.assertNotIn("## 核验提示", prepare_report_markdown(second, "evidence"))
        self.assertEqual(prepare_report_markdown(second, "body"), second["report_md"])
        self.assertIn("原文", second["qa_context_md"])

    async def test_old_versions_do_not_switch_when_flag_enabled(self):
        with patch.object(survey_service, "is_postwrite_report_enabled", return_value=True):
            self.assertFalse(survey_service._use_postwrite_engine({"report_md": "old", "report_mode": "insight"}))
            self.assertFalse(survey_service._use_postwrite_engine({"report_mode": "quick"}))
            self.assertFalse(survey_service._use_postwrite_engine({"report_mode": "statistics"}))
            self.assertTrue(survey_service._use_postwrite_engine(session()))

    async def test_saved_engine_disabled_does_not_fallback(self):
        sess = session()
        with runtime(sess):
            _ = [e async for e in survey_service.report_stream("flag-test", None)]
        with runtime(sess), patch.object(survey_service, "is_postwrite_report_enabled", return_value=False), patch.object(survey_service, "_full_report_stream") as old:
            result = events([e async for e in survey_service.report_stream("flag-test", None, generation_kind="regenerate", base_version=1)])
        self.assertTrue(any(e["type"] == "error" for e in result))
        old.assert_not_called()


class WriterOutputLimitTests(unittest.IsolatedAsyncioTestCase):
    @contextmanager
    def truncated_messages(self):
        from app.integrations import llm_client
        requests = []
        real_client = httpx.AsyncClient
        wire = [
            {"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 415768, "output_tokens": 0}}},
            {"type": "content_block_delta", "delta": {"text": "PRIVATE_PARTIAL_DO_NOT_SAVE"}},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 16000}},
            {"type": "message_stop"},
        ]
        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                content="".join("data: " + json.dumps(item) + "\n\n" for item in wire))
        with ExitStack() as stack:
            stack.enter_context(patch.object(llm_client, "LLM_API_BASE", "https://model.invalid/v1"))
            stack.enter_context(patch.object(llm_client, "LLM_API_KEY", "sk-PRIVATE_DO_NOT_LOG"))
            stack.enter_context(patch.object(llm_client, "FEISHU_LOGIN_REQUIRED", False))
            stack.enter_context(patch.object(llm_client, "LLM_REPORT_MAX_ATTEMPTS", 1))
            stack.enter_context(patch.object(llm_client, "_protocol_order", return_value=["messages"]))
            stack.enter_context(patch.object(httpx, "AsyncClient", side_effect=lambda **kw: real_client(
                transport=httpx.MockTransport(respond), **kw)))
            yield llm_client.collect_chat_completion, requests

    async def test_writer_64k_is_independent_of_registry_and_audit_limits(self):
        seen = []
        async def collect(messages, **kwargs):
            seen.append(kwargs["max_tokens"])
            return "ok", "fake"
        with patch.multiple(config, POSTWRITE_OUTPUT_TOKENS=16000, POSTWRITE_REGISTRY_OUTPUT_TOKENS=64000):
            caller = BoundedCaller(collect=collect)
            for stage in ("writer", "registry", "meaning:1", "members:1"):
                await caller.call(stage, [{"content": "small"}])
        self.assertEqual(seen, [64000, 64000, 16000, 16000])
        custom = BoundedCaller(writer_output_tokens=24000)
        self.assertEqual((custom.writer_output_tokens, custom.output_tokens), (24000, 16000))
        # Explicit acceptance callers retain their existing output-token override.
        self.assertEqual(BoundedCaller(output_tokens=12000).writer_output_tokens, 12000)

    async def test_writer_reserves_the_full_cap_without_raising_total_budget(self):
        collect = AsyncMock()
        caller = BoundedCaller(collect=collect, max_tokens=50000)
        with self.assertRaises(BudgetStop):
            await caller.call("writer", [{"content": "small"}])
        collect.assert_not_called()
        self.assertEqual(caller.ledger, [])
        self.assertEqual(caller.max_tokens, 50000)

    async def test_real_messages_truncation_keeps_diagnostic_and_usage_but_not_partial_text(self):
        checkpoints = []
        async def save(state):
            checkpoints.append(deepcopy(state))
        with self.truncated_messages() as (collect, requests):
            result = await run_pipeline(freeze_inputs(session()), prompts=PROMPTS, collect=collect,
                on_checkpoint=save, caller_options={"writer_output_tokens": 16000})
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["max_tokens"], 16000)
        self.assertEqual(result["draft"], "")
        self.assertEqual(result["steps"], {})
        self.assertEqual(result["audit"]["checked_claims"], 0)
        self.assertEqual(result["usage"]["tokens"], 431768)
        self.assertTrue(result["usage"]["usage_complete"])
        self.assertEqual(result["failure"]["error_category"], "output_truncated")
        self.assertEqual(result["failure"]["http_status"], 200)
        self.assertEqual(result["failure"]["output_cap"], 16000)
        self.assertIn("截断", result["stop_reason"])
        self.assertIn("16,000", result["stop_reason"])
        self.assertEqual(checkpoints[-1], result)
        rendered = json.dumps(result, ensure_ascii=False)
        for secret in ("PRIVATE_PARTIAL_DO_NOT_SAVE", "PRIVATE_DO_NOT_LOG", "model.invalid"):
            self.assertNotIn(secret, rendered)

    async def test_service_emits_chinese_failure_usage_and_timestamped_log(self):
        sess, terminal = session(), io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            store = RunStore(folder)
            with self.truncated_messages() as (collect, requests), runtime(sess, collect), \
                 patch.object(config, "POSTWRITE_WRITER_OUTPUT_TOKENS", 16000, create=True), \
                 patch.object(survey_service, "PostwriteRunStore", return_value=store), redirect_stdout(terminal):
                output = events([e async for e in survey_service.report_stream("synthetic-limit", None)])
            saved = store.load(sess["postwrite_run_id"])["state"]
        self.assertEqual(len(requests), 1)
        self.assertFalse(any(e["type"] == "report_done" for e in output))
        self.assertNotIn("report_versions", sess)
        error = next(e for e in output if e["type"] == "error")
        for text in ("截断", "16,000", "431,768"):
            self.assertIn(text, error["message"])
        status = next(e for e in output if e["type"] == "report_llm_status")
        self.assertEqual(status["report_llm_usage"]["totals"]["total_tokens"], 431768)
        self.assertEqual(status["report_llm_usage"]["totals"]["active_calls"], 0)
        self.assertLess(output.index(status), output.index(error))
        self.assertEqual(error["failure"], saved["failure"])
        log = terminal.getvalue()
        self.assertRegex(log, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\+08:00\]")
        for text in ("[失败]", "[问卷报告]", "初稿", "截断", "HTTP 状态：200", "431768"):
            self.assertIn(text, log)
        for secret in ("PRIVATE_PARTIAL_DO_NOT_SAVE", "PRIVATE_DO_NOT_LOG", "model.invalid"):
            self.assertNotIn(secret, log + json.dumps(output, ensure_ascii=False))

    async def test_unclassified_error_is_safe_and_unknown_usage_is_not_claimed_zero(self):
        async def collect(messages, **kwargs):
            raise RuntimeError("sk-PRIVATE_CREDENTIAL vendor-private-body")
        sess, terminal = session(), io.StringIO()
        with runtime(sess, collect), redirect_stdout(terminal):
            output = events([e async for e in survey_service.report_stream("synthetic-unknown", None)])
        error = next(e for e in output if e["type"] == "error")
        self.assertIn("用量尚未完整返回", error["message"])
        self.assertEqual(error["failure"]["error_category"], "unknown_error")
        self.assertIsNone(error["failure"]["http_status"])
        self.assertNotIn("PRIVATE_CREDENTIAL", terminal.getvalue() + json.dumps(output))
        self.assertNotIn("vendor-private-body", terminal.getvalue() + json.dumps(output))

    async def test_successful_report_has_no_failure_and_unchanged_prompts_and_audit(self):
        seen = []
        async def collect(messages, **kwargs):
            seen.append(kwargs["max_tokens"])
            return await fake_collect(messages, **kwargs)
        result = await run_pipeline(freeze_inputs(session()), prompts=PROMPTS, collect=collect)
        self.assertEqual(seen, [64000, 64000, 16000])
        self.assertEqual(result["draft"], REPORT)
        self.assertEqual(result["prompts"], PROMPTS)
        self.assertNotIn("failure", result)
        self.assertEqual(result["audit"]["status"], "complete")

    async def test_retry_budget_stop_is_not_replaced_by_an_older_failure(self):
        async def failed(messages, **kwargs):
            raise RuntimeError("private-upstream-body")
        frozen = freeze_inputs(session())
        previous = await run_pipeline(frozen, prompts=PROMPTS, collect=failed)
        self.assertIn("failure", previous)
        forbidden = AsyncMock(side_effect=AssertionError("no automatic retry"))
        result = await run_pipeline(frozen, prompts=PROMPTS, previous=previous, collect=forbidden,
                                    caller_options={"max_calls": 10})
        self.assertIn("上次请求用量不完整", result["stop_reason"])
        self.assertNotIn("failure", result)
        self.assertEqual(len(result["calls"]), 1)
        forbidden.assert_not_called()


class RuntimeBudgetPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_continues_beyond_old_limits_and_unknown_usage(self):
        history = [{"stage": "meaning:old", "state": "complete", "usage_complete": True,
                    "usage": {"total_tokens": 200000}, "charged_or_reserved_tokens": 200000,
                    "elapsed_seconds": 300} for _ in range(18)]
        history.append({"stage": "registry", "state": "failed", "usage_complete": False,
                        "charged_or_reserved_tokens": 64000, "elapsed_seconds": 480})
        async def collect(messages, **kwargs):
            await asyncio.sleep(0)
            await kwargs["on_attempt_event"]({"status": "completed", "usage_complete": True,
                                             "usage": {"total_tokens": 140}})
            return "finished", "fake"
        with patch("app.services.report_postwrite_pipeline.time") as clock, patch(
            "app.services.report_postwrite_pipeline.asyncio.wait_for",
            side_effect=AssertionError("production must not impose an elapsed-call deadline")
        ):
            clock.monotonic.side_effect = [100, 100, 1000]
            caller = BoundedCaller(collect=collect, ledger=deepcopy(history))
            self.assertEqual((caller.max_calls, caller.max_tokens, caller.max_seconds, caller.call_seconds),
                             (0, 0, 0, 0))
            self.assertEqual(await caller.call("registry", [{"content": "small"}]), "finished")
        self.assertEqual(len(caller.ledger), 20)
        self.assertEqual(caller.ledger[:-1], history)
        self.assertEqual(caller.ledger[-1]["elapsed_seconds"], 900)
        self.assertGreater(caller.summary()["tokens"], 3000000)
        self.assertFalse(caller.summary()["usage_complete"])

    async def test_explicit_zero_disables_configured_test_limits(self):
        with patch.multiple(config, POSTWRITE_MAX_CALLS=14, POSTWRITE_MAX_TOKENS=2800000,
                            POSTWRITE_MAX_SECONDS=3300, POSTWRITE_CALL_SECONDS=480):
            caller = BoundedCaller(max_calls=0, max_tokens=0, max_seconds=0, call_seconds=0)
        self.assertEqual((caller.max_calls, caller.max_tokens, caller.max_seconds, caller.call_seconds),
                         (0, 0, 0, 0))

    async def test_explicit_time_budgets_still_cancel_a_slow_test_call(self):
        for options in ({"call_seconds": 0.01}, {"max_seconds": 0.01}):
            with self.subTest(options=options):
                cancelled = asyncio.Event()
                async def collect(messages, **kwargs):
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
                caller = BoundedCaller(collect=collect, **options)
                with self.assertRaises(TimeoutError):
                    await caller.call("registry", [{"content": "small"}])
                self.assertTrue(cancelled.is_set())
                self.assertEqual(caller.ledger[-1]["state"], "failed")
                self.assertFalse(caller.summary()["usage_complete"])

    async def test_expired_explicit_test_budget_stops_before_network(self):
        collect = AsyncMock()
        caller = BoundedCaller(collect=collect, max_seconds=1)
        caller.previous_seconds = 2
        with self.assertRaises(BudgetStop):
            await caller.call("registry", [{"content": "small"}])
        collect.assert_not_called()
        self.assertEqual(caller.ledger, [])

    async def test_service_resumes_unknown_usage_registry_failure_without_rewriting(self):
        sess = session()
        async def first(messages, **kwargs):
            if "blocks" in json.loads(messages[-1]["content"]):
                await kwargs["on_attempt_event"]({"status": "failed", "http_status": 200,
                    "usage": {"input_tokens": 86886, "output_tokens": 8, "total_tokens": 86894},
                    "usage_complete": False, "error_category": "cancelled"})
                raise TimeoutError("private-timeout-detail")
            return await fake_collect(messages, **kwargs)
        with runtime(sess, first):
            first_output = events([e async for e in survey_service.report_stream("resume-usage", None)])
        self.assertTrue(any(e["type"] == "report_done" for e in first_output), first_output)
        original = deepcopy(report_versions.resolve_report_version(sess, 1))
        checkpoint = original["report_audit"]["checkpoint"]
        self.assertEqual(len(checkpoint["calls"]), 2)
        self.assertIsNone(checkpoint["registry"])
        self.assertFalse(checkpoint["usage"]["usage_complete"])
        resumed_requests = []
        async def resume(messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            self.assertTrue("blocks" in payload or "targets" in payload, "must not rewrite the draft")
            resumed_requests.append(deepcopy(messages))
            return await fake_collect(messages, **kwargs)
        with runtime(sess, resume):
            output = events([e async for e in survey_service.report_stream(
                "resume-usage", None, retry_failed=True, base_version=1)])
        self.assertTrue(any(e["type"] == "report_done" for e in output), output)
        second = report_versions.resolve_report_version(sess, 2)
        self.assertEqual(second["report_audit"]["status"], "complete")
        self.assertEqual(second["report_md"], original["report_md"])
        self.assertEqual(second["input_snapshot"], original["input_snapshot"])
        saved = second["report_audit"]["checkpoint"]
        self.assertEqual(saved["calls"][:2], checkpoint["calls"])
        self.assertEqual(saved["prompts"], checkpoint["prompts"])
        self.assertEqual(len(resumed_requests), 2)
        self.assertEqual(len(saved["calls"]), 4)
        self.assertEqual(saved["stop_reason"], "")
        self.assertNotIn("failure", saved)
        self.assertFalse(saved["usage"]["usage_complete"])
        self.assertEqual(saved["usage"]["tokens"], 87314)
        self.assertEqual(report_versions.resolve_report_version(sess, 1), original)
        self.assertNotIn("private-timeout-detail", json.dumps(output, ensure_ascii=False))

    async def test_unknown_usage_does_not_start_automatic_retry_loop(self):
        reached = []
        async def collect(messages, **kwargs):
            if "blocks" in json.loads(messages[-1]["content"]):
                reached.append("registry")
                raise TimeoutError("private-timeout-detail")
            return await fake_collect(messages, **kwargs)
        frozen = freeze_inputs(session())
        first = await run_pipeline(frozen, prompts=PROMPTS, collect=collect)
        self.assertEqual(reached, ["registry"])
        self.assertEqual(first["draft"], REPORT)
        self.assertEqual(len(first["calls"]), 2)
        self.assertEqual(first["audit"]["status"], "partial")
        again = await run_pipeline(frozen, prompts=PROMPTS, previous=first, collect=collect)
        self.assertEqual(reached, ["registry", "registry"])
        self.assertEqual(len(again["calls"]), 3)
        self.assertEqual(again["audit"]["status"], "partial")
        self.assertFalse(again["usage"]["usage_complete"])
        self.assertEqual(again["draft"], first["draft"])


class FailureDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_stream_error_preserves_safe_attempt_identifiers_and_subtype(self):
        from app.integrations import llm_client
        class BrokenStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                start = {"type": "message_start", "message": {"usage": {"input_tokens": 100, "output_tokens": 5}}}
                yield ("data: " + json.dumps(start) + "\n\n").encode()
                raise httpx.RemoteProtocolError("vendor-private-body sk-PRIVATE")
        real_client = httpx.AsyncClient
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BrokenStream())
        with ExitStack() as stack:
            stack.enter_context(patch.object(llm_client, "LLM_API_BASE", "https://model.invalid/v1"))
            stack.enter_context(patch.object(llm_client, "LLM_API_KEY", "sk-PRIVATE"))
            stack.enter_context(patch.object(llm_client, "FEISHU_LOGIN_REQUIRED", False))
            stack.enter_context(patch.object(llm_client, "_protocol_order", return_value=["messages"]))
            stack.enter_context(patch.object(httpx, "AsyncClient", side_effect=lambda **kw: real_client(
                transport=httpx.MockTransport(respond), **kw)))
            caller = BoundedCaller(collect=llm_client.collect_chat_completion)
            with self.assertRaises(RuntimeError):
                await caller.call("registry", [{"role": "user", "content": "synthetic"}])
        self.assertEqual(len(requests), 1)
        failure = caller.ledger[0]["failure"]
        self.assertEqual(failure["http_status"], 200)
        self.assertEqual(failure["error_type"], "RemoteProtocolError")
        self.assertEqual(failure["wrapper_error_type"], "RuntimeError")
        self.assertRegex(failure["request_id"], r"^[0-9a-f]{32}$")
        self.assertRegex(failure["call_id"], r"^[0-9a-f]{32}$")
        self.assertFalse(caller.summary()["usage_complete"])
        self.assertEqual(caller.summary()["tokens"], 105)
        self.assertNotIn("PRIVATE", json.dumps(caller.ledger, ensure_ascii=False))


class StorageTests(unittest.TestCase):
    def test_explicit_acceptance_budget_reaches_caller_without_changing_defaults(self):
        with patch.multiple(config, POSTWRITE_MAX_CALLS=14, POSTWRITE_MAX_TOKENS=2800000,
                            POSTWRITE_MAX_SECONDS=3300, POSTWRITE_OUTPUT_TOKENS=64000):
            caller = BoundedCaller()
            self.assertEqual((caller.max_calls, caller.max_tokens, caller.max_seconds, caller.output_tokens),
                             (14, 2800000, 3300, 64000))
        source = (__import__("pathlib").Path(config.__file__)).read_text(encoding="utf-8")
        for declaration in ('_env_int("POSTWRITE_MAX_CALLS", 0)',
                            '_env_int("POSTWRITE_MAX_TOKENS", 0)',
                            '_env_int("POSTWRITE_MAX_SECONDS", 0)',
                            '_env_int("POSTWRITE_CALL_SECONDS", 0)',
                            '_env_int("POSTWRITE_OUTPUT_TOKENS", 16000)'):
            self.assertIn(declaration, source)

    def test_new_prompt_catalog_preserves_every_legacy_entry(self):
        from tests.test_prompt_settings import EXPECTED_PROMPT_KEYS
        added = {"postwrite_writer_system", "postwrite_registry_system", "postwrite_auditor_system"}
        self.assertEqual(set(DEFAULT_PROMPTS), EXPECTED_PROMPT_KEYS | added)
        for key in added:
            self.assertTrue(DEFAULT_PROMPTS[key]["current"])

    def test_immutable_store_and_invalid_path(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RunStore(folder)
            run_id = store.create({"a": 1})
            store.save(run_id, {"inputs": {"a": 1}, "state": {"done": 1}})
            self.assertEqual(store.load(run_id)["state"], {"done": 1})
            with self.assertRaises(ValueError):
                store.save(run_id, {"inputs": {"a": 2}})
            with self.assertRaises(ValueError):
                store.load("../anything")

    def test_new_snapshot_does_not_borrow_previous_audit(self):
        source = {}
        report_versions.append_report_version(source, {"report_md": "one", "report_audit": {"status": "complete"}})
        second = report_versions.append_report_version(source, {"report_md": "two"})
        self.assertNotIn("report_audit", second)
