"""고정 AI 주소로만 통신한다. 인증 토큰·원격 오류 본문은 공개하지 않는다."""

import asyncio
from urllib.parse import quote
import httpx
from pydantic import ValidationError
from app.core.config import get_settings
from app.schemas.analysis import AIMessageAccepted, PipelineResult


class PipelineTransportError(Exception):
    def __init__(self, code="AI_UNAVAILABLE", *, retryable=True, status=503):
        self.code, self.retryable, self.status = code, retryable, status
        super().__init__(code)


class PipelineClient:
    def __init__(self, base_url, token, *, transport=None):
        self.base_url, self.token, self.transport = base_url, token, transport

    async def request(
        self, method, path, tenant, model, expected, *, payload=None, key=None
    ):
        if not self.base_url or not self.token:
            raise PipelineTransportError("AI_NOT_CONFIGURED", retryable=False)
        headers = {"Authorization": f"Bearer {self.token}", "X-Tenant-ID": tenant}
        if key:
            headers["Idempotency-Key"] = key
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"),
                timeout=10,
                transport=self.transport,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await asyncio.wait_for(
                    client.request(method, path, headers=headers, json=payload),
                    timeout=10,
                )
            if response.status_code != expected:
                raise PipelineTransportError(
                    "AI_RUN_NOT_FOUND"
                    if response.status_code == 404
                    else "AI_REQUEST_REJECTED",
                    retryable=response.status_code >= 500
                    or response.status_code == 429,
                    status=response.status_code,
                )
            if len(response.content) > 2 * 1024 * 1024:
                raise PipelineTransportError("AI_INVALID_RESPONSE", retryable=False)
            return model.model_validate(response.json())
        except (httpx.HTTPError, TimeoutError):
            raise PipelineTransportError() from None
        except (ValidationError, ValueError):
            raise PipelineTransportError(
                "AI_INVALID_RESPONSE", retryable=False
            ) from None

    async def submit(self, snapshot, key):
        accepted = await self.request(
            "POST",
            "/internal/v1/pipeline-runs",
            snapshot["tenant_id"],
            AIMessageAccepted,
            202,
            payload=snapshot,
            key=key,
        )
        if (accepted.inquiry_id, accepted.inquiry_revision) != (
            snapshot["inquiry_id"],
            snapshot["inquiry_revision"],
        ):
            raise PipelineTransportError("AI_INVALID_RESPONSE", retryable=False)
        return accepted

    async def get(self, tenant, run_id):
        return await self.request(
            "GET",
            "/internal/v1/pipeline-runs/" + quote(run_id, safe=""),
            tenant,
            PipelineResult,
            200,
        )


def get_pipeline_client():
    settings = get_settings()
    token = (
        settings.ai_service_token.get_secret_value()
        if settings.ai_service_token
        else None
    )
    return PipelineClient(settings.ai_base_url, token)
