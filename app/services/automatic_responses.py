"""결과 적용 중 자동 발송 여부를 결정한다. 호출자가 문의 잠금과 commit을 소유한다."""

import asyncio
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
from app.core.errors import APIError
from app.core.ids import IDSpaceExhausted
from app.core.rag import RagError, get_rag_client
from app.db.models import (
    AutomaticResponse,
    GitHubIntegration,
    Document,
    Answer,
    DocumentVersion,
    Message,
)
from app.schemas.answers import AnswerRequest
from app.services.knowledge import lock_knowledge


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
    if request.backend_knowledge_revision is None or result.knowledge_revision is None:
        return "KNOWLEDGE_BASELINE_MISSING", {}
    # 6단계와 동일하게 기업 변경번호를 문서보다 먼저 잠근다.
    # 모든 문서/worker 변경은 이 잠금과 DB 트리거의 변경번호에 참여한다.
    knowledge = await lock_knowledge(session, inquiry.tenant_id)
    if knowledge.knowledge_revision != request.backend_knowledge_revision:
        return "KNOWLEDGE_CHANGED", {}
    # 분석이 GitHub 동기화 중 생성되면 변경번호가 같아도 연결 문서는 아직 최신이 아니다.
    integration = await session.scalar(
        select(GitHubIntegration).where(GitHubIntegration.tenant_id == inquiry.tenant_id)
    )
    if integration and integration.status in {"동기화 중", "동기화 실패"}:
        bound = {b["doc_id"] for b in integration.pending_bindings + integration.document_bindings}
        if any(doc_id in bound for doc_id, _ in refs):
            return "GITHUB_SYNC_PENDING", {}
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


async def persist_question(session, inquiry, text):
    """정보 수집 질문에는 Answer/평가를 만들지 않는다. commit은 결과 적용의 책임이다."""
    message = Message(
        inquiry_id=inquiry.inquiry_id,
        sequence=inquiry.last_message_sequence + 1,
        sender=MessageSender.AI_AUTO,
        message_type=MessageType.INFORMATION_REQUEST,
        text=text,
    )
    session.add(message)
    await session.flush()
    inquiry.last_message_sequence = message.sequence
    inquiry.revision += 1
    inquiry.status = InquiryStatus.NEEDS_INFO
    inquiry.processing_stage = "고객 응답 대기"
    return message.message_id


async def persist_automatic_answer(
    session, inquiry, customer, request, run, result, draft, decision
):
    # 결과 적용 → 자동 처리 → 답변 저장의 import 순환을 함수 경계에서 끊는다.
    from app.services.answers import persist_answer

    response = await persist_answer(
        session,
        inquiry=inquiry,
        customer=customer,
        user=None,
        payload=AnswerRequest(
            draft_id=draft.public_id,
            draft_revision=draft.revision,
            expected_inquiry_revision=inquiry.revision,
            expected_last_customer_message_id=customer.public_id,
            expected_backend_knowledge_revision=request.backend_knowledge_revision,
            answer_text=result.draft.answer_text,
            evidence=result.draft.evidence,
        ),
        knowledge_revision=request.backend_knowledge_revision,
        draft=draft,
        run=run,
        automatic_approval={
            "policy": decision.policy,
            "validation": decision.validation,
        },
    )
    return await session.scalar(
        select(Message.message_id).where(Message.public_id == response.message_id)
    )


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
            "backend_knowledge_revision": request.backend_knowledge_revision,
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
                try:
                    async with asyncio.timeout(10):
                        reason, evidence = await current_evidence(
                            session, inquiry, request, result, policy
                        )
                except TimeoutError:
                    reason, evidence = "EVIDENCE_SYNC_UNAVAILABLE", {}
                decision.validation = {**decision.validation, "documents": evidence}
                if reason is None:
                    kind, text = MessageType.FINAL_ANSWER, result.draft.answer_text
    if reason is None:
        # 예상 가능한 발급/제약 실패만 savepoint로 격리한다. DB 장애는 전체 결과를 롤백해 재시도한다.
        try:
            async with session.begin_nested():
                if kind == MessageType.INFORMATION_REQUEST:
                    message_id = await persist_question(session, inquiry, text)
                else:
                    message_id = await persist_automatic_answer(
                        session,
                        inquiry,
                        customer,
                        request,
                        run,
                        result,
                        draft,
                        decision,
                    )
        except IDSpaceExhausted:
            reason = "ID_SPACE_EXHAUSTED"
            await session.refresh(inquiry)
        except IntegrityError:
            reason = "DELIVERY_CONFLICT"
            await session.refresh(inquiry)
        except APIError as exc:
            if exc.code != "ANSWER_ALREADY_SENT":
                raise
            reason = "CUSTOMER_MESSAGE_ALREADY_RESPONDED"
            await session.refresh(inquiry)
        else:
            decision.delivered_revision = inquiry.revision
            decision.message_id = message_id
            decision.action = (
                "추가 질문 전송"
                if kind == MessageType.INFORMATION_REQUEST
                else "자동 답변 전송"
            )
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
        "answer_id": await session.scalar(
            select(Answer.public_id).where(Answer.message_id == decision.message_id)
        )
        if decision.message_id
        else None,
        "run_id": decision.run_id,
        "delivery_action": decision.action,
        "message_id": message.public_id if message else None,
        "review_reasons": decision.review_reasons,
        "requires_review": bool(decision.review_reasons),
        "delivery_mode": "자동" if message else None,
        "agent_id": None,
    }
