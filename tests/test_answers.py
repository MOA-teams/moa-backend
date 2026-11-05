import asyncio

import pytest
from sqlalchemy import func, select, text
from app.db.models import (
    Answer,
    AnalysisDraft,
    AnalysisRequest,
    DocumentVersion,
    Feedback,
    Inquiry,
    Message,
    Tenant,
)
from app.services.knowledge import lock_knowledge
from test_documents import rag, create as create_document, drain
from test_pipeline import ai, submit, callback_headers, CALLBACK

pytestmark = pytest.mark.anyio
ADMIN = "/api/v1/admin/inquiries"
CUSTOMER = "/api/v1/inquiries"


async def detail(client, agent, inquiry):
    response = await client.get(
        f"{ADMIN}/{inquiry['inquiry_id']}", headers=agent["headers"]
    )
    assert response.status_code == 200, response.text
    return response.json()


def body_from(view, **changes):
    body = {
        "expected_inquiry_revision": view["revision"],
        "expected_last_customer_message_id": view["latest_customer_message_id"],
        "expected_backend_knowledge_revision": view["backend_knowledge_revision"],
        "answer_text": "담당자가 확인한 답변입니다.",
        "evidence": {"docs": [], "code": []},
    }
    if view["draft"]:
        body.update(
            draft_id=view["draft"]["draft_id"],
            draft_revision=view["draft"]["revision"],
            answer_text=view["draft"]["answer_text"],
            evidence=view["draft"]["evidence"],
        )
    return {**body, **changes}


async def send(client, actor, inquiry, body, key="answer"):
    return await client.post(
        f"{ADMIN}/{inquiry['inquiry_id']}/answers",
        headers={**actor["headers"], "Idempotency-Key": key},
        json=body,
    )


async def close(client, actor, inquiry, revision, key="close"):
    return await client.post(
        f"{ADMIN}/{inquiry['inquiry_id']}/close",
        headers={**actor["headers"], "Idempotency-Key": key},
        json={"expected_inquiry_revision": revision},
    )


async def feedback(client, actor, inquiry, body, key="feedback"):
    return await client.post(
        f"{CUSTOMER}/{inquiry['inquiry_id']}/feedback",
        headers={**actor["headers"], "Idempotency-Key": key},
        json=body,
    )


async def with_draft(client, agent, inquiry, session_factory, ai, evidence=None):
    run = await submit(session_factory, ai)
    ai.draft = True
    result = ai.result(run)
    result["draft"]["evidence"] = evidence or {"docs": [], "code": []}
    result["verification"]["status"] = "실패"
    result["verification"]["reasons"] = ["별도 검수 실패: 내부용 상세"]
    applied = await client.post(CALLBACK, headers=callback_headers(run), json=result)
    assert applied.status_code == 200, applied.text
    return await detail(client, agent, inquiry), result


@pytest.mark.parametrize("edited", [False, True])
async def test_draft_to_feedback_end_to_end(
    client, customer, agent, inquiry, session_factory, ai, edited
):
    view, result = await with_draft(client, agent, inquiry, session_factory, ai)
    payload = body_from(view)
    if edited:
        payload["answer_text"] = "수정 후 담당자가 승인한 답변입니다."
    response = await send(client, agent, inquiry, payload)
    assert response.status_code == 201, response.text
    answer = response.json()
    assert answer["answer_id"] == "A-1001" and answer["inquiry_revision"] == 2
    replay = await send(client, agent, inquiry, payload)
    assert replay.json() == answer
    chat = (
        await client.get(
            f"{CUSTOMER}/{inquiry['inquiry_id']}", headers=customer["headers"]
        )
    ).json()
    assert chat["messages"][-1]["text"] == payload["answer_text"]
    assert chat["messages"][-1]["answer_id"] == answer["answer_id"]
    assert "original_verification" not in str(chat) and "내부용 상세" not in str(chat)
    admin = await detail(client, agent, inquiry)
    audit = admin["answers"][0]
    assert audit["original_verification"]["status"] == "실패"
    assert audit["original_draft"] == result["draft"]
    assert audit["approval"]["text_modified"] == edited
    assert (audit["agent_edit_ratio"] > 0) == edited
    closed = await close(client, agent, inquiry, 2)
    assert closed.status_code == 200 and closed.json()["resolution_status"] == "미확인"
    assert (await close(client, agent, inquiry, 2)).json() == closed.json()
    rated = await feedback(client, customer, inquiry, {"rating": 5, "resolved": False})
    assert rated.status_code == 201 and rated.json()["resolution_status"] == "미해결"
    assert rated.json()["answer_id"] == answer["answer_id"]
    assert (
        await feedback(client, customer, inquiry, {"rating": 5, "resolved": False})
    ).json() == rated.json()
    assert (
        await feedback(client, customer, inquiry, {"resolved": True}, "other")
    ).status_code == 409
    after = (
        await client.get(
            f"{CUSTOMER}/{inquiry['inquiry_id']}", headers=customer["headers"]
        )
    ).json()
    assert after["status"] == "상담 종료" and after["feedback_submitted"]
    assert after["feedback"]["resolved"] is False
    assert (
        await client.post(
            f"{CUSTOMER}/{inquiry['inquiry_id']}/messages",
            headers={**customer["headers"], "Idempotency-Key": "late"},
            json={"text": "추가"},
        )
    ).status_code == 409
    async with session_factory() as s:
        assert (await s.scalar(select(AnalysisDraft))).content == result["draft"]
        assert await s.scalar(select(func.count()).select_from(Answer)) == 1
        assert await s.scalar(select(func.count()).select_from(Feedback)) == 1


async def test_manual_delivery_concurrent_idempotency_and_snapshot(
    client, customer, agent, inquiry, session_factory
):
    body = body_from(await detail(client, agent, inquiry))
    responses = await asyncio.gather(
        *(send(client, agent, inquiry, body) for _ in range(3))
    )
    assert all(r.status_code == 201 for r in responses)
    assert len({r.json()["answer_id"] for r in responses}) == 1
    assert (
        await send(client, agent, inquiry, {**body, "answer_text": "다른 본문"})
    ).status_code == 409
    assert (await send(client, agent, inquiry, body, "other-key")).status_code == 409
    added = await client.post(
        f"{CUSTOMER}/{inquiry['inquiry_id']}/messages",
        headers={**customer["headers"], "Idempotency-Key": "next"},
        json={"text": "감사합니다"},
    )
    assert added.status_code == 201
    async with session_factory() as s:
        inquiry_row = await s.scalar(select(Inquiry))
        snapshot = (
            await s.get(AnalysisRequest, inquiry_row.latest_analysis_request_id)
        ).snapshot
        assert (
            next(m for m in snapshot["messages"] if m["sender"] == "agent")["answer_id"]
            == "A-1001"
        )
        answer = await s.scalar(select(Answer))
        assert answer.original_draft is None and answer.agent_edit_ratio is None
    # 마지막 페이지의 메시지에 해당하는 답변만 감사 정보를 반환한다.
    first = (
        await client.get(
            f"{ADMIN}/{inquiry['inquiry_id']}?limit=1", headers=agent["headers"]
        )
    ).json()
    assert first["answers"] == []


@pytest.mark.parametrize(
    "mutation",
    ["message", "classification", "analysis", "draft_revision", "legacy_knowledge"],
)
async def test_stale_draft_rejected(
    client, customer, agent, inquiry, session_factory, ai, mutation
):
    view, _ = await with_draft(client, agent, inquiry, session_factory, ai)
    body = body_from(view)
    if mutation == "message":
        await client.post(
            f"{CUSTOMER}/{inquiry['inquiry_id']}/messages",
            headers={**customer["headers"], "Idempotency-Key": "next"},
            json={"text": "추가 질문"},
        )
    elif mutation == "classification":
        await client.patch(
            f"{ADMIN}/{inquiry['inquiry_id']}/classification",
            headers=agent["headers"],
            json={
                "expected_revision": 1,
                "category": "기타",
                "urgency": "낮음",
                "reason": "담당자 확인",
            },
        )
    elif mutation == "analysis":
        await client.post(
            f"{ADMIN}/{inquiry['inquiry_id']}/analysis",
            headers={**agent["headers"], "Idempotency-Key": "reanalyze"},
            json={
                "expected_inquiry_revision": 1,
                "based_on_message_id": view["latest_customer_message_id"],
            },
        )
    else:
        async with session_factory() as s:
            if mutation == "draft_revision":
                (await s.scalar(select(AnalysisDraft))).revision += 1
            else:
                (await s.scalar(select(AnalysisRequest))).backend_knowledge_revision = (
                    None
                )
            await s.commit()
    assert (await send(client, agent, inquiry, body)).status_code == 409
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Answer)) == 0


async def test_late_callback_cannot_overwrite_sent_or_closed(
    client, agent, inquiry, session_factory, ai
):
    run = await submit(session_factory, ai)
    sent = await send(
        client, agent, inquiry, body_from(await detail(client, agent, inquiry))
    )
    assert sent.status_code == 201
    closed = await close(client, agent, inquiry, 2)
    assert closed.status_code == 200
    applied = await client.post(
        CALLBACK, headers=callback_headers(run), json=ai.result(run)
    )
    assert (
        applied.status_code == 200 and applied.json()["apply_status"] == "ignored_stale"
    )
    view = await detail(client, agent, inquiry)
    assert view["status"] == "상담 종료" and view["revision"] == 3


async def test_document_changes_and_live_ai_check(
    client, agent, inquiry, session_factory, rag
):
    assert (await create_document(client, agent)).status_code == 202
    await drain(session_factory, rag)
    view = await detail(client, agent, inquiry)
    body = body_from(
        view, evidence={"docs": [{"doc_id": "DOC-001", "version": 1}], "code": []}
    )
    rag.fail_view = True
    assert (await send(client, agent, inquiry, body)).status_code == 503
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Answer)) == 0
        assert (await s.scalar(select(Inquiry))).revision == 1
    rag.fail_view = False
    rag.searchable = False
    assert (await send(client, agent, inquiry, body)).status_code == 409
    rag.searchable = True
    assert (await send(client, agent, inquiry, body)).status_code == 201


async def test_knowledge_trigger_and_draft_invalidation(
    client, agent, inquiry, session_factory, ai, rag
):
    view, _ = await with_draft(client, agent, inquiry, session_factory, ai)
    body = body_from(view)
    await create_document(client, agent)
    assert (await send(client, agent, inquiry, body)).json()["error"][
        "code"
    ] == "STALE_KNOWLEDGE"
    current = await detail(client, agent, inquiry)
    body["expected_backend_knowledge_revision"] = current["backend_knowledge_revision"]
    assert (await send(client, agent, inquiry, body)).json()["error"][
        "code"
    ] == "STALE_KNOWLEDGE"
    before = current["backend_knowledge_revision"]
    await drain(session_factory, rag)
    indexed = (await detail(client, agent, inquiry))["backend_knowledge_revision"]
    assert indexed > before
    await client.get("/api/v1/admin/documents", headers=agent["headers"])
    assert (await detail(client, agent, inquiry))[
        "backend_knowledge_revision"
    ] == indexed
    rag.searchable = False
    await client.get("/api/v1/admin/documents", headers=agent["headers"])
    assert (await detail(client, agent, inquiry))[
        "backend_knowledge_revision"
    ] > indexed


async def test_scope_and_permission(
    client, customer, other_customer, foreign_agent, agent, inquiry
):
    body = body_from(await detail(client, agent, inquiry))
    assert (await send(client, customer, inquiry, body)).status_code == 403
    assert (await send(client, foreign_agent, inquiry, body)).status_code == 404
    assert (await close(client, foreign_agent, inquiry, 1)).status_code == 404
    assert (
        await feedback(client, other_customer, inquiry, {"rating": 5})
    ).status_code == 404
    assert (await feedback(client, agent, inquiry, {"rating": 5})).status_code == 403
    assert (await close(client, agent, inquiry, 1)).json()["error"][
        "code"
    ] == "ANSWER_REQUIRED"
    assert (await feedback(client, customer, inquiry, {"rating": 5})).json()["error"][
        "code"
    ] == "INQUIRY_NOT_CLOSED"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"rating": 0},
        {"rating": 6},
        {"rating": True},
        {"rating": 1.5},
        {"resolved": "true"},
        {"comment": "  "},
    ],
)
async def test_feedback_validation(client, customer, inquiry, payload):
    assert (await feedback(client, customer, inquiry, payload)).status_code == 400


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"rating": 5}, "미확인"),
        ({"resolved": False}, "미해결"),
        ({"resolved": True}, "해결 확인"),
        ({"comment": "잘 봤습니다"}, "미확인"),
    ],
)
async def test_optional_feedback_and_first_submission(
    client, customer, agent, inquiry, payload, expected
):
    await send(client, agent, inquiry, body_from(await detail(client, agent, inquiry)))
    await close(client, agent, inquiry, 2)
    responses = await asyncio.gather(
        feedback(client, customer, inquiry, payload, "one"),
        feedback(client, customer, inquiry, {"rating": 2}, "two"),
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    submitted = next(r.json() for r in responses if r.status_code == 201)
    if submitted["rating"] != 2:
        assert submitted["resolution_status"] == expected


async def test_competing_agents_and_close(client, agent, inquiry, make_actor):
    other = await make_actor("secondagent", "agent")
    body = body_from(await detail(client, agent, inquiry))
    replies = await asyncio.gather(
        send(client, agent, inquiry, body), send(client, other, inquiry, body)
    )
    assert sorted(r.status_code for r in replies) == [201, 409]
    next_body = body_from(await detail(client, agent, inquiry))
    results = await asyncio.gather(
        send(client, agent, inquiry, next_body, "next"),
        close(client, other, inquiry, 2),
    )
    assert sorted(r.status_code for r in results) in ([201, 409], [200, 409])


async def test_sending_holds_document_change_until_commit(
    client, agent, inquiry, session_factory, rag
):
    await create_document(client, agent)
    await drain(session_factory, rag)
    body = body_from(
        await detail(client, agent, inquiry),
        evidence={"docs": [{"doc_id": "DOC-001", "version": 1}], "code": []},
    )
    entered, release, changed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_view = rag.view

    async def delayed(*args):
        entered.set()
        await release.wait()
        return await original_view(*args)

    rag.view = delayed

    async def mutate():
        async with session_factory() as s:
            await lock_knowledge(s, "alpha")
            version = await s.scalar(select(DocumentVersion))
            version.review_required = True
            await s.commit()
        changed.set()

    sending = asyncio.create_task(send(client, agent, inquiry, body))
    await asyncio.wait_for(entered.wait(), 3)
    mutation = asyncio.create_task(mutate())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(changed.wait()), 0.1)
    finally:
        release.set()
    assert (await asyncio.wait_for(sending, 3)).status_code == 201
    await asyncio.wait_for(mutation, 3)


async def test_migration_preserves_existing_inquiry(db_engine, migrate, inquiry):
    async with db_engine.begin() as conn:
        before = (
            await conn.execute(
                text("SELECT inquiry_id, public_id, revision FROM inquiries")
            )
        ).all()
        await conn.run_sync(migrate, "downgrade", "0009_analysis")
        await conn.run_sync(migrate, "upgrade", "head")
        assert (
            await conn.execute(
                text("SELECT inquiry_id, public_id, revision FROM inquiries")
            )
        ).all() == before
        assert await conn.scalar(
            text("SELECT backend_knowledge_revision IS NULL FROM analysis_requests")
        )
        assert await conn.scalar(text("SELECT nextval('public_answer_seq')")) == 1001


@pytest.mark.parametrize("kind", ["message", "close"])
async def test_customer_message_races_with_send_or_close(
    client, customer, agent, inquiry, kind
):
    body = body_from(await detail(client, agent, inquiry))
    if kind == "close":
        assert (await send(client, agent, inquiry, body)).status_code == 201
        action = close(client, agent, inquiry, 2)
    else:
        action = send(client, agent, inquiry, body)

    async def customer_post():
        return await client.post(
            f"{CUSTOMER}/{inquiry['inquiry_id']}/messages",
            headers={**customer["headers"], "Idempotency-Key": "racing-message"},
            json={"text": "새로운 정보입니다"},
        )

    actor_response, customer_response = await asyncio.gather(action, customer_post())
    final = await detail(client, agent, inquiry)
    if kind == "close":
        assert sorted([actor_response.status_code, customer_response.status_code]) in (
            [200, 409],
            [201, 409],
        )
        assert final["status"] == (
            "상담 종료" if actor_response.status_code == 200 else "접수"
        )
    else:
        assert customer_response.status_code == 201
        assert actor_response.status_code in (201, 409)
        assert final["status"] == "접수"


@pytest.mark.parametrize("kind", ["review", "retire", "version"])
async def test_document_transition_invalidates_approval_and_retry_is_stable(
    client, agent, inquiry, session_factory, rag, kind
):
    await create_document(client, agent)
    await drain(session_factory, rag)
    view = await detail(client, agent, inquiry)
    body = body_from(
        view, evidence={"docs": [{"doc_id": "DOC-001", "version": 1}], "code": []}
    )
    if kind == "version":
        assert (
            await create_document(
                client, agent, key="version2", doc_id="DOC-001", version=2
            )
        ).status_code == 202
    else:
        payload = {
            "status": "폐기" if kind == "retire" else "유효",
            "review_required": kind == "review",
            "reason": "담당자 확인",
        }
        url = "/api/v1/admin/documents/DOC-001/versions/1/status"
        assert (
            await client.patch(url, headers=agent["headers"], json=payload)
        ).status_code == 200
        changed = (await detail(client, agent, inquiry))["backend_knowledge_revision"]
        assert changed > view["backend_knowledge_revision"]
        assert (
            await client.patch(url, headers=agent["headers"], json=payload)
        ).status_code == 200
        assert (await detail(client, agent, inquiry))[
            "backend_knowledge_revision"
        ] == changed
    assert (await send(client, agent, inquiry, body)).json()["error"][
        "code"
    ] == "STALE_KNOWLEDGE"
    body["expected_backend_knowledge_revision"] = (
        await detail(client, agent, inquiry)
    )["backend_knowledge_revision"]
    assert (await send(client, agent, inquiry, body)).json()["error"][
        "code"
    ] == "STALE_EVIDENCE"


async def test_foreign_and_unretrieved_evidence_and_closed_answers(
    client, agent, foreign_agent, inquiry, session_factory, rag, ai
):
    await create_document(client, foreign_agent)
    await drain(session_factory, rag)
    view, _ = await with_draft(client, agent, inquiry, session_factory, ai)
    foreign = {"docs": [{"doc_id": "DOC-001", "version": 1}], "code": []}
    assert (
        await send(client, agent, inquiry, body_from(view, evidence=foreign))
    ).json()["error"]["code"] == "EVIDENCE_NOT_RETRIEVED"
    manual = body_from(view, draft_id=None, draft_revision=None, evidence=foreign)
    assert (await send(client, agent, inquiry, manual)).status_code == 404
    code = {"docs": [], "code": [{"path": "billing/pricing.ts", "commit_sha": None}]}
    assert (await send(client, agent, inquiry, body_from(view, evidence=code))).json()[
        "error"
    ]["code"] == "CODE_EVIDENCE_UNVERIFIED"
    assert (await send(client, agent, inquiry, body_from(view))).status_code == 201
    assert (await close(client, agent, inquiry, 2)).status_code == 200
    assert (
        await send(
            client,
            agent,
            inquiry,
            body_from(await detail(client, agent, inquiry)),
            "closed",
        )
    ).json()["error"]["code"] == "INQUIRY_CLOSED"
    classified = await client.patch(
        f"{ADMIN}/{inquiry['inquiry_id']}/classification",
        headers=agent["headers"],
        json={
            "expected_revision": 3,
            "category": "기타",
            "urgency": "낮음",
            "reason": "닫힌 문의",
        },
    )
    assert classified.status_code == 409


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:5173",
        "http://localhost:3000",
        "https://moa-client.vercel.app",
        "https://moa-admin-teal.vercel.app",
    ],
)
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/admin/inquiries/Q-1001/answers",
        "/api/v1/admin/inquiries/Q-1001/close",
        "/api/v1/inquiries/Q-1001/feedback",
    ],
)
async def test_answer_api_preflight(client, origin, path):
    response = await client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
        },
    )
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == origin
    assert "idempotency-key" in response.headers["Access-Control-Allow-Headers"].lower()


async def test_answer_id_exhaustion_rolls_back_message_and_idempotency(
    client, agent, inquiry, session_factory
):
    payload = body_from(await detail(client, agent, inquiry))
    async with session_factory() as s:
        await s.execute(text("SELECT setval('public_answer_seq', 9999, true)"))
        await s.commit()
    failed = await send(client, agent, inquiry, payload)
    assert (
        failed.status_code == 503
        and failed.json()["error"]["code"] == "ID_SPACE_EXHAUSTED"
    )
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Message)) == 2
        assert await s.scalar(select(func.count()).select_from(Answer)) == 0
        row = await s.scalar(select(Inquiry))
        assert row.revision == 1 and row.last_message_sequence == 2
        assert not await s.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM idempotency_records WHERE path LIKE '%/answers')"
            )
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"answer_text": "   "},
        {"answer_text": "x" * 10001},
        {"draft_id": "DRAFT-000001", "draft_revision": None},
        {"expected_inquiry_revision": True},
        {"expected_backend_knowledge_revision": -1},
    ],
)
async def test_answer_request_validation(client, agent, inquiry, changes):
    payload = body_from(await detail(client, agent, inquiry), **changes)
    assert (await send(client, agent, inquiry, payload)).status_code == 400


async def test_chat_read_and_feedback_are_consistent(
    client, customer, agent, inquiry, monkeypatch
):
    import app.services.answers as service

    await send(client, agent, inquiry, body_from(await detail(client, agent, inquiry)))
    await close(client, agent, inquiry, 2)
    entered, release, done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = service.feedback_state

    async def delayed(session, row):
        entered.set()
        await release.wait()
        return await original(session, row)

    monkeypatch.setattr(service, "feedback_state", delayed)
    reading = asyncio.create_task(
        client.get(f"{CUSTOMER}/{inquiry['inquiry_id']}", headers=customer["headers"])
    )
    await asyncio.wait_for(entered.wait(), 3)

    async def submit_rating():
        response = await feedback(client, customer, inquiry, {"resolved": True})
        done.set()
        return response

    writing = asyncio.create_task(submit_rating())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(done.wait()), 0.1)
    finally:
        release.set()
    before = (await asyncio.wait_for(reading, 3)).json()
    assert before["resolution_status"] == "미확인" and before["feedback"] is None
    assert (await asyncio.wait_for(writing, 3)).status_code == 201
    after = (
        await client.get(
            f"{CUSTOMER}/{inquiry['inquiry_id']}", headers=customer["headers"]
        )
    ).json()
    assert (
        after["resolution_status"] == "해결 확인"
        and after["feedback"]["resolved"] is True
    )
