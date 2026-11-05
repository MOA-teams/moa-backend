"""Issue HTTP 경계. POST 결과 불명확성과 안전한 재시도를 구분한다."""

import asyncio
from dataclasses import dataclass, field
import re

import httpx


class IssueError(Exception):
    def __init__(self, code, *, retryable=False, uncertain=False, retry_after=0):
        self.code, self.retryable, self.uncertain = code, retryable, uncertain
        self.retry_after = retry_after
        super().__init__(code)


def get_issue_gateway(factory):
    from app.services.group_github import IntegrationIssueGateway

    return IntegrationIssueGateway(factory)


@dataclass(frozen=True)
class VerifiedIssueTarget:
    repository: str
    token: str = field(repr=False)


class HttpIssueGateway:
    def __init__(self, targets: dict[str, VerifiedIssueTarget], *, transport=None):
        self.targets, self.transport = targets, transport
        for target in targets.values():
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", target.repository):
                raise ValueError("검증된 GitHub 저장소 경로가 필요합니다.")

    async def ready(self, tenant):
        return tenant in self.targets

    async def prepare(self, tenant, payload):
        return payload

    async def _request(self, tenant, method, *, payload=None, page=None):
        target = self.targets[tenant]
        try:
            async with httpx.AsyncClient(
                base_url="https://api.github.com",
                timeout=10,
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await asyncio.wait_for(
                    client.request(
                        method,
                        f"/repos/{target.repository}/issues",
                        headers={
                            "Authorization": f"Bearer {target.token}",
                            "Accept": "application/vnd.github+json",
                            "X-GitHub-Api-Version": "2026-03-10",
                        },
                        json=payload,
                        params=(
                            {"state": "all", "per_page": 100, "page": page}
                            if page
                            else None
                        ),
                    ),
                    timeout=10,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise IssueError("GITHUB_CONNECTION_FAILED", retryable=True) from None
        except (httpx.HTTPError, TimeoutError):
            raise IssueError(
                "GITHUB_RESPONSE_UNKNOWN",
                retryable=method == "GET",
                uncertain=method == "POST",
            ) from None
        if response.status_code == (201 if method == "POST" else 200):
            try:
                return response.json()
            except ValueError:
                raise IssueError(
                    "GITHUB_INVALID_RESPONSE",
                    retryable=method == "GET",
                    uncertain=method == "POST",
                ) from None
        if response.status_code == 429 or (
            response.status_code == 403
            and (
                response.headers.get("x-ratelimit-remaining") == "0"
                or response.headers.get("retry-after")
            )
        ):
            try:
                delay = max(
                    1, min(int(response.headers.get("retry-after", "60")), 3600)
                )
            except ValueError:
                delay = 60
            raise IssueError("GITHUB_RATE_LIMITED", retryable=True, retry_after=delay)
        if response.status_code in (401, 403, 404, 422):
            raise IssueError("GITHUB_REQUEST_REJECTED")
        raise IssueError(
            "GITHUB_UNAVAILABLE", retryable=method == "GET", uncertain=method == "POST"
        )

    def _url(self, tenant, row):
        number = row.get("number") if isinstance(row, dict) else None
        expected = (
            f"https://github.com/{self.targets[tenant].repository}/issues/{number}"
        )
        if type(number) is not int or number < 1 or row.get("html_url") != expected:
            raise IssueError("GITHUB_INVALID_RESPONSE", uncertain=True)
        return expected

    async def find(self, tenant, marker):
        # 제한에 도달했으면 '없음'으로 단정하지 않는다. PR도 /issues에 나오므로 제외한다.
        for page in range(1, 101):
            rows = await self._request(tenant, "GET", page=page)
            if not isinstance(rows, list):
                raise IssueError("GITHUB_INVALID_RESPONSE", retryable=True)
            if any(
                not isinstance(row, dict)
                or type(row.get("number")) is not int
                or (row.get("body") is not None and not isinstance(row["body"], str))
                for row in rows
            ):
                raise IssueError("GITHUB_INVALID_RESPONSE", retryable=True)
            matches = [
                row
                for row in rows
                if isinstance(row, dict)
                and "pull_request" not in row
                and marker in (row.get("body") or "")
            ]
            if matches:
                try:
                    return self._url(
                        tenant, min(matches, key=lambda row: row["number"])
                    )
                except IssueError:
                    raise IssueError(
                        "GITHUB_INVALID_RESPONSE", retryable=True
                    ) from None
            if len(rows) < 100:
                return None
        raise IssueError("GITHUB_RECONCILIATION_LIMIT")

    async def create(self, tenant, payload):
        row = await self._request(
            tenant,
            "POST",
            payload={key: payload[key] for key in ("title", "body", "labels")},
        )
        return self._url(tenant, row)
