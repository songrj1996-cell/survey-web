from copy import deepcopy
import unittest

from app.services.insight_report_v3 import chapters, audit
from app.services.insight_report_v3.research_context import (
    complete_stats_tables, statistic_facts, linked_source_context,
    expand_stat_references, validate_stat_references, coverage_ledger, model_statistic_facts,
    bound_statistic_facts,
)


STATS = """<metadata>总样本: 3, 有效样本: 3</metadata>
## 画像维度概览
### 模式偏好
| 取值 | 频数 | 占比 |
| --- | --- | --- |
| A | 1 | 33.3% |
| B | 1 | 33.3% |
| 无偏好 | 1 | 33.3% |
## Part 1 体验
### 评价
| 选项 | 频数 | 占比 |
| --- | --- | --- |
| 好玩 | 2 | 66.7% |
| 一般 | 1 | 33.3% |
**按模式偏好交叉**
| 模式偏好 | 好玩 | 一般 | 样本量 |
| --- | --- | --- | --- |
| A | 1 (100%) | 0 (0%) | 1 |
| B | 1 (100%) | 0 (0%) | 1 |
| 无偏好 | 0 (0%) | 1 (100%) | 1 |
"""


class ResearchContextTests(unittest.TestCase):
    def test_bound_row_cells_never_expand_to_another_row_or_table(self):
        facts = statistic_facts(chapters.build_stats_catalog(complete_stats_tables(STATS, [])))
        closed = bound_statistic_facts(facts, ["s003-r002"])
        self.assertEqual(set(closed), {"s003-r002", *(f"s003-r002-c{i:03d}" for i in range(1, 5))})
        self.assertEqual(closed["s003-r002-c004"]["column"], "样本量")
        self.assertEqual(closed["s003-r002-c004"]["statement"], "1")
        self.assertEqual(bound_statistic_facts(facts, ["s003-r002-c002"]), closed)
        self.assertIsNotNone(validate_stat_references("[S:s003-r003-c004]", closed))
        self.assertIsNotNone(validate_stat_references("[S:s001-r002-c002]", closed))
        self.assertEqual(len(model_statistic_facts(closed)), 1)
        self.assertNotIn("row_cells", model_statistic_facts(closed)[0])

    def test_historical_cell_fact_is_not_lost_or_expanded_from_unavailable_metadata(self):
        old = {"s001-r001-c002": {"id": "s001-r001-c002", "row_fact_id": "s001-r001", "statement": "9", "column": "总计"}}
        self.assertEqual(bound_statistic_facts(old, list(old)), old)
        self.assertEqual(model_statistic_facts(old), list(old.values()))

    def test_cross_tables_are_not_lost_and_all_options_survive(self):
        tables = complete_stats_tables(STATS, [])
        self.assertEqual(len(tables), 3)
        self.assertEqual(tables[0]["part"], "")
        self.assertEqual(tables[2]["kind"], "cross_tab")
        self.assertIn("无偏好", tables[2]["table_markdown"])
        self.assertEqual(len({t["table_key"] for t in tables}), 3)

    def test_code_fills_stats_without_accepting_unknown_or_malformed_tokens(self):
        catalog = chapters.build_stats_catalog(complete_stats_tables(STATS, []))
        facts = statistic_facts(catalog)
        rendered = expand_stat_references("[S:s003-r002]。偏好与评价并非同一问题。", facts)
        self.assertIn("模式偏好：B", rendered)
        self.assertIn("样本量：1", rendered)
        self.assertIn("100%", rendered)
        self.assertIsNone(validate_stat_references("[S:s003-r002]", facts))
        self.assertIsNotNone(validate_stat_references("[S:s999-r001]", facts))
        self.assertIsNotNone(validate_stat_references("[S:bad]", facts))

    def test_exact_cells_read_naturally_without_losing_scope_for_audit(self):
        facts = statistic_facts(chapters.build_stats_catalog(complete_stats_tables(STATS, [])))
        self.assertEqual(expand_stat_references("A有[S:s001-r001-c002]份，占[S:s001-r001-c003]。", facts), "A有1份，占33.3%。")
        self.assertIsNone(validate_stat_references("[S:s001-r001-c003]", facts))
        self.assertIsNotNone(validate_stat_references("[S:s001-r001-c999]", facts))
        cell = facts["s003-r002-c002"]
        self.assertEqual(cell["row_label"], "B")
        self.assertEqual(cell["column"], "好玩")
        self.assertIn("样本量：1", cell["row_statement"])
        rows = model_statistic_facts(facts)
        self.assertTrue(all(not row.get("row_fact_id") for row in rows))
        self.assertEqual(rows[0]["cells"][1], {"id": "s001-r001-c002", "column": "频数", "value": "1"})

    def test_sample_aliases_only_identical_profile_distribution(self):
        duplicate = """\n## Part 2 偏好
### 模式偏好
| 取值 | 频数 | 占比 |
| --- | --- | --- |
| A | 1 | 33.3% |
| B | 1 | 33.3% |
| 无偏好 | 1 | 33.3% |
"""
        tables = complete_stats_tables(STATS + duplicate, [])
        self.assertEqual(tables[0]["table_key"], tables[-1]["table_key"])
        alternate_header = complete_stats_tables(STATS + duplicate.replace("| 取值 |", "| 选项 |"), [])
        self.assertEqual(alternate_header[0]["table_key"], alternate_header[-1]["table_key"])
        changed = complete_stats_tables(STATS + duplicate.replace("| A | 1", "| A | 2"), [])
        self.assertNotEqual(changed[0]["table_key"], changed[-1]["table_key"])

    def test_bound_stats_do_not_open_a_backdoor_for_viewpoint_percentages(self):
        facts = statistic_facts(chapters.build_stats_catalog(complete_stats_tables(STATS, [])))
        normalized, error = chapters.normalize_report_block_text(
            heading="评价与选择", body="[S:s003-r002]。两种选择回答的问题不同。",
            findings=[], profiles=[{"profile_name": "模式偏好", "values": ["B"]}],
            numeric_sources="", stat_facts=facts,
        )
        self.assertIsNone(error)
        self.assertIn("[S:", normalized["body"])
        for body in ("[S:s003-r002]。B玩家中80%担忧操作。", "7名玩家担忧操作。"):
            normalized, error = chapters.normalize_report_block_text(
                heading="判断", body=body, findings=[],
                profiles=[{"profile_name": "模式偏好", "values": ["B"]}],
                numeric_sources="80% 7", stat_facts=facts,
            )
            self.assertIsNone(normalized)
            self.assertIsNotNone(error)

    def test_same_player_links_and_choices_do_not_expose_identity(self):
        plan = {"columns": [
            {"index": 0, "name": "玩家ID", "role": "id"},
            {"index": 1, "name": "总体评价", "role": "single_choice"},
            {"index": 2, "name": "反馈", "role": "open_text"},
        ]}
        registry = {
            "global-one": {"respondent_ref": "secret-hash", "respondent_key": "玩家ID=private-id", "text": "体验好", "column_index": 2},
            "global-two": {"respondent_ref": "secret-hash", "respondent_key": "玩家ID=private-id", "text": "时间不合适", "column_index": 3},
        }
        original = deepcopy(registry)
        enriched, diagnostic = linked_source_context(
            registry, rows=[["id", "rating", "text"], ["private-id", "好玩", "体验好"]], plan=plan,
        )
        self.assertEqual(registry, original)
        self.assertEqual(diagnostic["linked_sources"], 2)
        sources, _, _ = chapters._chapter_sources(
            ["f"], findings_by_id={"f": {"response_ids": list(registry)}}, source_registry=enriched,
        )
        self.assertEqual(sources[0]["player_id"], sources[1]["player_id"])
        self.assertEqual(sources[0]["case_context"][0]["answer"], "好玩")
        for secret in ("secret-hash", "private-id", "global-one"):
            self.assertNotIn(secret, str(sources))

    def test_ambiguous_rows_are_not_joined(self):
        registry = {"r": {"respondent_key": "ID=A", "respondent_ref": "hash"}}
        result, diagnostic = linked_source_context(
            registry, rows=[["ID", "Q"], ["A", "是"], ["A", "否"]],
            plan={"columns": [{"index": 0, "name": "ID", "role": "id"}, {"index": 1, "name": "Q", "role": "single_choice"}]},
        )
        self.assertEqual(result["r"]["case_context"], [])
        self.assertEqual(diagnostic["ambiguous_sources"], 1)

    def test_important_material_coverage_is_explicit(self):
        result = coverage_ledger([
            {"id": "f1", "preserve_reason": "specific_issue"}, {"id": "f2", "kind": "fact"},
        ], [{"id": "c1", "blocks": [{"id": "b1", "finding_ids": ["f2"]}]}])
        self.assertTrue(result[0]["required"])
        self.assertEqual(result[0]["locations"], [])
        self.assertEqual(result[1]["locations"], ["c1/b1"])

    def test_auditor_does_not_join_ambiguous_players(self):
        registry = {rid: {"respondent_ref": "same-hash", "case_link_status": "ambiguous_rows", "text": rid}
                    for rid in ("a", "b")}
        payload = audit._model_payload([{
            "claim_id": "c001", "kind": "block", "section_title": "判断", "findings": [],
            "source_ids": ["a", "b"], "heading": "标题", "body": "内容",
        }], registry)
        self.assertNotEqual(payload["sources"][0]["player_id"], payload["sources"][1]["player_id"])
        self.assertNotIn("same-hash", str(payload))


if __name__ == "__main__":
    unittest.main()
