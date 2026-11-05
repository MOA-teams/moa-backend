"""문서 단위 잠금과 영속 작업으로 DB/AI 사이의 부분 실패를 복구한다."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select

from app.core.enums import DocStatus, IndexingStatus
from app.core.rag import AI_STATUS, INDEX_STATUS, RagError
from app.db.models import Document, DocumentJob, DocumentVersion

TERMINAL_PHASES = ("done", "failed", "superseded")
RETRY_DELAYS = (5, 30, 120)


def utcnow():
    return datetime.now(timezone.utc)


async def lock_document(session, doc_id):
    return await session.scalar(select(Document).where(Document.doc_id == doc_id).with_for_update().execution_options(populate_existing=True))


async def lock_job(session, job_id):
    doc_id = await session.scalar(select(DocumentVersion.doc_id).join(DocumentJob, DocumentJob.version_id == DocumentVersion.version_id).where(DocumentJob.job_id == job_id))
    if doc_id is None:
        return None, None, None
    document = await lock_document(session, doc_id)
    job = await session.get(DocumentJob, job_id, populate_existing=True)
    version = await session.get(DocumentVersion, job.version_id, populate_existing=True)
    return document, version, job


def source_payload(document, version, *, revision=None, status=None, review=None):
    return {
        "tenant_id": document.tenant_id, "source_type": "문서", "source_id": document.ai_source_id,
        "version": version.version, "source_revision": revision if revision is not None else version.source_revision,
        "title": version.title, "text": version.extracted_text, "visibility": "고객 공개",
        "status": status if status is not None else AI_STATUS[version.status],
        "review_required": version.review_required if review is None else review,
        "content_hash": version.content_hash,
        "metadata": {"doc_type": version.doc_type.value, "created_at": version.authored_at.isoformat()},
        "evidence": {"docs": [], "code": []},
    }


def status_event(document, version, reason, *, revision=None):
    return {
        "tenant_id": document.tenant_id, "event_id": f"document-{uuid4().hex}", "reason": reason,
        "sources": [{"source_type": "문서", "source_id": document.ai_source_id, "version": version.version,
                     "source_revision": revision if revision is not None else version.source_revision,
                     "status": AI_STATUS[version.status], "review_required": version.review_required}],
    }


def fail_job(job, version, error):
    job.last_error = error.code
    version.sync_error = error.code
    version.searchable = False
    job.lease_until = job.lease_token = None
    if error.retryable and job.attempts < len(RETRY_DELAYS):
        job.available_at = utcnow() + timedelta(seconds=RETRY_DELAYS[job.attempts])
        job.attempts += 1
    else:
        job.phase = "failed"
        version.indexing_status = IndexingStatus.FAILED
        version.indexing_error = error.code


async def confirm_barrier(session, job_id, rag):
    """호출 전 의도가 이미 commit되어 있어야 한다. 성공을 확정하는 동안 문서를 잠근다."""
    document, version, job = await lock_job(session, job_id)
    if job is None or job.barrier_confirmed:
        await session.commit()
        return
    if job.source_revision != version.source_revision:
        await session.rollback()
        raise RagError("STALE_DOCUMENT_OPERATION", retryable=False)
    try:
        target = version
        if job.barrier:
            change = job.barrier["sources"][0]
            target = await session.scalar(select(DocumentVersion).where(
                DocumentVersion.doc_id == document.doc_id, DocumentVersion.version == change["version"],
            ).execution_options(populate_existing=True))
            if target.submission_possible:
                try:
                    await rag.statuses(document.tenant_id, job.barrier)
                except RagError as exc:
                    if exc.code != "SOURCE_NOT_FOUND" or not target.extracted_text:
                        raise
                    # 응답 유실 후 아직 manifest가 없을 수 있다. 검색 불가 원본으로 fence를 먼저 세운다.
                    blocked = change["status"] == "폐기" or change["review_required"]
                    await rag.submit(document.tenant_id, source_payload(
                        document, target, revision=change["source_revision"] if blocked else change["source_revision"] - 1,
                        status=change["status"] if blocked else "폐기", review=change["review_required"],
                    ))
                    await rag.statuses(document.tenant_id, job.barrier)
                view = await rag.view(document.tenant_id, document.ai_source_id, target.version)
                if (view.source_revision, view.status, view.review_required) != (
                    change["source_revision"], change["status"], change["review_required"],
                ) or ((change["status"] == "폐기" or change["review_required"]) and view.searchable):
                    raise RagError("AI_BLOCK_NOT_CONFIRMED")
            target.searchable = False
            target.sync_error = None
        job.barrier_confirmed = True
        job.last_error = version.sync_error = None
        job.attempts = 0
        job.lease_until = job.lease_token = None
        job.available_at = utcnow()
        # 폐기/재검토 요청은 차단으로 완료한다. 등록된 원본의 추출 결과는 그대로 보존한다.
        status_change = job.barrier and job.barrier["sources"][0]["version"] == version.version
        if status_change and (version.status == DocStatus.RETIRED or version.review_required):
            job.phase = "done"
        else:
            job.phase = "extract"
        await session.commit()
    except RagError as exc:
        fail_job(job, version, exc)
        await session.commit()
        raise


def apply_view(document, version, view):
    if (view.source_revision, view.status, view.review_required) != (
        version.source_revision, AI_STATUS[version.status], version.review_required,
    ):
        raise RagError("AI_REVISION_MISMATCH")
    version.indexing_status = IndexingStatus(INDEX_STATUS[view.indexing_status])
    # 외부 오류 문자열에 원문이 포함될 수 있으므로 실패의 분류만 공개한다.
    version.indexing_error = "AI_INDEXING_FAILED" if view.error or view.indexing_status == "실패" else None
    version.sync_error = None
    version.searchable = bool(
        view.searchable and view.indexing_status == "완료" and not view.error
        and version.version == document.latest_version and version.status == DocStatus.VALID
        and not version.review_required
    )


async def refresh_view(session, document, version, rag):
    if not version.submission_possible:
        version.searchable = False
        return
    try:
        apply_view(document, version, await rag.view(document.tenant_id, document.ai_source_id, version.version))
        # embedding 공간 변경 시 완료 상태여도 재인덱싱이 필요하다. 같은 원본/revision을 재전달한다.
        if version.status == DocStatus.VALID and not version.review_required and not version.searchable:
            job = await session.scalar(select(DocumentJob).where(
                DocumentJob.version_id == version.version_id, DocumentJob.source_revision == version.source_revision,
            ))
            if job and job.phase == "done" and job.resubmissions < 3:
                job.phase = "poll"
                job.available_at = utcnow()
                job.poll_started_at = utcnow() - timedelta(seconds=301)
    except RagError as exc:
        version.sync_error, version.searchable = exc.code, False
