# ruff: noqa: F811
"""실제 PostgreSQL 트랜잭션으로 자동 응대의 허용·차단·원자성을 검증한다."""

import asyncio
import copy
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from pydantic import ValidationError

from app.core.auto_response import AutoResponsePolicy, REQUIRED_CHECKS, safe_answer
from app.core.config import get_settings
from app.core.enums import InquiryStatus, MessageSender, MessageType
from app.db.models import (
    AutomaticResponse,
    AnalysisRequest,
    AnalysisRun,
    Inquiry,
    Message,
    DocumentVersion,
)
from app.services import automatic_responses as service
from app.workers.analysis import run_once
from test_documents import rag, create, drain  # noqa: F401
from test_pipeline import ai, submit, callback_headers, ADMIN, CALLBACK, due  # noqa: F401

pytestmark = pytest.mark.anyio


def configure(monkeypatch, **changes):
    policy = {
        "questions_enabled": True,
        "answers_enabled": True,
        "categories": ["이용 방법"],
        "public_documents": {"DOC-001": 1},
    }
    policy.update(changes)
    monkeypatch.setenv(
        "AUTO_RESPONSE_POLICIES", json.dumps({"alpha": policy}, ensure_ascii=False)
    )
    get_settings.cache_clear()


@pytest.fixture
async def auto_case(client, customer, agent, session_factory, ai, rag, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(service, "get_rag_client", lambda: rag)
    response = await create(
        client, agent, doc_type="FAQ", text="검색창에 검색어를 입력해 주세요."
    )
    assert response.status_code == 202, response.text
    await drain(session_factory, rag)
    response = await client.post(
        "/api/v1/inquiries",
        headers={**customer["headers"], "Idempotency-Key": "auto-case"},
        json={"message": "검색은 어떻게 하나요?", "customer_category": "이용 방법"},
    )
    assert response.status_code == 201, response.text
    inquiry = response.json()["inquiry_id"]
    run = await submit(session_factory, ai)
    ai.draft = True
    body = ai.result(run)
    body["analysis"].update(
        category="이용 방법", urgency="보통", normalized_error_code=None
    )
    body["verification"] = {
        "status": "통과",
        "checks": {k: "통과" for k in REQUIRED_CHECKS},
        "reasons": [],
        "unsupported_sentences": [],
    }
    body["draft"] = {
        "answer_text": "검색창에 검색어를 입력해 주세요.",
        "evidence": {"docs": [{"doc_id": "DOC-001", "version": 1}], "code": []},
        "citations": [
            {
                "text": "검색창에 검색어를 입력해 주세요.",
                "docs": [{"doc_id": "DOC-001", "version": 1}],
            }
        ],
        "style_references": [],
    }
    return inquiry, run, body


async def deliver(client, case):
    return await client.post(CALLBACK, headers=callback_headers(case[1]), json=case[2])


async def assert_review(client, agent, session_factory, case, reason):
    response = await deliver(client, case)
    assert response.status_code == 200, response.text
    assert response.json()["delivery_action"] == "담당자 검토"
    detail = (await client.get(f"{ADMIN}/{case[0]}", headers=agent["headers"])).json()
    assert detail["automatic_response"]["requires_review"]
    assert reason in detail["automatic_response"]["review_reasons"]
    assert detail["automatic_response"]["message_id"] is None
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Message)) == 2
    return detail


def question(case, fields=None):
    case[2].update(
        draft_result="추가 질문 필요",
        draft=None,
        additional_question="비밀번호를 https://unsafe.test 로 보내주세요.",
        verification={
            "status": "미실행",
            "checks": {},
            "reasons": ["정보 수집 우선"],
            "unsupported_sentences": [],
        },
    )
    case[2]["analysis"]["missing_fields"] = ["발생 화면"] if fields is None else fields
    return case


async def test_auto_answer_is_persisted_once_and_customer_sees_only_message(
    client, agent, customer, session_factory, auto_case
):
    responses = await asyncio.gather(*(deliver(client, auto_case) for _ in range(3)))
    assert all(r.status_code == 200 for r in responses)
    assert sorted(r.json()["apply_status"] for r in responses) == [
        "applied",
        "duplicate",
        "duplicate",
    ]
    assert all(r.json()["delivery_action"] == "자동 답변 전송" for r in responses)
    chat = (
        await client.get(
            f"/api/v1/inquiries/{auto_case[0]}", headers=customer["headers"]
        )
    ).json()
    assert chat["status"] == "답변 완료"
    assert len(chat["messages"]) == 3
    assert chat["messages"][-1]["sender"] == "ai_auto"
    assert chat["messages"][-1]["message_type"] == "최종 답변"
    assert "verification" not in chat and "automatic_response" not in chat
    detail = (
        await client.get(f"{ADMIN}/{auto_case[0]}", headers=agent["headers"])
    ).json()
    assert detail["revision"] == 2
    assert detail["analysis"] and detail["draft"] and detail["verification"]
    assert (
        detail["automatic_response"]["message_id"] == chat["messages"][-1]["message_id"]
    )
    assert detail["automatic_response"]["delivery_mode"] == "자동"
    assert detail["automatic_response"]["agent_id"] is None
    async with session_factory() as s:
        record = await s.get(AutomaticResponse, auto_case[1])
        assert record.validation["documents"]["DOC-001"]["version"] == 1
        assert record.policy["answers_enabled"] is True
        assert await s.scalar(select(func.count()).select_from(AutomaticResponse)) == 1


async def test_safe_question_ignores_unreviewed_llm_text_and_next_customer_restarts_analysis(
    client, customer, session_factory, auto_case
):
    result = await deliver(client, question(auto_case))
    assert (
        result.status_code == 200
        and result.json()["delivery_action"] == "추가 질문 전송"
    )
    chat = (
        await client.get(
            f"/api/v1/inquiries/{auto_case[0]}", headers=customer["headers"]
        )
    ).json()
    assert chat["status"] == "추가 정보 필요"
    message = chat["messages"][-1]
    assert message["message_type"] == "정보 수집 질문"
    assert message["text"] == "어떤 화면에서 문제가 발생했나요?"
    assert "unsafe" not in json.dumps(chat)
    response = await client.post(
        f"/api/v1/inquiries/{auto_case[0]}/messages",
        headers={**customer["headers"], "Idempotency-Key": "reply"},
        json={"text": "검색 화면이에요."},
    )
    assert response.status_code == 201
    async with session_factory() as s:
        inquiry = await s.scalar(select(Inquiry))
        job = await s.get(AnalysisRequest, inquiry.latest_analysis_request_id)
        assert job.request_id != (await s.get(AnalysisRun, auto_case[1])).request_id
        assert job.snapshot["messages"][-2]["text"] == message["text"]
        assert job.snapshot["based_on_message_id"] == response.json()["message_id"]


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("missing_check", "VERIFICATION_NOT_PASSED"),
        ("failed_check", "VERIFICATION_NOT_PASSED"),
        ("unknown_check", "VERIFICATION_NOT_PASSED"),
        ("skipped_check", "VERIFICATION_NOT_PASSED"),
        ("unsupported", "VERIFICATION_NOT_PASSED"),
        ("missing_fields", "MISSING_INFORMATION"),
        ("high", "OUTSIDE_AUTO_RESPONSE_SCOPE"),
        ("error", "OUTSIDE_AUTO_RESPONSE_SCOPE"),
        ("category", "OUTSIDE_AUTO_RESPONSE_SCOPE"),
        ("code", "EVIDENCE_REVIEW_REQUIRED"),
        ("style", "EVIDENCE_REVIEW_REQUIRED"),
        ("empty_evidence", "EVIDENCE_INCOMPLETE"),
        ("empty_citations", "EVIDENCE_INCOMPLETE"),
        ("bad_citation", "CITATION_UNSUPPORTED"),
        ("internal_text", "UNSAFE_CUSTOMER_CONTENT"),
        ("missing_knowledge", "KNOWLEDGE_BASELINE_MISSING"),
    ],
)
async def test_answer_guardrails(
    client, agent, session_factory, auto_case, kind, reason
):
    b = auto_case[2]
    if kind == "missing_check":
        b["verification"]["checks"].pop("source_conflict")
    if kind in ("failed_check", "unknown_check", "skipped_check"):
        b["verification"]["checks"]["source_conflict"] = {
            "failed_check": "실패",
            "unknown_check": "확인 불가",
            "skipped_check": "미실행",
        }[kind]
    if kind == "unsupported":
        b["verification"]["unsupported_sentences"] = ["불명확"]
    if kind == "missing_fields":
        b["analysis"]["missing_fields"] = ["발생 화면"]
    if kind == "high":
        b["analysis"]["urgency"] = "높음"
    if kind == "error":
        b["analysis"]["normalized_error_code"] = "E-502"
    if kind == "category":
        b["analysis"]["category"] = "기능 오류"
    if kind == "code":
        b["draft"]["evidence"]["code"] = [
            {"path": "server/private.py", "commit_sha": "abcdef1"}
        ]
    if kind == "style":
        b["draft"]["style_references"] = [{"best_id": "BA-001", "version": 1}]
    if kind == "empty_evidence":
        b["draft"]["evidence"]["docs"] = []
    if kind == "empty_citations":
        b["draft"]["citations"] = []
    if kind == "bad_citation":
        b["draft"]["citations"][0]["text"] = "다른 본문"
    if kind == "internal_text":
        b["draft"]["answer_text"] += " server/private.py"
    if kind == "missing_knowledge":
        b["knowledge_revision"] = None
    await assert_review(client, agent, session_factory, auto_case, reason)


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("disabled", "AUTO_RESPONSE_DISABLED"),
        ("answer_disabled", "AUTO_ANSWER_DISABLED"),
        ("not_public", "DOCUMENT_NOT_ALLOWLISTED"),
        ("different_version", "DOCUMENT_NOT_ALLOWLISTED"),
        ("other_tenant", "AUTO_RESPONSE_DISABLED"),
    ],
)
async def test_policy_fail_closed(
    client, agent, session_factory, auto_case, monkeypatch, mode, reason
):
    if mode in ("disabled", "other_tenant"):
        monkeypatch.setenv(
            "AUTO_RESPONSE_POLICIES",
            "{}" if mode == "disabled" else '{"beta":{"answers_enabled":true}}',
        )
        get_settings.cache_clear()
    elif mode == "answer_disabled":
        configure(monkeypatch, answers_enabled=False)
    elif mode == "not_public":
        configure(monkeypatch, public_documents={})
    else:
        configure(monkeypatch, public_documents={"DOC-001": 2})
    await assert_review(client, agent, session_factory, auto_case, reason)


@pytest.mark.parametrize(
    "fields", [["비밀번호"], ["발생 화면", "인증번호"], [], ["계좌번호"]]
)
async def test_question_unknown_or_sensitive_fields_require_review(
    client, agent, session_factory, auto_case, fields
):
    await assert_review(
        client,
        agent,
        session_factory,
        question(auto_case, fields),
        "QUESTION_FIELD_NOT_ALLOWED",
    )


@pytest.mark.parametrize("kind", ["question", "answer"])
async def test_review_only_never_sends(client, agent, session_factory, auto_case, kind):
    if kind == "question":
        question(auto_case)
    async with session_factory() as s:
        await s.execute(update(AnalysisRequest).values(review_only=True))
        await s.commit()
    await assert_review(client, agent, session_factory, auto_case, "REVIEW_ONLY")


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("changed", "KNOWLEDGE_CHANGED"),
        ("old_request", "KNOWLEDGE_BASELINE_MISSING"),
        ("remote_unavailable", "EVIDENCE_SYNC_UNAVAILABLE"),
        ("remote_excluded", "EVIDENCE_NOT_CURRENT"),
    ],
)
async def test_knowledge_checks(
    client, agent, session_factory, auto_case, rag, mode, reason
):
    async with session_factory() as s:
        if mode == "changed":
            await s.execute(update(DocumentVersion).values(source_revision=2))
        if mode == "old_request":
            await s.execute(update(AnalysisRequest).values(knowledge_snapshot=None))
        await s.commit()
    if mode == "remote_unavailable":
        rag.fail_view = True
    if mode == "remote_excluded":
        rag.searchable = False
    await assert_review(client, agent, session_factory, auto_case, reason)


async def test_id_exhaustion_keeps_result_and_review_without_partial_message(
    client, agent, session_factory, auto_case
):
    async with session_factory() as s:
        await s.execute(text("SELECT setval('public_message_seq',999999,true)"))
        await s.commit()
    detail = await assert_review(
        client, agent, session_factory, question(auto_case), "ID_SPACE_EXHAUSTED"
    )
    assert detail["revision"] == 1
    assert detail["run_status"] == "완료"
    repeated = await deliver(client, auto_case)
    assert repeated.json()["apply_status"] == "duplicate"


async def test_callback_and_poll_recovery_send_only_once(
    client, customer, session_factory, auto_case, ai
):
    body = copy.deepcopy(auto_case[2])
    ai.result = lambda run: copy.deepcopy(body)
    await due(session_factory)
    results = await asyncio.gather(
        deliver(client, auto_case), run_once(session_factory, ai.client)
    )
    assert results[0].status_code == 200
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(AutomaticResponse)) == 1
        assert await s.scalar(select(func.count()).select_from(Message)) == 3


async def test_new_message_race_serializes_against_auto_response(
    client, customer, session_factory, auto_case
):
    responses = await asyncio.gather(
        deliver(client, auto_case),
        client.post(
            f"/api/v1/inquiries/{auto_case[0]}/messages",
            headers={**customer["headers"], "Idempotency-Key": "racing"},
            json={"text": "추가 정보"},
        ),
    )
    assert responses[0].status_code == 200 and responses[1].status_code == 201
    async with session_factory() as s:
        messages = list(
            (await s.scalars(select(Message).order_by(Message.sequence))).all()
        )
        finals = [m for m in messages if m.message_type == MessageType.FINAL_ANSWER]
        assert len(finals) <= 1
        if finals:
            assert finals[0].sequence < messages[-1].sequence
        assert messages[-1].sender == MessageSender.CUSTOMER


async def test_existing_reply_blocks_new_auto_delivery(
    client, agent, session_factory, auto_case
):
    async with session_factory() as s:
        inquiry = await s.scalar(select(Inquiry).with_for_update())
        inquiry.last_message_sequence += 1
        inquiry.status = InquiryStatus.ANSWERED
        s.add(
            Message(
                inquiry_id=inquiry.inquiry_id,
                sequence=inquiry.last_message_sequence,
                sender=MessageSender.AGENT,
                message_type=MessageType.FINAL_ANSWER,
                text="담당자 답변",
            )
        )
        await s.commit()
    response = await deliver(client, auto_case)
    assert response.status_code == 200
    detail = (
        await client.get(f"{ADMIN}/{auto_case[0]}", headers=agent["headers"])
    ).json()
    assert detail["automatic_response"]["review_reasons"] == [
        "CUSTOMER_MESSAGE_ALREADY_RESPONDED"
    ]
    assert detail["status"] == "답변 완료"
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Message)) == 3


async def test_transaction_failure_rolls_back_message_and_result(
    client, session_factory, auto_case, monkeypatch
):
    original = service.persist_customer_message

    async def broken(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("injected-after-write")

    monkeypatch.setattr(service, "persist_customer_message", broken)
    with pytest.raises(RuntimeError):
        await deliver(client, auto_case)
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Message)) == 2
        assert await s.scalar(select(func.count()).select_from(AutomaticResponse)) == 0
        assert (await s.get(AnalysisRun, auto_case[1])).result_hash is None
    monkeypatch.setattr(service, "persist_customer_message", original)
    assert (await deliver(client, auto_case)).json()[
        "delivery_action"
    ] == "자동 답변 전송"


async def test_migration_preserves_messages_on_downgrade(
    db_engine, migrate, client, session_factory, auto_case
):
    assert (await deliver(client, question(auto_case))).status_code == 200
    async with db_engine.begin() as c:
        before = (
            await c.execute(text("SELECT * FROM messages ORDER BY message_id"))
        ).all()
        await c.run_sync(migrate, "downgrade", "0009_analysis")
        assert (
            await c.execute(text("SELECT * FROM messages ORDER BY message_id"))
        ).all() == before
        await c.run_sync(migrate, "upgrade", "head")
        assert await c.scalar(text("SELECT count(*) FROM automatic_responses")) == 0
        assert (
            await c.execute(text("SELECT * FROM messages ORDER BY message_id"))
        ).all() == before


@pytest.mark.parametrize(
    "value",
    [
        "server/private.py",
        "FAKE_KEY_SECRET",
        "abcdef12345",
        "decodeThumbnail()",
        "https://internal.example",
        "비밀번호 보내세요",
        "foo@example.com",
        "```code```",
    ],
)
def test_customer_text_guard(value):
    assert not safe_answer(value)


@pytest.mark.parametrize(
    "policy",
    [
        {"max_questions": 0},
        {"max_questions": 4},
        {"public_documents": {"DOC-1": 1}},
        {"categories": ["usage"]},
        {"public_documents": {"DOC-001": True}},
        {"unknown": True},
    ],
)
def test_invalid_policy_configuration(policy):
    with pytest.raises(ValidationError):
        AutoResponsePolicy.model_validate(policy)


async def next_question(client, customer, session_factory, ai, case, field):
    response = await client.post(
        f"/api/v1/inquiries/{case[0]}/messages",
        headers={**customer["headers"], "Idempotency-Key": str(uuid4())},
        json={"text": "추가로 알려드립니다."},
    )
    assert response.status_code == 201
    run = await submit(session_factory, ai)
    b = copy.deepcopy(case[2])
    b.update(
        {
            k: ai.runs[run][k]
            for k in (
                "tenant_id",
                "inquiry_id",
                "inquiry_revision",
                "based_on_message_id",
            )
        }
    )
    b["run_id"] = run
    new_case = (case[0], run, b)
    return question(new_case, [field])


@pytest.mark.parametrize(
    "mode,reason",
    [("repeat", "REPEATED_QUESTION"), ("limit", "QUESTION_LIMIT_REACHED")],
)
async def test_question_loop_is_bounded(
    client, agent, customer, session_factory, auto_case, ai, mode, reason
):
    assert (await deliver(client, question(auto_case))).json()[
        "delivery_action"
    ] == "추가 질문 전송"
    new_case = await next_question(
        client,
        customer,
        session_factory,
        ai,
        auto_case,
        "발생 화면" if mode == "repeat" else "앱 버전",
    )
    if mode == "limit":
        assert (await deliver(client, new_case)).json()[
            "delivery_action"
        ] == "추가 질문 전송"
        new_case = await next_question(
            client, customer, session_factory, ai, new_case, "오류 코드"
        )
    response = await deliver(client, new_case)
    assert (
        response.status_code == 200
        and response.json()["delivery_action"] == "담당자 검토"
    )
    detail = (
        await client.get(f"{ADMIN}/{auto_case[0]}", headers=agent["headers"])
    ).json()
    assert detail["automatic_response"]["review_reasons"] == [reason]
    async with session_factory() as s:
        assert await s.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.message_type == MessageType.INFORMATION_REQUEST)
        ) == (1 if mode == "repeat" else 2)


async def test_same_customer_message_reanalysis_does_not_send_again(
    client, agent, session_factory, auto_case, ai
):
    assert (await deliver(client, auto_case)).json()[
        "delivery_action"
    ] == "자동 답변 전송"
    detail = (
        await client.get(f"{ADMIN}/{auto_case[0]}", headers=agent["headers"])
    ).json()
    response = await client.post(
        f"{ADMIN}/{auto_case[0]}/analysis",
        headers={**agent["headers"], "Idempotency-Key": "again"},
        json={
            "expected_inquiry_revision": detail["revision"],
            "based_on_message_id": detail["latest_customer_message_id"],
            "review_only": False,
        },
    )
    assert response.status_code == 202
    run = await submit(session_factory, ai)
    b = copy.deepcopy(auto_case[2])
    b.update(run_id=run, inquiry_revision=detail["revision"])
    assert (await deliver(client, (auto_case[0], run, b))).json()[
        "delivery_action"
    ] == "담당자 검토"
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Message)) == 3


async def test_foreign_tenant_and_owner_cannot_read_delivery_audit(
    client, foreign_agent, other_customer, auto_case
):
    assert (await deliver(client, auto_case)).status_code == 200
    assert (
        await client.get(f"{ADMIN}/{auto_case[0]}", headers=foreign_agent["headers"])
    ).status_code == 404
    assert (
        await client.get(
            f"/api/v1/inquiries/{auto_case[0]}", headers=other_customer["headers"]
        )
    ).status_code == 404
    invalid = await client.post(
        CALLBACK,
        headers=callback_headers(auto_case[1], tenant="beta"),
        json=auto_case[2],
    )
    assert invalid.status_code == 403


async def test_document_update_waits_for_atomic_delivery(
    client, session_factory, auto_case, rag, monkeypatch
):
    from app.db.models import Document
    from app.core.enums import DocStatus

    entered, release, mutated = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = rag.view

    async def paused_view(*args):
        view = await original(*args)
        entered.set()
        await release.wait()
        return view

    monkeypatch.setattr(rag, "view", paused_view)

    async def retire():
        async with session_factory() as s:
            await s.scalar(select(Document).with_for_update())
            version = await s.scalar(select(DocumentVersion))
            version.status = DocStatus.RETIRED
            version.searchable = False
            await s.commit()
            mutated.set()

    sending = asyncio.create_task(deliver(client, auto_case))
    await asyncio.wait_for(entered.wait(), timeout=5)
    editing = asyncio.create_task(retire())
    try:
        await asyncio.sleep(0.05)
        assert not mutated.is_set()
    finally:
        release.set()
    response, _ = await asyncio.wait_for(asyncio.gather(sending, editing), timeout=10)
    assert (
        response.status_code == 200
        and response.json()["delivery_action"] == "자동 답변 전송"
    )
    assert mutated.is_set()


async def test_disabled_questions(
    client, agent, session_factory, auto_case, monkeypatch
):
    configure(monkeypatch, questions_enabled=False)
    await assert_review(
        client, agent, session_factory, question(auto_case), "AUTO_QUESTION_DISABLED"
    )


async def test_policy_disabled_after_submission_is_rechecked(
    client, agent, session_factory, auto_case, monkeypatch
):
    # 고정 AI 입력을 바꾸지 않아도, 실제 발송에는 현재 서버 정책을 적용한다.
    configure(monkeypatch, categories=[])
    await assert_review(
        client, agent, session_factory, auto_case, "OUTSIDE_AUTO_RESPONSE_SCOPE"
    )


async def test_new_uncited_document_invalidates_knowledge_snapshot(
    client, agent, session_factory, auto_case, rag
):
    added = await create(
        client, agent, key="new-policy", text="서로 충돌할 수 있는 새 정책"
    )
    assert added.status_code == 202
    await drain(session_factory, rag)
    await assert_review(client, agent, session_factory, auto_case, "KNOWLEDGE_CHANGED")


async def test_two_inquiries_share_document_locks_without_deadlock(
    client, customer, session_factory, auto_case, ai, rag, monkeypatch
):
    async with session_factory() as s:
        await s.execute(
            update(AnalysisRequest).values(
                available_at=func.now() + text("interval '1 hour'")
            )
        )
        await s.commit()
    second = await client.post(
        "/api/v1/inquiries",
        headers={**customer["headers"], "Idempotency-Key": "parallel-inquiry"},
        json={"message": "검색 기능을 알려주세요.", "customer_category": "이용 방법"},
    )
    assert second.status_code == 201
    assert await run_once(session_factory, ai.client)
    run = next(reversed(ai.runs))
    body = copy.deepcopy(auto_case[2])
    body.update(
        run_id=run,
        **{
            k: ai.runs[run][k]
            for k in (
                "tenant_id",
                "inquiry_id",
                "inquiry_revision",
                "based_on_message_id",
            )
        },
    )
    second_case = (second.json()["inquiry_id"], run, body)
    original, arrived, count = rag.view, asyncio.Event(), 0

    async def simultaneous(*args):
        nonlocal count
        result = await original(*args)
        count += 1
        if count == 2:
            arrived.set()
        await asyncio.wait_for(arrived.wait(), 5)
        return result

    monkeypatch.setattr(rag, "view", simultaneous)
    results = await asyncio.wait_for(
        asyncio.gather(deliver(client, auto_case), deliver(client, second_case)), 10
    )
    assert all(
        r.status_code == 200 and r.json()["delivery_action"] == "자동 답변 전송"
        for r in results
    )
    async with session_factory() as s:
        assert (
            await s.scalar(
                select(func.count())
                .select_from(AutomaticResponse)
                .where(AutomaticResponse.message_id.is_not(None))
            )
            == 2
        )
