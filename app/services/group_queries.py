"""권한 범위 안에서만 그룹·장애 구간을 조회한다. 조회로 상태를 변경하지 않는다."""

from sqlalchemy import func, select

from app.core.cursor import encode_cursor
from app.core.errors import APIError
from app.db.models import Inquiry, InquiryGroup, IssueJob, SurgeEpisode
from app.services.alerts import cursor_id
from app.services.attachments import require_tenant
from app.services.group_github import issue_context, integration_for, enabled
from app.services.github_security import exclusion
from app.services.groups import QUIET, THRESHOLD, WINDOW, utcnow


async def view(session, group, now, count=None):
    if count is None:
        count = await session.scalar(
            select(func.count())
            .select_from(Inquiry)
            .where(
                Inquiry.group_pk == group.group_id,
                Inquiry.is_seed.is_(False),
                Inquiry.received_at > now - WINDOW,
                Inquiry.received_at <= now,
            )
        )
    episode = await session.scalar(
        select(SurgeEpisode)
        .where(SurgeEpisode.group_id == group.group_id)
        .order_by(SurgeEpisode.episode_pk.desc())
        .limit(1)
    )
    job = (
        await session.scalar(
            select(IssueJob).where(IssueJob.episode_pk == episode.episode_pk)
        )
        if episode
        else None
    )
    first, last = (await session.execute(select(func.min(Inquiry.received_at), func.max(Inquiry.received_at)).where(
        Inquiry.group_pk == group.group_id, Inquiry.tenant_id == group.tenant_id,
    ))).one()
    metadata = (job.issue_metadata or {}) if job else {}
    integration = await integration_for(session, group.tenant_id)
    candidates = metadata.get("code_candidates", [])
    candidates = [c for c in candidates if enabled(integration) and not exclusion(
        c["path"], integration.include_paths, integration.exclude_patterns + ["labels/"],
    )] if enabled(integration) else []
    return {
        "group_id": group.public_id,
        "category": group.category,
        "first_received_at": first,
        "last_received_at": last,
        "summary": group.summary,
        "count": count,
        "window_minutes": 5,
        "surging": count >= THRESHOLD,
        "episode_id": episode.public_id if episode else None,
        "episode_ended_at": (
            (
                episode.ended_at
                or (
                    episode.last_received_at + QUIET
                    if now >= episode.last_received_at + QUIET
                    else None
                )
            )
            if episode
            else None
        ),
        "issue_creation_status": job.status if job else None,
        "issue_error_code": job.error_code if job else None,
        "issue_url": job.issue_url if job else None,
        "issue_number": metadata.get("number"),
        "issue_title": metadata.get("title"),
        "issue_created_at": metadata.get("created_at"),
        "created_by": metadata.get("created_by"),
        "issue_code_candidates": candidates,
        "issue_job": {"attempts": job.attempts, "phase": job.phase, "lease_until": job.lease_until,
                      "next_attempt_at": job.available_at if job.phase != "done" else None,
                      "readiness_reason": job.readiness_reason} if job else None,
    }


async def list_groups(session, user, cursor, limit, surging):
    tenant, now = require_tenant(user), utcnow()
    scope = {
        "type": "groups",
        "tenant": tenant,
        "user": user.internal_user_id,
        "surging": surging,
    }
    position = cursor_id(cursor, scope)
    counts = (
        select(Inquiry.group_pk.label("group_pk"), func.count().label("count"))
        .where(
            Inquiry.tenant_id == tenant,
            Inquiry.is_seed.is_(False),
            Inquiry.received_at > now - WINDOW,
            Inquiry.received_at <= now,
        )
        .group_by(Inquiry.group_pk)
        .subquery()
    )
    count = func.coalesce(counts.c.count, 0)
    query = (
        select(InquiryGroup, count)
        .outerjoin(counts, counts.c.group_pk == InquiryGroup.group_id)
        .where(InquiryGroup.tenant_id == tenant)
    )
    if position:
        query = query.where(InquiryGroup.group_id < position)
    if surging is not None:
        query = query.where((count >= THRESHOLD) if surging else (count < THRESHOLD))
    rows = (
        await session.execute(
            query.order_by(InquiryGroup.group_id.desc()).limit(limit + 1)
        )
    ).all()
    more, rows = len(rows) > limit, rows[:limit]
    return {
        "items": [await view(session, g, now, c) for g, c in rows],
        "next_cursor": (
            encode_cursor(scope, {"id": rows[-1][0].group_id}) if more else None
        ),
        "has_more": more,
    }


async def group_detail(session, user, external_id, cursor, limit):
    tenant = require_tenant(user)
    group = await session.scalar(
        select(InquiryGroup).where(
            InquiryGroup.tenant_id == tenant, InquiryGroup.public_id == external_id
        )
    )
    if group is None:
        raise APIError(404, "GROUP_NOT_FOUND", "그룹을 찾을 수 없습니다.")
    scope = {
        "type": "group_members",
        "tenant": tenant,
        "user": user.internal_user_id,
        "group": external_id,
    }
    position = cursor_id(cursor, scope)
    query = select(Inquiry).where(
        Inquiry.group_pk == group.group_id, Inquiry.tenant_id == tenant
    )
    if position:
        query = query.where(Inquiry.inquiry_id < position)
    rows = list(
        (
            await session.scalars(
                query.order_by(Inquiry.inquiry_id.desc()).limit(limit + 1)
            )
        ).all()
    )
    more, rows = len(rows) > limit, rows[:limit]
    _, candidates, _ = await issue_context(session, group)
    return {
        **await view(session, group, utcnow()),
        "inquiry_ids": [m.public_id for m in rows],
        "inquiries": [{"inquiry_id": m.public_id, "title": m.title, "status": m.status,
                       "created_at": m.created_at, "error_code": m.normalized_error_code or m.env.get("error_code"),
                       "os": m.env.get("os")} for m in rows],
        "normalized_error_codes": (
            [group.normalized_error_code] if group.normalized_error_code else []
        ),
        "suspected_code": candidates,
        "next_cursor": (
            encode_cursor(scope, {"id": rows[-1].inquiry_id}) if more else None
        ),
        "has_more": more,
    }
