"""지식 변경 트랜잭션에서 재검토 의도를 저장한다. 문의 잠금과 외부 호출은 하지 않는다."""

from sqlalchemy import select

from app.core.enums import DocStatus, InquiryStatus
from app.db.models import AnalysisDraft, AnalysisRequest, AnalysisRun, Answer, Document, DocumentVersion, Inquiry, ReviewItem
from app.services.github_security import includes
from app.services.knowledge import lock_knowledge


def references(content):
    docs = list(content.get("evidence", {}).get("docs", []))
    code = list(content.get("evidence", {}).get("code", []))
    for citation in content.get("citations", []):
        docs.extend(citation.get("docs", []))
        code.extend(citation.get("code", []))
    return docs, code


async def record_review(session, *, tenant, kind, target, version, cause, source, title, reason, text, snapshot,
                        inquiry_id=None, document_id=None, push_id=None):
    # 모든 생성자가 기업 지식 잠금을 먼저 취득하므로 대상 고유 제약과 함께 중복을 막는다.
    await lock_knowledge(session, tenant)
    row = await session.scalar(select(ReviewItem).where(
        ReviewItem.tenant_id == tenant, ReviewItem.target_type == kind,
        ReviewItem.target_id == target, ReviewItem.target_version == version,
    ).with_for_update())
    if row and (cause in row.cause_keys or (source == "existing_state" and cause.startswith("document:"))):
        # 이관 스캔은 이미 기록한 실제 push/문서 변경 원인과 담당자 결정을 덮어쓰지 않는다.
        return row
    if row is None:
        row = ReviewItem(tenant_id=tenant, target_type=kind, target_id=target, target_version=version,
                         inquiry_id=inquiry_id, document_id=document_id, current_text=text,
                         title=title, snapshot=snapshot, source=source, reason=reason, cause_keys=[cause])
        session.add(row)
    else:
        row.cause_keys = row.cause_keys + [cause]
        row.revision += 1
        row.snapshot = {**row.snapshot, **snapshot, "knowledge_revision": snapshot["knowledge_revision"],
                        "request_id": row.analysis_request_id or row.snapshot.get("request_id")}
        row.status = "pending"
        row.analysis_request_id = row.suggested_draft_id = row.suggested_text = None
        row.suggestion_error = row.decision = row.decided_by = row.resolved_at = row.result = None
    row.source, row.reason, row.push_event_id = source, reason, push_id
    row.suggestion_status = "manual_required" if kind == "document" else "waiting_for_sources"
    if kind == "document":
        row.suggestion_error = "DOCUMENT_MANUAL_EDIT_REQUIRED"
    await session.flush()
    return row


async def affected_drafts(session, tenant, predicate, *, cause, source, reason, push_id=None):
    knowledge = await lock_knowledge(session, tenant)
    rows = (await session.execute(select(Inquiry, AnalysisRequest, AnalysisDraft).join(
        AnalysisRequest, Inquiry.latest_analysis_request_id == AnalysisRequest.request_id,
    ).join(AnalysisRun, AnalysisRequest.run_id == AnalysisRun.run_id).join(
        AnalysisDraft, AnalysisDraft.run_id == AnalysisRun.run_id,
    ).where(Inquiry.tenant_id == tenant, Inquiry.status != InquiryStatus.CLOSED,
            Inquiry.revision == AnalysisRequest.inquiry_revision,
            AnalysisRun.superseded.is_(False), AnalysisRun.response["apply_status"].astext == "applied",
            ~select(Answer.answer_id).where(Answer.inquiry_id == Inquiry.inquiry_id,
                                          Answer.based_on_message_id == AnalysisRequest.based_on_message_id).exists()))).all()
    pending = list((await session.scalars(select(ReviewItem).where(
        ReviewItem.tenant_id == tenant, ReviewItem.target_type == "draft", ReviewItem.status == "pending",
    ))).all())
    generated = {r.analysis_request_id: r for r in pending if r.analysis_request_id}
    for inquiry, request, draft in rows:
        if not predicate(*references(draft.content)):
            continue
        existing = generated.get(request.request_id)
        if existing:
            docs, code = references(draft.content)
            snapshot = {**existing.snapshot, "docs": existing.snapshot.get("docs", []) + docs,
                        "code": existing.snapshot.get("code", []) + code}
            target, version, text = existing.target_id, existing.target_version, existing.current_text
        else:
            target, version, text = draft.public_id, draft.revision, draft.content["answer_text"]
            docs, code = references(draft.content)
            snapshot = {"request_id": request.request_id, "inquiry_revision": inquiry.revision,
                        "customer_message_id": request.snapshot["based_on_message_id"], "docs": docs, "code": code}
        await record_review(session, tenant=tenant, kind="draft", target=target, version=version,
                            cause=cause, source=source, title=inquiry.title, reason=reason, text=text,
                            snapshot={**snapshot, "knowledge_revision": knowledge.knowledge_revision},
                            inquiry_id=inquiry.inquiry_id, push_id=push_id)
    # 수정안 생성 중에는 원래 초안이 더 이상 latest가 아니다. 같은 미발송 대상을 빠뜨리지 않는다.
    for row in pending:
        if not predicate(row.snapshot.get("docs", []) + row.snapshot.get("suggestion_docs", []),
                         row.snapshot.get("code", []) + row.snapshot.get("suggestion_code", [])):
            continue
        inquiry = await session.get(Inquiry, row.inquiry_id)
        if inquiry.status == InquiryStatus.CLOSED or inquiry.revision != row.snapshot["inquiry_revision"]:
            continue
        await record_review(session, tenant=tenant, kind="draft", target=row.target_id, version=row.target_version,
                            cause=cause, source=source, title=row.title, reason=reason, text=row.current_text,
                            snapshot={**row.snapshot, "knowledge_revision": knowledge.knowledge_revision},
                            inquiry_id=row.inquiry_id, push_id=push_id)


async def capture_document(session, document, version, *, source="doc_version", cause=None, push_id=None):
    knowledge = await lock_knowledge(session, document.tenant_id)
    cause = cause or f"document:{document.doc_id}:{version.version}:{version.source_revision}"
    reason = "근거 문서의 버전 또는 유효 상태가 변경되었습니다."
    if version.version == document.latest_version and version.review_required:
        await record_review(session, tenant=document.tenant_id, kind="document", target=document.ai_source_id,
                            version=version.version, cause=cause, source=source, title=version.title, reason=reason,
                            text=version.extracted_text or version.original_text or "",
                            snapshot={"source_revision": version.source_revision, "knowledge_revision": knowledge.knowledge_revision},
                            document_id=document.doc_id, push_id=push_id)
    await affected_drafts(session, document.tenant_id,
                          lambda docs, code: any(d.get("doc_id") == document.ai_source_id for d in docs),
                          cause=cause, source=source, reason=reason, push_id=push_id)


async def capture_push(session, event, integration):
    paths = {p for change in event.changes if not change["excluded"] for p in (change["path"], change.get("previous_path")) if p}
    if not paths:
        return
    cause = f"push:{event.event_id}"
    bindings = {b["doc_id"] for b in event.document_bindings if any(includes(p, [b["path"]]) for p in paths)}
    for document in (await session.scalars(select(Document).where(
        Document.tenant_id == event.tenant_id, Document.ai_source_id.in_(bindings),
    ))).all():
        version = await session.scalar(select(DocumentVersion).where(
            DocumentVersion.doc_id == document.doc_id, DocumentVersion.version == document.latest_version,
        ))
        await capture_document(session, document, version, source="push", cause=cause, push_id=event.event_id)
    await affected_drafts(session, event.tenant_id,
                          lambda docs, code: any(c.get("path") in paths for c in code),
                          cause=cause, source="push", reason="근거로 연결된 GitHub 경로가 변경되었습니다.", push_id=event.event_id)


async def discover_existing(session, tenant):
    """기존 상태를 멱등 이관한다. 저장된 버전 이외의 과거 변경 원인은 추측하지 않는다."""
    await lock_knowledge(session, tenant)
    rows = (await session.execute(select(Document, DocumentVersion).join(
        DocumentVersion, (DocumentVersion.doc_id == Document.doc_id) & (DocumentVersion.version == Document.latest_version),
    ).where(Document.tenant_id == tenant))).all()
    for document, version in rows:
        cause = f"document:{document.doc_id}:{version.version}:{version.source_revision}"
        if version.review_required or version.status == DocStatus.RETIRED:
            await capture_document(session, document, version, source="existing_state", cause=cause)
        else:
            await affected_drafts(session, tenant,
                lambda docs, code, d=document: any(x.get("doc_id") == d.ai_source_id and x.get("version") != d.latest_version for x in docs),
                cause=cause, source="existing_state", reason="현재 버전이 아닌 문서를 참조한 미발송 초안입니다.")
