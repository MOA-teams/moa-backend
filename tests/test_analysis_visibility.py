from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import AnalysisRequest
from app.workers.analysis import run_once
from test_answers import body_from, close, detail, send
from test_pipeline import ai, callback_headers, due, submit, CALLBACK

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("closed", [False, True])
async def test_invalidated_analysis_is_not_shown_as_pending_before_or_after_worker(
    client, agent, inquiry, session_factory, ai, running, closed
):
    if running:
        await submit(session_factory, ai)
    before = await detail(client, agent, inquiry)
    assert before["run_status"] == ("처리 중" if running else "대기")
    answered = await send(client, agent, inquiry, body_from(before))
    assert answered.status_code == 201
    if closed:
        response = await close(
            client, agent, inquiry, answered.json()["inquiry_revision"]
        )
        assert response.status_code == 200

    after = await detail(client, agent, inquiry)
    assert after["run_status"] is None
    assert after["analysis_request_id"] == before["analysis_request_id"]
    assert after["draft"] is None
    async with session_factory() as session:
        job = await session.scalar(select(AnalysisRequest))
        assert job.run_status.value == before["run_status"]
        assert job.phase == ("poll" if running else "submit")

    await due(session_factory)
    assert await run_once(session_factory, ai.client)
    async with session_factory() as session:
        assert (await session.scalar(select(AnalysisRequest))).phase == "superseded"
    assert (await detail(client, agent, inquiry))["run_status"] is None


@pytest.mark.parametrize("status", ["완료", "실패"])
async def test_terminal_result_history_remains_visible_after_close(
    client, agent, inquiry, session_factory, ai, status
):
    ai.result_status = status
    run = await submit(session_factory, ai)
    response = await client.post(
        CALLBACK, headers=callback_headers(run), json=ai.result(run)
    )
    assert response.status_code == 200
    view = await detail(client, agent, inquiry)
    assert view["run_status"] == status
    answered = await send(client, agent, inquiry, body_from(view))
    assert answered.status_code == 201
    assert (
        await close(client, agent, inquiry, answered.json()["inquiry_revision"])
    ).status_code == 200
    after = await detail(client, agent, inquiry)
    assert after["run_status"] == status
    assert after["latest_run_id"] == run
    assert after["analysis_error"] == view["analysis_error"]


async def test_replacement_request_is_still_displayed_as_pending(
    client, agent, customer, inquiry, session_factory, ai
):
    previous = await detail(client, agent, inquiry)
    response = await client.post(
        f"/api/v1/inquiries/{inquiry['inquiry_id']}/messages",
        headers={**customer["headers"], "Idempotency-Key": "replacement"},
        json={"text": "새 증상입니다."},
    )
    assert response.status_code == 201
    await due(session_factory)
    async with session_factory() as session:
        old = await session.get(AnalysisRequest, previous["analysis_request_id"])
        old.available_at -= timedelta(seconds=5)
        await session.commit()
    assert await run_once(session_factory, ai.client)
    current = await detail(client, agent, inquiry)
    assert current["analysis_request_id"] != previous["analysis_request_id"]
    assert current["run_status"] == "대기"
