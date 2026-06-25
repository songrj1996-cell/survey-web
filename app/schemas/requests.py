"""所有 FastAPI 请求体模型(Pydantic)。

由 server.py / 各 router 共用。此处只定义数据结构,不含任何业务逻辑。
"""
from pydantic import BaseModel


# ── 问卷主流程 ──────────────────────────────────────────────
class ColumnConfirmRequest(BaseModel):
    columns: list[dict]


class PlanConfirmRequest(BaseModel):
    session_id: str
    user_text: str


class QARequest(BaseModel):
    session_id: str
    question: str


class HistoryQARequest(BaseModel):
    history_id: str
    question: str


# ── 管理后台 ────────────────────────────────────────────────
class AdminUserRequest(BaseModel):
    email: str
    perms: list[str] = ["survey", "annotate"]
    enabled: bool = True


class AdminUserPatch(BaseModel):
    perms: list[str] | None = None
    enabled: bool | None = None


# ── 提示词 / UI 文案 / 系统设置 ─────────────────────────────
class PromptUpdateRequest(BaseModel):
    content: str
    note: str = ""


class UiTextUpdateRequest(BaseModel):
    content: str


class AppSettingsPatch(BaseModel):
    comment_duplicate_reminder_enabled: bool | None = None


# ── 历史记录 ────────────────────────────────────────────────
class HistoryTitleUpdateRequest(BaseModel):
    title: str


class HistoryTitleUpdateByIdRequest(BaseModel):
    id: str
    title: str


# ── 数据标注 ────────────────────────────────────────────────
class AnnotateConfirmRequest(BaseModel):
    id_col: int
    open_text_cols: list[int]
    tasks: dict          # {ai_detect: bool, quality: bool}
    background: str = ""  # 可选调研背景，用于 AI 检测


class AnnotateConfirmAIRequest(BaseModel):
    confirmed_ai_ids: list[str]  # 用户确认为 AI 作答的 player ID 列表
