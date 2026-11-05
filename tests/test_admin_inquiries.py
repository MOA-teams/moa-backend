from conftest import get_row
import pytest
from sqlalchemy import select, text

from app.core.enums import InquiryCategory, InquiryUrgency
from app.db.models import Inquiry

pytestmark = pytest.mark.anyio
BASE = "/api/v1/admin/inquiries"


async def test_staff_list_and_detail_include_only_own_tenant(client, customer, inquiry, agent, foreign_agent, foreign_customer, make_actor):
    admin = await make_actor("admin", "admin")
    foreign = await client.post("/api/v1/inquiries", headers={**foreign_customer["headers"], "Idempotency-Key": "foreign"}, json={"message": "다른 회사"})
    assert foreign.status_code == 201
    for actor in (agent, admin):
        listing = await client.get(BASE, headers=actor["headers"])
        assert listing.status_code == 200
        assert [item["inquiry_id"] for item in listing.json()["items"]] == [inquiry["inquiry_id"]]
        detail = await client.get(f"{BASE}/{inquiry['inquiry_id']}", headers=actor["headers"])
        assert detail.status_code == 200
        body = detail.json()
        assert body["tenant_id"] == "alpha" and body["customer_id"] == customer["user_id"]
        assert body["revision"] == 1 and body["customer_category"] == "기능 오류"
        assert body["latest_customer_message_id"] == body["messages"][0]["message_id"]
        for field in ("category", "urgency", "processing_stage", "latest_run_id", "group_id", "draft", "verification"):
            assert body[field] is None
    assert (await client.get(f"{BASE}/{inquiry['inquiry_id']}", headers=foreign_agent["headers"])).status_code == 404
    assert [item["inquiry_id"] for item in (await client.get(BASE, headers=foreign_agent["headers"])).json()["items"]] == [foreign.json()["inquiry_id"]]


async def test_staff_role_and_tenant_required(client, customer, make_actor):
    assert (await client.get(BASE)).status_code == 401
    assert (await client.get(BASE, headers=customer["headers"])).status_code == 403
    actor = await make_actor("unassigned", "agent", None)
    response = await client.get(BASE, headers=actor["headers"])
    assert response.status_code == 403 and response.json()["error"]["code"] == "TENANT_REQUIRED"


async def test_list_keyset_pagination_equal_times_and_filters(client, customer, agent, session_factory):
    ids = []
    for i in range(5):
        response = await client.post("/api/v1/inquiries", headers={**customer["headers"], "Idempotency-Key": f"list-{i}"}, json={"message": str(i)})
        ids.append(response.json()["inquiry_id"])
    async with session_factory() as session:
        await session.execute(text("UPDATE inquiries SET created_at='2026-11-05T12:00:00+09:00'"))
        first = await get_row(session, Inquiry, ids[0])
        first.category = InquiryCategory.BUG
        first.urgency = InquiryUrgency.HIGH
        await session.commit()
    seen = []
    cursor = None
    while True:
        query = {"limit": 2, "status": "접수"}
        if cursor:
            query["cursor"] = cursor
        response = await client.get(BASE, headers=agent["headers"], params=query)
        assert response.status_code == 200
        page = response.json()
        seen.extend(item["inquiry_id"] for item in page["items"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
    assert seen == list(reversed(ids))
    filtered = (await client.get(BASE, headers=agent["headers"], params={"category": "기능 오류", "urgency": "높음"})).json()
    assert [item["inquiry_id"] for item in filtered["items"]] == ids[:1]
    assert (await client.get(BASE, headers=agent["headers"], params={"status": "상담 종료"})).json()["items"] == []
    assert (await client.get(BASE, headers=agent["headers"], params={"cursor": cursor, "status": "상담 종료"})).status_code == 400


async def test_admin_detail_pagination_and_read_does_not_change_timestamps(client, agent, inquiry, session_factory):
    inquiry_id = inquiry["inquiry_id"]
    async with session_factory() as session:
        row = await get_row(session, Inquiry, inquiry_id)
        before = row.updated_at
    first = (await client.get(f"{BASE}/{inquiry_id}", headers=agent["headers"], params={"limit": 1})).json()
    assert first["has_more"] and len(first["messages"]) == 1
    second = (await client.get(f"{BASE}/{inquiry_id}", headers=agent["headers"], params={"cursor": first["next_cursor"]})).json()
    assert not second["has_more"] and len(second["messages"]) == 1
    async with session_factory() as session:
        assert (await get_row(session, Inquiry, inquiry_id)).updated_at == before


@pytest.mark.parametrize("query", [{"status": "received"}, {"category": "bug"}, {"urgency": "high"}, {"limit": 101}, {"cursor": "invalid"}])
async def test_staff_query_validation(client, agent, query):
    assert (await client.get(BASE, headers=agent["headers"], params=query)).status_code == 400


async def test_swagger_and_idempotency_cors(client):
    response = await client.options("/api/v1/inquiries", headers={
        "Origin": "https://moa-client.vercel.app", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
    })
    assert response.status_code == 200
    spec = (await client.get("/openapi.json")).json()
    assert len(spec["paths"]) == 15
    assert spec["components"]["schemas"]["InquiryStatus"]["enum"] == ["접수", "담당자 확인 중", "추가 정보 필요", "답변 완료", "상담 종료"]
    operation = spec["paths"]["/api/v1/inquiries"]["post"]
    assert any(p["name"] == "Idempotency-Key" and p["required"] for p in operation["parameters"])
    assert {"400", "401", "403", "404", "409"} <= set(operation["responses"])
    for path in ("/api/v1/inquiries", "/api/v1/attachments", BASE):
        for method in spec["paths"][path].values():
            assert method["summary"] and method["description"] and method["security"]
