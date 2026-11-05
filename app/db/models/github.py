"""GitHub 권한 확인, 수집 원본 및 재시작 가능한 동기화 이력."""

from datetime import datetime
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import BaseEntity


class GitHubAuthorization(BaseEntity):
    __tablename__ = "github_authorizations"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    verifier: Mapped[str | None] = mapped_column(String(128))
    phase: Mapped[str] = mapped_column(String(20), nullable=False)
    repositories: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class GitHubIntegration(BaseEntity):
    __tablename__ = "github_integrations"
    integration_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    repository_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    repository: Mapped[str | None] = mapped_column(String(200))
    watch_branch: Mapped[str] = mapped_column(
        String(200), nullable=False, default="main"
    )
    security_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    include_paths: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    exclude_patterns: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    document_bindings: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    pending_bindings: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="연동 안 함"
    )
    last_synced_sha: Mapped[str | None] = mapped_column(String(40))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    report: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class GitHubEvent(BaseEntity):
    __tablename__ = "github_events"
    event_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    delivery_id: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    integration_id: Mapped[str | None] = mapped_column(
        ForeignKey("github_integrations.integration_id", ondelete="RESTRICT")
    )
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)


class GitHubJob(BaseEntity):
    __tablename__ = "github_jobs"
    __table_args__ = (
        UniqueConstraint(
            "integration_id", "generation", name="uq_github_job_generation"
        ),
        Index("ix_github_jobs_due", "phase", "available_at"),
    )
    job_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    integration_id: Mapped[str] = mapped_column(
        ForeignKey("github_integrations.integration_id", ondelete="CASCADE"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(20), nullable=False, default="discover")
    target_sha: Mapped[str | None] = mapped_column(String(40))
    manifest: Mapped[list | None] = mapped_column(JSONB)
    report: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(100))


class GitHubSource(BaseEntity):
    __tablename__ = "github_sources"
    __table_args__ = (
        UniqueConstraint(
            "integration_id", "source_id", "version", name="uq_github_source_version"
        ),
    )
    source_pk: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    integration_id: Mapped[str] = mapped_column(
        ForeignKey("github_integrations.integration_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    blob_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    phase: Mapped[str] = mapped_column(String(20), nullable=False, default="submit")
    submission_possible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    searchable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    barrier: Mapped[dict | None] = mapped_column(JSONB)
    poll_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
