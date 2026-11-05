"""moa-ai I03~I05 계약 경계. 외부 오류 본문과 토큰은 로그·공개 응답에 노출하지 않는다."""
import asyncio
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.config import get_settings

AI_STATUS = {"valid": "유효", "retired": "폐기"}
INDEX_STATUS = {"대기": "pending", "처리 중": "running", "완료": "completed", "실패": "failed"}


class RagError(Exception):
    def __init__(self, code="AI_UNAVAILABLE", *, retryable=True, status=503):
        self.code, self.retryable, self.status = code, retryable, status
        super().__init__(code)


class Accepted(BaseModel):
    model_config = ConfigDict(strict=True)
    source_id: str
    version: int
    indexing_status: Literal["대기", "처리 중", "완료", "실패"]


class SourceView(Accepted):
    source_revision: int
    status: Literal["유효", "폐기"]
    review_required: bool
    searchable: bool
    error: str | None = None


class StatusResult(BaseModel):
    model_config = ConfigDict(strict=True)
    knowledge_revision: int
    updated_count: int
    search_excluded: bool


class RagClient:
    def __init__(self, base_url: str | None, token: str | None, *, transport=None):
        self.base_url, self.token, self.transport = base_url, token, transport

    async def request(self, method: str, path: str, tenant: str, model, *, expected: int, payload=None, params=None):
        if not self.base_url or not self.token:
            raise RagError("AI_NOT_CONFIGURED", retryable=False)
        try:
            async with httpx.AsyncClient(base_url=self.base_url.rstrip("/"), timeout=10, transport=self.transport) as client:
                response = await asyncio.wait_for(client.request(
                    method, path, headers={"Authorization": f"Bearer {self.token}", "X-Tenant-ID": tenant},
                    json=payload, params=params,
                ), timeout=10)
            if response.status_code != expected:
                codes = {"SOURCE_NOT_FOUND", "STALE_SOURCE_REVISION", "SOURCE_CONFLICT", "EVENT_CONFLICT"}
                try:
                    code = response.json().get("error", {}).get("code")
                except (ValueError, AttributeError):
                    code = None
                raise RagError(code if code in codes else "AI_REQUEST_REJECTED",
                               retryable=response.status_code >= 500 or response.status_code == 429,
                               status=response.status_code)
            return model.model_validate(response.json())
        except (httpx.HTTPError, TimeoutError):
            raise RagError() from None
        except (ValidationError, ValueError):
            raise RagError("AI_INVALID_RESPONSE") from None

    async def submit(self, tenant: str, payload: dict) -> Accepted:
        result = await self.request("POST", "/internal/v1/rag/sources", tenant, Accepted, expected=202, payload=payload)
        if (result.source_id, result.version) != (payload["source_id"], payload["version"]):
            raise RagError("AI_INVALID_RESPONSE")
        return result

    async def statuses(self, tenant: str, payload: dict) -> StatusResult:
        result = await self.request("POST", "/internal/v1/rag/source-statuses", tenant, StatusResult, expected=200, payload=payload)
        if result.updated_count != len(payload["sources"]):
            raise RagError("AI_INVALID_RESPONSE")
        if all(s["status"] == "폐기" or s["review_required"] for s in payload["sources"]) and not result.search_excluded:
            raise RagError("AI_BLOCK_NOT_CONFIRMED")
        return result

    async def view(self, tenant: str, source_id: str, version: int) -> SourceView:
        result = await self.request("GET", f"/internal/v1/rag/sources/{quote(source_id, safe='')}/versions/{version}",
                                    tenant, SourceView, expected=200, params={"source_type": "문서"})
        if (result.source_id, result.version) != (source_id, version):
            raise RagError("AI_INVALID_RESPONSE")
        return result


def get_rag_client() -> RagClient:
    settings = get_settings()
    token = settings.ai_service_token.get_secret_value() if settings.ai_service_token else None
    return RagClient(settings.ai_base_url, token)
