import pytest
from sqlalchemy import func,select
from app.db.models import Alert, GroupMembershipHistory, Inquiry, InquirySummaryJob
from test_pipeline import ai, submit, callback_headers, CALLBACK

pytestmark=pytest.mark.anyio


async def test_callback_group_alert_duplicate_and_stale(client,inquiry,agent,customer,session_factory,ai):
    run=await submit(session_factory,ai)
    payload=ai.result(run)
    response=await client.post(CALLBACK,headers=callback_headers(run),json=payload)
    assert response.status_code==200,response.text
    assert response.json()["group_id"]=="G-001"
    repeated=await client.post(CALLBACK,headers=callback_headers(run),json=payload)
    assert repeated.json()["apply_status"]=="duplicate" and repeated.json()["group_id"]=="G-001"
    detail=(await client.get(f"/api/v1/admin/inquiries/{inquiry['inquiry_id']}",headers=agent["headers"])).json()
    assert detail["group_id"]=="G-001"
    listed=(await client.get("/api/v1/admin/inquiries",headers=agent["headers"])).json()
    assert listed["items"][0]["group_id"]=="G-001"
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(GroupMembershipHistory))==1
        assert await s.scalar(select(func.count()).select_from(InquirySummaryJob))==1
        assert await s.scalar(select(func.count()).select_from(Alert).where(Alert.type=="검증 확인 필요"))==1
    await client.post(f"/api/v1/inquiries/{inquiry['inquiry_id']}/messages",headers={**customer["headers"],"Idempotency-Key":"new"},json={"text":"새 메시지"})
    next_run=await submit(session_factory,ai)
    await client.post(f"/api/v1/inquiries/{inquiry['inquiry_id']}/messages",headers={**customer["headers"],"Idempotency-Key":"new2"},json={"text":"다른 메시지"})
    stale=ai.result(next_run);stale["analysis"]["normalized_error_code"]="E-413"
    response=await client.post(CALLBACK,headers=callback_headers(next_run),json=stale)
    assert response.json()["apply_status"]=="ignored_stale"
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(GroupMembershipHistory))==1


async def test_normal_review_only_or_disabled_does_not_alert(client,inquiry,session_factory,ai):
    run=await submit(session_factory,ai)
    payload=ai.result(run);payload["verification"]={"status":"통과","checks":{}}
    response=await client.post(CALLBACK,headers=callback_headers(run),json=payload)
    assert response.status_code==200
    async with session_factory() as s: assert await s.scalar(select(func.count()).select_from(Alert))==0


async def test_failed_result_alert_deduplicated(client,inquiry,session_factory,ai):
    run=await submit(session_factory,ai);ai.result_status="실패"
    for _ in range(2):
        response=await client.post(CALLBACK,headers=callback_headers(run),json=ai.result(run))
        assert response.status_code==200
    async with session_factory() as s:
        alert=await s.scalar(select(Alert));assert alert.type=="분석 실패" and alert.error_code=="AI_PIPELINE_FAILED"
        assert await s.scalar(select(func.count()).select_from(Alert))==1


async def test_worker_retry_exhaustion_creates_one_sanitized_alert(client,inquiry,session_factory,ai):
    from app.workers.analysis import run_once
    from test_pipeline import due
    ai.fail=503
    for _ in range(6):
        await due(session_factory)
        await run_once(session_factory,ai.client)
    async with session_factory() as s:
        alerts=list((await s.scalars(select(Alert))).all())
        assert len(alerts)==1 and alerts[0].type=="분석 실패"
        assert "secret" not in alerts[0].message
