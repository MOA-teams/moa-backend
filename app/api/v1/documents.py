from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.documentation import documented_errors

from app.core.enums import DocStatus, UserRole
from app.core.rag import RagClient, get_rag_client
from app.deps.auth import get_user, require_roles
from app.deps.db import get_db
from app.schemas.auth import AuthenticatedUser
from app.schemas.documents import DocumentAccepted, DocumentCreateRequest, DocumentListResponse, DocumentStatusRequest, DocumentStatusResponse
from app.services.documents import change_status, create_document, list_documents

router = APIRouter(prefix="/admin/documents", tags=["documents"], dependencies=[Depends(require_roles(UserRole.AGENT, UserRole.ADMIN))])
User = Annotated[AuthenticatedUser, Depends(get_user)]
Session = Annotated[AsyncSession, Depends(get_db)]
Rag = Annotated[RagClient, Depends(get_rag_client)]
DocId = Annotated[int, Path(gt=0, le=9223372036854775807)]
COMMON_ERRORS = ("VALIDATION_ERROR", "INVALID_ACCESS_TOKEN", "FORBIDDEN", "TENANT_REQUIRED", "DOCUMENT_NOT_FOUND")


@router.post("", status_code=202, response_model=DocumentAccepted, summary="문서 원본과 새 버전 등록",
             responses=documented_errors(*COMMON_ERRORS, "IDEMPOTENCY_CONFLICT", "DOCUMENT_ID_EXHAUSTED", "DOCUMENT_VERSION_CONFLICT", "DOCUMENT_SYNC_PENDING", "DOCUMENT_SYNC_FAILED", "ATTACHMENT_NOT_FOUND", "ATTACHMENT_ALREADY_LINKED", bearer_auth=True),
             description="""같은 기업의 agent/admin 전용. 신규 문서는 doc_id를 생략하고 version=1로 등록합니다.
기존 문서는 숫자 doc_id와 다음 version을 지정합니다. text 또는 purpose=document로 업로드한 본인 attachment_id 하나가 필요합니다.
created_at은 원본 작성 시각이며 타임존 필수입니다. uploaded_at은 서버 접수 시각입니다.
Idempotency-Key 필수. 같은 키·본문 재시도는 최초 202 응답을 재현하고 다른 본문은 409입니다.
새 버전의 이전 소스 검색 차단이 확인되지 않으면 원본·작업을 보존하고 503을 반환합니다. 이때 같은 키로 재시도하세요.
추출·인덱싱은 별도 worker가 수행하며 결과는 목록의 indexing_status/indexing_error/sync_error/searchable로 확인합니다.""")
async def create(payload: DocumentCreateRequest, user: User, session: Session, rag: Rag,
                 key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200, pattern=r"^[!-~]+$")]):
    return await create_document(session, user=user, payload=payload, key=key, rag=rag)


@router.get("", response_model=DocumentListResponse, summary="최신 문서 버전과 검색 상태 조회",
            responses=documented_errors(*COMMON_ERRORS, "INVALID_CURSOR", bearer_auth=True),
            description="최신 버전을 먼저 선정한 뒤 status를 적용합니다. 과거 버전으로 자동 복귀하지 않습니다. AI 조회 실패 시 마지막 인덱싱 상태와 sync_error를 보존하고 searchable=false로 반환합니다.")
async def listing(user: User, session: Session, rag: Rag, status: DocStatus | None = None,
                  cursor: Annotated[str | None, Query(max_length=2048)] = None,
                  limit: Annotated[int, Query(ge=1, le=100)] = 20):
    return await list_documents(session, user=user, status=status, cursor=cursor, limit=limit, rag=rag)


@router.patch("/{doc_id}/versions/{version}/status", response_model=DocumentStatusResponse,
              responses=documented_errors(*COMMON_ERRORS, "DOCUMENT_VERSION_CONFLICT", "DOCUMENT_SYNC_PENDING", "DOCUMENT_SYNC_FAILED", bearer_auth=True),
              summary="문서 폐기·재검토 또는 재활성화",
              description="AI 상태 반영을 확인한 뒤 200을 반환합니다. 503이면 같은 본문을 재시도하세요. 이전 버전 활성화는 409입니다. 재활성화 시 재인덱싱하며 완료 전 검색 가능으로 표시하지 않습니다.")
async def status_change(doc_id: DocId, version: Annotated[int, Path(ge=1, le=2147483647)],
                        payload: DocumentStatusRequest, user: User, session: Session, rag: Rag):
    return await change_status(session, user=user, doc_id=doc_id, version_number=version, payload=payload, rag=rag)
