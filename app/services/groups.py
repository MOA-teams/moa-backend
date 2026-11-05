"""그룹 확정과 장애 구간. 모든 그룹 변경은 기업별 advisory lock으로 직렬화한다.

문의 잠금을 이미 보유한 호출자가 사용한다. 기업/User/다른 문의 행을 역순으로
잠그지 않으므로 분석·답변의 기존 잠금 순서를 유지한다. 이 모듈은 commit하지 않는다.
"""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from app.core.enums import InquiryCategory
from app.db.models import Inquiry, InquiryGroup, GroupMembershipHistory, SurgeEpisode
from app.services.alerts import notify

WINDOW = timedelta(minutes=5)
QUIET = timedelta(minutes=10)
THRESHOLD = 8


def utcnow():
    return datetime.now(timezone.utc)


async def lock_groups(session, tenant):
    await session.flush()
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"groups:{tenant}"})


def normalize_code(value):
    value = (value or "").strip().upper().replace("_", "-")
    return value if re.fullmatch(r"E-(?:[A-Z]+-)?[0-9]{1,10}", value) else None


def effective_category(inquiry):
    return (inquiry.category or inquiry.customer_category).value


async def group_public_id(session, inquiry):
    return await session.scalar(select(InquiryGroup.public_id).where(InquiryGroup.group_id == inquiry.group_pk)) if inquiry.group_pk else None


async def recent_members(session, group, now):
    return list((await session.scalars(select(Inquiry).where(
        Inquiry.group_pk == group.group_id, Inquiry.is_seed.is_(False),
        Inquiry.received_at > now - WINDOW, Inquiry.received_at <= now,
    ).order_by(Inquiry.received_at, Inquiry.inquiry_id))).all())


async def evaluate(session, group, now=None):
    """호출자는 기업 그룹 잠금을 보유한다. 수신 시각의 변화만 구간을 연장한다."""
    now = now or utcnow()
    episode = await session.scalar(select(SurgeEpisode).where(SurgeEpisode.group_id == group.group_id, SurgeEpisode.ended_at.is_(None)))
    latest = await session.scalar(select(func.max(Inquiry.received_at)).where(
        Inquiry.group_pk == group.group_id, Inquiry.is_seed.is_(False), Inquiry.received_at <= now))
    # 만료된 구간을 먼저 닫아야 긴 공백 후 들어온 문의가 이전 구간에 붙지 않는다.
    if episode and latest and latest > episode.last_received_at:
        arrivals = list((await session.scalars(select(Inquiry.received_at).where(
            Inquiry.group_pk == group.group_id, Inquiry.is_seed.is_(False),
            Inquiry.received_at > episode.last_received_at, Inquiry.received_at <= now,
        ).order_by(Inquiry.received_at))).all())
        for received in arrivals:
            if received - episode.last_received_at >= QUIET:
                episode.ended_at = episode.last_received_at + QUIET
                break
            episode.last_received_at = received
    if episode and episode.ended_at is None and now - episode.last_received_at >= QUIET:
        episode.ended_at = episode.last_received_at + QUIET
    if episode and episode.ended_at is not None:
        await session.flush()
        episode = None
    members = await recent_members(session, group, now)
    if episode is None and len(members) >= THRESHOLD:
        episode = SurgeEpisode(group_id=group.group_id, started_at=now, last_received_at=max(m.received_at for m in members))
        session.add(episode)
        await session.flush()
        await notify(session, tenant=group.tenant_id, cause=f"surge:{episode.public_id}", kind="문의 급증", group_id=group.public_id)
    if episode and sum(effective_category(m) == InquiryCategory.BUG.value for m in members) >= THRESHOLD:
        from app.services.group_issues import enqueue_issue
        await enqueue_issue(session, group, episode, automatic=True, now=now)
    return episode


async def assign_group(session, inquiry, result=None, *, manual=False):
    await lock_groups(session, inquiry.tenant_id)
    old = inquiry.group_pk
    existing = await session.get(InquiryGroup, old) if old else None
    category = effective_category(inquiry)
    env_code = normalize_code(inquiry.env.get("error_code"))
    ai_code = normalize_code(result.analysis.normalized_error_code) if result and result.analysis else inquiry.normalized_error_code
    conflict = bool(env_code and ai_code and env_code != ai_code)
    code = env_code or ai_code
    group = None
    if conflict:
        await notify(session, tenant=inquiry.tenant_id, cause=f"group:{result.run_id}", kind="검증 확인 필요",
                     inquiry_id=inquiry.public_id, run_id=result.run_id, code="ERROR_CODE_CONFLICT")
    elif code:
        group = await session.scalar(select(InquiryGroup).where(InquiryGroup.tenant_id == inquiry.tenant_id, InquiryGroup.normalized_error_code == code))
    else:
        candidates = []
        similar = result.similar_inquiries if result else None
        if similar:
            for item in similar.items:
                if item.score < .85 or item.inquiry_id == inquiry.public_id:
                    continue
                row = (await session.execute(select(Inquiry, InquiryGroup).join(InquiryGroup, InquiryGroup.group_id == Inquiry.group_pk).where(
                    Inquiry.public_id == item.inquiry_id, Inquiry.tenant_id == inquiry.tenant_id,
                    InquiryGroup.tenant_id == inquiry.tenant_id, InquiryGroup.normalized_error_code.is_(None),
                    InquiryGroup.category == category, Inquiry.normalized_error_code.is_(None),
                ))).first()
                if row:
                    candidate, candidate_group = row
                    if effective_category(candidate) != category or normalize_code(candidate.env.get("error_code")):
                        continue
                    if item.group_id != candidate_group.public_id or (similar.suggested_group_id and similar.suggested_group_id != candidate_group.public_id):
                        continue
                    candidates.append((-item.score, candidate_group.group_id, candidate_group))
        if candidates:
            group = min(candidates, key=lambda v: v[:2])[2]
        elif existing and existing.normalized_error_code is None and existing.category == category:
            group = existing
    if not conflict and group is None:
        group = InquiryGroup(tenant_id=inquiry.tenant_id, normalized_error_code=code, category=category,
                             summary=f"{code} 오류 문의" if code else f"{category} 관련 문의")
        session.add(group)
        await session.flush()
    inquiry.group_pk = group.group_id if group else None
    inquiry.normalized_error_code = code if not conflict else None
    if old != inquiry.group_pk:
        session.add(GroupMembershipHistory(inquiry_id=inquiry.inquiry_id, previous_group_id=old, group_id=inquiry.group_pk,
                                          reason="ERROR_CODE_CONFLICT" if conflict else "MANUAL_CLASSIFICATION" if manual else "AI_RESULT",
                                          run_id=result.run_id if result else None))
    await session.flush()
    from app.services.inquiry_summaries import enqueue_summary
    await enqueue_summary(session, inquiry, group, result)
    for target in sorted({g.group_id: g for g in (existing, group) if g}.values(), key=lambda g:g.group_id):
        await evaluate(session, target)
    return group


async def tick(factory):
    async with factory() as session:
        tenants = list((await session.scalars(select(InquiryGroup.tenant_id).distinct())).all())
    for tenant in tenants:
        async with factory() as session:
            await lock_groups(session, tenant)
            groups = list((await session.scalars(select(InquiryGroup).where(InquiryGroup.tenant_id == tenant).order_by(InquiryGroup.group_id))).all())
            for group in groups:
                await evaluate(session, group)
            await session.commit()
