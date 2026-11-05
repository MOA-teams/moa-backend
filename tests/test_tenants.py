from conftest import get_row
import asyncio

import jwt
import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.db.models import Tenant, User
from app.services.tenants import TenantAlreadyAssignedError, create_tenant, join_tenant

pytestmark = pytest.mark.anyio
TENANTS = "/api/v1/tenants"


async def actor(client, name, role="customer"):
    credentials = {"email": f"{name}@example.com", "password": "example-password"}
    response = await client.post("/api/v1/auth/signup", json={**credentials, "role": role})
    assert response.status_code == 201
    pair = (await client.post("/api/v1/auth/login", json=credentials)).json()
    return response.json(), {"Authorization": f"Bearer {pair['access_token']}"}


async def company(client, tenant_id):
    owner, headers = await actor(client, f"owner{tenant_id}", "admin")
    response = await client.post(TENANTS, headers=headers, json={"tenant_id": tenant_id, "name": f"{tenant_id} 회사"})
    assert response.status_code == 201
    return owner, headers


@pytest.mark.parametrize("role", ["customer", "agent", "admin"])
async def test_me_returns_unassigned_account(client, role):
    account, headers = await actor(client, "me", role)
    response = await client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 200
    assert response.json() == {**account, "tenant_id": None}


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/api/v1/auth/me", None),
    ("POST", TENANTS, {"tenant_id": "alpha", "name": "Alpha"}),
    ("POST", f"{TENANTS}/join", {"tenant_id": "alpha"}),
    ("GET", f"{TENANTS}/me", None),
])
async def test_new_endpoints_require_authentication(client, method, path, payload):
    response = await client.request(method, path, json=payload)
    assert response.status_code == 401


async def test_admin_creation_links_creator_atomically(client, session_factory):
    account, headers = await actor(client, "creator", "admin")
    response = await client.post(TENANTS, headers=headers, json={"tenant_id": "syncnote", "name": "  싱크노트  "})
    assert response.status_code == 201
    assert response.json() == {"tenant_id": "syncnote", "name": "싱크노트"}
    assert (await client.get("/api/v1/auth/me", headers=headers)).json()["tenant_id"] == "syncnote"
    async with session_factory() as session:
        user = await get_row(session, User, account["user_id"])
        assert user.tenant_id == "syncnote"
        tenant = await get_row(session, Tenant, "syncnote")
        assert tenant.created_at.utcoffset() is not None


@pytest.mark.parametrize("role", ["customer", "agent"])
async def test_non_admin_creation_is_forbidden(client, session_factory, role):
    account, headers = await actor(client, "blocked", role)
    response = await client.post(TENANTS, headers=headers, json={"tenant_id": "alpha", "name": "Alpha"})
    assert response.status_code == 403
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Tenant)) == 0
        assert (await get_row(session, User, account["user_id"])).tenant_id is None


@pytest.mark.parametrize("role", ["customer", "agent", "admin"])
async def test_all_roles_join_and_old_token_sees_membership(client, role):
    await company(client, "alpha")
    account, headers = await actor(client, "member", role)
    key = get_settings().jwt_secret_key.get_secret_value()
    token = headers["Authorization"].removeprefix("Bearer ")
    assert "tenant_id" not in jwt.decode(token, key, algorithms=["HS256"])
    response = await client.post(f"{TENANTS}/join", headers=headers, json={"tenant_id": "alpha"})
    assert response.status_code == 200 and response.json() == {"tenant_id": "alpha", "name": "alpha 회사"}
    assert (await client.get("/api/v1/auth/me", headers=headers)).json() == {**account, "tenant_id": "alpha"}
    assert (await client.get(f"{TENANTS}/me", headers=headers)).json()["tenant_id"] == "alpha"


async def test_idempotent_join_does_not_update_user_again(client, session_factory):
    await company(client, "alpha")
    account, headers = await actor(client, "member")
    assert (await client.post(f"{TENANTS}/join", headers=headers, json={"tenant_id": "alpha"})).status_code == 200
    async with session_factory() as session:
        user = await get_row(session, User, account["user_id"])
        before = (user.created_at, user.updated_at)
    assert (await client.post(f"{TENANTS}/join", headers=headers, json={"tenant_id": "alpha"})).status_code == 200
    async with session_factory() as session:
        user = await get_row(session, User, account["user_id"])
        assert (user.created_at, user.updated_at) == before


async def test_missing_tenant_and_unassigned_lookup(client, tokens):
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    response = await client.get(f"{TENANTS}/me", headers=headers)
    assert response.status_code == 403 and response.json()["error"]["code"] == "TENANT_REQUIRED"
    response = await client.post(f"{TENANTS}/join", headers=headers, json={"tenant_id": "missing"})
    assert response.status_code == 404 and response.json()["error"]["code"] == "TENANT_NOT_FOUND"


async def test_duplicate_id_does_not_link_second_admin(client, session_factory):
    await company(client, "alpha")
    account, headers = await actor(client, "other", "admin")
    response = await client.post(TENANTS, headers=headers, json={"tenant_id": "alpha", "name": "다른 이름"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "TENANT_ALREADY_EXISTS"
    async with session_factory() as session:
        assert (await get_row(session, User, account["user_id"])).tenant_id is None
        assert (await get_row(session, Tenant, "alpha")).name == "alpha 회사"


async def test_assigned_admin_cannot_create_another_company(client, session_factory):
    account, headers = await company(client, "alpha")
    response = await client.post(TENANTS, headers=headers, json={"tenant_id": "beta", "name": "Beta"})
    assert response.status_code == 409
    async with session_factory() as session:
        assert await get_row(session, Tenant, "beta") is None
        assert (await get_row(session, User, account["user_id"])).tenant_id == "alpha"


async def test_cross_tenant_reassignment_is_blocked_and_reads_are_scoped(client):
    _, alpha_headers = await company(client, "alpha")
    _, beta_headers = await company(client, "beta")
    response = await client.post(f"{TENANTS}/join", headers=alpha_headers, json={"tenant_id": "beta"})
    assert response.status_code == 409
    assert (await client.get(f"{TENANTS}/me", headers=beta_headers)).json()["tenant_id"] == "beta"
    forged_headers = {**alpha_headers, "X-Tenant-ID": "beta"}
    response = await client.get(f"{TENANTS}/me?tenant_id=beta&user_id=999", headers=forged_headers)
    assert response.json() == {"tenant_id": "alpha", "name": "alpha 회사"}
    key = get_settings().jwt_secret_key.get_secret_value()
    payload = jwt.decode(alpha_headers["Authorization"].removeprefix("Bearer "), key, algorithms=["HS256"])
    payload["tenant_id"] = "beta"
    forged = {"Authorization": f"Bearer {jwt.encode(payload, key, algorithm='HS256')}"}
    assert (await client.get("/api/v1/auth/me", headers=forged)).json()["tenant_id"] == "alpha"
    assert (await client.get(f"{TENANTS}/me", headers=forged)).json()["tenant_id"] == "alpha"


@pytest.mark.parametrize("payload", [
    {}, {"tenant_id": "alpha"}, {"name": "Alpha"},
    {"tenant_id": "Alpha", "name": "Alpha"}, {"tenant_id": "alpha1", "name": "Alpha"},
    {"tenant_id": "a" * 65, "name": "Alpha"}, {"tenant_id": "", "name": "Alpha"},
    {"tenant_id": "alpha", "name": " "}, {"tenant_id": "alpha", "name": "x" * 101},
    {"tenant_id": "alpha", "name": "Alpha", "user_id": 999},
])
async def test_create_validation(client, payload):
    _, headers = await actor(client, "validate", "admin")
    response = await client.post(TENANTS, headers=headers, json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("payload", [{}, {"tenant_id": "Alpha"}, {"tenant_id": ""}, {"tenant_id": "alpha", "user_id": 999}])
async def test_join_validation(client, tokens, payload):
    response = await client.post(f"{TENANTS}/join", headers={"Authorization": f"Bearer {tokens['access_token']}"}, json=payload)
    assert response.status_code == 400


async def test_concurrent_creation_has_one_company_and_no_orphan(client, session_factory):
    account, headers = await actor(client, "concurrent", "admin")
    responses = await asyncio.gather(*[
        client.post(TENANTS, headers=headers, json={"tenant_id": tenant_id, "name": tenant_id})
        for tenant_id in ("alpha", "beta")
    ])
    assert sorted(response.status_code for response in responses) == [201, 409]
    winner = next(response.json()["tenant_id"] for response in responses if response.status_code == 201)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Tenant)) == 1
        assert (await get_row(session, User, account["user_id"])).tenant_id == winner


async def test_concurrent_duplicate_id_links_only_one_admin(client, session_factory):
    first, first_headers = await actor(client, "first", "admin")
    second, second_headers = await actor(client, "second", "admin")
    responses = await asyncio.gather(*[
        client.post(TENANTS, headers=headers, json={"tenant_id": "alpha", "name": "Alpha"})
        for headers in (first_headers, second_headers)
    ])
    assert sorted(response.status_code for response in responses) == [201, 409]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(User).where(User.tenant_id == "alpha")) == 1
        assert await session.scalar(select(func.count()).select_from(Tenant)) == 1


async def test_concurrent_join_does_not_overwrite_membership(client, session_factory):
    await company(client, "alpha")
    await company(client, "beta")
    account, headers = await actor(client, "concurrent")
    responses = await asyncio.gather(*[
        client.post(f"{TENANTS}/join", headers=headers, json={"tenant_id": tenant_id})
        for tenant_id in ("alpha", "beta")
    ])
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json()["tenant_id"] for response in responses if response.status_code == 200)
    async with session_factory() as session:
        assert (await get_row(session, User, account["user_id"])).tenant_id == winner


async def test_lock_refreshes_preloaded_identity_map(client, session_factory):
    await company(client, "alpha")
    await company(client, "beta")
    account, _ = await actor(client, "preloaded")
    async with session_factory() as first, session_factory() as second:
        cached = [await get_row(first, User, account["user_id"]), await get_row(second, User, account["user_id"])]
        assert all(user.tenant_id is None for user in cached)
        results = await asyncio.gather(
            join_tenant(first, user_id=cached[0].user_id, tenant_id="alpha"),
            join_tenant(second, user_id=cached[1].user_id, tenant_id="beta"),
            return_exceptions=True,
        )
        assert sum(isinstance(result, Tenant) for result in results) == 1
        assert sum(isinstance(result, TenantAlreadyAssignedError) for result in results) == 1


async def test_creation_and_join_share_one_membership_lock(client, session_factory):
    await company(client, "beta")
    account, headers = await actor(client, "race", "admin")
    responses = await asyncio.gather(
        client.post(TENANTS, headers=headers, json={"tenant_id": "alpha", "name": "Alpha"}),
        client.post(f"{TENANTS}/join", headers=headers, json={"tenant_id": "beta"}),
    )
    assert sum(response.status_code < 300 for response in responses) == 1
    assert sum(response.status_code == 409 for response in responses) == 1
    async with session_factory() as session:
        user = await get_row(session, User, account["user_id"])
        assert user.tenant_id == ("alpha" if responses[0].status_code == 201 else "beta")
        assert await session.scalar(select(func.count()).select_from(Tenant)) == (2 if user.tenant_id == "alpha" else 1)


async def test_failed_membership_write_rolls_back_company_creation(client, session_factory):
    account, _ = await actor(client, "rollback", "admin")
    async with session_factory() as session:
        await session.execute(text("ALTER TABLE users ADD CONSTRAINT test_reject_membership CHECK (tenant_id IS NULL)"))
        await session.commit()
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await create_tenant(session, user_id=(await get_row(session, User, account["user_id"])).user_id, tenant_id="alpha", name="Alpha")
        assert not session.in_transaction()
        assert await get_row(session, Tenant, "alpha") is None
        assert (await get_row(session, User, account["user_id"])).tenant_id is None


async def test_database_restricts_deleting_an_occupied_company(client, session_factory):
    await company(client, "alpha")
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(delete(Tenant).where(Tenant.tenant_id == "alpha"))
            await session.commit()
        await session.rollback()
        assert await get_row(session, Tenant, "alpha") is not None


async def test_no_extra_tenant_management_endpoints(client):
    spec = (await client.get("/openapi.json")).json()
    assert set(spec["paths"][TENANTS]) == {"post"}
    assert f"{TENANTS}/{{tenant_id}}" not in spec["paths"]
