"""담당자 결정과 발송/새 문서 버전을 원자적으로 보존한다. 일괄 결과는 항목마다 확정한다."""

from sqlalchemy import select

from app.core.cursor import encode_cursor
from app.core.enums import DocStatus, IndexingStatus
from app.core.errors import APIError
from app.db.models import AnalysisDraft, Document, DocumentJob, DocumentVersion, Inquiry, ReviewItem, Tenant
from app.schemas.answers import AnswerRequest
from app.schemas.reviews import DecisionResult, ReviewDecision
from app.services.alerts import cursor_id
from app.services.analysis import lock_inquiry, utcnow
from app.services.answers import validate_and_persist_answer
from app.services.document_indexing import lock_document, status_event
from app.services.documents import check_barriers
from app.services.github import staff
from app.services.inquiries import begin_idempotent_request
from app.services.knowledge import lock_knowledge


def conflict(code="STALE_REVIEW", message="재검토 내용이 변경되었습니다. 목록을 다시 조회해 주세요."):
    raise APIError(409, code, message)


async def review_view(session, row):
    inquiry = await session.get(Inquiry, row.inquiry_id) if row.inquiry_id else None
    excluded = None
    suggestion, suggestion_status, suggestion_error = row.suggested_text, row.suggestion_status, row.suggestion_error
    if row.target_type == "document":
        number = row.result.get("version") if row.result and "version" in row.result else row.target_version
        version = await session.scalar(select(DocumentVersion).where(
            DocumentVersion.doc_id == row.document_id, DocumentVersion.version == number,
        ))
        excluded = not (version and version.searchable and not version.review_required and version.status == DocStatus.VALID)
    elif row.status == "pending":
        knowledge = await session.get(Tenant, row.tenant_id)
        if (inquiry.revision != row.snapshot["inquiry_revision"]
                or inquiry.latest_analysis_request_id != (row.analysis_request_id or row.snapshot["request_id"])
                or knowledge.knowledge_revision != row.snapshot["knowledge_revision"]):
            suggestion, suggestion_status, suggestion_error = None, "failed", "STALE_REVIEW_TARGET"
    return {"review_id": row.public_id, "target_type": row.target_type, "target_id": row.target_id,
            "target_version": row.target_version, "inquiry_id": inquiry.public_id if inquiry else None,
            "revision": row.revision, "title": row.title, "reason": row.reason, "source": row.source,
            "push_event_id": f"EVENT-{row.push_event_id:06d}" if row.push_event_id else None,
            "current_text": row.current_text, "suggested_text": suggestion, "suggestion_status": suggestion_status,
            "suggestion_error": suggestion_error, "excluded_from_search": excluded, "status": row.status,
            "created_at": row.created_at, "resolved_at": row.resolved_at, "result": row.result}


async def list_reviews(session, user, status, cursor, limit):
    tenant = staff(user)
    scope = {"type": "reviews", "tenant": tenant, "user": user.internal_user_id, "status": status}
    position = cursor_id(cursor, scope)
    statement = select(ReviewItem).where(ReviewItem.tenant_id == tenant)
    if status:
        statement = statement.where(ReviewItem.status == status)
    if position:
        statement = statement.where(ReviewItem.review_id < position)
    rows = list((await session.scalars(statement.order_by(ReviewItem.review_id.desc()).limit(limit + 1))).all())
    more, rows = len(rows) > limit, rows[:limit]
    return {"items": [await review_view(session, r) for r in rows], "has_more": more,
            "next_cursor": encode_cursor(scope, {"id": rows[-1].review_id}) if more else None}


async def replace_document(session, row, document, user, text):
    if document.latest_version != row.target_version:
        conflict("STALE_REVIEW_TARGET")
    old = await session.scalar(select(DocumentVersion).where(
        DocumentVersion.doc_id == document.doc_id, DocumentVersion.version == row.target_version,
    ))
    if old.source_revision != row.snapshot["source_revision"]:
        conflict("STALE_REVIEW_TARGET")
    await check_barriers(session, document.doc_id)
    old.status, old.searchable = DocStatus.RETIRED, False
    old.source_revision += 1
    barrier = status_event(document, old, "담당자 재검토 후 새 버전 등록") if old.submission_possible else None
    document.latest_version += 1
    version = DocumentVersion(doc_id=document.doc_id, version=document.latest_version, uploader_id=user.internal_user_id,
                              title=old.title, doc_type=old.doc_type, status=DocStatus.VALID, authored_at=utcnow(),
                              original_text=text, indexing_status=IndexingStatus.PENDING)
    session.add(version)
    await session.flush()
    session.add(DocumentJob(version_id=version.version_id, source_revision=1,
                            phase="barrier" if barrier else "extract", barrier=barrier, barrier_confirmed=barrier is None))
    from app.services.review_discovery import capture_document
    await capture_document(session, document, version)
    return {"doc_id": document.ai_source_id, "version": version.version, "indexing_status": "대기", "excluded_from_search": True}


async def apply_decision(session, user, review_id, payload, rag):
    tenant = staff(user)
    row = await session.scalar(select(ReviewItem).where(ReviewItem.public_id == review_id, ReviewItem.tenant_id == tenant))
    if row is None:
        raise APIError(404, "REVIEW_NOT_FOUND", "재검토 항목을 찾을 수 없습니다.")
    inquiry = await lock_inquiry(session, row.inquiry_id) if row.inquiry_id else None
    knowledge = await lock_knowledge(session, tenant)
    document = await lock_document(session, row.document_id) if row.document_id else None
    row = await session.get(ReviewItem, row.review_id, with_for_update=True, populate_existing=True)
    if row.revision != payload.expected_revision:
        conflict()
    if row.status != "pending":
        conflict("REVIEW_ALREADY_DECIDED", "이미 처리한 재검토 항목입니다.")
    result = None
    if payload.action != "dismiss":
        text = row.suggested_text if payload.action == "approve" else payload.text
        if payload.action == "approve" and (row.suggestion_status != "ready" or not text):
            conflict("REVIEW_SUGGESTION_UNAVAILABLE", "승인할 수정안이 없습니다. 직접 수정해 주세요.")
        if row.target_type == "document":
            result = await replace_document(session, row, document, user, text)
        else:
            if (inquiry.latest_analysis_request_id != (row.analysis_request_id or row.snapshot["request_id"])
                    or knowledge.knowledge_revision != row.snapshot["knowledge_revision"]):
                conflict("STALE_REVIEW_TARGET")
            if len(text) > 10000:
                raise APIError(400, "VALIDATION_ERROR", "답변은 10,000자 이하로 입력해 주세요.")
            draft = await session.get(AnalysisDraft, row.suggested_draft_id) if row.suggested_draft_id else None
            answer = AnswerRequest(
                draft_id=draft.public_id if draft else None, draft_revision=draft.revision if draft else None,
                expected_inquiry_revision=row.snapshot["inquiry_revision"],
                expected_last_customer_message_id=row.snapshot["customer_message_id"],
                expected_backend_knowledge_revision=row.snapshot["knowledge_revision"], answer_text=text,
                evidence=draft.content["evidence"] if draft else {},
            )
            result = (await validate_and_persist_answer(session, inquiry=inquiry, user=user, payload=answer, rag=rag)).model_dump(mode="json")
    row.status = "dismissed" if payload.action == "dismiss" else "resolved"
    row.decision, row.decided_by, row.resolved_at, row.result = payload.action, user.internal_user_id, utcnow(), result
    await session.flush()
    return DecisionResult(review_id=row.public_id, revision=row.revision, status=row.status, resolved_at=row.resolved_at, result=result)


async def decide(session, user, review_id, payload, key, rag):
    staff(user)
    try:
        record = await begin_idempotent_request(session, user_id=user.internal_user_id,
            path=f"/admin/reviews/{review_id}/decisions", key=key, payload=payload)
        if record.response is None:
            record.response = (await apply_decision(session, user, review_id, payload, rag)).model_dump(mode="json")
        response = DecisionResult.model_validate(record.response)
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


async def bulk_approve(session, user, payload, key, rag):
    staff(user)
    # 상위 멱등 기록의 행 잠금 아래 항목 결과를 하나씩 commit한다. 중단 후 같은 키로 재개 가능하다.
    for index, item in enumerate(payload.items):
        try:
            record = await begin_idempotent_request(session, user_id=user.internal_user_id,
                path="/admin/reviews/bulk-approve", key=key, payload=payload)
            completed = list((record.response or {}).get("items", []))
            if len(completed) <= index:
                try:
                    async with session.begin_nested():
                        result = await apply_decision(session, user, item.review_id,
                            ReviewDecision(action="approve", expected_revision=item.expected_revision), rag)
                    entry = {"review_id": item.review_id, "success": True, "status_code": 200,
                             "error_code": None, "result": result.model_dump(mode="json")}
                except APIError as exc:
                    entry = {"review_id": item.review_id, "success": False, "status_code": exc.status_code,
                             "error_code": exc.code, "result": None}
                completed.append(entry)
                record.response = {"items": completed}
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return {"items": completed}
