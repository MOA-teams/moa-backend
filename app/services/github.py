"""설치 소유권·정책 변경·Webhook 접수. GitHub 원문 수집은 worker 책임이다."""

import hashlib
import hmac
import json
import secrets
import re
from datetime import timedelta
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from app.core.enums import UserRole
from app.core.errors import APIError
from app.core.github import GitHubError
from app.db.models import (
    Document,
    GitHubAuthorization,
    GitHubIntegration,
    GitHubEvent,
    GitHubJob,
)
from app.services.attachments import require_tenant
from app.services.analysis import utcnow
from app.services.knowledge import lock_knowledge
from app.services.inquiries import begin_idempotent_request
from app.schemas.github import BASE_EXCLUDES, GitHubAccepted, GitHubStatus


def staff(user, *, admin=False):
    if user.role not in (
        (UserRole.ADMIN,) if admin else (UserRole.ADMIN, UserRole.AGENT)
    ):
        raise APIError(403, "FORBIDDEN", "관리자 권한이 필요합니다.")
    return require_tenant(user)


def digest(value):
    return hashlib.sha256(
        value.encode() if isinstance(value, str) else value
    ).hexdigest()


def integration_id(tenant):
    return "github_" + tenant


async def start_authorization(session, user, github):
    tenant = staff(user, admin=True)
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    url = github.authorization_url(state, verifier)
    session.add(
        GitHubAuthorization(
            state_hash=digest(state),
            user_id=user.internal_user_id,
            tenant_id=tenant,
            verifier=verifier,
            phase="pending",
            repositories=[],
            expires_at=utcnow() + timedelta(minutes=10),
        )
    )
    await session.commit()
    return {
        "authorization_url": url,
        "installation_url": f"https://github.com/apps/{quote(github.settings.github_app_slug, safe='')}/installations/new",
        "expires_in": 600,
    }


async def authorization(session, user, state, *, phases):
    tenant = staff(user, admin=True)
    row = await session.get(
        GitHubAuthorization, digest(state), with_for_update=True, populate_existing=True
    )
    if (
        row is None
        or row.user_id != user.internal_user_id
        or row.tenant_id != tenant
        or row.expires_at <= utcnow()
        or row.phase not in phases
    ):
        raise APIError(
            400,
            "GITHUB_AUTHORIZATION_INVALID",
            "인증이 만료되었거나 유효하지 않습니다. GitHub 연결을 다시 시작해 주세요.",
        )
    return row


async def complete_authorization(session, user, body, github):
    row = await authorization(session, user, body.state, phases={"pending"})
    verifier = row.verifier
    row.phase, row.verifier = "exchanging", None
    await session.commit()
    try:
        repos = await github.authorized_repositories(
            body.code.get_secret_value(), verifier
        )
        row = await authorization(session, user, body.state, phases={"exchanging"})
        row.repositories, row.phase = repos, "verified"
        row.expires_at = utcnow() + timedelta(minutes=30)
        await session.commit()
        return {
            "authorization_id": body.state,
            "repositories": repos,
            "expires_in": 1800,
        }
    except Exception:
        await session.rollback()
        raise


async def repository_selection(
    session, user, authorization_id, repository_id, github, branch=None
):
    row = await authorization(session, user, authorization_id, phases={"verified"})
    repo = next(
        (r for r in row.repositories if r["repository_id"] == repository_id), None
    )
    if repo is None:
        raise APIError(
            404, "GITHUB_REPOSITORY_NOT_FOUND", "연결 가능한 저장소가 아닙니다."
        )
    await session.commit()
    token = await github.installation_token(repo["installation_id"], repository_id)
    if branch is None:
        return {
            "branches": [
                {"name": b["name"], "sha": b["commit"]["sha"]}
                for b in await github.branches(repo["repository"], token)
            ]
        }
    head = await github.head(repo["repository"], branch, token)
    tree = await github.tree(repo["repository"], head["commit"]["tree"]["sha"], token)
    if len(tree) > 10000:
        raise GitHubError("GITHUB_TREE_LIMIT", retryable=False)
    return {
        "sha": head["sha"],
        "paths": [e["path"] for e in tree if e["type"] == "tree"],
    }


async def enqueue(session, integration, *, report=None):
    integration.pending_bindings = [
        dict(t)
        for t in {
            tuple(sorted(b.items()))
            for b in (integration.pending_bindings or [])
            + (integration.document_bindings or [])
        }
    ]
    integration.generation += 1
    integration.status, integration.error_code = "동기화 중", None
    job = GitHubJob(
        integration_id=integration.integration_id,
        generation=integration.generation,
        phase="discover",
        report=report or {},
    )
    session.add(job)
    # 새 이벤트 이후 만들어진 초안도 동기화 완료 전의 결과와 구분한다.
    tenant = await lock_knowledge(session, integration.tenant_id)
    tenant.knowledge_revision += 1
    return job


async def connect(session, user, body, key, github):
    tenant = staff(user, admin=True)
    try:
        record = await begin_idempotent_request(
            session,
            user_id=user.internal_user_id,
            path="/admin/integrations/github",
            key=key,
            payload=body,
        )
        if record.response:
            response = GitHubAccepted.model_validate(record.response)
            await session.commit()
            return response
        proof, repo = None, None
        if body.security_level:
            proof = await authorization(
                session, user, body.authorization_id, phases={"verified"}
            )
            repo = next(
                (
                    r
                    for r in proof.repositories
                    if r["repository"].lower() == body.repository.lower()
                    and r["installation_id"] == body.installation_id
                ),
                None,
            )
            if repo is None:
                raise APIError(
                    403,
                    "GITHUB_OWNERSHIP_REQUIRED",
                    "해당 설치와 저장소의 관리자 권한을 확인할 수 없습니다.",
                )
            token = await github.installation_token(
                body.installation_id, repo["repository_id"]
            )
            await github.head(repo["repository"], body.watch_branch, token)
        await lock_knowledge(session, tenant)
        integration = await session.get(
            GitHubIntegration,
            integration_id(tenant),
            with_for_update=True,
            populate_existing=True,
        )
        if integration is None:
            integration = GitHubIntegration(
                integration_id=integration_id(tenant), tenant_id=tenant, generation=0
            )
            session.add(integration)
        for binding in body.document_bindings:
            if not await session.scalar(
                select(Document.doc_id).where(
                    Document.tenant_id == tenant,
                    Document.ai_source_id == binding.doc_id,
                )
            ):
                raise APIError(
                    404, "DOCUMENT_NOT_FOUND", "연결할 문서를 찾을 수 없습니다."
                )
        # 다른 기업이 이미 연결한 저장소는 재사용하지 않는다.
        if repo and await session.scalar(
            select(GitHubIntegration.integration_id).where(
                GitHubIntegration.repository_id == repo["repository_id"],
                GitHubIntegration.tenant_id != tenant,
            )
        ):
            raise APIError(
                409, "GITHUB_REPOSITORY_CONNECTED", "이미 연결된 저장소입니다."
            )
        old_bindings = (integration.pending_bindings or []) + (
            integration.document_bindings or []
        )
        integration.pending_bindings = old_bindings
        if repo:
            integration.installation_id, integration.repository_id = (
                body.installation_id,
                repo["repository_id"],
            )
            integration.repository, integration.watch_branch = (
                repo["repository"],
                body.watch_branch,
            )
        integration.security_level, integration.enabled = body.security_level, bool(
            body.security_level
        )
        integration.include_paths = body.include_paths
        integration.exclude_patterns = list(
            dict.fromkeys(BASE_EXCLUDES + body.exclude_patterns)
        )
        integration.document_bindings = [b.model_dump() for b in body.document_bindings]
        await enqueue(session, integration, report={"previous_bindings": old_bindings})
        if proof:
            proof.phase = "used"
        response = GitHubAccepted(
            integration_id=integration.integration_id, status=integration.status
        )
        record.response = response.model_dump(mode="json")
        await session.commit()
        return response
    except IntegrityError:
        await session.rollback()
        raise APIError(
            409, "GITHUB_REPOSITORY_CONNECTED", "이미 연결된 저장소입니다."
        ) from None
    except Exception:
        await session.rollback()
        raise


async def status(session, user):
    tenant = staff(user)
    row = await session.get(GitHubIntegration, integration_id(tenant))
    if row is None:
        return {
            "integration_id": integration_id(tenant),
            "status": "연동 안 함",
            "security_level": 0,
            "installation_id": None,
            "repository": None,
            "watch_branch": "main",
            "include_paths": [],
            "exclude_patterns": BASE_EXCLUDES,
            "document_bindings": [],
            "analysis_provider": "none",
            "last_synced_sha": None,
            "last_synced_at": None,
            "error_code": None,
            "report": {},
        }
    return {
        "integration_id": row.integration_id,
        "analysis_provider": "external_llm" if row.security_level else "none",
        "last_synced_at": (
            row.last_synced_at.isoformat() if row.last_synced_at else None
        ),
        **{
            name: getattr(row, name)
            for name in (
                "installation_id",
                "repository",
                "watch_branch",
                "security_level",
                "include_paths",
                "exclude_patterns",
                "document_bindings",
                "status",
                "last_synced_sha",
                "error_code",
                "report",
            )
        },
    }


async def webhook(session, *, raw, signature, delivery, event_type, secret):
    if not secret:
        raise APIError(
            503, "GITHUB_NOT_CONFIGURED", "GitHub Webhook 설정이 필요합니다."
        )
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if (
        not signature
        or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature)
        or not hmac.compare_digest(expected, signature)
    ):
        raise APIError(
            401, "GITHUB_SIGNATURE_INVALID", "Webhook 서명이 올바르지 않습니다."
        )
    if not delivery or len(delivery) > 200 or not event_type or len(event_type) > 60:
        raise APIError(400, "GITHUB_EVENT_INVALID", "이벤트 헤더가 필요합니다.")
    try:
        data = json.loads(raw)
        iid = data.get("installation", {}).get("id")
        rid = data.get("repository", {}).get("id")
        if "repositories_removed" in data and (
            not isinstance(data["repositories_removed"], list)
            or any(not isinstance(r, dict) for r in data["repositories_removed"])
        ):
            raise ValueError
    except (ValueError, AttributeError):
        raise APIError(
            400, "GITHUB_EVENT_INVALID", "이벤트 형식이 올바르지 않습니다."
        ) from None
    fingerprint = digest(event_type.encode() + b"\x00" + raw)
    try:
        created_id = await session.scalar(
            insert(GitHubEvent)
            .values(
                delivery_id=delivery,
                fingerprint=fingerprint,
                event_type=event_type,
                accepted=False,
            )
            .on_conflict_do_nothing()
            .returning(GitHubEvent.event_id)
        )
        received = await session.scalar(
            select(GitHubEvent)
            .where(GitHubEvent.delivery_id == delivery)
            .with_for_update()
        )
        if received.fingerprint != fingerprint:
            raise APIError(
                409,
                "GITHUB_DELIVERY_CONFLICT",
                "같은 delivery ID에 다른 본문이 전달되었습니다.",
            )
        if created_id is None:
            await session.commit()
            return {
                "accepted": received.accepted,
                "event_id": f"EVENT-{received.event_id:06d}",
            }
        rows = (
            list(
                (
                    await session.scalars(
                        select(GitHubIntegration).where(
                            GitHubIntegration.installation_id == iid
                        )
                    )
                ).all()
            )
            if type(iid) is int
            else []
        )
        for row in sorted(rows, key=lambda r: r.tenant_id):
            await lock_knowledge(session, row.tenant_id)
            row = await session.get(
                GitHubIntegration,
                row.integration_id,
                with_for_update=True,
                populate_existing=True,
            )
            # 인증 설정 변경과 경합한 이벤트는 잠금 이후의 설치·저장소로 다시 판정한다.
            if row.installation_id != iid or not row.enabled:
                continue
            revoke = event_type == "installation" and data.get("action") in {
                "deleted",
                "suspend",
            }
            removed = (
                event_type == "installation_repositories"
                and data.get("action") == "removed"
                and any(
                    isinstance(r, dict) and r.get("id") == row.repository_id
                    for r in (data.get("repositories_removed") or [])
                    if isinstance(data.get("repositories_removed"), list)
                )
            )
            push = (
                event_type == "push"
                and rid == row.repository_id
                and data.get("ref") == f"refs/heads/{row.watch_branch}"
            )
            if not (revoke or removed or push) or not row.enabled:
                continue
            if revoke or removed or data.get("deleted"):
                row.enabled = False
            job = await enqueue(session, row)
            received.integration_id, received.accepted = row.integration_id, True
            if push:
                from app.services.push_events import preserve_push
                await preserve_push(session, row, job, received, data)
        await session.commit()
        return {
            "accepted": received.accepted,
            "event_id": f"EVENT-{received.event_id:06d}",
        }
    except Exception:
        await session.rollback()
        raise
