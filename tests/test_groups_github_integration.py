"""8단계의 설치·소스 검증을 9단계 Issue 전달과 연결해 검증한다. 실제 HTTP는 사용하지 않는다."""
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text

from app.core.github import GitHubClient
from app.core.group_issues import get_issue_gateway
from app.db.models import AnalysisRequest, AnalysisRun, GitHubIntegration, GitHubSource, Inquiry, InquiryGroup, IssueJob, Message, SurgeEpisode
from app.core.enums import MessageSender, MessageType
from app.services.github_security import source_payload
from app.services.group_github import IntegrationIssueGateway
from app.workers.group_issues import run_once
from test_group_issues import due
from test_groups_alerts import add_inquiry

pytestmark=pytest.mark.anyio


@pytest.fixture
async def connected(customer,session_factory):
    async with session_factory() as s:
        row=GitHubIntegration(integration_id="github_alpha",tenant_id="alpha",installation_id=11,repository_id=22,repository="example/demo",security_level=2,include_paths=["sync"],exclude_patterns=[],enabled=True,status="연결 완료",generation=1)
        s.add(row);await s.commit()
    return row


@pytest.fixture
async def episode(customer,session_factory):
    for _ in range(8): _,group=await add_inquiry(session_factory,customer)
    async with session_factory() as s:
        return group,await s.scalar(select(SurgeEpisode)),await s.scalar(select(IssueJob))


class GitHubHTTP:
    def __init__(self,factory):
        self.factory=factory;self.issues=[];self.posts=0;self.calls=[];self.permission="write";self.repo_id=22;self.lost=False;self.on_list=None;self.reject_token=False
        self.transport=httpx.MockTransport(self.handle)
        self.github=GitHubClient(SimpleNamespace(github_app_id="7",github_app_private_key_base64=SecretStr("test-only")),transport=self.transport)
        self.github.app_token=lambda:"test-app-jwt"

    def gateway(self):
        return IntegrationIssueGateway(self.factory,self.github,transport=self.transport)

    async def handle(self,req):
        self.calls.append(req)
        assert req.url.host=="api.github.com"
        if req.url.path=="/app/installations/11/access_tokens":
            assert json.loads(req.content)=={"repository_ids":[22],"permissions":{"issues":"write","metadata":"read"}}
            if self.reject_token:return httpx.Response(403,text="private-install-token-error")
            return httpx.Response(201,json={"token":"fake-install-token","permissions":{"issues":self.permission,"metadata":"read"}})
        assert req.headers["Authorization"]=="Bearer fake-install-token"
        if req.url.path=="/repos/example/demo":
            return httpx.Response(200,json={"id":self.repo_id,"full_name":"example/demo","has_issues":True,"archived":False})
        assert req.url.path=="/repos/example/demo/issues"
        if req.method=="GET":
            if self.on_list:await self.on_list()
            return httpx.Response(200,json=self.issues)
        self.posts+=1
        item={**json.loads(req.content),"number":1,"html_url":"https://github.com/example/demo/issues/1"}
        self.issues.append(item)
        if self.lost:raise httpx.ReadTimeout("private-upstream-error")
        return httpx.Response(201,json=item)


async def source_and_candidate(factory,actor,*,path="sync/save.ts",sha="a"*40,source_phase="ready",level=None,tenant="alpha",candidate_sha=None):
    inquiry,group=await add_inquiry(factory,actor)
    async with factory() as s:
        integration=await s.scalar(select(GitHubIntegration).where(GitHubIntegration.tenant_id==tenant))
        if level is not None:integration.security_level=level
        kind="커밋" if integration.security_level==1 else "코드"
        payload=source_payload(integration,{"path":path},sha,"마스킹된 코드","코드" if kind=="코드" else "커밋","private@example.com FAKE_KEY_moa_demo_x")
        source=GitHubSource(integration_id=integration.integration_id,source_id=payload["source_id"],version=sha,path=path,blob_sha="b"*40,payload=payload,source_revision=1,phase=source_phase,searchable=source_phase=="ready")
        s.add(source)
        message=Message(inquiry_id=inquiry.inquiry_id,sequence=1,sender=MessageSender.CUSTOMER,message_type=MessageType.CUSTOMER_MESSAGE,text="민감한 원문")
        s.add(message);await s.flush()
        request=AnalysisRequest(inquiry_id=inquiry.inquiry_id,inquiry_revision=1,based_on_message_id=message.message_id,snapshot={},review_only=False,run_id="RUN-"+uuid4().hex)
        s.add(request);await s.flush()
        s.add(AnalysisRun(run_id=request.run_id,request_id=request.request_id,result={"draft":{"evidence":{"code":[{"path":path,"commit_sha":candidate_sha or sha}]}}},response={"apply_status":"applied"}))
        row=await s.get(Inquiry,inquiry.inquiry_id);row.latest_analysis_request_id=request.request_id
        await s.commit()
        return inquiry,group,source


async def test_real_gateway_uses_verified_installation_and_default_factory(session_factory,connected,episode):
    assert isinstance(get_issue_gateway(session_factory),IntegrationIssueGateway)
    http=GitHubHTTP(session_factory)
    await run_once(session_factory,http.gateway())
    assert http.posts==1
    async with session_factory() as s:
        job=await s.scalar(select(IssueJob))
        assert job.status=="완료" and job.payload["github_target"]["repository_id"]==22
        assert "fake-install-token" not in json.dumps(job.payload)


@pytest.mark.parametrize("condition,code",[("permission","GITHUB_ISSUES_PERMISSION_REQUIRED"),("repo","GITHUB_REPOSITORY_MISMATCH"),("denied","GITHUB_ACCESS_DENIED")])
async def test_permission_and_repository_validation_fail_closed(session_factory,connected,episode,condition,code):
    http=GitHubHTTP(session_factory)
    if condition=="permission":http.permission="read"
    if condition=="repo":http.repo_id=23
    if condition=="denied":http.reject_token=True
    await run_once(session_factory,http.gateway())
    assert http.posts==0
    async with session_factory() as s:
        job=await s.scalar(select(IssueJob));assert job.status=="실패" and job.error_code==code


async def test_unconnected_tenant_cannot_use_other_tenant_installation(session_factory,foreign_customer,connected):
    for _ in range(8):await add_inquiry(session_factory,foreign_customer)
    http=GitHubHTTP(session_factory)
    await run_once(session_factory,http.gateway())
    assert http.calls==[]
    async with session_factory() as s:
        job=await s.scalar(select(IssueJob));assert job.status=="대기" and job.error_code=="GITHUB_INTEGRATION_NOT_READY"


@pytest.mark.parametrize("change,code",[("disable","GITHUB_INTEGRATION_DISABLED"),("repo","GITHUB_TARGET_CHANGED"),("policy","GITHUB_POLICY_CHANGED")])
async def test_policy_rechecked_after_lookup_before_post(session_factory,connected,episode,change,code):
    http=GitHubHTTP(session_factory)
    async def alter():
        async with session_factory() as s:
            row=await s.get(GitHubIntegration,connected.integration_id)
            if change=="disable":row.enabled=False
            if change=="repo":row.repository_id=23;row.repository="example/new"
            if change=="policy":row.exclude_patterns=["sync/"]
            await s.commit()
    http.on_list=alter
    await run_once(session_factory,http.gateway())
    assert http.posts==0
    async with session_factory() as s:
        job=await s.scalar(select(IssueJob));assert job.status=="실패" and job.error_code==code


async def test_lost_response_recovered_with_new_installation_token(session_factory,connected,episode):
    http=GitHubHTTP(session_factory);http.lost=True
    await run_once(session_factory,http.gateway());await due(session_factory)
    await run_once(session_factory,http.gateway())
    assert http.posts==1
    assert sum(r.url.path.endswith("access_tokens") for r in http.calls)==2
    async with session_factory() as s:assert (await s.scalar(select(IssueJob))).status=="완료"


async def test_changed_repository_after_uncertain_post_never_retargets(session_factory,connected,episode):
    http=GitHubHTTP(session_factory);http.lost=True
    await run_once(session_factory,http.gateway())
    async with session_factory() as s:
        row=await s.get(GitHubIntegration,connected.integration_id);row.repository_id=23;row.repository="example/new";await s.commit()
    await due(session_factory)
    await run_once(session_factory,http.gateway())
    assert http.posts==1
    async with session_factory() as s:
        job=await s.scalar(select(IssueJob));assert job.status=="확인 필요" and job.error_code=="GITHUB_TARGET_CHANGED"


@pytest.mark.parametrize("path,phase,sha,expected",[("sync/save.ts","ready","a"*40,1),("labels/result.ts","ready","a"*40,0),("sync/.env","ready","a"*40,0),("private/key.ts","ready","a"*40,0),("sync/save.ts","retired","a"*40,0),("sync/save.ts","ready","c"*40,0)])
async def test_candidates_require_current_allowed_source(client,agent,customer,session_factory,connected,path,phase,sha,expected):
    _,group,_=await source_and_candidate(session_factory,customer,path=path,source_phase=phase,candidate_sha=sha)
    response=await client.get(f"/api/v1/admin/groups/{group.public_id}",headers=agent["headers"])
    assert response.status_code==200,response.text
    candidates=response.json()["suspected_code"]
    assert len(candidates)==expected
    assert "private@example.com" not in response.text and "FAKE_KEY" not in response.text


@pytest.mark.parametrize("include",[True,False])
async def test_issue_snapshots_verified_candidates_and_rechecks_source(client,agent,customer,session_factory,connected,include):
    _,group,source=await source_and_candidate(session_factory,customer)
    # 자동 작업을 만들지 않는 계정 분류의 급증 구간으로 수동 요청을 검증한다.
    from app.services.groups import lock_groups,evaluate,utcnow
    async with session_factory() as s:
        ep=SurgeEpisode(group_id=group.group_id,started_at=utcnow(),last_received_at=utcnow());s.add(ep);await s.commit()
    response=await client.post(f"/api/v1/admin/groups/{group.public_id}/github-issues",headers={**agent["headers"],"Idempotency-Key":"candidate"},json={"episode_id":ep.public_id,"include_suspected_code":include})
    assert response.status_code==202,response.text
    async with session_factory() as s:
        job=await s.scalar(select(IssueJob))
        assert ("sync/save.ts" in job.payload["body"]) is include
        assert "private@example.com" not in job.payload["body"] and "FAKE_KEY" not in job.payload["body"]
        current=await s.get(GitHubSource,source.source_pk);current.phase="retired";current.searchable=False;await s.commit()
    http=GitHubHTTP(session_factory)
    await run_once(session_factory,http.gateway())
    assert http.posts==(0 if include else 1)
    async with session_factory() as s:
        job=await s.scalar(select(IssueJob))
        assert job.error_code==("GITHUB_EVIDENCE_CHANGED" if include else None)


async def test_groups_branch_upgrade_preserves_jobs_and_alerts(db_engine,migrate,session_factory,episode):
    tables=["users","inquiries","inquiry_groups","surge_episodes","alerts","issue_jobs"]
    async with db_engine.begin() as c:
        await c.run_sync(migrate,"downgrade","0012_groups_alerts")
        before={table:(await c.execute(text(f"SELECT * FROM {table} ORDER BY 1"))).all() for table in tables}
        await c.run_sync(migrate,"upgrade","head")
        assert (await c.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()==["0013_groups_github"]
        for table in tables:
            assert (await c.execute(text(f"SELECT * FROM {table} ORDER BY 1"))).all()==before[table]


async def test_metadata_only_policy_exposes_paths_without_code(client,agent,customer,session_factory,connected):
    _,group,_=await source_and_candidate(session_factory,customer,level=1)
    response=await client.get(f"/api/v1/admin/groups/{group.public_id}",headers=agent["headers"])
    assert response.status_code==200
    assert response.json()["suspected_code"][0]["path"]=="sync/save.ts"
    assert "마스킹된 코드" not in response.text


async def test_legacy_pending_job_binds_once_without_changing_frozen_body(session_factory,connected,episode):
    async with session_factory() as s:
        job=await s.scalar(select(IssueJob));job.payload={key:value for key,value in job.payload.items() if key not in ("github_target","github_sources")};original=job.payload["body"];await s.commit()
    http=GitHubHTTP(session_factory)
    await run_once(session_factory,http.gateway())
    assert http.posts==1 and http.issues[0]["body"]==original
    async with session_factory() as s:
        job=await s.scalar(select(IssueJob));assert job.payload["github_target"]["repository_id"]==22


async def test_unbound_uncertain_job_never_guesses_repository(session_factory,connected,episode):
    async with session_factory() as s:
        job=await s.scalar(select(IssueJob));job.payload={key:value for key,value in job.payload.items() if key!="github_target"};job.phase="reconcile";await s.commit()
    http=GitHubHTTP(session_factory)
    await run_once(session_factory,http.gateway())
    assert http.calls==[]
    async with session_factory() as s:
        job=await s.scalar(select(IssueJob));assert job.status=="확인 필요" and job.error_code=="GITHUB_TARGET_UNKNOWN"


async def test_malformed_issue_lookup_never_assumes_no_duplicate(session_factory,connected,episode):
    http=GitHubHTTP(session_factory)
    http.issues=[{"number":1,"body":[],"html_url":"https://github.com/example/demo/issues/1"}]
    for _ in range(5):
        await due(session_factory);await run_once(session_factory,http.gateway())
    assert http.posts==0
    async with session_factory() as s:
        job=await s.scalar(select(IssueJob));assert job.status=="실패" and job.error_code=="GITHUB_INVALID_RESPONSE"
