import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import UploadFile
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.enums import UserRole
from app.core.errors import APIError
from app.core.storage import S3Storage
from app.db.models import Attachment, DocumentVersion, Inquiry, Message
from app.schemas.attachments import AttachmentResponse
from app.schemas.auth import AuthenticatedUser

logger = logging.getLogger(__name__)
MAX_FILE_SIZE = 10 * 1024 * 1024
FILE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "pdf": "application/pdf"}


def require_tenant(user: AuthenticatedUser) -> str:
    if user.tenant_id is None:
        raise APIError(403, "TENANT_REQUIRED", "기업 소속이 필요합니다.")
    return user.tenant_id


def attachment_not_found() -> APIError:
    return APIError(404, "ATTACHMENT_NOT_FOUND", "첨부를 찾을 수 없습니다.")


async def validate_upload(file: UploadFile) -> tuple[str, str, str, int]:
    name = file.filename or ""
    if not name or len(name) > 255 or name.strip() == "" or any(c in name for c in ("/", "\\", "..")) or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise APIError(400, "INVALID_FILE", "파일 이름이 올바르지 않습니다.")
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mime = FILE_TYPES.get(extension)
    if mime is None or file.content_type != mime:
        raise APIError(415, "UNSUPPORTED_FILE_TYPE", "PNG, JPEG, PDF 파일만 업로드할 수 있습니다.")
    size = 0
    signature = b""
    while chunk := await file.read(1024 * 1024):
        if not signature:
            signature = chunk[:8]
        size += len(chunk)
        if size > MAX_FILE_SIZE:
            raise APIError(413, "FILE_TOO_LARGE", "파일은 10MiB 이하여야 합니다.")
    if size == 0:
        raise APIError(400, "INVALID_FILE", "빈 파일은 업로드할 수 없습니다.")
    valid = (
        (mime == "image/png" and signature.startswith(b"\x89PNG\r\n\x1a\n"))
        or (mime == "image/jpeg" and signature.startswith(b"\xff\xd8\xff"))
        or (mime == "application/pdf" and signature.startswith(b"%PDF-"))
    )
    if not valid:
        raise APIError(415, "UNSUPPORTED_FILE_TYPE", "파일 내용과 형식이 일치하지 않습니다.")
    await file.seek(0)
    return name, extension, mime, size


async def signed_attachment(attachment: Attachment, storage: S3Storage) -> AttachmentResponse:
    issued_at = datetime.now(timezone.utc)
    url = await run_in_threadpool(storage.download_url, attachment.storage_key)
    return AttachmentResponse(
        attachment_id=attachment.public_id,
        **{name: getattr(attachment, name) for name in ("file_name", "mime", "size")},
        url=url, expires_at=issued_at + timedelta(seconds=storage.expires_in),
    )


async def upload_attachment(
    session: AsyncSession, *, user: AuthenticatedUser, file: UploadFile,
    storage: S3Storage, request_id: str,
    purpose: str = "inquiry",
) -> AttachmentResponse:
    tenant_id = require_tenant(user)
    allowed = (purpose == "inquiry" and user.role == UserRole.CUSTOMER) or (
        purpose == "document" and user.role in (UserRole.AGENT, UserRole.ADMIN)
    )
    if not allowed:
        raise APIError(403, "FORBIDDEN", "이 용도의 첨부를 업로드할 권한이 없습니다.")
    name, extension, mime, size = await validate_upload(file)
    key = f"attachments/{tenant_id}/{user.internal_user_id}/{uuid4().hex}.{extension}"
    try:
        # UUID key는 이 요청만 소유하므로 실패 시 해당 key만 보상 삭제한다.
        await run_in_threadpool(storage.put, key, file.file, mime, size)
        attachment = Attachment(tenant_id=tenant_id, uploader_id=user.internal_user_id, storage_key=key,
                                file_name=name, mime=mime, size=size, purpose=purpose)
        session.add(attachment)
        await session.flush()
        response = await signed_attachment(attachment, storage)
        await session.commit()
        return response
    except Exception:
        await session.rollback()
        try:
            await run_in_threadpool(storage.delete, key)
        except Exception:
            # URL·원본 파일·AWS 예외를 기록하지 않고 수동 정리에 필요한 key만 남긴다.
            logger.error("attachment_cleanup_failed request_id=%s storage_key=%s", request_id, key)
        raise


async def get_attachment(
    session: AsyncSession, *, user: AuthenticatedUser, attachment_id: str, storage: S3Storage,
) -> AttachmentResponse:
    tenant_id = require_tenant(user)
    statement = select(Attachment).where(Attachment.public_id == attachment_id, Attachment.tenant_id == tenant_id)
    if user.role == UserRole.CUSTOMER:
        statement = statement.where(Attachment.uploader_id == user.internal_user_id, Attachment.purpose == "inquiry")
    else:
        linked_inquiry = exists(select(Message.message_id).join(Inquiry).where(
            Message.message_id == Attachment.message_id, Inquiry.tenant_id == tenant_id,
        ))
        linked_document = exists(select(DocumentVersion.version_id).where(
            DocumentVersion.attachment_id == Attachment.attachment_id,
        ))
        statement = statement.where(or_(
            and_(Attachment.purpose == "inquiry", linked_inquiry),
            and_(Attachment.purpose == "document", or_(Attachment.uploader_id == user.internal_user_id, linked_document)),
        ))
    attachment = await session.scalar(statement)
    if attachment is None:
        raise attachment_not_found()
    return await signed_attachment(attachment, storage)
