import json
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.cursor import encode_cursor
from app.core.enums import InquiryCategory, InquiryStatus
from app.db.models import AnalysisRequest, Inquiry, InquirySessionEvent, Message, User
from app.main import app
from conftest import get_row

pytestmark = pytest.mark.anyio
BASE = "/api/v1/inquiries"
CREATED_AT = datetime(2025, 1, 1, tzinfo=timezone.utc)


async def create(client, actor, message="과거 문의", **fields):
    response = await client.post(
        BASE, headers={**actor["headers"], "Idempotency-Key": uuid4().hex},
        json={"message": message, **fields},
    )
    assert response.status_code == 201, response.text
    return response.json()["inquiry_id"]


async def page(client, actor, **params):
    response = await client.get(BASE, headers=actor["headers"], params=params)
    assert response.status_code == 200, response.text
    return response.json()


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert set(error) == {"code", "message", "request_id"}
    assert error["code"] == code
    assert error["message"] and error["request_id"]


async def test_history_includes_all_states_and_scopes_both_customer_and_tenant(
    client, customer, other_customer, foreign_customer, session_factory,
):
    ids = [await create(client, customer, f"  첫 문의 {status.value}  ", source_customer_id="shared-cid")
           for status in InquiryStatus]
    await create(client, other_customer, source_customer_id="shared-cid")
    await create(client, foreign_customer, source_customer_id="shared-cid")
    previous_tenant_id = await create(client, foreign_customer, source_customer_id="shared-cid")
    async with session_factory() as session:
        for index, (inquiry_id, status) in enumerate(zip(ids, InquiryStatus)):
            row = await get_row(session, Inquiry, inquiry_id)
            row.status = status
            row.created_at = CREATED_AT + timedelta(days=index)
        # 같은 사용자의 다른 기업 기록도 고객 ID만으로 노출되지 않아야 한다.
        previous = await get_row(session, Inquiry, previous_tenant_id)
        previous.customer_id = customer["internal_id"]
        await session.commit()
    result = await page(client, customer, cid="shared-cid", customer_id=other_customer["user_id"], tenant_id="beta")
    assert set(result) == {"items", "next_cursor", "has_more"}
    assert [item["inquiry_id"] for item in result["items"]] == list(reversed(ids))
    assert {item["status"] for item in result["items"]} == {status.value for status in InquiryStatus}
    for item in result["items"]:
        assert set(item) == {"inquiry_id", "title", "status", "created_at"}
        assert item["title"] == f"첫 문의 {item['status']}"
        assert datetime.fromisoformat(item["created_at"]).utcoffset() is not None
        assert datetime.fromisoformat(item["created_at"]).year == 2025
    assert result["next_cursor"] is None and not result["has_more"]


@pytest.mark.parametrize("limit", [None, 1, 100])
async def test_empty_history_and_valid_page_sizes(client, customer, limit):
    assert await page(client, customer, **({} if limit is None else {"limit": limit})) == {
        "items": [], "next_cursor": None, "has_more": False,
    }


@pytest.mark.parametrize("params,code", [
    ({"limit": 0}, "VALIDATION_ERROR"), ({"limit": -1}, "VALIDATION_ERROR"),
    ({"limit": 101}, "VALIDATION_ERROR"), ({"limit": "abc"}, "VALIDATION_ERROR"),
    ({"limit": "1.5"}, "VALIDATION_ERROR"), ({"cursor": ""}, "INVALID_CURSOR"),
    ({"cursor": "invalid"}, "INVALID_CURSOR"), ({"cursor": "a" * 2049}, "VALIDATION_ERROR"),
])
async def test_invalid_page_parameters(client, customer, params, code):
    assert_error(await client.get(BASE, headers=customer["headers"], params=params), 400, code)


async def test_authentication_role_and_tenant_required(client, make_actor):
    response = await client.get(BASE)
    assert_error(response, 401, "INVALID_ACCESS_TOKEN")
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert_error(await client.get(BASE, headers={"Authorization": "Bearer invalid"}), 401, "INVALID_ACCESS_TOKEN")
    for role in ("agent", "admin"):
        actor = await make_actor(role, role)
        assert_error(await client.get(BASE, headers=actor["headers"]), 403, "FORBIDDEN")
    unassigned = await make_actor("unassigned", tenant_id=None)
    assert_error(await client.get(BASE, headers=unassigned["headers"]), 403, "TENANT_REQUIRED")


async def test_keyset_order_uses_internal_id_for_ties_and_defaults_to_twenty(client, customer, session_factory):
    async with session_factory() as session:
        rows = []
        for index in range(21):
            row = Inquiry(
                public_id=f"Q-{9000 - index}", tenant_id=customer["tenant_id"], customer_id=customer["internal_id"],
                title=f"기존 문의 {index}", customer_category=InquiryCategory.OTHER, env={},
                status=InquiryStatus.RECEIVED, created_at=CREATED_AT,
            )
            session.add(row)
            await session.flush()
            rows.append(row)
        # 가장 작은 내부 ID의 최신 접수 건이 동일 시각의 다른 건보다 먼저 와야 한다.
        rows[0].created_at = CREATED_AT + timedelta(days=1)
        await session.commit()
        expected = [row.public_id for row in sorted(rows, key=lambda row: (row.created_at, row.inquiry_id), reverse=True)]
    first = await page(client, customer)
    assert len(first["items"]) == 20 and first["has_more"] and first["next_cursor"]
    last = await page(client, customer, cursor=first["next_cursor"], limit=1)
    assert last["next_cursor"] is None and not last["has_more"]
    assert [item["inquiry_id"] for item in first["items"] + last["items"]] == expected
    complete = await page(client, customer, limit=100)
    assert [item["inquiry_id"] for item in complete["items"]] == expected
    assert complete["next_cursor"] is None and not complete["has_more"]


async def test_new_inquiry_does_not_duplicate_pages_and_refresh_shows_it(client, customer):
    ids = [await create(client, customer, str(index)) for index in range(3)]
    first = await page(client, customer, limit=1)
    new_id = await create(client, customer, "새 문의")
    second = await page(client, customer, limit=1, cursor=first["next_cursor"])
    last = await page(client, customer, limit=1, cursor=second["next_cursor"])
    assert [item["inquiry_id"] for result in (first, second, last) for item in result["items"]] == list(reversed(ids))
    assert not last["has_more"] and last["next_cursor"] is None
    assert (await page(client, customer, limit=1))["items"][0]["inquiry_id"] == new_id


async def test_cursors_reject_other_users_tenants_purposes_and_tampering(
    client, customer, other_customer, foreign_customer, agent,
):
    inquiry_id = await create(client, customer)
    await create(client, customer)
    cursor = (await page(client, customer, limit=1))["next_cursor"]
    for actor in (other_customer, foreign_customer):
        assert_error(await client.get(BASE, headers=actor["headers"], params={"cursor": cursor}), 400, "INVALID_CURSOR")
    chat_cursor = (await client.get(f"{BASE}/{inquiry_id}", headers=customer["headers"])).json()["next_cursor"]
    admin_cursor = (await client.get("/api/v1/admin/inquiries", headers=agent["headers"], params={"limit": 1})).json()["next_cursor"]
    assert admin_cursor
    payload, signature = cursor.split(".")
    tampered = payload + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
    for invalid in (chat_cursor, admin_cursor, tampered):
        assert_error(await client.get(BASE, headers=customer["headers"], params={"cursor": invalid}), 400, "INVALID_CURSOR")
    for overrides in ({"tenant": "beta"}, {"type": "admin_inquiries"}):
        wrong_scope = encode_cursor(
            {"type": "customer_inquiries", "tenant": "alpha", "user": customer["internal_id"], **overrides},
            {"created_at": CREATED_AT.isoformat(), "id": 1},
        )
        assert_error(await client.get(BASE, headers=customer["headers"], params={"cursor": wrong_scope}), 400, "INVALID_CURSOR")
    assert_error(await client.get(f"{BASE}/{inquiry_id}", headers=customer["headers"], params={"cursor": cursor}), 400, "INVALID_CURSOR")


@pytest.mark.parametrize("position", [
    {}, {"created_at": "invalid", "id": 1}, {"created_at": "2025-01-01T00:00:00", "id": 1},
    {"created_at": None, "id": 1}, {"created_at": CREATED_AT.isoformat(), "id": True},
    {"created_at": CREATED_AT.isoformat(), "id": 0}, {"created_at": CREATED_AT.isoformat(), "id": "1"},
    {"created_at": CREATED_AT.isoformat(), "id": 2**63},
])
async def test_signed_cursor_position_validation(client, customer, position):
    cursor = encode_cursor({"type": "customer_inquiries", "tenant": "alpha", "user": customer["internal_id"]}, position)
    assert_error(await client.get(BASE, headers=customer["headers"], params={"cursor": cursor}), 400, "INVALID_CURSOR")


async def test_history_does_not_change_read_presence_revision_timestamps_or_analysis(
    client, customer, inquiry, session_factory,
):
    inquiry_id = inquiry["inquiry_id"]
    assert (await client.post(f"{BASE}/{inquiry_id}/session-events", headers=customer["headers"], json={"type": "new_chat"})).status_code == 200
    assert (await client.post(f"{BASE}/{inquiry_id}/read", headers=customer["headers"],
                              json={"last_read_message_id": inquiry["auto_message"]["message_id"]})).status_code == 200

    async def snapshot():
        async with session_factory() as session:
            return [list((await session.execute(select(model.__table__).order_by(*model.__table__.primary_key.columns))).mappings())
                    for model in (Inquiry, Message, InquirySessionEvent, AnalysisRequest)]

    before = await snapshot()
    for _ in range(2):
        assert (await page(client, customer))["items"][0]["inquiry_id"] == inquiry_id
    assert await snapshot() == before


async def test_new_login_without_browser_history_can_list_and_open_old_inquiries(
    client, account, tokens, make_actor, session_factory,
):
    await make_actor("tenant-owner", "admin")
    async with session_factory() as session:
        row = await get_row(session, User, account["user_id"])
        row.tenant_id = "alpha"
        await session.commit()
    actor = {"headers": {"Authorization": f"Bearer {tokens['access_token']}"}}
    inquiry_id = await create(client, actor, "다른 기기에서도 확인할 상담")
    assert (await client.post(f"{BASE}/{inquiry_id}/session-events", headers=actor["headers"], json={"type": "new_chat"})).status_code == 200
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://new-device") as other_device:
        login = await other_device.post("/api/v1/auth/login", json={"email": account["email"], "password": account["password"]})
        assert login.status_code == 200
        fresh_actor = {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}
        result = await page(other_device, fresh_actor)
        assert [item["inquiry_id"] for item in result["items"]] == [inquiry_id]
        detail = await other_device.get(f"{BASE}/{result['items'][0]['inquiry_id']}", headers=fresh_actor["headers"])
        assert detail.status_code == 200
        assert detail.json()["messages"][0]["text"] == "다른 기기에서도 확인할 상담"


def test_swagger_exposes_customer_dto_pagination_and_errors():
    spec = app.openapi()
    operation = spec["paths"][BASE]["get"]
    assert operation["security"]
    assert {"200", "400", "401", "403"} <= operation["responses"].keys()
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    assert set(parameters) == {"cursor", "limit"}
    assert {key: parameters["limit"]["schema"][key] for key in ("default", "minimum", "maximum")} == {
        "default": 20, "minimum": 1, "maximum": 100,
    }
    assert "채팅 cursor" in operation["description"] and "첫 페이지" in operation["description"]
    content = operation["responses"]["200"]["content"]["application/json"]
    assert content["schema"]["$ref"].endswith("CustomerInquiryListResponse")
    examples = [json.loads(example) for example in re.findall(r"```json\n(.*?)\n```", operation["description"], re.DOTALL)]
    assert examples[-1] == {"items": [], "next_cursor": None, "has_more": False}
    properties = spec["components"]["schemas"]["CustomerInquirySummary"]["properties"]
    assert set(properties) == {"inquiry_id", "title", "status", "created_at"}
