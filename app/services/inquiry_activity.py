from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import UserRole
from app.core.errors import APIError
from app.db.models import Inquiry, InquirySessionEvent, Message
from app.schemas.auth import AuthenticatedUser
from app.schemas.inquiries import CustomerPresence, ReadRequest, ReadResponse, SessionEventRequest, SessionEventResponse
from app.services.attachments import require_tenant


async def lock_inquiry(session: AsyncSession, *, user: AuthenticatedUser, inquiry_id: str) -> Inquiry:
    statement = select(Inquiry).where(
        Inquiry.public_id == inquiry_id, Inquiry.tenant_id == require_tenant(user),
    )
    if user.role == UserRole.CUSTOMER:
        statement = statement.where(Inquiry.customer_id == user.internal_user_id)
    inquiry = await session.scalar(statement.with_for_update().execution_options(populate_existing=True))
    if inquiry is None:
        raise APIError(404, "INQUIRY_NOT_FOUND", "문의를 찾을 수 없습니다.")
    return inquiry


async def latest_session_event(session: AsyncSession, inquiry: Inquiry) -> InquirySessionEvent | None:
    return await session.scalar(select(InquirySessionEvent).where(
        InquirySessionEvent.inquiry_id == inquiry.inquiry_id,
    ).order_by(InquirySessionEvent.event_id.desc()).limit(1))


def customer_presence(event: InquirySessionEvent | None) -> CustomerPresence:
    if event is None or event.type == "returned":
        return CustomerPresence()
    return CustomerPresence(status="나감", left_at=event.created_at, left_reason=event.type)


async def record_session_event(
    session: AsyncSession, *, user: AuthenticatedUser, inquiry_id: str, payload: SessionEventRequest,
) -> SessionEventResponse:
    try:
        inquiry = await lock_inquiry(session, user=user, inquiry_id=inquiry_id)
        event = await latest_session_event(session, inquiry)
        # 새 상담 직후 창 닫힘 알림이 겹쳐도 명시적인 이탈 사유를 덮어쓰지 않는다.
        duplicate = event is not None and (
            event.type == payload.type or (event.type == "new_chat" and payload.type == "left")
        )
        if not duplicate:
            event = InquirySessionEvent(inquiry_id=inquiry.inquiry_id, type=payload.type)
            session.add(event)
            await session.flush()
        response = SessionEventResponse(customer_presence=customer_presence(event))
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


async def read_message_id(session: AsyncSession, inquiry: Inquiry, sequence: int) -> str | None:
    if sequence == 0:
        return None
    return await session.scalar(select(Message.public_id).where(
        Message.inquiry_id == inquiry.inquiry_id, Message.sequence == sequence,
    ))


async def record_read(
    session: AsyncSession, *, user: AuthenticatedUser, inquiry_id: str, payload: ReadRequest,
) -> ReadResponse:
    try:
        inquiry = await lock_inquiry(session, user=user, inquiry_id=inquiry_id)
        message = await session.scalar(select(Message).where(
            Message.public_id == payload.last_read_message_id,
            Message.inquiry_id == inquiry.inquiry_id,
            Message.sequence <= inquiry.last_message_sequence,
        ))
        if message is None:
            raise APIError(404, "MESSAGE_NOT_FOUND", "메시지를 찾을 수 없습니다.")
        field = "customer_last_read_sequence" if user.role == UserRole.CUSTOMER else "agent_last_read_sequence"
        sequence = getattr(inquiry, field)
        if message.sequence > sequence:
            # 읽음은 상담 내용 변경이 아니므로 revision과 updated_at을 유지한다.
            await session.execute(update(Inquiry).where(Inquiry.inquiry_id == inquiry.inquiry_id).values(
                {field: message.sequence, "updated_at": Inquiry.updated_at},
            ))
            last_read_id = message.public_id
        else:
            last_read_id = await read_message_id(session, inquiry, sequence)
        response = ReadResponse(last_read_message_id=last_read_id)
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise
