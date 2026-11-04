import asyncio
from datetime import datetime, timedelta, timezone
import logging

import jwt
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.enums import UserRole
from app.core.security import hash_refresh_token
from app.db.models import RefreshToken, User
from app.services.auth import refresh_tokens

pytestmark = pytest.mark.anyio
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"


@pytest.mark.parametrize("role", ["customer", "agent", "admin"])
async def test_all_roles_login_with_two_tokens(client, session_factory, role):
    credentials = {"email": f"{role}@example.com", "password": "  kept password  "}
    assert (await client.post("/api/v1/auth/signup", json={**credentials, "role": role})).status_code == 201
    response = await client.post(LOGIN, json={**credentials, "email": credentials["email"].upper()})
    assert response.status_code == 200
    pair = response.json()
    assert set(pair) == {"access_token", "refresh_token"}
    payload = jwt.decode(pair["access_token"], get_settings().jwt_secret_key.get_secret_value(), algorithms=["HS256"])
    assert payload["sub"] == "1" and payload["role"] == role
    assert payload["token_type"] == "access" and payload["jti"]
    assert payload["exp"] - payload["iat"] == 3600
    async with session_factory() as session:
        stored = await session.scalar(select(RefreshToken))
        assert stored.token_hash == hash_refresh_token(pair["refresh_token"])
        assert len(stored.token_hash) == 64 and stored.token_hash != pair["refresh_token"]
        assert stored.expires_at.utcoffset() is not None
        remaining = stored.expires_at - datetime.now(timezone.utc)
        assert timedelta(days=6) < remaining <= timedelta(days=7)


async def test_unknown_email_and_wrong_password_have_same_failure(client, account, session_factory):
    credentials = {"email": account["email"], "password": "wrong"}
    wrong = await client.post(LOGIN, json=credentials)
    missing = await client.post(LOGIN, json={**credentials, "email": "missing@example.com"})
    for response in (wrong, missing):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert wrong.json()["error"]["message"] == missing.json()["error"]["message"]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(RefreshToken)) == 0


async def test_login_does_not_trim_password(client, account):
    response = await client.post(LOGIN, json={"email": account["email"], "password": account["password"].strip()})
    assert response.status_code == 401


@pytest.mark.parametrize("payload", [
    {}, {"email": "user@example.com"}, {"password": "password"},
    {"email": "invalid", "password": "password"},
    {"email": "user@example.com", "password": ""},
    {"email": "user@example.com", "password": "x" * 129},
    {"email": "user@example.com", "password": "password", "role": "admin"},
])
async def test_login_input_errors(client, payload):
    response = await client.post(LOGIN, json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_rotation_rejects_reuse_and_old_access_remains_valid(client, protected_client, tokens, session_factory):
    response = await client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 200
    new = response.json()
    assert set(new) == {"access_token", "refresh_token"}
    assert new["access_token"] != tokens["access_token"]
    assert new["refresh_token"] != tokens["refresh_token"]
    assert (await client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]})).status_code == 401
    for token in (tokens["access_token"], new["access_token"]):
        assert (await protected_client.get("/protected", headers={"Authorization": f"Bearer {token}"})).status_code == 200
    async with session_factory() as session:
        stored = (await session.scalars(select(RefreshToken))).all()
        assert len(stored) == 1 and stored[0].token_hash == hash_refresh_token(new["refresh_token"])


async def test_concurrent_refresh_only_succeeds_once(client, tokens, session_factory):
    responses = await asyncio.gather(*[
        client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]}) for _ in range(2)
    ])
    assert sorted(response.status_code for response in responses) == [200, 401]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(RefreshToken)) == 1


async def test_other_login_session_survives_rotation(client, account, tokens, session_factory):
    other = (await client.post(LOGIN, json={"email": account["email"], "password": account["password"]})).json()
    assert (await client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]})).status_code == 200
    assert (await client.post(REFRESH, json={"refresh_token": other["refresh_token"]})).status_code == 200
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(RefreshToken)) == 2


async def test_expired_refresh_token_is_rejected(client, tokens, session_factory):
    async with session_factory() as session:
        await session.execute(update(RefreshToken).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await session.commit()
    response = await client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 401 and response.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"


@pytest.mark.parametrize("token", ["unknown-token", "changed-token"])
async def test_unknown_refresh_token_is_rejected(client, token):
    response = await client.post(REFRESH, json={"refresh_token": token})
    assert response.status_code == 401
    assert token not in response.text


@pytest.mark.parametrize("payload", [{}, {"refresh_token": ""}, {"refresh_token": "x" * 513}, {"refresh_token": None}])
async def test_refresh_input_errors(client, payload):
    response = await client.post(REFRESH, json=payload)
    assert response.status_code == 400


async def test_failed_rotation_rolls_back_old_token_deletion(client, account, tokens, session_factory, monkeypatch):
    other = (await client.post(LOGIN, json={"email": account["email"], "password": account["password"]})).json()
    # 실제 고유 제약 위반을 일으켜 삭제와 신규 저장의 원자성을 검증한다.
    with monkeypatch.context() as patch:
        patch.setattr("app.services.auth.create_refresh_token", lambda: other["refresh_token"])
        async with session_factory() as session:
            with pytest.raises(IntegrityError):
                await refresh_tokens(session, refresh_token=tokens["refresh_token"])
            assert not session.in_transaction()
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(RefreshToken)) == 2
    assert (await client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]})).status_code == 200


async def test_user_deletion_cascades_and_invalidates_tokens(client, protected_client, account, tokens, session_factory):
    async with session_factory() as session:
        await session.execute(delete(User).where(User.user_id == account["user_id"]))
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(RefreshToken)) == 0
    assert (await client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]})).status_code == 401
    assert (await protected_client.get("/protected", headers={"Authorization": f"Bearer {tokens['access_token']}"})).status_code == 401


async def test_role_change_rejects_old_access_and_refresh_uses_current_role(client, protected_client, account, tokens, session_factory):
    async with session_factory() as session:
        await session.execute(update(User).where(User.user_id == account["user_id"]).values(role=UserRole.ADMIN))
        await session.commit()
    old = await protected_client.get("/protected", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert old.status_code == 401
    new = await client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]})
    assert new.status_code == 200
    current = await protected_client.get("/protected", headers={"Authorization": f"Bearer {new.json()['access_token']}"})
    assert current.status_code == 200 and current.json()["role"] == "admin"


async def test_login_and_refresh_preserve_user_timestamps(client, account, session_factory):
    async with session_factory() as session:
        user = await session.get(User, account["user_id"])
        before = (user.created_at, user.updated_at)
    pair = (await client.post(LOGIN, json={"email": account["email"], "password": account["password"]})).json()
    assert (await client.post(REFRESH, json={"refresh_token": pair["refresh_token"]})).status_code == 200
    async with session_factory() as session:
        user = await session.get(User, account["user_id"])
        assert (user.created_at, user.updated_at) == before


async def test_secrets_are_not_logged_or_returned_in_errors(client, account, session_factory, caplog):
    async with session_factory() as session:
        hashed_password = (await session.get(User, account["user_id"])).password
    with caplog.at_level(logging.INFO):
        login = await client.post(LOGIN, json={"email": account["email"], "password": account["password"]})
        pair = login.json()
        refreshed = await client.post(REFRESH, json={"refresh_token": pair["refresh_token"]})
        rejected = await client.post(REFRESH, json={"refresh_token": pair["refresh_token"]})
    assert refreshed.status_code == 200 and rejected.status_code == 401
    secrets = [account["password"], hashed_password, get_settings().jwt_secret_key.get_secret_value(), *pair.values(), *refreshed.json().values()]
    for secret in secrets:
        assert secret not in caplog.text + rejected.text
    assert account["password"] not in login.text + refreshed.text
    assert hashed_password not in login.text + refreshed.text


async def test_production_api_has_no_me_or_protected_endpoint(client):
    spec = (await client.get("/openapi.json")).json()
    assert "/api/v1/auth/me" not in spec["paths"] and "/protected" not in spec["paths"]
    assert set(spec["components"]["schemas"]["TokenResponse"]["properties"]) == {"access_token", "refresh_token"}
