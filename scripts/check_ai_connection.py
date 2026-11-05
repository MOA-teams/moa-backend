"""실제 생성 호출 없이 AI 주소·서비스 인증·tenant·worker 설정을 점검한다."""

import argparse
import asyncio
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

import httpx
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from app.core.config import Settings


async def check_connection(settings, tenant, *, transport=None):
    if not re.fullmatch(r"[a-z]{1,64}", tenant):
        raise ValueError("invalid tenant")
    report = {
        "tenant": tenant,
        "pipeline_worker_enabled": settings.ai_pipeline_worker_enabled,
        "callback_receiver_configured": bool(
            settings.ai_callback_token and settings.ai_callback_token.get_secret_value()
        ),
        "health": "not_checked",
        "service_auth": "not_checked",
        "llm_execution_verified": False,
        "ready": False,
    }
    if (
        not settings.ai_base_url
        or not settings.ai_service_token
        or not settings.ai_service_token.get_secret_value()
    ):
        report["error"] = "AI_NOT_CONFIGURED"
        return report
    try:
        async with asyncio.timeout(15), httpx.AsyncClient(
            base_url=settings.ai_base_url.rstrip("/"),
            timeout=5,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            health = await client.get("/health")
            report["health"] = "ok" if health.status_code == 200 else "unavailable"
            # 새로운 run을 만들지 않고 인증 이후의 도메인 404를 확인한다.
            response = await client.get(
                "/internal/v1/pipeline-runs/RUN-connectivity-" + uuid4().hex,
                headers={
                    "Authorization": "Bearer "
                    + settings.ai_service_token.get_secret_value(),
                    "X-Tenant-ID": tenant,
                },
            )
            if response.status_code == 404:
                try:
                    valid_missing = (
                        response.json().get("error", {}).get("code") == "RUN_NOT_FOUND"
                    )
                except (ValueError, AttributeError):
                    valid_missing = False
                report["service_auth"] = (
                    "ok" if valid_missing else "unexpected_response"
                )
            else:
                report["service_auth"] = {
                    401: "invalid_token",
                    403: "tenant_denied",
                    503: "ai_not_configured",
                }.get(response.status_code, "unexpected_response")
    except (httpx.HTTPError, TimeoutError):
        report["error"] = "AI_UNAVAILABLE"
    report["ready"] = bool(
        report["pipeline_worker_enabled"]
        and report["health"] == "ok"
        and report["service_auth"] == "ok"
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    args = parser.parse_args()
    try:
        settings = Settings(_env_file=args.env_file)
        report = asyncio.run(check_connection(settings, args.tenant))
    except (ValueError, ValidationError, OSError):
        print(json.dumps({"ready": False, "error": "INVALID_CONFIGURATION"}))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
