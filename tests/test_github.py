"""외부 토큰 없이 GitHub 설치 인증과 영속 동기화 실패 경로를 검증한다."""

import asyncio
import copy
import hashlib
import hmac
import json
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import func, select, text, update
from pydantic import SecretStr

from app.core.github import GitHubClient, GitHubError, get_github_client
from app.core.rag import GitHubAccepted, GitHubSourceView, RagClient, RagError
from app.db.models import (
    GitHubAuthorization,
    GitHubEvent,
    GitHubIntegration,
    GitHubJob,
    GitHubSource,
    DocumentVersion,
    Tenant,
)
from app.main import app
from app.services.analysis import utcnow
from app.services.github_security import sanitize
from app.workers.github import claim, process, run_once
from test_documents import FakeRag, create as create_document, drain as drain_documents

pytestmark = pytest.mark.anyio
BASE = "/api/v1/admin/integrations/github"
WEBHOOK = "/api/v1/integrations/github/webhook"
REPOSITORY = {
    "installation_id": 11,
    "repository_id": 22,
    "repository": "MOA-demo/syncnote-app",
    "default_branch": "main",
}


class FakeGitHub:
    settings = SimpleNamespace(github_app_slug="test-moa")

    def __init__(self):
        self.sha = "a" * 40
        self.files = {
            "billing/pricing.ts": ("1" * 40, "const price = 9900;"),
            "billing/.env": ("2" * 40, "never-read"),
            "other/private.ts": ("3" * 40, "never-read"),
        }
        self.blobs, self.oauth, self.head_calls = [], [], 0
        self.failure = None
        self.repos = [REPOSITORY]

    def authorization_url(self, state, verifier):
        self.state, self.verifier = state, verifier
        return f"https://github.com/login/oauth/authorize?state={state}"

    async def authorized_repositories(self, code, verifier):
        assert verifier == self.verifier
        self.oauth.append(code)
        return self.repos

    async def installation_token(self, iid, rid):
        assert iid == 11 and rid == 22
        return "test-only-installation-token"

    async def head(self, repo, branch, token):
        self.head_calls += 1
        if self.failure:
            raise self.failure
        return {
            "sha": self.sha,
            "commit": {"tree": {"sha": "tree"}, "message": "가격 수정"},
        }

    async def tree(self, repo, sha, token):
        return [{"type": "tree", "path": "billing", "sha": "tree"}] + [
            {
                "type": "blob",
                "path": p,
                "sha": sha,
                "mode": "100644",
                "size": len(t.encode()),
            }
            for p, (sha, t) in self.files.items()
        ]

    async def branches(self, repo, token):
        return [{"name": "main", "commit": {"sha": self.sha}}]

    async def blob(self, repo, sha, token):
        self.blobs.append(sha)
        return next(t for s, t in self.files.values() if s == sha)


class GitHubRag(FakeRag):
    async def submit_github(self, tenant, payload):
        self.calls.append(("submit_github", copy.deepcopy(payload)))
        key = (tenant, payload["source_id"], payload["version"])
        old = self.sources.get(key)
        if old and old["source_revision"] > payload["source_revision"]:
            raise RagError("STALE_SOURCE_REVISION", retryable=False)
        if (
            old
            and old["source_revision"] == payload["source_revision"]
            and old != payload
        ):
            raise RagError("SOURCE_CONFLICT", retryable=False)
        self.sources[key] = copy.deepcopy(payload)
        if self.lose_submit:
            self.lose_submit = False
            raise RagError()
        return GitHubAccepted(
            source_id=payload["source_id"],
            version=payload["version"],
            indexing_status="대기",
        )

    async def view_github(self, tenant, payload):
        if self.fail_view:
            raise RagError()
        s = self.sources.get((tenant, payload["source_id"], payload["version"]))
        if not s:
            raise RagError("SOURCE_NOT_FOUND", retryable=False, status=404)
        return GitHubSourceView(
            source_id=s["source_id"],
            version=s["version"],
            source_revision=s["source_revision"],
            status=s["status"],
            review_required=s["review_required"],
            indexing_status="완료",
            searchable=self.searchable
            and s["status"] == "유효"
            and not s["review_required"],
        )


@pytest.fixture
async def admin(make_actor):
    return await make_actor("admin", "admin")


@pytest.fixture
def github(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-tests-only")
    get_settings.cache_clear()
    fake = FakeGitHub()
    app.dependency_overrides[get_github_client] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_github_client, None)


@pytest.fixture
def rag():
    from app.core.rag import get_rag_client

    fake = GitHubRag()
    app.dependency_overrides[get_rag_client] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_rag_client, None)


async def authorize(client, admin, github):
    result = await client.post(BASE + "/authorization", headers=admin["headers"])
    assert result.status_code == 200, result.text
    state = parse_qs(urlsplit(result.json()["authorization_url"]).query)["state"][0]
    result = await client.post(
        BASE + "/authorization/complete",
        headers=admin["headers"],
        json={"state": state, "code": "temporary-test-code"},
    )
    assert result.status_code == 200, result.text
    return result.json()["authorization_id"]


async def connect(client, admin, github, *, key="github-connect", **changes):
    proof = await authorize(client, admin, github)
    body = {
        **REPOSITORY,
        "authorization_id": proof,
        "security_level": 2,
        "include_paths": ["billing/"],
    }
    body.pop("repository_id")
    body.pop("default_branch")
    body.update(changes)
    result = await client.post(
        BASE, headers={**admin["headers"], "Idempotency-Key": key}, json=body
    )
    assert result.status_code == 202, result.text
    return body


async def event(client, *, delivery="push-1", kind="push", signature=None, **changes):
    body = {
        "installation": {"id": 11},
        "repository": {"id": 22},
        "ref": "refs/heads/main",
        "after": "b" * 40,
        **changes,
    }
    raw = json.dumps(body).encode()
    signature = (
        signature
        or "sha256=" + hmac.new(b"webhook-tests-only", raw, hashlib.sha256).hexdigest()
    )
    return await client.post(
        WEBHOOK,
        content=raw,
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Delivery": delivery,
            "X-GitHub-Event": kind,
        },
    )


async def step(factory, github, rag):
    async with factory() as session:
        await session.execute(update(GitHubJob).values(available_at=utcnow()))
        await session.commit()
    return await run_once(factory, github, rag)


async def drain(factory, github, rag):
    for _ in range(80):
        if not await step(factory, github, rag):
            return
    raise AssertionError("동기화 작업이 끝나지 않았습니다.")


async def test_install_authorization_role_owner_expiry_and_one_time(
    client, admin, agent, foreign_agent, github, session_factory
):
    assert (
        await client.post(BASE + "/authorization", headers=agent["headers"])
    ).status_code == 403
    await client.post(BASE + "/authorization", headers=admin["headers"])
    state = github.state
    body = {"state": state, "code": "test"}
    assert (
        await client.post(
            BASE + "/authorization/complete",
            headers=foreign_agent["headers"],
            json=body,
        )
    ).status_code == 403
    first = await client.post(
        BASE + "/authorization/complete", headers=admin["headers"], json=body
    )
    assert first.status_code == 200
    assert (
        await client.post(
            BASE + "/authorization/complete", headers=admin["headers"], json=body
        )
    ).status_code == 400
    assert github.oauth == ["test"]
    selection = {"authorization_id": state, "repository_id": 22}
    assert (
        await client.post(BASE + "/selection", headers=admin["headers"], json=selection)
    ).json()["branches"][0]["name"] == "main"
    assert (
        await client.post(
            BASE + "/selection",
            headers=admin["headers"],
            json={**selection, "branch": "main"},
        )
    ).json()["paths"] == ["billing"]
    assert (
        await client.post(
            BASE + "/selection",
            headers=admin["headers"],
            json={**selection, "repository_id": 23},
        )
    ).status_code == 404
    async with session_factory() as session:
        await session.execute(
            update(GitHubAuthorization).values(
                expires_at=utcnow() - timedelta(seconds=1)
            )
        )
        await session.commit()
    assert (
        await client.post(BASE + "/selection", headers=admin["headers"], json=selection)
    ).status_code == 400


async def test_connection_idempotency_and_tenant_isolation(
    client, admin, github, session_factory, foreign_agent, customer, make_actor
):
    body = await connect(client, admin, github)
    headers = {**admin["headers"], "Idempotency-Key": "github-connect"}
    assert (await client.post(BASE, headers=headers, json=body)).status_code == 202
    assert (
        await client.post(BASE, headers=headers, json={**body, "security_level": 1})
    ).status_code == 409
    assert (
        await client.post(
            BASE,
            headers={**admin["headers"], "Idempotency-Key": "reuse-proof"},
            json=body,
        )
    ).status_code == 400
    assert (await client.get(BASE, headers=foreign_agent["headers"])).json()[
        "repository"
    ] is None
    assert (await client.get(BASE, headers=customer["headers"])).status_code == 403
    no_tenant = await make_actor("no-tenant-admin", "admin", None)
    assert (
        await client.post(BASE + "/authorization", headers=no_tenant["headers"])
    ).status_code == 403
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(GitHubJob)) == 1


@pytest.mark.parametrize("level", [1, 2])
async def test_security_collection_and_ready_status(
    client, admin, github, rag, session_factory, level
):
    github.files["billing/pricing.ts"] = (
        "1" * 40,
        'const price = 9900;\nconst api_key = "FAKE_KEY_moa_demo_DEMO";',
    )
    await connect(client, admin, github, security_level=level)
    await drain(session_factory, github, rag)
    result = (await client.get(BASE, headers=admin["headers"])).json()
    assert result["status"] == "연결 완료" and result["last_synced_sha"] == github.sha
    assert result["analysis_provider"] == "external_llm"
    assert github.blobs == ([] if level == 1 else ["1" * 40])
    assert result["report"]["indexed_files"] == ["billing/pricing.ts"]
    payload = next(p for k, p in rag.calls if k == "submit_github")
    assert payload["visibility"] == "내부 전용" and payload["version"] == github.sha
    assert payload["source_type"] == ("커밋" if level == 1 else "코드")
    assert "FAKE_KEY" not in json.dumps(payload)
    assert (
        payload["content_hash"] == hashlib.sha256(payload["text"].encode()).hexdigest()
    )
    if level == 2:
        assert result["report"]["masked"][0]["line"] == 2


async def test_level_zero_requires_no_github_config_and_rejects_bad_levels(
    client, admin, session_factory, github, rag
):
    for level in [3, True, "2", -1]:
        assert (
            await client.post(
                BASE,
                headers={**admin["headers"], "Idempotency-Key": str(level)},
                json={"security_level": level},
            )
        ).status_code == 400
    result = await client.post(
        BASE,
        headers={**admin["headers"], "Idempotency-Key": "zero"},
        json={"security_level": 0},
    )
    assert result.status_code == 202
    await drain(session_factory, github, rag)
    assert github.head_calls == 0 and not rag.calls
    assert (await client.get(BASE, headers=admin["headers"])).json()[
        "status"
    ] == "연동 안 함"


async def test_signature_dedup_conflict_and_unwatched_events(
    client, admin, github, session_factory
):
    await connect(client, admin, github)
    assert (await event(client, signature="sha256=" + "0" * 64)).status_code == 401
    replies = await asyncio.gather(*(event(client) for _ in range(4)))
    assert all(r.status_code == 202 and r.json() == replies[0].json() for r in replies)
    assert replies[0].json()["accepted"]
    assert (await event(client, after="c" * 40)).status_code == 409
    ignored = await event(client, delivery="ignored", ref="refs/heads/other")
    assert not ignored.json()["accepted"]
    assert (
        await event(client, delivery="ignored", ref="refs/heads/other")
    ).json() == ignored.json()
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(GitHubEvent)) == 2
        assert await session.scalar(select(func.count()).select_from(GitHubJob)) == 2


async def test_push_blocks_old_before_new_and_retries_lost_status(
    client, admin, github, rag, session_factory
):
    await connect(client, admin, github)
    await drain(session_factory, github, rag)
    github.sha, github.files["billing/pricing.ts"] = "b" * 40, (
        "4" * 40,
        "const price = 12900;",
    )
    assert (await event(client)).json()["accepted"]
    rag.fail_status = True
    await step(session_factory, github, rag)
    assert not any(
        p["version"] == "b" * 40 for k, p in rag.calls if k == "submit_github"
    )
    rag.fail_status, rag.lose_status = False, True
    await step(session_factory, github, rag)
    await drain(session_factory, github, rag)
    old = [p for p in rag.sources.values() if p["version"] == "a" * 40][0]
    assert old["status"] == "폐기"
    statuses = [p for k, p in rag.calls if k == "status"]
    assert all(p == statuses[0] for p in statuses)
    assert (await client.get(BASE, headers=admin["headers"])).json()[
        "last_synced_sha"
    ] == "b" * 40
    # 역순 도착 이벤트 본문의 after가 아니라 현재 브랜치 HEAD를 수집한다.
    await event(client, delivery="late", after="a" * 40)
    await drain(session_factory, github, rag)
    assert (await client.get(BASE, headers=admin["headers"])).json()[
        "last_synced_sha"
    ] == "b" * 40


async def test_policy_change_invalidates_bound_document_and_disable_waits_for_barrier(
    client, admin, agent, github, rag, session_factory
):
    doc = await create_document(client, agent)
    assert doc.status_code == 202
    await drain_documents(session_factory, rag)
    await connect(
        client,
        admin,
        github,
        document_bindings=[{"path": "billing/pricing.ts", "doc_id": "DOC-001"}],
    )
    await drain(session_factory, github, rag)
    await client.post(
        BASE,
        headers={**admin["headers"], "Idempotency-Key": "disable"},
        json={"security_level": 0},
    )
    rag.fail_status = True
    await step(session_factory, github, rag)
    async with session_factory() as session:
        assert (await session.scalar(select(DocumentVersion))).review_required
        integration = await session.scalar(select(GitHubIntegration))
        assert integration.pending_bindings and integration.status == "동기화 중"
    rag.fail_status = False
    await drain(session_factory, github, rag)
    assert not (await rag.view("alpha", "DOC-001", 1)).searchable
    assert (await client.get(BASE, headers=admin["headers"])).json()[
        "status"
    ] == "연동 안 함"


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "installation", "action": "deleted"},
        {
            "kind": "installation_repositories",
            "action": "removed",
            "repositories_removed": [{"id": 22}],
        },
        {"deleted": True},
    ],
)
async def test_revoke_and_branch_delete_block_without_github_read(
    client, admin, github, rag, session_factory, changes
):
    await connect(client, admin, github)
    await drain(session_factory, github, rag)
    calls = github.head_calls
    assert (await event(client, **changes)).json()["accepted"]
    await drain(session_factory, github, rag)
    assert github.head_calls == calls
    assert all(s["status"] == "폐기" for s in rag.sources.values())


async def test_removed_file_and_force_push_reactivation_revision(
    client, admin, github, rag, session_factory
):
    await connect(client, admin, github)
    await drain(session_factory, github, rag)
    github.files.pop("billing/pricing.ts")
    github.sha = "b" * 40
    await event(client)
    await drain(session_factory, github, rag)
    assert all(s["status"] == "폐기" for s in rag.sources.values())
    github.files["billing/pricing.ts"] = ("1" * 40, "const price = 9900;")
    github.sha = "a" * 40
    await event(client, delivery="force")
    await drain(session_factory, github, rag)
    source = list(rag.sources.values())[0]
    assert source["status"] == "유효" and source["source_revision"] == 3


async def test_lease_reclaim_and_stale_generation_never_submit(
    client, admin, github, rag, session_factory
):
    await connect(client, admin, github)
    first = await claim(session_factory)
    assert await claim(session_factory) is None
    await event(client)
    await process(session_factory, *first, github, rag)
    assert not rag.calls
    second = await claim(session_factory)
    async with session_factory() as session:
        job = await session.get(GitHubJob, second[0])
        job.lease_until = utcnow() - timedelta(seconds=1)
        await session.commit()
    reclaimed = await claim(session_factory)
    assert reclaimed[0] == second[0] and reclaimed[1] != second[1]
    await process(session_factory, *second, github, rag)
    assert not rag.calls
    await process(session_factory, *reclaimed, github, rag)
    await drain(session_factory, github, rag)
    assert (await client.get(BASE, headers=admin["headers"])).json()[
        "status"
    ] == "연결 완료"


async def test_lost_submit_and_missing_ai_source_recover_same_payload(
    client, admin, github, rag, session_factory
):
    await connect(client, admin, github)
    rag.lose_submit = True
    await step(session_factory, github, rag)
    await step(session_factory, github, rag)
    await step(session_factory, github, rag)
    rag.sources.clear()
    await drain(session_factory, github, rag)
    submissions = [p for k, p in rag.calls if k == "submit_github"]
    assert len(submissions) >= 3 and all(p == submissions[0] for p in submissions)
    assert (await client.get(BASE, headers=admin["headers"])).json()[
        "status"
    ] == "연결 완료"


async def test_rate_limit_and_permanent_failure_status(
    client, admin, github, rag, session_factory
):
    await connect(client, admin, github)
    github.failure = GitHubError("GITHUB_RATE_LIMITED", retry_after=125)
    await step(session_factory, github, rag)
    async with session_factory() as session:
        job = await session.scalar(select(GitHubJob))
        assert job.attempts == 1 and (job.available_at - utcnow()).total_seconds() > 120
    github.failure = GitHubError("GITHUB_TREE_TRUNCATED", retryable=False)
    await step(session_factory, github, rag)
    result = (await client.get(BASE, headers=admin["headers"])).json()
    assert (
        result["status"] == "동기화 실패"
        and result["error_code"] == "GITHUB_TREE_TRUNCATED"
    )
    assert not rag.calls


async def test_migration_roundtrip_preserves_existing_inquiry(
    db_engine, migrate, inquiry
):
    async with db_engine.begin() as connection:
        before = (await connection.execute(text("SELECT to_jsonb(i) - ARRAY['group_pk','normalized_error_code','received_at','is_seed'] FROM inquiries i"))).all()
        await connection.run_sync(migrate, "downgrade", "0010_answers")
        assert (
            await connection.execute(text("SELECT to_jsonb(i) - ARRAY['group_pk','normalized_error_code','received_at','is_seed'] FROM inquiries i"))
        ).all() == before
        await connection.run_sync(migrate, "upgrade", "head")
        assert (
            await connection.execute(text("SELECT to_jsonb(i) - ARRAY['group_pk','normalized_error_code','received_at','is_seed'] FROM inquiries i"))
        ).all() == before


async def test_github_http_oauth_permissions_and_fail_closed_tree():
    settings = SimpleNamespace(
        github_app_id="7",
        github_app_private_key_base64=SecretStr("unused"),
        github_client_id="client",
        github_client_secret=SecretStr("test-secret"),
        github_redirect_uri="https://admin.example.test/github/callback",
        github_app_slug="moa",
    )
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/login/oauth/access_token":
            assert json.loads(request.content)["code_verifier"] == "verifier"
            return httpx.Response(200, json={"access_token": "user-test-only"})
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "installations": [{"id": 11, "app_id": 7}, {"id": 12, "app_id": 9}]
                },
            )
        if request.url.path == "/user/installations/11/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": 22,
                            "full_name": "MOA-demo/syncnote-app",
                            "default_branch": "main",
                            "permissions": {"admin": True},
                        },
                        {"id": 23, "permissions": {"admin": False}},
                    ]
                },
            )
        return httpx.Response(200, json={"tree": [], "truncated": True})

    github = GitHubClient(settings, transport=httpx.MockTransport(handle))
    auth = parse_qs(urlsplit(github.authorization_url("state", "verifier")).query)
    assert auth["state"] == ["state"] and auth["code_challenge_method"] == ["S256"]
    assert await github.authorized_repositories("code", "verifier") == [REPOSITORY]
    assert calls[1].headers["authorization"] == "Bearer user-test-only"
    with pytest.raises(GitHubError, match="GITHUB_TREE_TRUNCATED"):
        await github.tree("MOA-demo/syncnote-app", "abc", "token")


async def test_rag_github_headers_and_string_version():
    payload = {
        "source_id": "github:22:code:path",
        "version": "a" * 40,
        "source_type": "코드",
    }

    def handle(request):
        assert request.headers["authorization"] == "Bearer test-ai-token"
        assert request.headers["x-tenant-id"] == "alpha"
        if request.method == "POST":
            assert json.loads(request.content) == payload
            return httpx.Response(202, json={**payload, "indexing_status": "대기"})
        assert request.url.params["source_type"] == "코드"
        return httpx.Response(
            200,
            json={
                **payload,
                "source_revision": 1,
                "indexing_status": "완료",
                "status": "유효",
                "review_required": False,
                "searchable": True,
            },
        )

    rag = RagClient(
        "http://ai.test", "test-ai-token", transport=httpx.MockTransport(handle)
    )
    assert (await rag.submit_github("alpha", payload)).version == "a" * 40
    assert (await rag.view_github("alpha", payload)).searchable


async def test_secret_filter_does_not_persist_raw_findings():
    raw = 'before\n-----BEGIN RSA PRIVATE KEY-----\nFAKEPRIVATE\n-----END RSA PRIVATE KEY-----\nconst token = "ghp_abcdefghijk";\nafter'
    filtered, findings = sanitize(raw, "billing/test.ts")
    assert "FAKEPRIVATE" not in filtered and "ghp_" not in filtered
    assert {f["kind"] for f in findings} == {"private_key", "secret_pattern"}
    assert all(set(f) == {"path", "line", "kind"} for f in findings)
    assert next(f["line"] for f in findings if f["kind"] == "secret_pattern") == 5


async def test_pending_github_change_blocks_document_answer(
    client, admin, agent, inquiry, github, rag, session_factory
):
    from test_answers import detail, body_from, send

    await create_document(client, agent)
    await drain_documents(session_factory, rag)
    await connect(
        client,
        admin,
        github,
        document_bindings=[{"path": "billing/", "doc_id": "DOC-001"}],
    )
    await drain(session_factory, github, rag)
    await event(client)
    body = body_from(
        await detail(client, agent, inquiry),
        evidence={"docs": [{"doc_id": "DOC-001", "version": 1}], "code": []},
    )
    result = await send(client, agent, inquiry, body)
    assert (
        result.status_code == 503
        and result.json()["error"]["code"] == "GITHUB_SYNC_PENDING"
    )


async def test_discovery_snapshot_loses_race_to_disable(
    client, admin, github, rag, session_factory
):
    await connect(client, admin, github)
    entered, release = asyncio.Event(), asyncio.Event()
    original = github.tree

    async def delayed(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    github.tree = delayed
    worker = asyncio.create_task(run_once(session_factory, github, rag))
    await asyncio.wait_for(entered.wait(), 3)
    try:
        response = await client.post(
            BASE,
            headers={**admin["headers"], "Idempotency-Key": "disable-race"},
            json={"security_level": 0},
        )
        assert response.status_code == 202
    finally:
        release.set()
    await asyncio.wait_for(worker, 3)
    await drain(session_factory, github, rag)
    assert not rag.calls
    assert (await client.get(BASE, headers=admin["headers"])).json()[
        "status"
    ] == "연동 안 함"


async def test_failed_job_manual_retry_preserves_payload(
    client, admin, github, rag, session_factory
):
    from app.workers.github import retry_job

    await connect(client, admin, github)
    github.failure = GitHubError("GITHUB_RATE_LIMITED")
    for _ in range(4):
        await step(session_factory, github, rag)
    async with session_factory() as session:
        job = await session.scalar(select(GitHubJob))
        assert job.phase == "failed"
        job_id = job.job_id
    github.failure = None
    await retry_job(session_factory, job_id)
    await drain(session_factory, github, rag)
    assert (await client.get(BASE, headers=admin["headers"])).json()[
        "status"
    ] == "연결 완료"
    with pytest.raises(ValueError):
        await retry_job(session_factory, job_id)


async def test_rsa_jwt_installation_token_is_repo_scoped():
    import base64
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    settings = SimpleNamespace(
        github_app_id="7",
        github_app_private_key_base64=SecretStr(base64.b64encode(pem).decode()),
    )

    def handle(request):
        token = request.headers["authorization"].split(" ", 1)[1]
        claims = jwt.decode(token, key.public_key(), algorithms=["RS256"], issuer="7")
        assert 0 < claims["exp"] - claims["iat"] <= 600
        assert json.loads(request.content) == {
            "repository_ids": [22],
            "permissions": {"contents": "read", "metadata": "read"},
        }
        return httpx.Response(201, json={"token": "test-install-token"})

    github = GitHubClient(settings, transport=httpx.MockTransport(handle))
    assert await github.installation_token(11, 22) == "test-install-token"


@pytest.mark.parametrize(
    "path",
    [
        "billing/.env",
        "billing/test.pem",
        "billing/secrets/a.ts",
        "billing/config/prod.env.example",
        "../billing/a.ts",
    ],
)
async def test_nested_excludes_are_mandatory(path):
    from app.services.github_security import exclusion

    assert exclusion(path, ["."], []) is not None


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:5173",
        "http://localhost:3000",
        "https://moa-client.vercel.app",
        "https://moa-admin-teal.vercel.app",
    ],
)
async def test_github_frontend_preflight(client, origin):
    response = await client.options(
        BASE,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization,Idempotency-Key,Content-Type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


async def test_unchanged_source_recovers_after_ai_storage_loss(
    client, admin, github, rag, session_factory
):
    await connect(client, admin, github)
    await drain(session_factory, github, rag)
    original = next(copy.deepcopy(p) for k, p in rag.calls if k == "submit_github")
    rag.sources.clear()
    assert (await event(client)).json()["accepted"]
    await drain(session_factory, github, rag)
    assert next(iter(rag.sources.values())) == original
    assert (await client.get(BASE, headers=admin["headers"])).json()[
        "status"
    ] == "연결 완료"
