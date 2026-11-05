import asyncio
from datetime import datetime

import pytest
from sqlalchemy import func, inspect, select, text

from app.core.enums import InquiryStatus, MessageSender, MessageType
from app.db.models import AnalysisRequest, Inquiry, InquirySessionEvent, Message
from conftest import get_row

pytestmark = pytest.mark.anyio
CUSTOMER = "/api/v1/inquiries"
ADMIN = "/api/v1/admin/inquiries"


async def test_initial_presence_and_read_fields_are_read_only(client, customer, agent, inquiry, session_factory):
    path = inquiry["inquiry_id"]
    async with session_factory() as session:
        row = await get_row(session, Inquiry, path)
        updated_at = row.updated_at
    detail = (await client.get(f"{ADMIN}/{path}", headers=agent["headers"])).json()
    assert detail["customer_presence"] == {"status": "상담 중", "left_at": None, "left_reason": None}
    assert detail["customer_last_read_message_id"] is None
    chat = (await client.get(f"{CUSTOMER}/{path}", headers=customer["headers"])).json()
    assert chat["agent_last_read_message_id"] is None
    assert not {"customer_presence", "customer_last_read_message_id"} & chat.keys()
    async with session_factory() as session:
        row = await get_row(session, Inquiry, path)
        assert row.updated_at == updated_at
        assert row.agent_last_read_sequence == row.customer_last_read_sequence == 0
        assert await session.scalar(select(func.count()).select_from(InquirySessionEvent)) == 0


async def test_session_transitions_duplicates_and_no_business_side_effects(client, customer, agent, inquiry, session_factory):
    path = inquiry["inquiry_id"]
    async with session_factory() as session:
        row = await get_row(session, Inquiry, path)
        row.status = InquiryStatus.NEEDS_INFO
        await session.commit()
        await session.refresh(row)
        before = (row.revision, row.last_message_sequence, row.updated_at, row.latest_analysis_request_id, row.processing_stage)
        analysis = await session.scalar(select(AnalysisRequest))
        snapshot = analysis.snapshot
    url = f"{CUSTOMER}/{path}/session-events"
    first = await client.post(url, headers=customer["headers"], json={"type": "left"})
    assert first.status_code == 200
    presence = first.json()["customer_presence"]
    assert presence["status"] == "나감" and presence["left_reason"] == "left"
    assert datetime.fromisoformat(presence["left_at"]).utcoffset() is not None
    assert (await client.post(url, headers=customer["headers"], json={"type": "left"})).json() == first.json()
    new_chat = await client.post(url, headers=customer["headers"], json={"type": "new_chat"})
    assert new_chat.json()["customer_presence"]["left_reason"] == "new_chat"
    assert (await client.post(url, headers=customer["headers"], json={"type": "left"})).json() == new_chat.json()
    detail = (await client.get(f"{ADMIN}/{path}", headers=agent["headers"])).json()
    assert detail["customer_presence"] == new_chat.json()["customer_presence"]
    for _ in range(2):
        returned = await client.post(url, headers=customer["headers"], json={"type": "returned"})
        assert returned.json()["customer_presence"] == {"status": "상담 중", "left_at": None, "left_reason": None}
    assert (await client.post(url, headers=customer["headers"], json={"type": "left"})).status_code == 200
    async with session_factory() as session:
        row = await get_row(session, Inquiry, path)
        assert row.status == InquiryStatus.NEEDS_INFO
        assert (row.revision, row.last_message_sequence, row.updated_at, row.latest_analysis_request_id, row.processing_stage) == before
        assert await session.scalar(select(func.count()).select_from(Message)) == 2
        assert await session.scalar(select(func.count()).select_from(AnalysisRequest)) == 1
        assert (await session.scalar(select(AnalysisRequest))).snapshot == snapshot
        assert list((await session.scalars(select(InquirySessionEvent.type).order_by(InquirySessionEvent.event_id))).all()) == ["left", "new_chat", "returned", "left"]


async def test_bidirectional_read_receipts_and_empty_poll(client, customer, agent, inquiry, session_factory, make_actor):
    path = inquiry["inquiry_id"]
    async with session_factory() as session:
        row = await get_row(session, Inquiry, path)
        row.last_message_sequence += 1
        row.status = InquiryStatus.CLOSED
        reply = Message(inquiry_id=row.inquiry_id, sequence=row.last_message_sequence,
                        sender=MessageSender.AGENT, message_type=MessageType.FINAL_ANSWER, text="담당자 답변")
        session.add(reply)
        await session.commit()
        reply_id = reply.public_id
        await session.refresh(row)
        before = (row.revision, row.updated_at, row.latest_analysis_request_id)
    chat = (await client.get(f"{CUSTOMER}/{path}", headers=customer["headers"])).json()
    first_id = chat["messages"][0]["message_id"]
    customer_read = await client.post(f"{CUSTOMER}/{path}/read", headers=customer["headers"], json={"last_read_message_id": reply_id})
    assert customer_read.status_code == 200 and customer_read.json() == {"last_read_message_id": reply_id}
    # 읽음 위치는 상대 발신자만의 ID가 아니라, 화면에서 읽은 마지막 메시지의 경계다.
    admin = await make_actor("admin-reader", "admin")
    for actor in (agent, admin):
        response = await client.post(f"{ADMIN}/{path}/read", headers=actor["headers"], json={"last_read_message_id": first_id})
        assert response.status_code == 200 and response.json() == {"last_read_message_id": first_id}
    detail = (await client.get(f"{ADMIN}/{path}", headers=agent["headers"])).json()
    assert detail["customer_last_read_message_id"] == reply_id
    poll = (await client.get(f"{CUSTOMER}/{path}", headers=customer["headers"], params={"cursor": chat["next_cursor"]})).json()
    assert poll["messages"] == [] and poll["agent_last_read_message_id"] == first_id
    assert (await client.post(f"{CUSTOMER}/{path}/session-events", headers=customer["headers"], json={"type": "left"})).status_code == 200
    async with session_factory() as session:
        row = await get_row(session, Inquiry, path)
        assert row.status == InquiryStatus.CLOSED and row.last_message_sequence == 3
        assert (row.revision, row.updated_at, row.latest_analysis_request_id) == before
        assert await session.scalar(select(func.count()).select_from(AnalysisRequest)) == 1


@pytest.mark.parametrize("is_agent", [False, True])
async def test_reads_do_not_regress_under_duplicates_or_concurrency(client, customer, agent, inquiry, is_agent):
    path = inquiry["inquiry_id"]
    chat = (await client.get(f"{CUSTOMER}/{path}", headers=customer["headers"])).json()
    ids = [message["message_id"] for message in chat["messages"]]
    actor, base = (agent, ADMIN) if is_agent else (customer, CUSTOMER)
    url = f"{base}/{path}/read"
    responses = await asyncio.gather(*[
        client.post(url, headers=actor["headers"], json={"last_read_message_id": message_id})
        for message_id in [ids[1], ids[0], ids[1], ids[0]]
    ])
    assert all(response.status_code == 200 for response in responses)
    old = await client.post(url, headers=actor["headers"], json={"last_read_message_id": ids[0]})
    assert old.json() == {"last_read_message_id": ids[1]}
    detail = (await client.get(f"{ADMIN}/{path}", headers=agent["headers"])).json()
    field = "agent_last_read_message_id" if is_agent else "customer_last_read_message_id"
    assert detail[field] == ids[1]


async def test_concurrent_duplicate_departures_record_one_event(client, customer, inquiry, session_factory):
    responses = await asyncio.gather(*[
        client.post(f"{CUSTOMER}/{inquiry['inquiry_id']}/session-events", headers=customer["headers"], json={"type": "left"})
        for _ in range(4)
    ])
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json() == responses[0].json() for response in responses)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(InquirySessionEvent)) == 1


@pytest.mark.parametrize("base,suffix,payload", [
    (CUSTOMER, "session-events", {"type": "left"}),
    (CUSTOMER, "read", {"last_read_message_id": "MSG-000001"}),
    (ADMIN, "read", {"last_read_message_id": "MSG-000001"}),
])
async def test_write_access_is_scoped(client, customer, agent, other_customer, foreign_customer, foreign_agent, inquiry, make_actor, base, suffix, payload):
    url = f"{base}/{inquiry['inquiry_id']}/{suffix}"
    assert (await client.post(url, json=payload)).status_code == 401
    wrong_role = customer if base == ADMIN else agent
    assert (await client.post(url, headers=wrong_role["headers"], json=payload)).status_code == 403
    outsiders = [foreign_agent] if base == ADMIN else [other_customer, foreign_customer]
    for actor in outsiders:
        response = await client.post(url, headers=actor["headers"], json=payload)
        assert response.status_code == 404 and response.json()["error"]["code"] == "INQUIRY_NOT_FOUND"
    unassigned = await make_actor("unassigned", "agent" if base == ADMIN else "customer", None)
    assert (await client.post(url, headers=unassigned["headers"], json=payload)).status_code == 403
    allowed = agent if base == ADMIN else customer
    assert (await client.post(f"{base}/Q-9999/{suffix}", headers=allowed["headers"], json=payload)).status_code == 404


@pytest.mark.parametrize("is_agent", [False, True])
async def test_read_rejects_missing_and_other_inquiry_messages(client, customer, agent, inquiry, session_factory, is_agent):
    other = await client.post(CUSTOMER, headers={**customer["headers"], "Idempotency-Key": "other-inquiry"}, json={"message": "별도 문의"})
    assert other.status_code == 201
    actor, base = (agent, ADMIN) if is_agent else (customer, CUSTOMER)
    for message_id in ["MSG-999999", other.json()["auto_message"]["message_id"]]:
        response = await client.post(f"{base}/{inquiry['inquiry_id']}/read", headers=actor["headers"], json={"last_read_message_id": message_id})
        assert response.status_code == 404 and response.json()["error"]["code"] == "MESSAGE_NOT_FOUND"
    async with session_factory() as session:
        row = await get_row(session, Inquiry, inquiry["inquiry_id"])
        assert row.agent_last_read_sequence == row.customer_last_read_sequence == 0


@pytest.mark.parametrize("payload", [{}, {"type": "LEFT"}, {"type": "left", "customer_id": "U-2001"}, {"type": None}])
async def test_event_validation(client, customer, inquiry, payload):
    response = await client.post(f"{CUSTOMER}/{inquiry['inquiry_id']}/session-events", headers=customer["headers"], json=payload)
    assert response.status_code == 400 and response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("is_agent", [False, True])
@pytest.mark.parametrize("payload", [{}, {"last_read_message_id": 1}, {"last_read_message_id": "MSG-x"}, {"last_read_message_id": None}, {"last_read_message_id": "MSG-000001", "sender": "customer"}])
async def test_read_validation(client, customer, agent, inquiry, is_agent, payload):
    actor, base = (agent, ADMIN) if is_agent else (customer, CUSTOMER)
    response = await client.post(f"{base}/{inquiry['inquiry_id']}/read", headers=actor["headers"], json=payload)
    assert response.status_code == 400 and response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_activity_migration_preserves_existing_data_and_roundtrips(db_engine, migrate, inquiry):
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "0009_analysis")
        before = (await connection.execute(text("SELECT * FROM inquiries"))).all()
        messages = (await connection.execute(text("SELECT * FROM messages"))).all()
        analyses = (await connection.execute(text("SELECT * FROM analysis_requests"))).all()
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "upgrade", "head")
        assert (await connection.execute(text("SELECT customer_last_read_sequence,agent_last_read_sequence FROM inquiries"))).one() == (0, 0)
        await connection.execute(text("INSERT INTO inquiry_session_events(inquiry_id,type) SELECT inquiry_id,'left' FROM inquiries"))
        await connection.execute(text("UPDATE inquiries SET customer_last_read_sequence=2"))
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "0009_analysis")
        assert (await connection.execute(text("SELECT * FROM inquiries"))).all() == before
        assert (await connection.execute(text("SELECT * FROM messages"))).all() == messages
        assert (await connection.execute(text("SELECT * FROM analysis_requests"))).all() == analyses
        assert not await connection.run_sync(lambda conn: inspect(conn).has_table("inquiry_session_events"))
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "upgrade", "head")
