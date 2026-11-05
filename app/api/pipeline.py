from typing import Annotated
from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.documentation import documented_errors
from app.deps.db import get_db
from app.deps.pipeline import pipeline_tenant
from app.schemas.analysis import PipelineResult, ApplyResponse
from app.services.analysis import apply_result

router = APIRouter(prefix="/internal/v1", tags=["pipeline-internal"])


@router.post(
    "/pipeline-results",
    response_model=ApplyResponse,
    responses=documented_errors(
        "VALIDATION_ERROR",
        "FORBIDDEN",
        "INVALID_SERVICE_TOKEN",
        "CALLBACK_NOT_CONFIGURED",
        "RUN_NOT_REGISTERED",
        "PIPELINE_IDENTITY_MISMATCH",
        "PIPELINE_RESULT_CONFLICT",
        "NON_TERMINAL_RESULT",
        bearer_auth=True,
    ),
    summary="AI 완료·실패 결과 수신",
    description="AI 서비스 전용. tenant·run·revision·기준 메시지를 대조합니다. 중복은 duplicate, 오래된 결과는 ignored_stale, 다른 최종 결과는 409입니다. 서버의 테넌트별 자동 응대 정책과 최신성 검사를 통과한 경우에만 고객 메시지를 저장합니다. 실패·불명확한 결과는 담당자 검토로 전환하며 중복 콜백은 재발송하지 않습니다.",
)
async def callback(
    payload: PipelineResult,
    tenant: Annotated[str, Depends(pipeline_tenant)],
    session: Annotated[AsyncSession, Depends(get_db)],
    key: Annotated[
        str,
        Header(
            alias="Idempotency-Key", min_length=1, max_length=200, pattern=r"^[!-~]+$"
        ),
    ],
):
    # 최종 결과의 멱등 범위는 run이다. 헤더 키를 바꿔도 같은 run을 다시 적용할 수 없다.
    return await apply_result(session, payload, tenant=tenant)
