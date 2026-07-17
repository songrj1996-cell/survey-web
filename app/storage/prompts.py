"""storage/prompts:提示词(prompts.json)的读写与默认值。

默认提示词文案(DEFAULT_UPLOAD_GUIDE 等)来自 core/config;此处负责持久化与默认升级。
"""
import json
import os

from app.core.config import (
    DEFAULT_PLANNER_EXTRA,
    DEFAULT_UPLOAD_GUIDE,
    DEFAULT_WRITER_REQUIREMENTS,
    DIFY_CONSOLE_URL,
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
    "dify_planner_system": {
        "key": "dify_planner_system",
        "label": "规划器 System Prompt（Dify 管理）",
        "description": (
            "配置在 Dify「调研分析-规划器」应用中的 System Prompt。"
            "需在 Dify 后台「编排 → 提示词」中修改，此处仅供参考。"
        ),
        "dify_app": "调研分析-规划器",
        "dify_url": DIFY_CONSOLE_URL,
        "editable": False,
        "current": "（请前往 Dify 后台查看：调研分析-规划器 → 编排 → 提示词）",
        "history": [],
    },
    "dify_analyst_system": {
        "key": "dify_analyst_system",
        "label": "分析师 System Prompt（Dify 管理）",
        "description": (
            "配置在 Dify「调研分析-分析师」应用中的 System Prompt。"
            "需在 Dify 后台「编排 → 提示词」中修改，此处仅供参考。"
        ),
        "dify_app": "调研分析-分析师",
        "dify_url": DIFY_CONSOLE_URL,
        "editable": False,
        "current": "（请前往 Dify 后台查看：调研分析-分析师 → 编排 → 提示词）",
        "history": [],
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
