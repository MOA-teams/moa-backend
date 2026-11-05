from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResponseSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0, strict=True)
    questions_enabled: bool | None = Field(default=None, strict=True)
    answers_enabled: bool | None = Field(default=None, strict=True)
    max_questions: int | None = Field(default=None, ge=1, le=3, strict=True)

    @model_validator(mode="after")
    def nonempty_changes(self):
        fields = self.model_fields_set - {"expected_revision"}
        if not fields or any(getattr(self, name) is None for name in fields):
            raise ValueError("Provide at least one non-null setting")
        return self


class ResponseSettingsView(BaseModel):
    tenant_id: str
    revision: int
    questions_enabled: bool
    answers_enabled: bool
    max_questions: int
    updated_at: datetime | None = None
    source: Literal["database", "environment", "default"]
