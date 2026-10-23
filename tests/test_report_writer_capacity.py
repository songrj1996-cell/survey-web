"""Offline capacity advice: boundaries, uncertainty, full output reserve and privacy."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from app.core import config
from app.services.report_writer_capacity import estimate_writer_capacity


class CapacityTests(unittest.TestCase):
    def evaluate(self, tokens, *, output=64000, limits=None, warning=80.0, model="claude-opus-5-5"):
        with patch.object(config, "POSTWRITE_WRITER_CONTEXT_LIMITS", limits if limits is not None else
                          json.dumps({"claude-opus-5-5": 1000000})), \
                patch.object(config, "POSTWRITE_WRITER_WARNING_PERCENT", warning):
            return estimate_writer_capacity([{"role": "user", "content": "合成材料"}], model=model,
                                            output_tokens=output, estimate_input=lambda request: tokens)

    def test_boundaries_include_entire_output_reserve(self):
        for tokens, status, percent in [(736000, "sufficient", 80), (736001, "near_limit", 80.1),
                                        (856000, "near_limit", 92), (936000, "over_limit", 100),
                                        (1016000, "over_limit", 108)]:
            with self.subTest(tokens=tokens):
                result = self.evaluate(tokens)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["occupancy_percent"], percent)
                self.assertEqual(result["required_estimate"], tokens + 64000)
                self.assertEqual(result["output_reserved"], 64000)
                self.assertNotIn("保证", result["impact"])

    def test_request_is_passed_whole_and_is_not_changed(self):
        request = [{"role": "system", "content": "完整写作要求"},
                   {"role": "user", "content": json.dumps({"background": "背景", "questions": ["题目"],
                       "raw_answers": ["全部原文"], "statistics": {"players": 120}, "instruction": "补充要求"})}]
        before, seen = deepcopy(request), []
        def count(value):
            seen.append(deepcopy(value))
            return 425072
        result = estimate_writer_capacity(request, model=config.LLM_POSTWRITE_MODEL,
                                          output_tokens=64000, estimate_input=count)
        self.assertEqual(request, before)
        self.assertEqual(seen, [before])
        self.assertEqual(result["required_estimate"], 489072)
        self.assertNotIn("全部原文", json.dumps(result, ensure_ascii=False))

    def test_invalid_configuration_and_unknown_model_are_not_zero_usage(self):
        for limits in ("{bad json", "[]", '{"claude-opus-5-5":0}', '{"claude-opus-5-5":true}',
                       '{"claude-opus-5-5":"1000000"}', "{}"):
            with self.subTest(limits=limits):
                result = self.evaluate(100, limits=limits)
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["occupancy_percent"])
                self.assertIn("继续生成", result["impact"])
        self.assertEqual(self.evaluate(100, model="different-model")["reason"], "unknown_model")
        for warning in (0, 100, float("nan"), float("inf"), "80", True):
            self.assertEqual(self.evaluate(100, warning=warning)["status"], "unknown")

    def test_invalid_count_or_output_does_not_raise_or_disclose(self):
        for tokens in (-1, None, True, 12.5, float("inf")):
            self.assertEqual(self.evaluate(tokens)["status"], "unknown")
        for output in (0, -1, True, None):
            self.assertEqual(self.evaluate(100, output=output)["status"], "unknown")
        def failed(request):
            raise RuntimeError("PRIVATE_ANSWER_AND_KEY")
        result = estimate_writer_capacity([], model=config.LLM_POSTWRITE_MODEL,
                                          output_tokens=64000, estimate_input=failed)
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_rounding_does_not_call_an_under_limit_request_over_limit(self):
        result = self.evaluate(935999)
        self.assertEqual(result["status"], "near_limit")
        self.assertIn("接近100%", result["message"])


if __name__ == "__main__":
    unittest.main()
