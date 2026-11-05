# ruff: noqa: F401, F811
"""Customer progress never leaks model drafts and survives empty cursor pages."""

import pytest
from sqlalchemy import select
from app.core.enums import InquiryStatus
from app.db.models import Inquiry
from test_pipeline import ai, callback_headers, CALLBACK
from test_documents import rag
from test_automatic_responses import auto_case, deliver, question

pytestmark = pytest.mark.anyio


async def chat(client, customer, case, cursor=None):
    response = await client.get(
        f"/api/v1/inquiries/{case[0]}",
        headers=customer["headers"],
        params={"cursor": cursor} if cursor else {},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_pending_and_empty_pages_keep_real_progress(client, customer, auto_case):
    first = await chat(client, customer, auto_case)
    assert first["response_progress"]["state"] == "analyzing"
    second = await chat(client, customer, auto_case, first["next_cursor"])
    assert second["messages"] == []
    assert second["response_progress"] == first["response_progress"]
    assert "verification" not in str(second) and "draft" not in second


async def test_auto_answer_is_identified_and_stops_thinking(
    client, customer, auto_case
):
    assert (await deliver(client, auto_case)).status_code == 200
    page = await chat(client, customer, auto_case)
    assert page["response_progress"]["state"] == "answered"
    assert page["messages"][-1]["sender"] == "ai_auto"
    assert page["messages"][-1]["message_type"] == "최종 답변"


async def test_reconfirmation_question_and_new_reply(client, customer, auto_case):
    assert (
        await deliver(client, question(auto_case, ["문의 내용"]))
    ).status_code == 200
    page = await chat(client, customer, auto_case)
    assert page["response_progress"]["state"] == "awaiting_customer"
    assert page["messages"][-1]["sender"] == "ai_auto"
    assert page["messages"][-1]["message_type"] == "정보 수집 질문"
    assert "서비스와 관련해" in page["messages"][-1]["text"]
    old_basis = page["response_progress"]["based_on_message_id"]
    new = await client.post(
        f"/api/v1/inquiries/{auto_case[0]}/messages",
        headers={**customer["headers"], "Idempotency-Key": "clarify-followup"},
        json={"text": "노트를 저장하면 오류가 납니다", "attachments": []},
    )
    assert new.status_code == 201, new.text
    updated = await chat(client, customer, auto_case)
    assert updated["response_progress"]["state"] == "analyzing"
    assert updated["response_progress"]["based_on_message_id"] != old_basis


async def test_unusable_evidence_hands_off_without_customer_internal_details(
    client, customer, auto_case
):
    auto_case[2].update(draft_result="직접 확인 필요", draft=None)
    assert (await deliver(client, auto_case)).status_code == 200
    page = await chat(client, customer, auto_case)
    assert page["response_progress"]["state"] == "waiting_agent"
    assert "draft_result" not in page and "analysis" not in page


async def test_failed_analysis_does_not_think_forever(client, customer, auto_case):
    raw = {
        k: auto_case[2][k]
        for k in [
            "run_id",
            "tenant_id",
            "inquiry_id",
            "inquiry_revision",
            "based_on_message_id",
        ]
    }
    raw.update(
        run_status="실패",
        error={"code": "LLM_TIMEOUT", "message": "private failure", "retryable": True},
    )
    response = await client.post(
        CALLBACK, headers=callback_headers(auto_case[1]), json=raw
    )
    assert response.status_code == 200, response.text
    page = await chat(client, customer, auto_case)
    assert page["response_progress"]["state"] == "waiting_agent"
    assert "private failure" not in str(page) and "LLM_TIMEOUT" not in str(page)


async def test_closed_inquiry_stops_progress(
    client, customer, auto_case, session_factory
):
    async with session_factory() as session:
        inquiry = await session.scalar(
            select(Inquiry).where(Inquiry.public_id == auto_case[0])
        )
        inquiry.status = InquiryStatus.CLOSED
        await session.commit()
    page = await chat(client, customer, auto_case)
    assert page["response_progress"]["state"] == "closed"


@pytest.mark.parametrize("mode", ["direct", "approved_ai_draft", "edited_ai_draft"])
async def test_human_delivery_is_agent_even_when_ai_wrote_the_draft(
    client,
    customer,
    agent,
    inquiry,
    session_factory,
    ai,
    mode,
):
    from test_answers import detail, with_draft, body_from, send

    if mode == "direct":
        view = await detail(client, agent, inquiry)
    else:
        view, _ = await with_draft(client, agent, inquiry, session_factory, ai)
    payload = body_from(view)
    if mode == "edited_ai_draft":
        payload["answer_text"] = "담당자가 확인하고 수정한 안내입니다."
    response = await send(client, agent, inquiry, payload)
    assert response.status_code == 201, response.text
    page = await chat(client, customer, [inquiry["inquiry_id"]])
    final = page["messages"][-1]
    assert final["sender"] == "agent"
    assert final["message_type"] == "최종 답변"
    assert final["answer_id"] == response.json()["answer_id"]
    assert page["response_progress"]["state"] == "answered"
    audited = await detail(client, agent, inquiry)
    assert audited["answers"][-1]["delivery_mode"] == "담당자"
