"""Customer-visible progress derived from stored work; no draft or internal reasons."""

from sqlalchemy import select

from app.core.enums import InquiryStatus, MessageSender, MessageType, RunStatus
from app.db.models import AnalysisRequest, Message
from app.schemas.inquiries import ResponseProgress


async def customer_response_progress(session, inquiry):
    customer = await session.scalar(
        select(Message).where(
            Message.inquiry_id == inquiry.inquiry_id,
            Message.sender == MessageSender.CUSTOMER,
        ).order_by(Message.sequence.desc()).limit(1)
    )
    basis = customer.public_id if customer else None

    def state(value):
        return ResponseProgress(state=value, based_on_message_id=basis)

    if inquiry.status == InquiryStatus.CLOSED:
        return state("closed")
    if customer is None:
        return state("idle")
    reply = await session.scalar(
        select(Message).where(
            Message.inquiry_id == inquiry.inquiry_id,
            Message.sequence > customer.sequence,
            Message.sender != MessageSender.CUSTOMER,
            Message.message_type.in_([MessageType.INFORMATION_REQUEST, MessageType.FINAL_ANSWER]),
        ).order_by(Message.sequence.desc()).limit(1)
    )
    if reply is not None:
        return state("awaiting_customer" if reply.message_type == MessageType.INFORMATION_REQUEST else "answered")
    request = await session.get(AnalysisRequest, inquiry.latest_analysis_request_id) if inquiry.latest_analysis_request_id else None
    if (request is not None and request.based_on_message_id == customer.message_id
            and request.phase not in {"failed", "superseded"}
            and request.run_status in {RunStatus.PENDING, RunStatus.RUNNING}):
        return state("analyzing")
    return state("waiting_agent")
