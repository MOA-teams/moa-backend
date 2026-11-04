import os
from typing import Annotated
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

# 테스트 프로세스는 실제 .env의 서명키를 사용하지 않는다.
os.environ["JWT_SECRET_KEY"] = "moa-tests-only-signing-key-" * 4
os.environ["ACCESS_TOKEN_EXPIRE_SECONDS"] = "3600"
os.environ["REFRESH_TOKEN_EXPIRE_DAYS"] = "7"

from app.core.config import PROJECT_ROOT, get_settings
from app.core.errors import install_error_handlers
from app.deps.auth import get_user
from app.deps.db import get_db
from app.main import app
from app.schemas.auth import AuthenticatedUser


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def migrate():
    def run(connection, direction, revision):
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.attributes["connection"] = connection
        getattr(command, direction)(config, revision)
    return run


@pytest.fixture
async def db_engine(anyio_backend, migrate):
    schema = f"test_auth_{uuid4().hex}"
    url = get_settings().database_url.get_secret_value()
    admin = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    engine = create_async_engine(
        url, poolclass=NullPool, hide_parameters=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    created = False
    try:
        async with admin.begin() as connection:
            await connection.execute(CreateSchema(schema))
            created = True
        async with engine.begin() as connection:
            await connection.run_sync(migrate, "upgrade", "head")
        yield engine
    finally:
        await engine.dispose()
        if created:
            async with admin.begin() as connection:
                await connection.execute(DropSchema(schema, cascade=True))
        await admin.dispose()


@pytest.fixture
async def session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
async def client(session_factory):
    async def override_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def protected_client(session_factory):
    protected = FastAPI()
    install_error_handlers(protected)

    @protected.get("/protected", response_model=AuthenticatedUser)
    async def protected_endpoint(user: Annotated[AuthenticatedUser, Depends(get_user)]):
        return user

    async def override_db():
        async with session_factory() as session:
            yield session

    protected.dependency_overrides[get_db] = override_db
    async with AsyncClient(transport=ASGITransport(app=protected), base_url="http://test") as client:
        yield client


@pytest.fixture
async def account(client):
    credentials = {"email": "account@example.com", "password": "  kept password  "}
    response = await client.post("/api/v1/auth/signup", json={**credentials, "role": "customer"})
    assert response.status_code == 201
    return {**credentials, **response.json()}


@pytest.fixture
async def tokens(client, account):
    response = await client.post("/api/v1/auth/login", json={"email": account["email"], "password": account["password"]})
    assert response.status_code == 200
    return response.json()
