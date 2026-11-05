from typing import Annotated

from fastapi import APIRouter, Depends, File, Path, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.documentation import documented_errors
from app.core.enums import UserRole
from app.core.storage import S3Storage, get_s3_storage
from app.deps.auth import get_user, require_roles
from app.deps.db import get_db
from app.schemas.attachments import AttachmentResponse
from app.schemas.auth import AuthenticatedUser
from app.services.attachments import get_attachment, upload_attachment

router = APIRouter(prefix="/attachments", tags=["attachments"])


@router.post(
    "", status_code=201, response_model=AttachmentResponse,
    dependencies=[Depends(require_roles(UserRole.CUSTOMER))],
    summary="임시 첨부 업로드",
    description="""기업에 소속된 customer가 multipart/form-data의 **file** 필드로 파일 하나를 업로드합니다.
PNG·JPEG·PDF, 파일당 10MiB 이하만 허용하며 확장자·MIME·시그니처를 검사합니다.
성공 응답의 문자열 attachment_id를 문의 생성 또는 메시지 추가의 attachments 배열에 넣으세요.
파일 하나는 메시지 하나에만 연결할 수 있습니다. 임시 첨부는 업로더만 조회·연결할 수 있습니다.
url은 기본 300초 동안 유효한 GET URL이며 만료 시 첨부 조회 API로 다시 발급받으세요.
S3 key나 URL을 요청으로 지정하지 않습니다. 실제 AI 분석은 아직 실행하지 않습니다.""",
    responses=documented_errors("VALIDATION_ERROR", "INVALID_ACCESS_TOKEN", "FORBIDDEN", "TENANT_REQUIRED", "INVALID_FILE", "FILE_TOO_LARGE", "UNSUPPORTED_FILE_TYPE", "STORAGE_UNAVAILABLE", bearer_auth=True),
)
async def upload(
    request: Request,
    file: Annotated[UploadFile, File(description="PNG/JPEG/PDF, 최대 10MiB")],
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    storage: Annotated[S3Storage, Depends(get_s3_storage)],
) -> AttachmentResponse:
    return await upload_attachment(session, user=user, file=file, storage=storage, request_id=request.state.request_id)


@router.get(
    "/{attachment_id}", response_model=AttachmentResponse,
    summary="권한 확인 후 첨부 다운로드 URL 발급",
    description="""고객은 본인이 올린 첨부만, agent/admin은 같은 기업 문의에 연결된 첨부만 조회합니다.
다른 기업·다른 고객의 첨부 또는 담당자가 타인의 임시 첨부에 접근하면 404입니다.
기본 300초의 presigned GET URL을 반환합니다. URL은 만료 전까지 소지자가 사용할 수 있으므로 공유하거나 로그에 기록하지 마세요.
응답은 리다이렉트가 아닌 JSON이며 프론트에서 url로 파일을 다운로드합니다.""",
    responses=documented_errors("VALIDATION_ERROR", "INVALID_ACCESS_TOKEN", "TENANT_REQUIRED", "ATTACHMENT_NOT_FOUND", "STORAGE_UNAVAILABLE", bearer_auth=True),
)
async def download(
    attachment_id: Annotated[str, Path(pattern=r"^ATT-[0-9]{4}$", description="외부 첨부 ID")],
    user: Annotated[AuthenticatedUser, Depends(get_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    storage: Annotated[S3Storage, Depends(get_s3_storage)],
) -> AttachmentResponse:
    return await get_attachment(session, user=user, attachment_id=attachment_id, storage=storage)
