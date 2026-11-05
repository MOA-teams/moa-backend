import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, text

from app.core.group_issues import HttpIssueGateway, VerifiedIssueTarget
from app.db.models import Alert, Inquiry, InquiryGroup, IssueJob, SurgeEpisode
from app.services.groups import utcnow
from app.workers.group_issues import claim, process, run_once
from test_groups_alerts import add_inquiry

pytestmark = pytest.mark.anyio


class GitHub:
    def __init__(self):
        self.issues = []
        self.posts = 0
        self.gets = 0
        self.mode = None
        self.gateway = HttpIssueGateway(
            {"alpha": VerifiedIssueTarget("example/demo", "fake-token")},
            transport=httpx.MockTransport(self.handle),
        )

    async def handle(self, req):
        assert (
            req.url.host == "api.github.com"
            and req.url.path == "/repos/example/demo/issues"
        )
        assert req.headers["Authorization"] == "Bearer fake-token"
        if req.method == "GET":
            self.gets += 1
            if self.mode == "get_failure":
                return httpx.Response(503, text="private-failure")
            return httpx.Response(200, json=self.issues)
        self.posts += 1
        payload = json.loads(req.content)
        if self.mode == "forbidden":
            return httpx.Response(403, text="private-failure")
        if self.mode == "rate":
            return httpx.Response(429, headers={"retry-after": "10"})
        if self.mode == "connect":
            raise httpx.ConnectError("private-failure")
        if self.mode == "unknown":
            raise httpx.ReadTimeout("private-failure")
        item = {
            **payload,
            "number": 1,
            "html_url": "https://github.com/example/demo/issues/1",
        }
        self.issues.append(item)
        if self.mode == "lost":
            raise httpx.ReadTimeout("private-failure")
        if self.mode == "crash":
            raise RuntimeError("process-like interruption after creation")
        return httpx.Response(201, json=item)


@pytest.fixture
async def episode(customer, session_factory):
    for _ in range(8):
        row, group = await add_inquiry(session_factory, customer)
    async with session_factory() as s:
        episode = await s.scalar(select(SurgeEpisode))
        job = await s.scalar(select(IssueJob))
        return group, episode, job


async def due(factory):
    async with factory() as s:
        await s.execute(
            text(
                "UPDATE issue_jobs SET available_at=now()-interval '1 second', lease_until=now()-interval '1 second'"
            )
        )
        await s.commit()


async def test_unconfigured_is_pending(session_factory, episode):
    assert await run_once(session_factory)
    async with session_factory() as s:
        job = await s.get(IssueJob, episode[2].job_id)
        assert (
            job.status == "대기"
            and job.error_code == "GITHUB_INTEGRATION_NOT_READY"
            and job.attempts == 0
        )


async def test_manual_automatic_dedup_and_idempotency(
    client, agent, foreign_agent, customer, session_factory, episode
):
    group, ep, _ = episode
    url = f"/api/v1/admin/groups/{group.public_id}/github-issues"
    payload = {"episode_id": ep.public_id, "include_suspected_code": True}

    async def send(key):
        return await client.post(
            url, json=payload, headers={**agent["headers"], "Idempotency-Key": key}
        )

    a, b = await asyncio.gather(send("a"), send("b"))
    assert a.status_code == b.status_code == 202 and a.json() == b.json()
    assert (await send("a")).json() == a.json()
    conflict = await client.post(
        url,
        json={**payload, "include_suspected_code": False},
        headers={**agent["headers"], "Idempotency-Key": "a"},
    )
    assert conflict.status_code == 409
    assert (
        await client.post(url, json=payload, headers=agent["headers"])
    ).status_code == 400
    assert (
        await client.post(
            url,
            json=payload,
            headers={**foreign_agent["headers"], "Idempotency-Key": "a"},
        )
    ).status_code == 404
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(IssueJob)) == 1
        assert not (await s.scalar(select(IssueJob))).automatic
    github = GitHub()
    await asyncio.gather(
        run_once(session_factory, github.gateway),
        run_once(session_factory, github.gateway),
    )
    assert github.posts == 1
    assert not await run_once(session_factory, github.gateway)
    detail = (
        await client.get(
            f"/api/v1/admin/groups/{group.public_id}", headers=agent["headers"]
        )
    ).json()
    assert detail["issue_creation_status"] == "완료" and detail["issue_url"].endswith(
        "/issues/1"
    )


@pytest.mark.parametrize("mode", ["lost", "crash"])
async def test_lost_response_and_crash_reconcile_without_post(
    session_factory, episode, mode
):
    github = GitHub()
    github.mode = mode
    await run_once(session_factory, github.gateway)
    await due(session_factory)
    github.mode = None
    await run_once(session_factory, github.gateway)
    assert github.posts == 1
    async with session_factory() as s:
        job = await s.scalar(select(IssueJob))
        assert job.status == "완료" and job.issue_url.endswith("/1")


async def test_unknown_post_is_not_repeated_and_alerted(session_factory, episode):
    github = GitHub()
    github.mode = "unknown"
    await run_once(session_factory, github.gateway)
    await due(session_factory)
    await run_once(session_factory, github.gateway)
    async with session_factory() as s:
        job = await s.scalar(select(IssueJob))
        assert job.status == "확인 필요" and job.error_code == "GITHUB_RESULT_UNKNOWN"
        alert = await s.scalar(
            select(Alert).where(Alert.type == "GitHub 이슈 생성 실패")
        )
        assert alert and "private" not in alert.message
    assert github.posts == 1 and not await run_once(session_factory, github.gateway)


@pytest.mark.parametrize(
    "mode,posts", [("rate", 5), ("connect", 5), ("get_failure", 0), ("forbidden", 1)]
)
async def test_bounded_retries_and_permission_failure(
    session_factory, episode, mode, posts
):
    github = GitHub()
    github.mode = mode
    for _ in range(6):
        await due(session_factory)
        await run_once(session_factory, github.gateway)
    assert github.posts == posts
    async with session_factory() as s:
        job = await s.scalar(select(IssueJob))
        assert job.status == "실패" and job.attempts <= 5
        assert (
            await s.scalar(
                select(func.count())
                .select_from(Alert)
                .where(Alert.type == "GitHub 이슈 생성 실패")
            )
            == 1
        )


async def test_expired_lease_is_fenced(session_factory, episode):
    first = await claim(session_factory)
    await due(session_factory)
    second = await claim(session_factory)
    github = GitHub()
    await process(session_factory, *first, github.gateway)
    assert github.posts == 0
    await process(session_factory, *second, github.gateway)
    assert github.posts == 1


async def test_closed_episode_auto_cancel_then_manual_can_run(
    client, agent, session_factory, episode
):
    group, ep, job = episode
    async with session_factory() as s:
        await s.execute(
            text("UPDATE inquiries SET received_at=now()-interval '20 minutes'")
        )
        row = await s.get(SurgeEpisode, ep.episode_pk)
        row.last_received_at = utcnow() - timedelta(minutes=20)
        await s.commit()
    github = GitHub()
    await run_once(session_factory, github.gateway)
    assert github.posts == 0
    async with session_factory() as s:
        row = await s.get(IssueJob, job.job_id)
        assert row.error_code == "EPISODE_ENDED"
    response = await client.post(
        f"/api/v1/admin/groups/{group.public_id}/github-issues",
        headers={**agent["headers"], "Idempotency-Key": "historical"},
        json={"episode_id": ep.public_id},
    )
    assert response.status_code == 202
    await run_once(session_factory, github.gateway)
    assert github.posts == 1


async def test_find_uses_all_pages_and_ignores_pull_requests():
    marker = "<!-- moa-test -->"
    seen = []

    def handle(req):
        seen.append(dict(req.url.params))
        if req.url.params["page"] == "1":
            return httpx.Response(
                200,
                json=[
                    {"number": i + 1, "body": marker, "pull_request": {}}
                    for i in range(100)
                ],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "number": 101,
                    "body": marker,
                    "html_url": "https://github.com/example/demo/issues/101",
                }
            ],
        )

    gateway = HttpIssueGateway(
        {"alpha": VerifiedIssueTarget("example/demo", "fake")},
        transport=httpx.MockTransport(handle),
    )
    assert (await gateway.find("alpha", marker)).endswith("/101")
    assert len(seen) == 2 and seen[0]["state"] == "all"


async def test_remote_creation_survives_database_save_failure(
    session_factory, episode, db_engine
):
    from sqlalchemy import event

    github = GitHub()

    def fail_result_save(
        connection, cursor, statement, parameters, context, executemany
    ):
        if statement.startswith("UPDATE issue_jobs") and "완료" in parameters:
            raise RuntimeError("simulated database interruption")

    event.listen(db_engine.sync_engine, "before_cursor_execute", fail_result_save)
    try:
        await run_once(session_factory, github.gateway)
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", fail_result_save)
    assert github.posts == 1
    async with session_factory() as s:
        job = await s.scalar(select(IssueJob))
        assert job.phase == "reconcile" and job.issue_url is None
    await due(session_factory)
    await run_once(session_factory, github.gateway)
    assert github.posts == 1
    async with session_factory() as s:
        assert (await s.scalar(select(IssueJob))).status == "완료"


async def test_web_environment_is_kept_but_private_values_are_not():
    from types import SimpleNamespace
    from app.services.group_issues import safe_environment

    values = safe_environment(
        [
            SimpleNamespace(env={"os": "Chrome 142.0.0.0", "app_version": "web"}),
            SimpleNamespace(
                env={
                    "os": "secret@example.com",
                    "app_version": "https://private.example/token",
                }
            ),
        ]
    )
    assert values == ["OS: Chrome 142.0.0.0", "앱 버전: web"]


async def test_rate_limit_delay_and_token_repr(session_factory, episode):
    github = GitHub()
    github.mode = "rate"
    before = utcnow()
    await run_once(session_factory, github.gateway)
    async with session_factory() as s:
        job = await s.scalar(select(IssueJob))
        assert job.available_at >= before + timedelta(seconds=10)
    assert "fake-token" not in repr(github.gateway.targets["alpha"])
