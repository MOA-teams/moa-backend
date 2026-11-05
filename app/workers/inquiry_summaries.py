"""요약별 session advisory lock으로 HTTP를 직렬화한다.

DB 의도는 HTTP 전에 commit한다. 연결을 전용으로 유지하므로 프로세스가 종료되면
잠금이 해제되고, lease 시간 추정만으로 오래된 HTTP와 새 메타데이터가 경합하지 않는다.
"""
from datetime import timedelta
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rag import RagError, get_rag_client
from app.db.models import InquirySummaryJob
from app.services.groups import utcnow

TERMINAL = ("done", "failed", "superseded")


async def run_once(factory, rag=None):
    rag = rag or get_rag_client()
    async with factory() as session:
        inquiry_id = await session.scalar(select(InquirySummaryJob.inquiry_id).where(
            InquirySummaryJob.phase.not_in(TERMINAL), InquirySummaryJob.available_at <= utcnow()
        ).order_by(InquirySummaryJob.available_at, InquirySummaryJob.job_id).limit(1))
    if inquiry_id is None:
        return False
    # 명시적으로 같은 물리 연결에서 session lock 획득/해제. pool로 잠금을 반환하지 않는다.
    async with factory.kw["bind"].connect() as connection:
        key = f"summary:{inquiry_id}"
        acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), {"key":key})
        await connection.commit()
        if not acquired:
            return False
        try:
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                jobs = list((await session.scalars(select(InquirySummaryJob).where(InquirySummaryJob.inquiry_id == inquiry_id).order_by(InquirySummaryJob.source_revision))).all())
                job = jobs[-1]
                for old in jobs[:-1]:
                    if old.phase not in TERMINAL:
                        old.phase = "superseded"
                if job.phase in TERMINAL or job.available_at > utcnow():
                    await session.commit()
                    return True
                if job.deadline is None:
                    job.deadline = utcnow()+timedelta(minutes=10)
                if job.deadline <= utcnow():
                    job.phase, job.error_code = "failed", "SUMMARY_SYNC_TIMEOUT"
                    await session.commit()
                    return True
                job.attempts += 1
                payload, phase = job.payload, job.phase
                await session.commit()
                try:
                    if phase == "barrier":
                        for blocked in job.barriers:
                            # 존재 여부가 불명확한 과거 버전도 폐기 manifest로 먼저 fence한다.
                            await rag.submit(payload["tenant_id"], blocked)
                            event = {"tenant_id": payload["tenant_id"], "event_id":f"summary-{job.job_id}-{blocked['version']}",
                                     "reason":"문의 요약 최신성 갱신", "sources":[{k:blocked[k] for k in ("source_type","source_id","version","source_revision","status","review_required")}]}
                            await rag.statuses(payload["tenant_id"], event)
                        job.phase = "submit"
                    elif phase == "submit":
                        await rag.submit(payload["tenant_id"], payload)
                        job.phase = "poll"
                    else:
                        view = await rag.view(payload["tenant_id"], payload["source_id"], payload["version"], source_type="문의 요약")
                        if view.source_revision != payload["source_revision"] or view.status != payload["status"] or view.review_required:
                            raise RagError("AI_REVISION_MISMATCH")
                        if view.indexing_status == "실패" or view.error:
                            raise RagError("SUMMARY_INDEXING_FAILED", retryable=False)
                        if view.indexing_status == "완료" and view.searchable == (payload["status"] == "유효"):
                            job.phase = "done"
                    job.attempts, job.error_code = 0, None
                    job.available_at = utcnow()+timedelta(seconds=5 if job.phase == "poll" else 0)
                except RagError as error:
                    job.error_code = error.code
                    job.available_at = utcnow()+timedelta(seconds=min(2**job.attempts, 30))
                    if not error.retryable or job.attempts >= 5:
                        job.phase = "failed"
                await session.commit()
        finally:
            await connection.rollback()
            await connection.execute(text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), {"key":key})
            await connection.commit()
    return True
