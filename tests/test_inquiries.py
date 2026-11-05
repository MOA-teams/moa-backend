import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.cursor import encode_cursor
from app.core.enums import InquiryStatus
from app.db.models import Attachment, IdempotencyRecord, Inquiry, Message
from app.schemas.auth import AuthenticatedUser
from app.schemas.inquiries import InquiryCreateRequest
from app.services.inquiries import create_inquiry

pytestmark = pytest.mark.anyio
BASE = "/api/v1/inquiries"


def writing(actor, key=None):
    return {**actor["headers"], "Idempotency-Key": key or uuid4().hex}


async def test_creation_persists_ordered_messages_and_english_values(client, customer, session_factory):
    response = await client.post(BASE, headers=writing(customer), json={
        "message": "  오류가 나요  ", "customer_category": "bug", "env": {"os": "iOS 18", "error_code": "E-502"},
    })
    assert response.status_code == 201
    body = response.json()
    assert type(body["inquiry_id"]) is int and body["inquiry_id"] == 1
    assert body["status"] == "received"
    assert body["auto_message"]["sender"] == "ai_auto"
    assert body["auto_message"]["message_type"] == "acknowledgement"
    async with session_factory() as session:
        inquiry = await session.get(Inquiry, body["inquiry_id"])
        assert (inquiry.customer_id, inquiry.tenant_id, inquiry.revision, inquiry.last_message_sequence) == (customer["user_id"], "alpha", 1, 2)
        assert inquiry.category is None and inquiry.urgency is None
        assert inquiry.title == "오류가 나요"
        assert inquiry.created_at.utcoffset() is not None
        messages = (await session.scalars(select(Message).order_by(Message.sequence))).all()
        assert [(m.sequence, m.text) for m in messages] == [(1, "  오류가 나요  "), (2, body["auto_message"]["text"])]
        assert await session.scalar(text("SELECT status::text FROM inquiries")) == "received"
        assert await session.scalar(text("SELECT customer_category::text FROM inquiries")) == "bug"


@pytest.mark.parametrize("payload", [
    {}, {"message": ""}, {"message": " \n "}, {"message": "a" * 10001},
    {"message": "x", "customer_category": "기능 오류"}, {"message": "x", "customer_category": "BUG"},
    {"message": "x", "tenant_id": "beta"}, {"message": "x", "customer_id": 999},
    {"message": "x", "created_at": "2026-11-05T12:00:00+09:00"},
    {"message": "x", "attachments": [1, 1]}, {"message": "x", "attachments": [True]},
    {"message": "x", "attachments": ["1"]}, {"message": "x", "attachments": [-1]},
    {"message": "x", "env": {"unknown": "value"}},
])
async def test_create_validation(client, customer, payload):
    response = await client.post(BASE, headers=writing(customer), json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("key", [None, "", "with spaces", "x" * 201])
async def test_idempotency_header_required_and_validated(client, customer, key):
    headers = customer["headers"] if key is None else writing(customer, key)
    if key == "":
        headers = {**customer["headers"], "Idempotency-Key": ""}
    response = await client.post(BASE, headers=headers, json={"message": "hi"})
    assert response.status_code == 400


async def test_idempotent_creation_and_conflict(client, customer, session_factory):
    headers = writing(customer, "same")
    first = await client.post(BASE, headers=headers, json={"message": "hi"})
    second = await client.post(BASE, headers=headers, json={"message": "hi"})
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    conflict = await client.post(BASE, headers=headers, json={"message": "changed"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Inquiry)) == 1
        assert await session.scalar(select(func.count()).select_from(Message)) == 2
        record = await session.scalar(select(IdempotencyRecord))
        assert record.key_hash != "same" and len(record.key_hash) == 64


async def test_same_key_is_scoped_by_user(client, customer, other_customer):
    responses = [await client.post(BASE, headers=writing(actor, "shared"), json={"message": "hi"}) for actor in (customer, other_customer)]
    assert all(response.status_code == 201 for response in responses)
    assert responses[0].json()["inquiry_id"] != responses[1].json()["inquiry_id"]


async def test_concurrent_duplicate_creation_has_one_inquiry(client, customer, session_factory):
    responses = await asyncio.gather(*[
        client.post(BASE, headers=writing(customer, "race"), json={"message": "동시 문의"}) for _ in range(4)
    ])
    assert all(response.status_code == 201 for response in responses)
    assert all(response.json() == responses[0].json() for response in responses)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Inquiry)) == 1
        assert await session.scalar(select(func.count()).select_from(Message)) == 2


async def test_concurrent_same_key_different_body_commits_one(client, customer):
    responses = await asyncio.gather(*[
        client.post(BASE, headers=writing(customer, "race"), json={"message": value}) for value in ("one", "two")
    ])
    assert sorted(response.status_code for response in responses) == [201, 409]


async def test_append_increments_revision_and_replay_does_not(client, customer, inquiry, session_factory):
    inquiry_id = inquiry["inquiry_id"]
    path = f"{BASE}/{inquiry_id}/messages"
    response = await client.post(path, headers=writing(customer, "append"), json={"text": "  추가 내용  "})
    assert response.status_code == 201
    assert response.json()["inquiry_revision"] == 2
    replay = await client.post(path, headers=writing(customer, "append"), json={"text": "  추가 내용  "})
    assert replay.json() == response.json()
    conflict = await client.post(path, headers=writing(customer, "append"), json={"text": "other"})
    assert conflict.status_code == 409
    async with session_factory() as session:
        row = await session.get(Inquiry, inquiry_id)
        assert row.revision == 2 and row.last_message_sequence == 3
        assert await session.scalar(select(Message.text).where(Message.message_id == response.json()["message_id"])) == "  추가 내용  "


@pytest.mark.parametrize("payload", [{}, {"text": " "}, {"text": "x", "sender": "agent"}, {"text": "x", "sender": "ai_auto"}, {"text": "x", "attachments": [1, 1]}])
async def test_append_validation(client, customer, inquiry, payload):
    response = await client.post(f"{BASE}/{inquiry['inquiry_id']}/messages", headers=writing(customer), json=payload)
    assert response.status_code == 400


async def test_concurrent_appends_have_contiguous_committed_order(client, customer, inquiry, session_factory):
    inquiry_id = inquiry["inquiry_id"]
    responses = await asyncio.gather(*[
        client.post(f"{BASE}/{inquiry_id}/messages", headers=writing(customer, f"parallel-{i}"), json={"text": str(i)})
        for i in range(8)
    ])
    assert all(response.status_code == 201 for response in responses)
    assert sorted(response.json()["inquiry_revision"] for response in responses) == list(range(2, 10))
    async with session_factory() as session:
        sequences = (await session.scalars(select(Message.sequence).where(Message.inquiry_id == inquiry_id).order_by(Message.sequence))).all()
        assert sequences == list(range(1, 11))


async def test_cursor_pages_empty_poll_and_new_message(client, customer, inquiry):
    path = f"{BASE}/{inquiry['inquiry_id']}"
    first = (await client.get(path, headers=customer["headers"], params={"limit": 1})).json()
    assert len(first["messages"]) == 1 and first["has_more"]
    second = (await client.get(path, headers=customer["headers"], params={"cursor": first["next_cursor"], "limit": 1})).json()
    assert not second["has_more"] and second["messages"][0]["message_type"] == "acknowledgement"
    assert first["messages"][0]["message_id"] != second["messages"][0]["message_id"]
    empty = (await client.get(path, headers=customer["headers"], params={"cursor": second["next_cursor"]})).json()
    assert empty["messages"] == [] and empty["next_cursor"] == second["next_cursor"]
    assert not {"draft", "category", "urgency", "verification", "customer_id", "tenant_id"} & set(empty)
    created = await client.post(f"{path}/messages", headers=writing(customer), json={"text": "new"})
    fresh = (await client.get(path, headers=customer["headers"], params={"cursor": empty["next_cursor"]})).json()
    assert [m["message_id"] for m in fresh["messages"]] == [created.json()["message_id"]]


async def test_poll_during_concurrent_appends_has_no_loss(client, customer, inquiry):
    path = f"{BASE}/{inquiry['inquiry_id']}"
    initial = (await client.get(path, headers=customer["headers"])).json()
    pending = asyncio.gather(*[
        client.post(f"{path}/messages", headers=writing(customer, f"stream-{i}"), json={"text": str(i)}) for i in range(10)
    ])
    cursor = initial["next_cursor"]
    seen = []
    while not pending.done():
        page = (await client.get(path, headers=customer["headers"], params={"cursor": cursor, "limit": 2})).json()
        seen.extend(m["message_id"] for m in page["messages"])
        cursor = page["next_cursor"]
    responses = await pending
    while True:
        page = (await client.get(path, headers=customer["headers"], params={"cursor": cursor, "limit": 2})).json()
        seen.extend(m["message_id"] for m in page["messages"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert len(seen) == len(set(seen)) == 10
    assert set(seen) == {response.json()["message_id"] for response in responses}


@pytest.mark.parametrize("cursor", ["not-a-cursor", "eA.eA", "x" * 2049])
async def test_invalid_cursor(client, customer, inquiry, cursor):
    response = await client.get(f"{BASE}/{inquiry['inquiry_id']}", headers=customer["headers"], params={"cursor": cursor})
    assert response.status_code == 400


async def test_cursor_cannot_be_reused_for_another_inquiry_or_admin(client, customer, inquiry, agent):
    path = f"{BASE}/{inquiry['inquiry_id']}"
    cursor = (await client.get(path, headers=customer["headers"])).json()["next_cursor"]
    other = (await client.post(BASE, headers=writing(customer), json={"message": "other"})).json()
    assert (await client.get(f"{BASE}/{other['inquiry_id']}", headers=customer["headers"], params={"cursor": cursor})).status_code == 400
    assert (await client.get(f"/api/v1/admin/inquiries/{inquiry['inquiry_id']}", headers=agent["headers"], params={"cursor": cursor})).status_code == 400
    encoded, signature = cursor.split(".")
    changed = ("A" if encoded[0] != "A" else "B") + encoded[1:] + "." + signature
    assert (await client.get(path, headers=customer["headers"], params={"cursor": changed})).status_code == 400


async def test_signed_cursor_invalid_position_is_rejected(client, customer, inquiry):
    scope = {"type": "messages", "tenant": "alpha", "inquiry": inquiry["inquiry_id"], "user": customer["user_id"]}
    cursor = encode_cursor(scope, {"after": -1})
    assert (await client.get(f"{BASE}/{inquiry['inquiry_id']}", headers=customer["headers"], params={"cursor": cursor})).status_code == 400


async def test_ownership_and_tenant_isolation(client, customer, inquiry, other_customer, foreign_customer):
    path = f"{BASE}/{inquiry['inquiry_id']}"
    for actor in (other_customer, foreign_customer):
        assert (await client.get(path, headers=actor["headers"])).status_code == 404
        assert (await client.post(f"{path}/messages", headers=writing(actor), json={"text": "forged"})).status_code == 404
    assert (await client.get(path, headers={**other_customer["headers"], "X-User-ID": str(customer["user_id"])})).status_code == 404


async def test_role_and_unassigned_and_authentication(client, make_actor):
    assert (await client.post(BASE, headers={"Idempotency-Key": "a"}, json={"message": "hi"})).status_code == 401
    for role in ("agent", "admin"):
        actor = await make_actor(role, role)
        assert (await client.post(BASE, headers=writing(actor), json={"message": "hi"})).status_code == 403
    actor = await make_actor("unassigned", tenant_id=None)
    assert (await client.post(BASE, headers=writing(actor), json={"message": "hi"})).status_code == 403


async def test_closed_inquiry_rejected_and_answered_reopened(client, customer, inquiry, session_factory):
    inquiry_id = inquiry["inquiry_id"]
    async with session_factory() as session:
        row = await session.get(Inquiry, inquiry_id)
        row.status = InquiryStatus.CLOSED
        await session.commit()
    response = await client.post(f"{BASE}/{inquiry_id}/messages", headers=writing(customer, "closed"), json={"text": "x"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "INQUIRY_CLOSED"
    async with session_factory() as session:
        row = await session.get(Inquiry, inquiry_id)
        row.status = InquiryStatus.ANSWERED
        await session.commit()
    response = await client.post(f"{BASE}/{inquiry_id}/messages", headers=writing(customer, "closed"), json={"text": "x"})
    assert response.status_code == 201 and response.json()["status"] == "received"


async def test_failed_creation_rolls_back_all_writes_and_allows_retry(client, customer, session_factory):
    async with session_factory() as session:
        await session.execute(text("ALTER TABLE messages ADD CONSTRAINT test_reject_ack CHECK (message_type <> 'acknowledgement')"))
        await session.commit()
    user = AuthenticatedUser(user_id=customer["user_id"], email="customer@example.com", role="customer", tenant_id="alpha")
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await create_inquiry(session, user=user, payload=InquiryCreateRequest(message="atomic"), key="retry")
        for model in (Inquiry, Message, IdempotencyRecord):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        await session.execute(text("ALTER TABLE messages DROP CONSTRAINT test_reject_ack"))
        await session.commit()
    response = await client.post(BASE, headers=writing(customer, "retry"), json={"message": "atomic"})
    assert response.status_code == 201


@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 101}, {"limit": "bad"}])
async def test_page_size_validation(client, customer, inquiry, query):
    assert (await client.get(f"{BASE}/{inquiry['inquiry_id']}", headers=customer["headers"], params=query)).status_code == 400
