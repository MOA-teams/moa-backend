import asyncio
import json
from uuid import uuid4
import httpx
import pytest
from sqlalchemy import select, text, func
from app.core.config import get_settings
from app.core.enums import MessageSender, MessageType
from app.core.pipeline import PipelineClient
from app.db.models import (
    Inquiry,
    AnalysisRequest,
    AnalysisRun,
    Message,
    ClassificationHistory,
)
from app.workers.analysis import run_once, claim, process
from conftest import get_row

pytestmark = pytest.mark.anyio
ADMIN = "/api/v1/admin/inquiries"
CALLBACK = "/internal/v1/pipeline-results"


class FakeAI:
    def __init__(self):
        self.runs = {}
        self.keys = {}
        self.calls = []
        self.fail = 0
        self.missing = False
        self.result_status = "완료"
        self.draft = False
        self.client = PipelineClient(
            "http://ai.test", "send-only", transport=httpx.MockTransport(self.handle)
        )

    def result(self, run):
        body = self.runs[run]
        result = {
            k: body[k]
            for k in (
                "tenant_id",
                "inquiry_id",
                "inquiry_revision",
                "based_on_message_id",
            )
        }
        result.update(run_id=run, run_status=self.result_status, knowledge_revision=1)
        if self.result_status == "실패":
            result.update(
                error={
                    "code": "LLM_UNAVAILABLE",
                    "message": "private-exception",
                    "retryable": True,
                }
            )
        elif self.result_status == "완료":
            result.update(
                analysis={
                    "category": "기능 오류",
                    "urgency": "높음",
                    "summary": "저장 오류",
                    "normalized_error_code": "E-502",
                    "missing_fields": [],
                },
                draft_result="초안 가능" if self.draft else "직접 확인 필요",
                verification={
                    "status": "확인 불가",
                    "checks": {"claims_supported": "확인 불가"},
                    "reasons": ["추가 확인 필요"],
                    "unsupported_sentences": [],
                },
            )
            if self.draft:
                result["draft"] = {
                    "answer_text": "확인 중입니다.",
                    "evidence": {
                        "docs": [{"doc_id": "DOC-999", "version": 1}],
                        "code": [],
                    },
                    "citations": [],
                    "style_references": [],
                }
        else:
            result["current_stage"] = "문의 분석"
        return result

    async def handle(self, request):
        self.calls.append(request)
        assert request.headers["Authorization"] == "Bearer send-only"
        assert request.headers["X-Tenant-ID"] == "alpha"
        if self.fail:
            return httpx.Response(
                self.fail, json={"error": {"message": "secret-token-and-body"}}
            )
        if request.method == "POST":
            key = request.headers["Idempotency-Key"]
            body = json.loads(request.content)
            run = self.keys.setdefault(key, "RUN-" + uuid4().hex)
            self.runs[run] = body
            return httpx.Response(
                202,
                json={
                    "run_id": run,
                    "inquiry_id": body["inquiry_id"],
                    "inquiry_revision": body["inquiry_revision"],
                    "run_status": "대기",
                },
            )
        run = request.url.path.rsplit("/", 1)[1]
        if self.missing or run not in self.runs:
            return httpx.Response(404, json={"error": {"code": "RUN_NOT_FOUND"}})
        return httpx.Response(200, json=self.result(run))


@pytest.fixture
async def ai(monkeypatch):
    monkeypatch.setenv("AI_CALLBACK_TOKEN", "receive-only")
    get_settings.cache_clear()
    return FakeAI()


def callback_headers(run, tenant="alpha"):
    return {
        "Authorization": "Bearer receive-only",
        "X-Tenant-ID": tenant,
        "Idempotency-Key": "pipeline-result:" + run,
    }


async def due(factory):
    async with factory() as s:
        await s.execute(
            text("UPDATE analysis_requests SET available_at=now()-interval '1 second'")
        )
        await s.commit()


async def submit(factory, ai):
    await due(factory)
    assert await run_once(factory, ai.client)
    return next(reversed(ai.runs))


async def test_snapshot_atomic_and_ai_submission_contract(
    client, customer, inquiry, session_factory, ai
):
    run = await submit(session_factory, ai)
    snapshot = ai.runs[run]
    assert snapshot["inquiry_id"] == inquiry["inquiry_id"]
    assert snapshot["customer_category"] == "기능 오류"
    assert snapshot["based_on_message_id"] == snapshot["messages"][0]["message_id"]
    assert [m["sender"] for m in snapshot["messages"]] == ["customer", "ai_auto"]
    assert "source_customer_id" not in snapshot and "customer_id" not in snapshot
    assert snapshot["review_only"] is False
    async with session_factory() as s:
        job = await s.scalar(select(AnalysisRequest))
        assert job.phase == "poll" and job.run_id == run
        assert job.snapshot == snapshot


async def test_callback_applies_once_and_never_sends_customer_message(
    client, customer, agent, inquiry, session_factory, ai
):
    run = await submit(session_factory, ai)
    body = ai.result(run)
    headers = callback_headers(run)
    responses = await asyncio.gather(
        *[client.post(CALLBACK, headers=headers, json=body) for _ in range(3)]
    )
    assert all(r.status_code == 200 for r in responses)
    assert sorted(r.json()["apply_status"] for r in responses) == [
        "applied",
        "duplicate",
        "duplicate",
    ]
    detail = (
        await client.get(f"{ADMIN}/{inquiry['inquiry_id']}", headers=agent["headers"])
    ).json()
    assert detail["category"] == "기능 오류" and detail["urgency"] == "높음"
    assert detail["analysis"]["normalized_error_code"] == "E-502"
    assert detail["run_status"] == "완료" and detail["processing_stage"] == "검토 대기"
    chat = (
        await client.get(
            "/api/v1/inquiries/" + inquiry["inquiry_id"], headers=customer["headers"]
        )
    ).json()
    assert len(chat["messages"]) == 2 and "analysis" not in chat
    changed = {**body, "knowledge_revision": 2}
    assert (
        await client.post(CALLBACK, headers=headers, json=changed)
    ).status_code == 409


async def test_callback_lost_is_recovered_by_poll(
    client, agent, inquiry, session_factory, ai
):
    run = await submit(session_factory, ai)
    await due(session_factory)
    await run_once(session_factory, ai.client)
    async with session_factory() as s:
        assert (await s.get(AnalysisRun, run)).response["apply_status"] == "applied"
        assert (await s.scalar(select(AnalysisRequest))).phase == "done"


async def test_new_customer_message_makes_old_result_stale(
    client, customer, agent, inquiry, session_factory, ai
):
    run = await submit(session_factory, ai)
    assert (
        await client.post(
            "/api/v1/inquiries/" + inquiry["inquiry_id"] + "/messages",
            headers={**customer["headers"], "Idempotency-Key": "new"},
            json={"text": "새 증상"},
        )
    ).status_code == 201
    response = await client.post(
        CALLBACK, headers=callback_headers(run), json=ai.result(run)
    )
    assert response.json()["apply_status"] == "ignored_stale"
    detail = (
        await client.get(f"{ADMIN}/{inquiry['inquiry_id']}", headers=agent["headers"])
    ).json()
    assert detail["category"] is None and detail["analysis"] is None


async def test_manual_classification_survives_old_and_new_ai_results(
    client, agent, inquiry, session_factory, ai
):
    run = await submit(session_factory, ai)
    path = f"{ADMIN}/{inquiry['inquiry_id']}"
    payload = {
        "expected_revision": 1,
        "category": "계정",
        "urgency": "낮음",
        "reason": "담당자가 확인",
    }
    response = await client.patch(
        path + "/classification", headers=agent["headers"], json=payload
    )
    assert response.status_code == 200 and response.json()["revision"] == 2
    assert (
        await client.patch(
            path + "/classification", headers=agent["headers"], json=payload
        )
    ).status_code == 409
    assert (
        await client.post(CALLBACK, headers=callback_headers(run), json=ai.result(run))
    ).json()["apply_status"] == "ignored_stale"
    detail = (await client.get(path, headers=agent["headers"])).json()
    retry = {
        "expected_inquiry_revision": 2,
        "based_on_message_id": detail["latest_customer_message_id"],
    }
    accepted = await client.post(
        path + "/analysis",
        headers={**agent["headers"], "Idempotency-Key": "retry"},
        json=retry,
    )
    assert accepted.status_code == 202 and accepted.json()["run_id"] is None
    await due(session_factory)
    await run_once(session_factory, ai.client)  # 이전 실행 정리
    if len(ai.runs) == 1:
        await submit(session_factory, ai)
    newest = next(reversed(ai.runs))
    await client.post(
        CALLBACK, headers=callback_headers(newest), json=ai.result(newest)
    )
    detail = (await client.get(path, headers=agent["headers"])).json()
    assert (detail["category"], detail["urgency"]) == ("계정", "낮음")
    assert detail["analysis"]["category"] == "기능 오류"
    async with session_factory() as s:
        assert (
            await s.scalar(select(func.count()).select_from(ClassificationHistory)) == 1
        )


async def test_reanalysis_idempotency_and_roles(
    client, agent, customer, foreign_agent, inquiry, session_factory, ai
):
    path = f"{ADMIN}/{inquiry['inquiry_id']}/analysis"
    detail = (
        await client.get(f"{ADMIN}/{inquiry['inquiry_id']}", headers=agent["headers"])
    ).json()
    body = {
        "expected_inquiry_revision": 1,
        "based_on_message_id": detail["latest_customer_message_id"],
    }
    headers = {**agent["headers"], "Idempotency-Key": "same"}
    a = await client.post(path, headers=headers, json=body)
    b = await client.post(path, headers=headers, json=body)
    assert a.status_code == b.status_code == 202 and a.json() == b.json()
    assert (
        await client.post(path, headers=headers, json={**body, "review_only": False})
    ).status_code == 409
    assert (
        await client.post(
            path, headers={**customer["headers"], "Idempotency-Key": "x"}, json=body
        )
    ).status_code == 403
    assert (
        await client.post(
            path,
            headers={**foreign_agent["headers"], "Idempotency-Key": "x"},
            json=body,
        )
    ).status_code == 404
    assert (
        await client.post(path, headers={"Idempotency-Key": "x"}, json=body)
    ).status_code == 401


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"tenant_id": "beta"}, 403),
        ({"inquiry_revision": 9}, 409),
        ({"based_on_message_id": "MSG-999999"}, 409),
        ({"run_status": "처리 중"}, 400),
    ],
)
async def test_callback_rejects_wrong_context(
    client, inquiry, session_factory, ai, change, expected
):
    run = await submit(session_factory, ai)
    response = await client.post(
        CALLBACK, headers=callback_headers(run), json={**ai.result(run), **change}
    )
    assert response.status_code == expected


async def test_callback_before_registration_and_wrong_credentials(
    client, inquiry, session_factory, ai
):
    run = await submit(session_factory, ai)
    body = {**ai.result(run), "run_id": "RUN-unknown"}
    assert (
        await client.post(CALLBACK, headers=callback_headers(run), json=body)
    ).status_code == 409
    headers = {**callback_headers(run), "Authorization": "Bearer bad"}
    assert (await client.post(CALLBACK, headers=headers, json=body)).status_code == 401


async def test_failure_is_staff_visible_without_private_error(
    client, agent, customer, inquiry, session_factory, ai
):
    run = await submit(session_factory, ai)
    ai.result_status = "실패"
    assert (
        await client.post(CALLBACK, headers=callback_headers(run), json=ai.result(run))
    ).status_code == 200
    detail = await client.get(
        f"{ADMIN}/{inquiry['inquiry_id']}", headers=agent["headers"]
    )
    assert (
        detail.json()["analysis_error"] == "AI_PIPELINE_FAILED"
        and "private-exception" not in detail.text
    )
    chat = await client.get(
        "/api/v1/inquiries/" + inquiry["inquiry_id"], headers=customer["headers"]
    )
    assert "private-exception" not in chat.text


async def test_retry_limit_keeps_original_inquiry(inquiry, session_factory, ai):
    ai.fail = 503
    for _ in range(5):
        await due(session_factory)
        await run_once(session_factory, ai.client)
    async with session_factory() as s:
        job = await s.scalar(select(AnalysisRequest))
        assert job.phase == "failed" and job.attempts == 5
        assert await s.scalar(select(func.count()).select_from(Inquiry)) == 1
    assert len({r.headers["Idempotency-Key"] for r in ai.calls}) == 1
    assert all(r.content == ai.calls[0].content for r in ai.calls)


async def test_ai_restart_resubmits_once_and_rejects_old_run(
    client, inquiry, session_factory, ai
):
    old = await submit(session_factory, ai)
    old_body = ai.result(old)
    ai.runs.clear()
    ai.keys.clear()
    await due(session_factory)
    await run_once(session_factory, ai.client)
    new = await submit(session_factory, ai)
    assert new != old
    assert (
        await client.post(CALLBACK, headers=callback_headers(old), json=old_body)
    ).json()["apply_status"] == "ignored_stale"
    ai.runs.clear()
    ai.keys.clear()
    await due(session_factory)
    await run_once(session_factory, ai.client)
    async with session_factory() as s:
        job = await s.scalar(select(AnalysisRequest))
        assert job.phase == "failed" and job.resubmissions == 1


async def test_lease_only_one_worker_and_expired_lease_is_reclaimed(
    inquiry, session_factory, ai
):
    claims = await asyncio.gather(claim(session_factory), claim(session_factory))
    assert sum(c is not None for c in claims) == 1
    old = next(c for c in claims if c)
    async with session_factory() as s:
        await s.execute(
            text("UPDATE analysis_requests SET lease_until=now()-interval '1 second'")
        )
        await s.commit()
    new = await claim(session_factory)
    assert new[0] == old[0] and new[1] != old[1]
    await process(session_factory, *old, ai.client)
    assert not ai.calls
    await process(session_factory, *new, ai.client)
    assert len(ai.calls) == 1


async def test_deadline_stops_polling(inquiry, session_factory, ai):
    await submit(session_factory, ai)
    async with session_factory() as s:
        await s.execute(
            text(
                "UPDATE analysis_requests SET deadline=now()-interval '1 second', available_at=now()-interval '1 second'"
            )
        )
        await s.commit()
    await run_once(session_factory, ai.client)
    async with session_factory() as s:
        job = await s.scalar(select(AnalysisRequest))
        assert job.error_code == "AI_ANALYSIS_TIMEOUT"
    assert len(ai.calls) == 1


async def test_large_conversation_preserved_but_not_submitted(
    client, customer, inquiry, session_factory, ai
):
    async with session_factory() as s:
        row = await get_row(s, Inquiry, inquiry["inquiry_id"])
        for seq in range(3, 202):
            s.add(
                Message(
                    inquiry_id=row.inquiry_id,
                    sequence=seq,
                    sender=MessageSender.CUSTOMER,
                    message_type=MessageType.CUSTOMER_MESSAGE,
                    text="x",
                )
            )
        row.last_message_sequence = 201
        await s.commit()
    response = await client.post(
        "/api/v1/inquiries/" + inquiry["inquiry_id"] + "/messages",
        headers={**customer["headers"], "Idempotency-Key": "large"},
        json={"text": "추가"},
    )
    assert response.status_code == 201
    async with session_factory() as s:
        row = await get_row(s, Inquiry, inquiry["inquiry_id"])
        job = await s.get(AnalysisRequest, row.latest_analysis_request_id)
        assert (
            job.error_code == "ANALYSIS_INPUT_TOO_LARGE"
            and len(job.snapshot["messages"]) == 202
        )


async def test_same_revision_reanalysis_supersedes_old_run(
    client, agent, inquiry, session_factory, ai
):
    old = await submit(session_factory, ai)
    detail = (
        await client.get(f"{ADMIN}/{inquiry['inquiry_id']}", headers=agent["headers"])
    ).json()
    body = {
        "expected_inquiry_revision": 1,
        "based_on_message_id": detail["latest_customer_message_id"],
    }
    response = await client.post(
        f"{ADMIN}/{inquiry['inquiry_id']}/analysis",
        headers={**agent["headers"], "Idempotency-Key": "new-run"},
        json=body,
    )
    assert response.status_code == 202
    assert (
        await client.post(CALLBACK, headers=callback_headers(old), json=ai.result(old))
    ).json()["apply_status"] == "ignored_stale"


async def test_callback_and_poll_race_apply_once(client, inquiry, session_factory, ai):
    run = await submit(session_factory, ai)
    await due(session_factory)
    results = await asyncio.gather(
        run_once(session_factory, ai.client),
        client.post(CALLBACK, headers=callback_headers(run), json=ai.result(run)),
    )
    assert results[1].status_code == 200
    async with session_factory() as s:
        job = await s.scalar(select(AnalysisRequest))
        assert job.phase == "done"
        assert await s.scalar(select(func.count()).select_from(AnalysisRun)) == 1
        assert await s.scalar(select(func.count()).select_from(Message)) == 2


async def test_snapshot_environment_and_source_id_are_separate(
    client, customer, session_factory, ai
):
    env = {"os": "iOS 18", "app_version": "2.4.0", "error_code": "E-502"}
    response = await client.post(
        "/api/v1/inquiries",
        headers={**customer["headers"], "Idempotency-Key": "env"},
        json={
            "message": "저장이 안 됩니다",
            "customer_category": "기능 오류",
            "source_customer_id": "U-9999",
            "env": env,
        },
    )
    assert response.status_code == 201
    run = await submit(session_factory, ai)
    assert ai.runs[run]["env"] == env and "source_customer_id" not in ai.runs[run]


async def test_explicit_malformed_terminal_result_rejected(
    client, inquiry, session_factory, ai
):
    run = await submit(session_factory, ai)
    response = await client.post(
        CALLBACK,
        headers=callback_headers(run),
        json={**ai.result(run), "draft_result": "초안 가능", "draft": None},
    )
    assert response.status_code == 400
    async with session_factory() as s:
        assert (await s.get(AnalysisRun, run)).result is None


async def test_http_failure_and_contract_mismatch_do_not_leak_secrets(
    inquiry, session_factory, ai, caplog
):
    async def invalid(request):
        return httpx.Response(
            202,
            json={
                "run_id": "RUN-invalid",
                "inquiry_id": "Q-9999",
                "inquiry_revision": 1,
                "run_status": "대기",
            },
        )

    remote = PipelineClient(
        "http://ai.test", "test-secret", transport=httpx.MockTransport(invalid)
    )
    await run_once(session_factory, remote)
    async with session_factory() as s:
        job = await s.scalar(select(AnalysisRequest))
        assert job.error_code == "AI_INVALID_RESPONSE" and job.phase == "failed"
    assert "test-secret" not in caplog.text


async def test_failed_message_rolls_back_new_analysis_request(
    client, customer, inquiry, session_factory
):
    async with session_factory() as s:
        await s.execute(
            text(
                "ALTER TABLE analysis_requests ADD CONSTRAINT test_block_snapshot CHECK (inquiry_revision < 2)"
            )
        )
        await s.commit()
    from app.services.inquiries import add_message
    from app.schemas.inquiries import MessageCreateRequest
    from app.schemas.auth import AuthenticatedUser
    from sqlalchemy.exc import IntegrityError

    user = AuthenticatedUser(
        user_id=customer["user_id"],
        internal_user_id=customer["internal_id"],
        email="customer@example.com",
        role="customer",
        tenant_id="alpha",
    )
    async with session_factory() as s:
        with pytest.raises(IntegrityError):
            await add_message(
                s,
                user=user,
                inquiry_id=inquiry["inquiry_id"],
                payload=MessageCreateRequest(text="new"),
                key="rollback",
            )
        row = await get_row(s, Inquiry, inquiry["inquiry_id"])
        assert row.revision == 1 and row.last_message_sequence == 2
        assert await s.scalar(select(func.count()).select_from(AnalysisRequest)) == 1


async def test_new_api_swagger_errors_and_patch_cors(client):
    spec = (await client.get("/openapi.json")).json()
    for path, method in [
        (ADMIN + "/{inquiry_id}/analysis", "post"),
        (ADMIN + "/{inquiry_id}/classification", "patch"),
        (CALLBACK, "post"),
    ]:
        assert {"400", "401", "403", "409"} <= set(
            spec["paths"][path][method]["responses"]
        )
    response = await client.options(
        ADMIN + "/Q-1001/classification",
        headers={
            "Origin": "https://moa-admin-teal.vercel.app",
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
