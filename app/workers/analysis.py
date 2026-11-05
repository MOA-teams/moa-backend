"""DB에 보존된 분석 요청 전달·결과 회수. LLM 실행 단계는 관리하지 않는다."""

import asyncio
import logging
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import or_, select
from app.core.enums import RunStatus
from app.core.errors import APIError
from app.core.pipeline import PipelineTransportError, get_pipeline_client
from app.db.models import AnalysisRequest, AnalysisRun
from app.services.analysis import (
    apply_result,
    current_request,
    lock_inquiry,
    utcnow,
    validate_identity,
)

logger = logging.getLogger(__name__)
TERMINAL = ("done", "failed", "superseded")


async def claim(factory):
    async with factory() as session:
        job = await session.scalar(
            select(AnalysisRequest)
            .where(
                AnalysisRequest.phase.not_in(TERMINAL),
                AnalysisRequest.available_at <= utcnow(),
                or_(
                    AnalysisRequest.lease_until.is_(None),
                    AnalysisRequest.lease_until < utcnow(),
                ),
            )
            .order_by(AnalysisRequest.available_at, AnalysisRequest.request_id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        job.lease_token = uuid4().hex
        job.lease_until = utcnow() + timedelta(seconds=60)
        await session.commit()
        return job.request_id, job.lease_token


async def locked(session, request_id):
    job = await session.get(AnalysisRequest, request_id)
    if job is None:
        return None, None
    inquiry = await lock_inquiry(session, job.inquiry_id)
    job = await session.get(
        AnalysisRequest, request_id, with_for_update=True, populate_existing=True
    )
    return inquiry, job


def owns(job, token):
    return job is not None and job.lease_token == token and job.phase not in TERMINAL


def release(job, delay=0):
    job.lease_token = job.lease_until = None
    job.available_at = utcnow() + timedelta(seconds=delay)


def fail(inquiry, job, code):
    job.phase, job.run_status, job.error_code = "failed", RunStatus.FAILED, code
    release(job)
    if current_request(inquiry, job):
        inquiry.processing_stage = "검토 대기"


async def process(factory, request_id, token, client):
    async with factory() as session:
        inquiry, job = await locked(session, request_id)
        if not owns(job, token):
            return
        if not current_request(inquiry, job):
            job.phase = "superseded"
            release(job)
            await session.commit()
            return
        if job.deadline and job.deadline <= utcnow():
            fail(inquiry, job, "AI_ANALYSIS_TIMEOUT")
            await session.commit()
            return
        phase, snapshot, run_id = job.phase, job.snapshot, job.run_id
        if phase == "submit":
            if job.attempts >= 5:
                fail(inquiry, job, "AI_RETRY_EXHAUSTED")
                await session.commit()
                return
            job.attempts += 1
            if job.deadline is None:
                job.deadline = utcnow() + timedelta(minutes=10)
        key = f"pipeline:{request_id}:{job.resubmissions}"
        # HTTP 호출 전에 시도 횟수·기준 입력이 commit되어 crash 후에도 같은 요청을 재사용한다.
        await session.commit()
    try:
        value = (
            await client.submit(snapshot, key)
            if phase == "submit"
            else await client.get(snapshot["tenant_id"], run_id)
        )
    except PipelineTransportError as error:
        async with factory() as session:
            inquiry, job = await locked(session, request_id)
            if not owns(job, token):
                return
            if not current_request(inquiry, job):
                job.phase = "superseded"
                release(job)
            elif phase == "poll" and error.status == 404 and job.resubmissions < 1:
                previous = await session.get(AnalysisRun, job.run_id)
                previous.superseded = True
                job.run_id = None
                job.resubmissions += 1
                job.attempts = 0
                job.phase = "submit"
                job.run_status = RunStatus.PENDING
                release(job)
            elif error.retryable and (phase == "poll" or job.attempts < 5):
                job.error_code = error.code
                release(job, 5 if phase == "poll" else min(2**job.attempts, 30))
            else:
                fail(inquiry, job, error.code)
            await session.commit()
        return
    async with factory() as session:
        inquiry, job = await locked(session, request_id)
        if not owns(job, token):
            return
        if phase == "submit":
            existing = await session.get(AnalysisRun, value.run_id)
            if existing and existing.request_id != job.request_id:
                fail(inquiry, job, "AI_RUN_ID_CONFLICT")
                await session.commit()
                return
            if existing is None:
                session.add(AnalysisRun(run_id=value.run_id, request_id=job.request_id))
            job.run_id = value.run_id
            job.phase = "poll" if current_request(inquiry, job) else "superseded"
            job.run_status = RunStatus.RUNNING
            job.error_code = None
            if current_request(inquiry, job):
                inquiry.processing_stage = "처리 중"
            release(job, 5)
            await session.commit()
            return
        if value.run_id != job.run_id:
            fail(inquiry, job, "AI_INVALID_RESPONSE")
            await session.commit()
            return
        try:
            validate_identity(inquiry, job, value)
        except APIError:
            fail(inquiry, job, "AI_INVALID_RESPONSE")
            await session.commit()
            return
        if value.run_status in (RunStatus.COMPLETED, RunStatus.FAILED):
            # 결과 적용 서비스가 동일 잠금 순서로 재검사한다. lease는 commit 후에도 유지한다.
            await session.commit()
            await apply_result(
                session, value, tenant=snapshot["tenant_id"], lease_token=token
            )
        else:
            job.run_status = value.run_status
            job.current_stage = value.current_stage
            job.error_code = None
            if current_request(inquiry, job):
                inquiry.processing_stage = value.current_stage or value.run_status.value
            else:
                job.phase = "superseded"
            release(job, 5)
            await session.commit()


async def run_once(factory, client):
    item = await claim(factory)
    if item is None:
        return False
    request_id, token = item
    try:
        await process(factory, request_id, token, client)
    except Exception:
        logger.error("analysis_worker_failed request_id=%s", request_id)
        async with factory() as session:
            inquiry, job = await locked(session, request_id)
            if owns(job, token):
                fail(inquiry, job, "AI_WORKER_ERROR")
                await session.commit()
    return True


async def run_forever(factory):
    while True:
        try:
            worked = await run_once(factory, get_pipeline_client())
        except Exception:
            logger.error("analysis_worker_database_unavailable")
            worked = False
        await asyncio.sleep(0.05 if worked else 2)


async def main():
    from app.db.session import async_session_factory, engine

    try:
        await run_forever(async_session_factory)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
