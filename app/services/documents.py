import hashlib
import json
import time

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.cursor import decode_cursor, encode_cursor
from app.core.enums import DocStatus, IndexingStatus, UserRole
from app.core.errors import APIError
from app.core.rag import RagError
from app.db.models import Attachment, Document, DocumentCounter, DocumentJob, DocumentVersion, IdempotencyRecord
from app.schemas.documents import DocumentAccepted, DocumentListResponse, DocumentStatusResponse, DocumentSummary
from app.services.attachments import attachment_not_found, require_tenant
from app.services.document_indexing import confirm_barrier, lock_document, refresh_view, status_event


def tenant_for(user):
    if user.role not in (UserRole.AGENT, UserRole.ADMIN):
        raise APIError(403, "FORBIDDEN", "담당자 또는 관리자 권한이 필요합니다.")
    return require_tenant(user)


def not_found():
    return APIError(404, "DOCUMENT_NOT_FOUND", "문서 또는 버전을 찾을 수 없습니다.")


async def require_document(session, doc_id, tenant_id):
    # 외부 조회는 기업 범위를 포함한다. 내부 작업의 숫자 PK는 이 경계 밖으로 반환하지 않는다.
    if isinstance(doc_id, str):
        internal_id = await session.scalar(select(Document.doc_id).where(Document.tenant_id == tenant_id, Document.ai_source_id == doc_id))
        if internal_id is None:
            raise not_found()
        doc_id = internal_id
    document = await lock_document(session, doc_id)
    if document is None or document.tenant_id != tenant_id:
        raise not_found()
    return document


async def check_barriers(session, doc_id):
    pending = await session.scalar(select(DocumentJob.job_id).join(DocumentVersion).where(
        DocumentVersion.doc_id == doc_id, DocumentJob.barrier_confirmed.is_(False),
    ).limit(1))
    if pending:
        raise APIError(409, "DOCUMENT_SYNC_PENDING", "이전 상태 변경을 먼저 동기화해야 합니다. 기존 요청을 재시도하세요.")


async def begin_request(session, user, key, payload):
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    request_hash = hashlib.sha256(json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    await session.execute(insert(IdempotencyRecord).values(
        user_id=user.internal_user_id, path="/admin/documents", key_hash=key_hash, request_hash=request_hash, contract_version=2,
    ).on_conflict_do_nothing(constraint="uq_idempotency_user_path_key"))
    record = await session.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.user_id == user.internal_user_id, IdempotencyRecord.path == "/admin/documents", IdempotencyRecord.key_hash == key_hash,
    ).with_for_update())
    if record.contract_version != 2:
        raise APIError(409, "LEGACY_IDEMPOTENCY_KEY", "이전 API 계약의 요청입니다. 처리 결과를 확인해 주세요.")
    if record.request_hash != request_hash:
        raise APIError(409, "IDEMPOTENCY_CONFLICT", "같은 Idempotency-Key로 다른 요청을 보낼 수 없습니다.")
    return record


async def new_document(session, tenant_id):
    from app.services.knowledge import lock_knowledge
    await lock_knowledge(session, tenant_id)
    await session.execute(insert(DocumentCounter).values(tenant_id=tenant_id).on_conflict_do_nothing())
    counter = await session.scalar(select(DocumentCounter).where(DocumentCounter.tenant_id == tenant_id).with_for_update())
    if counter.last_number >= 999:
        raise APIError(503, "ID_SPACE_EXHAUSTED", "발급 가능한 문서 ID 범위를 초과했습니다.")
    counter.last_number += 1
    document = Document(tenant_id=tenant_id, ai_source_id=f"DOC-{counter.last_number:03d}", latest_version=1)
    session.add(document)
    await session.flush()
    return document


async def document_attachment(session, attachment_id, user):
    if attachment_id is None:
        return
    attachment = await session.scalar(select(Attachment).where(
        Attachment.public_id == attachment_id, Attachment.tenant_id == user.tenant_id,
        Attachment.uploader_id == user.internal_user_id, Attachment.purpose == "document",
    ).with_for_update())
    if attachment is None:
        raise attachment_not_found()
    linked = await session.scalar(select(DocumentVersion.version_id).where(DocumentVersion.attachment_id == attachment.attachment_id))
    if attachment.message_id is not None or linked is not None:
        raise APIError(409, "ATTACHMENT_ALREADY_LINKED", "이미 연결된 첨부입니다.")
    return attachment.attachment_id


async def ensure_accepted(session, job_id, rag):
    try:
        await confirm_barrier(session, job_id, rag)
    except RagError:
        raise APIError(503, "DOCUMENT_SYNC_FAILED", "검색 상태 반영을 확인하지 못했습니다. 같은 요청으로 재시도하세요.") from None


async def create_document(session, *, user, payload, key, rag):
    tenant = tenant_for(user)
    try:
        record = await begin_request(session, user, key, payload)
        if record.response:
            response = DocumentAccepted.model_validate(record.response)
            document = await require_document(session, response.doc_id, tenant)
            job_id = await session.scalar(select(DocumentJob.job_id).join(DocumentVersion).where(
                DocumentVersion.doc_id == document.doc_id, DocumentVersion.version == response.version,
                DocumentJob.source_revision == 1,
            ))
        else:
            barrier = None
            if payload.doc_id is None:
                document = await new_document(session, tenant)
            else:
                document = await require_document(session, payload.doc_id, tenant)
                await check_barriers(session, document.doc_id)
                if payload.version != document.latest_version + 1:
                    raise APIError(409, "DOCUMENT_VERSION_CONFLICT", "현재 최신 버전의 다음 번호만 등록할 수 있습니다.")
                old = await session.scalar(select(DocumentVersion).where(
                    DocumentVersion.doc_id == document.doc_id, DocumentVersion.version == document.latest_version,
                ))
                old.status, old.searchable = DocStatus.RETIRED, False
                old.source_revision += 1
                if old.submission_possible:
                    barrier = status_event(document, old, "새 문서 버전 등록")
                document.latest_version = payload.version
            attachment_id = await document_attachment(session, payload.attachment_id, user)
            version = DocumentVersion(
                doc_id=document.doc_id, version=payload.version, uploader_id=user.internal_user_id,
                title=payload.title, doc_type=payload.doc_type, status=payload.status,
                authored_at=payload.created_at, original_text=payload.text, attachment_id=attachment_id,
                indexing_status=IndexingStatus.PENDING,
            )
            session.add(version)
            await session.flush()
            job = DocumentJob(version_id=version.version_id, source_revision=1,
                              phase="barrier" if barrier else "extract", barrier=barrier, barrier_confirmed=barrier is None)
            session.add(job)
            await session.flush()
            response = DocumentAccepted(doc_id=document.ai_source_id, version=version.version, status=version.status,
                                        indexing_status=version.indexing_status, uploaded_at=version.created_at)
            record.response = response.model_dump(mode="json")
            job_id = job.job_id
            from app.services.review_discovery import capture_document
            await capture_document(session, document, version)
        # 원본/멱등 응답/차단 의도가 함께 보존된 이후에만 외부 AI를 호출한다.
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    await ensure_accepted(session, job_id, rag)
    return response


async def change_status(session, *, user, doc_id, version_number, payload, rag):
    tenant = tenant_for(user)
    try:
        document = await require_document(session, doc_id, tenant)
        doc_id = document.doc_id
        version = await session.scalar(select(DocumentVersion).where(
            DocumentVersion.doc_id == doc_id, DocumentVersion.version == version_number,
        ))
        if version is None:
            raise not_found()
        if version_number != document.latest_version and payload.status == DocStatus.VALID:
            raise APIError(409, "DOCUMENT_VERSION_CONFLICT", "이전 버전을 다시 활성화할 수 없습니다. 새 버전을 등록하세요.")
        job = await session.scalar(select(DocumentJob).where(
            DocumentJob.version_id == version.version_id, DocumentJob.source_revision == version.source_revision,
        ))
        same = (version.status, version.review_required) == (payload.status, payload.review_required)
        if same:
            pending = await session.scalar(select(DocumentJob).join(DocumentVersion).where(
                DocumentVersion.doc_id == doc_id, DocumentJob.barrier_confirmed.is_(False),
            ).order_by(DocumentJob.job_id).limit(1))
            if pending is not None:
                job = pending
        # 동일 상태 재시도는 저장된 event를 재사용한다. 이전 등록의 차단을 우회하지 않는다.
        if not same:
            await check_barriers(session, doc_id)
            version.status, version.review_required = payload.status, payload.review_required
            version.searchable = False
            version.source_revision += 1
            barrier = status_event(document, version, payload.reason)
            if payload.status == DocStatus.VALID and not payload.review_required:
                # I04가 완료 manifest를 재활성화해도 실제 embedding은 없을 수 있다.
                # 상태 event 다음 revision의 I03으로 반드시 새 generation을 만든다.
                version.source_revision += 1
                version.indexing_status = IndexingStatus.PENDING
            job = DocumentJob(version_id=version.version_id, source_revision=version.source_revision,
                              phase="barrier", barrier=barrier, barrier_confirmed=False)
            session.add(job)
            await session.flush()
            from app.services.review_discovery import capture_document
            await capture_document(session, document, version)
        await session.commit()
        if job is not None:
            await ensure_accepted(session, job.job_id, rag)
        # 재시도 중 새 변경이 들어왔으면 현재 확정 상태를 다시 읽는다.
        await require_document(session, doc_id, tenant)
        await session.refresh(version)
        response = DocumentStatusResponse(doc_id=document.ai_source_id, version=version_number, status=version.status,
                                          review_required=version.review_required, searchable=version.searchable)
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


def summary(document, version, attachment_id):
    return DocumentSummary(
        doc_id=document.ai_source_id, version=version.version, title=version.title, doc_type=version.doc_type,
        status=version.status, review_required=version.review_required, searchable=version.searchable,
        created_at=version.authored_at, uploaded_at=version.created_at, attachment_id=attachment_id,
        indexing_status=version.indexing_status, indexing_error=version.indexing_error, sync_error=version.sync_error,
    )


async def list_documents(session, *, user, status, cursor, limit, rag):
    tenant = tenant_for(user)
    scope = {"type": "documents", "tenant": tenant, "user": user.internal_user_id, "status": status.value if status else None}
    position = decode_cursor(cursor, scope)
    statement = select(Document.doc_id).join(DocumentVersion, (DocumentVersion.doc_id == Document.doc_id) & (DocumentVersion.version == Document.latest_version)).where(Document.tenant_id == tenant)
    if status is not None:
        statement = statement.where(DocumentVersion.status == status)
    if position is not None:
        before = position.get("before")
        if type(before) is not int or before <= 0:
            raise APIError(400, "INVALID_CURSOR", "유효한 조회 cursor가 필요합니다.")
        statement = statement.where(Document.doc_id < before)
    ids = list((await session.scalars(statement.order_by(Document.doc_id.desc()).limit(limit + 1))).all())
    has_more, ids = len(ids) > limit, ids[:limit]
    await session.commit()
    items, started = [], time.monotonic()
    for doc_id in ids:
        document = await require_document(session, doc_id, tenant)
        doc_id = document.doc_id
        version = await session.scalar(select(DocumentVersion).where(DocumentVersion.doc_id == doc_id, DocumentVersion.version == document.latest_version))
        if time.monotonic() - started < 10:
            await refresh_view(session, document, version, rag)
        else:
            version.sync_error, version.searchable = "AI_SYNC_TIMEOUT", False
        if status is None or version.status == status:
            attachment_id = await session.scalar(select(Attachment.public_id).where(Attachment.attachment_id == version.attachment_id)) if version.attachment_id else None
            items.append(summary(document, version, attachment_id))
        await session.commit()
    return DocumentListResponse(items=items, has_more=has_more,
                                next_cursor=encode_cursor(scope, {"before": ids[-1]}) if has_more else None)
