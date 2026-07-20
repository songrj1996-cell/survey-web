"""storage/ui_texts:前端可配置文案(ui_texts.json)读写 + 默认值。"""
import json
import os

from app.core.config import UI_TEXTS_FILE

DEFAULT_UI_TEXTS: dict = {
    "panel_col_desc": {
        "key": "panel_col_desc",
        "label": "数据确认说明",
        "current": "AI 已识别每道题的题型与中文题名，请逐一核对并修正。题型直接影响后续统计口径。",
    },
    "panel_plan_desc": {
        "key": "panel_plan_desc",
        "label": "分析方案说明",
        "current": "AI 已规划以下分析方案，请确认或提出修改意见",
    },
    "panel_report_desc": {
        "key": "panel_report_desc",
        "label": "生成报告说明",
        "current": "AI 正在基于确定性统计结果与开放题反馈逐章撰写报告，章节完成并校验后将自动展示。",
    },
    "panel_done_desc": {
        "key": "panel_done_desc",
        "label": "报告完成说明",
        "current": "报告已生成完毕，可下载或继续追问",
    },
    "qa_hint": {
        "key": "qa_hint",
        "label": "追问提示文字",
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
        "current": "正在分批标注每道主观题的回答质量，请耐心等待",
    },
    "ann_panel_done_desc": {
        "key": "ann_panel_done_desc",
        "label": "数据标注·完成说明",
        "current": "所有标注任务已完成，可下载 Excel 文件",
    },
}


def _load_ui_texts() -> dict:
    if not os.path.exists(UI_TEXTS_FILE):
        _save_ui_texts(DEFAULT_UI_TEXTS)
        return DEFAULT_UI_TEXTS
    with open(UI_TEXTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    dirty = False
    for k, v in DEFAULT_UI_TEXTS.items():
        if k not in data:
            data[k] = v
            dirty = True
    if dirty:
        _save_ui_texts(data)
    return data


def _save_ui_texts(texts: dict) -> None:
    with open(UI_TEXTS_FILE, "w", encoding="utf-8") as f:
        json.dump(texts, f, ensure_ascii=False, indent=2)
