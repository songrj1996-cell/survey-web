"""固定页面说明目录。只返回代码副本，不读取、迁移或写入历史 JSON。"""
from copy import deepcopy

from app.core.config import DEFAULT_UPLOAD_GUIDE


DEFAULT_UI_TEXTS: dict = {
    "upload_guide": {
        "key": "upload_guide",
        "label": "定性问卷分析·上传说明",
        "current": DEFAULT_UPLOAD_GUIDE,
    },
    "panel_col_desc": {
        "key": "panel_col_desc",
        "label": "问卷分析·数据确认说明",
        "current": "AI 已识别每道题的题型与中文题名，请逐一核对并修正。题型直接影响后续统计口径。",
    },
    "panel_plan_desc": {
        "key": "panel_plan_desc",
        "label": "问卷分析·方案确认说明",
        "current": "AI 已规划以下分析方案，请确认或提出修改意见",
    },
    "panel_report_desc": {
        "key": "panel_report_desc",
        "label": "问卷分析·报告生成说明",
        "current": "AI 正在基于确定性统计结果与开放题反馈逐章撰写报告，章节完成并校验后将自动展示。",
    },
    "panel_done_desc": {
        "key": "panel_done_desc",
        "label": "问卷分析·报告完成说明",
        "current": "报告已生成完毕，可下载或继续追问",
    },
    "qa_hint": {
        "key": "qa_hint",
        "label": "问卷分析·报告追问提示",
        "current": "对报告有疑问？直接提问，AI 会回到原始数据找答案",
    },
    "ann_panel_upload_desc": {
        "key": "ann_panel_upload_desc",
        "label": "数据标注·上传说明",
        "current": "上传问卷原始数据，支持 CSV / Excel（最大 50MB）",
    },
    "ann_panel_col_desc": {
        "key": "ann_panel_col_desc",
        "label": "数据标注·列确认说明",
        "current": "AI 已自动检测 ID 列和主观题列，请核对。主观题列将用于 AI 识别和质量打标。",
    },
    "ann_panel_run_desc": {
        "key": "ann_panel_run_desc",
        "label": "数据标注·识别中说明",
        "current": "正在分批分析受访者回答，请耐心等待",
    },
    "ann_panel_quality_desc": {
        "key": "ann_panel_quality_desc",
        "label": "数据标注·打标中说明",
        "current": "正在结合题意标注逐题质量，并根据每位玩家的完整作答独立判断整体质量，请耐心等待",
    },
    "ann_panel_done_desc": {
        "key": "ann_panel_done_desc",
        "label": "数据标注·完成说明",
        "current": "查看逐题标签、整体判断及回答原文；新版整体质量独立判断，人工修改逐题标签后保留原整体判断",
    },
}


def _load_ui_texts() -> dict:
    """保留旧只读调用契约；返回独立副本，调用方不能改变代码默认值。"""
    return deepcopy(DEFAULT_UI_TEXTS)
