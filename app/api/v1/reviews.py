from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.github import external
from app.api.v1.inquiries import Cursor, IdempotencyKey, Limit
from app.core.enums import UserRole
from app.core.github import get_github_client
from app.core.rag import get_rag_client
from app.deps.auth import get_user, require_roles
from app.deps.db import get_db
from app.schemas.auth import AuthenticatedUser
from app.schemas.common import ErrorResponse
from app.schemas.reviews import BulkApproval, BulkResult, CommitPage, DecisionResult, PushEventPage, ReviewDecision, ReviewID, ReviewPage, ReviewStatus
from app.services import push_events, reviews

router = APIRouter(prefix="/admin", tags=["change-reviews"],
                   dependencies=[Depends(require_roles(UserRole.AGENT, UserRole.ADMIN))],
                   responses={s: {"model": ErrorResponse} for s in (400, 401, 403, 404, 409, 503)})
User = Annotated[AuthenticatedUser, Depends(get_user)]
DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("/push-events", response_model=PushEventPage, summary="실제 수신한 GitHub push와 영향 대상 조회",
            description="같은 기업의 agent/admin 전용. 수신 ID 내림차순, 기본 20·최대 100개이며 cursor는 사용자·기업·용도에 묶입니다. 신규 저장 기능 적용 이후 실제 수신한 감시 브랜치 push만 반환합니다. pending/processing은 수집 중, completed는 범위 확인 완료, incomplete는 비교 누락·잘린 diff, failed는 수집 실패입니다. summary_kind=change_statistics는 변경 경로·추가/삭제 수의 요약이며 AI 요약이 아닙니다. 보안 레벨 1은 메타데이터만, 2는 허용 경로의 정제 diff를 제공합니다. 제외 원문은 제공하지 않으며 현재 권한 축소도 반영합니다. 영향 대상은 미발송 초안의 affected_draft_ids와 affected_document_ids입니다.")
async def pushes(user: User, session: DB, cursor: Cursor = None, limit: Limit = 20):
    return await push_events.list_events(session, user, cursor, limit)


@router.get("/integrations/github/commits", response_model=CommitPage, summary="감시 브랜치의 최근 커밋 조회",
            description="현재 연결의 보안 레벨 1 이상이 필요하며 없으면 403입니다. GitHub 감시 브랜치의 첫 조회 head를 cursor에 고정합니다. 기본 20·최대 100개. 새 커밋 확인은 cursor 없이 조회하세요. 사용자·기업·저장소·경로 정책이 바뀐 cursor는 400입니다. 실시간 커밋 메타데이터 조회이며 실제 Webhook 수신 이력과 다릅니다. 커밋별 파일 상세를 수집하지 않으므로 files는 빈 배열입니다. GitHub 접근 실패 시 403 또는 503입니다.")
async def commits(user: User, session: DB, cursor: Cursor = None, limit: Limit = 20, github=Depends(get_github_client)):
    return await external(push_events.recent_commits(session, user, github, cursor, limit))


@router.get("/reviews", response_model=ReviewPage, summary="미발송 초안·문서 재검토 목록",
            description="기본 status=pending. pending/resolved/dismissed별 ID 내림차순 페이지이며 기본 20·최대 100개입니다. 필터·사용자 변경 시 cursor를 비우세요. target_type은 draft/document이며 발송된 답변은 제외합니다. source=existing_state는 과거 원인을 복원할 수 없는 기존 상태 이관입니다. revision을 결정 요청에 그대로 전달하세요. suggestion_status: waiting_for_sources(검색 동기화 대기), processing(AI 처리), ready(수정안 준비), failed(실패·최신성 충돌), manual_required(문서 직접 수정). suggested_text=null이면 승인할 수 없으며 suggestion_error를 확인하세요. 초안은 검색 대상이 아니므로 excluded_from_search=null입니다. 문서는 실제 검색 상태를 반환하므로 resolved여도 재인덱싱 중에는 true일 수 있습니다. 재검토 AI 분석은 review_only=true로 실행됩니다.")
async def review_list(user: User, session: DB, status: Annotated[ReviewStatus | None, Query()] = "pending",
                      cursor: Cursor = None, limit: Limit = 20):
    return await reviews.list_reviews(session, user, status, cursor, limit)


@router.post("/reviews/bulk-approve", response_model=BulkResult, summary="수정안 일괄 승인·발송과 항목별 결과",
             description="Idempotency-Key 필수. items에 review_id와 화면에서 확인한 expected_revision을 최대 50개 전달합니다. 빈 배열·중복 ID는 400입니다. 각 항목을 독립 트랜잭션으로 승인하며 초안은 즉시 고객 발송됩니다. 전체 응답은 200이며 각 항목의 success/status_code/error_code/result를 확인하세요. 실패 항목은 pending으로 남습니다. 같은 키는 저장된 동일 결과를 반환하고 중단된 요청은 완료 항목 이후부터 재개합니다. 새 키로 다시 요청해도 이미 완료된 항목은 REVIEW_ALREADY_DECIDED로 거절하여 재발송하지 않습니다.")
async def bulk(payload: BulkApproval, key: IdempotencyKey, user: User, session: DB, rag=Depends(get_rag_client)):
    return await reviews.bulk_approve(session, user, payload, key, rag)


@router.post("/reviews/{review_id}/decisions", response_model=DecisionResult, summary="재검토 승인·직접 수정·보관",
             description="Idempotency-Key 필수. approve는 준비된 suggested_text를 승인하며 수정안이 없으면 409 REVIEW_SUGGESTION_UNAVAILABLE입니다. edit에는 text가 필수입니다(초안 10,000자/문서 200,000자). 초안의 approve/edit는 기존 문의·고객 메시지·분석·지식 revision과 근거 최신성·내부 코드 노출 검사를 거쳐 고객에게 발송합니다. 수정안이 없을 때 edit는 직접 작성한 수동 답변으로 검사하며 AI 근거가 자동 첨부되지 않습니다. 문서 edit는 원본·첨부를 보존하고 새 버전 및 인덱싱 작업을 저장합니다. 문서의 AI 수정안은 지원하지 않아 직접 수정해야 합니다. dismiss는 발송·검색 복구 없이 보관합니다. expected_revision 불일치는 409 STALE_REVIEW, 대상 변경은 409 STALE_REVIEW_TARGET, 처리 완료는 409 REVIEW_ALREADY_DECIDED, 다른 기업 또는 없는 항목은 404입니다. 성공한 동일 키 재요청은 같은 결과를 반환합니다.")
async def decision(review_id: ReviewID, payload: ReviewDecision, key: IdempotencyKey, user: User, session: DB,
                   rag=Depends(get_rag_client)):
    return await reviews.decide(session, user, review_id, payload, key, rag)
