"""자동·수동 Issue 요청의 공통 저장. 외부 호출과 트랜잭션을 분리한다."""

import re
from datetime import timedelta

from sqlalchemy import select

from app.core.errors import APIError
from app.db.models import Inquiry, InquiryGroup, IssueJob, SurgeEpisode
from app.services.attachments import require_tenant
from app.services.groups import WINDOW, lock_groups, recent_members, utcnow
from app.services.inquiries import begin_idempotent_request
from app.services.group_github import issue_context


def safe_environment(members):
    # URL·고객 식별 정보가 환경 필드에 실려도 Issue로 유출되지 않게 알려진 형식만 사용한다.
    values = set()
    for member in members:
        os = member.env.get("os") or ""
        version = member.env.get("app_version") or ""
        if re.fullmatch(
            r"(?:iOS|Android|Windows|macOS|Linux|Chrome|Firefox|Safari|Edge|Opera|Samsung Internet)(?:[/ ][0-9.]{1,20})?",
            os,
        ):
            values.add("OS: " + os)
        if version == "web" or re.fullmatch(
            r"[0-9]{1,4}(?:\.[0-9]{1,4}){1,3}", version
        ):
            values.add("앱 버전: " + version)
    return sorted(values)


async def enqueue_issue(
    session, group, episode, *, automatic, now=None, include_suspected_code=True
):
    """기업 그룹 잠금 아래 호출. 본문은 최초 요청에서만 고정한다."""
    job = await session.scalar(
        select(IssueJob).where(IssueJob.episode_pk == episode.episode_pk)
    )
    if job:
        if not automatic:
            job.automatic = False
            if job.error_code == "EPISODE_ENDED" and job.phase == "done":
                job.phase, job.status, job.error_code = (
                    "prepare",
                    "대기",
                    "GITHUB_INTEGRATION_NOT_READY",
                )
                job.available_at = utcnow()
        return job
    now = now or utcnow()
    # 과거 구간 수동 요청도 당시 임계값 시점의 문의로 고정한다.
    anchor = episode.started_at if episode.ended_at else now
    members = await recent_members(session, group, anchor)
    marker = (
        f"<!-- moa-episode:{group.tenant_id}:{group.public_id}:{episode.public_id} -->"
    )
    symptom = (
        f"{group.normalized_error_code} 오류"
        if group.normalized_error_code
        else f"{group.category} 관련"
    )
    target, candidates, references = await issue_context(session, group)
    if not include_suspected_code:
        candidates, references = [], []
    investigation = "코드 후보: " + (
        ", ".join(f"{c['path']} ({c['commit_sha']})" for c in candidates)
        or "검증된 후보 없음"
    )
    investigation += "\n관련 커밋: " + (
        ", ".join(sorted({c["commit_sha"] for c in candidates})) or "없음"
    )
    body = "\n".join(
        [
            f"## 문의 요약\n{symptom} 문의 급증",
            f"그룹: {group.public_id}",
            f"문의 수: {len(members)}건",
            f"집계 구간: {(anchor-WINDOW).isoformat()} ~ {anchor.isoformat()}",
            "대표 문의: " + (members[0].public_id if members else "없음"),
            "환경: " + (", ".join(safe_environment(members)) or "확인 필요"),
            "문의 ID: " + ", ".join(m.public_id for m in members),
            investigation,
            "MOA가 자동 작성한 조사 요청입니다. 원인이 확정된 것이 아닙니다.",
            marker,
        ]
    )
    job = IssueJob(
        episode_pk=episode.episode_pk,
        automatic=automatic,
        payload={
            "title": f"[MOA] {symptom} 문의 {len(members)}건 급증 ({group.public_id})",
            "body": body,
            "labels": ["bug", "moa-auto"],
            "marker": marker,
            "include_suspected_code": include_suspected_code,
            "github_target": target,
            "github_sources": references,
        },
    )
    session.add(job)
    await session.flush()
    return job


async def request_issue(session, user, external_id, payload, key):
    tenant = require_tenant(user)
    try:
        await lock_groups(session, tenant)
        group = await session.scalar(
            select(InquiryGroup).where(
                InquiryGroup.public_id == external_id, InquiryGroup.tenant_id == tenant
            )
        )
        if group is None:
            raise APIError(404, "GROUP_NOT_FOUND", "그룹을 찾을 수 없습니다.")
        record = await begin_idempotent_request(
            session,
            user_id=user.internal_user_id,
            path=f"/admin/groups/{external_id}/github-issues",
            key=key,
            payload=payload,
        )
        if record.response is not None:
            result = record.response
        else:
            episode = await session.scalar(
                select(SurgeEpisode).where(
                    SurgeEpisode.group_id == group.group_id,
                    SurgeEpisode.public_id == payload.episode_id,
                )
            )
            if episode is None:
                raise APIError(
                    404, "EPISODE_NOT_FOUND", "그룹의 장애 구간을 찾을 수 없습니다."
                )
            job = await enqueue_issue(
                session,
                group,
                episode,
                automatic=False,
                include_suspected_code=payload.include_suspected_code,
            )
            result = {
                "group_id": group.public_id,
                "episode_id": episode.public_id,
                "issue_creation_status": job.status,
            }
            record.response = result
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise
