"""Selections are explicit; previews do not reserve or spend a source."""
from datetime import date
from pydantic import BaseModel, ConfigDict, Field, StrictInt


class OtherRewardCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payout_date: date
    source_id: str
    player_id: str = Field(min_length=1, max_length=200)
    amount: StrictInt = Field(gt=0, le=2147483647)
    reason: str = Field(min_length=1, max_length=2000)
    candidate_key: str = ""


class RewardSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dates: list[date] = Field(min_length=1, max_length=1000)
    task_versions: dict[str, StrictInt] = Field(default_factory=dict)
    other_reward_ids: list[str] = Field(default_factory=list)
    imported_session_ids: list[str] = Field(default_factory=list)


class BatchResolution(BaseModel):
    expected_revision: StrictInt = Field(ge=1)
    selections: dict[str, list[str]] = Field(default_factory=dict)
    identity_choices: dict[str, str] = Field(default_factory=dict)


class BatchGenerate(BaseModel):
    expected_revision: StrictInt = Field(ge=1)
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
