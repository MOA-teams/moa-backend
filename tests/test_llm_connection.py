import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings
from scripts.check_ai_connection import check_connection
from scripts.configure_env import parse_environment, runtime_environment
from test_env_deploy_config import PAYLOAD


def settings(**changes):
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test:fake@localhost/test",
        jwt_secret_key="test-only-signing-key-" * 3,
        ai_base_url="http://ai.test",
        ai_service_token="private-test-service-token",
        **changes,
    )


def test_deploy_enables_analysis_worker_when_flag_is_absent(monkeypatch):
    monkeypatch.setenv("AI_PIPELINE_WORKER_ENABLED", "false")
    result = runtime_environment(parse_environment(PAYLOAD))
    assert result["AI_PIPELINE_WORKER_ENABLED"] == "True"
    assert result["AI_CALLBACK_TOKEN"] == ""


@pytest.mark.parametrize(
    "flag,expected", [("false", "False"), ("0", "False"), ("true", "True")]
)
def test_deploy_respects_explicit_worker_policy(flag, expected):
    result = runtime_environment(
        parse_environment(PAYLOAD + f"AI_PIPELINE_WORKER_ENABLED={flag}\n")
    )
    assert result["AI_PIPELINE_WORKER_ENABLED"] == expected


@pytest.mark.anyio
@pytest.mark.parametrize("worker", [False, True])
async def test_probe_is_read_only_and_distinguishes_worker_from_auth(worker):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        assert request.headers["authorization"] == "Bearer private-test-service-token"
        assert request.headers["x-tenant-id"] == "syncnote"
        return httpx.Response(404, json={"error": {"code": "RUN_NOT_FOUND"}})

    report = await check_connection(
        settings(ai_pipeline_worker_enabled=worker),
        "syncnote",
        transport=httpx.MockTransport(handle),
    )
    assert len(calls) == 2 and report["service_auth"] == "ok"
    assert report["ready"] == worker
    assert report["callback_receiver_configured"] is False
    assert report["llm_execution_verified"] is False
    assert "private-test-service-token" not in json.dumps(report)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, {}, "invalid_token"),
        (403, {}, "tenant_denied"),
        (404, {"detail": "Not Found"}, "unexpected_response"),
        (503, {}, "ai_not_configured"),
    ],
)
async def test_probe_classifies_failures_without_exposing_response(
    status, body, expected
):
    def handle(request):
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(status, json={**body, "private": "remote-secret"})

    report = await check_connection(
        settings(ai_pipeline_worker_enabled=True),
        "syncnote",
        transport=httpx.MockTransport(handle),
    )
    assert not report["ready"] and report["service_auth"] == expected
    assert "remote-secret" not in json.dumps(report)


@pytest.mark.anyio
async def test_probe_reports_network_error_without_exception_text():
    def handle(request):
        raise httpx.ConnectError("private endpoint data")

    report = await check_connection(
        settings(ai_pipeline_worker_enabled=True),
        "syncnote",
        transport=httpx.MockTransport(handle),
    )
    assert report["error"] == "AI_UNAVAILABLE" and not report["ready"]
    assert "private endpoint data" not in json.dumps(report)


@pytest.mark.anyio
async def test_api_lifespan_starts_and_cancels_enabled_worker(monkeypatch):
    import app.main as module

    started, stopped = asyncio.Event(), asyncio.Event()

    async def worker(factory):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def dispose():
        pass

    monkeypatch.setattr(
        module, "get_settings", lambda: SimpleNamespace(ai_pipeline_worker_enabled=True)
    )
    monkeypatch.setattr(module, "run_forever", worker)
    monkeypatch.setattr(module, "engine", SimpleNamespace(dispose=dispose))
    async with module.lifespan(module.app):
        await asyncio.wait_for(started.wait(), 1)
    assert stopped.is_set()
