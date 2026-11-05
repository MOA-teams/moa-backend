"""재검토 이관·수정안 예약 및 결과 동기화. 실제 AI HTTP는 기존 분석 worker가 담당한다."""

from sqlalchemy import select

from app.core.enums import DocStatus, IndexingStatus, InquiryStatus, RunStatus
from app.db.models import AnalysisDraft, AnalysisRequest, AnalysisRun, Document, DocumentVersion, GitHubIntegration, ReviewItem, Tenant
from app.services.analysis import enqueue_analysis, latest_customer, lock_inquiry
from app.services.knowledge import lock_knowledge
from app.services.review_discovery import discover_existing, record_review, references


async def sources_ready(session, row):
    integration = await session.scalar(select(GitHubIntegration).where(GitHubIntegration.tenant_id == row.tenant_id))
    if integration and integration.status in {"동기화 중", "동기화 실패"}:
        return "GITHUB_SYNC_PENDING"
    for external in sorted({d["doc_id"] for d in row.snapshot.get("docs", [])}):
        version = await session.scalar(select(DocumentVersion).join(Document).where(
            Document.tenant_id == row.tenant_id, Document.ai_source_id == external,
            DocumentVersion.version == Document.latest_version,
        ))
        if version is None or version.status != DocStatus.VALID or version.review_required:
            return "SOURCE_REVIEW_REQUIRED"
        if not version.searchable or version.indexing_status != IndexingStatus.COMPLETED or version.sync_error:
            return "DOCUMENT_SYNC_PENDING"
    return None


def unavailable(row, code):
    row.suggestion_status, row.suggestion_error = "failed", code
    row.suggested_text = row.suggested_draft_id = None


async def advance(session, review_id):
    row = await session.get(ReviewItem, review_id)
    if row is None or row.target_type != "draft":
        return
    # 기존 발송과 동일한 순서: inquiry → knowledge → review. worker 중복 실행도 직렬화한다.
    inquiry = await lock_inquiry(session, row.inquiry_id)
    knowledge = await lock_knowledge(session, row.tenant_id)
    row = await session.get(ReviewItem, review_id, with_for_update=True, populate_existing=True)
    if row.status != "pending":
        return
    customer = await latest_customer(session, inquiry)
    if (inquiry.status == InquiryStatus.CLOSED or inquiry.revision != row.snapshot["inquiry_revision"]
            or not customer or customer.public_id != row.snapshot["customer_message_id"]
            or inquiry.latest_analysis_request_id != (row.analysis_request_id or row.snapshot["request_id"])):
        unavailable(row, "STALE_REVIEW_TARGET")
        return
    if row.snapshot["knowledge_revision"] != knowledge.knowledge_revision:
        await record_review(session, tenant=row.tenant_id, kind="draft", target=row.target_id, version=row.target_version,
                            cause=f"knowledge:{knowledge.knowledge_revision}", source=row.source, title=row.title,
                            reason="수정안 생성 이후 지식 상태가 변경되어 다시 확인합니다.", text=row.current_text,
                            snapshot={**row.snapshot, "knowledge_revision": knowledge.knowledge_revision},
                            inquiry_id=row.inquiry_id, push_id=row.push_event_id)
    code = await sources_ready(session, row)
    if code:
        row.suggestion_status, row.suggestion_error = "waiting_for_sources", code
        row.suggested_text = row.suggested_draft_id = None
        return
    if row.analysis_request_id is None:
        request = await enqueue_analysis(session, inquiry, review_only=True)
        row.analysis_request_id = request.request_id
        row.suggestion_status, row.suggestion_error = "processing", None
        return
    request = await session.get(AnalysisRequest, row.analysis_request_id)
    if request.phase in {"failed", "superseded"} or request.run_status == RunStatus.FAILED:
        unavailable(row, request.error_code or "AI_PIPELINE_FAILED")
        return
    if request.phase != "done":
        return
    run = await session.get(AnalysisRun, request.run_id) if request.run_id else None
    if not run or run.superseded or not run.response or run.response.get("apply_status") != "applied":
        unavailable(row, "STALE_REVIEW_TARGET")
        return
    draft = await session.scalar(select(AnalysisDraft).where(AnalysisDraft.run_id == run.run_id))
    if not draft or not draft.content.get("answer_text") or run.result.get("draft_result") != "초안 가능":
        unavailable(row, "REVIEW_SUGGESTION_UNAVAILABLE")
        return
    row.suggested_draft_id, row.suggested_text = draft.draft_id, draft.content["answer_text"]
    row.suggestion_status, row.suggestion_error = "ready", None
    docs, code = references(draft.content)
    row.snapshot = {**row.snapshot, "suggestion_docs": docs, "suggestion_code": code}


async def run_once(factory):
    async with factory() as session:
        tenants = list((await session.scalars(select(Tenant.tenant_id).order_by(Tenant.tenant_id))).all())
    for tenant in tenants:
        async with factory() as session:
            await discover_existing(session, tenant)
            await session.commit()
    async with factory() as session:
        ids = list((await session.scalars(select(ReviewItem.review_id).where(
            ReviewItem.status == "pending", ReviewItem.target_type == "draft",
        ).order_by(ReviewItem.review_id))).all())
    for review_id in ids:
        async with factory() as session:
            await advance(session, review_id)
            await session.commit()
    return bool(ids)
