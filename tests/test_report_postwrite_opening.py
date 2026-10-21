"""Opening supplement delivery and compatibility; no real model or data writes."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core import config
from app.routers import survey as survey_router
from app.services import survey_service, report_versions
from app.services.report_postwrite_inputs import freeze_inputs
from app.storage.prompts import DEFAULT_PROMPTS
from tests.test_report_postwrite_audit import session
from tests.test_report_postwrite_flow import REPORT, PROMPTS, events, fake_collect, runtime


class OpeningContractTests(unittest.TestCase):
    def test_original_tested_prompts_are_unchanged(self):
        # Freeze the accepted base texts, independently of the new supplement.
        expected = {
            "DEFAULT_POSTWRITE_WRITER_V1": "7f08cf579022b0354a4e47330f53e9a3dbf01a00d1d0844570f6871ec111ebc3",
            "DEFAULT_POSTWRITE_WRITER": "28631ba2df56408fc23019b4f4431e96cb370b841dcd6d43bd70f6df654f8c13",
            "DEFAULT_POSTWRITE_REGISTRY": "038e68d6a0cf8659ad7526f2729962cd8941efb8d71ea0423a32dfd0c5d3e770",
            "DEFAULT_POSTWRITE_AUDITOR": "d6427a50b5fad6f145835e9c5f2fb4088a491ce4d79b8c59e043848c9cdcb050",
        }
        for name, digest in expected.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256(getattr(config, name).encode()).hexdigest(), digest)
        self.assertEqual(DEFAULT_PROMPTS["postwrite_writer_system"]["current"], config.DEFAULT_POSTWRITE_WRITER)
        self.assertEqual(DEFAULT_PROMPTS["postwrite_writer_system"]["version"], 2)

    def test_order_and_missing_background_fallback_are_explicit(self):
        self.assertEqual(config.POSTWRITE_OPENING_SECTIONS, ("调研背景", "样本说明", "核心结论"))
        numbered = [f"{i}. {name}" for i, name in enumerate(config.POSTWRITE_OPENING_SECTIONS, 1)]
        self.assertEqual([line for line in config.POSTWRITE_OPENING_REQUIREMENTS.splitlines()
                          if line in numbered], numbered)
        for text in ("用户填写", "未填写时", "问卷明确主题", "未提供具体调研背景", "不要求补填", "不阻止生成", "Python统计口径"):
            self.assertIn(text, config.POSTWRITE_OPENING_REQUIREMENTS)

    def test_options_add_metadata_only_for_enabled_fresh_insight(self):
        for enabled in (False, True):
            for changes, fresh in (({}, True), ({"report_mode": "quick"}, False),
                                   ({"report_mode": "statistics"}, False), ({"mode": "crosstab"}, False),
                                   ({"mode": "quantitative"}, False), ({"mode": "comment"}, False),
                                   ({"mode": "interview"}, False), ({"report_md": REPORT}, False)):
                with self.subTest(enabled=enabled, changes=changes):
                    sess = {**session(), **changes}
                    before = deepcopy(sess)
                    with patch.object(survey_service, "get_session", return_value=sess), \
                         patch.object(survey_service, "is_quick_report_enabled", return_value=False), \
                         patch.object(survey_service, "is_postwrite_report_enabled", return_value=enabled):
                        options = survey_service.report_style_options("synthetic")
                    expected = list(config.POSTWRITE_OPENING_SECTIONS) if enabled and fresh else []
                    self.assertEqual(options["postwrite_opening_sections"], expected)
                    self.assertEqual(sess, before)
                    self.assertEqual(options["report_mode"], survey_service.resolve_report_mode(sess))
                    self.assertEqual(options["fixed_mode"], "statistics" if changes.get("mode") == "crosstab" else None)

    def test_options_route_uses_existing_owner_guard_and_never_plans(self):
        app = FastAPI()
        app.include_router(survey_router.router)
        with TestClient(app) as client, \
             patch.object(survey_router, "require_session_request_access", new=AsyncMock()) as access, \
             patch("app.storage.sessions.get_session", return_value=session()), \
             patch.object(survey_service, "get_session", return_value=session()), \
             patch.object(survey_service, "is_quick_report_enabled", return_value=False), \
             patch.object(survey_service, "is_postwrite_report_enabled", return_value=True), \
             patch.object(survey_service, "collect_chat_completion", new=AsyncMock()) as model, \
             patch.object(survey_service, "save_session") as save:
            response = client.get("/api/report/synthetic/options")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["postwrite_opening_sections"], list(config.POSTWRITE_OPENING_SECTIONS))
            access.side_effect = HTTPException(status_code=403, detail="无权访问此会话")
            self.assertEqual(client.get("/api/report/synthetic/options").status_code, 403)
            model.assert_not_awaited()
            save.assert_not_called()


class OpeningWriterDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sample_dimensions_keep_python_counts_denominators_and_call_stages(self):
        source = session()
        dimensions = (
            ("年龄段", ["18–24", "25–34", ""]),
            ("地区", ["A地区", "A地区", "A地区"]),
            ("语言", ["中文", "中文", ""]),
            ("设备", ["", "", ""]),
        )
        for name, values in dimensions:
            index = len(source["rows"][0])
            source["rows"][0].append(name)
            for row, value in zip(source["rows"][1:], values):
                row.append(value)
            source["confirmed_columns"].append(
                {"column_indexes": [index], "role": "profile_dim", "name": name})
        before = deepcopy(source)
        expected_packet = freeze_inputs(source)["postwrite_packet"]
        # Real Python statistics: sparse dimensions use two respondents,
        # while the complete, uniform dimension uses all three respondents.
        markdown = expected_packet["statistics_markdown"]
        for text in ("| 18–24 | 1 | 50.0% |", "| 25–34 | 1 | 50.0% |",
                     "| A地区 | 3 | 100.0% |", "| 中文 | 2 | 100.0% |",
                     "共 2 份非空回答", "共 3 份非空回答", "该画像维度无有效数据"):
            self.assertIn(text, markdown)
        self.assertEqual(source, before)

        # Compare the prior opening contract with the new one using the same
        # inputs and mock model responses; no new statistics or model stage.
        prefix, sample = config.POSTWRITE_OPENING_REQUIREMENTS.split("样本说明：", 1)
        _, suffix = sample.split("核心结论：", 1)
        previous = (prefix + "样本说明：沿用已提供的样本信息及Python统计口径；"
                    "不补写未提供的抽样方式或调研时间。\n核心结论：" + suffix)
        call_sequences = []
        for supplement in (previous, config.POSTWRITE_OPENING_REQUIREMENTS):
            with self.subTest(updated=supplement != previous):
                sess = deepcopy(source)
                requests = []

                async def collect(messages, **kwargs):
                    requests.append(deepcopy(messages))
                    return await fake_collect(messages, **kwargs)

                with runtime(sess, collect), patch.object(config, "POSTWRITE_OPENING_REQUIREMENTS", supplement):
                    output = events([e async for e in survey_service.report_stream("sample-dimensions", None)])
                self.assertTrue(any(e["type"] == "report_done" for e in output), output)
                saved = report_versions.resolve_report_version(sess, 1)
                self.assertEqual(json.loads(requests[0][-1]["content"]), {**expected_packet, "instruction": ""})
                self.assertEqual(requests[0][0]["content"], config.DEFAULT_POSTWRITE_WRITER + "\n\n" + supplement)
                self.assertEqual(saved["input_snapshot"]["postwrite_packet"], expected_packet)
                self.assertEqual(saved["report_md"], REPORT)
                call_sequences.append([sorted(json.loads(request[-1]["content"])) for request in requests])
        self.assertEqual(call_sequences[0], call_sequences[1])
        self.assertEqual(len(call_sequences[1]), 3)  # writer, registry, meaning audit

    async def test_writer_gets_only_the_appendix_and_keeps_all_inputs_and_other_prompts(self):
        candidates = (config.DEFAULT_POSTWRITE_WRITER_V1, config.DEFAULT_POSTWRITE_WRITER,
                      "CUSTOM_TEXT_STAYS_VERBATIM", "CUSTOM_TEXT_STAYS_VERBATIM\n\n" + config.POSTWRITE_OPENING_REQUIREMENTS)
        for original in candidates:
            for context in ({}, {"problem": "了解已有功能体验", "target_users": "本次受访玩家"}):
                with self.subTest(custom=original[:16], background=bool(context)):
                    sess = session()
                    sess["qualitative_context"] = deepcopy(context)
                    sent = []

                    async def collect(messages, **kwargs):
                        if len(sent) == 0:
                            sent.append(deepcopy(messages))
                        return await fake_collect(messages, **kwargs)

                    def catalog(key):
                        return original if key == "postwrite_writer_system" else DEFAULT_PROMPTS[key]["current"]

                    with runtime(sess, collect), patch.object(survey_service, "_get_prompt_text", side_effect=catalog):
                        output = events([e async for e in survey_service.report_stream("opening-new", None)])
                    self.assertTrue(any(e["type"] == "report_done" for e in output), output)
                    saved = report_versions.resolve_report_version(sess, 1)
                    effective = original if original.endswith(config.POSTWRITE_OPENING_REQUIREMENTS) else original + "\n\n" + config.POSTWRITE_OPENING_REQUIREMENTS
                    self.assertEqual(sent[0][0], {"role": "system", "content": effective})
                    self.assertTrue(effective.startswith(original))
                    self.assertEqual(effective.count(config.POSTWRITE_OPENING_REQUIREMENTS), 1)
                    self.assertEqual(json.loads(sent[0][-1]["content"]), {
                        **saved["input_snapshot"]["postwrite_packet"], "instruction": ""})
                    self.assertEqual(saved["input_snapshot"]["postwrite_packet"]["research_task"], context)
                    self.assertEqual(saved["report_audit"]["checkpoint"]["prompts"], {**PROMPTS, "writer": effective})
                    # The supplement is not an extra format gate or rewrite stage.
                    self.assertEqual(saved["report_md"], REPORT)

    async def test_checkpoint_without_the_appendix_resumes_without_rewriting_or_changing_prompts(self):
        sess = session()

        async def partial(messages, **kwargs):
            raw, model = await fake_collect(messages, **kwargs)
            return ('{"checks":[]}', model) if "targets" in json.loads(messages[-1]["content"]) else (raw, model)

        with runtime(sess, partial), patch.object(config, "POSTWRITE_OPENING_REQUIREMENTS", ""):
            _ = [e async for e in survey_service.report_stream("opening-legacy", None)]
        before = deepcopy(report_versions.resolve_report_version(sess, 1))
        self.assertEqual(before["report_audit"]["status"], "partial")
        self.assertEqual(before["report_audit"]["checkpoint"]["prompts"]["writer"], config.DEFAULT_POSTWRITE_WRITER)
        requests = []

        async def resume(messages, **kwargs):
            requests.append(deepcopy(messages))
            self.assertIn("targets", json.loads(messages[-1]["content"]))
            return await fake_collect(messages, **kwargs)

        with runtime(sess, resume):
            output = events([e async for e in survey_service.report_stream("opening-legacy", None, retry_failed=True, base_version=1)])
        self.assertTrue(any(e["type"] == "report_done" for e in output), output)
        after = report_versions.resolve_report_version(sess, 1)
        self.assertTrue(requests)
        self.assertEqual(after["report_audit"]["status"], "complete")
        self.assertEqual(after["report_audit"]["checkpoint"]["prompts"], before["report_audit"]["checkpoint"]["prompts"])
        self.assertEqual(after["report_md"], before["report_md"])
        self.assertEqual(after["input_snapshot"], before["input_snapshot"])
        self.assertEqual(len(sess["report_versions"]), 1)
        self.assertEqual(sess["next_report_version"], 2)


if __name__ == "__main__":
    unittest.main()
