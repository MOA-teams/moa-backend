from datetime import datetime

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cursor import decode_cursor, encode_cursor
from app.core.enums import InquiryCategory, InquiryStatus, InquiryUrgency, MessageSender
from app.core.errors import APIError
from app.db.models import Inquiry, Message, User
from app.schemas.auth import AuthenticatedUser
from app.schemas.inquiries import InquiryDetailResponse, InquiryListResponse, InquirySummary
from app.services.attachments import require_tenant
from app.services.inquiries import inquiry_not_found, message_page


async def list_inquiries(
    session: AsyncSession, *, user: AuthenticatedUser, cursor: str | None, limit: int,
    status: InquiryStatus | None, category: InquiryCategory | None, urgency: InquiryUrgency | None,
) -> InquiryListResponse:
    tenant_id = require_tenant(user)
    scope = {"type": "admin_inquiries", "tenant": tenant_id, "user": user.internal_user_id,
             "status": status.value if status else None, "category": category.value if category else None,
             "urgency": urgency.value if urgency else None}
    position = decode_cursor(cursor, scope)
    statement = select(Inquiry).where(Inquiry.tenant_id == tenant_id)
    for column, value in ((Inquiry.status, status), (Inquiry.category, category), (Inquiry.urgency, urgency)):
        if value is not None:
            statement = statement.where(column == value)
    if position is not None:
        try:
            created_at = datetime.fromisoformat(position["created_at"])
            inquiry_id = position["id"]
            if created_at.utcoffset() is None or type(inquiry_id) is not int or inquiry_id < 1:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise APIError(400, "INVALID_CURSOR", "유효한 조회 cursor가 필요합니다.") from None
        statement = statement.where(tuple_(Inquiry.created_at, Inquiry.inquiry_id) < tuple_(created_at, inquiry_id))
    rows = list((await session.scalars(statement.order_by(Inquiry.created_at.desc(), Inquiry.inquiry_id.desc()).limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more:
        last = rows[-1]
        next_cursor = encode_cursor(scope, {"created_at": last.created_at.isoformat(), "id": last.inquiry_id})
    return InquiryListResponse(items=[InquirySummary.model_validate(row) for row in rows], next_cursor=next_cursor, has_more=has_more)


async def get_inquiry_detail(
    session: AsyncSession, *, user: AuthenticatedUser, inquiry_id: str, cursor: str | None, limit: int,
) -> InquiryDetailResponse:
    tenant_id = require_tenant(user)
    inquiry = await session.scalar(select(Inquiry).where(Inquiry.public_id == inquiry_id, Inquiry.tenant_id == tenant_id))
    if inquiry is None:
        raise inquiry_not_found()
    messages, next_cursor, has_more = await message_page(session, inquiry=inquiry, user=user, cursor=cursor, limit=limit)
    latest_customer_message_id = await session.scalar(select(Message.public_id).where(
        Message.inquiry_id == inquiry.inquiry_id, Message.sender == MessageSender.CUSTOMER,
        Message.sequence <= inquiry.last_message_sequence,
    ).order_by(Message.sequence.desc()).limit(1))
    return InquiryDetailResponse(
        **{field: getattr(inquiry, field) for field in (
            "status", "tenant_id", "source_customer_id", "title", "revision", "customer_category",
            "env", "category", "urgency", "created_at", "updated_at",
        )},
        inquiry_id=inquiry.public_id,
        customer_id=await session.scalar(select(User.public_id).where(User.user_id == inquiry.customer_id)),
        messages=messages, next_cursor=next_cursor, has_more=has_more,
        latest_customer_message_id=latest_customer_message_id,
    )
