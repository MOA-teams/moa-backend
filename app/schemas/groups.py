from datetime import datetime
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

GroupID = Annotated[str, Field(pattern=r"^G-[0-9]{3}$")]
AlertID = Annotated[str, Field(pattern=r"^ALERT-[0-9]{6}$")]
IssueStatus = Literal["대기", "처리 중", "완료", "실패", "확인 필요"]


class GroupView(BaseModel):
    group_id: GroupID
    summary: str
    count: int
    window_minutes: Literal[5] = 5
    surging: bool
    episode_id: str | None
    episode_ended_at: datetime | None
    issue_creation_status: IssueStatus | None
    issue_error_code: str | None
    issue_url: str | None


class GroupPage(BaseModel):
    items: list[GroupView]
    next_cursor: str | None
    has_more: bool


class GroupDetail(GroupView):
    inquiry_ids: list[str]
    normalized_error_codes: list[str]
    suspected_code: list[dict] = Field(default_factory=list, description="8단계에서 검증된 조사 후보만 제공. 현재 미연결로 빈 배열")
    next_cursor: str | None
    has_more: bool


class AlertView(BaseModel):
    alert_id: AlertID
    type: Literal["검증 확인 필요", "분석 실패", "문의 급증", "GitHub 이슈 생성 실패"]
    inquiry_id: str | None
    group_id: GroupID | None
    run_id: str | None
    message: str
    error_code: str | None
    read_at: datetime | None
    created_at: datetime


class AlertPage(BaseModel):
    items: list[AlertView]
    next_cursor: str | None
    has_more: bool


class AlertReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    read: bool = Field(strict=True)

    @field_validator("read")
    @classmethod
    def only_read(cls, value):
        if not value:
            raise ValueError("읽음 처리는 true만 허용합니다.")
        return value


class AlertReadResponse(BaseModel):
    alert_id: AlertID
    read_at: datetime


class IssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    episode_id: str = Field(pattern=r"^episode_[a-f0-9]{32}$")
    include_suspected_code: bool = Field(default=True, strict=True)


class IssueAccepted(BaseModel):
    group_id: GroupID
    episode_id: str
    issue_creation_status: IssueStatus
