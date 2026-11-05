import hashlib
import json

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cursor import decode_cursor, encode_cursor
from app.core.enums import InquiryStatus, MessageSender, MessageType
from app.core.errors import APIError
from app.db.models import Attachment, IdempotencyRecord, Inquiry, Message
from app.schemas.attachments import AttachmentMetadata
from app.schemas.auth import AuthenticatedUser
from app.schemas.inquiries import (
    InquiryChatResponse, InquiryCreateRequest, InquiryCreateResponse,
    MessageCreateRequest, MessageCreateResponse, MessageResponse,
)
from app.services.analysis import enqueue_analysis
from app.services.attachments import attachment_not_found, require_tenant
from app.services.inquiry_activity import read_message_id

ACKNOWLEDGEMENT = "접수됐어요. 문의를 확인하고 있어요."


def inquiry_not_found() -> APIError:
    return APIError(404, "INQUIRY_NOT_FOUND", "문의를 찾을 수 없습니다.")


async def begin_idempotent_request(session: AsyncSession, *, user_id: int, path: str, key: str, payload) -> IdempotencyRecord:
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    request_data = payload.model_dump(mode="json")
    # 선택 필드 추가 전 저장한 요청 해시와 호환해 기존 문의 재시도가 409가 되지 않도록 한다.
    if request_data.get("source_customer_id") is None:
        request_data.pop("source_customer_id", None)
    request_hash = hashlib.sha256(json.dumps(request_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    # 같은 키의 INSERT는 선행 트랜잭션 완료를 기다린다. 실패한 요청의 placeholder도 함께 롤백된다.
    await session.execute(insert(IdempotencyRecord).values(
        user_id=user_id, path=path, key_hash=key_hash, request_hash=request_hash, contract_version=2,
    ).on_conflict_do_nothing(constraint="uq_idempotency_user_path_key"))
    record = await session.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.user_id == user_id, IdempotencyRecord.path == path, IdempotencyRecord.key_hash == key_hash,
    ).with_for_update())
    if record.contract_version != 2:
        raise APIError(409, "LEGACY_IDEMPOTENCY_KEY", "이전 API 계약의 요청입니다. 처리 결과를 확인해 주세요.")
    if record.request_hash != request_hash:
        raise APIError(409, "IDEMPOTENCY_CONFLICT", "같은 Idempotency-Key로 다른 요청을 보낼 수 없습니다.")
    return record


async def lock_attachments(session: AsyncSession, *, user: AuthenticatedUser, attachment_ids: list[str]) -> list[Attachment]:
    if not attachment_ids:
        return []
    # 첨부 배열 순서가 다른 요청도 같은 순서로 잠가 교착을 방지한다.
    attachments = list((await session.scalars(select(Attachment).where(
        Attachment.public_id.in_(attachment_ids), Attachment.tenant_id == user.tenant_id,
        Attachment.uploader_id == user.internal_user_id,
        Attachment.purpose == "inquiry",
    ).order_by(Attachment.attachment_id).with_for_update().execution_options(populate_existing=True))).all())
    if len(attachments) != len(attachment_ids):
        raise attachment_not_found()
    if any(attachment.message_id is not None for attachment in attachments):
        raise APIError(409, "ATTACHMENT_ALREADY_LINKED", "이미 메시지에 연결된 첨부입니다.")
    return attachments


async def create_inquiry(session: AsyncSession, *, user: AuthenticatedUser, payload: InquiryCreateRequest, key: str) -> InquiryCreateResponse:
    tenant_id = require_tenant(user)
    try:
        record = await begin_idempotent_request(session, user_id=user.internal_user_id, path="/inquiries", key=key, payload=payload)
        if record.response is not None:
            response = InquiryCreateResponse.model_validate(record.response)
            await session.commit()
            return response
        attachments = await lock_attachments(session, user=user, attachment_ids=payload.attachments)
        inquiry = Inquiry(tenant_id=tenant_id, customer_id=user.internal_user_id, title=payload.message.strip()[:100],
                          source_customer_id=payload.source_customer_id,
                          customer_category=payload.customer_category, env=payload.env.model_dump(),
                          status=InquiryStatus.RECEIVED, revision=1, last_message_sequence=2)
        session.add(inquiry)
        await session.flush()
        first = Message(inquiry_id=inquiry.inquiry_id, sequence=1, sender=MessageSender.CUSTOMER,
                        message_type=MessageType.CUSTOMER_MESSAGE, text=payload.message)
        acknowledgement = Message(inquiry_id=inquiry.inquiry_id, sequence=2, sender=MessageSender.AI_AUTO,
                                  message_type=MessageType.ACKNOWLEDGEMENT, text=ACKNOWLEDGEMENT)
        session.add_all([first, acknowledgement])
        await session.flush()
        for attachment in attachments:
            attachment.message_id = first.message_id
        await enqueue_analysis(session, inquiry)
        response = InquiryCreateResponse(inquiry_id=inquiry.public_id, status=inquiry.status,
                                         auto_message=MessageResponse.model_validate(acknowledgement))
        record.response = response.model_dump(mode="json")
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


async def add_message(session: AsyncSession, *, user: AuthenticatedUser, inquiry_id: str, payload: MessageCreateRequest, key: str) -> MessageCreateResponse:
    tenant_id = require_tenant(user)
    try:
        inquiry = await session.scalar(select(Inquiry).where(
            Inquiry.public_id == inquiry_id, Inquiry.tenant_id == tenant_id, Inquiry.customer_id == user.internal_user_id,
        ).with_for_update().execution_options(populate_existing=True))
        if inquiry is None:
            raise inquiry_not_found()
        record = await begin_idempotent_request(session, user_id=user.internal_user_id, path=f"/inquiries/{inquiry.inquiry_id}/messages", key=key, payload=payload)
        if record.response is not None:
            response = MessageCreateResponse.model_validate(record.response)
            await session.commit()
            return response
        if inquiry.status == InquiryStatus.CLOSED:
            raise APIError(409, "INQUIRY_CLOSED", "종료된 문의에는 메시지를 추가할 수 없습니다. 새 문의를 생성해 주세요.")
        attachments = await lock_attachments(session, user=user, attachment_ids=payload.attachments)
        inquiry.last_message_sequence += 1
        inquiry.revision += 1
        inquiry.status = InquiryStatus.RECEIVED
        message = Message(inquiry_id=inquiry.inquiry_id, sequence=inquiry.last_message_sequence, sender=MessageSender.CUSTOMER,
                          message_type=MessageType.CUSTOMER_MESSAGE, text=payload.text)
        session.add(message)
        await session.flush()
        for attachment in attachments:
            attachment.message_id = message.message_id
        await enqueue_analysis(session, inquiry)
        response = MessageCreateResponse(message_id=message.public_id, inquiry_id=inquiry_id,
                                         inquiry_revision=inquiry.revision, status=inquiry.status)
        record.response = response.model_dump(mode="json")
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        raise


async def message_page(session: AsyncSession, *, inquiry: Inquiry, user: AuthenticatedUser, cursor: str | None, limit: int) -> tuple[list[MessageResponse], str, bool]:
    scope = {"type": "messages", "tenant": inquiry.tenant_id, "inquiry": inquiry.inquiry_id, "user": user.internal_user_id}
    position = decode_cursor(cursor, scope)
    after = 0 if position is None else position.get("after")
    if type(after) is not int or after < 0:
        raise APIError(400, "INVALID_CURSOR", "유효한 조회 cursor가 필요합니다.")
    rows = list((await session.scalars(select(Message).where(
        Message.inquiry_id == inquiry.inquiry_id, Message.sequence > after,
        Message.sequence <= inquiry.last_message_sequence,
    ).order_by(Message.sequence).limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    by_message: dict[int, list[AttachmentMetadata]] = {}
    if rows:
        attachments = (await session.scalars(select(Attachment).where(
            Attachment.message_id.in_([row.message_id for row in rows]), Attachment.tenant_id == inquiry.tenant_id,
        ).order_by(Attachment.attachment_id))).all()
        for attachment in attachments:
            by_message.setdefault(attachment.message_id, []).append(AttachmentMetadata.model_validate(attachment))
    messages = [MessageResponse.model_validate(row).model_copy(update={"attachments": by_message.get(row.message_id, [])}) for row in rows]
    next_cursor = encode_cursor(scope, {"after": rows[-1].sequence if rows else after})
    return messages, next_cursor, has_more


async def get_customer_chat(session: AsyncSession, *, user: AuthenticatedUser, inquiry_id: str, cursor: str | None, limit: int) -> InquiryChatResponse:
    tenant_id = require_tenant(user)
    inquiry = await session.scalar(select(Inquiry).where(
        Inquiry.public_id == inquiry_id, Inquiry.tenant_id == tenant_id, Inquiry.customer_id == user.internal_user_id,
    ))
    if inquiry is None:
        raise inquiry_not_found()
    messages, next_cursor, has_more = await message_page(session, inquiry=inquiry, user=user, cursor=cursor, limit=limit)
    return InquiryChatResponse(
        inquiry_id=inquiry_id, status=inquiry.status, messages=messages, next_cursor=next_cursor, has_more=has_more,
        agent_last_read_message_id=await read_message_id(session, inquiry, inquiry.agent_last_read_sequence),
    )
