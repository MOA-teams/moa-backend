import asyncio
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select, update

from app.core.github import GitHubClient
from app.db.models import GitHubIntegration, PushEvent, ReviewItem
from app.services.analysis import utcnow
from app.workers.push_events import run_once
from test_github import admin, github, rag, connect, event, drain
from test_documents import create, drain as drain_documents

pytestmark = pytest.mark.anyio
BASE = "/api/v1/admin"


def history_http(github):
    calls = []
    files = [{"filename": "billing/pricing.ts", "status": "modified", "additions": 1, "deletions": 1,
              "patch": "@@ -1 +1 @@\n-const price = 9900;\n+const price = 12900;"},
             {"filename": "billing/.env", "status": "modified", "patch": "NEVER_EXPOSE_SECRET"},
             {"filename": "labels/answer.json", "status": "modified", "patch": "NEVER_EXPOSE_LABEL"}]

    def commit(sha):
        return {"sha": sha, "commit": {"message": "가격 변경", "author": {"name": "개발자", "date": "2026-11-05T08:00:00Z"}}}

    def handle(req):
        calls.append(req)
        if "/compare/" in req.url.path:
            return httpx.Response(200, json={"status": "ahead", "files": files})
        if req.url.path.endswith("/commits"):
            page = int(req.url.params["page"])
            return httpx.Response(200, json=[commit(f"{i:040x}") for i in range((page - 1) * 100 + 1, min(page * 100, 125) + 1)])
        return httpx.Response(200, json=commit(req.url.path.rsplit("/", 1)[1]))

    client = GitHubClient(SimpleNamespace(), transport=httpx.MockTransport(handle))
    github.request = client.request
    return calls, files


async def test_push_fixed_shas_duplicate_delivery_and_safe_history(client, admin, agent, foreign_agent, github, rag, session_factory):
    await connect(client, admin, github)
    calls, files = history_http(github)
    a, b = await asyncio.gather(*(event(client, before="a" * 40) for _ in range(2)))
    assert a.json() == b.json()
    await event(client, delivery="next", before="b" * 40, after="c" * 40)
    await run_once(session_factory, github)
    await run_once(session_factory, github)
    paths = [r.url.path for r in calls]
    assert any("a" * 40 + "..." + "b" * 40 in p for p in paths)
    assert any("b" * 40 + "..." + "c" * 40 in p for p in paths)
    response = await client.get(BASE + "/push-events?limit=1", headers=agent["headers"])
    assert response.status_code == 200, response.text
    page = response.json()
    item = page["items"][0]
    assert item["status"] == "completed" and item["complete"]
    assert item["summary_kind"] == "change_statistics"
    assert item["changes"][1]["excluded"] and item["changes"][1]["diff"] is None
    assert "NEVER_EXPOSE" not in response.text
    assert page["has_more"]
    end = await client.get(BASE + "/push-events", params={"cursor": page["next_cursor"]}, headers=agent["headers"])
    assert not end.json()["has_more"]
    assert (await client.get(BASE + "/push-events", headers=foreign_agent["headers"])).json()["items"] == []
    assert (await client.get(BASE + "/push-events", params={"cursor": page["next_cursor"]}, headers=foreign_agent["headers"])).status_code == 400
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(PushEvent)) == 2
        integration = await s.scalar(select(GitHubIntegration))
        integration.security_level = 1
        await s.commit()
    hidden = (await client.get(BASE + "/push-events", headers=agent["headers"])).json()
    assert all(c["diff"] is None for item in hidden["items"] for c in item["changes"])


@pytest.mark.parametrize("mode", ["truncated", "rename_excluded", "secret", "missing_base", "compare_failure", "deleted"])
async def test_push_incomplete_security_and_retry(client, admin, agent, github, rag, session_factory, mode):
    await connect(client, admin, github)
    calls, files = history_http(github)
    if mode == "truncated":
        files[0]["additions"] = 50
    elif mode == "rename_excluded":
        files[0]["previous_filename"] = "other/private.ts"
    elif mode == "secret":
        files[0]["patch"] = "+api_key = 'FAKE_KEY_moa_demo_secret'"
    elif mode == "compare_failure":
        original = github.request
        async def fail(method, path, *args, **kwargs):
            if "/compare/" in path:
                from app.core.github import GitHubError
                raise GitHubError()
            return await original(method, path, *args, **kwargs)
        github.request = fail
    await event(client, before="0" * 40 if mode == "missing_base" else "a" * 40,
                **({"after": "0" * 40, "deleted": True} if mode == "deleted" else {}))
    for _ in range(5 if mode == "compare_failure" else 1):
        async with session_factory() as s:
            await s.execute(update(PushEvent).values(available_at=utcnow()))
            await s.commit()
        await run_once(session_factory, github)
    response = await client.get(BASE + "/push-events", headers=agent["headers"])
    item = response.json()["items"][0]
    assert "FAKE_KEY" not in response.text and "NEVER_EXPOSE" not in response.text
    if mode == "rename_excluded":
        assert item["changes"][0]["excluded"] and item["changes"][0]["diff"] is None
    else:
        assert not item["complete"]
        assert item["error_code"]
    if mode == "compare_failure":
        assert item["status"] == "failed"


async def test_recent_commits_fixed_head_and_policy_cursor(client, admin, agent, customer, github, session_factory):
    assert (await client.get(BASE + "/integrations/github/commits", headers=agent["headers"])).status_code == 403
    await connect(client, admin, github)
    calls, _ = history_http(github)
    response = await client.get(BASE + "/integrations/github/commits?limit=100", headers=agent["headers"])
    assert response.status_code == 200, response.text
    page = response.json()
    assert len(page["items"]) == 100 and page["has_more"]
    github.sha = "f" * 40
    end = await client.get(BASE + "/integrations/github/commits", headers=agent["headers"], params={"cursor": page["next_cursor"], "limit": 100})
    assert len(end.json()["items"]) == 25 and not end.json()["has_more"]
    assert all(r.url.params.get("sha") == "a" * 40 for r in calls)
    assert (await client.get(BASE + "/integrations/github/commits", headers=customer["headers"])).status_code == 403
    assert (await client.get(BASE + "/push-events", headers=agent["headers"], params={"cursor": page["next_cursor"]})).status_code == 400
    async with session_factory() as s:
        row = await s.scalar(select(GitHubIntegration))
        row.include_paths = ["sync/"]
        await s.commit()
    assert (await client.get(BASE + "/integrations/github/commits", headers=agent["headers"], params={"cursor": page["next_cursor"]})).status_code == 400


async def test_push_links_document_review_and_repeated_push_invalidates_revision(client, admin, agent, github, rag, session_factory):
    await create(client, agent)
    await drain_documents(session_factory, rag)
    await connect(client, admin, github, document_bindings=[{"path": "billing/", "doc_id": "DOC-001"}])
    await drain(session_factory, github, rag)
    calls, _ = history_http(github)
    for i, sha in enumerate(("b", "c")):
        github.sha = sha * 40
        github.files["billing/pricing.ts"] = (sha * 40, "const price = 12900;")
        await event(client, delivery=f"event-{i}", before="a" * 40, after=sha * 40)
        await drain(session_factory, github, rag)
        await run_once(session_factory, github)
    response = await client.get(BASE + "/push-events", headers=agent["headers"])
    assert all(x["affected_document_ids"] == ["DOC-001"] for x in response.json()["items"])
    from app.workers.reviews import run_once as review_once
    await review_once(session_factory)
    async with session_factory() as s:
        review = await s.scalar(select(ReviewItem))
        assert review.target_type == "document" and review.source == "push" and review.revision == 2
        assert len(review.cause_keys) == 2


@pytest.mark.parametrize("data", [None, {"sha": 123}, {"sha": "a" * 40, "commit": []},
                                  {"sha": "a" * 40, "commit": {"author": "invalid"}}])
async def test_malformed_commit_is_controlled_error(data):
    from app.core.github import GitHubError
    from app.services.push_events import commit_view
    with pytest.raises(GitHubError) as error:
        commit_view(data)
    assert error.value.code == "GITHUB_INVALID_RESPONSE"


async def test_policy_reduction_during_compare_never_persists_old_diff(client, admin, agent, github, session_factory):
    await connect(client, admin, github)
    history_http(github)
    original = github.request
    async def changed(method, path, *args, **kwargs):
        response = await original(method, path, *args, **kwargs)
        if "/compare/" in path:
            async with session_factory() as s:
                row = await s.scalar(select(GitHubIntegration))
                row.security_level = 1
                await s.commit()
        return response
    github.request = changed
    await event(client, before="a" * 40)
    await run_once(session_factory, github)
    async with session_factory() as s:
        event_row = await s.scalar(select(PushEvent))
        assert event_row.changes == [] and event_row.status == "pending"
        assert event_row.error_code == "GITHUB_POLICY_CHANGED"
        event_row.available_at = utcnow()
        await s.commit()
    await run_once(session_factory, github)
    async with session_factory() as s:
        assert all(c["diff"] is None for c in (await s.scalar(select(PushEvent))).changes)
