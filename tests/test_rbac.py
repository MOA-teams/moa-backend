from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
import jwt
import pytest

from app.core.config import get_settings
from app.core.enums import UserRole
from app.core.errors import install_error_handlers
from app.deps.auth import require_roles
from app.deps.db import get_db

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def rbac_client(session_factory, roles):
    secured = FastAPI()
    install_error_handlers(secured)

    @secured.get("/rbac", dependencies=[Depends(require_roles(*roles))])
    async def endpoint():
        return {"ok": True}

    async def override_db():
        async with session_factory() as session:
            yield session

    secured.dependency_overrides[get_db] = override_db
    async with AsyncClient(transport=ASGITransport(app=secured), base_url="http://test") as client:
        yield client


@pytest.mark.parametrize("role", list(UserRole))
@pytest.mark.parametrize("allowed", [
    (UserRole.CUSTOMER,), (UserRole.ADMIN,), (UserRole.AGENT, UserRole.ADMIN), tuple(UserRole),
])
async def test_role_matrix(client, session_factory, role, allowed):
    credentials = {"email": "rbac@example.com", "password": "example-password"}
    assert (await client.post("/api/v1/auth/signup", json={**credentials, "role": role.value})).status_code == 201
    pair = (await client.post("/api/v1/auth/login", json=credentials)).json()
    async with rbac_client(session_factory, allowed) as secured:
        response = await secured.get("/rbac", headers={"Authorization": f"Bearer {pair['access_token']}"})
    assert response.status_code == (200 if role in allowed else 403)
    if response.status_code == 403:
        assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_unauthenticated_request_is_401(session_factory):
    async with rbac_client(session_factory, (UserRole.ADMIN,)) as secured:
        response = await secured.get("/rbac")
    assert response.status_code == 401 and response.headers["WWW-Authenticate"] == "Bearer"


async def test_request_role_cannot_override_database_role(session_factory, tokens):
    async with rbac_client(session_factory, (UserRole.ADMIN,)) as secured:
        response = await secured.get("/rbac?role=admin", headers={"Authorization": f"Bearer {tokens['access_token']}", "X-Role": "admin"})
    assert response.status_code == 403


async def test_signed_but_stale_role_is_401(session_factory, tokens):
    key = get_settings().jwt_secret_key.get_secret_value()
    payload = jwt.decode(tokens["access_token"], key, algorithms=["HS256"])
    payload["role"] = "admin"
    token = jwt.encode(payload, key, algorithm="HS256")
    async with rbac_client(session_factory, (UserRole.ADMIN,)) as secured:
        response = await secured.get("/rbac", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_missing_role_configuration_is_error():
    with pytest.raises(ValueError):
        require_roles()


def test_role_configuration_requires_enum_values():
    with pytest.raises(TypeError):
        require_roles("admin")
