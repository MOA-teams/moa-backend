"""서명 검증된 push의 고정 범위를 조회하고 현재 접근 정책 안에서만 공개한다."""

import re
from datetime import datetime
from urllib.parse import quote

from sqlalchemy import select

from app.core.cursor import decode_cursor, encode_cursor
from app.core.errors import APIError
from app.core.github import GitHubError
from app.db.models import GitHubIntegration, GitHubJob, PushEvent, ReviewItem
from app.services.alerts import cursor_id
from app.services.github import staff
from app.services.github_security import exclusion, sanitize
from app.services.group_github import enabled, policy_hash

SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def clean(value, path, limit=10000):
    return sanitize(value, path)[0][:limit] if isinstance(value, str) else ""


def commit_view(row):
    if not isinstance(row, dict) or not isinstance(row.get("sha"), str) or not SHA.fullmatch(row["sha"]):
        raise GitHubError("GITHUB_INVALID_RESPONSE", retryable=False)
    info = row.get("commit", {})
    if not isinstance(info, dict):
        raise GitHubError("GITHUB_INVALID_RESPONSE", retryable=False)
    author = info.get("author") or {}
    if not isinstance(author, dict):
        raise GitHubError("GITHUB_INVALID_RESPONSE", retryable=False)
    date = author.get("date")
    try:
        if datetime.fromisoformat(date.replace("Z", "+00:00")).utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise GitHubError("GITHUB_INVALID_RESPONSE", retryable=False) from None
    return {"sha": row["sha"], "message": clean(info.get("message"), "commit_message"),
            "author": clean(author.get("name"), "commit_author", 200), "committed_at": date}


async def preserve_push(session, integration, job, event, data):
    def sha(key):
        value = data.get(key)
        return value if isinstance(value, str) and SHA.fullmatch(value) else None

    await session.flush()
    session.add(PushEvent(
        event_id=event.event_id, tenant_id=integration.tenant_id, integration_id=integration.integration_id,
        job_id=job.job_id, repository_id=integration.repository_id, repository=integration.repository,
        branch=integration.watch_branch, before_sha=sha("before"), after_sha=sha("after"),
        security_level=integration.security_level, include_paths=integration.include_paths,
        exclude_patterns=integration.exclude_patterns, document_bindings=integration.document_bindings,
    ))


def matching_policy(event, integration):
    return enabled(integration) and integration.repository_id == event.repository_id and integration.repository == event.repository


def path_exclusion(path, event, integration):
    if not isinstance(path, str) or not path or sanitize(path, "path")[1]:
        return "invalid_path"
    return exclusion(path, event.include_paths, event.exclude_patterns + ["labels/"]) or exclusion(
        path, integration.include_paths, integration.exclude_patterns + ["labels/"],
    )


async def collect_push(event, integration, github):
    if not matching_policy(event, integration):
        raise GitHubError("GITHUB_INTEGRATION_DISABLED", retryable=False)
    if not event.after_sha or event.after_sha == "0" * 40:
        return None, [], False, "PUSH_BRANCH_DELETED"
    token = await github.installation_token(integration.installation_id, integration.repository_id)
    repo = quote(event.repository, safe="/")
    head = await github.request("GET", f"/repos/{repo}/commits/{event.after_sha}", token)
    commit = commit_view(head)
    if commit["sha"] != event.after_sha:
        raise GitHubError("GITHUB_INVALID_RESPONSE", retryable=False)
    if not event.before_sha or event.before_sha == "0" * 40:
        return commit, [], False, "PUSH_BASE_UNAVAILABLE"
    comparison = await github.request("GET", f"/repos/{repo}/compare/{event.before_sha}...{event.after_sha}", token,
                                      params={"per_page": 1, "page": 1})
    files = comparison.get("files") if isinstance(comparison, dict) else None
    if not isinstance(files, list) or any(not isinstance(f, dict) for f in files):
        raise GitHubError("GITHUB_INVALID_RESPONSE", retryable=False)
    complete = len(files) < 300 and comparison.get("status") in {"ahead", "identical"}
    changes = []
    for row in files[:300]:
        path = row.get("filename")
        reason = path_exclusion(path, event, integration)
        previous = row.get("previous_filename")
        if previous and path_exclusion(previous, event, integration):
            reason = reason or "previous_path_excluded"
        display_path = clean(path, "path", 1000) or "[제외된 경로]"
        item = {"path": display_path, "previous_path": clean(previous, "path", 1000) or None,
                "summary": "제외된 파일" if reason else f"{row.get('status', 'changed')} 파일 변경",
                "excluded": bool(reason), "exclude_reason": reason, "diff": None, "diff_complete": False}
        if not reason:
            additions, deletions = row.get("additions", 0), row.get("deletions", 0)
            if type(additions) is not int or type(deletions) is not int:
                raise GitHubError("GITHUB_INVALID_RESPONSE", retryable=False)
            item["summary"] += f" (+{max(additions, 0)} / -{max(deletions, 0)})"
            if min(event.security_level, integration.security_level) >= 2:
                patch = row.get("patch")
                if isinstance(patch, str):
                    item["diff"] = clean(patch, path, 20000)
                    lines = patch.splitlines()
                    item["diff_complete"] = (len(patch) <= 20000 and not sanitize(patch, path)[1]
                        and sum(line.startswith("+") for line in lines) == additions
                        and sum(line.startswith("-") for line in lines) == deletions)
                complete = complete and item["diff_complete"]
        changes.append(item)
    commit["files"] = [c["path"] for c in changes if not c["excluded"]]
    return commit, changes, complete, None if complete else "GITHUB_DIFF_INCOMPLETE"


async def event_view(session, event, integration):
    job = await session.get(GitHubJob, event.job_id)
    permitted = matching_policy(event, integration)
    changes = []
    for entry in event.changes:
        value = dict(entry)
        reason = path_exclusion(value["path"], event, integration) if permitted else "integration_unavailable"
        if permitted and value.get("previous_path") and path_exclusion(value["previous_path"], event, integration):
            reason = reason or "previous_path_excluded"
        if reason:
            value.update(excluded=True, exclude_reason=reason, diff=None, diff_complete=False, summary="제외된 파일")
        elif min(event.security_level, integration.security_level) < 2:
            value.update(diff=None, diff_complete=False)
        changes.append(value)
    affected = (await session.execute(select(ReviewItem.target_type, ReviewItem.target_id).where(
        ReviewItem.tenant_id == event.tenant_id,
        ReviewItem.cause_keys.contains([f"push:{event.event_id}"]),
    ))).all()
    commit = dict(event.commit_info) if event.commit_info else None
    if commit:
        commit["files"] = [c["path"] for c in changes if not c["excluded"]]
    return {"push_event_id": event.public_id, "repo": event.repository, "branch": event.branch,
            "before_sha": event.before_sha, "after_sha": event.after_sha, "commit": commit,
            "summary": event.summary, "summary_kind": "change_statistics", "changes": changes,
            "affected_draft_ids": sorted({target for kind, target in affected if kind == "draft"}),
            "affected_document_ids": sorted({target for kind, target in affected if kind == "document"}),
            "status": event.status, "complete": event.complete, "error_code": event.error_code,
            "sync_status": job.phase, "sync_error_code": job.error_code, "created_at": event.created_at}


async def list_events(session, user, cursor, limit):
    tenant = staff(user)
    scope = {"type": "push_events", "tenant": tenant, "user": user.internal_user_id}
    position = cursor_id(cursor, scope)
    statement = select(PushEvent).where(PushEvent.tenant_id == tenant)
    if position:
        statement = statement.where(PushEvent.event_id < position)
    rows = list((await session.scalars(statement.order_by(PushEvent.event_id.desc()).limit(limit + 1))).all())
    more, rows = len(rows) > limit, rows[:limit]
    integration = await session.scalar(select(GitHubIntegration).where(GitHubIntegration.tenant_id == tenant))
    return {"items": [await event_view(session, row, integration) for row in rows], "has_more": more,
            "next_cursor": encode_cursor(scope, {"id": rows[-1].event_id}) if more else None}


async def recent_commits(session, user, github, cursor, limit):
    tenant = staff(user)
    integration = await session.scalar(select(GitHubIntegration).where(GitHubIntegration.tenant_id == tenant))
    if not enabled(integration):
        raise APIError(403, "GITHUB_INTEGRATION_REQUIRED", "보안 레벨 1 이상의 GitHub 연결이 필요합니다.")
    identity = (integration.repository_id, integration.repository, integration.installation_id, policy_hash(integration))
    scope = {"type": "github_commits", "tenant": tenant, "user": user.internal_user_id, "target": list(identity)}
    position = decode_cursor(cursor, scope)
    token = await github.installation_token(integration.installation_id, integration.repository_id)
    await session.commit()
    if position is None:
        response = await github.head(integration.repository, integration.watch_branch, token)
        if not isinstance(response, dict) or not isinstance(response.get("sha"), str) or not SHA.fullmatch(response["sha"]):
            raise GitHubError("GITHUB_INVALID_RESPONSE", retryable=False)
        head = response["sha"]
        offset = 0
    else:
        head, offset = position.get("head"), position.get("offset")
    if not isinstance(head, str) or not SHA.fullmatch(head) or type(offset) is not int or not 0 <= offset <= 100000:
        raise APIError(400, "INVALID_CURSOR", "유효한 조회 cursor가 필요합니다.")
    page, skip = divmod(offset, 100)
    rows = []
    while len(rows) <= limit:
        data = await github.request("GET", f"/repos/{integration.repository}/commits", token,
                                    params={"sha": head, "per_page": 100, "page": page + 1})
        if not isinstance(data, list):
            raise GitHubError("GITHUB_INVALID_RESPONSE", retryable=False)
        rows.extend(data[skip:])
        if len(data) < 100:
            break
        page, skip = page + 1, 0
    await session.refresh(integration)
    if not enabled(integration) or identity != (integration.repository_id, integration.repository, integration.installation_id, policy_hash(integration)):
        raise APIError(409, "GITHUB_POLICY_CHANGED", "GitHub 연결 설정이 변경되었습니다. 다시 조회하세요.")
    more, rows = len(rows) > limit, rows[:limit]
    return {"items": [commit_view(row) for row in rows], "has_more": more,
            "next_cursor": encode_cursor(scope, {"head": head, "offset": offset + len(rows)}) if more else None}
