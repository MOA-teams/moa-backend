import asyncio
import logging

import pytest
from pwdlib import PasswordHash
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.enums import UserRole
from app.db.models import User
from app.services.auth import signup

pytestmark = pytest.mark.anyio
ENDPOINT = "/api/v1/auth/signup"
VALID = {"email": "user@example.com", "password": "example-password", "role": "customer"}


@pytest.mark.parametrize("role", ["customer", "agent", "admin"])
async def test_signup_roles_and_response(client, session_factory, role):
    response = await client.post(ENDPOINT, json={**VALID, "email": "User@EXAMPLE.COM", "role": role})
    assert response.status_code == 201
    assert response.json() == {"user_id": 1, "email": "user@example.com", "role": role}
    async with session_factory() as session:
        user = await session.get(User, 1)
        assert isinstance(user.user_id, int)
        assert user.role is UserRole(role)
        assert await session.scalar(text("SELECT role::text FROM users")) == role


async def test_password_preserves_spaces_and_never_leaks(client, session_factory, caplog):
    password = "  password with spaces  "
    with caplog.at_level(logging.INFO):
        response = await client.post(ENDPOINT, json={**VALID, "password": password})
        duplicate = await client.post(ENDPOINT, json={**VALID, "password": password})
    assert response.status_code == 201
    assert duplicate.status_code == 409
    async with session_factory() as session:
        user = await session.get(User, response.json()["user_id"])
        assert user.password.startswith("$argon2id$")
        assert PasswordHash.recommended().verify(password, user.password)
        assert not PasswordHash.recommended().verify(password.strip(), user.password)
        assert user.password not in response.text + duplicate.text + caplog.text
    assert password not in response.text + duplicate.text + caplog.text


@pytest.mark.parametrize("length", [8, 128])
async def test_password_length_boundaries(client, length):
    response = await client.post(ENDPOINT, json={**VALID, "password": "x" * length})
    assert response.status_code == 201


async def test_case_insensitive_duplicate_email(client, session_factory):
    assert (await client.post(ENDPOINT, json=VALID)).status_code == 201
    response = await client.post(ENDPOINT, json={**VALID, "email": "USER@EXAMPLE.COM", "role": "admin"})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "EMAIL_ALREADY_EXISTS"
    assert error["request_id"] == response.headers["X-Request-ID"]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_concurrent_duplicate_signup(client, session_factory):
    responses = await asyncio.gather(
        client.post(ENDPOINT, json=VALID),
        client.post(ENDPOINT, json=VALID),
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.parametrize("change", [
    {"email": "invalid"},
    {"password": "short"},
    {"password": "x" * 129},
    {"role": "owner"},
    {"role": "ADMIN"},
    {"role": None},
    {"user_id": 99},
    {"tenant_id": "syncnote"},
    {"created_at": "2026-01-01T00:00:00+09:00"},
    {"updated_at": "2026-01-01T00:00:00+09:00"},
])
async def test_invalid_input_returns_safe_common_error(client, session_factory, change, caplog):
    payload = {**VALID, **change}
    with caplog.at_level(logging.INFO):
        response = await client.post(ENDPOINT, json=payload)
    assert response.status_code == 400
    assert set(response.json()) == {"error"}
    error = response.json()["error"]
    assert set(error) == {"code", "message", "request_id"}
    assert error["code"] == "VALIDATION_ERROR"
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert payload["password"] not in response.text + caplog.text
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 0


@pytest.mark.parametrize("field", ["email", "password", "role"])
async def test_missing_required_field(client, field):
    payload = {key: value for key, value in VALID.items() if key != field}
    response = await client.post(ENDPOINT, json=payload)
    assert response.status_code == 400


async def test_malformed_json_does_not_echo_password(client):
    response = await client.post(ENDPOINT, content='{"password":"do-not-echo",', headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert "do-not-echo" not in response.text


async def test_other_unique_constraints_are_not_reported_as_duplicate_email(session_factory):
    async with session_factory() as session:
        # 명시한 Identity 값은 자동 발급 시퀀스를 진행시키지 않는다.
        session.add(User(user_id=1, email="existing@example.com", password="test-only", role=UserRole.CUSTOMER))
        await session.commit()
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await signup(session, email="different@example.com", password="example-password", role=UserRole.AGENT)
        assert not session.in_transaction()
        assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_health_and_generated_api_documentation(client):
    health = await client.get("/health")
    assert health.status_code == 200 and health.json() == {"status": "ok"}
    assert (await client.get("/docs")).status_code == 200
    spec = (await client.get("/openapi.json")).json()
    assert ENDPOINT in spec["paths"]
    assert "422" not in spec["paths"][ENDPOINT]["post"]["responses"]
    response_properties = spec["components"]["schemas"]["SignupResponse"]["properties"]
    assert set(response_properties) == {"user_id", "email", "role"}
