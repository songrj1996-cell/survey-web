"""Single menu/permission catalog shared with the browser through /api/feishu/me."""
from copy import deepcopy

FEATURE_TREE = [
    {"key": "research", "label": "调研分析", "children": [
        {"key": "survey", "label": "问卷分析"},
        {"key": "interview", "label": "访谈报告"},
        {"key": "comment", "label": "评论分析"},
    ]},
    {"key": "groups", "label": "群组管理", "children": [
        {"key": "annotate", "label": "质量标注"},
        {"key": "reward", "label": "奖励整理"},
        {"key": "roster", "label": "名单来源"},
    ]},
    {"key": "settings", "label": "设置", "children": [
        {"key": "prompts", "label": "提示词管理", "tab": "prompts"},
        {"key": "glossary", "label": "术语库", "tab": "glossary"},
        {"key": "model_settings", "label": "模型管理", "tab": "models", "admin_only": True},
        {"key": "platform", "label": "功能与平台设置", "tab": "system",
         "tabs": ["functions", "system"], "admin_tabs": ["system"]},
        {"key": "members", "label": "成员与权限", "tab": "perms", "admin_only": True},
        {"key": "audit", "label": "操作日志", "tab": "audit", "view_only": True},
    ]},
]
ALL_PERMS = [item["key"] for group in FEATURE_TREE for item in group["children"]]
LEGACY_PERMS = ["survey", "interview", "annotate", "comment", "reward"]


def feature_catalog() -> list[dict]:
    return deepcopy(FEATURE_TREE)


def permission_levels(user: dict) -> dict[str, str]:
    """Conservative compatibility: never turn a saved read restriction into use."""
    raw = user.get("perms", LEGACY_PERMS)
    if isinstance(raw, dict):
        levels = {
            key: ("read" if value in ("read", "readonly", "read_only") else "use")
            for key, value in raw.items()
            if key in ALL_PERMS and value in (True, "use", "edit", "read", "readonly", "read_only")
        }
    elif isinstance(raw, list):
        levels = {key: "use" for key in raw if isinstance(key, str) and key in ALL_PERMS}
    else:
        return {}
    saved = user.get("permission_levels")
    if isinstance(saved, dict):
        for key in levels:
            if saved.get(key) in ("read", "readonly", "read_only"):
                levels[key] = "read"
    if user.get("read_only") is True or user.get("readonly") is True:
        levels = {key: "read" for key in levels}
    if "audit" in levels:
        levels["audit"] = "read"
    # Ordinary whitelist entries can never create administrators.
    levels.pop("members", None)
    levels.pop("model_settings", None)
    return levels
