"""새 AI 폴링 응답과 기존 callback이 동일한 초안으로 적용되는지 검증한다."""

from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.pipeline import PipelineClient, PipelineTransportError
from app.db.models import AnalysisDraft, AnalysisRequest, AnalysisRun
from app.schemas.analysis import PipelineResult
from app.workers.analysis import run_once
from test_pipeline import ADMIN, CALLBACK, callback_headers, due, submit
from test_pipeline import ai as ai

pytestmark = pytest.mark.anyio

ROUTING = {"team": "CS", "reason": "공개 문서의 이용 안내", "requires_human": False}
NOTE = "담당 팀: CS. 공개 문서의 이용 안내"


@pytest.mark.parametrize("callback_first", [False, True])
@pytest.mark.parametrize("structured_callback", [False, True])
async def test_routed_draft_poll_and_callback_are_idempotent(
    client,
    agent,
    inquiry,
    session_factory,
    ai,
    monkeypatch,
    callback_first,
    structured_callback,
):
    ai.draft = True
    run = await submit(session_factory, ai)
    original_result = ai.result

    def routed_result(run_id):
        return {**original_result(run_id), "routing": dict(ROUTING)}

    monkeypatch.setattr(ai, "result", routed_result)
    callback_body = original_result(run)
    if structured_callback:
        callback_body["routing"] = dict(ROUTING)
    else:
        callback_body["verification"]["reasons"].append(NOTE)
    poll_result = await ai.client.get("alpha", run)

    if callback_first:
        first = await client.post(
            CALLBACK, headers=callback_headers(run), json=callback_body
        )
        assert first.status_code == 200
        # callback이 이미 확정한 결과가 늦은 poll로 다시 도착하는 경우.
        from app.services.analysis import apply_result

        async with session_factory() as session:
            replay = await apply_result(session, poll_result, tenant="alpha")
        assert replay.apply_status == "duplicate"
    else:
        await due(session_factory)
        assert await run_once(session_factory, ai.client)
        replay = await client.post(
            CALLBACK, headers=callback_headers(run), json=callback_body
        )
        assert replay.status_code == 200
        assert replay.json()["apply_status"] == "duplicate"

    async with session_factory() as session:
        assert (await session.scalar(select(AnalysisRequest))).phase == "done"
        saved = await session.get(AnalysisRun, run)
        assert saved.result["verification"]["reasons"].count(NOTE) == 1
        assert "routing" not in saved.result
        assert (
            await session.scalar(select(func.count()).select_from(AnalysisDraft)) == 1
        )

    detail = await client.get(
        f"{ADMIN}/{inquiry['inquiry_id']}", headers=agent["headers"]
    )
    assert detail.status_code == 200
    assert detail.json()["run_status"] == "완료"
    assert detail.json()["draft"]["answer_text"] == "확인 중입니다."
    assert NOTE in detail.json()["verification"]["reasons"]


def pending_result():
    return {
        "run_id": "RUN-routing-test",
        "tenant_id": "alpha",
        "inquiry_id": "Q-1001",
        "inquiry_revision": 1,
        "based_on_message_id": "MSG-000001",
        "run_status": "처리 중",
        "current_stage": "RAG 검색",
    }


@pytest.mark.parametrize("routing", [None, ROUTING])
async def test_poll_accepts_routing_before_draft_is_ready(routing):
    payload = {**pending_result(), "routing": routing}
    client = PipelineClient(
        "http://ai.test",
        "test-token",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    result = await client.get("alpha", payload["run_id"])
    assert result.current_stage == "RAG 검색"
    assert result.draft is None and result.verification is None


def test_missing_and_null_routing_preserve_legacy_fingerprint():
    legacy = PipelineResult.model_validate(pending_result()).model_dump(mode="json")
    assert "routing" not in legacy
    assert (
        PipelineResult.model_validate({**legacy, "routing": None}).model_dump(
            mode="json"
        )
        == legacy
    )


@pytest.mark.parametrize(
    "routing",
    [
        {**ROUTING, "team": "unknown"},
        {**ROUTING, "requires_human": "false"},
        {**ROUTING, "reason": "x" * 501},
        {**ROUTING, "unexpected": True},
    ],
)
async def test_invalid_routing_is_still_rejected(routing):
    payload = {**pending_result(), "routing": routing}
    client = PipelineClient(
        "http://ai.test",
        "test-token",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    with pytest.raises(PipelineTransportError, match="AI_INVALID_RESPONSE"):
        await client.get("alpha", payload["run_id"])


def test_unrelated_extra_fields_remain_forbidden():
    with pytest.raises(ValidationError):
        PipelineResult.model_validate({**pending_result(), "unrecognized": True})


def test_normalization_preserves_human_review_checks_and_does_not_duplicate_note():
    payload = {
        **pending_result(),
        "routing": {**ROUTING, "requires_human": True},
        "verification": {
            "status": "확인 불가",
            "checks": {"auto_response_scope": "확인 불가"},
            "reasons": [NOTE],
        },
    }
    original = deepcopy(payload)
    result = PipelineResult.model_validate(payload)
    assert payload == original
    assert result.verification.reasons == [NOTE]
    assert result.verification.checks["auto_response_scope"].value == "확인 불가"
    assert result.verification.status.value == "확인 불가"
