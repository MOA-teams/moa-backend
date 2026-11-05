import asyncio

import pytest
from sqlalchemy import func, select, text

from app.db.models import AnalysisDraft, AnalysisRequest, Answer, Document, DocumentVersion, Inquiry, Message, ReviewItem
from app.services.review_discovery import discover_existing
from app.workers.reviews import run_once
from test_answers import with_draft, detail, body_from, send
from test_documents import rag, create, drain
from test_pipeline import ai, submit, CALLBACK, callback_headers

pytestmark = pytest.mark.anyio
BASE = "/api/v1/admin/reviews"


async def reviews(client, actor, **params):
    response = await client.get(BASE, headers=actor["headers"], params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def decision(client, actor, row, action="approve", key="review", **body):
    return await client.post(f"{BASE}/{row['review_id']}/decisions",
        headers={**actor["headers"], "Idempotency-Key": key},
        json={"action": action, "expected_revision": row["revision"], **body})


async def pending_draft(client, agent, inquiry, session_factory, ai, rag):
    assert (await create(client, agent)).status_code == 202
    await drain(session_factory, rag)
    await with_draft(client, agent, inquiry, session_factory, ai, {"docs": [{"doc_id": "DOC-001", "version": 1}], "code": []})
    response = await create(client, agent, key="v2", doc_id="DOC-001", version=2, text="새 정책은 3일입니다.")
    assert response.status_code == 202, response.text
    row = (await reviews(client, agent))["items"][0]
    assert row["target_type"] == "draft" and row["excluded_from_search"] is None
    return row


async def ready_draft(client, agent, inquiry, session_factory, ai, rag):
    await pending_draft(client, agent, inquiry, session_factory, ai, rag)
    await run_once(session_factory)
    assert (await reviews(client, agent))["items"][0]["suggestion_status"] == "waiting_for_sources"
    await drain(session_factory, rag)
    await run_once(session_factory)
    run = await submit(session_factory, ai)
    assert ai.runs[run]["review_only"] is True
    body = ai.result(run)
    body["draft"]["answer_text"] = "변경된 정책은 3일입니다."
    body["draft"]["evidence"] = {"docs": [{"doc_id": "DOC-001", "version": 2}], "code": []}
    response = await client.post(CALLBACK, headers=callback_headers(run), json=body)
    assert response.status_code == 200, response.text
    await run_once(session_factory)
    row = (await reviews(client, agent))["items"][0]
    assert row["suggestion_status"] == "ready", row
    return row


async def pending_document(client, agent, session_factory, rag, key="doc"):
    doc = (await create(client, agent, key=key)).json()
    await drain(session_factory, rag)
    response = await client.patch(f"/api/v1/admin/documents/{doc['doc_id']}/versions/1/status",
        headers=agent["headers"], json={"status": "유효", "review_required": True, "reason": "정책 검토"})
    assert response.status_code == 200, response.text
    return next(x for x in (await reviews(client, agent))["items"] if x["target_id"] == doc["doc_id"])


async def test_draft_review_sends_once_preserves_original_and_same_transaction(client, agent, customer, inquiry, session_factory, ai, rag):
    row = await ready_draft(client, agent, inquiry, session_factory, ai, rag)
    async with session_factory() as s:
        original = (await s.scalar(select(AnalysisDraft).where(AnalysisDraft.public_id == row["target_id"]))).content
        assert await s.scalar(select(func.count()).select_from(Answer)) == 0
    responses = await asyncio.gather(*(decision(client, agent, row) for _ in range(3)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert responses[0].json() == responses[1].json()
    assert responses[0].json()["result"]["answer_id"]
    assert (await decision(client, agent, row, key="another")).status_code == 409
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Answer)) == 1
        assert (await s.scalar(select(AnalysisDraft).where(AnalysisDraft.public_id == row["target_id"]))).content == original
        assert (await s.scalar(select(ReviewItem))).status == "resolved"
    chat = await client.get(f"/api/v1/inquiries/{inquiry['inquiry_id']}", headers=customer["headers"])
    assert chat.json()["messages"][-1]["text"] == row["suggested_text"]


async def test_document_edit_creates_version_and_search_restores_later(client, agent, session_factory, rag):
    row = await pending_document(client, agent, session_factory, rag)
    assert row["suggested_text"] is None and row["excluded_from_search"]
    assert (await decision(client, agent, row)).status_code == 409
    response = await decision(client, agent, row, "edit", text="담당자가 확인한 최신 정책입니다.")
    assert response.status_code == 200, response.text
    assert response.json()["result"]["version"] == 2
    assert response.json()["result"]["excluded_from_search"] is True
    async with session_factory() as s:
        versions = (await s.scalars(select(DocumentVersion).order_by(DocumentVersion.version))).all()
        assert versions[0].original_text != versions[1].original_text
        assert versions[0].status == "폐기" and not versions[1].searchable
    await drain(session_factory, rag)
    done = (await reviews(client, agent, status="resolved"))["items"][0]
    assert done["excluded_from_search"] is False
    assert (await decision(client, agent, row, "edit", text="담당자가 확인한 최신 정책입니다.")).json() == response.json()


async def test_dismiss_never_sends_or_restores_and_isolation_cursor(client, agent, foreign_agent, customer, make_actor, session_factory, rag):
    first = await pending_document(client, agent, session_factory, rag)
    second = await pending_document(client, agent, session_factory, rag, "second")
    page = await reviews(client, agent, limit=1)
    assert page["has_more"]
    end = await reviews(client, agent, limit=1, cursor=page["next_cursor"])
    assert not end["has_more"] and end["next_cursor"] is None
    other = await make_actor("other-agent", "agent")
    for actor in (other, foreign_agent):
        assert (await client.get(BASE, params={"cursor": page["next_cursor"]}, headers=actor["headers"])).status_code == 400
    assert (await reviews(client, foreign_agent))["items"] == []
    assert (await decision(client, foreign_agent, first, "dismiss")).status_code == 404
    assert (await decision(client, customer, first, "dismiss")).status_code == 403
    assert (await client.get(BASE)).status_code == 401
    assert (await decision(client, agent, first, "dismiss")).status_code == 200
    await run_once(session_factory)
    assert len((await reviews(client, agent))["items"]) == 1
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Answer)) == 0
        v = await s.scalar(select(DocumentVersion).where(DocumentVersion.doc_id == 1))
        assert v.review_required and not v.searchable


@pytest.mark.parametrize("change", ["message", "analysis", "document", "normal_send"])
async def test_stale_review_never_sends(client, agent, customer, inquiry, session_factory, ai, rag, change):
    row = await ready_draft(client, agent, inquiry, session_factory, ai, rag)
    if change == "message":
        assert (await client.post(f"/api/v1/inquiries/{inquiry['inquiry_id']}/messages",
            headers={**customer["headers"], "Idempotency-Key": "message"}, json={"text": "새로운 증상입니다."})).status_code == 201
    elif change == "document":
        assert (await create(client, agent, key="v3", doc_id="DOC-001", version=3, text="다시 변경")).status_code == 202
    elif change == "analysis":
        from app.services.analysis import enqueue_analysis, lock_inquiry
        async with session_factory() as s:
            inquiry_row = await s.scalar(select(Inquiry))
            await enqueue_analysis(s, await lock_inquiry(s, inquiry_row.inquiry_id), review_only=True)
            await s.commit()
    else:
        assert (await send(client, agent, inquiry, body_from(await detail(client, agent, inquiry)))).status_code == 201
    response = await decision(client, agent, row)
    assert response.status_code == 409, response.text
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Answer)) == (1 if change == "normal_send" else 0)


async def test_bulk_partial_replay_and_concurrent_regular_delivery(client, agent, inquiry, session_factory, ai, rag):
    row = await ready_draft(client, agent, inquiry, session_factory, ai, rag)
    doc = await pending_document(client, agent, session_factory, rag, "another-doc")
    # 다른 문서 변경도 knowledge revision을 바꾸므로 수정안은 한 번 더 새 지식 기준으로 생성한다.
    await run_once(session_factory)
    run = await submit(session_factory, ai)
    result = ai.result(run)
    result["draft"]["evidence"] = {"docs": [{"doc_id": "DOC-001", "version": 2}], "code": []}
    await client.post(CALLBACK, headers=callback_headers(run), json=result)
    await run_once(session_factory)
    row = next(r for r in (await reviews(client, agent))["items"] if r["target_type"] == "draft")
    body = {"items": [{"review_id": r["review_id"], "expected_revision": r["revision"]} for r in (row, doc)]}
    headers = {**agent["headers"], "Idempotency-Key": "bulk"}
    first = await client.post(BASE + "/bulk-approve", headers=headers, json=body)
    assert first.status_code == 200, first.text
    assert [r["success"] for r in first.json()["items"]] == [True, False]
    assert first.json()["items"][1]["error_code"] == "REVIEW_SUGGESTION_UNAVAILABLE"
    assert (await client.post(BASE + "/bulk-approve", headers=headers, json=body)).json() == first.json()
    retried = await client.post(BASE + "/bulk-approve", headers={**headers, "Idempotency-Key": "new"}, json=body)
    assert not any(r["success"] for r in retried.json()["items"])
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Answer)) == 1


async def test_manual_review_and_regular_send_race_share_lock(client, agent, inquiry, session_factory, ai, rag):
    row = await pending_draft(client, agent, inquiry, session_factory, ai, rag)
    view = await detail(client, agent, inquiry)
    normal = body_from(view, draft_id=None, draft_revision=None, evidence={"docs": [], "code": []})
    a, b = await asyncio.gather(decision(client, agent, row, "edit", text="담당자가 직접 확인했습니다."),
                               send(client, agent, inquiry, normal))
    assert sorted([a.status_code, b.status_code]) in ([200, 409], [201, 409]), (a.text, b.text)
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Answer)) == 1


async def test_unsafe_text_rollback_and_existing_state_backfill(client, agent, inquiry, session_factory, ai, rag):
    row = await pending_draft(client, agent, inquiry, session_factory, ai, rag)
    result = await decision(client, agent, row, "edit", text="billing/pricing.ts를 수정합니다.")
    assert result.status_code == 400, result.text
    async with session_factory() as s:
        assert (await s.scalar(select(ReviewItem))).status == "pending"
        assert await s.scalar(select(func.count()).select_from(Answer)) == 0
        await s.execute(text("DELETE FROM review_items"))
        await discover_existing(s, "alpha")
        await s.commit()
        restored = await s.scalar(select(ReviewItem))
        assert restored.source == "existing_state" and restored.push_event_id is None


async def test_review_input_and_cursor_boundaries(client, agent):
    for params in ({"limit": 0}, {"limit": 101}, {"status": "invalid"}, {"cursor": "tampered"}):
        assert (await client.get(BASE, params=params, headers=agent["headers"])).status_code == 400
    item = {"review_id": "REV-" + "a" * 32, "expected_revision": 1}
    for items in ([], [item] * 2, [item] * 51):
        assert (await client.post(BASE + "/bulk-approve", headers={**agent["headers"], "Idempotency-Key": "invalid"}, json={"items": items})).status_code == 400
    assert (await client.post(BASE + "/bulk-approve", headers=agent["headers"], json={"items": [item]})).status_code == 400


async def test_review_migration_roundtrip_preserves_existing_data(db_engine, migrate, inquiry, session_factory):
    async with session_factory() as s:
        before = (await s.execute(select(Inquiry.public_id, Inquiry.created_at, Inquiry.revision))).one()
    async with db_engine.begin() as c:
        await c.run_sync(migrate, "downgrade", "0015_tenant_response_policies")
        await c.run_sync(migrate, "upgrade", "head")
    async with session_factory() as s:
        assert (await s.execute(select(Inquiry.public_id, Inquiry.created_at, Inquiry.revision))).one() == before
        assert await s.scalar(select(func.count()).select_from(ReviewItem)) == 0


@pytest.mark.parametrize("failure", ["ai_failed", "no_evidence", "indexing_failed"])
async def test_suggestion_failure_keeps_review_pending_without_sending(client, agent, inquiry, session_factory, ai, rag, failure):
    await pending_draft(client, agent, inquiry, session_factory, ai, rag)
    if failure == "indexing_failed":
        rag.fail_view = True
        await drain(session_factory, rag)
    else:
        await drain(session_factory, rag)
    await run_once(session_factory)
    if failure != "indexing_failed":
        run = await submit(session_factory, ai)
        if failure == "ai_failed":
            ai.result_status = "실패"
        else:
            ai.draft = False
        result = ai.result(run)
        assert (await client.post(CALLBACK, headers=callback_headers(run), json=result)).status_code == 200
        await run_once(session_factory)
    row = (await reviews(client, agent))["items"][0]
    assert row["status"] == "pending" and row["suggested_text"] is None and row["suggestion_error"]
    assert (await decision(client, agent, row)).status_code == 409
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Answer)) == 0


async def test_bulk_same_key_concurrency_and_rollback_on_evidence_failure(client, agent, inquiry, session_factory, ai, rag):
    row = await ready_draft(client, agent, inquiry, session_factory, ai, rag)
    body = {"items": [{"review_id": row["review_id"], "expected_revision": row["revision"]}]}
    headers = {**agent["headers"], "Idempotency-Key": "parallel-bulk"}
    rag.fail_view = True
    failed = await client.post(BASE + "/bulk-approve", headers=headers, json=body)
    assert failed.json()["items"][0]["error_code"] == "EVIDENCE_CHECK_UNAVAILABLE"
    async with session_factory() as s:
        assert (await s.scalar(select(ReviewItem))).status == "pending"
        assert await s.scalar(select(func.count()).select_from(Answer)) == 0
    rag.fail_view = False
    assert (await client.post(BASE + "/bulk-approve", headers=headers, json=body)).json() == failed.json()
    headers["Idempotency-Key"] = "retry-bulk"
    results = await asyncio.gather(*(client.post(BASE + "/bulk-approve", headers=headers, json=body) for _ in range(3)))
    assert all(r.status_code == 200 and r.json()["items"][0]["success"] for r in results), [r.text for r in results]
    assert results[0].json() == results[1].json()
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Answer)) == 1


async def test_swagger_review_contract():
    from app.main import app
    schema = app.openapi()
    operation = schema["paths"][BASE + "/{review_id}/decisions"]["post"]
    assert "발송" in operation["description"] and "Idempotency-Key" in operation["description"]
    assert operation["security"]
    assert schema["components"]["schemas"]["ReviewDecision"]["properties"]["action"]["enum"] == ["approve", "edit", "dismiss"]
    assert "inquiries" in schema["components"]["schemas"]["GroupDetail"]["properties"]


async def test_document_review_fk_and_inquiry_writer_do_not_deadlock(client, agent, inquiry, session_factory, ai, rag):
    await pending_draft(client, agent, inquiry, session_factory, ai, rag)
    from app.services.analysis import lock_inquiry
    from app.services.knowledge import lock_knowledge
    from app.services.review_discovery import capture_document
    async with session_factory() as s:
        await s.execute(text("DELETE FROM review_items"))
        inquiry_id = await s.scalar(select(Inquiry.inquiry_id))
        await s.commit()
    inquiry_locked, knowledge_locked = asyncio.Event(), asyncio.Event()

    async def writer():
        async with session_factory() as s:
            await lock_inquiry(s, inquiry_id)
            inquiry_locked.set()
            await knowledge_locked.wait()
            await lock_knowledge(s, "alpha")
            await s.commit()

    async def change():
        await inquiry_locked.wait()
        async with session_factory() as s:
            await lock_knowledge(s, "alpha")
            knowledge_locked.set()
            doc = await s.scalar(select(Document))
            version = await s.scalar(select(DocumentVersion).where(DocumentVersion.version == 2))
            await capture_document(s, doc, version)
            await s.commit()

    await asyncio.wait_for(asyncio.gather(writer(), change()), timeout=5)
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(ReviewItem)) == 1
