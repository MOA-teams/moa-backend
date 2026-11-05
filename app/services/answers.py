"""담당자 승인·채팅 전달·종료·평가. AI 원본과 사람의 최종 승인은 구분한다."""

import asyncio
from difflib import SequenceMatcher

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.core.enums import (
    DocStatus,
    InquiryStatus,
    MessageSender,
    MessageType,
    UserRole,
)
from app.core.errors import APIError
from app.core.rag import RagError
from app.db.models import (
    Answer,
    AnalysisDraft,
    AnalysisRequest,
    AnalysisRun,
    Document,
    DocumentVersion,
    Feedback,
    Inquiry,
    Message,
    User,
)
from app.schemas.answers import AnswerResponse, CloseResponse, FeedbackResponse
from app.services.analysis import current_request, latest_customer, lock_inquiry, utcnow
from app.services.attachments import require_tenant
from app.services.inquiries import begin_idempotent_request
from app.services.knowledge import lock_knowledge


def conflict(code, message):
    raise APIError(409, code, message)


async def begin_action(
    session, user, inquiry_id, payload, key, action, *, customer=False
):
    roles = (UserRole.CUSTOMER,) if customer else (UserRole.AGENT, UserRole.ADMIN)
    if user.role not in roles:
        raise APIError(403, "FORBIDDEN", "허용되지 않은 역할입니다.")
    tenant = require_tenant(user)
    query = select(Inquiry.inquiry_id).where(
        Inquiry.public_id == inquiry_id, Inquiry.tenant_id == tenant
    )
    if customer:
        query = query.where(Inquiry.customer_id == user.internal_user_id)
    internal_id = await session.scalar(query)
    if internal_id is None:
        raise APIError(404, "INQUIRY_NOT_FOUND", "문의를 찾을 수 없습니다.")
    prefix = "" if customer else "/admin"
    record = await begin_idempotent_request(
        session,
        user_id=user.internal_user_id,
        path=f"{prefix}/inquiries/{inquiry_id}/{action}",
        key=key,
        payload=payload,
    )
    inquiry = await lock_inquiry(session, internal_id)
    return inquiry, record


def check_open_revision(inquiry, expected):
    if inquiry.status == InquiryStatus.CLOSED:
        conflict("INQUIRY_CLOSED", "종료된 문의입니다.")
    if inquiry.revision != expected:
        conflict("STALE_INQUIRY_REVISION", "최신 문의를 다시 조회해 주세요.")


async def approved_draft(session, inquiry, payload, customer):
    if payload.draft_id is None:
        return None, None, None
    request = (
        await session.get(
            AnalysisRequest,
            inquiry.latest_analysis_request_id,
            with_for_update=True,
            populate_existing=True,
        )
        if inquiry.latest_analysis_request_id
        else None
    )
    if (
        request is None
        or not current_request(inquiry, request)
        or request.based_on_message_id != customer.message_id
    ):
        conflict("STALE_DRAFT", "최신 문의를 기준으로 초안을 다시 생성해 주세요.")
    run = (
        await session.get(
            AnalysisRun, request.run_id, with_for_update=True, populate_existing=True
        )
        if request.run_id
        else None
    )
    if (
        run is None
        or run.superseded
        or not run.result
        or not run.response
        or run.response.get("apply_status") != "applied"
    ):
        conflict("STALE_DRAFT", "현재 적용된 초안이 없습니다.")
    draft = await session.scalar(
        select(AnalysisDraft)
        .where(
            AnalysisDraft.run_id == run.run_id,
            AnalysisDraft.public_id == payload.draft_id,
        )
        .with_for_update()
    )
    if draft is None or draft.revision != payload.draft_revision:
        conflict("STALE_DRAFT", "초안 revision이 변경되었습니다.")
    allowed = {(x["doc_id"], x["version"]) for x in draft.content["evidence"]["docs"]}
    if not {(x.doc_id, x.version) for x in payload.evidence.docs}.issubset(allowed):
        conflict(
            "EVIDENCE_NOT_RETRIEVED", "초안에서 확인한 문서 근거만 사용할 수 있습니다."
        )
    return draft, request, run


async def verify_evidence(session, inquiry, evidence, rag):
    if evidence.code:
        conflict(
            "CODE_EVIDENCE_UNVERIFIED",
            "코드 근거는 GitHub 최신성 검증 후 사용할 수 있습니다.",
        )
    for external_id, number in sorted({(e.doc_id, e.version) for e in evidence.docs}):
        document = await session.scalar(
            select(Document)
            .where(
                Document.tenant_id == inquiry.tenant_id,
                Document.ai_source_id == external_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if document is None:
            raise APIError(
                404, "DOCUMENT_NOT_FOUND", "문서 또는 버전을 찾을 수 없습니다."
            )
        version = await session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.doc_id == document.doc_id,
                DocumentVersion.version == number,
            )
        )
        if version is None:
            raise APIError(
                404, "DOCUMENT_NOT_FOUND", "문서 또는 버전을 찾을 수 없습니다."
            )
        if (
            document.latest_version != number
            or version.status != DocStatus.VALID
            or version.review_required
            or not version.searchable
        ):
            conflict(
                "STALE_EVIDENCE", "근거 문서의 최신 유효 상태를 다시 확인해 주세요."
            )
        view = await rag.view(inquiry.tenant_id, external_id, number)
        if (
            view.source_revision != version.source_revision
            or view.status != "유효"
            or view.review_required
            or not view.searchable
            or view.indexing_status != "완료"
            or view.error
        ):
            conflict("STALE_EVIDENCE", "AI 검색 상태와 현재 문서가 일치하지 않습니다.")


async def persist_answer(
    session,
    *,
    inquiry,
    customer,
    user,
    payload,
    knowledge_revision,
    draft,
    run,
    automatic_approval=None,
):
    """호출자가 권한·최신성 검사와 잠금을 완료한다. commit은 호출자의 멱등 응답과 함께 한다."""
    automatic = automatic_approval is not None
    if automatic != (user is None):
        raise ValueError("발송 주체와 승인 기록이 일치해야 합니다.")
    duplicates = [Answer.inquiry_revision == inquiry.revision]
    if draft is not None:
        duplicates.append(
            (Answer.draft_id == draft.draft_id)
            & (Answer.based_on_message_id == customer.message_id)
        )
    if automatic:
        duplicates.append(Answer.based_on_message_id == customer.message_id)
    if await session.scalar(
        select(Answer.answer_id)
        .where(
            Answer.inquiry_id == inquiry.inquiry_id,
            or_(*duplicates),
        )
        .limit(1)
    ):
        conflict("ANSWER_ALREADY_SENT", "이미 발송된 답변입니다.")
    now = utcnow()
    mode = "자동" if automatic else "담당자"
    inquiry.last_message_sequence += 1
    message = Message(
        inquiry_id=inquiry.inquiry_id,
        sequence=inquiry.last_message_sequence,
        sender=MessageSender.AI_AUTO if automatic else MessageSender.AGENT,
        message_type=MessageType.FINAL_ANSWER,
        text=payload.answer_text,
        sent_at=now,
    )
    session.add(message)
    await session.flush()
    modified = draft is not None and draft.content["answer_text"] != payload.answer_text
    answer = Answer(
        inquiry_id=inquiry.inquiry_id,
        message_id=message.message_id,
        based_on_message_id=customer.message_id,
        inquiry_revision=inquiry.revision,
        backend_knowledge_revision=knowledge_revision,
        draft_id=draft.draft_id if draft else None,
        draft_revision=draft.revision if draft else None,
        run_id=run.run_id if run else None,
        agent_id=None if automatic else user.internal_user_id,
        delivery_mode=mode,
        answer_text=payload.answer_text,
        evidence=payload.evidence.model_dump(mode="json"),
        original_draft=draft.content if draft else None,
        original_verification=run.result.get("verification") if run else None,
        approval={
            "approved_by": None if automatic else user.user_id,
            "approved_at": now.isoformat(),
            "text_modified": modified,
            "review_reasons": draft.review_reasons if draft else [],
            "approval_type": "자동 응대 정책" if automatic else "담당자 승인",
            **({"automatic_response": automatic_approval} if automatic else {}),
        },
        agent_edit_ratio=(
            round(
                1
                - SequenceMatcher(
                    None,
                    draft.content["answer_text"],
                    payload.answer_text,
                    autojunk=False,
                ).ratio(),
                4,
            )
            if draft and not automatic
            else None
        ),
        answered_at=now,
    )
    session.add(answer)
    inquiry.revision += 1
    inquiry.status = InquiryStatus.ANSWERED
    inquiry.processing_stage = None
    await session.flush()
    return AnswerResponse(
        answer_id=answer.public_id,
        message_id=message.public_id,
        inquiry_id=inquiry.public_id,
        inquiry_revision=inquiry.revision,
        status=inquiry.status,
        delivery_mode=mode,
        agent_id=None if automatic else user.user_id,
        answered_at=now,
    )


async def send_answer(session, *, user, inquiry_id, payload, key, rag):
    try:
        inquiry, record = await begin_action(
            session, user, inquiry_id, payload, key, "answers"
        )
        if record.response:
            response = AnswerResponse.model_validate(record.response)
        else:
            check_open_revision(inquiry, payload.expected_inquiry_revision)
            customer = await latest_customer(session, inquiry)
            if (
                customer is None
                or customer.public_id != payload.expected_last_customer_message_id
            ):
                conflict("STALE_CUSTOMER_MESSAGE", "최신 고객 메시지를 확인해 주세요.")
            draft, request, run = await approved_draft(
                session, inquiry, payload, customer
            )
            knowledge = await lock_knowledge(session, inquiry.tenant_id)
            if (
                knowledge.knowledge_revision
                != payload.expected_backend_knowledge_revision
                or (
                    request is not None
                    and request.backend_knowledge_revision
                    != knowledge.knowledge_revision
                )
            ):
                conflict(
                    "STALE_KNOWLEDGE",
                    "문서가 변경되었습니다. 최신 문서를 확인하고 초안을 재분석해 주세요.",
                )
            try:
                await asyncio.wait_for(
                    verify_evidence(session, inquiry, payload.evidence, rag), timeout=10
                )
            except (RagError, TimeoutError):
                raise APIError(
                    503,
                    "EVIDENCE_CHECK_UNAVAILABLE",
                    "근거 최신성을 확인하지 못했습니다. 같은 요청으로 재시도해 주세요.",
                ) from None
            response = await persist_answer(
                session,
                inquiry=inquiry,
                customer=customer,
                user=user,
                payload=payload,
                knowledge_revision=knowledge.knowledge_revision,
                draft=draft,
                run=run,
            )
            record.response = response.model_dump(mode="json")
        await session.commit()
        return response
    except IntegrityError as exc:
        await session.rollback()
        if getattr(exc.orig, "sqlstate", None) == "23505":
            conflict("ANSWER_ALREADY_SENT", "이미 발송된 답변입니다.")
        raise
    except Exception:
        await session.rollback()
        raise


async def close_inquiry(session, *, user, inquiry_id, payload, key):
    try:
        inquiry, record = await begin_action(
            session, user, inquiry_id, payload, key, "close"
        )
        if record.response:
            response = CloseResponse.model_validate(record.response)
        else:
            check_open_revision(inquiry, payload.expected_inquiry_revision)
            if not await session.scalar(
                select(Answer.answer_id)
                .where(Answer.inquiry_id == inquiry.inquiry_id)
                .limit(1)
            ):
                conflict("ANSWER_REQUIRED", "최종 답변을 보낸 뒤 상담을 종료해 주세요.")
            inquiry.closed_at = inquiry.feedback_requested_at = utcnow()
            inquiry.closed_by = user.internal_user_id
            inquiry.status = InquiryStatus.CLOSED
            inquiry.processing_stage = None
            inquiry.revision += 1
            response = CloseResponse(
                inquiry_id=inquiry.public_id,
                inquiry_revision=inquiry.revision,
                status=inquiry.status,
                resolution_status=inquiry.resolution_status,
                closed_at=inquiry.closed_at,
                feedback_requested=True,
            )
            record.response = response.model_dump(mode="json")
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


def feedback_response(inquiry, feedback, answer_id):
    return FeedbackResponse(
        inquiry_id=inquiry.public_id,
        answer_id=answer_id,
        rating=feedback.rating,
        resolved=feedback.resolved,
        comment=feedback.comment,
        resolution_status=inquiry.resolution_status,
        recorded_at=feedback.recorded_at,
    )


async def submit_feedback(session, *, user, inquiry_id, payload, key):
    try:
        inquiry, record = await begin_action(
            session, user, inquiry_id, payload, key, "feedback", customer=True
        )
        if record.response:
            response = FeedbackResponse.model_validate(record.response)
        else:
            if inquiry.status != InquiryStatus.CLOSED:
                conflict("INQUIRY_NOT_CLOSED", "상담 종료 후 평가할 수 있습니다.")
            if await session.get(Feedback, inquiry.inquiry_id):
                conflict(
                    "FEEDBACK_ALREADY_SUBMITTED",
                    "평가는 최초 한 번만 제출할 수 있습니다.",
                )
            answer = await session.scalar(
                select(Answer)
                .join(Message, Answer.message_id == Message.message_id)
                .where(Answer.inquiry_id == inquiry.inquiry_id)
                .order_by(Message.sequence.desc())
                .limit(1)
            )
            if answer is None:
                conflict("ANSWER_REQUIRED", "평가할 최종 답변이 없습니다.")
            feedback = Feedback(
                inquiry_id=inquiry.inquiry_id,
                answer_id=answer.answer_id,
                customer_id=user.internal_user_id,
                **payload.model_dump(),
                recorded_at=utcnow(),
            )
            session.add(feedback)
            inquiry.resolution_status = (
                "미확인"
                if payload.resolved is None
                else "해결 확인"
                if payload.resolved
                else "미해결"
            )
            response = feedback_response(inquiry, feedback, answer.public_id)
            record.response = response.model_dump(mode="json")
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


async def feedback_state(session, inquiry):
    feedback = await session.get(Feedback, inquiry.inquiry_id)
    answer_id = (
        await session.scalar(
            select(Answer.public_id).where(Answer.answer_id == feedback.answer_id)
        )
        if feedback
        else None
    )
    return {
        "closed_at": inquiry.closed_at,
        "resolution_status": inquiry.resolution_status,
        "feedback_requested": inquiry.feedback_requested_at is not None,
        "feedback_submitted": feedback is not None,
        "feedback": (
            feedback_response(inquiry, feedback, answer_id) if feedback else None
        ),
    }


async def answer_audits(session, inquiry, messages):
    message_ids = [m.message_id for m in messages]
    rows = (
        await session.execute(
            select(Answer, Message.public_id, User.public_id)
            .join(Message, Answer.message_id == Message.message_id)
            .outerjoin(User, Answer.agent_id == User.user_id)
            .where(
                Answer.inquiry_id == inquiry.inquiry_id,
                Message.public_id.in_(message_ids),
            )
            .order_by(Message.sequence)
        )
    ).all()
    return [
        {
            "answer_id": a.public_id,
            "message_id": mid,
            "agent_id": uid,
            **{
                field: getattr(a, field)
                for field in (
                    "answer_text",
                    "evidence",
                    "original_draft",
                    "original_verification",
                    "approval",
                    "agent_edit_ratio",
                    "delivery_mode",
                    "run_id",
                    "draft_revision",
                    "answered_at",
                )
            },
        }
        for a, mid, uid in rows
    ]
