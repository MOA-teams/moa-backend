import json

import httpx
import pytest

from app.core.rag import RagClient, RagError, get_rag_client

pytestmark = pytest.mark.anyio


async def test_actual_i03_i04_i05_paths_headers_and_responses():
    seen = []
    def handle(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer test-service-token"
        assert request.headers["X-Tenant-ID"] == "alpha"
        if request.method == "GET":
            assert request.url.path == "/internal/v1/rag/sources/DOC-001/versions/1"
            assert request.url.params["source_type"] == "문서"
            return httpx.Response(200, json={"source_id": "DOC-001", "version": 1, "source_revision": 2, "status": "폐기", "review_required": False, "indexing_status": "완료", "searchable": False, "error": None})
        payload = json.loads(request.content)
        assert payload["tenant_id"] == "alpha"
        if request.url.path.endswith("source-statuses"):
            assert payload["sources"][0]["status"] == "폐기"
            return httpx.Response(200, json={"knowledge_revision": 2, "updated_count": 1, "search_excluded": True})
        assert request.url.path == "/internal/v1/rag/sources"
        assert payload["source_id"] == "DOC-001" and payload["source_type"] == "문서"
        return httpx.Response(202, json={"source_id": "DOC-001", "version": 1, "indexing_status": "처리 중"})
    client = RagClient("http://ai.test", "test-service-token", transport=httpx.MockTransport(handle))
    await client.submit("alpha", {"tenant_id": "alpha", "source_type": "문서", "source_id": "DOC-001", "version": 1})
    await client.statuses("alpha", {"tenant_id": "alpha", "event_id": "event-1", "sources": [{"status": "폐기", "review_required": False}]})
    assert not (await client.view("alpha", "DOC-001", 1)).searchable
    assert len(seen) == 3


@pytest.mark.parametrize("response,code,retryable", [
    (httpx.Response(404, json={"error": {"code": "SOURCE_NOT_FOUND"}}), "SOURCE_NOT_FOUND", False),
    (httpx.Response(409, json={"error": {"code": "STALE_SOURCE_REVISION"}}), "STALE_SOURCE_REVISION", False),
    (httpx.Response(503, text="secret upstream body"), "AI_REQUEST_REJECTED", True),
    (httpx.Response(200, json={"source_id": "DOC-002", "version": 1, "source_revision": 1, "status": "유효", "review_required": False, "indexing_status": "완료", "searchable": True}), "AI_INVALID_RESPONSE", True),
    (httpx.Response(200, json={"searchable": "yes"}), "AI_INVALID_RESPONSE", True),
])
async def test_bad_responses_are_fail_closed_and_sanitized(response, code, retryable):
    client = RagClient("http://ai.test", "test-token", transport=httpx.MockTransport(lambda _: response))
    with pytest.raises(RagError) as error:
        await client.view("alpha", "DOC-001", 1)
    assert error.value.code == code and error.value.retryable == retryable
    assert "secret" not in str(error.value)


async def test_no_success_when_ai_does_not_confirm_block():
    client = RagClient("http://ai.test", "test-token", transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"knowledge_revision": 1, "updated_count": 1, "search_excluded": False})))
    with pytest.raises(RagError, match="AI_BLOCK_NOT_CONFIRMED"):
        await client.statuses("alpha", {"sources": [{"status": "폐기", "review_required": False}]})


async def test_config_is_lazy_and_network_timeout_is_retryable(monkeypatch):
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    monkeypatch.delenv("AI_SERVICE_TOKEN", raising=False)
    client = get_rag_client()
    with pytest.raises(RagError, match="AI_NOT_CONFIGURED"):
        await client.view("alpha", "DOC-001", 1)
    def timeout(request):
        raise httpx.ReadTimeout("sensitive upstream detail", request=request)
    client = RagClient("http://ai.test", "test-token", transport=httpx.MockTransport(timeout))
    with pytest.raises(RagError) as error:
        await client.view("alpha", "DOC-001", 1)
    assert error.value.retryable and str(error.value) == "AI_UNAVAILABLE"
