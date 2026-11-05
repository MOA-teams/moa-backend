"""Issue 생성 의도를 먼저 commit하고 결과가 불명확한 POST는 재전송하지 않는다."""

import asyncio
import logging
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import or_, select

from app.core.group_issues import IssueError, get_issue_gateway
from app.db.models import InquiryGroup, IssueJob, SurgeEpisode
from app.services.alerts import notify
from app.services.github_security import sanitize
from app.services.groups import (
    THRESHOLD,
    effective_category,
    evaluate,
    lock_groups,
    recent_members,
    utcnow,
)

logger = logging.getLogger(__name__)


async def claim(factory):
    async with factory() as session:
        job = await session.scalar(
            select(IssueJob)
            .where(
                or_(IssueJob.phase != "done", (IssueJob.status == "완료") & IssueJob.issue_metadata.is_(None)),
                IssueJob.available_at <= utcnow(),
                or_(IssueJob.lease_until.is_(None), IssueJob.lease_until < utcnow()),
            )
            .order_by(IssueJob.available_at, IssueJob.job_id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        job.lease_token, job.lease_until = uuid4().hex, utcnow() + timedelta(seconds=60)
        await session.commit()
        return job.job_id, job.lease_token


async def locked(session, job_id, token):
    row = (
        await session.execute(
            select(InquiryGroup, SurgeEpisode)
            .join(SurgeEpisode, SurgeEpisode.group_id == InquiryGroup.group_id)
            .join(IssueJob, IssueJob.episode_pk == SurgeEpisode.episode_pk)
            .where(IssueJob.job_id == job_id)
        )
    ).first()
    if row is None:
        return None, None, None
    group, episode = row
    await lock_groups(session, group.tenant_id)
    await session.refresh(episode)
    job = await session.get(
        IssueJob, job_id, with_for_update=True, populate_existing=True
    )
    return (
        (group, episode, job)
        if job.lease_token == token and (job.phase != "done" or (job.status == "완료" and job.issue_metadata is None))
        else (group, episode, None)
    )


def release(job, delay=0):
    job.lease_token = job.lease_until = None
    job.available_at = utcnow() + timedelta(seconds=delay)


async def failed(session, group, job, code, *, uncertain=False):
    logger.warning("issue_state_changed job_id=%s code=%s", job.job_id, code)
    job.status, job.error_code, job.phase = (
        "확인 필요" if uncertain else "실패",
        code,
        "done",
    )
    release(job)
    await notify(
        session,
        tenant=group.tenant_id,
        cause=f"issue:{job.job_id}",
        kind="GitHub 이슈 생성 실패",
        group_id=group.public_id,
        code=code,
    )


async def process(factory, job_id, token, gateway):
    async with factory() as session:
        group, episode, job = await locked(session, job_id, token)
        if job is None:
            return
        tenant, payload, phase = group.tenant_id, job.payload, job.phase
        await session.commit()
    ready = await gateway.ready(tenant)
    async with factory() as session:
        group, episode, job = await locked(session, job_id, token)
        if job is None:
            return
        if not ready:
            reason = getattr(gateway, "readiness_reason", None) or "GITHUB_INTEGRATION_NOT_READY"
            if job.readiness_reason != reason:
                logger.warning("issue_readiness_changed job_id=%s code=%s", job_id, reason)
            job.readiness_reason = reason
            if phase != "done":
                job.status, job.error_code = "대기", "GITHUB_INTEGRATION_NOT_READY"
            release(job, 30)
            await session.commit()
            return
        if phase == "prepare" and job.automatic:
            await evaluate(session, group)
            members = await recent_members(session, group, utcnow())
            if (
                episode.ended_at
                or sum(effective_category(m) == "기능 오류" for m in members)
                < THRESHOLD
            ):
                # 임계값이 일시 하락한 활성 구간은 종료 전까지 다시 확인한다.
                if episode.ended_at:
                    job.phase, job.status, job.error_code = (
                        "done",
                        "실패",
                        "EPISODE_ENDED",
                    )
                    release(job)
                    logger.info("issue_state_changed job_id=%s code=EPISODE_ENDED", job_id)
                else:
                    job.status, job.error_code = "대기", "SURGE_POLICY_NOT_MET"
                    release(job, 5)
                await session.commit()
                return
        job.readiness_reason = None
        if phase != "done":
            job.status, job.error_code = "처리 중", None
            job.attempts += 1
        await session.commit()
    try:
        prepared = await gateway.prepare(tenant, payload)
        async with factory() as session:
            _, _, job = await locked(session, job_id, token)
            if job is None:
                return
            if (
                job.phase == "reconcile"
                and payload.get("github_target") is None
                and prepared.get("github_target")
            ):
                raise IssueError("GITHUB_TARGET_UNKNOWN")
            job.payload = prepared
            payload = prepared
            await session.commit()
        url = await gateway.find(tenant, payload["marker"])
        if phase == "done":
            async with factory() as session:
                _, _, job = await locked(session, job_id, token)
                if job:
                    if url and url == job.issue_url:
                        job.issue_metadata = metadata(gateway, payload)
                    else:
                        job.readiness_reason = "GITHUB_METADATA_UNAVAILABLE"
                    release(job, 300)
                    await session.commit()
            return
        if url is None and phase == "reconcile":
            async with factory() as session:
                group, _, job = await locked(session, job_id, token)
                if job:
                    await failed(
                        session, group, job, "GITHUB_RESULT_UNKNOWN", uncertain=True
                    )
                    await session.commit()
            return
        if url is None:
            async with factory() as session:
                group, episode, job = await locked(session, job_id, token)
                if job is None:
                    return
                if job.automatic:
                    await evaluate(session, group)
                    members = await recent_members(session, group, utcnow())
                    if (
                        episode.ended_at
                        or sum(effective_category(m) == "기능 오류" for m in members)
                        < THRESHOLD
                    ):
                        job.status, job.error_code = "대기", "SURGE_POLICY_NOT_MET"
                        release(job, 5)
                        await session.commit()
                        return
                # 전송 의도 상태를 커밋한 뒤에만 POST. crash/lease 만료 후 다른 워커는 조회만 한다.
                job.payload = {**job.payload, "creation_mode": "자동" if job.automatic else "수동"}
                payload = job.payload
                job.phase = "reconcile"
                await session.commit()
            url = await gateway.create(tenant, payload)
        async with factory() as session:
            _, _, job = await locked(session, job_id, token)
            if job:
                job.issue_url, job.status, job.error_code, job.phase = (
                    url,
                    "완료",
                    None,
                    "done",
                )
                job.issue_metadata = metadata(gateway, payload)
                logger.info("issue_state_changed job_id=%s code=COMPLETED", job_id)
                release(job)
                await session.commit()
    except IssueError as error:
        async with factory() as session:
            group, _, job = await locked(session, job_id, token)
            if job is None:
                return
            if phase == "done":
                job.readiness_reason = error.code
                release(job, 300)
                await session.commit()
                return
            if error.uncertain:
                logger.warning("issue_state_changed job_id=%s code=GITHUB_RESPONSE_UNKNOWN", job_id)
                job.phase, job.status, job.error_code = (
                    "reconcile",
                    "처리 중",
                    "GITHUB_RESPONSE_UNKNOWN",
                )
                release(job, 5)
            elif error.retryable and job.attempts < 5:
                # 재전송 가능한 연결 실패/명시적 rate-limit만 POST 이전 상태로 되돌린다.
                if phase == "prepare" and error.code in {
                    "GITHUB_CONNECTION_FAILED",
                    "GITHUB_RATE_LIMITED",
                }:
                    job.phase = "prepare"
                job.status, job.error_code = "대기", error.code
                release(job, max(error.retry_after, min(2**job.attempts, 30)))
            else:
                await failed(
                    session, group, job, error.code, uncertain=phase == "reconcile"
                )
            await session.commit()


async def run_once(factory, gateway=None):
    item = await claim(factory)
    if item is None:
        return False
    try:
        await process(factory, *item, gateway or get_issue_gateway(factory))
    except Exception:
        # POST가 시작됐을 수 있다. lease 만료 후 reconcile로 복구하며 원문 예외를 노출하지 않는다.
        logger.error("issue_worker_failed job_id=%s", item[0])
    return True


def metadata(gateway, payload):
    result = dict(getattr(gateway, "last_issue", None) or {})
    if result.get("title"):
        result["title"] = sanitize(result["title"], "issue_title")[0]
    return {**result, "created_by": payload.get("creation_mode"), "code_candidates": payload.get("code_candidates", [])}
