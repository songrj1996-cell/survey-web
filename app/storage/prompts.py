"""storage/prompts:可配置提示词目录的读取、迁移与原子写入。"""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import tempfile
import threading

from app.core.config import (
    DEFAULT_ANNOTATE_AI_SYSTEM_PROMPT,
    DEFAULT_ANNOTATE_QUALITY_SYSTEM_PROMPT,
    DEFAULT_ANNOTATE_TRANSLATION_SYSTEM_PROMPT,
    DEFAULT_COLUMN_DETECT_SYSTEM_PROMPT,
    DEFAULT_COMMENT_CLASSIFY_SYSTEM_PROMPT,
    DEFAULT_COMMENT_EXTRACT_SYSTEM_PROMPT,
    DEFAULT_COMMENT_MERGE_SYSTEM_PROMPT,
    DEFAULT_COMMENT_QUOTE_BATCH_SYSTEM_PROMPT,
    DEFAULT_COMMENT_QUOTE_FINAL_SYSTEM_PROMPT,
    DEFAULT_COMMENT_RELEVANCE_SYSTEM_PROMPT,
    DEFAULT_COMMENT_REPORT_SYSTEM_PROMPT,
    DEFAULT_CROSSTAB_PLANNER_SYSTEM_PROMPT,
    DEFAULT_INTERVIEW_AUDIT_SYSTEM_PROMPT,
    DEFAULT_INTERVIEW_EXTRACT_SYSTEM_PROMPT,
    DEFAULT_INTERVIEW_REPAIR_SYSTEM_PROMPT,
    DEFAULT_INTERVIEW_REPORT_SYSTEM_PROMPT,
    DEFAULT_INTERVIEW_V2_ATTRIBUTE_SYSTEM_PROMPT,
    DEFAULT_INTERVIEW_V2_ANALYSIS_SYSTEM_PROMPT,
    DEFAULT_INTERVIEW_V2_DOSSIER_SYSTEM_PROMPT,
    DEFAULT_INTERVIEW_V2_REPORT_AUDIT_SYSTEM_PROMPT,
    DEFAULT_INTERVIEW_V2_REPORT_SYSTEM_PROMPT,
    DEFAULT_LARGE_SAMPLE_WRITER_REQUIREMENTS,
    DEFAULT_PLANNER_EXTRA,
    DEFAULT_QUESTIONNAIRE_TRANSLATION_SYSTEM_PROMPT,
    DEFAULT_REPORT_QA_SYSTEM_PROMPT,
    DEFAULT_REPORT_WRITER_SYSTEM_PROMPT,
    DEFAULT_RESPONSE_CLASSIFY_SYSTEM_PROMPT,
    DEFAULT_SURVEY_PLANNER_SYSTEM_PROMPT,
    DEFAULT_THEME_EXTRACT_SYSTEM_PROMPT,
    DEFAULT_THEME_MERGE_SYSTEM_PROMPT,
    DEFAULT_WRITER_REQUIREMENTS,
    PROMPTS_FILE,
)

_PROMPTS_LOCK = threading.RLock()
_METADATA_FIELDS = (
    "key",
    "label",
    "description",
    "group",
    "group_order",
    "order",
    "kind",
    "editable",
)
_RETIRED_METADATA_FIELDS = ("dify_app", "dify_url")


def _prompt(
    key: str,
    label: str,
    description: str,
    *,
    group: str,
    group_order: int,
    order: int,
    kind: str,
    current: str,
    version: int = 1,
) -> dict:
    return {
        "key": key,
        "label": label,
        "description": description,
        "group": group,
        "group_order": group_order,
        "order": order,
        "kind": kind,
        "editable": True,
        "current": current,
        "history": [],
        "version": version,
    }


DEFAULT_PROMPTS: dict = {
    "questionnaire_translation_system": _prompt(
        "questionnaire_translation_system",
        "原问卷中文翻译 System Prompt",
        "用于把来源问卷的题干、选项和矩阵行翻译为中文；题型和结构由代码保持。",
        group="问卷理解与规划",
        group_order=10,
        order=10,
        kind="system",
        current=DEFAULT_QUESTIONNAIRE_TRANSLATION_SYSTEM_PROMPT,
    ),
    "column_detect_system": _prompt(
        "column_detect_system",
        "题型识别 System Prompt",
        "用于识别回收表列的题型、生成中文短名并归并多语言选项。",
        group="问卷理解与规划",
        group_order=10,
        order=20,
        kind="system",
        current=DEFAULT_COLUMN_DETECT_SYSTEM_PROMPT,
        version=2,
    ),
    "survey_planner_system": _prompt(
        "survey_planner_system",
        "问卷方案规划 System Prompt",
        "用于生成和修订普通问卷的分析方案，约束列结构、章节与交叉分析。",
        group="问卷理解与规划",
        group_order=10,
        order=30,
        kind="system",
        current=DEFAULT_SURVEY_PLANNER_SYSTEM_PROMPT,
        version=4,
    ),
    "crosstab_planner_system": _prompt(
        "crosstab_planner_system",
        "跑数表章节规划 System Prompt",
        "用于生成和修订跑数表报告的主题化章节大纲。",
        group="问卷理解与规划",
        group_order=10,
        order=40,
        kind="system",
        current=DEFAULT_CROSSTAB_PLANNER_SYSTEM_PROMPT,
    ),
    "planner_extra": _prompt(
        "planner_extra",
        "问卷方案补充指令",
        "附加到普通问卷方案规划请求，补充章节、交叉分析和待确认问题的业务规则。",
        group="问卷理解与规划",
        group_order=10,
        order=50,
        kind="instruction",
        current=DEFAULT_PLANNER_EXTRA,
        version=3,
    ),
    "report_writer_system": _prompt(
        "report_writer_system",
        "问卷报告写作 System Prompt",
        "用于约束标准问卷和大样本问卷报告的证据边界、数字使用与分轮输出。",
        group="报告生成与追问",
        group_order=20,
        order=10,
        kind="system",
        current=DEFAULT_REPORT_WRITER_SYSTEM_PROMPT,
    ),
    "writer_requirements": _prompt(
        "writer_requirements",
        "标准问卷报告写作要求",
        "附加到标准问卷报告写作上下文，定义报告结构、证据展示、语言风格和建议格式。",
        group="报告生成与追问",
        group_order=20,
        order=20,
        kind="instruction",
        current=DEFAULT_WRITER_REQUIREMENTS,
        version=14,
    ),
    "large_sample_writer_requirements": _prompt(
        "large_sample_writer_requirements",
        "大样本报告写作要求",
        "用于跑数表或大样本问卷报告的稳定写作要求；满意度和业务背景规则由运行时追加。",
        group="报告生成与追问",
        group_order=20,
        order=30,
        kind="instruction",
        current=DEFAULT_LARGE_SAMPLE_WRITER_REQUIREMENTS,
    ),
    "report_qa_system": _prompt(
        "report_qa_system",
        "报告追问 System Prompt",
        "用于普通问卷、大样本问卷和跑数表报告的证据型追问回答。",
        group="报告生成与追问",
        group_order=20,
        order=40,
        kind="system",
        current=DEFAULT_REPORT_QA_SYSTEM_PROMPT,
    ),
    "theme_extract_system": _prompt(
        "theme_extract_system",
        "大样本主题提取 System Prompt",
        "用于从每批多语言开放题回答中提取候选主题。",
        group="大样本开放题",
        group_order=30,
        order=10,
        kind="system",
        current=DEFAULT_THEME_EXTRACT_SYSTEM_PROMPT,
        version=3,
    ),
    "theme_merge_system": _prompt(
        "theme_merge_system",
        "大样本主题合并 System Prompt",
        "用于跨批次合并候选主题，去重后生成连续主题 ID。",
        group="大样本开放题",
        group_order=30,
        order=20,
        kind="system",
        current=DEFAULT_THEME_MERGE_SYSTEM_PROMPT,
        version=2,
    ),
    "response_classify_system": _prompt(
        "response_classify_system",
        "大样本回答分类 System Prompt",
        "用于把每条开放题回答归入最终主题并判断情感倾向。",
        group="大样本开放题",
        group_order=30,
        order=30,
        kind="system",
        current=DEFAULT_RESPONSE_CLASSIFY_SYSTEM_PROMPT,
        version=2,
    ),
    "comment_relevance_system": _prompt(
        "comment_relevance_system",
        "评论相关性筛选 System Prompt",
        "用于判断多语言评论是否与帖子主题相关且有分析价值。",
        group="评论分析",
        group_order=40,
        order=10,
        kind="system",
        current=DEFAULT_COMMENT_RELEVANCE_SYSTEM_PROMPT,
    ),
    "comment_extract_system": _prompt(
        "comment_extract_system",
        "评论主题提取 System Prompt",
        "用于从已通过相关性筛选的评论中提取候选主题。",
        group="评论分析",
        group_order=40,
        order=20,
        kind="system",
        current=DEFAULT_COMMENT_EXTRACT_SYSTEM_PROMPT,
    ),
    "comment_merge_system": _prompt(
        "comment_merge_system",
        "评论主题合并 System Prompt",
        "用于跨批次合并候选主题并生成稳定主题 ID。",
        group="评论分析",
        group_order=40,
        order=30,
        kind="system",
        current=DEFAULT_COMMENT_MERGE_SYSTEM_PROMPT,
    ),
    "comment_classify_system": _prompt(
        "comment_classify_system",
        "评论分类 System Prompt",
        "用于将每条评论多标签归类、判断情感并翻译代表引用。",
        group="评论分析",
        group_order=40,
        order=40,
        kind="system",
        current=DEFAULT_COMMENT_CLASSIFY_SYSTEM_PROMPT,
    ),
    "comment_report_system": _prompt(
        "comment_report_system",
        "评论舆情简报 System Prompt",
        "用于基于本地统计结果生成中文核心结论、玩家观点和业务建议。",
        group="评论分析",
        group_order=40,
        order=50,
        kind="system",
        current=DEFAULT_COMMENT_REPORT_SYSTEM_PROMPT,
    ),
    "comment_quote_batch_system": _prompt(
        "comment_quote_batch_system",
        "评论原文初筛 System Prompt",
        "用于分批筛选表达完整、有业务价值的长评论并翻译为中文。",
        group="评论分析",
        group_order=40,
        order=60,
        kind="system",
        current=DEFAULT_COMMENT_QUOTE_BATCH_SYSTEM_PROMPT,
    ),
    "comment_quote_final_system": _prompt(
        "comment_quote_final_system",
        "评论原文精选 System Prompt",
        "用于从各批候选中精选最终展示的多样化玩家原文。",
        group="评论分析",
        group_order=40,
        order=70,
        kind="system",
        current=DEFAULT_COMMENT_QUOTE_FINAL_SYSTEM_PROMPT,
    ),
    "annotate_ai_system": _prompt(
        "annotate_ai_system",
        "AI 作答识别 System Prompt",
        "用于判断玩家开放题回答的 AI 生成或润色概率，并给出证据与反证。",
        group="数据标注",
        group_order=50,
        order=10,
        kind="system",
        current=DEFAULT_ANNOTATE_AI_SYSTEM_PROMPT,
    ),
    "annotate_quality_system": _prompt(
        "annotate_quality_system",
        "回答质量识别 System Prompt",
        "用于逐题标记玩家开放题回答的质量类型、理由与证据。",
        group="数据标注",
        group_order=50,
        order=20,
        kind="system",
        current=DEFAULT_ANNOTATE_QUALITY_SYSTEM_PROMPT,
        version=2,
    ),
    "annotate_translation_system": _prompt(
        "annotate_translation_system",
        "标注中文翻译 System Prompt",
        "用于补齐标注结果中缺失的题头或玩家回答中文翻译。",
        group="数据标注",
        group_order=50,
        order=30,
        kind="system",
        current=DEFAULT_ANNOTATE_TRANSLATION_SYSTEM_PROMPT,
    ),
    "interview_extract_system": _prompt(
        "interview_extract_system",
        "访谈证据归并 System Prompt",
        "用于识别多 Sheet 中的玩家、模块和互补证据，并保留可追溯的单元格引用。",
        group="访谈报告",
        group_order=60,
        order=10,
        kind="system",
        current=DEFAULT_INTERVIEW_EXTRACT_SYSTEM_PROMPT,
    ),
    "interview_report_system": _prompt(
        "interview_report_system",
        "访谈模块写作 System Prompt",
        "用于根据归并证据逐模块撰写需求、形成逻辑、主要发现与产品建议。",
        group="访谈报告",
        group_order=60,
        order=20,
        kind="system",
        current=DEFAULT_INTERVIEW_REPORT_SYSTEM_PROMPT,
    ),
    "interview_repair_system": _prompt(
        "interview_repair_system",
        "访谈模块修订 System Prompt",
        "用于按结构校验或审校问题修订单个访谈报告模块，不引入新证据。",
        group="访谈报告",
        group_order=60,
        order=30,
        kind="system",
        current=DEFAULT_INTERVIEW_REPAIR_SYSTEM_PROMPT,
    ),
    "interview_audit_system": _prompt(
        "interview_audit_system",
        "访谈报告审校 System Prompt",
        "用于审核访谈报告的需求逻辑、逐玩家证据、引用一致性和建议边界。",
        group="访谈报告",
        group_order=60,
        order=40,
        kind="system",
        current=DEFAULT_INTERVIEW_AUDIT_SYSTEM_PROMPT,
    ),
    "interview_v2_attribute_system": _prompt(
        "interview_v2_attribute_system",
        "访谈 V2 玩家属性抽取 System Prompt",
        "仅从当前玩家背景证据抽取可追溯属性事实和独立分析标签。",
        group="访谈报告",
        group_order=60,
        order=50,
        kind="system",
        current=DEFAULT_INTERVIEW_V2_ATTRIBUTE_SYSTEM_PROMPT,
    ),
    "interview_v2_dossier_system": _prompt(
        "interview_v2_dossier_system",
        "访谈 V2 玩家档案 System Prompt",
        "基于当前玩家证据白名单重建行为逻辑、矛盾和缺失信息。",
        group="访谈报告",
        group_order=60,
        order=60,
        kind="system",
        current=DEFAULT_INTERVIEW_V2_DOSSIER_SYSTEM_PROMPT,
    ),
    "interview_v2_analysis_system": _prompt(
        "interview_v2_analysis_system",
        "访谈 V2 跨玩家分析 System Prompt",
        "基于当前档案、覆盖矩阵和证据白名单生成跨玩家发现，不自行计算人数。",
        group="访谈报告",
        group_order=60,
        order=70,
        kind="system",
        current=DEFAULT_INTERVIEW_V2_ANALYSIS_SYSTEM_PROMPT,
    ),
    "interview_v2_report_system": _prompt(
        "interview_v2_report_system",
        "访谈 V2 研究报告写作 System Prompt",
        "基于冻结分析版本按固定七章结构生成逐句可审计的研究报告。",
        group="访谈报告",
        group_order=60,
        order=80,
        kind="system",
        current=DEFAULT_INTERVIEW_V2_REPORT_SYSTEM_PROMPT,
    ),
    "interview_v2_report_audit_system": _prompt(
        "interview_v2_report_audit_system",
        "访谈 V2 研究报告审校 System Prompt",
        "在确定性证据审计之上补充检查反例、限定和建议边界。",
        group="访谈报告",
        group_order=60,
        order=90,
        kind="system",
        current=DEFAULT_INTERVIEW_V2_REPORT_AUDIT_SYSTEM_PROMPT,
    ),
}


def _version_number(value) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _atomic_write_json(path: str, value: dict) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{os.path.basename(path)}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(value, temp_file, ensure_ascii=False, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
        temp_path = ""
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass


def _sync_entry(entry: dict, default: dict) -> bool:
    dirty = False
    if "current" not in entry:
        entry["current"] = default["current"]
        dirty = True
    if "history" not in entry:
        entry["history"] = []
        dirty = True

    stored_version = _version_number(entry.get("version"))
    default_version = _version_number(default.get("version"))
    if stored_version < default_version:
        if not entry.get("history"):
            entry["current"] = default["current"]
        entry["version"] = default_version
        dirty = True
    elif entry.get("version") != stored_version:
        entry["version"] = stored_version
        dirty = True

    for field in _METADATA_FIELDS:
        expected = default[field]
        if entry.get(field) != expected:
            entry[field] = deepcopy(expected)
            dirty = True
    for field in _RETIRED_METADATA_FIELDS:
        if field in entry:
            entry.pop(field, None)
            dirty = True
    return dirty


def _load_prompts() -> dict:
    with _PROMPTS_LOCK:
        if not os.path.exists(PROMPTS_FILE):
            data = deepcopy(DEFAULT_PROMPTS)
            _atomic_write_json(PROMPTS_FILE, data)
            return data
        with open(PROMPTS_FILE, "r", encoding="utf-8") as prompt_file:
            data = json.load(prompt_file)
        if not isinstance(data, dict):
            raise ValueError("提示词存储格式无效")

        dirty = False
        for key, default in DEFAULT_PROMPTS.items():
            entry = data.get(key)
            if entry is None:
                data[key] = deepcopy(default)
                dirty = True
                continue
            if not isinstance(entry, dict):
                raise ValueError("提示词条目格式无效")
            dirty = _sync_entry(entry, default) or dirty
        if dirty:
            _atomic_write_json(PROMPTS_FILE, data)
        return data


def _save_prompts(prompts: dict) -> None:
    with _PROMPTS_LOCK:
        _atomic_write_json(PROMPTS_FILE, prompts)


@contextmanager
def _prompt_update_transaction():
    """在同一进程锁内完成读取、业务修改和原子写入。"""
    with _PROMPTS_LOCK:
        prompts = _load_prompts()
        yield prompts
        _atomic_write_json(PROMPTS_FILE, prompts)


def _get_prompt_text(key: str) -> str:
    return _load_prompts()[key]["current"]


def _get_questionnaire_translation_system_prompt() -> str:
    return _get_prompt_text("questionnaire_translation_system")


def _get_column_detect_system_prompt() -> str:
    return _get_prompt_text("column_detect_system")


def _get_survey_planner_system_prompt() -> str:
    return _get_prompt_text("survey_planner_system")


def _get_crosstab_planner_system_prompt() -> str:
    return _get_prompt_text("crosstab_planner_system")


def _get_planner_extra() -> str:
    return _get_prompt_text("planner_extra")


def _get_report_writer_system_prompt() -> str:
    return _get_prompt_text("report_writer_system")


def _get_writer_requirements() -> str:
    return _get_prompt_text("writer_requirements")


def _get_large_sample_writer_requirements() -> str:
    return _get_prompt_text("large_sample_writer_requirements")


def _get_report_qa_system_prompt() -> str:
    return _get_prompt_text("report_qa_system")


def _get_theme_extract_system_prompt() -> str:
    return _get_prompt_text("theme_extract_system")


def _get_theme_merge_system_prompt() -> str:
    return _get_prompt_text("theme_merge_system")


def _get_response_classify_system_prompt() -> str:
    return _get_prompt_text("response_classify_system")


def _get_comment_relevance_system_prompt() -> str:
    return _get_prompt_text("comment_relevance_system")


def _get_comment_extract_system_prompt() -> str:
    return _get_prompt_text("comment_extract_system")


def _get_comment_merge_system_prompt() -> str:
    return _get_prompt_text("comment_merge_system")


def _get_comment_classify_system_prompt() -> str:
    return _get_prompt_text("comment_classify_system")


def _get_comment_report_system_prompt() -> str:
    return _get_prompt_text("comment_report_system")


def _get_comment_quote_batch_system_prompt() -> str:
    return _get_prompt_text("comment_quote_batch_system")


def _get_comment_quote_final_system_prompt() -> str:
    return _get_prompt_text("comment_quote_final_system")


def _get_annotate_ai_system_prompt() -> str:
    return _get_prompt_text("annotate_ai_system")


def _get_annotate_quality_system_prompt() -> str:
    return _get_prompt_text("annotate_quality_system")


def _get_annotate_translation_system_prompt() -> str:
    return _get_prompt_text("annotate_translation_system")


def _get_interview_extract_system_prompt() -> str:
    return _get_prompt_text("interview_extract_system")


def _get_interview_report_system_prompt() -> str:
    return _get_prompt_text("interview_report_system")


def _get_interview_repair_system_prompt() -> str:
    return _get_prompt_text("interview_repair_system")


def _get_interview_audit_system_prompt() -> str:
    return _get_prompt_text("interview_audit_system")


def _get_interview_v2_attribute_system_prompt() -> str:
    return _get_prompt_text("interview_v2_attribute_system")


def _get_interview_v2_dossier_system_prompt() -> str:
    return _get_prompt_text("interview_v2_dossier_system")


def _get_interview_v2_analysis_system_prompt() -> str:
    return _get_prompt_text("interview_v2_analysis_system")


def _get_interview_v2_report_system_prompt() -> str:
    return _get_prompt_text("interview_v2_report_system")


def _get_interview_v2_report_audit_system_prompt() -> str:
    return _get_prompt_text("interview_v2_report_audit_system")
