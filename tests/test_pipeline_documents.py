import asyncio
import pytest
from sqlalchemy import select, func
from app.db.models import AnalysisDraft, Message
from test_documents import rag, create, drain
from test_pipeline import ai, submit, callback_headers, ADMIN, CALLBACK

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "mode", ["valid", "retired", "new_version", "missing", "foreign"]
)
async def test_draft_evidence_uses_current_tenant_document_version(
    client, agent, foreign_agent, inquiry, session_factory, ai, rag, mode
):
    owner = foreign_agent if mode == "foreign" else agent
    created = await create(client, owner)
    assert created.status_code == 202 and created.json()["doc_id"] == "DOC-001"
    await drain(session_factory, rag)
    run = await submit(session_factory, ai)
    if mode == "retired":
        response = await client.patch(
            "/api/v1/admin/documents/DOC-001/versions/1/status",
            headers=agent["headers"],
            json={"status": "폐기", "review_required": False, "reason": "잘못된 정책"},
        )
        assert response.status_code == 200
    if mode == "new_version":
        assert (
            await create(client, agent, key="v2", doc_id="DOC-001", version=2)
        ).status_code == 202
    ai.draft = True
    body = ai.result(run)
    body["draft"]["evidence"]["docs"][0]["doc_id"] = (
        "DOC-999" if mode == "missing" else "DOC-001"
    )
    responses = await asyncio.gather(
        *[
            client.post(CALLBACK, headers=callback_headers(run), json=body)
            for _ in range(2)
        ]
    )
    assert all(r.status_code == 200 for r in responses)
    detail = (
        await client.get(f"{ADMIN}/{inquiry['inquiry_id']}", headers=agent["headers"])
    ).json()
    assert (
        detail["draft"]["draft_id"] == "DRAFT-000001"
        and detail["draft_result"] == "초안 가능"
    )
    assert bool(detail["draft"]["review_reasons"]) == (mode != "valid")
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(AnalysisDraft)) == 1
        assert await s.scalar(select(func.count()).select_from(Message)) == 2


async def test_doc_attachment_external_id_roundtrip(
    client, agent, session_factory, rag, fake_storage
):
    upload = await client.post(
        "/api/v1/attachments",
        headers=agent["headers"],
        data={"purpose": "document"},
        files={"file": ("x.pdf", b"%PDF-test", "application/pdf")},
    )
    aid = upload.json()["attachment_id"]
    response = await create(client, agent, text=None, attachment_id=aid)
    assert response.status_code == 202
    item = (
        await client.get("/api/v1/admin/documents", headers=agent["headers"])
    ).json()["items"][0]
    assert (
        item["attachment_id"] == aid
        and item["doc_id"] == "DOC-001"
        and item["doc_type"] == "정책"
    )
