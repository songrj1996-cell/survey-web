import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import comment_analysis
from app.core import config
from app.services import comment_pipeline
from app.storage import prompts as prompt_storage


class CommentDirectLLMHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_failure_switches_to_fallback_model(self):
        calls = []

        async def fake_collect(messages, **kwargs):
            calls.append(kwargs["models"][0])
            if len(calls) <= 2:
                return "{}", kwargs["models"][0]
            return '[{"ok": true}]', kwargs["models"][0]

        with patch.object(
            comment_pipeline,
            "collect_chat_completion",
            new=AsyncMock(side_effect=fake_collect),
        ):
            parsed, model, repaired = await comment_pipeline._comment_json_call(
                task="test",
                system_prompt="system",
                query="{}",
                models=("primary", "fallback"),
                reasoning_effort="medium",
                max_tokens=1024,
                validate=lambda value: "not list" if not isinstance(value, list) else "",
            )

        self.assertEqual(parsed, [{"ok": True}])
        self.assertEqual(model, "fallback")
        self.assertFalse(repaired)
        self.assertEqual(calls, ["primary", "primary", "fallback"])

    def test_defaults_preserve_dsl_report_and_quote_contracts(self):
        self.assertIn("## 玩家核心观点", config.DEFAULT_COMMENT_REPORT_SYSTEM_PROMPT)
        self.assertIn("## 业务建议", config.DEFAULT_COMMENT_REPORT_SYSTEM_PROMPT)
        self.assertIn(
            "每个对象必须且只能包含 idx、text、translation、score、reason",
            config.DEFAULT_COMMENT_QUOTE_BATCH_SYSTEM_PROMPT,
        )
        self.assertFalse(hasattr(config, "DIFY_COMMENT_ANALYSIS_KEY"))

    def test_all_comment_prompts_are_registered_for_admin_editing(self):
        expected = {
            "comment_relevance_system",
            "comment_extract_system",
            "comment_merge_system",
            "comment_classify_system",
            "comment_report_system",
            "comment_quote_batch_system",
            "comment_quote_final_system",
        }
        with tempfile.TemporaryDirectory(prefix="comment-prompts-") as temp_dir:
            prompt_file = os.path.join(temp_dir, "prompts.json")
            with patch.object(prompt_storage, "PROMPTS_FILE", prompt_file):
                loaded = prompt_storage._load_prompts()
        self.assertTrue(expected.issubset(loaded))
        self.assertTrue(all(loaded[key]["editable"] for key in expected))


class CommentDirectPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_pipeline_uses_direct_models_and_preserves_stats(self):
        calls = []

        async def fake_collect(messages, **kwargs):
            system = messages[0]["content"]
            query = json.loads(messages[1]["content"])
            calls.append((system, kwargs["models"][0]))
            if system == "RELEVANCE":
                return json.dumps([
                    {"idx": 0, "is_related": True, "relation": "direct", "reason": "related"},
                    {"idx": 1, "is_related": True, "relation": "implicit", "reason": "related"},
                ]), kwargs["models"][0]
            if system == "EXTRACT":
                return json.dumps([
                    {
                        "theme_name": "价格与视觉",
                        "description": "玩家讨论价格和视觉效果",
                        "sentiment": "neutral",
                    }
                ], ensure_ascii=False), kwargs["models"][0]
            if system == "MERGE":
                return json.dumps([
                    {
                        "theme_id": "theme_price_visual",
                        "theme_name": "价格与视觉",
                        "description": "玩家讨论价格和视觉效果",
                        "sentiment": "neutral",
                    }
                ], ensure_ascii=False), kwargs["models"][0]
            if system == "CLASSIFY":
                items = []
                for item in query["comments_json"]:
                    items.append({
                        "idx": item["idx"],
                        "theme_ids": ["theme_price_visual"],
                        "sentiment": "negative" if item["idx"] == 0 else "positive",
                        "is_quote_candidate": False,
                        "translation": "",
                    })
                return json.dumps(items, ensure_ascii=False), kwargs["models"][0]
            if system == "REPORT":
                return (
                    "## 核心结论\n\n- 结论\n\n"
                    "## 玩家核心观点\n\n- 观点\n\n"
                    "## 业务建议\n\n- 建议"
                ), kwargs["models"][0]
            raise AssertionError(system)

        sess = {
            "comment_post_title": "Skin preview",
            "comment_post_content": "Price, event exchange and visual effects",
            "comment_sample": [
                "The effects are nice but the price is too high.",
                "Efeknya bagus dan saya suka warna ungunya.",
                "Matchmaking is terrible and unrelated to this skin.",
            ],
            "comment_sample_meta": {},
        }
        prompt_patches = (
            patch.object(comment_pipeline, "_get_comment_relevance_system_prompt", return_value="RELEVANCE"),
            patch.object(comment_pipeline, "_get_comment_extract_system_prompt", return_value="EXTRACT"),
            patch.object(comment_pipeline, "_get_comment_merge_system_prompt", return_value="MERGE"),
            patch.object(comment_pipeline, "_get_comment_classify_system_prompt", return_value="CLASSIFY"),
            patch.object(comment_pipeline, "_get_comment_report_system_prompt", return_value="REPORT"),
            patch.object(comment_pipeline, "collect_chat_completion", new=AsyncMock(side_effect=fake_collect)),
        )
        for item in prompt_patches:
            item.start()
        try:
            events = []
            async for event in comment_pipeline._comment_analysis_pipeline(sess):
                events.append(event)
        finally:
            for item in reversed(prompt_patches):
                item.stop()

        result = next(payload for kind, payload in events if kind == "result")
        self.assertEqual(result["total_classified"], 2)
        self.assertEqual(result["relevance_stats"]["off_topic_count"], 1)
        self.assertEqual(result["themes"][0]["count"], 2)
        self.assertEqual(result["themes"][0]["percentage"], 100.0)
        self.assertTrue(result["report_md"].startswith("## 核心结论"))
        self.assertEqual(
            [model for _system, model in calls],
            [
                config.LLM_COMMENT_RELEVANCE_MODEL,
                config.LLM_COMMENT_EXTRACT_MODEL,
                config.LLM_COMMENT_MERGE_MODEL,
                config.LLM_COMMENT_CLASSIFY_MODEL,
                config.LLM_COMMENT_REPORT_MODEL,
            ],
        )

    async def test_quote_pipeline_uses_direct_batch_and_final_models(self):
        calls = []

        async def fake_collect(messages, **kwargs):
            system = messages[0]["content"]
            query = json.loads(messages[1]["content"])
            calls.append((system, kwargs["models"][0]))
            comments = query["comments_json"]
            if system == "QUOTE_BATCH":
                items = [
                    {
                        "idx": item["idx"],
                        "text": item["text"],
                        "translation": f"中文翻译 {item['idx']}",
                        "score": 90 - item["idx"],
                        "reason": "信息完整",
                    }
                    for item in comments
                ]
                return json.dumps(items, ensure_ascii=False), kwargs["models"][0]
            if system == "QUOTE_FINAL":
                items = [
                    {
                        "idx": item["idx"],
                        "text": item["text"],
                        "translation": item["translation"],
                        "score": item["score"],
                        "reason": item["reason"],
                    }
                    for item in comments[:2]
                ]
                return json.dumps(items, ensure_ascii=False), kwargs["models"][0]
            raise AssertionError(system)

        sess = {
            "comment_post_title": "Skin preview",
            "comment_post_content": "Price and event exchange",
            "comment_long_candidates": [
                "The visual effects are beautiful but the price is too high.",
                "Saya berharap skin ini bisa ditukar dengan token event.",
                "The model looks similar to an older skin and needs more detail.",
            ],
        }
        patches = (
            patch.object(comment_pipeline, "_get_comment_quote_batch_system_prompt", return_value="QUOTE_BATCH"),
            patch.object(comment_pipeline, "_get_comment_quote_final_system_prompt", return_value="QUOTE_FINAL"),
            patch.object(comment_pipeline, "collect_chat_completion", new=AsyncMock(side_effect=fake_collect)),
        )
        for item in patches:
            item.start()
        try:
            selected = await comment_pipeline._select_comment_raw_quotes(sess)
        finally:
            for item in reversed(patches):
                item.stop()

        self.assertEqual(len(selected), 2)
        self.assertTrue(all(item["translation"].startswith("中文翻译") for item in selected))
        self.assertEqual(
            [model for _system, model in calls],
            [config.LLM_COMMENT_QUOTE_BATCH_MODEL, config.LLM_COMMENT_QUOTE_FINAL_MODEL],
        )


if __name__ == "__main__":
    unittest.main()
