import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def cors_client(anyio_backend):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.mark.parametrize("origin", [
    "http://localhost:5173",
    "http://localhost:3000",
    "http://localhost:3001",
    "https://moa-client.vercel.app",
    "https://moa-admin-teal.vercel.app",
])
async def test_allowed_frontend_preflight(cors_client, origin):
    response = await cors_client.options("/api/v1/auth/login", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == origin
    assert "origin" in response.headers["Vary"].lower()
    assert "POST" in response.headers["Access-Control-Allow-Methods"]
    allowed_headers = {value.strip().lower() for value in response.headers["Access-Control-Allow-Headers"].split(",")}
    assert {"authorization", "content-type"} <= allowed_headers
    assert "Access-Control-Allow-Credentials" not in response.headers


@pytest.mark.parametrize("method,path,payload,status_code", [
    ("GET", "/health", None, 200),
    ("POST", "/api/v1/auth/login", {}, 400),
    ("GET", "/api/v1/auth/me", None, 401),
])
async def test_cors_headers_preserve_success_and_handled_errors(cors_client, method, path, payload, status_code):
    origin = "http://localhost:5173"
    response = await cors_client.request(method, path, json=payload, headers={"Origin": origin})

    assert response.status_code == status_code
    assert response.headers["Access-Control-Allow-Origin"] == origin
    assert response.headers["X-Request-ID"].startswith("REQ-")
    if status_code >= 400:
        assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.parametrize("origin", [
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://localhost:3002",
    "https://localhost:5173",
])
async def test_unregistered_origins_have_no_cors_access(cors_client, origin):
    preflight = await cors_client.options("/api/v1/auth/login", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
    })
    assert preflight.status_code == 400
    assert "Access-Control-Allow-Origin" not in preflight.headers

    response = await cors_client.get("/health", headers={"Origin": origin})
    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response.headers


@pytest.mark.parametrize("method,headers", [
    ("DELETE", "authorization,content-type"),
    ("POST", "x-unapproved-header"),
])
async def test_unapproved_preflight_method_or_headers_are_rejected(cors_client, method, headers):
    response = await cors_client.options("/api/v1/auth/login", headers={
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": headers,
    })

    assert response.status_code == 400


@pytest.mark.parametrize("origin", [
    "http://localhost:5173", "http://localhost:3000", "http://localhost:3001",
    "https://moa-client.vercel.app", "https://moa-admin-teal.vercel.app",
])
async def test_document_patch_and_idempotency_preflight(cors_client, origin):
    response = await cors_client.options("/api/v1/admin/documents/1/versions/1/status", headers={
        "Origin": origin, "Access-Control-Request-Method": "PATCH",
        "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
    })
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == origin
    assert "PATCH" in response.headers["Access-Control-Allow-Methods"]
    assert "idempotency-key" in response.headers["Access-Control-Allow-Headers"].lower()


async def test_local_admin_github_authorization_preflight(cors_client):
    response = await cors_client.options("/api/v1/admin/integrations/github/authorization", headers={
        "Origin": "http://localhost:3001",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
    })
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:3001"
