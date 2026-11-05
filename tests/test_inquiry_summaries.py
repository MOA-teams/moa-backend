import asyncio
import json

import httpx
import pytest
from sqlalchemy import select, text

from app.core.rag import RagClient
from app.db.models import Inquiry, InquirySummaryJob
from app.services.groups import assign_group
from app.workers.inquiry_summaries import run_once
from test_groups_alerts import add_inquiry, result

pytestmark=pytest.mark.anyio


class Rag:
    def __init__(self):
        self.sources={};self.calls=[];self.fail=False;self.block=None;self.entered=asyncio.Event()
        self.client=RagClient("http://ai.test","fake",transport=httpx.MockTransport(self.handle))

    async def handle(self,req):
        self.calls.append(req)
        assert req.headers["X-Tenant-ID"]=="alpha"
        if self.fail:return httpx.Response(503,text="private-error")
        if req.method=="GET":
            assert req.url.params["source_type"]=="문의 요약"
            _,source,_,version=req.url.path.rsplit("/",3)
            value=self.sources[(source,int(version))]
            return httpx.Response(200,json={"source_id":source,"version":int(version),"source_revision":value["source_revision"],"status":value["status"],"review_required":False,"searchable":value["status"]=="유효","indexing_status":"완료"})
        body=json.loads(req.content)
        if req.url.path.endswith("source-statuses"):
            for change in body["sources"]:
                source=self.sources[(change["source_id"],change["version"])]
                assert source["source_revision"]==change["source_revision"]
            return httpx.Response(200,json={"knowledge_revision":1,"updated_count":len(body["sources"]),"search_excluded":True})
        assert body["source_type"]=="문의 요약" and body["visibility"]=="내부 전용"
        key=(body["source_id"],body["version"])
        previous=self.sources.get(key)
        if previous and previous["source_revision"]>body["source_revision"]:
            return httpx.Response(409,json={"error":{"code":"STALE_SOURCE_REVISION"}})
        if self.block:
            self.entered.set();await self.block.wait()
        self.sources[key]=body
        return httpx.Response(202,json={"source_id":body["source_id"],"version":body["version"],"indexing_status":"대기"})


async def due(factory):
    async with factory() as s:
        await s.execute(text("UPDATE inquiry_summary_jobs SET available_at=now()-interval '1 second'"));await s.commit()


async def drain(factory,rag):
    for _ in range(8):
        await due(factory)
        if not await run_once(factory,rag.client):break


async def update(factory,row,**kwargs):
    async with factory() as s:
        inquiry=await s.get(Inquiry,row.inquiry_id,with_for_update=True)
        await assign_group(s,inquiry,result(**kwargs));await s.commit()


async def test_content_version_metadata_revision_and_exclusion(customer,session_factory):
    row,group=await add_inquiry(session_factory,customer)
    rag=Rag();await drain(session_factory,rag)
    first=rag.sources[(row.public_id,1)]
    assert first["metadata"]["group_id"]==group.public_id and first["status"]=="유효"
    await update(session_factory,row,code="E-413")
    await drain(session_factory,rag)
    second=rag.sources[(row.public_id,1)]
    assert second["metadata"]["group_id"]!=first["metadata"]["group_id"]
    assert second["source_revision"]>first["source_revision"] and second["content_hash"]==first["content_hash"]
    await update(session_factory,row,code="E-413",summary="수정된 요약")
    await drain(session_factory,rag)
    assert rag.sources[(row.public_id,1)]["status"]=="폐기"
    assert rag.sources[(row.public_id,2)]["status"]=="유효"
    async with session_factory() as s:
        jobs=list((await s.scalars(select(InquirySummaryJob).order_by(InquirySummaryJob.job_id))).all())
        assert all(job.phase=="done" for job in jobs)


async def test_failure_does_not_rollback_inquiry_and_recovers(customer,session_factory):
    row,_=await add_inquiry(session_factory,customer)
    rag=Rag();rag.fail=True
    await run_once(session_factory,rag.client)  # barrier 없는 첫 단계
    await run_once(session_factory,rag.client)
    async with session_factory() as s:
        job=await s.scalar(select(InquirySummaryJob))
        assert job.phase=="submit" and job.error_code=="AI_REQUEST_REJECTED"
        assert await s.get(Inquiry,row.inquiry_id)
    rag.fail=False
    await drain(session_factory,rag)
    async with session_factory() as s: assert (await s.scalar(select(InquirySummaryJob))).phase=="done"


async def test_stale_jobs_never_overwrite_latest_group(customer,session_factory):
    row,_=await add_inquiry(session_factory,customer)
    await update(session_factory,row,code="E-413")
    await update(session_factory,row,code="E-401",summary="새 본문")
    rag=Rag();await drain(session_factory,rag)
    async with session_factory() as s:
        jobs=list((await s.scalars(select(InquirySummaryJob).order_by(InquirySummaryJob.job_id))).all())
        assert [j.phase for j in jobs]==["superseded","superseded","done"]
    assert rag.sources[(row.public_id,1)]["status"]=="폐기"
    assert rag.sources[(row.public_id,2)]["metadata"]["normalized_error_code"]=="E-401"


async def test_http_in_flight_serialized_across_workers(customer,session_factory):
    row,_=await add_inquiry(session_factory,customer)
    rag=Rag()
    await run_once(session_factory,rag.client)
    rag.block=asyncio.Event()
    first=asyncio.create_task(run_once(session_factory,rag.client))
    await asyncio.wait_for(rag.entered.wait(),5)
    await update(session_factory,row,code="E-413")
    assert not await run_once(session_factory,rag.client)
    rag.block.set();await first
    await drain(session_factory,rag)
    assert rag.sources[(row.public_id,1)]["metadata"]["normalized_error_code"]=="E-413"


async def test_retries_exhausted_are_visible_without_sensitive_error(customer,session_factory):
    row,_=await add_inquiry(session_factory,customer)
    rag=Rag();rag.fail=True
    await drain(session_factory,rag)
    async with session_factory() as s:
        job=await s.scalar(select(InquirySummaryJob))
        assert job.phase=="failed" and job.attempts==5
        assert "private" not in job.error_code
