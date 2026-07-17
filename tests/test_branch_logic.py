import unittest

from app.services.branch_logic import (
    branch_rule_for_column,
    branch_rule_label,
    infer_branch_rules,
)
from app.services.report_engine import _build_branch_logic_block


def _question(name, role, index, **extra):
    return {
        "name_zh": name,
        "role": role,
        "column_indexes": [index],
        **extra,
    }


class BranchLogicTests(unittest.TestCase):
    def test_multilingual_aliases_are_merged_before_inference(self):
        columns = [
            _question(
                "好友私聊使用情况",
                "single_choice",
                0,
                options=["几乎不用", "视情况使用", "经常使用"],
                value_aliases={
                    "几乎不用": ["Never", "Tidak pernah"],
                    "视情况使用": ["Sometimes", "Tergantung situasi"],
                    "经常使用": ["Often", "Sering"],
                },
            ),
            _question("不使用原因", "open_text", 1),
            _question("使用场景", "open_text", 2),
            _question("使用体验", "open_text", 3),
            _question("高频使用体验", "open_text", 4),
        ]
        rows = [["使用情况", "不使用原因", "使用场景", "使用体验", "高频体验"]]
        rows += [[value, "原因", "", "", ""] for value in ["Never", "Tidak pernah", "Never"]]
        rows += [[value, "", "场景", "体验", ""] for value in ["Sometimes", "Tergantung situasi"] * 2]
        rows += [[value, "", "", "", "体验"] for value in ["Often", "Sering", "Often", "Sering", "Often"]]

        rules = infer_branch_rules(rows, columns)

        by_options = {tuple(rule["allowed_options"]): rule for rule in rules}
        self.assertEqual(by_options[("几乎不用",)]["eligible_count"], 3)
        self.assertEqual(by_options[("视情况使用",)]["eligible_count"], 4)
        self.assertEqual(by_options[("经常使用",)]["eligible_count"], 5)
        self.assertEqual(
            [target["name"] for target in by_options[("视情况使用",)]["targets"]],
            ["使用场景", "使用体验"],
        )

    def test_nested_branch_prefers_the_nearest_explanatory_parent(self):
        columns = [
            _question("是否加入 Squad", "single_choice", 0, options=["是", "否"]),
            _question("Squad 使用情况", "single_choice", 1, options=["只看", "会发消息"]),
            _question("只看不发的原因", "open_text", 2),
        ]
        rows = [["是否加入", "使用情况", "原因"]]
        rows += [["是", "只看", "原因"] for _ in range(3)]
        rows += [["是", "会发消息", ""] for _ in range(3)]
        rows += [["否", "", ""] for _ in range(4)]

        rules = infer_branch_rules(rows, columns)

        joined_rule = next(rule for rule in rules if rule["parent_name"] == "是否加入 Squad")
        usage_rule = next(rule for rule in rules if rule["parent_name"] == "Squad 使用情况")
        self.assertEqual(joined_rule["allowed_options"], ["是"])
        self.assertEqual([target["name"] for target in joined_rule["targets"]], ["Squad 使用情况"])
        self.assertEqual(usage_rule["allowed_options"], ["只看"])
        self.assertEqual([target["name"] for target in usage_rule["targets"]], ["只看不发的原因"])

    def test_small_amount_of_out_of_branch_data_is_tolerated(self):
        columns = [
            _question("使用情况", "single_choice", 0, options=["A", "B"]),
            _question("A 分支问题", "open_text", 1),
        ]
        rows = [["使用情况", "分支题"]]
        rows += [["A", "回答"] for _ in range(20)]
        rows += [["B", "异常回答"]]
        rows += [["B", ""] for _ in range(19)]

        rules = infer_branch_rules(rows, columns)

        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["allowed_options"], ["A"])
        self.assertEqual(rules[0]["precision"], 0.9524)
        self.assertEqual(rules[0]["targets"][0]["answered_count"], 21)

    def test_one_person_branch_is_not_rejected_by_sample_size(self):
        columns = [
            _question("使用情况", "single_choice", 0, options=["罕见选项", "常见选项"]),
            _question("罕见选项后续题", "open_text", 1),
        ]
        rows = [["使用情况", "后续题"]]
        rows += [["罕见选项", "唯一回答"]]
        rows += [["常见选项", ""] for _ in range(8)]

        rules = infer_branch_rules(rows, columns)

        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["allowed_options"], ["罕见选项"])
        self.assertEqual(rules[0]["eligible_count"], 1)
        self.assertEqual(rules[0]["confidence"], "high")
        self.assertIn("完全吻合", rules[0]["confidence_reason"])

    def test_sparse_optional_answer_without_structure_is_only_suspected(self):
        columns = [
            _question("分组题", "single_choice", 0, options=["A", "B"]),
            _question("可选意见", "open_text", 1),
        ]
        rows = [["分组题", "可选意见"]]
        rows += [["A", "唯一意见"]]
        rows += [["A", ""] for _ in range(4)]
        rows += [["B", ""] for _ in range(5)]

        rules = infer_branch_rules(rows, columns)

        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["confidence"], "medium")
        self.assertIn("疑似条件关系", branch_rule_label(rules[0], 1))

    def test_ordinary_optional_nonresponse_is_not_treated_as_branching(self):
        columns = [
            _question("分组题", "single_choice", 0, options=["A", "B"]),
            _question("可选意见", "open_text", 1),
        ]
        rows = [["分组题", "可选意见"]]
        rows += [["A", "意见"], ["A", ""], ["A", "意见"], ["A", ""]]
        rows += [["B", "意见"], ["B", ""], ["B", "意见"], ["B", ""]]

        self.assertEqual(infer_branch_rules(rows, columns), [])

    def test_branch_context_labels_applicable_population_and_denominator(self):
        rule = {
            "parent_index": 0,
            "parent_name": "好友私聊使用情况",
            "allowed_options": ["视情况使用"],
            "eligible_count": 45,
            "targets": [{"indexes": [2], "name": "使用体验", "answered_count": 43}],
            "confidence": "high",
        }

        self.assertIs(branch_rule_for_column([rule], 2), rule)
        label = branch_rule_label(rule, 2)
        self.assertIn("进入该分支 45 人", label)
        self.assertIn("本题 43 条有效回答", label)
        block = _build_branch_logic_block([rule])
        self.assertIn("不得合并回答池", block)
        self.assertIn("不得使用问卷总样本", block)


if __name__ == "__main__":
    unittest.main()
