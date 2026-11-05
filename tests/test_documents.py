import asyncio
import copy
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.core.rag import Accepted, RagError, SourceView, StatusResult, get_rag_client
from app.db.models import Document, DocumentCounter, DocumentJob, DocumentVersion
from app.main import app
from app.services.document_indexing import utcnow
from app.workers.documents import claim_job, process_claim, run_once

pytestmark = pytest.mark.anyio
BASE = "/api/v1/admin/documents"
PAYLOAD = {"version": 1, "title": "환불 정책", "doc_type": "정책", "status": "유효", "created_at": "2026-11-01T12:00:00+09:00", "text": "환불 신청은 결제일로부터 7일 이내 가능합니다."}


class FakeRag:
    """AI의 revision/event 멱등 계약을 구현한 네트워크 실패 주입용 대역."""
    def __init__(self):
        self.sources, self.events, self.calls = {}, {}, []
        self.fail_status = self.fail_view = self.lose_status = self.lose_submit = False
        self.searchable = True

    async def submit(self, tenant, payload):
        self.calls.append(("submit", copy.deepcopy(payload)))
        key = (tenant, payload["source_id"], payload["version"])
        old = self.sources.get(key)
        if old and old["source_revision"] > payload["source_revision"]:
            raise RagError("STALE_SOURCE_REVISION", retryable=False, status=409)
        if old and old["source_revision"] == payload["source_revision"] and old != payload:
            raise RagError("SOURCE_CONFLICT", retryable=False, status=409)
        self.sources[key] = copy.deepcopy(payload)
        if self.lose_submit:
            self.lose_submit = False
            raise RagError()
        return Accepted(source_id=payload["source_id"], version=payload["version"], indexing_status="대기")

    async def statuses(self, tenant, payload):
        self.calls.append(("status", copy.deepcopy(payload)))
        if self.fail_status:
            raise RagError()
        if payload["event_id"] in self.events:
            assert self.events[payload["event_id"]] == payload
        else:
            for change in payload["sources"]:
                key = (tenant, change["source_id"], change["version"])
                if key not in self.sources:
                    raise RagError("SOURCE_NOT_FOUND", retryable=False, status=404)
                old = self.sources[key]
                if change["source_revision"] < old["source_revision"]:
                    raise RagError("STALE_SOURCE_REVISION", retryable=False, status=409)
                old.update({k: change[k] for k in ("status", "review_required", "source_revision")})
            self.events[payload["event_id"]] = copy.deepcopy(payload)
        if self.lose_status:
            self.lose_status = False
            raise RagError()
        return StatusResult(knowledge_revision=1, updated_count=len(payload["sources"]),
                            search_excluded=all(c["status"] == "폐기" or c["review_required"] for c in payload["sources"]))

    async def view(self, tenant, source_id, version):
        if self.fail_view:
            raise RagError()
        source = self.sources.get((tenant, source_id, version))
        if source is None:
            raise RagError("SOURCE_NOT_FOUND", retryable=False, status=404)
        latest = max(k[2] for k in self.sources if k[:2] == (tenant, source_id))
        return SourceView(source_id=source_id, version=version, source_revision=source["source_revision"],
                          status=source["status"], review_required=source["review_required"], indexing_status="완료",
                          searchable=self.searchable and version == latest and source["status"] == "유효" and not source["review_required"])


@pytest.fixture
def rag():
    fake = FakeRag()
    app.dependency_overrides[get_rag_client] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_rag_client, None)


async def create(client, actor, *, key="doc", **changes):
    return await client.post(BASE, headers={**actor["headers"], "Idempotency-Key": key}, json={**PAYLOAD, **changes})


async def step(factory, rag):
    async with factory() as session:
        await session.execute(update(DocumentJob).values(available_at=utcnow()))
        await session.commit()
    return await run_once(factory, rag)


async def drain(factory, rag):
    for _ in range(20):
        if not await step(factory, rag):
            return
    raise AssertionError("작업이 종료되지 않았습니다.")


async def test_registration_preserves_original_and_search_state(client, agent, session_factory, rag):
    result = await create(client, agent)
    assert result.status_code == 202, result.text
    assert result.json()["doc_id"] == "DOC-001"
    await drain(session_factory, rag)
    item = (await client.get(BASE, headers=agent["headers"])).json()["items"][0]
    assert item["searchable"] and item["indexing_status"] == "완료"
    assert item["created_at"] != item["uploaded_at"]
    async with session_factory() as session:
        version = await session.scalar(select(DocumentVersion))
        assert version.original_text == version.extracted_text == PAYLOAD["text"]
        assert len(version.content_hash) == 64
        assert (await session.scalar(select(Document))).ai_source_id == "DOC-001"
    sent = rag.calls[0][1]
    assert sent["tenant_id"] == "alpha" and sent["source_type"] == "문서" and sent["status"] == "유효"
    assert sent["metadata"]["doc_type"] == "정책"


async def test_concurrent_idempotency_and_conflicting_payload(client, agent, session_factory, rag):
    results = await asyncio.gather(*(create(client, agent) for _ in range(5)))
    assert all(r.status_code == 202 for r in results)
    assert all(r.json() == results[0].json() for r in results)
    assert (await create(client, agent, title="변경")).status_code == 409
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Document)) == 1
        assert await session.scalar(select(func.count()).select_from(DocumentJob)) == 1


async def test_concurrent_ai_ids_and_tenant_limit(client, agent, session_factory, rag):
    results = await asyncio.gather(*(create(client, agent, key=f"doc-{i}") for i in range(6)))
    assert all(r.status_code == 202 for r in results)
    async with session_factory() as session:
        assert len(set((await session.scalars(select(Document.ai_source_id))).all())) == 6
        await session.execute(update(DocumentCounter).values(last_number=998))
        await session.commit()
    results = await asyncio.gather(*(create(client, agent, key=f"last-{i}") for i in range(2)))
    assert sorted(r.status_code for r in results) == [202, 503]
    assert next(r for r in results if r.status_code == 503).json()["error"]["code"] == "ID_SPACE_EXHAUSTED"


async def test_version_blocks_old_before_success_and_no_fallback(client, agent, session_factory, rag):
    first = (await create(client, agent)).json()
    await drain(session_factory, rag)
    second = await create(client, agent, key="v2", doc_id=first["doc_id"], version=2, text="새 환불 정책")
    assert second.status_code == 202, second.text
    assert not (await rag.view("alpha", "DOC-001", 1)).searchable
    assert (await create(client, agent, key="duplicate", doc_id=first["doc_id"], version=2)).status_code == 409
    await drain(session_factory, rag)
    items = (await client.get(BASE, headers=agent["headers"])).json()["items"]
    assert len(items) == 1 and items[0]["version"] == 2 and items[0]["searchable"]
    retired = await client.get(BASE, headers=agent["headers"], params={"status": "폐기"})
    assert retired.json()["items"] == []
    old = await client.patch(f"{BASE}/{first['doc_id']}/versions/1/status", headers=agent["headers"], json={"status": "유효", "review_required": False, "reason": "복구"})
    assert old.status_code == 409


@pytest.mark.parametrize("failure", ["fail_status", "lose_status"])
async def test_barrier_failure_keeps_intent_and_reuses_event(client, agent, session_factory, rag, failure):
    doc_id = (await create(client, agent)).json()["doc_id"]
    await drain(session_factory, rag)
    setattr(rag, failure, True)
    response = await create(client, agent, key="v2", doc_id=doc_id, version=2)
    assert response.status_code == 503
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(DocumentVersion)) == 2
    # 같은 상태의 K03도 미확정 차단을 우회해 성공하면 안 된다.
    rag.fail_status = True
    old = await client.patch(f"{BASE}/{doc_id}/versions/1/status", headers=agent["headers"], json={"status": "폐기", "review_required": False, "reason": "재시도"})
    assert old.status_code == 503
    rag.fail_status = False
    response = await create(client, agent, key="v2", doc_id=doc_id, version=2)
    assert response.status_code == 202
    events = [p for kind, p in rag.calls if kind == "status"]
    assert all(p == events[0] for p in events)
    await drain(session_factory, rag)


async def test_uncertain_submit_and_missing_source_are_safely_fenced(client, agent, session_factory, rag):
    doc_id = (await create(client, agent)).json()["doc_id"]
    await step(session_factory, rag)  # 텍스트/hash와 제출 가능성을 먼저 commit. I03은 아직 없음.
    response = await create(client, agent, key="v2", doc_id=doc_id, version=2)
    assert response.status_code == 202, response.text
    assert not (await rag.view("alpha", "DOC-001", 1)).searchable
    first_submit = next(p for kind, p in rag.calls if kind == "submit")
    assert first_submit["status"] == "폐기" and first_submit["source_revision"] == 2
    await drain(session_factory, rag)
    assert all(p["status"] == "폐기" for kind, p in rag.calls if kind == "submit" and p["version"] == 1)


async def test_status_review_and_reactivation_force_new_revision(client, agent, session_factory, rag):
    doc_id = (await create(client, agent)).json()["doc_id"]
    await drain(session_factory, rag)
    path = f"{BASE}/{doc_id}/versions/1/status"
    response = await client.patch(path, headers=agent["headers"], json={"status": "유효", "review_required": True, "reason": "검토"})
    assert response.status_code == 200 and not response.json()["searchable"]
    assert not (await rag.view("alpha", "DOC-001", 1)).searchable
    response = await client.patch(path, headers=agent["headers"], json={"status": "유효", "review_required": False, "reason": "검토 완료"})
    assert response.status_code == 200 and not response.json()["searchable"]
    await drain(session_factory, rag)
    assert (await rag.view("alpha", "DOC-001", 1)).source_revision == 4
    assert (await client.get(BASE, headers=agent["headers"])).json()["items"][0]["searchable"]


async def test_list_sync_failure_and_embedding_mismatch_never_claim_searchable(client, agent, session_factory, rag):
    await create(client, agent)
    await drain(session_factory, rag)
    rag.fail_view = True
    item = (await client.get(BASE, headers=agent["headers"])).json()["items"][0]
    assert item["indexing_status"] == "완료" and not item["searchable"] and item["sync_error"]
    rag.fail_view, rag.searchable = False, False
    item = (await client.get(BASE, headers=agent["headers"])).json()["items"][0]
    assert item["indexing_status"] == "완료" and not item["searchable"]
    await step(session_factory, rag)
    rag.searchable = True
    await drain(session_factory, rag)
    assert len([p for kind, p in rag.calls if kind == "submit"]) == 2


async def test_roles_tenant_and_cursor_isolation(client, agent, customer, foreign_agent, make_actor, rag):
    response = await create(client, agent)
    doc_id = response.json()["doc_id"]
    assert (await create(client, customer)).status_code == 403
    unassigned = await make_actor("unassigned", "admin", None)
    assert (await create(client, unassigned)).status_code == 403
    assert (await create(client, foreign_agent, doc_id=doc_id, version=2)).status_code == 404
    await create(client, agent, key="second")
    page = (await client.get(BASE, headers=agent["headers"], params={"limit": 1})).json()
    assert page["has_more"] and page["next_cursor"]
    assert (await client.get(BASE, headers=foreign_agent["headers"], params={"cursor": page["next_cursor"]})).status_code == 400
    assert (await client.get(BASE, headers=agent["headers"], params={"cursor": page["next_cursor"], "status": "유효"})).status_code == 400
    assert (await client.get(BASE, headers=agent["headers"], params={"cursor": page["next_cursor"]})).json()["items"][0]["doc_id"] == doc_id


@pytest.mark.parametrize("changes", [
    {"created_at": "2026-11-01T12:00:00"}, {"version": 2}, {"version": True},
    {"text": None}, {"attachment_id": 1}, {"title": " "}, {"text": "\n "},
    {"doc_type": "policy"}, {"tenant_id": "beta"}, {"status": "review_required"},
])
async def test_document_validation(client, agent, rag, changes):
    assert (await create(client, agent, **changes)).status_code == 400


async def test_attachment_purpose_ownership_and_reuse(client, agent, customer, foreign_agent, make_actor, fake_storage, rag):
    file = {"file": ("policy.pdf", b"%PDF-test", "application/pdf")}
    assert (await client.post("/api/v1/attachments", headers=customer["headers"], data={"purpose": "document"}, files=file)).status_code == 403
    assert (await client.post("/api/v1/attachments", headers=agent["headers"], files=file)).status_code == 403
    upload = await client.post("/api/v1/attachments", headers=agent["headers"], data={"purpose": "document"}, files=file)
    assert upload.status_code == 201
    attachment_id = upload.json()["attachment_id"]
    other = await make_actor("other-agent", "admin")
    assert (await client.get(f"/api/v1/attachments/{attachment_id}", headers=other["headers"])).status_code == 404
    assert (await create(client, other, text=None, attachment_id=attachment_id)).status_code == 404
    response = await create(client, agent, text=None, attachment_id=attachment_id)
    assert response.status_code == 202
    assert (await client.get(f"/api/v1/attachments/{attachment_id}", headers=other["headers"])).status_code == 200
    assert (await client.get(f"/api/v1/attachments/{attachment_id}", headers=foreign_agent["headers"])).status_code == 404
    assert (await create(client, agent, key="reuse", text=None, attachment_id=attachment_id)).status_code == 409
    inquiry_upload = await client.post("/api/v1/attachments", headers=customer["headers"], files=file)
    assert (await create(client, agent, key="wrongpurpose", text=None, attachment_id=inquiry_upload.json()["attachment_id"])).status_code == 404


async def test_worker_lease_recovery_and_late_revision(client, agent, session_factory, rag):
    doc_id = (await create(client, agent)).json()["doc_id"]
    first = await claim_job(session_factory)
    assert first and await claim_job(session_factory) is None
    async with session_factory() as session:
        await session.execute(update(DocumentJob).values(lease_until=utcnow() - timedelta(seconds=1)))
        await session.commit()
    second = await claim_job(session_factory)
    assert first[0] == second[0] and first[1] != second[1]
    await process_claim(session_factory, *first, rag)
    assert rag.calls == []
    # 이전 OCR/작업이 늦게 끝나도 새 버전 등록 후에는 AI에 전송하지 않는다.
    assert (await create(client, agent, key="v2", doc_id=doc_id, version=2)).status_code == 202
    await process_claim(session_factory, *second, rag)
    await drain(session_factory, rag)
    assert all(p["version"] == 2 for kind, p in rag.calls if kind == "submit")


async def test_submit_response_loss_uses_same_immutable_payload(client, agent, session_factory, rag):
    await create(client, agent)
    rag.lose_submit = True
    await drain(session_factory, rag)
    submissions = [p for kind, p in rag.calls if kind == "submit"]
    assert len(submissions) == 2 and submissions[0] == submissions[1]


async def test_text_limit_fails_without_silent_truncation(client, agent, session_factory, rag):
    await create(client, agent, text="가" * 54551)
    await drain(session_factory, rag)
    item = (await client.get(BASE, headers=agent["headers"])).json()["items"][0]
    assert item["indexing_status"] == "실패" and item["indexing_error"] == "DOCUMENT_CHUNK_LIMIT"
    assert not rag.calls


async def test_worker_recovers_barrier_without_http_retry(client, agent, session_factory, rag):
    doc_id = (await create(client, agent)).json()["doc_id"]
    await drain(session_factory, rag)
    rag.fail_status = True
    assert (await create(client, agent, key="v2", doc_id=doc_id, version=2)).status_code == 503
    rag.fail_status = False
    await drain(session_factory, rag)
    assert not (await rag.view("alpha", "DOC-001", 1)).searchable
    assert (await rag.view("alpha", "DOC-001", 2)).searchable
    assert (await create(client, agent, key="v2", doc_id=doc_id, version=2)).status_code == 202


async def test_concurrent_version_registration_has_one_winner(client, agent, session_factory, rag):
    doc_id = (await create(client, agent)).json()["doc_id"]
    results = await asyncio.gather(*(create(client, agent, key=f"v2-{i}", doc_id=doc_id, version=2) for i in range(4)))
    assert sorted(r.status_code for r in results) == [202, 409, 409, 409]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(DocumentVersion)) == 2


async def test_ocr_runs_outside_lock_and_late_result_is_discarded(client, agent, session_factory, fake_storage, rag, monkeypatch):
    from app.services.document_extraction import validate_text
    from app.workers import documents as worker
    entered, finish = asyncio.Event(), asyncio.Event()
    async def extraction(data, mime):
        entered.set()
        await finish.wait()
        return validate_text("늦게 끝난 OCR 결과")
    monkeypatch.setattr(worker, "extract_file", extraction)
    fake_storage.read = lambda key: fake_storage.objects[key]
    upload = await client.post("/api/v1/attachments", headers=agent["headers"], data={"purpose": "document"}, files={"file": ("scan.pdf", b"%PDF-test", "application/pdf")})
    doc_id = (await create(client, agent, text=None, attachment_id=upload.json()["attachment_id"])).json()["doc_id"]
    task = asyncio.create_task(run_once(session_factory, rag, fake_storage))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        response = await asyncio.wait_for(create(client, agent, key="v2", doc_id=doc_id, version=2), timeout=5)
        assert response.status_code == 202
    finally:
        finish.set()
        await task
    await drain(session_factory, rag)
    assert all(p["version"] == 2 for kind, p in rag.calls if kind == "submit")
    async with session_factory() as session:
        old = await session.scalar(select(DocumentVersion).where(DocumentVersion.version == 1))
        assert old.extracted_text is None


async def test_retry_exhaustion_is_visible_and_manual_retry_recovers(client, agent, session_factory, rag):
    from app.workers.documents import retry_job
    await create(client, agent)
    rag.fail_view = True
    await drain(session_factory, rag)
    async with session_factory() as session:
        job = await session.scalar(select(DocumentJob))
        assert job.phase == "failed" and job.attempts == 3
        job_id = job.job_id
    rag.fail_view = False
    await retry_job(session_factory, job_id)
    await drain(session_factory, rag)
    assert (await client.get(BASE, headers=agent["headers"])).json()["items"][0]["searchable"]
