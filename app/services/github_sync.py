"""GitHub 스냅샷과 RAG 검색 차단. 외부 전달 의도는 호출 전에 영속화한다."""

import copy
from pathlib import PurePosixPath
from uuid import uuid4

from sqlalchemy import select
from app.core.github import GitHubError
from app.core.rag import RagError
from app.db.models import (
    Document,
    DocumentVersion,
    DocumentJob,
    GitHubIntegration,
    GitHubJob,
    GitHubSource,
)
from app.services.analysis import utcnow
from app.services.document_indexing import status_event
from app.services.knowledge import lock_knowledge
from app.services.github_security import (
    exclusion,
    includes,
    sanitize,
    source_payload,
    EXTENSIONS,
)


async def locked_job(session, job_id):
    target = await session.scalar(
        select(GitHubIntegration.tenant_id)
        .join(GitHubJob)
        .where(GitHubJob.job_id == job_id)
    )
    if target is None:
        return None, None
    await lock_knowledge(session, target)
    job = await session.get(GitHubJob, job_id, populate_existing=True)
    integration = await session.get(
        GitHubIntegration,
        job.integration_id,
        with_for_update=True,
        populate_existing=True,
    )
    job = await session.get(
        GitHubJob, job_id, with_for_update=True, populate_existing=True
    )
    return integration, job


def current(integration, job, token):
    return (
        job is not None
        and job.lease_token == token
        and job.phase not in {"done", "failed", "superseded"}
        and job.generation == integration.generation
    )


async def collect(integration, old_sources, github):
    report = {"indexed_files": [], "excluded": [], "masked": [], "outside_watch": 0}
    if not integration.enabled:
        return None, [], report
    token = await github.installation_token(
        integration.installation_id, integration.repository_id
    )
    head = await github.head(integration.repository, integration.watch_branch, token)
    sha = head["sha"]
    tree = await github.tree(
        integration.repository, head["commit"]["tree"]["sha"], token
    )
    if len(tree) > 10000:
        raise GitHubError("GITHUB_TREE_LIMIT", retryable=False)
    message, findings = sanitize(head["commit"]["message"], "commit_message")
    message = message[:10000]
    report["masked"].extend(findings)
    old = {s.path: s for s in old_sources if s.phase != "retired"}
    manifest, total = [], 0
    for entry in tree:
        if entry["type"] != "blob":
            continue
        path = entry["path"]
        reason = exclusion(
            path, integration.include_paths, integration.exclude_patterns
        )
        if reason == "outside_watch":
            report["outside_watch"] += 1
            continue
        if not reason and entry.get("mode") not in {"100644", "100755"}:
            reason = "non_regular_file"
        if (
            not reason
            and integration.security_level == 2
            and PurePosixPath(path).suffix.lower() not in EXTENSIONS
        ):
            reason = "unsupported_file_type"
        if (
            not reason
            and integration.security_level == 2
            and entry.get("size", 0) > 200000
        ):
            reason = "file_too_large"
        if reason:
            report["excluded"].append({"path": path, "reason": reason})
            continue
        if len(manifest) >= 200:
            raise GitHubError("GITHUB_FILE_LIMIT", retryable=False)
        previous = old.get(path)
        if (
            previous
            and previous.blob_sha == entry["sha"]
            and previous.payload["metadata"].get("repository") == integration.repository
            and previous.payload["metadata"].get("security_level")
            == integration.security_level
            and previous.phase == "ready"
        ):
            payload = previous.payload
        else:
            if integration.security_level == 1:
                text = f"파일 경로: {path}\n기준 커밋: {sha}\n커밋 메시지: {message}"
            else:
                try:
                    original = await github.blob(
                        integration.repository, entry["sha"], token
                    )
                except GitHubError as exc:
                    if exc.code not in {
                        "GITHUB_FILE_TOO_LARGE",
                        "GITHUB_FILE_UNSUPPORTED",
                    }:
                        raise
                    report["excluded"].append({"path": path, "reason": exc.code})
                    continue
                text, findings = sanitize(original, path)
                report["masked"].extend(findings)
                if not text.strip():
                    report["excluded"].append({"path": path, "reason": "empty_file"})
                    continue
            total += len(text.encode())
            if total > 4 * 1024 * 1024:
                raise GitHubError("GITHUB_SNAPSHOT_LIMIT", retryable=False)
            payload = source_payload(
                integration,
                entry,
                sha,
                text,
                "코드" if integration.security_level == 2 else "커밋",
                message,
            )
        manifest.append({"path": path, "blob_sha": entry["sha"], "payload": payload})
        report["indexed_files"].append(path)
    return sha, manifest, report


def retire_source(source, tenant):
    if source.phase in {"retired", "blocking"}:
        return
    source.source_revision += 1
    source.searchable = False
    source.phase = "blocking"
    source.barrier = {
        "tenant_id": tenant,
        "event_id": "github-source-" + uuid4().hex,
        "reason": "GitHub 소스 변경 또는 접근 범위 변경",
        "sources": [
            {
                "source_type": source.payload["source_type"],
                "source_id": source.source_id,
                "version": source.version,
                "source_revision": source.source_revision,
                "status": "폐기",
                "review_required": False,
            }
        ],
    }


async def invalidate_documents(session, integration, changed_paths, bindings, push_id=None):
    affected = {
        b["doc_id"]
        for b in bindings
        if any(includes(path, [b["path"]]) for path in changed_paths)
    }
    for external in sorted(affected):
        document = await session.scalar(
            select(Document)
            .where(
                Document.tenant_id == integration.tenant_id,
                Document.ai_source_id == external,
            )
            .with_for_update()
        )
        if document is None:
            continue
        version = await session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.doc_id == document.doc_id,
                DocumentVersion.version == document.latest_version,
            )
        )
        if version.review_required:
            if push_id:
                from app.services.review_discovery import capture_document
                await capture_document(session, document, version, source="push", cause=f"push:{push_id}", push_id=push_id)
            continue
        version.review_required, version.searchable = True, False
        version.source_revision += 1
        session.add(
            DocumentJob(
                version_id=version.version_id,
                source_revision=version.source_revision,
                phase="barrier",
                barrier=status_event(document, version, "연결된 GitHub 경로 변경"),
                barrier_confirmed=False,
            )
        )
        from app.services.review_discovery import capture_document
        await capture_document(session, document, version, source="push" if push_id else "doc_version",
                               cause=f"push:{push_id}" if push_id else None, push_id=push_id)


async def prepare_snapshot(session, integration, job, sha, manifest, report):
    sources = list(
        (
            await session.scalars(
                select(GitHubSource).where(
                    GitHubSource.integration_id == integration.integration_id
                )
            )
        ).all()
    )
    desired = {
        (m["payload"]["source_id"], m["payload"]["version"]): m for m in manifest
    }
    old = {(s.source_id, s.version): s for s in sources}
    changed = set()
    for source in sources:
        if (source.source_id, source.version) not in desired:
            if source.phase != "retired":
                changed.add(source.path)
            retire_source(source, integration.tenant_id)
    for identity, entry in desired.items():
        source = old.get(identity)
        if source is None:
            source = GitHubSource(
                integration_id=integration.integration_id,
                source_id=identity[0],
                version=identity[1],
                path=entry["path"],
                blob_sha=entry["blob_sha"],
                payload=entry["payload"],
                source_revision=1,
                phase="submit",
            )
            session.add(source)
            changed.add(entry["path"])
        elif source.phase == "ready":
            # 동일 blob도 AI 재시작 후 사라질 수 있으므로 재사용 전 I05를 확인한다.
            source.phase, source.searchable = "poll", False
            source.poll_started_at = utcnow()
        elif source.phase in {"retired", "blocking"}:
            # force-push로 과거 SHA가 돌아와도 같은 revision으로 재활성화하지 않는다.
            source.source_revision += 1
            payload = copy.deepcopy(entry["payload"])
            payload["source_revision"] = source.source_revision
            source.payload, source.phase, source.barrier = payload, "submit", None
            source.searchable = False
            changed.add(source.path)
    if sources:
        from app.db.models import PushEvent
        push_id = await session.scalar(select(PushEvent.event_id).where(PushEvent.job_id == job.job_id))
        await invalidate_documents(
            session, integration, changed, integration.pending_bindings, push_id
        )
    job.manifest, job.target_sha, job.phase = manifest, sha, "sync"
    job.report = report
    await session.commit()


async def block_source(source, tenant, rag):
    if source.submission_possible:
        try:
            await rag.statuses(tenant, source.barrier)
        except RagError as exc:
            if exc.code != "SOURCE_NOT_FOUND":
                raise
            # AI 재시작·유실 시 비검색 상태로 등록한 뒤 같은 이벤트를 다시 적용한다.
            payload = copy.deepcopy(source.payload)
            payload.update(
                status="폐기",
                review_required=False,
                source_revision=source.source_revision,
            )
            await rag.submit_github(tenant, payload)
            await rag.statuses(tenant, source.barrier)
        view = await rag.view_github(tenant, source.payload)
        if (
            view.source_revision != source.source_revision
            or view.status != "폐기"
            or view.searchable
        ):
            raise RagError("AI_BLOCK_NOT_CONFIRMED")
    source.phase, source.searchable = "retired", False


async def sync_one(session, integration, job, rag):
    # 이전 자료의 차단 확인 전에는 새 자료를 I03으로 전달하지 않는다.
    source = await session.scalar(
        select(GitHubSource)
        .where(
            GitHubSource.integration_id == integration.integration_id,
            GitHubSource.phase == "blocking",
        )
        .order_by(GitHubSource.source_pk)
        .limit(1)
    )
    if source:
        await block_source(source, integration.tenant_id, rag)
        return False
    bound_ids = [b["doc_id"] for b in integration.pending_bindings]
    doc_job = await session.scalar(
        select(DocumentJob.job_id)
        .join(DocumentVersion)
        .join(Document)
        .where(
            Document.tenant_id == integration.tenant_id,
            Document.ai_source_id.in_(bound_ids),
            DocumentJob.barrier_confirmed.is_(False),
            DocumentJob.source_revision == DocumentVersion.source_revision,
        )
        .order_by(DocumentJob.job_id)
        .limit(1)
    )
    if doc_job:
        # confirm_barrier가 자체 commit하므로 여기서는 작업을 반환해 외부에서 처리한다.
        return ("document", doc_job)
    source = await session.scalar(
        select(GitHubSource)
        .where(
            GitHubSource.integration_id == integration.integration_id,
            GitHubSource.phase.in_(["submit", "poll"]),
        )
        .order_by(GitHubSource.source_pk)
        .limit(1)
    )
    if source:
        if not source.submission_possible:
            source.submission_possible = True
            await session.commit()
            return False
        if source.phase == "submit":
            await rag.submit_github(integration.tenant_id, source.payload)
            source.phase, source.poll_started_at = "poll", utcnow()
            return False
        try:
            view = await rag.view_github(integration.tenant_id, source.payload)
        except RagError as exc:
            if exc.code != "SOURCE_NOT_FOUND":
                raise
            # 응답 유실·AI 재시작 후에도 저장한 동일 payload만 재접수한다.
            await rag.submit_github(integration.tenant_id, source.payload)
            if (
                source.poll_started_at
                and (utcnow() - source.poll_started_at).total_seconds() > 300
            ):
                raise RagError("GITHUB_INDEXING_TIMEOUT", retryable=False)
            return ("poll", None)
        if (
            view.source_revision != source.source_revision
            or view.status != "유효"
            or view.review_required
        ):
            raise RagError("AI_REVISION_MISMATCH", retryable=False)
        if view.indexing_status == "실패" or view.error:
            raise RagError("GITHUB_INDEXING_FAILED", retryable=False)
        if view.searchable and view.indexing_status == "완료":
            source.phase, source.searchable = "ready", True
            return False
        if (
            source.poll_started_at
            and (utcnow() - source.poll_started_at).total_seconds() > 300
        ):
            raise RagError("GITHUB_INDEXING_TIMEOUT", retryable=False)
        return ("poll", None)
    integration.status = "연결 완료" if integration.enabled else "연동 안 함"
    integration.last_synced_sha, integration.last_synced_at = job.target_sha, utcnow()
    integration.report, integration.error_code = job.report, None
    integration.pending_bindings = []
    job.phase = "done"
    tenant = await lock_knowledge(session, integration.tenant_id)
    tenant.knowledge_revision += 1
    return True
