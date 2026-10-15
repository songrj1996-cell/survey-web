"""Feishu roster mappings; all columns are zero based, header rows one based."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


class FormulaReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["same_workbook", "synced_sheet"] = "same_workbook"
    target_spreadsheet: str = Field(default="", max_length=500)
    sheet_title: str = Field(default="", max_length=200)
    player_col: StrictInt = Field(default=2, ge=0, le=18277)
    ori_col: StrictInt = Field(default=4, ge=0, le=18277)
    status_col: StrictInt = Field(default=3, ge=0, le=18277)
    first_data_row: StrictInt = Field(default=2, ge=1)
    synchronization_note: str = Field(default="", max_length=2000)


class RosterSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    spreadsheet_url: str = Field(min_length=1, max_length=1000)
    sheet_id: str = Field(min_length=1, max_length=128)
    header_row: StrictInt = Field(default=1, ge=1)
    player_col: StrictInt = Field(ge=0, le=18277)
    ori_col: StrictInt = Field(ge=0, le=18277)
    status_col: StrictInt = Field(ge=0, le=18277)
    nickname_col: StrictInt | None = Field(default=None, ge=0, le=18277)
    expected_headers: dict[str, str] = Field(default_factory=dict)
    eligible_statuses: list[str] = Field(default_factory=lambda: ["群内人员"], min_length=1)
    ineligible_statuses: list[str] = Field(default_factory=lambda: ["已退群", "退群", "不在群内"])
    auth_mode: Literal["user", "application"] = "user"
    enabled: StrictBool = True
    formula: FormulaReference = Field(default_factory=FormulaReference)


class RosterSourceUpdate(RosterSourceInput):
    expected_revision: StrictInt = Field(ge=1)


class SourceInspect(BaseModel):
    spreadsheet_url: str = Field(min_length=1, max_length=1000)
    auth_mode: Literal["user", "application"] = "user"
    sheet_id: str = Field(default="", max_length=128)
    header_row: StrictInt = Field(default=1, ge=1)
