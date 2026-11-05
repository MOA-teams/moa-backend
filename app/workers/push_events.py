"""Webhook의 고정 SHA 범위를 수집한다. 최신 인덱싱 작업과 달리 연속 push도 보존한다."""

import asyncio
import logging
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select

from app.core.github import GitHubError
from app.db.models import GitHubIntegration, PushEvent
from app.services.analysis import utcnow
from app.services.knowledge import lock_knowledge
from app.services.push_events import collect_push, matching_policy
from app.services.group_github import policy_hash

logger = logging.getLogger(__name__)


async def run_once(factory, github):
    async with factory() as session:
        event = await session.scalar(select(PushEvent).where(
            PushEvent.status.in_(["pending", "processing"]), PushEvent.available_at <= utcnow(),
            or_(PushEvent.lease_until.is_(None), PushEvent.lease_until < utcnow()),
        ).order_by(PushEvent.event_id).with_for_update(skip_locked=True).limit(1))
        if event is None:
            return False
        event.status, event.lease_token = "processing", uuid4().hex
        event.lease_until = utcnow() + timedelta(seconds=60)
        event.attempts += 1
        event_id, tenant, token = event.event_id, event.tenant_id, event.lease_token
        integration = await session.get(GitHubIntegration, event.integration_id)
        policy = policy_hash(integration) if integration else None
        await session.commit()
        try:
            result = await asyncio.wait_for(collect_push(event, integration, github), 30)
            error = None
        except Exception as exc:
            result = None
            error = exc if isinstance(exc, GitHubError) else GitHubError("GITHUB_HISTORY_UNAVAILABLE", retryable=True)
        await lock_knowledge(session, tenant)
        event = await session.get(PushEvent, event_id, with_for_update=True, populate_existing=True)
        if event.lease_token != token:
            await session.rollback()
            return True
        integration = await session.get(GitHubIntegration, event.integration_id, populate_existing=True)
        if result and not matching_policy(event, integration):
            result, error = None, GitHubError("GITHUB_INTEGRATION_DISABLED", retryable=False)
        elif result and policy != policy_hash(integration):
            # 외부 조회 중 축소된 경로·보안 수준으로 다시 수집한다. 이전 권한의 diff는 저장하지 않는다.
            result, error = None, GitHubError("GITHUB_POLICY_CHANGED", retryable=True)
        event.lease_until = event.lease_token = None
        if result:
            event.commit_info, event.changes, event.complete, event.error_code = result
            event.status = "completed" if event.complete else "incomplete"
            event.summary = f"변경 경로 {len(event.changes)}개 · 제외 {sum(c['excluded'] for c in event.changes)}개"
            from app.services.review_discovery import capture_push
            await capture_push(session, event, integration)
        else:
            event.error_code = error.code
            event.status = "pending" if error.retryable and event.attempts < 5 else "failed"
            event.available_at = utcnow() + timedelta(seconds=max(5 * event.attempts, error.retry_after))
        logger.info("push_history event_id=%s status=%s code=%s", event_id, event.status, event.error_code)
        await session.commit()
        return True
