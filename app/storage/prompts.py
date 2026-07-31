"""storage/prompts:提示词(prompts.json)的读写与默认值。

默认提示词文案(DEFAULT_UPLOAD_GUIDE 等)来自 core/config;此处负责持久化与默认升级。
"""
import json
import os

from app.core.config import (
    DEFAULT_COLUMN_DETECT_SYSTEM_PROMPT,
    DEFAULT_CROSSTAB_PLANNER_SYSTEM_PROMPT,
    DEFAULT_PLANNER_EXTRA,
    DEFAULT_RESPONSE_CLASSIFY_SYSTEM_PROMPT,
    DEFAULT_REPORT_QA_SYSTEM_PROMPT,
    DEFAULT_SURVEY_PLANNER_SYSTEM_PROMPT,
    DEFAULT_THEME_EXTRACT_SYSTEM_PROMPT,
    DEFAULT_THEME_MERGE_SYSTEM_PROMPT,
    DEFAULT_UPLOAD_GUIDE,
    DEFAULT_WRITER_REQUIREMENTS,
    PROMPTS_FILE,
)

DEFAULT_PROMPTS: dict = {
    "upload_guide": {
        "key": "upload_guide",
        "label": "上传说明文案",
        "description": (
            "显示在上传文件按钮上方的说明文本，支持 Markdown 格式。"
            "修改后刷新页面即可生效。"
        ),
        "dify_app": None,
        "dify_url": None,
        "editable": True,
        "current": DEFAULT_UPLOAD_GUIDE,
        "history": [],
    },
    "writer_requirements": {
        "key": "writer_requirements",
        "label": "分析师写报告要求",
        "description": (
            "附加在发送给 Analyst（调研分析-分析师）的 query 末尾的写报告要求。"
            "修改后下一次分析立即生效，无需重启服务。"
        ),
        "dify_app": None,
        "dify_url": None,
        "editable": True,
        "current": DEFAULT_WRITER_REQUIREMENTS,
        "history": [],
        "version": 10,  # 改了默认值就 +1：未被用户编辑过的会自动升级
    },
    "planner_extra": {
        "key": "planner_extra",
        "label": "Planner 分析指令",
        "description": (
            "附加在发送给 Planner（调研分析-规划器）的 query 末尾的补充指令。"
            "影响列分类、章节划分、交叉分析的规划方式。"
        ),
        "dify_app": None,
        "dify_url": None,
        "editable": True,
        "current": DEFAULT_PLANNER_EXTRA,
        "history": [],
        "version": 3,  # 改了默认值就 +1：未被用户编辑过的会自动升级
    },
    "column_detect_system": {
        "key": "column_detect_system",
        "label": "题型识别 System Prompt",
        "description": (
            "用于识别问卷列题型、翻译中文短名并归并多语言选项。"
            "修改后下一次题型识别立即生效，无需重启服务。"
        ),
        "dify_app": None,
        "dify_url": None,
        "editable": True,
        "current": DEFAULT_COLUMN_DETECT_SYSTEM_PROMPT,
        "history": [],
        "version": 1,
    },
    "survey_planner_system": {
        "key": "survey_planner_system",
        "label": "问卷方案规划 System Prompt",
        "description": (
            "用于生成和修订普通问卷分析方案，包含列结构、章节和交叉分析约束。"
            "修改后下一次方案规划立即生效，无需重启服务。"
        ),
        "dify_app": None,
        "dify_url": None,
        "editable": True,
        "current": DEFAULT_SURVEY_PLANNER_SYSTEM_PROMPT,
        "history": [],
        "version": 1,
    },
    "crosstab_planner_system": {
        "key": "crosstab_planner_system",
        "label": "跑数表章节规划 System Prompt",
        "description": (
            "用于生成和修订跑数表报告的主题化章节大纲。"
            "修改后下一次章节规划立即生效，无需重启服务。"
        ),
        "dify_app": None,
        "dify_url": None,
        "editable": True,
        "current": DEFAULT_CROSSTAB_PLANNER_SYSTEM_PROMPT,
        "history": [],
        "version": 1,
    },
    "theme_extract_system": {
        "key": "theme_extract_system",
        "label": "大样本主题提取 System Prompt",
        "description": (
            "用于从每批多语言开放题回答提取候选主题。"
            "修改后下一次大样本开放题分析立即生效，无需重启服务。"
        ),
        "dify_app": None,
        "dify_url": None,
        "editable": True,
        "current": DEFAULT_THEME_EXTRACT_SYSTEM_PROMPT,
        "history": [],
        "version": 1,
    },
    "theme_merge_system": {
        "key": "theme_merge_system",
        "label": "大样本主题合并 System Prompt",
        "description": (
            "用于跨批次合并候选主题，并生成连续主题 ID。"
            "修改后下一次大样本开放题分析立即生效，无需重启服务。"
        ),
        "dify_app": None,
        "dify_url": None,
        "editable": True,
        "current": DEFAULT_THEME_MERGE_SYSTEM_PROMPT,
        "history": [],
        "version": 1,
    },
    "response_classify_system": {
        "key": "response_classify_system",
        "label": "大样本回答分类 System Prompt",
        "description": (
            "用于把每条开放题回答归入最终主题并判断情感倾向。"
            "修改后下一次大样本开放题分析立即生效，无需重启服务。"
        ),
        "dify_app": None,
        "dify_url": None,
        "editable": True,
        "current": DEFAULT_RESPONSE_CLASSIFY_SYSTEM_PROMPT,
        "history": [],
        "version": 1,
    },
    "report_qa_system": {
        "key": "report_qa_system",
        "label": "报告追问 System Prompt",
        "description": (
            "用于普通问卷、大样本问卷和跑数表报告的统一追问。"
            "修改后下一次追问立即生效，无需重启服务。"
        ),
        "dify_app": None,
        "dify_url": None,
        "editable": True,
        "current": DEFAULT_REPORT_QA_SYSTEM_PROMPT,
        "history": [],
        "version": 1,
    },
}


def _load_prompts() -> dict:
    if not os.path.exists(PROMPTS_FILE):
        _save_prompts(DEFAULT_PROMPTS)
        return DEFAULT_PROMPTS
    with open(PROMPTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    dirty = False
    for k, v in DEFAULT_PROMPTS.items():
        if k not in data:
            data[k] = v
            dirty = True
            continue
        # 默认值升级：版本落后且用户从未编辑过（history 为空）→ 用新默认覆盖 current
        default_ver = v.get("version", 1)
        if data[k].get("version", 1) < default_ver:
            if not data[k].get("history"):
                data[k]["current"] = v["current"]
            data[k]["version"] = default_ver
            dirty = True
    if dirty:
        _save_prompts(data)
    return data


def _save_prompts(prompts: dict) -> None:
    with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
        json.dump(prompts, f, ensure_ascii=False, indent=2)


def _get_writer_requirements() -> str:
    return _load_prompts()["writer_requirements"]["current"]


def _get_planner_extra() -> str:
    return _load_prompts()["planner_extra"]["current"]


def _get_column_detect_system_prompt() -> str:
    return _load_prompts()["column_detect_system"]["current"]


def _get_survey_planner_system_prompt() -> str:
    return _load_prompts()["survey_planner_system"]["current"]


def _get_crosstab_planner_system_prompt() -> str:
    return _load_prompts()["crosstab_planner_system"]["current"]


def _get_theme_extract_system_prompt() -> str:
    return _load_prompts()["theme_extract_system"]["current"]


def _get_theme_merge_system_prompt() -> str:
    return _load_prompts()["theme_merge_system"]["current"]


def _get_response_classify_system_prompt() -> str:
    return _load_prompts()["response_classify_system"]["current"]


def _get_report_qa_system_prompt() -> str:
    return _load_prompts()["report_qa_system"]["current"]
