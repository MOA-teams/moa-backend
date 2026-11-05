import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import null, select

from app.db.models import Inquiry, IssueJob, SurgeEpisode
from app.services.groups import utcnow
from app.workers.group_issues import run_once
from test_group_issues import GitHub, episode, due

pytestmark = pytest.mark.anyio


def with_metadata():
    fake = GitHub()
    original = fake.handle
    async def handle(req):
        try:
            response = await original(req)
        finally:
            for issue in fake.issues:
                issue["created_at"] = "2026-11-05T14:00:00Z"
        if req.method == "POST" and response.status_code == 201:
            return httpx.Response(201, json=fake.issues[-1])
        return response
    fake.gateway.transport = httpx.MockTransport(handle)
    return fake


async def test_group_members_and_all_time_received_bounds(client, agent, foreign_agent, session_factory, episode):
    group, _, _ = episode
    async with session_factory() as s:
        rows = (await s.scalars(select(Inquiry).order_by(Inquiry.inquiry_id))).all()
        rows[0].received_at = utcnow() - timedelta(days=2)
        rows[0].env = {"os": "Android 15", "error_code": "E-502"}
        await s.commit()
        first = rows[0].received_at
    url = f"/api/v1/admin/groups/{group.public_id}"
    page = (await client.get(url, headers=agent["headers"], params={"limit": 3})).json()
    assert page["category"] == "기능 오류" and len(page["inquiries"]) == 3
    assert [x["inquiry_id"] for x in page["inquiries"]] == page["inquiry_ids"]
    assert all(set(x) == {"inquiry_id", "title", "status", "created_at", "error_code", "os"} for x in page["inquiries"])
    assert page["first_received_at"] == first.isoformat().replace("+00:00", "Z")
    assert page["count"] == 7
    assert page["issue_job"]["attempts"] == 0
    assert page["issue_job"]["phase"] == "prepare"
    assert (await client.get(url, headers=foreign_agent["headers"])).status_code == 404


@pytest.mark.parametrize("mode", [None, "lost", "crash"])
async def test_issue_metadata_survives_recovery_and_later_manual_request(client, agent, session_factory, episode, mode):
    group, ep, _ = episode
    fake = with_metadata()
    fake.mode = mode
    await run_once(session_factory, fake.gateway)
    if mode:
        fake.mode = None
        await due(session_factory)
        await run_once(session_factory, fake.gateway)
    assert fake.posts == 1
    response = await client.get(f"/api/v1/admin/groups/{group.public_id}", headers=agent["headers"])
    item = response.json()
    assert item["created_by"] == "자동" and item["issue_number"] == 1
    assert item["issue_title"] and item["issue_created_at"]
    assert item["issue_job"]["lease_until"] is None
    result = await client.post(f"/api/v1/admin/groups/{group.public_id}/github-issues",
        headers={**agent["headers"], "Idempotency-Key": "later-manual"}, json={"episode_id": ep.public_id})
    assert result.status_code == 202, result.text
    async with session_factory() as s:
        job = await s.scalar(select(IssueJob))
        assert job.automatic and job.issue_metadata["created_by"] == "자동"
        # 구버전 완료 행을 모사해도 보강 과정은 GET만 실행한다.
        job.issue_metadata = null()
        await s.commit()
    await due(session_factory)
    await run_once(session_factory, fake.gateway)
    assert fake.posts == 1
    async with session_factory() as s:
        assert (await s.scalar(select(IssueJob))).issue_metadata["number"] == 1


async def test_readiness_reason_does_not_claim_issue_failure(client, agent, session_factory, episode):
    fake = with_metadata()
    fake.gateway.readiness_reason = "GITHUB_CONFIG_MISSING"
    async def not_ready(tenant):
        return False
    fake.gateway.ready = not_ready
    await run_once(session_factory, fake.gateway)
    group = episode[0]
    result = (await client.get(f"/api/v1/admin/groups/{group.public_id}", headers=agent["headers"])).json()
    assert result["issue_creation_status"] == "대기"
    assert result["issue_job"]["readiness_reason"] == "GITHUB_CONFIG_MISSING"
    assert result["issue_job"]["attempts"] == 0 and result["issue_job"]["next_attempt_at"]
    assert fake.posts == 0


async def test_ended_episode_finishes_even_when_github_not_ready(client, agent, session_factory, episode):
    async with session_factory() as s:
        ep = await s.get(SurgeEpisode, episode[1].episode_pk)
        ep.ended_at = utcnow()
        await s.commit()
    fake = with_metadata()
    async def not_ready(tenant):
        raise AssertionError("종료된 구간은 연결 조회도 필요하지 않음")
    fake.gateway.ready = not_ready
    await run_once(session_factory, fake.gateway)
    async with session_factory() as s:
        job = await s.get(IssueJob, episode[2].job_id)
        assert job.status == "실패" and job.error_code == "EPISODE_ENDED"
        assert job.phase == "done" and job.attempts == 0 and job.lease_until is None
    assert fake.posts == 0
