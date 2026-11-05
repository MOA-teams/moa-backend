"""main 병합 후 답변·평가와 이탈·읽음의 응답 및 migration 호환성을 검증한다."""

import pytest
from sqlalchemy import inspect, text

from test_answers import body_from, close, detail, feedback, send

pytestmark = pytest.mark.anyio


async def test_presence_and_reads_preserve_answer_and_feedback(client, customer, agent, inquiry):
    inquiry_id = inquiry["inquiry_id"]
    customer_path = f"/api/v1/inquiries/{inquiry_id}"
    admin_path = f"/api/v1/admin/inquiries/{inquiry_id}"
    view = await detail(client, agent, inquiry)
    answer = await send(client, agent, inquiry, body_from(view))
    assert answer.status_code == 201
    assert (await close(client, agent, inquiry, answer.json()["inquiry_revision"])).status_code == 200
    assert (await feedback(client, customer, inquiry, {"rating": 5, "resolved": True})).status_code == 201
    chat = (await client.get(customer_path, headers=customer["headers"])).json()
    before = await detail(client, agent, inquiry)
    last_id = chat["messages"][-1]["message_id"]
    for path, actor in ((customer_path, customer), (admin_path, agent)):
        response = await client.post(f"{path}/read", headers=actor["headers"], json={"last_read_message_id": last_id})
        assert response.status_code == 200
    left = await client.post(f"{customer_path}/session-events", headers=customer["headers"], json={"type": "new_chat"})
    assert left.status_code == 200
    after = await detail(client, agent, inquiry)
    for field in ("answers", "automatic_response", "backend_knowledge_revision", "group_id", "status", "revision", "updated_at"):
        assert after[field] == before[field]
    assert after["customer_presence"]["left_reason"] == "new_chat"
    assert after["customer_last_read_message_id"] == last_id
    poll = (await client.get(customer_path, headers=customer["headers"], params={"cursor": chat["next_cursor"]})).json()
    assert poll["messages"] == [] and poll["agent_last_read_message_id"] == last_id
    for field in ("closed_at", "resolution_status", "feedback_requested", "feedback_submitted", "feedback"):
        assert poll[field] == chat[field] == after[field]
    assert poll["feedback"]["answer_id"] == answer.json()["answer_id"]


@pytest.mark.parametrize("revision", ["0010_inquiry_activity", "0013_groups_github"])
async def test_released_activity_and_main_migrations_roundtrip(db_engine, migrate, revision):
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "base")
        await connection.run_sync(migrate, "upgrade", revision)
        await connection.execute(text("INSERT INTO tenants(tenant_id,name) VALUES ('alpha','Alpha')"))
        await connection.execute(text("INSERT INTO users(public_id,email,password,role,tenant_id) VALUES ('U-2001','merge@example.com','test-only','customer','alpha')"))
        await connection.execute(text("INSERT INTO inquiries(public_id,tenant_id,customer_id,title,customer_category,env,status,revision,last_message_sequence) VALUES ('Q-1001','alpha',1,'보존할 문의','기능 오류','{}','접수',1,1)"))
        await connection.execute(text("INSERT INTO messages(public_id,inquiry_id,sequence,sender,message_type,text) VALUES ('MSG-000001',1,1,'customer','고객 메시지','원문 보존')"))
        if revision == "0010_inquiry_activity":
            await connection.execute(text("UPDATE inquiries SET customer_last_read_sequence=1,agent_last_read_sequence=1"))
            await connection.execute(text("INSERT INTO inquiry_session_events(inquiry_id,type) VALUES (1,'left')"))
        tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
        before = dict((await connection.execute(text("SELECT * FROM inquiries"))).mappings().one())
        messages = (await connection.execute(text("SELECT * FROM messages"))).all()
        events = (await connection.execute(text("SELECT * FROM inquiry_session_events"))).all() if revision == "0010_inquiry_activity" else []
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "upgrade", "head")
        assert (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars().all() == ["0014_inquiry_activity_merge"]
        upgraded = (await connection.execute(text("SELECT * FROM inquiries"))).mappings().one()
        assert {key: upgraded[key] for key in before} == before
        assert (await connection.execute(text("SELECT * FROM messages"))).all() == messages
        assert (await connection.execute(text("SELECT * FROM inquiry_session_events"))).all() == events
        if revision == "0013_groups_github":
            assert upgraded["customer_last_read_sequence"] == upgraded["agent_last_read_sequence"] == 0
            await connection.execute(text("INSERT INTO inquiry_session_events(inquiry_id,type) VALUES (1,'new_chat')"))
            await connection.execute(text("UPDATE inquiries SET customer_last_read_sequence=1"))
        # 병합 revision만 내리면 두 분기가 모두 남으므로 제거할 분기를 명시한다.
        removed_branch = "0013_groups_github" if revision == "0010_inquiry_activity" else "0010_inquiry_activity"
        await connection.run_sync(migrate, "downgrade", f"{removed_branch}@0009_analysis")
        assert (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars().all() == [revision]
        assert set(await connection.run_sync(lambda conn: inspect(conn).get_table_names())) == set(tables)
        assert dict((await connection.execute(text("SELECT * FROM inquiries"))).mappings().one()) == before
        assert (await connection.execute(text("SELECT * FROM messages"))).all() == messages
        if revision == "0010_inquiry_activity":
            assert (await connection.execute(text("SELECT * FROM inquiry_session_events"))).all() == events
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "upgrade", "head")
