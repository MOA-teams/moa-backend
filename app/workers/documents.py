"""python -m app.workers.documents: PostgreSQL lease 기반 문서 처리 프로세스."""
import argparse
import asyncio
import logging
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select
from starlette.concurrency import run_in_threadpool

from app.core.enums import DocStatus, IndexingStatus
from app.core.errors import APIError
from app.core.rag import INDEX_STATUS, RagError, get_rag_client
from app.core.storage import get_s3_storage
from app.db.models import Attachment, DocumentJob
from app.services.document_extraction import ExtractionError, extract_file, validate_text
from app.services.document_indexing import (
    TERMINAL_PHASES, apply_view, confirm_barrier, fail_job, lock_job, source_payload, utcnow,
)

logger = logging.getLogger(__name__)


async def claim_job(factory):
    async with factory() as session:
        job = await session.scalar(select(DocumentJob).where(
            DocumentJob.phase.not_in(TERMINAL_PHASES), DocumentJob.available_at <= utcnow(),
            or_(DocumentJob.lease_until.is_(None), DocumentJob.lease_until < utcnow()),
        ).order_by(DocumentJob.available_at, DocumentJob.job_id).with_for_update(skip_locked=True).limit(1))
        if job is None:
            return None
        job.lease_token = uuid4().hex
        job.lease_until = utcnow() + timedelta(seconds=600)
        await session.commit()
        return job.job_id, job.lease_token


def current(version, job, token):
    return job.lease_token == token and version.source_revision == job.source_revision and job.phase not in TERMINAL_PHASES


def release(job, *, delay=0):
    job.lease_token = job.lease_until = None
    job.available_at = utcnow() + timedelta(seconds=delay)
    job.attempts = 0
    job.last_error = None


def resubmit_or_fail(job, version):
    if job.resubmissions >= 3:
        fail_job(job, version, RagError("AI_INDEXING_RETRY_EXHAUSTED", retryable=False))
    else:
        job.resubmissions += 1
        job.phase = "submit"
        release(job, delay=(5, 30, 120)[job.resubmissions - 1])


async def process_claim(factory, job_id, token, rag, storage=None):
    async with factory() as session:
        document, version, job = await lock_job(session, job_id)
        if not current(version, job, token):
            if job.lease_token == token:
                job.phase = "superseded"
                release(job)
            await session.commit()
            return
        if not job.barrier_confirmed:
            await session.commit()
            try:
                await confirm_barrier(session, job_id, rag)
            except RagError:
                pass  # confirm_barrier가 오류와 재시도 일정을 이미 저장했다.
            return
        phase = job.phase
        if phase == "extract":
            original = version.extracted_text if version.extracted_text is not None else version.original_text
            attachment = await session.get(Attachment, version.attachment_id) if version.attachment_id else None
            key, mime = (attachment.storage_key, attachment.mime) if attachment else (None, None)
            version.indexing_status = IndexingStatus.RUNNING
            await session.commit()
        else:
            try:
                if phase == "submit":
                    result = await rag.submit(document.tenant_id, source_payload(document, version))
                    version.indexing_status = IndexingStatus(INDEX_STATUS[result.indexing_status])
                    version.sync_error = version.indexing_error = None
                    job.phase, job.poll_started_at = "poll", utcnow()
                    release(job, delay=5)
                elif phase == "poll":
                    try:
                        view = await rag.view(document.tenant_id, document.ai_source_id, version.version)
                    except RagError as exc:
                        if exc.code != "SOURCE_NOT_FOUND":
                            raise
                        resubmit_or_fail(job, version)
                    else:
                        apply_view(document, version, view)
                        if version.searchable or (view.indexing_status == "완료" and (version.status == DocStatus.RETIRED or version.review_required)):
                            job.phase = "done"
                            release(job)
                        elif view.indexing_status == "실패" or (job.poll_started_at and utcnow() - job.poll_started_at >= timedelta(seconds=300)):
                            resubmit_or_fail(job, version)
                        else:
                            release(job, delay=5)
                else:
                    raise RagError("INVALID_DOCUMENT_JOB", retryable=False)
            except RagError as exc:
                fail_job(job, version, exc)
            await session.commit()
            return
    # OCR/S3 읽기는 문서 잠금 밖에서 수행한다. 결과 저장 시 lease와 revision을 다시 비교한다.
    error = None
    try:
        if original is not None:
            text, digest = validate_text(original)
        else:
            source_storage = storage if storage is not None else get_s3_storage()
            data = await run_in_threadpool(source_storage.read, key)
            text, digest = await extract_file(data, mime)
    except ExtractionError as exc:
        error = RagError(exc.code, retryable=False)
    except APIError as exc:
        error = RagError(exc.code, retryable=exc.status_code >= 500)
    async with factory() as session:
        document, version, job = await lock_job(session, job_id)
        if not current(version, job, token):
            if job.lease_token == token:
                job.phase = "superseded"
                release(job)
        elif error:
            fail_job(job, version, error)
        else:
            version.extracted_text, version.content_hash = text, digest
            version.indexing_error = version.sync_error = None
            # 네트워크 전 별도 commit: 이후 crash 시 I03이 도착했을 가능성을 보존한다.
            version.submission_possible = True
            job.phase = "submit"
            release(job)
        await session.commit()


async def run_once(factory, rag, storage=None):
    claimed = await claim_job(factory)
    if claimed is None:
        return False
    job_id, token = claimed
    try:
        await process_claim(factory, job_id, token, rag, storage)
    except Exception:
        # 예외 상세에 원문·인증정보가 섞일 수 있어 job ID만 기록한다.
        logger.error("document_job_unexpected_error job_id=%s", job_id)
        async with factory() as session:
            _, version, job = await lock_job(session, job_id)
            if job and current(version, job, token):
                fail_job(job, version, RagError("DOCUMENT_WORKER_ERROR"))
                await session.commit()
    return True


async def retry_job(factory, job_id):
    async with factory() as session:
        _, version, job = await lock_job(session, job_id)
        if job is None or job.phase != "failed" or version.source_revision != job.source_revision:
            raise ValueError("현재 revision의 실패한 작업만 재시도할 수 있습니다.")
        job.phase = "barrier" if not job.barrier_confirmed else "extract"
        job.resubmissions = 0
        release(job)
        version.searchable = False
        version.sync_error = version.indexing_error = None
        version.indexing_status = IndexingStatus.PENDING
        await session.commit()


async def main(once=False, retry=None):
    from app.db.session import async_session_factory, engine

    try:
        if retry is not None:
            await retry_job(async_session_factory, retry)
            return
        while True:
            worked = await run_once(async_session_factory, get_rag_client())
            if once:
                return
            if not worked:
                await asyncio.sleep(2)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="문서 인덱싱 작업 처리")
    parser.add_argument("--once", action="store_true", help="대기 작업 한 단계만 처리")
    parser.add_argument("--retry-job", type=int, help="현재 revision의 실패 작업을 재시도")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main(args.once, args.retry_job))
