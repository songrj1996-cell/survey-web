"""Group task input contracts; domain decisions live in services."""
from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


class TaskRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="", max_length=200)
    payout_date: date | None = None
    base_reward: StrictInt | None = Field(default=None, ge=0, le=2147483647)
    excellent_reward: StrictInt | None = Field(default=None, ge=0, le=2147483647)
    requires_count: StrictBool = False
    min_count: StrictInt | None = Field(default=None, ge=1, le=2147483647)
    allow_repeat: StrictBool = False
    announcement: str = Field(default="", max_length=30000)
    ai_detect: StrictBool = True


class TaskCreate(BaseModel):
    rules: TaskRules = Field(default_factory=TaskRules)


class TaskRulesUpdate(BaseModel):
    expected_version: StrictInt = Field(ge=1)
    rules: TaskRules


class FeedbackMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id_col: StrictInt = Field(ge=0)
    open_text_cols: list[StrictInt] = Field(min_length=1)
    count_col: StrictInt | None = Field(default=None, ge=0)
    adv_col: StrictInt | None = Field(default=None, ge=0)


class MappingUpdate(BaseModel):
    expected_version: StrictInt = Field(ge=1)
    mapping: FeedbackMapping


class FeedbackCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: StrictInt = Field(ge=1)
    player_id: str | None = Field(default=None, min_length=1, max_length=200)
    count: StrictInt | None = Field(default=None, ge=0, le=2147483647)
    adv: str | None = Field(default=None, max_length=200)
    candidate_key: str | None = Field(default=None, max_length=128)
    note: str = Field(min_length=1, max_length=2000)


class DuplicateDecision(BaseModel):
    expected_version: StrictInt = Field(ge=1)
    group_id: str
    keep_response_id: str


class QualityOverride(BaseModel):
    expected_version: StrictInt = Field(ge=1)
    label: Literal["优秀反馈", "有效反馈", "无效反馈"] | None = None
    restore: StrictBool = False
    note: str = Field(min_length=1, max_length=2000)


class ConfirmAI(BaseModel):
    expected_version: StrictInt = Field(ge=1)
    response_ids: list[str]


class RunTask(BaseModel):
    expected_version: StrictInt = Field(ge=1)
    response_ids: list[str] | None = None


class VersionRequest(BaseModel):
    expected_version: StrictInt = Field(ge=1)


class PlayerADVDecision(BaseModel):
    expected_version: StrictInt = Field(ge=1)
    player_id: str = Field(min_length=1, max_length=200)
    adv: str = Field(max_length=200)
    note: str = Field(min_length=1, max_length=2000)


class AnnouncementRequest(BaseModel):
    announcement: str = Field(min_length=1, max_length=30000)
