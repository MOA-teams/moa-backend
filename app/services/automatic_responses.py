"""결과 적용 중 자동 발송 여부를 결정한다. 호출자가 문의 잠금과 commit을 소유한다."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.auto_response import REQUIRED_CHECKS, safe_answer, safe_question
from app.core.config import get_settings
from app.core.enums import (
    CheckStatus,
    DocStatus,
    DraftResult,
    IndexingStatus,
    InquiryStatus,
    InquiryUrgency,
    MessageSender,
    MessageType,
    RunStatus,
)
from app.core.ids import IDSpaceExhausted
from app.core.rag import RagError, get_rag_client
from app.db.models import (
    AutomaticResponse,
    Document,
    DocumentCounter,
    DocumentVersion,
    Message,
)


def document_stamp(document, version):
    return {
        "version": document.latest_version,
        "source_revision": version.source_revision,
        "content_hash": version.content_hash,
        "status": version.status.value,
        "review_required": version.review_required,
        "searchable": version.searchable,
    }


async def capture_knowledge(session, tenant):
    """AI 고정 스냅샷과 분리한 백엔드 비교값. 구형 요청의 null은 자동 발송하지 않는다."""
    rows = await session.execute(
        select(Document, DocumentVersion)
        .join(
            DocumentVersion,
            (DocumentVersion.doc_id == Document.doc_id)
            & (DocumentVersion.version == Document.latest_version),
        )
        .where(Document.tenant_id == tenant)
    )
    return {d.ai_source_id: document_stamp(d, v) for d, v in rows}


async def current_evidence(session, inquiry, request, result, policy):
    """문서 갱신과 같은 잠금 순서로 로컬 원본과 AI 검색 상태를 발송 직전 확인한다."""
    draft = result.draft
    refs = {(r.doc_id, r.version) for r in draft.evidence.docs}
    citations = {(r.doc_id, r.version) for c in draft.citations for r in c.docs}
    if not refs or len(refs) > 10 or not draft.citations or not citations <= refs:
        return "EVIDENCE_INCOMPLETE", {}
    if any(
        not c.docs or not c.text.strip() or c.text not in draft.answer_text
        for c in draft.citations
    ):
        return "CITATION_UNSUPPORTED", {}
    if request.knowledge_snapshot is None or result.knowledge_revision is None:
        return "KNOWLEDGE_BASELINE_MISSING", {}
    # 신규 문서는 counter를 먼저 잠그고 생성된다. 같은 행의 공유 잠금으로
    # 새 문서 삽입을 막고, 모든 기존 문서의 공유 잠금으로 버전/상태 변경을 막는다.
    # 여러 문의의 발송은 공유 잠금을 함께 보유하므로 문서 순서가 달라도 교착하지 않는다.
    counter = await session.scalar(
        select(DocumentCounter)
        .where(
            DocumentCounter.tenant_id == inquiry.tenant_id,
        )
        .with_for_update(read=True)
    )
    if counter is None:
        return "KNOWLEDGE_BASELINE_MISSING", {}
    await session.scalars(
        select(Document)
        .where(
            Document.tenant_id == inquiry.tenant_id,
        )
        .order_by(Document.doc_id)
        .with_for_update(read=True)
    )
    if (
        await capture_knowledge(session, inquiry.tenant_id)
        != request.knowledge_snapshot
    ):
        return "KNOWLEDGE_CHANGED", {}
    checked = {}
    rag = get_rag_client()
    for doc_id, version in sorted(refs):
        if policy.public_documents.get(doc_id) != version:
            return "DOCUMENT_NOT_ALLOWLISTED", checked
        document = await session.scalar(
            select(Document)
            .where(
                Document.tenant_id == inquiry.tenant_id,
                Document.ai_source_id == doc_id,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        source = (
            None
            if document is None
            else await session.scalar(
                select(DocumentVersion)
                .where(
                    DocumentVersion.doc_id == document.doc_id,
                    DocumentVersion.version == version,
                )
                .execution_options(populate_existing=True)
            )
        )
        if (
            source is None
            or document.latest_version != version
            or source.status != DocStatus.VALID
            or source.review_required
            or source.indexing_status != IndexingStatus.COMPLETED
            or not source.searchable
            or source.sync_error
            or source.indexing_error
            or not source.content_hash
        ):
            return "EVIDENCE_NOT_CURRENT", checked
        stamp = document_stamp(document, source)
        if request.knowledge_snapshot.get(doc_id) != stamp:
            return "KNOWLEDGE_CHANGED", checked
        try:
            view = await rag.view(inquiry.tenant_id, doc_id, version)
        except RagError:
            return "EVIDENCE_SYNC_UNAVAILABLE", checked
        if (
            view.source_revision != source.source_revision
            or view.status != "유효"
            or view.indexing_status != "완료"
            or view.review_required
            or not view.searchable
            or view.error
        ):
            return "EVIDENCE_NOT_CURRENT", checked
        checked[doc_id] = stamp
    return None, checked


def answer_block_reason(inquiry, request, result, draft, policy):
    if not policy.answers_enabled:
        return "AUTO_ANSWER_DISABLED"
    if result.draft_result != DraftResult.READY or draft is None:
        return "HUMAN_REVIEW_REQUIRED"
    if (
        result.analysis.category not in policy.categories
        or inquiry.category not in policy.categories
        or result.analysis.urgency == InquiryUrgency.HIGH
        or inquiry.urgency == InquiryUrgency.HIGH
        or result.analysis.normalized_error_code
        or inquiry.env.get("error_code")
    ):
        return "OUTSIDE_AUTO_RESPONSE_SCOPE"
    if result.analysis.missing_fields:
        return "MISSING_INFORMATION"
    verification = result.verification
    if (
        verification is None
        or verification.status != CheckStatus.PASS
        or not REQUIRED_CHECKS <= verification.checks.keys()
        or any(c != CheckStatus.PASS for c in verification.checks.values())
        or verification.unsupported_sentences
    ):
        return "VERIFICATION_NOT_PASSED"
    if (
        result.draft.evidence.code
        or result.draft.style_references
        or draft.review_reasons
    ):
        return "EVIDENCE_REVIEW_REQUIRED"
    if draft.revision != 1 or draft.content != result.draft.model_dump(mode="json"):
        return "DRAFT_CHANGED"
    if not safe_answer(result.draft.answer_text):
        return "UNSAFE_CUSTOMER_CONTENT"
    return None


async def persist_customer_message(session, inquiry, *, text, message_type, sender):
    """문의 잠금 하에서 다음 순번의 고객 메시지를 저장한다. revision·상태·commit은 호출자 책임이다.

    6단계와의 통합 시 수동 발송도 이 저장 경로를 사용해야 한다.
    이 함수만으로 Answer/승인 레코드 또는 중복 검사까지 수행한 것으로 간주하지 않는다.
    """
    message = Message(
        inquiry_id=inquiry.inquiry_id,
        sequence=inquiry.last_message_sequence + 1,
        sender=sender,
        message_type=message_type,
        text=text,
    )
    session.add(message)
    await session.flush()
    return message


async def handle_automatic_response(session, inquiry, request, run, result, draft):
    policy = get_settings().auto_response_policies.get(inquiry.tenant_id)
    decision = AutomaticResponse(
        run_id=run.run_id,
        inquiry_id=inquiry.inquiry_id,
        based_on_message_id=request.based_on_message_id,
        draft_id=draft.draft_id if draft else None,
        action="담당자 검토",
        review_reasons=[],
        policy=policy.model_dump(mode="json") if policy else {},
        validation={
            "inquiry_revision": inquiry.revision,
            "knowledge_revision": result.knowledge_revision,
            "draft_revision": draft.revision if draft else None,
            "delivery_mode": "자동",
            "agent_id": None,
        },
    )
    reason, text, kind = None, None, None
    if request.review_only:
        reason = "REVIEW_ONLY"
    elif result.run_status != RunStatus.COMPLETED:
        reason = "ANALYSIS_FAILED"
    elif policy is None:
        reason = "AUTO_RESPONSE_DISABLED"
    else:
        customer = await session.get(Message, request.based_on_message_id)
        # 같은 기준 메시지의 재분석도 이미 나간 질문/답변 뒤에 다시 발송할 수 없다.
        responded = await session.scalar(
            select(Message.message_id)
            .where(
                Message.inquiry_id == inquiry.inquiry_id,
                Message.sequence > customer.sequence,
                Message.message_type.in_(
                    [MessageType.INFORMATION_REQUEST, MessageType.FINAL_ANSWER]
                ),
            )
            .limit(1)
        )
        if responded:
            reason = "CUSTOMER_MESSAGE_ALREADY_RESPONDED"
        elif result.draft_result == DraftResult.NEED_INFO:
            if not policy.questions_enabled:
                reason = "AUTO_QUESTION_DISABLED"
            else:
                text = safe_question(result.analysis.missing_fields)
                previous = list(
                    (
                        await session.scalars(
                            select(Message.text).where(
                                Message.inquiry_id == inquiry.inquiry_id,
                                Message.message_type == MessageType.INFORMATION_REQUEST,
                            )
                        )
                    ).all()
                )
                if text is None:
                    reason = "QUESTION_FIELD_NOT_ALLOWED"
                elif len(previous) >= policy.max_questions:
                    reason = "QUESTION_LIMIT_REACHED"
                elif any(
                    set(text.splitlines()) & set(p.splitlines()) for p in previous
                ):
                    reason = "REPEATED_QUESTION"
                else:
                    kind = MessageType.INFORMATION_REQUEST
        else:
            reason = answer_block_reason(inquiry, request, result, draft, policy)
            if reason is None:
                reason, evidence = await current_evidence(
                    session, inquiry, request, result, policy
                )
                decision.validation = {**decision.validation, "documents": evidence}
                if reason is None:
                    kind, text = MessageType.FINAL_ANSWER, result.draft.answer_text
    if reason is None:
        # 예상 가능한 발급/제약 실패만 savepoint로 격리한다. DB 장애는 전체 결과를 롤백해 재시도한다.
        try:
            async with session.begin_nested():
                message = await persist_customer_message(
                    session,
                    inquiry,
                    text=text,
                    message_type=kind,
                    sender=MessageSender.AI_AUTO,
                )
        except IDSpaceExhausted:
            reason = "ID_SPACE_EXHAUSTED"
        except IntegrityError:
            reason = "DELIVERY_CONFLICT"
        else:
            inquiry.last_message_sequence = message.sequence
            inquiry.revision += 1
            decision.delivered_revision = inquiry.revision
            decision.message_id = message.message_id
            if kind == MessageType.INFORMATION_REQUEST:
                decision.action = "추가 질문 전송"
                inquiry.status = InquiryStatus.NEEDS_INFO
                inquiry.processing_stage = "고객 응답 대기"
            else:
                decision.action = "자동 답변 전송"
                inquiry.status = InquiryStatus.ANSWERED
                inquiry.processing_stage = "답변 완료"
    if reason:
        decision.review_reasons = [reason]
        inquiry.processing_stage = "검토 대기"
    session.add(decision)
    await session.flush()
    return decision


async def response_view(session, decision):
    if decision is None:
        return None
    message = (
        await session.get(Message, decision.message_id) if decision.message_id else None
    )
    return {
        "run_id": decision.run_id,
        "delivery_action": decision.action,
        "message_id": message.public_id if message else None,
        "review_reasons": decision.review_reasons,
        "requires_review": bool(decision.review_reasons),
        "delivery_mode": "자동" if message else None,
        "agent_id": None,
    }
