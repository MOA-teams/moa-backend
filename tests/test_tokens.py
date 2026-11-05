from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings

pytestmark = pytest.mark.anyio


def payload_for(user_id):
    now = datetime.now(timezone.utc)
    return {"sub": str(user_id), "role": "customer", "token_type": "access", "iat": now, "exp": now + timedelta(hours=1), "jti": uuid4().hex}


def encode(payload, key=None, algorithm="HS256"):
    return jwt.encode(payload, key if key is not None else get_settings().jwt_secret_key.get_secret_value(), algorithm=algorithm)


async def assert_rejected(client, token):
    response = await client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "INVALID_ACCESS_TOKEN"
    assert token not in response.text


async def test_get_user_returns_only_public_account_fields(protected_client, account, tokens):
    response = await protected_client.get("/protected", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert response.status_code == 200
    assert response.json() == {"user_id": account["user_id"], "email": account["email"], "role": "customer", "tenant_id": None}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"}])
async def test_missing_or_wrong_auth_scheme(protected_client, headers):
    response = await protected_client.get("/protected", headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_malformed_token(protected_client):
    await assert_rejected(protected_client, "not-a-jwt")


async def test_expired_token(protected_client, account):
    payload = payload_for(account["user_id"])
    payload["exp"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    await assert_rejected(protected_client, encode(payload))


async def test_wrong_signature(protected_client, account):
    await assert_rejected(protected_client, encode(payload_for(account["user_id"]), key="different-test-key-" * 4))


@pytest.mark.parametrize("algorithm", ["HS384", "HS512"])
async def test_non_hs256_algorithm(protected_client, account, algorithm):
    await assert_rejected(protected_client, encode(payload_for(account["user_id"]), algorithm=algorithm))


async def test_unsigned_token(protected_client, account):
    token = jwt.encode(payload_for(account["user_id"]), key=None, algorithm="none")
    await assert_rejected(protected_client, token)


@pytest.mark.parametrize("claim", ["sub", "role", "token_type", "iat", "exp", "jti"])
async def test_missing_required_claim(protected_client, account, claim):
    payload = payload_for(account["user_id"])
    del payload[claim]
    await assert_rejected(protected_client, encode(payload))


@pytest.mark.parametrize("change", [
    {"sub": 1}, {"sub": "0"}, {"sub": "-1"}, {"sub": "abc"},
    {"sub": "9223372036854775808"}, {"sub": "١"},
    {"role": "owner"}, {"role": None}, {"token_type": "refresh"},
    {"iat": "invalid"}, {"exp": "invalid"}, {"jti": 123},
])
async def test_invalid_claims(protected_client, account, change):
    await assert_rejected(protected_client, encode({**payload_for(account["user_id"]), **change}))


async def test_future_issued_at(protected_client, account):
    payload = payload_for(account["user_id"])
    payload["iat"] = datetime.now(timezone.utc) + timedelta(minutes=1)
    await assert_rejected(protected_client, encode(payload))


async def test_nonexistent_user(protected_client):
    await assert_rejected(protected_client, encode(payload_for(999999)))


def test_missing_signing_key_is_configuration_error(monkeypatch):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, database_url="postgresql+asyncpg://test:test@localhost/test")
    assert "jwt_secret_key" in str(error.value)


def test_short_signing_key_error_does_not_expose_value():
    secret = "do-not-show-this-key"
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, database_url="postgresql+asyncpg://test:test@localhost/test", jwt_secret_key=secret)
    assert secret not in str(error.value)


@pytest.mark.parametrize("field", ["access_token_expire_seconds", "refresh_token_expire_days"])
def test_nonpositive_expiration_setting(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql+asyncpg://test:test@localhost/test", **{field: 0})
