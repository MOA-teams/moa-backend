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


@router.get("/push-events", response_model=PushEventPage, summary="실제 수신한 GitHub push와 영향 대상 조회")
async def pushes(user: User, session: DB, cursor: Cursor = None, limit: Limit = 20):
    return await push_events.list_events(session, user, cursor, limit)


@router.get("/integrations/github/commits", response_model=CommitPage, summary="감시 브랜치의 최근 커밋 조회")
async def commits(user: User, session: DB, cursor: Cursor = None, limit: Limit = 20, github=Depends(get_github_client)):
    return await external(push_events.recent_commits(session, user, github, cursor, limit))


@router.get("/reviews", response_model=ReviewPage, summary="미발송 초안·문서 재검토 목록")
async def review_list(user: User, session: DB, status: Annotated[ReviewStatus | None, Query()] = "pending",
                      cursor: Cursor = None, limit: Limit = 20):
    return await reviews.list_reviews(session, user, status, cursor, limit)


@router.post("/reviews/bulk-approve", response_model=BulkResult, summary="수정안 일괄 승인·발송과 항목별 결과")
async def bulk(payload: BulkApproval, key: IdempotencyKey, user: User, session: DB, rag=Depends(get_rag_client)):
    return await reviews.bulk_approve(session, user, payload, key, rag)


@router.post("/reviews/{review_id}/decisions", response_model=DecisionResult, summary="재검토 승인·직접 수정·보관")
async def decision(review_id: ReviewID, payload: ReviewDecision, key: IdempotencyKey, user: User, session: DB,
                   rag=Depends(get_rag_client)):
    return await reviews.decide(session, user, review_id, payload, key, rag)
