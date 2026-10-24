"""Transport and persisted shapes for administrator model settings."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints


class StrictShape(BaseModel):
    model_config = ConfigDict(extra="forbid")


ModelId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")]
Effort = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
TokenLimit = Annotated[StrictInt, Field(ge=1024, le=100_000_000)]


class ModelDefinition(StrictShape):
    id: ModelId
    name: str = Field(min_length=1, max_length=100)
    efforts: list[Effort] = Field(min_length=1, max_length=16)
    context_tokens: TokenLimit | None = None
    max_output_tokens: TokenLimit | None = None
    verified: StrictBool = False
    verification_note: str = Field(default="", max_length=1000)


class ModelCandidate(StrictShape):
    # Empty primary is retained for environments where a business is unconfigured.
    model: str = Field(max_length=100)
    reasoning: Effort = "default"
    max_tokens: TokenLimit


class TaskModelSettings(StrictShape):
    primary: ModelCandidate
    fallbacks: list[ModelCandidate] = Field(default_factory=list, max_length=16)
    timeout_seconds: Annotated[StrictInt, Field(ge=1, le=86400)] | None = None
    max_http_attempts: Annotated[StrictInt, Field(ge=1, le=100)] | None = None


class ModelConfiguration(StrictShape):
    models: list[ModelDefinition] = Field(max_length=200)
    tasks: dict[str, TaskModelSettings] = Field(max_length=100)


class CheckModelSettingsRequest(StrictShape):
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    configuration: ModelConfiguration
    restored_from: Annotated[StrictInt, Field(ge=1)] | None = None


class PublishModelSettingsRequest(CheckModelSettingsRequest):
    note: str = Field(default="", max_length=1000)


class RestoreModelSettingsRequest(StrictShape):
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class ModelSettingsVersion(StrictShape):
    version: Annotated[StrictInt, Field(ge=1)]
    configuration: ModelConfiguration
    created_at: str = Field(max_length=80)
    actor_name: str = Field(max_length=100)
    note: str = Field(max_length=1000)
    restored_from: Annotated[StrictInt, Field(ge=1)] | None = None


class ModelSettingsDocument(StrictShape):
    schema_version: Literal[1] = 1
    current: ModelSettingsVersion
    history: list[ModelSettingsVersion] = Field(max_length=1000)
