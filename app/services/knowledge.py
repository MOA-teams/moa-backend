"""문서 변경과 답변 승인의 공통 잠금. 문서 행보다 기업 행을 먼저 잠근다."""
from sqlalchemy import select
from app.db.models.tenant import Tenant


async def lock_knowledge(session, tenant_id):
    # 트리거가 증가시킨 값을 ORM identity map의 이전 값으로 읽지 않는다.
    await session.flush()
    return await session.scalar(
        select(Tenant).where(Tenant.tenant_id == tenant_id)
        .with_for_update().execution_options(populate_existing=True)
    )
