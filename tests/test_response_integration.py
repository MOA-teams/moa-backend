# ruff: noqa: F401, F811
"""6·7단계의 실제 API·저장 경로·마이그레이션 통합 검증."""

import asyncio
import copy

import pytest
from sqlalchemy import func, select, text, update

from app.core.enums import MessageType
from app.db.models import (
    Answer,
    AnalysisRequest,
    AnalysisRun,
    AutomaticResponse,
    DocumentVersion,
    Feedback,
    Inquiry,
    Message,
)
from app.services import answers
from test_documents import rag
from test_pipeline import ai, callback_headers, CALLBACK, ADMIN
from test_automatic_responses import auto_case, deliver, question
from test_answers import body_from, send, close, feedback

pytestmark = pytest.mark.anyio


async def view(client, agent, case):
    response = await client.get(f"{ADMIN}/{case[0]}", headers=agent["headers"])
    assert response.status_code == 200, response.text
    return response.json()


async def test_auto_answer_to_close_and_feedback(
    client, customer, agent, session_factory, auto_case
):
    applied = await deliver(client, auto_case)
    assert applied.json()["delivery_action"] == "자동 답변 전송"
    detail = await view(client, agent, auto_case)
    aid = detail["automatic_response"]["answer_id"]
    assert aid == "A-1001" and detail["revision"] == 2
    audit = detail["answers"][0]
    assert audit["answer_id"] == aid and audit["delivery_mode"] == "자동"
    assert audit["agent_id"] is None and audit["agent_edit_ratio"] is None
    assert audit["original_draft"] == auto_case[2]["draft"]
    assert audit["original_verification"] == auto_case[2]["verification"]
    assert audit["approval"]["approved_by"] is None
    assert audit["approval"]["approval_type"] == "자동 응대 정책"
    assert (
        audit["approval"]["automatic_response"]["validation"][
            "backend_knowledge_revision"
        ]
        == detail["backend_knowledge_revision"]
    )
    chat = (
        await client.get(
            f"/api/v1/inquiries/{auto_case[0]}", headers=customer["headers"]
        )
    ).json()
    assert chat["messages"][-1]["answer_id"] == aid
    assert "automatic_response" not in chat and "original_verification" not in str(chat)
    inquiry = {"inquiry_id": auto_case[0]}
    closed = await close(client, agent, inquiry, detail["revision"])
    assert closed.status_code == 200, closed.text
    rated = await feedback(client, customer, inquiry, {"rating": 5, "resolved": True})
    assert rated.status_code == 201, rated.text
    assert rated.json()["answer_id"] == aid
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Answer)) == 1
        assert await s.scalar(select(func.count()).select_from(Feedback)) == 1
    duplicate = await deliver(client, auto_case)
    assert (
        duplicate.status_code == 200 and duplicate.json()["apply_status"] == "duplicate"
    )


@pytest.mark.parametrize("winner", ["automatic", "agent"])
async def test_real_manual_and_automatic_delivery_race(
    client, agent, session_factory, auto_case, monkeypatch, winner
):
    inquiry = {"inquiry_id": auto_case[0]}
    payload = body_from(await view(client, agent, auto_case))
    original, entered, release = (
        answers.persist_answer,
        asyncio.Event(),
        asyncio.Event(),
    )

    async def gated(*args, **kwargs):
        automatic = kwargs["user"] is None
        if automatic == (winner == "automatic"):
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(answers, "persist_answer", gated)
    if winner == "automatic":
        first = asyncio.create_task(deliver(client, auto_case))
    else:
        first = asyncio.create_task(send(client, agent, inquiry, payload))
    await asyncio.wait_for(entered.wait(), 5)
    second = asyncio.create_task(
        send(client, agent, inquiry, payload)
        if winner == "automatic"
        else deliver(client, auto_case)
    )
    try:
        await asyncio.sleep(0.05)
        assert not second.done()
    finally:
        release.set()
    first_result, second_result = await asyncio.wait_for(
        asyncio.gather(first, second), 10
    )
    if winner == "automatic":
        assert (
            first_result.status_code == 200
            and first_result.json()["delivery_action"] == "자동 답변 전송"
        )
        assert second_result.status_code == 409
    else:
        assert first_result.status_code == 201
        assert (
            second_result.status_code == 200
            and second_result.json()["apply_status"] == "ignored_stale"
        )
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Answer)) == 1
        assert await s.scalar(select(func.count()).select_from(Message)) == 3
        answer = await s.scalar(select(Answer))
        assert answer.delivery_mode == ("자동" if winner == "automatic" else "담당자")
        assert (answer.agent_id is None) == (winner == "automatic")


async def test_question_then_manual_answer_uses_only_one_answer_record(
    client, agent, customer, session_factory, auto_case
):
    assert (await deliver(client, question(auto_case))).json()[
        "delivery_action"
    ] == "추가 질문 전송"
    detail = await view(client, agent, auto_case)
    assert detail["automatic_response"]["answer_id"] is None and detail["answers"] == []
    inquiry = {"inquiry_id": auto_case[0]}
    result = await send(client, agent, inquiry, body_from(detail))
    assert result.status_code == 201, result.text
    assert result.json()["answer_id"] == "A-1001"
    assert (
        await close(client, agent, inquiry, result.json()["inquiry_revision"])
    ).status_code == 200
    assert (
        await feedback(client, customer, inquiry, {"resolved": False})
    ).status_code == 201
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Answer)) == 1
        assert await s.scalar(select(func.count()).select_from(Message)) == 4


async def test_auto_answer_id_is_in_next_analysis_snapshot(
    client, customer, session_factory, auto_case
):
    assert (await deliver(client, auto_case)).status_code == 200
    added = await client.post(
        f"/api/v1/inquiries/{auto_case[0]}/messages",
        headers={**customer["headers"], "Idempotency-Key": "next"},
        json={"text": "추가 문의가 있어요."},
    )
    assert added.status_code == 201
    async with session_factory() as s:
        inquiry = await s.scalar(select(Inquiry))
        job = await s.get(AnalysisRequest, inquiry.latest_analysis_request_id)
        final = next(m for m in job.snapshot["messages"] if m.get("answer_id"))
        assert final["answer_id"] == "A-1001" and final["sender"] == "ai_auto"


async def test_answer_id_exhaustion_rolls_back_message_answer_and_revision(
    client, agent, session_factory, auto_case
):
    async with session_factory() as s:
        await s.execute(text("SELECT setval('public_answer_seq',9999,true)"))
        await s.commit()
    result = await deliver(client, auto_case)
    assert (
        result.status_code == 200 and result.json()["delivery_action"] == "담당자 검토"
    )
    detail = await view(client, agent, auto_case)
    assert detail["revision"] == 1
    assert detail["automatic_response"]["review_reasons"] == ["ID_SPACE_EXHAUSTED"]
    assert detail["automatic_response"]["answer_id"] is None
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Answer)) == 0
        assert await s.scalar(select(func.count()).select_from(Message)) == 2
        assert (await s.scalar(select(Inquiry))).last_message_sequence == 2
        assert (await s.get(AnalysisRun, auto_case[1])).result_hash


async def test_knowledge_changed_and_restored_still_requires_reanalysis(
    client, agent, session_factory, auto_case
):
    async with session_factory() as s:
        await s.execute(update(DocumentVersion).values(review_required=True))
        await s.execute(update(DocumentVersion).values(review_required=False))
        await s.commit()
    result = await deliver(client, auto_case)
    assert result.status_code == 200
    detail = await view(client, agent, auto_case)
    assert detail["automatic_response"]["review_reasons"] == ["KNOWLEDGE_CHANGED"]
    assert detail["answers"] == []


async def test_delivered_auto_draft_cannot_be_approved_twice(
    client, agent, session_factory, auto_case
):
    assert (await deliver(client, auto_case)).status_code == 200
    detail = await view(client, agent, auto_case)
    assert detail["draft"]
    response = await send(
        client, agent, {"inquiry_id": auto_case[0]}, body_from(detail)
    )
    assert response.status_code == 409
    # 6단계의 명시적인 새 revision·직접 작성 후속 답변 정책은 유지한다.
    followup = body_from(
        detail,
        draft_id=None,
        draft_revision=None,
        answer_text="담당자 추가 안내",
        evidence={"docs": [], "code": []},
    )
    response = await send(
        client, agent, {"inquiry_id": auto_case[0]}, followup, key="followup"
    )
    assert response.status_code == 201
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Answer)) == 2


@pytest.mark.parametrize("revision", ["0010_answers", "0010_automatic_responses"])
async def test_each_released_migration_branch_upgrades_without_data_loss(
    db_engine, migrate, revision
):
    async with db_engine.begin() as c:
        await c.run_sync(migrate, "downgrade", "base")
        await c.run_sync(migrate, "upgrade", revision)
        await c.execute(
            text("INSERT INTO tenants(tenant_id,name) VALUES ('alpha','Alpha')")
        )
        await c.execute(
            text(
                "INSERT INTO users(public_id,email,password,role,tenant_id) VALUES ('U-2001','old@example.com','preserved-hash','customer','alpha'),('agent_01','agent@example.com','agent-hash','agent','alpha')"
            )
        )
        await c.execute(
            text(
                "INSERT INTO inquiries(public_id,tenant_id,customer_id,title,customer_category,env,status,revision,last_message_sequence) VALUES ('Q-1001','alpha',1,'old','이용 방법','{}','답변 완료',2,2)"
            )
        )
        await c.execute(
            text(
                "INSERT INTO messages(public_id,inquiry_id,sequence,sender,message_type,text) VALUES ('MSG-000001',1,1,'customer','고객 메시지','원본 문의'),('MSG-000002',1,2,'agent','최종 답변','원본 답변')"
            )
        )
        tables = ["users", "inquiries", "messages"]
        if revision == "0010_answers":
            await c.execute(
                text(
                    "INSERT INTO answers(public_id,inquiry_id,message_id,based_on_message_id,inquiry_revision,backend_knowledge_revision,agent_id,delivery_mode,answer_text,evidence,approval) VALUES ('A-1001',1,2,1,1,0,2,'담당자','원본 답변','{}','{}')"
                )
            )
            await c.execute(
                text(
                    "INSERT INTO feedback(inquiry_id,answer_id,customer_id,rating,resolved) VALUES (1,1,1,5,true)"
                )
            )
            await c.execute(text("SELECT setval('public_answer_seq',1001,true)"))
            tables += ["answers", "feedback"]
        else:
            await c.execute(
                text(
                    "INSERT INTO analysis_requests(request_id,inquiry_id,inquiry_revision,based_on_message_id,snapshot,review_only,run_status,phase,run_id,attempts,resubmissions) VALUES ('old-request',1,1,1,'{}',false,'완료','done','RUN-old',1,0)"
                )
            )
            await c.execute(
                text(
                    "INSERT INTO analysis_runs(run_id,request_id,superseded) VALUES ('RUN-old','old-request',false)"
                )
            )
            await c.execute(
                text(
                    "INSERT INTO automatic_responses(run_id,inquiry_id,based_on_message_id,action,review_reasons,policy,validation) VALUES ('RUN-old',1,1,'담당자 검토','[]','{}','{}')"
                )
            )
            tables += ["automatic_responses", "analysis_runs"]
        before = {
            table: (await c.execute(text(f"SELECT * FROM {table} ORDER BY 1"))).all()
            for table in tables
        }
        # 6단계 추가 필드가 생기는 inquiries는 공통 필드로 비교한다.
        before["inquiries"] = (
            await c.execute(
                text(
                    "SELECT inquiry_id,public_id,revision,created_at,updated_at FROM inquiries"
                )
            )
        ).all()
        await c.run_sync(migrate, "upgrade", "head")
        for table in tables:
            sql = (
                "SELECT inquiry_id,public_id,revision,created_at,updated_at FROM inquiries"
                if table == "inquiries"
                else f"SELECT * FROM {table} ORDER BY 1"
            )
            assert (await c.execute(text(sql))).all() == before[table]
        assert (
            await c.execute(text("SELECT version_num FROM alembic_version"))
        ).scalars().all() == ["0012_groups_alerts"]
        if revision == "0010_answers":
            assert await c.scalar(text("SELECT nextval('public_answer_seq')")) == 1002
        else:
            assert (
                await c.scalar(
                    text("SELECT backend_knowledge_revision FROM analysis_requests")
                )
                is None
            )
