"""문의의 최신성·권한·결과 적용을 담당한다. LLM 단계 실행은 위임한다."""

import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import select
from app.core.enums import InquiryStatus, MessageSender, RunStatus, UserRole
from app.core.errors import APIError
from app.db.models import (
    Inquiry,
    Message,
    Attachment,
    AnalysisRequest,
    AnalysisRun,
    AnalysisDraft,
    ClassificationHistory,
    AutomaticResponse,
)
from app.schemas.analysis import (
    AnalysisAccepted,
    ApplyResponse,
    ClassificationResponse,
    DraftView,
    PipelineResult,
)
from app.services.attachments import require_tenant
from app.services.automatic_responses import capture_knowledge, handle_automatic_response, response_view


def utcnow():
    return datetime.now(timezone.utc)


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


async def lock_inquiry(session, internal_id):
    return await session.scalar(
        select(Inquiry)
        .where(Inquiry.inquiry_id == internal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def staff_inquiry(session, user, external_id):
    if user.role not in (UserRole.AGENT, UserRole.ADMIN):
        raise APIError(403, "FORBIDDEN", "담당자 권한이 필요합니다.")
    tenant = require_tenant(user)
    inquiry = await session.scalar(
        select(Inquiry)
        .where(Inquiry.public_id == external_id, Inquiry.tenant_id == tenant)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if inquiry is None:
        raise APIError(404, "INQUIRY_NOT_FOUND", "문의를 찾을 수 없습니다.")
    return inquiry


async def latest_customer(session, inquiry):
    return await session.scalar(
        select(Message)
        .where(
            Message.inquiry_id == inquiry.inquiry_id,
            Message.sender == MessageSender.CUSTOMER,
            Message.sequence <= inquiry.last_message_sequence,
        )
        .order_by(Message.sequence.desc())
        .limit(1)
    )


async def enqueue_analysis(session, inquiry, *, review_only=False):
    """호출자가 문의 잠금을 보유하고 메시지 저장과 함께 commit한다."""
    messages = list(
        (
            await session.scalars(
                select(Message)
                .where(
                    Message.inquiry_id == inquiry.inquiry_id,
                    Message.sequence <= inquiry.last_message_sequence,
                )
                .order_by(Message.sequence)
            )
        ).all()
    )
    customer = next(
        (m for m in reversed(messages) if m.sender == MessageSender.CUSTOMER), None
    )
    if customer is None:
        raise APIError(
            409, "CUSTOMER_MESSAGE_REQUIRED", "분석 기준 고객 메시지가 없습니다."
        )
    files = list(
        (
            await session.execute(
                select(Attachment.message_id, Attachment.public_id)
                .where(
                    Attachment.tenant_id == inquiry.tenant_id,
                    Attachment.message_id.in_([m.message_id for m in messages]),
                )
                .order_by(Attachment.attachment_id)
            )
        ).all()
    )
    by_message = {}
    for mid, aid in files:
        by_message.setdefault(mid, []).append(aid)
    from app.db.models import Answer
    answer_ids = dict((await session.execute(select(Answer.message_id, Answer.public_id).where(Answer.inquiry_id == inquiry.inquiry_id))).all())
    snapshot = {
        "tenant_id": inquiry.tenant_id,
        "inquiry_id": inquiry.public_id,
        "inquiry_revision": inquiry.revision,
        "based_on_message_id": customer.public_id,
        "customer_category": inquiry.customer_category.value,
        "messages": [
            {
                "message_id": m.public_id,
                "sender": m.sender.value,
                "text": m.text,
                "sent_at": m.sent_at.isoformat(),
                "attachments": by_message.get(m.message_id, []),
                **({"answer_id": answer_ids[m.message_id]} if m.message_id in answer_ids else {}),
            }
            for m in messages
        ],
        "env": inquiry.env,
        "review_only": review_only,
    }
    from app.services.knowledge import lock_knowledge
    knowledge = await lock_knowledge(session, inquiry.tenant_id)
    request = AnalysisRequest(
        backend_knowledge_revision=knowledge.knowledge_revision,
        inquiry_id=inquiry.inquiry_id,
        inquiry_revision=inquiry.revision,
        based_on_message_id=customer.message_id,
        snapshot=snapshot,
        knowledge_snapshot=await capture_knowledge(session, inquiry.tenant_id),
        review_only=review_only,
    )
    if len(messages) > 200:
        request.phase, request.run_status, request.error_code = (
            "failed",
            RunStatus.FAILED,
            "ANALYSIS_INPUT_TOO_LARGE",
        )
    session.add(request)
    await session.flush()
    inquiry.latest_analysis_request_id = request.request_id
    inquiry.processing_stage = "검토 대기" if request.phase == "failed" else "대기"
    return request


async def request_analysis(session, *, user, inquiry_id, payload, key):
    from app.services.inquiries import begin_idempotent_request

    try:
        inquiry = await staff_inquiry(session, user, inquiry_id)
        record = await begin_idempotent_request(
            session,
            user_id=user.internal_user_id,
            path=f"/admin/inquiries/{inquiry.inquiry_id}/analysis",
            key=key,
            payload=payload,
        )
        if record.response is not None:
            response = AnalysisAccepted.model_validate(record.response)
        else:
            if inquiry.status == InquiryStatus.CLOSED:
                raise APIError(409, "INQUIRY_CLOSED", "종료된 문의입니다.")
            customer = await latest_customer(session, inquiry)
            if (
                inquiry.revision != payload.expected_inquiry_revision
                or customer is None
                or customer.public_id != payload.based_on_message_id
            ):
                raise APIError(
                    409, "STALE_INQUIRY_REVISION", "최신 문의를 다시 조회해 주세요."
                )
            request = await enqueue_analysis(
                session, inquiry, review_only=payload.review_only
            )
            response = AnalysisAccepted(
                inquiry_id=inquiry.public_id,
                analysis_request_id=request.request_id,
                run_status=request.run_status,
            )
            record.response = response.model_dump(mode="json")
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


async def classify(session, *, user, inquiry_id, payload):
    try:
        inquiry = await staff_inquiry(session, user, inquiry_id)
        if inquiry.status == InquiryStatus.CLOSED:
            raise APIError(409, "INQUIRY_CLOSED", "종료된 문의는 변경할 수 없습니다.")
        if inquiry.revision != payload.expected_revision:
            raise APIError(
                409, "STALE_INQUIRY_REVISION", "최신 문의를 다시 조회해 주세요."
            )
        before = {
            "category": inquiry.category.value if inquiry.category else None,
            "urgency": inquiry.urgency.value if inquiry.urgency else None,
        }
        inquiry.category, inquiry.urgency = payload.category, payload.urgency
        inquiry.classification_manual = True
        inquiry.revision += 1
        inquiry.processing_stage = "검토 대기"
        session.add(
            ClassificationHistory(
                inquiry_id=inquiry.inquiry_id,
                actor_id=user.internal_user_id,
                revision=inquiry.revision,
                before=before,
                after={
                    "category": payload.category.value,
                    "urgency": payload.urgency.value,
                },
                reason=payload.reason,
            )
        )
        response = ClassificationResponse(
            inquiry_id=inquiry.public_id,
            revision=inquiry.revision,
            category=inquiry.category,
            urgency=inquiry.urgency,
        )
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


def current_request(inquiry, request):
    return (
        inquiry.latest_analysis_request_id == request.request_id
        and inquiry.revision == request.inquiry_revision
        and inquiry.status != InquiryStatus.CLOSED
    )


def validate_identity(inquiry, request, result):
    expected = (
        inquiry.tenant_id,
        inquiry.public_id,
        request.inquiry_revision,
        request.snapshot["based_on_message_id"],
    )
    actual = (
        result.tenant_id,
        result.inquiry_id,
        result.inquiry_revision,
        result.based_on_message_id,
    )
    if actual != expected:
        raise APIError(
            409, "PIPELINE_IDENTITY_MISMATCH", "등록된 분석 요청과 일치하지 않습니다."
        )


async def evidence_review(session, inquiry, result):
    if result.draft is None:
        return []
    from app.db.models import Document, DocumentVersion
    from app.core.enums import DocStatus

    from app.services.knowledge import lock_knowledge
    await lock_knowledge(session, inquiry.tenant_id)
    reasons = []
    references = {(e.doc_id, e.version) for e in result.draft.evidence.docs}
    references.update(
        (e.doc_id, e.version) for c in result.draft.citations for e in c.docs
    )
    for external, version in sorted(references):
        document = await session.scalar(
            select(Document)
            .where(
                Document.tenant_id == inquiry.tenant_id,
                Document.ai_source_id == external,
            )
            .with_for_update(read=True)
        )
        source = (
            None
            if document is None
            else await session.scalar(
                select(DocumentVersion).where(
                    DocumentVersion.doc_id == document.doc_id,
                    DocumentVersion.version == version,
                )
            )
        )
        if (
            source is None
            or document.latest_version != version
            or source.status != DocStatus.VALID
            or source.review_required
            or not source.searchable
        ):
            reasons.append(f"유효한 문서 근거 확인 필요: {external} v{version}")
    if result.draft.evidence.code:
        reasons.append("코드 근거 최신성은 GitHub 연동 후 확인해야 합니다.")
    if result.draft.style_references:
        reasons.append("우수 답변 참조의 현재 유효성 확인이 필요합니다.")
    return reasons


async def apply_result(session, result, *, tenant, lease_token=None):
    """콜백과 조회 복구가 사용하는 단일 트랜잭션. inquiry → request → run 순서로 잠근다."""
    try:
        if result.run_status not in (RunStatus.COMPLETED, RunStatus.FAILED):
            raise APIError(
                400, "NON_TERMINAL_RESULT", "완료 또는 실패 결과만 수신합니다."
            )
        if tenant != result.tenant_id:
            raise APIError(403, "FORBIDDEN", "기업 정보가 일치하지 않습니다.")
        registered = await session.get(AnalysisRun, result.run_id)
        if registered is None:
            raise APIError(409, "RUN_NOT_REGISTERED", "실행 등록 후 재시도해 주세요.")
        request = await session.get(AnalysisRequest, registered.request_id)
        inquiry = await lock_inquiry(session, request.inquiry_id)
        request = await session.get(
            AnalysisRequest,
            request.request_id,
            with_for_update=True,
            populate_existing=True,
        )
        run = await session.get(
            AnalysisRun, result.run_id, with_for_update=True, populate_existing=True
        )
        validate_identity(inquiry, request, result)
        if lease_token is not None and request.lease_token != lease_token:
            await session.rollback()
            return None
        raw = result.model_dump(mode="json")
        digest = fingerprint(raw)
        if run.result_hash:
            if run.result_hash != digest:
                raise APIError(
                    409, "PIPELINE_RESULT_CONFLICT", "이미 저장된 최종 결과와 다릅니다."
                )
            response = ApplyResponse.model_validate(
                {**run.response, "apply_status": "duplicate"}
            )
            await session.commit()
            return response
        customer = await latest_customer(session, inquiry)
        stale = (
            not current_request(inquiry, request)
            or run.superseded
            or request.run_id != run.run_id
            or request.phase in ("failed", "superseded")
            or customer is None
            or customer.message_id != request.based_on_message_id
        )
        draft = None
        automatic = None
        if not stale:
            if (
                result.run_status == RunStatus.COMPLETED
                and result.analysis
                and not inquiry.classification_manual
            ):
                inquiry.category, inquiry.urgency = (
                    result.analysis.category,
                    result.analysis.urgency,
                )
            if result.draft:
                draft = AnalysisDraft(
                    run_id=run.run_id,
                    content=result.draft.model_dump(mode="json"),
                    review_reasons=await evidence_review(session, inquiry, result),
                )
                session.add(draft)
                await session.flush()
            if inquiry.status not in (InquiryStatus.ANSWERED, InquiryStatus.NEEDS_INFO):
                inquiry.status = InquiryStatus.IN_REVIEW
            inquiry.processing_stage = "검토 대기"
            request.run_status = result.run_status
            request.current_stage = result.current_stage
            # 외부 오류 원문은 결과 보존에만 사용하고 공개 상태에는 고정 코드를 제공한다.
            request.error_code = "AI_PIPELINE_FAILED" if result.error else None
            request.phase = "done"
            request.lease_token = request.lease_until = None
            automatic = await handle_automatic_response(session, inquiry, request, run, result, draft)
        if stale and lease_token is not None and request.run_id == run.run_id:
            request.phase = "superseded"
            request.lease_token = request.lease_until = None
        run.result, run.result_hash = raw, digest
        response = ApplyResponse(
            run_id=result.run_id,
            apply_status="ignored_stale" if stale else "applied",
            inquiry_id=inquiry.public_id,
            status=inquiry.status.value,
            draft_id=draft.public_id if draft else None,
            delivery_action="없음" if stale else automatic.action,
        )
        run.response = response.model_dump(mode="json")
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


async def detail_analysis(session, inquiry):
    request = (
        await session.get(AnalysisRequest, inquiry.latest_analysis_request_id)
        if inquiry.latest_analysis_request_id
        else None
    )
    if request is None:
        return {}
    run = await session.get(AnalysisRun, request.run_id) if request.run_id else None
    automatic = await session.get(AutomaticResponse, request.run_id) if request.run_id else None
    delivered_current = (
        automatic is not None and automatic.message_id is not None
        and automatic.delivered_revision == inquiry.revision
        and inquiry.latest_analysis_request_id == request.request_id
        and inquiry.status != InquiryStatus.CLOSED
    )
    usable = (
        (current_request(inquiry, request) or delivered_current)
        and run is not None
        and run.result is not None
        and run.response["apply_status"] == "applied"
    )
    result = PipelineResult.model_validate(run.result) if usable else None
    draft = (
        await session.scalar(
            select(AnalysisDraft).where(AnalysisDraft.run_id == run.run_id)
        )
        if usable
        else None
    )
    draft_view = None
    if draft:
        draft_view = DraftView(
            **draft.content,
            draft_id=draft.public_id,
            revision=draft.revision,
            draft_result=result.draft_result,
            review_reasons=draft.review_reasons,
        )
    return {
        "automatic_response": await response_view(session, automatic),
        "analysis_request_id": request.request_id,
        "latest_run_id": request.run_id,
        "run_status": request.run_status,
        "analysis_error": request.error_code,
        "analysis": result.analysis if result else None,
        "additional_question": result.additional_question if result else None,
        "similar_inquiries": result.similar_inquiries if result else None,
        "knowledge_revision": result.knowledge_revision if result else None,
        "draft_result": result.draft_result if result else None,
        "draft": draft_view,
        "verification": result.verification if result else None,
    }
