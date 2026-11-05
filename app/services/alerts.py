"""공유 알림과 개인 읽음 상태. 고객 원문·외부 예외는 안내문에 사용하지 않는다."""

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.core.cursor import decode_cursor, encode_cursor
from app.core.errors import APIError
from app.db.models import Alert, AlertRead
from app.services.attachments import require_tenant

MESSAGES = {
    "검증 확인 필요": "분석 결과의 안전성 또는 근거를 담당자가 확인해 주세요.",
    "분석 실패": "문의 분석을 완료하지 못했습니다. 재분석이 필요합니다.",
    "문의 급증": "최근 5분간 동일 그룹에 문의가 8건 이상 접수됐습니다.",
    "GitHub 이슈 생성 실패": "GitHub 이슈 연결 상태를 확인해 주세요.",
}


async def notify(
    session,
    *,
    tenant,
    cause,
    kind,
    inquiry_id=None,
    group_id=None,
    run_id=None,
    code=None,
):
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"alert:{tenant}:{cause}"},
    )
    alert = await session.scalar(
        select(Alert).where(Alert.tenant_id == tenant, Alert.cause == cause)
    )
    if alert is None:
        alert = Alert(
            tenant_id=tenant,
            cause=cause,
            type=kind,
            inquiry_id=inquiry_id,
            group_id=group_id,
            run_id=run_id,
            error_code=code,
            message=MESSAGES[kind],
        )
        session.add(alert)
        await session.flush()
    return alert


def cursor_id(cursor, scope):
    position = decode_cursor(cursor, scope)
    if position is None:
        return None
    if set(position) != {"id"} or type(position["id"]) is not int or position["id"] < 1:
        raise APIError(400, "INVALID_CURSOR", "유효한 조회 cursor가 필요합니다.")
    return position["id"]


async def list_alerts(session, user, cursor, limit, unread_only):
    tenant = require_tenant(user)
    scope = {
        "type": "alerts",
        "tenant": tenant,
        "user": user.internal_user_id,
        "unread": unread_only,
    }
    position = cursor_id(cursor, scope)
    query = (
        select(Alert, AlertRead.read_at)
        .outerjoin(
            AlertRead,
            (AlertRead.alert_id == Alert.alert_id)
            & (AlertRead.user_id == user.internal_user_id),
        )
        .where(Alert.tenant_id == tenant)
    )
    if unread_only:
        query = query.where(AlertRead.read_at.is_(None))
    if position:
        query = query.where(Alert.alert_id < position)
    rows = (
        await session.execute(query.order_by(Alert.alert_id.desc()).limit(limit + 1))
    ).all()
    more, rows = len(rows) > limit, rows[:limit]
    return {
        "items": [
            {
                "alert_id": a.public_id,
                "type": a.type,
                "inquiry_id": a.inquiry_id,
                "group_id": a.group_id,
                "run_id": a.run_id,
                "message": a.message,
                "error_code": a.error_code,
                "read_at": read,
                "created_at": a.created_at,
            }
            for a, read in rows
        ],
        "next_cursor": (
            encode_cursor(scope, {"id": rows[-1][0].alert_id}) if more else None
        ),
        "has_more": more,
    }


async def mark_read(session, user, external_id):
    tenant = require_tenant(user)
    alert = await session.scalar(
        select(Alert).where(Alert.public_id == external_id, Alert.tenant_id == tenant)
    )
    if alert is None:
        raise APIError(404, "ALERT_NOT_FOUND", "알림을 찾을 수 없습니다.")
    await session.execute(
        insert(AlertRead)
        .values(alert_id=alert.alert_id, user_id=user.internal_user_id)
        .on_conflict_do_nothing()
    )
    read = await session.get(AlertRead, (alert.alert_id, user.internal_user_id))
    result = {"alert_id": alert.public_id, "read_at": read.read_at}
    await session.commit()
    return result
