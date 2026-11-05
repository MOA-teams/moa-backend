"""싱크노트 유입 정보는 보존하되 인증·문의 소유권은 기존 로그인 사용자로 유지한다."""
import hashlib
import json

import pytest
from sqlalchemy import func, select

from app.db.models import IdempotencyRecord, Inquiry, User

pytestmark = pytest.mark.anyio
BASE = "/api/v1/inquiries"
ENVIRONMENT = {"os": "iOS 18", "app_version": "2.4.0", "error_code": "E-502"}


def writing(actor, key):
    return {**actor["headers"], "Idempotency-Key": key}


@pytest.mark.parametrize("external_id", ["U-2001", "000042", "외부고객-가", "x" * 128])
async def test_external_id_and_environment_are_saved_as_context(client, customer, agent, session_factory, external_id):
    payload = {"message": "저장할 때 오류가 발생해요.", "source_customer_id": external_id,
               "customer_category": "bug", "env": ENVIRONMENT, "attachments": []}
    response = await client.post(BASE, headers=writing(customer, "syncnote"), json=payload)
    assert response.status_code == 201
    body = response.json()
    assert "source_customer_id" not in body
    async with session_factory() as session:
        row = await session.get(Inquiry, body["inquiry_id"])
        assert row.source_customer_id == external_id
        assert row.env == ENVIRONMENT
        assert row.customer_id == customer["user_id"] and row.tenant_id == customer["tenant_id"]
        user = await session.get(User, customer["user_id"])
        assert user.tenant_id == "alpha" and user.role.value == "customer"
    detail = await client.get(f"/api/v1/admin/inquiries/{body['inquiry_id']}", headers=agent["headers"])
    assert detail.status_code == 200
    assert detail.json()["source_customer_id"] == external_id
    assert detail.json()["customer_id"] == customer["user_id"]
    assert detail.json()["env"] == ENVIRONMENT
    listing = await client.get("/api/v1/admin/inquiries", headers=agent["headers"])
    assert "source_customer_id" not in listing.json()["items"][0]
    chat = await client.get(f"{BASE}/{body['inquiry_id']}", headers=customer["headers"])
    assert chat.status_code == 200 and "source_customer_id" not in chat.json()


@pytest.mark.parametrize("with_explicit_null", [False, True])
async def test_missing_or_null_external_id_keeps_existing_creation_contract(client, customer, agent, session_factory, with_explicit_null):
    payload = {"message": "일반 문의"}
    if with_explicit_null:
        payload["source_customer_id"] = None
    response = await client.post(BASE, headers=writing(customer, "plain"), json=payload)
    assert response.status_code == 201
    inquiry_id = response.json()["inquiry_id"]
    async with session_factory() as session:
        assert (await session.get(Inquiry, inquiry_id)).source_customer_id is None
    detail = await client.get(f"/api/v1/admin/inquiries/{inquiry_id}", headers=agent["headers"])
    assert detail.status_code == 200 and detail.json()["source_customer_id"] is None


@pytest.mark.parametrize("value", ["x" * 129, 2001, True, [], {}])
async def test_external_id_type_and_length_validation(client, customer, session_factory, value):
    response = await client.post(BASE, headers=writing(customer, "invalid"), json={"message": "x", "source_customer_id": value})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Inquiry)) == 0


async def test_external_id_is_not_an_identity_and_does_not_grant_access(client, customer, other_customer, foreign_customer, foreign_agent, session_factory):
    first = await client.post(BASE, headers=writing(customer, "first"), json={"message": "본인 문의", "source_customer_id": "shared-cid"})
    assert first.status_code == 201
    first_id = first.json()["inquiry_id"]
    for actor in (other_customer, foreign_customer):
        response = await client.post(BASE, headers=writing(actor, "same-external-id"), json={
            "message": "같은 외부 ID지만 다른 로그인 사용자", "source_customer_id": "shared-cid",
        })
        assert response.status_code == 201
        async with session_factory() as session:
            row = await session.get(Inquiry, response.json()["inquiry_id"])
            assert row.customer_id == actor["user_id"] and row.tenant_id == actor["tenant_id"]
        forged_headers = {**actor["headers"], "X-Customer-ID": str(customer["user_id"]), "X-Tenant-ID": "alpha"}
        lookup = await client.get(f"{BASE}/{first_id}", headers=forged_headers, params={"cid": "shared-cid", "source_customer_id": "shared-cid"})
        assert lookup.status_code == 404
        append = await client.post(f"{BASE}/{first_id}/messages?cid=shared-cid", headers=writing(actor, "forged"), json={"text": "위조"})
        assert append.status_code == 404
        me = await client.get("/api/v1/auth/me", headers=actor["headers"])
        assert me.json()["user_id"] == actor["user_id"] and me.json()["tenant_id"] == actor["tenant_id"]
    assert (await client.get(f"/api/v1/admin/inquiries/{first_id}", headers=foreign_agent["headers"])).status_code == 404


async def test_external_id_cannot_replace_login_or_escalate_role(client, make_actor):
    payload = {"message": "cid만으로는 접수 불가", "source_customer_id": "U-2001"}
    assert (await client.post(BASE, headers={"Idempotency-Key": "unauthenticated"}, json=payload)).status_code == 401
    for role in ("agent", "admin"):
        actor = await make_actor(role, role)
        assert (await client.post(BASE, headers=writing(actor, "role"), json=payload)).status_code == 403
    unassigned = await make_actor("unassigned", tenant_id=None)
    assert (await client.post(BASE, headers=writing(unassigned, "tenant"), json=payload)).status_code == 403


async def test_replay_uses_external_id_in_request_fingerprint(client, customer, session_factory):
    payload = {"message": "오류 문의", "source_customer_id": "U-2001", "env": ENVIRONMENT}
    first = await client.post(BASE, headers=writing(customer, "external"), json=payload)
    replay = await client.post(BASE, headers=writing(customer, "external"), json=payload)
    assert first.status_code == replay.status_code == 201 and first.json() == replay.json()
    changed = await client.post(BASE, headers=writing(customer, "external"), json={**payload, "source_customer_id": "U-2002"})
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Inquiry)) == 1
        assert (await session.get(Inquiry, first.json()["inquiry_id"])).source_customer_id == "U-2001"


async def test_replay_of_pre_upgrade_record_is_compatible(client, customer, session_factory):
    first = await client.post(BASE, headers=writing(customer, "initial"), json={"message": "배포 전 문의"})
    assert first.status_code == 201
    # 이전 버전의 정규화 요청에는 source_customer_id 키 자체가 없었다.
    old_payload = {"message": "배포 전 문의", "customer_category": "other",
                   "env": {"os": None, "app_version": None, "error_code": None}, "attachments": []}
    old_hash = hashlib.sha256(json.dumps(old_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    async with session_factory() as session:
        session.add(IdempotencyRecord(user_id=customer["user_id"], path="/inquiries",
                                     key_hash=hashlib.sha256(b"pre-upgrade").hexdigest(), request_hash=old_hash, response=first.json()))
        await session.commit()
    for payload in ({"message": "배포 전 문의"}, {"message": "배포 전 문의", "source_customer_id": None}):
        replay = await client.post(BASE, headers=writing(customer, "pre-upgrade"), json=payload)
        assert replay.status_code == 201 and replay.json() == first.json()
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Inquiry)) == 1


async def test_source_id_swagger_contract_and_examples(client):
    spec = (await client.get("/openapi.json")).json()
    field = spec["components"]["schemas"]["InquiryCreateRequest"]["properties"]["source_customer_id"]
    assert any(option.get("maxLength") == 128 for option in field["anyOf"])
    assert "source_customer_id" not in spec["components"]["schemas"]["InquiryCreateRequest"].get("required", [])
    assert "source_customer_id" in spec["components"]["schemas"]["InquiryDetailResponse"]["properties"]
    assert "source_customer_id" not in spec["components"]["schemas"]["InquiryChatResponse"]["properties"]
    assert "source_customer_id" not in spec["components"]["schemas"]["InquirySummary"]["properties"]
    example = spec["paths"][BASE]["post"]["requestBody"]["content"]["application/json"]["examples"]["syncnote"]["value"]
    assert example["source_customer_id"] == "U-2001" and example["env"]["error_code"] == "E-502"
