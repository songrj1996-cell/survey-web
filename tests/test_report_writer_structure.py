import json
import unittest

from app.core.config import DEFAULT_WRITER_REQUIREMENTS
from app.services.report_engine import (
    _build_analysis_focus_block,
    _build_qa_context,
    _build_crosstab_plan_revision_query,
    _build_crosstab_planner_query,
    _build_large_sample_writer_query,
    _build_writer_action_query,
    _build_writer_action_repair_query,
    _build_writer_context,
    _build_writer_core_review_query,
    _build_writer_core_query,
    _build_writer_first_query,
    _build_writer_part_query,
    _normalize_action_section,
)


def _analysis_focus() -> dict:
    return {
        "core_question": "三个案例共同说明了什么，以及是否值得继续投入",
        "report_organization": "先上提跨案例对比框架，再把各案例作为证据",
        "supporting_analyses": ["比较目标用户差异", "归纳共性风险"],
        "evidence_role": "案例只作为支持或反证，不作为一级主线",
        "expected_deliverables": ["投入判断", "跨案例优先级"],
        "avoid_structures": ["不要按案例逐章平铺"],
    }


class ReportWriterStructureTests(unittest.TestCase):
    def test_default_requirements_use_topic_structure_and_translation_only_evidence(self):
        requirements = DEFAULT_WRITER_REQUIREMENTS

        self.assertIn("同一 Topic 下的客观题和相关开放题必须结合分析", requirements)
        self.assertIn("Part 内部**禁止使用任何 `###` 或 `####` 标题**", requirements)
        self.assertIn("表头固定为 `玩家ID`、`画像信息`、`中文翻译`", requirements)
        self.assertIn("不得保留原始语言文本", requirements)
        self.assertIn("`**本节总结：**`", requirements)
        self.assertIn("Markdown 编号列表", requirements)
        self.assertIn("`**相关具体信息引用：**`", requirements)
        self.assertIn("不看玩家原文也能立即理解的**大白话**", requirements)
        self.assertIn("优先沿用玩家反馈中文翻译中的具体说法", requirements)
        self.assertIn("不得为了通俗而补造", requirements)
        self.assertIn("未参与调研立项、未看过问卷提纲的读者也能独立理解", requirements)
        self.assertIn("分别回车成短段，不强制编号", requirements)
        self.assertIn("不同范围的观点与风险不得混写", requirements)
        self.assertIn("第一句话就用", requirements)
        self.assertIn("禁止使用「针对……这一核心问题」", requirements)
        self.assertIn("不得写成已经证明因果的「A 导致 B」", requirements)
        self.assertIn("精确人数和百分比", requirements)
        self.assertIn("<subjective_viewpoint_stats>", requirements)
        self.assertIn("`- **提及情况：** X名玩家提及", requirements)
        self.assertIn("`**分析推断：短标题**`", requirements)
        self.assertIn("不得表述为“玩家认为/玩家提及”", requirements)
        self.assertIn("每个 `**观点：观点短标题**` 观点块结束后，必须立即单独写", requirements)
        self.assertIn("每个观点必须分别选择 1–5 条能直接支撑该观点的反馈", requirements)
        self.assertIn("禁止将多个观点的引用合并成 Part 末尾的公共引用表", requirements)
        self.assertIn("不得编造、重复或挪用其它观点的反馈", requirements)
        self.assertIn("必须同时说明对应分母或有效回答范围", requirements)
        self.assertNotIn("**不使用百分比，也不使用精确人数**", requirements)
        self.assertIn("### 少数但值得关注的反馈", requirements)
        self.assertNotIn("高信号少数观点与风险", requirements)
        self.assertNotIn("用 1 段话概括本次调研", requirements)
        self.assertIn("事实优先于洞察强度", requirements)
        self.assertIn("选择题没有提供某个选项", requirements)
        self.assertIn("当前数据无法判断", requirements)
        self.assertIn("不能单独证明它「最多」「最普遍」", requirements)
        self.assertIn("不能自动推出入口容易发现", requirements)
        self.assertIn("不得自行改写来源归属", requirements)
        self.assertIn("数字是佐证，不是正文主体", requirements)
        self.assertIn("玩家为什么这样想、在什么具体场景下发生", requirements)
        self.assertIn("可以接受更长的篇幅", requirements)
        self.assertIn("可以在不同判断中简要复用必要的数字、原因或场景", requirements)
        self.assertIn("不得停留在渠道占比", requirements)
        self.assertIn("产品内部仍可能承接的具体使用场景", requirements)
        self.assertIn("使用 3–5 条 Markdown 编号列表，禁止使用表格", requirements)
        self.assertIn("`- **不确定性/前提：**`", requirements)
        self.assertNotIn("只使用一张 Markdown 表格呈现 3–5 条建议", requirements)
        self.assertNotIn("用连贯的段落文字（不用列表）", requirements)
        self.assertNotIn("`**代表性玩家反馈：**`", requirements)
        self.assertNotIn("每个具体题目必须使用 `### 题目名`", requirements)
        self.assertNotIn("表格必须同时有 `玩家ID` 和 `MLBBID` 两列", requirements)

    def test_part_query_requires_topic_synthesis_without_nested_headings(self):
        query = _build_writer_part_query({
            "i": 2,
            "name": "公频聊天",
            "col_desc": "使用情况(single_choice); 使用原因(open_text)",
        })

        self.assertIn("客观题与相关开放题必须结合分析", query)
        self.assertIn("不要按问卷题目逐题复述", query)
        self.assertIn("禁止使用任何 `###` 或 `####` 标题", query)
        self.assertIn("玩家ID | 画像信息 | 中文翻译", query)
        self.assertIn("3–6 条带加粗短标题的 Markdown 编号列表", query)
        self.assertIn("`**相关具体信息引用：**`", query)
        self.assertIn("不看后文原文也能理解的大白话", query)
        self.assertIn("确需概括时", query)
        self.assertIn("不得补造原文中没有的例子", query)
        self.assertNotIn("`**代表性玩家反馈：**`", query)
        self.assertNotIn("`#### 正面观点`", query)
        self.assertIn("系统会在本节总结之后确定性插入客观题统计表", query)
        self.assertIn("不要自行复制客观题统计表", query)
        self.assertIn("大致代表什么、有哪些样本或解释限制", query)
        self.assertIn("不要机械复述最高项和最低项", query)
        self.assertIn("玩家直接表达的观点", query)
        self.assertIn("<subjective_viewpoint_stats>", query)
        self.assertIn("`- **提及情况：** X名玩家提及", query)
        self.assertIn("`**分析推断：短标题**`", query)
        self.assertIn("每个玩家直接表达的 `**观点：短标题**` 观点块结束后，必须立即单独写", query)
        self.assertIn("该观点自己的 1–5 条 `玩家ID | 画像信息 | 中文翻译` 证据表", query)
        self.assertIn("不足 5 条时展示该观点的全部可用证据", query)
        self.assertIn("禁止将多个观点的引用合并成 Part 末尾的公共引用表", query)
        self.assertIn("不得挪用其他观点的证据", query)
        self.assertIn("事实与证据边界（最高优先级）", query)
        self.assertIn("都只能写为“当前数据无法判断”", query)
        self.assertIn("只有 <stats> 或 <subjective_viewpoint_stats>", query)
        self.assertIn("不能自动推出入口易发现", query)
        self.assertIn("不得自行改写归属", query)

    def test_filtered_part_context_excludes_other_option_responses(self):
        plan = {
            "parts": [
                {
                    "name": "中等按钮反馈",
                    "column_indexes": [1],
                    "filter": {"column_index": 0, "allowed_options": ["中等按钮模式"]},
                },
                {
                    "name": "默认模式反馈",
                    "column_indexes": [1],
                    "filter": {"column_index": 0, "allowed_options": ["默认模式"]},
                },
            ],
            "columns": [
                {"index": 0, "name": "控制模式", "role": "single_choice"},
                {"index": 1, "name": "选择原因", "role": "open_text"},
            ],
        }
        open_text = {
            1: [
                {"ids": {}, "profile": {}, "segments": {"0": "中等按钮模式"}, "text": "大小正好"},
                {"ids": {}, "profile": {}, "segments": {"0": "默认模式"}, "text": "已经习惯"},
            ],
        }

        plan_md, open_text_md, requirements = _build_writer_context(
            "", open_text, plan, ["模式", "原因"],
        )
        part_query = _build_writer_part_query({
            "i": 1,
            "name": "中等按钮反馈",
            "col_desc": "选择原因(open_text)",
            "filter_desc": "适用人群：控制模式选择「中等按钮模式」",
        })

        self.assertIn("适用人群：控制模式选择「中等按钮模式」", plan_md)
        self.assertIn("Part 1 中等按钮反馈 / 选择原因", open_text_md)
        self.assertIn("大小正好", open_text_md)
        self.assertIn("Part 2 默认模式反馈 / 选择原因", open_text_md)
        self.assertIn("已经习惯", open_text_md)
        self.assertIn("分组选项成章强制规则", requirements)
        self.assertIn("不得混入其他选项玩家", part_query)

    def test_core_query_requires_plain_language_grounded_in_player_wording(self):
        query = _build_writer_core_query(
            [{"i": 1, "name": "皮肤编号", "col_desc": "编号评价(open_text)"}],
            has_bug=False,
        )

        self.assertIn("不看玩家原文也能立即理解的大白话", query)
        self.assertIn("优先沿用玩家中文翻译中的具体词语", query)
        self.assertIn("功能性增益", query)
        self.assertIn("具体希望增加、取消或改变什么", query)
        self.assertIn("解释和例子只能来自 <open_text> 或已生成章节", query)
        self.assertIn("未参与调研立项、未看过问卷提纲的读者也能独立理解", query)
        self.assertIn("分别回车成短段，不使用 1、2、3 编号", query)
        self.assertIn("不得写成一个超长段落", query)
        self.assertIn("不同范围不得混写", query)
        self.assertIn("不得机械套用标签或补造研究阶段", query)
        self.assertIn("不要复述、转述或重新提出业务问题或调研需求", query)
        self.assertIn("第一句话就用", query)
        self.assertIn("证据显示相关", query)
        self.assertIn("从本次调研看，A 与 B 有关", query)
        self.assertIn("不得写成已经证明因果的「A 导致 B」", query)
        self.assertIn("精确人数和百分比", query)
        self.assertIn("玩家原话没有直接表达", query)
        self.assertIn("不得写成玩家的逻辑", query)
        self.assertIn("必须说明对应分母或有效回答范围", query)
        self.assertNotIn("核心结论里不使用百分比、不使用精确人数", query)
        self.assertIn("### 少数但值得关注的反馈", query)
        self.assertNotIn("高信号少数观点与风险", query)
        self.assertIn("有必要、重要且必须优先展示的跨题判断", query)
        self.assertIn("第一句实质判断", query)
        self.assertIn("全报告最高优先级的信息", query)
        self.assertIn("必须先写跨题洞察、判断标准或取舍逻辑", query)
        self.assertIn("不得以“方案X排名第一/获得最多第一名/满意度最高”", query)
        self.assertIn("使用 `**加粗**` 标出这些关键标准", query)
        self.assertIn("不设置段数和字数硬限制", query)
        self.assertIn("不要写成后续业务小节的目录式预告", query)
        self.assertIn("不要机械汇总各 Part", query)
        self.assertIn("不设置小标题或最终观点数量上限", query)
        self.assertIn("不要为了形式机械地给每个 Part 都设置一个核心小节", query)
        self.assertIn("可以原样复用或少量调整", query)
        self.assertIn("不要求保留或删除 `Part X` 编号", query)
        self.assertIn("可以在不同业务判断中按需引用同一证据", query)
        self.assertIn("不得逐字复制同一整段内容", query)
        self.assertIn("每个业务小节优先用有序列表", query)
        self.assertIn("主要发现 → 玩家原因与具体场景 → 分析推断 → 产品含义 → 证据边界", query)
        self.assertIn("人数和占比只用于支撑判断", query)
        self.assertIn("不能替代原因、场景和逻辑", query)
        self.assertIn("主要观点必须完整说明数量和逻辑", query)
        self.assertIn("提及率低于 5% 的观点视为低频补充", query)
        self.assertIn("这里只控制核心结论的展示层级", query)
        self.assertIn("其他小节为建立跨题逻辑时可以简要引用必要的数字、原因或场景", query)
        self.assertIn("不要再原样复制到 `### 少数但值得关注的反馈`", query)
        self.assertIn("待确认问题概述` 按问题类型使用有序列表", query)
        self.assertIn("`**加粗**`", query)
        self.assertIn("`*斜体*`", query)
        self.assertIn("`<u>下划线</u>`", query)
        self.assertIn("判断标准、核心取舍、结论短语和产品优先级", query)
        self.assertIn("禁止给仅陈述方案排名、样本量、人数、占比、均值", query)
        self.assertIn("这类数字应放在判断之后作为普通证据", query)
        self.assertIn("避免连续堆叠星号造成格式损坏", query)
        self.assertIn("事实优先于洞察强度", query)
        self.assertIn("不得据此断言该人群或行为不存在", query)
        self.assertIn("没有对应观点统计目录时", query)
        self.assertIn("不能自动推出入口易发现", query)
        self.assertIn("不得自行改写归属", query)
        self.assertIn("应当基于真实的跨题关系", query)
        self.assertIn("明确标注“分析推断”", query)
        self.assertIn("不得停留在渠道占比", query)
        self.assertIn("产品内部仍可能承接的", query)
        self.assertNotIn("否则逐个写 `### Part X", query)

    def test_analysis_focus_reaches_explicit_standard_writer_rounds(self):
        focus = _analysis_focus()
        plan = {
            "parts": [{"name": "案例证据", "column_indexes": [0]}],
            "columns": [{"index": 0, "name": "案例反馈", "role": "open_text"}],
            "analysis_focus": focus,
        }
        part_meta = {
            "i": 1,
            "name": "案例证据",
            "col_desc": "案例反馈(open_text)",
        }

        first_query = _build_writer_first_query("", {}, plan, ["案例反馈"])
        core_query = _build_writer_core_query(
            [part_meta],
            has_bug=False,
            analysis_focus=focus,
        )
        selected_core = (
            "<!--CORE_START-->\n## 核心结论\n跨案例判断。\n<!--CORE_END-->"
        )
        action_query = _build_writer_action_query(
            [part_meta],
            has_bug=False,
            analysis_focus=focus,
            selected_core=selected_core,
        )

        for query in (first_query, core_query, action_query):
            self.assertIn("<analysis_focus>", query)
            self.assertIn("三个案例共同说明了什么", query)
            self.assertIn("投入判断", query)
            self.assertIn("不要按案例逐章平铺", query)

        self.assertIn("<selected_core>", action_query)
        self.assertIn("跨案例判断", action_query)
        self.assertEqual(_build_analysis_focus_block(None), "")

    def test_core_review_prioritizes_deliverables_and_promotes_cross_case_framework(self):
        query = _build_writer_core_review_query(_analysis_focus(), has_bug=False)

        self.assertIn("expected_deliverables", query)
        self.assertIn("最高优先级", query)
        self.assertIn("跨案例对比框架", query)
        self.assertIn("上提", query)
        self.assertIn("不能用机械逐 Part 摘要代替", query)
        self.assertIn("PASS", query)
        self.assertIn("<!--CORE_REPAIRS_START-->", query)
        self.assertIn("<!--CORE_REPAIRS_END-->", query)
        self.assertIn("<!--CORE_REPAIR_START-->", query)
        self.assertIn("<original>", query)
        self.assertIn("<replacement>", query)
        self.assertIn("覆盖与表达复核", query)
        self.assertIn("未参与调研立项的读者也能理解的大白话", query)
        self.assertIn("针对……这个/这一问题", query)
        self.assertIn("已经清楚、正确的段落不会被修补协议触及", query)
        self.assertIn("观点提及人数继续与 <subjective_viewpoint_stats> 一致", query)
        self.assertIn("必须保留“分析推断”标识", query)
        self.assertIn("不得用连续超长段落", query)
        self.assertIn("不限制段数和字数", query)
        self.assertIn("不得机械汇总各 Part", query)
        self.assertIn("主要观点应以有序列表展开", query)
        self.assertIn("不得为了形式逐 Part 搬运总结", query)
        self.assertIn("本节总结可以直接复用或少量调整", query)
        self.assertIn("是否保留 `Part X` 编号由可读性决定", query)
        self.assertIn("低频观点应简要保留其存在", query)
        self.assertIn("少数反馈不得原样复制业务小节已有整段内容", query)
        self.assertIn("待确认问题必须按类型列成短条目", query)
        self.assertIn("不得把后台不同主题合并成一个宽泛结论", query)
        self.assertIn("缺失的问卷选项", query)
        self.assertIn("不得使用“最多”“最普遍”", query)
        self.assertIn("均视为不合格", query)
        self.assertIn("当前数据无法判断", query)
        self.assertIn("局部修补清单", query)
        self.assertIn("只选择最小必要范围", query)
        self.assertIn("不得输出完整 CORE 替换稿", query)
        self.assertIn("人数与占比只能支撑判断", query)
        self.assertIn("分析推断不是错误", query)
        self.assertIn("首句优先级必须单独复核", query)
        self.assertIn("即使这些判断标准已在第二段或后文出现", query)
        self.assertIn("也必须判定为不合格", query)
        self.assertIn("局部修补将判断标准上提", query)
        self.assertIn("事实句和数字不得单独使用 `<u>下划线</u>`", query)
        self.assertIn("只修补对应句段，不得改动无关内容", query)

    def test_quantitative_part_query_prioritizes_objective_statistics(self):
        query = _build_writer_part_query({
            "i": 1,
            "name": "使用情况",
            "col_desc": "使用频率(single_choice); 使用原因(open_text)",
        }, quantitative_first=True)

        self.assertIn("本次为定量优先报告", query)
        self.assertIn("主要分布、最高/最低项和显著差异", query)
        self.assertIn("完整逐题统计表由系统在附录确定性插入", query)
        self.assertNotIn("本节总结之后确定性插入客观题统计表", query)

    def test_large_sample_mode_keeps_qualitative_stats_rule_out_of_quantitative_query(self):
        plan = {"parts": [], "columns": []}
        qualitative = _build_large_sample_writer_query(
            "", {}, plan, [], quantitative_first=False,
        )
        quantitative = _build_large_sample_writer_query(
            "", {}, plan, [], quantitative_first=True,
        )

        self.assertIn("系统会在对应 Part 内确定性插入客观题统计表", qualitative)
        self.assertNotIn("系统会在对应 Part 内确定性插入客观题统计表", quantitative)

    def test_crosstab_planning_uses_business_context(self):
        context = {
            "problem": "判断新剧情是否值得继续投入",
            "key_concerns": "不同熟悉度玩家的认知差异",
            "target_users": "剧情内容玩家",
        }

        initial = _build_crosstab_planner_query("Q1", ["熟悉度"], [], context)
        revised = _build_crosstab_plan_revision_query(
            "Q1", ["熟悉度"], [], [{"name": "认知"}], "突出差异", context,
        )

        for query in (initial, revised):
            self.assertIn("判断新剧情是否值得继续投入", query)
            self.assertIn("不同熟悉度玩家的认知差异", query)
            self.assertIn("剧情内容玩家", query)

    def test_action_query_requires_nested_list_and_fact_boundaries(self):
        query = _build_writer_action_query(
            [{"i": 1, "name": "聊天体验", "col_desc": "体验反馈(open_text)"}],
            has_bug=False,
        )

        self.assertIn("使用 Markdown 编号列表，禁止使用表格", query)
        self.assertIn("`1. **建议短标题**（优先级：高/中/低）`", query)
        self.assertIn("`- **核心判断：**`", query)
        self.assertIn("`- **不确定性/前提：**`", query)
        self.assertIn("缺失选项、未询问原因、未覆盖人群", query)
        self.assertIn("把补充数据、访谈或实验作为验证动作", query)
        self.assertNotIn("建议内容 | 优先级 | 产品动作", query)

        repair = _build_writer_action_repair_query()
        self.assertIn("Markdown 编号列表", repair)
        self.assertIn("禁止使用表格", repair)
        self.assertNotIn("整理为一张表格", repair)

    def test_action_normalizer_accepts_nested_list_and_converts_table(self):
        valid = (
            "### 行动建议（修正版）\n\n"
            "1. **修复播放体验**（优先级：高）\n"
            "   - **核心判断：** 播放问题需要优先验证。\n"
            "   - **产品动作：** 分层排查。\n"
            "   - **验证方式：** 对比卡顿率。\n"
            "   - **依据：** 玩家报告卡顿。\n"
            "   - **不确定性/前提：** 仍需区分机型。"
        )
        table = (
            "## 行动建议\n\n"
            "| 建议内容 | 优先级 | 产品动作 | 验证方式 | 依据 | 不确定性/前提 |\n"
            "|---|---|---|---|---|---|\n"
            "| 修复播放 | 高 | 排查 | 测试 | 卡顿反馈 | 机型未知 |"
        )

        normalized = _normalize_action_section(valid)
        self.assertTrue(normalized.startswith("## 行动建议\n\n1."))
        converted = _normalize_action_section(table)
        self.assertIn("1. **修复播放**（优先级：高）", converted)
        self.assertIn("- **产品动作：** 排查", converted)
        self.assertIn("- **不确定性/前提：** 机型未知", converted)
        self.assertNotIn("| 建议内容 |", converted)

    def test_writer_context_merges_all_identity_sources_into_player_id(self):
        plan = {
            "parts": [{"name": "聊天体验", "column_indexes": [1]}],
            "columns": [{"index": 1, "name": "体验反馈", "role": "open_text"}],
        }
        open_text = {
            1: [{
                "ids": {"Discord用户ID": "discord-1", "MLBBID": "mlbb-2"},
                "profile": {"好友数量": "1~5"},
                "text": "Too many spam messages.",
            }],
        }

        _, open_text_md, requirements = _build_writer_context("", open_text, plan, ["ID", "体验反馈"])

        self.assertIn("玩家ID=discord-1 / mlbb-2", open_text_md)
        self.assertNotIn("MLBBID=mlbb-2", open_text_md)
        self.assertIn("只能使用一个 `玩家ID` 列", requirements)
        self.assertIn("不得展示原始语言文本", requirements)

    def test_qa_context_contains_report_and_questionnaire_evidence(self):
        source = {
            "report_md": "# 聊天功能报告\n\n## 核心结论\n存在消息丢失反馈。",
            "stats_md": "有效样本(总计):总体=1",
            "questionnaire_text": "Q1：是否遇到聊天问题？",
            "qualitative_context": {"problem": "了解聊天体验"},
            "plan": {
                "parts": [{"name": "聊天体验", "column_indexes": [1]}],
                "columns": [{"index": 1, "name": "聊天反馈", "role": "open_text"}],
            },
            "rows": [["玩家ID", "聊天反馈"], ["p-1", "切换设备后消息消失"]],
        }

        context = _build_qa_context(source)

        self.assertIn("<report>", context)
        self.assertIn("存在消息丢失反馈", context)
        self.assertIn("<analysis_plan>", context)
        self.assertIn("有效样本(总计):总体=1", context)
        self.assertIn("Q1：是否遇到聊天问题？", context)
        self.assertIn("切换设备后消息消失", context)

    def test_qa_context_preserves_repeated_headers_with_semantic_column_names(self):
        repeated_header = "How satisfied are you with this design overall?"
        source = {
            "plan": {
                "parts": [],
                "columns": [
                    {"index": 1, "name": "概念1整体满意度", "role": "scale"},
                    {"index": 2, "name": "概念2整体满意度", "role": "scale"},
                    {"index": 3, "name": "概念3整体满意度", "role": "scale"},
                ],
            },
            "rows": [
                ["玩家ID", repeated_header, repeated_header, repeated_header],
                ["p-1", "2", "4", "5"],
            ],
        }

        context = _build_qa_context(source)
        rows_block = context.split("<rows>\n", 1)[1].split("\n</rows>", 1)[0]
        row = json.loads(rows_block)

        self.assertEqual(row["col_1｜概念1整体满意度｜原题：" + repeated_header], "2")
        self.assertEqual(row["col_2｜概念2整体满意度｜原题：" + repeated_header], "4")
        self.assertEqual(row["col_3｜概念3整体满意度｜原题：" + repeated_header], "5")
        self.assertEqual(len(row), 4)

if __name__ == "__main__":
    unittest.main()
