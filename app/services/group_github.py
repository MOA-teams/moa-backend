"""8단계의 검증된 연결·검색 원본을 9단계 Issue 전송 경계에 연결한다.

DB에는 저장소·정책·소스 식별자만 고정한다. 설치 토큰은 요청 시 발급하고 저장하지 않는다.
그룹 잠금 안에서 기업/연동 행을 추가로 잠그지 않아 기존 지식 잠금 순서를 보존한다.
"""

import hashlib
import json
import re

from sqlalchemy import select
from app.core.github import GitHubError, get_github_client
from app.core.group_issues import HttpIssueGateway, IssueError, VerifiedIssueTarget
from app.db.models import (
    AnalysisRequest,
    AnalysisRun,
    GitHubIntegration,
    GitHubSource,
    Inquiry,
)
from app.services.github_security import exclusion, sanitize


def enabled(row):
    return bool(
        row
        and row.enabled
        and row.security_level in (1, 2)
        and row.installation_id
        and row.repository_id
        and row.repository
    )


def policy_hash(row):
    policy = {
        key: getattr(row, key)
        for key in (
            "security_level",
            "include_paths",
            "exclude_patterns",
            "watch_branch",
        )
    }
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def binding(row):
    return {
        "integration_id": row.integration_id,
        "installation_id": row.installation_id,
        "repository_id": row.repository_id,
        "repository": row.repository,
        "policy_hash": policy_hash(row),
    }


async def integration_for(session, tenant):
    return await session.scalar(
        select(GitHubIntegration).where(GitHubIntegration.tenant_id == tenant)
    )


def allowed_source(source, integration):
    payload = source.payload
    metadata = payload.get("metadata", {})
    path = source.path
    return (
        source.phase == "ready"
        and source.searchable
        and re.fullmatch(r"[A-Za-z0-9_./-]+", path)
        and not exclusion(
            path, integration.include_paths, integration.exclude_patterns + ["labels/"]
        )
        and not sanitize(path, path)[1]
        and payload.get("tenant_id") == integration.tenant_id
        and payload.get("status") == "유효"
        and not payload.get("review_required")
        and payload.get("source_type") in ("코드", "커밋")
        and metadata.get("repository") == integration.repository
        and metadata.get("security_level") == integration.security_level
        and metadata.get("path") == path
        and metadata.get("commit_sha") == source.version
        and re.fullmatch(r"[a-fA-F0-9]{40}", source.version)
    )


async def issue_context(session, group):
    integration = await integration_for(session, group.tenant_id)
    if not enabled(integration) or integration.status != "연결 완료":
        return None, [], []
    sources = list(
        (
            await session.scalars(
                select(GitHubSource)
                .where(
                    GitHubSource.integration_id == integration.integration_id,
                    GitHubSource.phase == "ready",
                    GitHubSource.searchable.is_(True),
                )
                .order_by(GitHubSource.source_pk)
            )
        ).all()
    )
    by_reference = {
        (s.path, s.version): s for s in sources if allowed_source(s, integration)
    }
    runs = list(
        (
            await session.scalars(
                select(AnalysisRun)
                .join(AnalysisRequest, AnalysisRequest.run_id == AnalysisRun.run_id)
                .join(
                    Inquiry,
                    Inquiry.latest_analysis_request_id == AnalysisRequest.request_id,
                )
                .where(
                    Inquiry.tenant_id == group.tenant_id,
                    Inquiry.group_pk == group.group_id,
                    AnalysisRequest.inquiry_revision == Inquiry.revision,
                    AnalysisRun.superseded.is_(False),
                    AnalysisRun.result.is_not(None),
                )
            )
        ).all()
    )
    selected = {}
    for run in runs:
        if not run.response or run.response.get("apply_status") != "applied":
            continue
        draft = (run.result or {}).get("draft") or {}
        for candidate in (draft.get("evidence") or {}).get("code", []):
            source = by_reference.get(
                (candidate.get("path"), candidate.get("commit_sha"))
            )
            if source:
                selected[source.source_pk] = source
    sources = sorted(selected.values(), key=lambda s: (s.path, s.version))[:10]
    candidates = [
        {
            "path": s.path,
            "commit_sha": s.version,
            "reason": "현재 GitHub 수집 자료와 일치하는 조사 후보",
        }
        for s in sources
    ]
    refs = [
        {
            "source_pk": s.source_pk,
            "version": s.version,
            "source_revision": s.source_revision,
        }
        for s in sources
    ]
    return binding(integration), candidates, refs


class IntegrationIssueGateway(HttpIssueGateway):
    def __init__(self, factory, github=None, *, transport=None):
        super().__init__({}, transport=transport)
        self.factory = factory
        self.github = github or get_github_client()
        self.bindings = {}
        self.references = {}

    async def ready(self, tenant):
        # 설정 또는 연결 자체가 아직 없으면 durable 작업을 대기로 유지한다.
        try:
            self.github.require_config()
        except GitHubError as error:
            if error.code == "GITHUB_NOT_CONFIGURED":
                return False
            raise
        async with self.factory() as session:
            row = await integration_for(session, tenant)
            return enabled(row) and row.status == "연결 완료"

    async def prepare(self, tenant, payload):
        async with self.factory() as session:
            row = await integration_for(session, tenant)
            if not enabled(row):
                raise IssueError("GITHUB_INTEGRATION_DISABLED")
            target = payload.get("github_target") or binding(row)
            self.bindings[tenant] = target
            self.references[tenant] = payload.get("github_sources", [])
            # 이전 버전의 미전송 작업은 최초 처리 시에만 대상을 고정한다.
            return {**payload, "github_target": target}

    async def _validate_target(self, tenant, *, creating):
        target = self.bindings[tenant]
        async with self.factory() as session:
            row = await integration_for(session, tenant)
            if not enabled(row):
                raise IssueError("GITHUB_INTEGRATION_DISABLED")
            identity = (
                "integration_id",
                "installation_id",
                "repository_id",
                "repository",
            )
            if any(getattr(row, key) != target[key] for key in identity):
                raise IssueError("GITHUB_TARGET_CHANGED")
            if creating:
                if (
                    row.status != "연결 완료"
                    or policy_hash(row) != target["policy_hash"]
                ):
                    raise IssueError("GITHUB_POLICY_CHANGED")
                for ref in self.references[tenant]:
                    source = await session.get(GitHubSource, ref["source_pk"])
                    if (
                        source is None
                        or source.integration_id != row.integration_id
                        or not allowed_source(source, row)
                        or source.version != ref["version"]
                        or source.source_revision != ref["source_revision"]
                    ):
                        raise IssueError("GITHUB_EVIDENCE_CHANGED")
        return target

    async def _request(self, tenant, method, *, payload=None, page=None):
        target = await self._validate_target(tenant, creating=method == "POST")
        if tenant not in self.targets:
            try:
                token = await self.github.installation_token(
                    target["installation_id"],
                    target["repository_id"],
                    issues_write=True,
                )
                # 이름이 재사용·변경된 저장소로 전송하지 않도록 실제 숫자 ID도 대조한다.
                repo = await self.github.request(
                    "GET", f"/repos/{target['repository']}", token
                )
                if (
                    not isinstance(repo, dict)
                    or repo.get("id") != target["repository_id"]
                    or repo.get("full_name") != target["repository"]
                    or repo.get("has_issues") is not True
                    or repo.get("archived")
                ):
                    raise IssueError("GITHUB_REPOSITORY_MISMATCH")
            except GitHubError as error:
                raise IssueError(
                    error.code, retryable=error.retryable, retry_after=error.retry_after
                ) from None
            self.targets[tenant] = VerifiedIssueTarget(target["repository"], token)
        # 토큰 발급/저장소 확인 중 접근이 해제된 경우도 POST 직전에 차단한다.
        await self._validate_target(tenant, creating=method == "POST")
        return await super()._request(tenant, method, payload=payload, page=page)
