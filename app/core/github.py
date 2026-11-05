"""GitHub의 고정 HTTPS API만 호출한다. 외부 오류 원문과 자격 증명은 전달하지 않는다."""

import asyncio
import base64
import hashlib
import time
from urllib.parse import quote, urlencode

import httpx
import jwt
from app.core.config import get_settings


class GitHubError(Exception):
    def __init__(self, code="GITHUB_UNAVAILABLE", *, retryable=True, retry_after=30):
        self.code, self.retryable, self.retry_after = (
            code,
            retryable,
            min(max(retry_after, 5), 3600),
        )
        super().__init__(code)


class GitHubClient:
    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport

    def require_config(self, oauth=False):
        names = ["github_app_id", "github_app_private_key_base64"]
        if oauth:
            names += [
                "github_client_id",
                "github_client_secret",
                "github_redirect_uri",
                "github_app_slug",
            ]
        if not all(getattr(self.settings, name) for name in names):
            raise GitHubError("GITHUB_NOT_CONFIGURED", retryable=False)

    def app_token(self):
        self.require_config()
        try:
            key = base64.b64decode(
                self.settings.github_app_private_key_base64.get_secret_value(),
                validate=True,
            )
            now = int(time.time())
            return jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": self.settings.github_app_id},
                key,
                algorithm="RS256",
            )
        except Exception:
            raise GitHubError("GITHUB_KEY_INVALID", retryable=False) from None

    async def request(
        self, method, path, token=None, *, payload=None, params=None, oauth=False
    ):
        base = "https://github.com" if oauth else "https://api.github.com"
        headers = {
            "Accept": "application/json" if oauth else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with asyncio.timeout(10), httpx.AsyncClient(
                base_url=base,
                timeout=10,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    method, path, headers=headers, json=payload, params=params
                ) as response:
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 8 * 1024 * 1024:
                            raise GitHubError(
                                "GITHUB_RESPONSE_TOO_LARGE", retryable=False
                            )
                        chunks.append(chunk)
                    import json

                    if response.status_code in (403, 429) and (
                        response.headers.get("x-ratelimit-remaining") == "0"
                        or "retry-after" in response.headers
                        or response.status_code == 429
                    ):
                        try:
                            delay = int(response.headers.get("retry-after", "30"))
                        except ValueError:
                            delay = 30
                        raise GitHubError("GITHUB_RATE_LIMITED", retry_after=delay)
                    if response.status_code not in (200, 201):
                        code = (
                            "GITHUB_ACCESS_DENIED"
                            if response.status_code in (401, 403, 404)
                            else "GITHUB_REQUEST_FAILED"
                        )
                        raise GitHubError(code, retryable=response.status_code >= 500)
                    return json.loads(b"".join(chunks))
        except (httpx.HTTPError, ValueError, TimeoutError):
            raise GitHubError() from None

    def authorization_url(self, state, verifier):
        self.require_config(oauth=True)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        return "https://github.com/login/oauth/authorize?" + urlencode(
            {
                "client_id": self.settings.github_client_id,
                "redirect_uri": self.settings.github_redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )

    async def authorized_repositories(self, code, verifier):
        self.require_config(oauth=True)
        result = await self.request(
            "POST",
            "/login/oauth/access_token",
            oauth=True,
            payload={
                "client_id": self.settings.github_client_id,
                "client_secret": self.settings.github_client_secret.get_secret_value(),
                "code": code,
                "redirect_uri": self.settings.github_redirect_uri,
                "code_verifier": verifier,
            },
        )
        token = result.get("access_token")
        if not token:
            raise GitHubError("GITHUB_AUTHORIZATION_FAILED", retryable=False)
        installs = await self.pages(
            "/user/installations", token, "installations", limit=5
        )
        repositories = []
        for installation in installs:
            if str(
                installation.get("app_id")
            ) != self.settings.github_app_id or installation.get("suspended_at"):
                continue
            iid = installation["id"]
            for repo in await self.pages(
                f"/user/installations/{iid}/repositories",
                token,
                "repositories",
                limit=10,
            ):
                if repo.get("permissions", {}).get("admin"):
                    repositories.append(
                        {
                            "installation_id": iid,
                            "repository_id": repo["id"],
                            "repository": repo["full_name"],
                            "default_branch": repo["default_branch"],
                        }
                    )
        return repositories

    async def pages(self, path, token, key=None, *, limit=10):
        items = []
        for page in range(1, limit + 1):
            data = await self.request(
                "GET", path, token, params={"per_page": 100, "page": page}
            )
            batch = data[key] if key else data
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise GitHubError("GITHUB_LIST_LIMIT", retryable=False)

    async def installation_token(self, iid, repository_id, *, issues_write=False):
        result = await self.request(
            "POST",
            f"/app/installations/{iid}/access_tokens",
            self.app_token(),
            payload={
                "repository_ids": [repository_id],
                "permissions": (
                    {"issues": "write", "metadata": "read"}
                    if issues_write
                    else {"contents": "read", "metadata": "read"}
                ),
            },
        )
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("token"), str)
            or not result["token"]
        ):
            raise GitHubError("GITHUB_INVALID_RESPONSE")
        if issues_write and (
            not isinstance(result.get("permissions"), dict)
            or result["permissions"].get("issues") != "write"
        ):
            raise GitHubError("GITHUB_ISSUES_PERMISSION_REQUIRED", retryable=False)
        return result["token"]

    async def branches(self, repo, token):
        return await self.pages(f"/repos/{repo}/branches", token, limit=10)

    async def head(self, repo, branch, token):
        return await self.request(
            "GET", f"/repos/{repo}/commits/{quote(branch, safe='')}", token
        )

    async def tree(self, repo, sha, token):
        result = await self.request(
            "GET", f"/repos/{repo}/git/trees/{sha}", token, params={"recursive": "1"}
        )
        if result.get("truncated"):
            raise GitHubError("GITHUB_TREE_TRUNCATED", retryable=False)
        return result["tree"]

    async def blob(self, repo, sha, token):
        result = await self.request("GET", f"/repos/{repo}/git/blobs/{sha}", token)
        if result.get("encoding") != "base64" or result.get("size", 0) > 200000:
            raise GitHubError("GITHUB_FILE_TOO_LARGE", retryable=False)
        try:
            data = base64.b64decode(result["content"].replace("\n", ""), validate=True)
            if len(data) > 200000 or b"\x00" in data:
                raise ValueError
            return data.decode("utf-8")
        except (ValueError, UnicodeError):
            raise GitHubError("GITHUB_FILE_UNSUPPORTED", retryable=False) from None


def get_github_client():
    return GitHubClient(get_settings())
