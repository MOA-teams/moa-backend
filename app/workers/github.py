"""python -m app.workers.github: PostgreSQL lease를 사용하는 GitHub 동기화 worker."""

import argparse
import asyncio
import logging
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import or_, select
from app.core.github import GitHubError, get_github_client
from app.core.rag import RagError, get_rag_client
from app.db.models import GitHubJob, GitHubSource
from app.services.analysis import utcnow
from app.services.document_indexing import confirm_barrier
from app.services.github_sync import (
    locked_job,
    current,
    collect,
    prepare_snapshot,
    sync_one,
)

logger = logging.getLogger(__name__)


async def claim(factory):
    async with factory() as session:
        job = await session.scalar(
            select(GitHubJob)
            .where(
                GitHubJob.phase.not_in(["done", "failed", "superseded"]),
                GitHubJob.available_at <= utcnow(),
                or_(GitHubJob.lease_until.is_(None), GitHubJob.lease_until < utcnow()),
            )
            .order_by(GitHubJob.job_id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        job.lease_token, job.lease_until = uuid4().hex, utcnow() + timedelta(
            seconds=600
        )
        await session.commit()
        return job.job_id, job.lease_token


async def process(factory, job_id, token, github, rag):
    async with factory() as session:
        integration, job = await locked_job(session, job_id)
        if not current(integration, job, token):
            if job and job.lease_token == token:
                job.phase, job.lease_token, job.lease_until = "superseded", None, None
                await session.commit()
            return
        if job.phase == "discover":
            sources = list(
                (
                    await session.scalars(
                        select(GitHubSource).where(
                            GitHubSource.integration_id == integration.integration_id
                        )
                    )
                ).all()
            )
            await session.commit()
            snapshot = await asyncio.wait_for(
                collect(integration, sources, github), 300
            )
            integration, job = await locked_job(session, job_id)
            if not current(integration, job, token):
                await session.rollback()
                return
            await prepare_snapshot(session, integration, job, *snapshot)
            integration, job = await locked_job(session, job_id)
            if not current(integration, job, token):
                await session.rollback()
                return
        result = await sync_one(session, integration, job, rag)
        # 네트워크 호출 전에 submission_possible을 저장한 경우도 lease를 다시 확인한다.
        integration, job = await locked_job(session, job_id)
        if not current(integration, job, token) and job.phase != "done":
            await session.rollback()
            return
        if isinstance(result, tuple) and result[0] == "document":
            await session.commit()
            await confirm_barrier(session, result[1], rag)
            integration, job = await locked_job(session, job_id)
            if not current(integration, job, token):
                await session.rollback()
                return
        job.lease_token = job.lease_until = None
        job.attempts, job.error_code = 0, None
        job.available_at = utcnow() + timedelta(
            seconds=5 if isinstance(result, tuple) and result[0] == "poll" else 0
        )
        await session.commit()


async def run_once(factory, github, rag):
    claimed = await claim(factory)
    if claimed is None:
        return False
    job_id, token = claimed
    try:
        await process(factory, job_id, token, github, rag)
    except Exception as exc:
        known = isinstance(exc, (GitHubError, RagError))
        code = exc.code if known else "GITHUB_WORKER_ERROR"
        retryable = exc.retryable if known else True
        if not known:
            logger.error("github_worker_error job_id=%s", job_id)
        async with factory() as session:
            integration, job = await locked_job(session, job_id)
            if current(integration, job, token):
                job.error_code = integration.error_code = code
                job.lease_token = job.lease_until = None
                if retryable and job.attempts < 3:
                    delay = (5, 30, 120)[job.attempts]
                    if isinstance(exc, GitHubError):
                        delay = max(delay, exc.retry_after)
                    job.attempts += 1
                    job.available_at = utcnow() + timedelta(seconds=delay)
                else:
                    job.phase, integration.status = "failed", "동기화 실패"
                await session.commit()
    return True


async def retry_job(factory, job_id):
    """운영자가 원인을 해결한 후 최신 실패 작업의 저장된 의도를 재개한다."""
    async with factory() as session:
        integration, job = await locked_job(session, job_id)
        if (
            job is None
            or job.generation != integration.generation
            or job.phase != "failed"
        ):
            raise ValueError("현재 세대의 실패한 GitHub 작업만 재시도할 수 있습니다.")
        job.phase = "discover" if job.manifest is None else "sync"
        job.attempts, job.error_code = 0, None
        job.lease_token = job.lease_until = None
        job.available_at = utcnow()
        integration.status, integration.error_code = "동기화 중", None
        sources = (
            await session.scalars(
                select(GitHubSource).where(
                    GitHubSource.integration_id == integration.integration_id,
                    GitHubSource.phase == "poll",
                )
            )
        ).all()
        for source in sources:
            source.poll_started_at = utcnow()
            # 인덱싱 실패도 동일 원본/revision의 I03 재접수로 복구한다.
            source.phase = "submit"
        await session.commit()


async def main(once=False, retry=None):
    from app.db.session import async_session_factory, engine

    try:
        if retry is not None:
            await retry_job(async_session_factory, retry)
            return
        while True:
            worked = await run_once(
                async_session_factory, get_github_client(), get_rag_client()
            )
            if once:
                return
            if not worked:
                await asyncio.sleep(2)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--retry-job", type=int)
    args = parser.parse_args()
    asyncio.run(main(args.once, args.retry_job))
