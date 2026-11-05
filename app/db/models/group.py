"""그룹 확정·장애 구간·알림과 외부 전달 의도를 보존한다."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import public_id_default
from app.db.base import BaseEntity


class InquiryGroup(BaseEntity):
    __tablename__ = "inquiry_groups"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "normalized_error_code", name="uq_group_tenant_code"
        ),
    )
    group_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(16), unique=True, default=public_id_default("group")
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT")
    )
    normalized_error_code: Mapped[str | None] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(30))
    summary: Mapped[str] = mapped_column(String(200))


class GroupMembershipHistory(BaseEntity):
    __tablename__ = "group_membership_history"
    history_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    inquiry_id: Mapped[int] = mapped_column(
        ForeignKey("inquiries.inquiry_id", ondelete="CASCADE")
    )
    previous_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("inquiry_groups.group_id")
    )
    group_id: Mapped[int | None] = mapped_column(ForeignKey("inquiry_groups.group_id"))
    reason: Mapped[str] = mapped_column(String(100))
    run_id: Mapped[str | None] = mapped_column(String(100))


class SurgeEpisode(BaseEntity):
    __tablename__ = "surge_episodes"
    __table_args__ = (
        Index(
            "uq_active_group_episode",
            "group_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
    )
    episode_pk: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(50), unique=True, default=lambda: "episode_" + uuid4().hex
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey("inquiry_groups.group_id", ondelete="RESTRICT")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Alert(BaseEntity):
    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cause", name="uq_alert_tenant_cause"),
        Index("ix_alert_tenant_id", "tenant_id", "alert_id"),
    )
    alert_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, default=public_id_default("alert")
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT")
    )
    cause: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(40))
    inquiry_id: Mapped[str | None] = mapped_column(String(32))
    group_id: Mapped[str | None] = mapped_column(String(16))
    run_id: Mapped[str | None] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(String(200))
    error_code: Mapped[str | None] = mapped_column(String(100))


class AlertRead(BaseEntity):
    __tablename__ = "alert_reads"
    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.alert_id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True
    )
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IssueJob(BaseEntity):
    __tablename__ = "issue_jobs"
    job_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    episode_pk: Mapped[int] = mapped_column(
        ForeignKey("surge_episodes.episode_pk", ondelete="RESTRICT"), unique=True
    )
    automatic: Mapped[bool] = mapped_column(Boolean)
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="대기")
    phase: Mapped[str] = mapped_column(String(20), default="prepare")
    error_code: Mapped[str | None] = mapped_column(
        String(100), default="GITHUB_INTEGRATION_NOT_READY"
    )
    issue_url: Mapped[str | None] = mapped_column(Text)
    issue_metadata: Mapped[dict | None] = mapped_column(JSONB)
    readiness_reason: Mapped[str | None] = mapped_column(String(100))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(32))


class InquirySummaryJob(BaseEntity):
    __tablename__ = "inquiry_summary_jobs"
    __table_args__ = (
        UniqueConstraint("inquiry_id", "source_revision", name="uq_summary_revision"),
    )
    job_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    inquiry_id: Mapped[int] = mapped_column(
        ForeignKey("inquiries.inquiry_id", ondelete="CASCADE")
    )
    source_revision: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSONB)
    # I04 의도도 고정한다. 과거 버전이 검색에 남지 않도록 새 등록보다 먼저 처리한다.
    barriers: Mapped[list] = mapped_column(JSONB, default=list)
    phase: Mapped[str] = mapped_column(String(20), default="barrier")
    error_code: Mapped[str | None] = mapped_column(String(100))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# 조회·작업 확보 인덱스는 마이그레이션과 같은 이름을 사용한다.
Index("ix_inquirygroup_tenant_id", InquiryGroup.tenant_id)
Index("ix_groupmembershiphistory_inquiry_id", GroupMembershipHistory.inquiry_id)
Index("ix_surgeepisode_group_id", SurgeEpisode.group_id)
Index("ix_issuejob_available_at", IssueJob.available_at)
Index("ix_inquirysummaryjob_inquiry_id", InquirySummaryJob.inquiry_id)
Index("ix_inquirysummaryjob_available_at", InquirySummaryJob.available_at)
