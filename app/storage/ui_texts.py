"""storage/ui_texts:可配置页面文案的读取、迁移与原子写入。"""
from copy import deepcopy
import json
import os
import tempfile
import threading

from app.core.config import DEFAULT_UPLOAD_GUIDE, PROMPTS_FILE, UI_TEXTS_FILE

_UI_TEXTS_LOCK = threading.RLock()

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
        "current": "正在分批标注每道主观题的回答质量，请耐心等待",
    },
    "ann_panel_done_desc": {
        "key": "ann_panel_done_desc",
        "label": "数据标注·完成说明",
        "current": "所有标注任务已完成，可下载 Excel 文件",
    },
}


def _legacy_upload_guide() -> str | None:
    """仅迁移有编辑历史的旧上传说明；旧默认值升级为当前默认。"""
    try:
        with open(PROMPTS_FILE, "r", encoding="utf-8") as prompt_file:
            prompts = json.load(prompt_file)
        entry = prompts.get("upload_guide", {})
        if not entry.get("history"):
            return None
        content = entry.get("current")
        return content if isinstance(content, str) else None
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
        return None


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


def _new_defaults_with_legacy_guide() -> dict:
    data = deepcopy(DEFAULT_UI_TEXTS)
    legacy = _legacy_upload_guide()
    if legacy is not None:
        data["upload_guide"]["current"] = legacy
    return data


def _load_ui_texts() -> dict:
    with _UI_TEXTS_LOCK:
        if not os.path.exists(UI_TEXTS_FILE):
            data = _new_defaults_with_legacy_guide()
            _atomic_write_json(UI_TEXTS_FILE, data)
            return data
        with open(UI_TEXTS_FILE, "r", encoding="utf-8") as ui_file:
            data = json.load(ui_file)
        if not isinstance(data, dict):
            raise ValueError("页面文案存储格式无效")

        dirty = False
        for key, default in DEFAULT_UI_TEXTS.items():
            entry = data.get(key)
            if entry is None:
                entry = deepcopy(default)
                if key == "upload_guide":
                    legacy = _legacy_upload_guide()
                    if legacy is not None:
                        entry["current"] = legacy
                data[key] = entry
                dirty = True
                continue
            if not isinstance(entry, dict):
                raise ValueError("页面文案条目格式无效")
            for field in ("key", "label"):
                if entry.get(field) != default[field]:
                    entry[field] = default[field]
                    dirty = True
            if "current" not in entry:
                entry["current"] = default["current"]
                dirty = True
        if dirty:
            _atomic_write_json(UI_TEXTS_FILE, data)
        return data


def _save_ui_texts(texts: dict) -> None:
    with _UI_TEXTS_LOCK:
        _atomic_write_json(UI_TEXTS_FILE, texts)
