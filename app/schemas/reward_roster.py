"""奖励整理请求结构。"""

from pydantic import BaseModel, Field


class RewardSheetMapping(BaseModel):
    date_col: int = Field(ge=0)
    task_col: int = Field(ge=0)
    player_id_col: int = Field(ge=0)
    account_id_col: int = Field(ge=0)
    status_col: int | None = Field(default=None, ge=0)
    normal_reward_col: int = Field(ge=0)
    excellent_reward_col: int | None = Field(default=None, ge=0)
    other_reward_col: int | None = Field(default=None, ge=0)


class RewardAnalyzeRequest(BaseModel):
    selected_sheets: list[str] = Field(min_length=1, max_length=64)
    reward_dates: list[str] = Field(min_length=1, max_length=1000)
    mappings: dict[str, RewardSheetMapping]


class RewardConflictResolution(BaseModel):
    conflict_id: str = Field(min_length=1, max_length=128)
    selected_row_ids: list[str] = Field(default_factory=list, max_length=1000)


class RewardResolveRequest(BaseModel):
    resolutions: list[RewardConflictResolution] = Field(default_factory=list, max_length=1000)
