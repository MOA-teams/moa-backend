from typing import Annotated
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.inquiries import Cursor, Limit, IdempotencyKey
from app.core.enums import UserRole
from app.deps.auth import get_user, require_roles
from app.deps.db import get_db
from app.schemas.auth import AuthenticatedUser
from app.schemas.groups import AlertID, AlertPage, AlertReadRequest, AlertReadResponse, GroupDetail, GroupID, GroupPage, IssueAccepted, IssueRequest
from app.schemas.common import ErrorResponse
from app.services import alerts, group_queries, group_issues

router = APIRouter(prefix="/admin", tags=["groups-alerts"], dependencies=[Depends(require_roles(UserRole.AGENT, UserRole.ADMIN))],
                   responses={s:{"model":ErrorResponse} for s in (400,401,403,404,409,503)})
User = Annotated[AuthenticatedUser, Depends(get_user)]
DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("/groups", response_model=GroupPage, summary="그룹과 최근 5분 급증 조회",
            description="agent/admin의 본인 기업만 조회합니다. 서로 다른 실시간 문의 8건 이상이면 surging=true입니다. 시드는 제외합니다. 기본 20개·최대 100개이며 ID 내림차순입니다. 필터가 바뀌면 cursor를 비우세요. episode는 10분 무접수까지 유지됩니다.")
async def groups(user:User, session:DB, cursor:Cursor=None, limit:Limit=20, surging:Annotated[bool | None, Query()]=None):
    return await group_queries.list_groups(session, user, cursor, limit, surging)


@router.get("/groups/{group_id}", response_model=GroupDetail, summary="그룹 구성원·장애 구간·Issue 상태 조회",
            description="구성원은 숫자 내부 순서의 내림차순 cursor 페이지입니다. 다른 기업 그룹은 404입니다. issue_creation_status가 대기이고 issue_error_code가 GITHUB_INTEGRATION_NOT_READY이면 GitHub 연결 대기입니다. 확인 필요이면 재생성하지 말고 기존 Issue 유무를 확인해야 합니다. 코드 후보는 8단계 연결 전 빈 배열입니다.")
async def group(group_id:GroupID, user:User, session:DB, cursor:Cursor=None, limit:Limit=20):
    return await group_queries.group_detail(session, user, group_id, cursor, limit)


@router.get("/alerts", response_model=AlertPage, summary="담당자별 미읽음 알림 조회",
            description="알림 내용은 기업 내 공유하고 read_at은 로그인 담당자별로 제공합니다. unread_only 기본 true. ID 내림차순, 기본 20개·최대 100개. cursor는 사용자·기업·필터에 종속됩니다.")
async def alert_list(user:User, session:DB, cursor:Cursor=None, limit:Limit=20, unread_only:bool=True):
    return await alerts.list_alerts(session, user, cursor, limit, unread_only)


@router.patch("/alerts/{alert_id}", response_model=AlertReadResponse, summary="본인 알림 읽음 처리",
              description='본문은 {"read": true}입니다. 반복 요청해도 최초 read_at을 유지하고 다른 담당자의 읽음 상태를 변경하지 않습니다.')
async def alert_read(alert_id:AlertID, payload:AlertReadRequest, user:User, session:DB):
    return await alerts.mark_read(session, user, alert_id)


@router.post("/groups/{group_id}/github-issues", response_model=IssueAccepted, status_code=202, summary="장애 구간의 GitHub Issue 생성 요청",
             description="그룹 조회의 서버 발급 episode_id와 Idempotency-Key를 사용합니다. 자동 요청과 같은 작업을 재사용합니다. 202는 작업 저장 완료이며 생성 완료가 아닙니다. G02/G03으로 결과를 조회하세요. 종료된 구간도 수동 요청 가능하며 고객 원문은 전송하지 않습니다. include_suspected_code는 검증된 GitHub 자료가 있을 때만 적용됩니다.")
async def issue(group_id:GroupID, payload:IssueRequest, user:User, session:DB, idempotency_key:IdempotencyKey):
    return await group_issues.request_issue(session, user, group_id, payload, idempotency_key)
