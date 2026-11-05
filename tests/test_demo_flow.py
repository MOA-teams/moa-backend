import json

import httpx
import pytest

from scripts.seed_documents import SeedError
from scripts.verify_demo_flow import EDIT, answer_payload, operation, run_flow


@pytest.fixture
def detail():
    return {
        "run_status": "완료",
        "latest_run_id": "RUN-test",
        "draft_result": "초안 가능",
        "revision": 1,
        "latest_customer_message_id": "MSG-000001",
        "backend_knowledge_revision": 12,
        "verification": {"status": "통과"},
        "draft": {
            "draft_id": "DRAFT-000001",
            "revision": 1,
            "answer_text": "Pro는 월 9,900원입니다.",
            "evidence": {"docs": [{"doc_id": "DOC-001", "version": 1}], "code": []},
        },
    }


def test_answer_keeps_original_evidence_and_optimistic_revisions(detail):
    body = answer_payload(detail)
    assert body["expected_backend_knowledge_revision"] == 12
    assert body["answer_text"] == detail["draft"]["answer_text"] + EDIT
    assert body["evidence"] == detail["draft"]["evidence"]
    assert detail["draft"]["answer_text"] == "Pro는 월 9,900원입니다."


@pytest.mark.parametrize("failure", ["verification", "empty_docs", "code"])
def test_demo_does_not_automatically_approve_failed_or_unverified_draft(
    detail, failure
):
    if failure == "verification":
        detail["verification"]["status"] = "실패"
    elif failure == "empty_docs":
        detail["draft"]["evidence"]["docs"] = []
    else:
        detail["draft"]["evidence"]["code"] = [{"path": "billing/pricing.ts"}]
    with pytest.raises(SeedError, match="VERIFIED_DOCUMENT_DRAFT_REQUIRED"):
        answer_payload(detail)


def test_lost_mutation_response_replays_stored_revision_and_body(tmp_path):
    state = {"run_id": "retry", "operations": {}}
    seen = []

    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ReadTimeout("lost")
        return httpx.Response(201, json={"answer_id": "A-1001"})

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(httpx.ReadTimeout):
            operation(
                client,
                state,
                tmp_path / "flow.json",
                "answer",
                "/answers",
                {"expected_inquiry_revision": 1},
                201,
            )
        loaded = json.loads((tmp_path / "flow.json").read_text())
        operation(
            client, loaded, tmp_path / "flow.json", "answer", "/answers", None, 201
        )
    assert seen[0].content == seen[1].content
    assert seen[0].headers["idempotency-key"] == seen[1].headers["idempotency-key"]


@pytest.mark.parametrize("leak", [False, True])
def test_complete_flow_and_resume_or_customer_privacy_failure(tmp_path, detail, leak):
    posts, sent, closed, feedback = [], None, False, False

    def handle(request):
        nonlocal sent, closed, feedback
        path = request.url.path
        if path.endswith("/auth/me"):
            role = request.headers["x-role"]
            return httpx.Response(
                200, json={"user_id": role, "role": role, "tenant_id": "syncnotedemo"}
            )
        if request.method == "POST":
            posts.append(path)
            if path.endswith("/answers"):
                sent = json.loads(request.content)["answer_text"]
                return httpx.Response(
                    201, json={"answer_id": "A-1001", "inquiry_revision": 2}
                )
            if path.endswith("/close"):
                closed = True
                return httpx.Response(200, json={"status": "상담 종료"})
            if path.endswith("/feedback"):
                feedback = True
                return httpx.Response(201, json={"answer_id": "A-1001"})
            return httpx.Response(201, json={"inquiry_id": "Q-1001"})
        if "/admin/" in path:
            return httpx.Response(
                200,
                json={
                    **detail,
                    "status": "상담 종료" if closed else "접수",
                    "feedback_submitted": feedback,
                    "resolution_status": "해결 확인" if feedback else "미확인",
                    "answers": (
                        [
                            {
                                "answer_id": "A-1001",
                                "original_draft": {"answer_text": "원본"},
                                "original_verification": {"status": "통과"},
                                "agent_edit_ratio": 0.1,
                            }
                        ]
                        if sent
                        else []
                    ),
                },
            )
        return httpx.Response(
            200,
            json={
                "messages": [{"answer_id": "A-1001", "text": sent}],
                **({"draft": {}} if leak else {}),
            },
        )

    transport = httpx.MockTransport(handle)
    with httpx.Client(
        base_url="http://localhost", headers={"x-role": "customer"}, transport=transport
    ) as customer, httpx.Client(
        base_url="http://localhost", headers={"x-role": "admin"}, transport=transport
    ) as agent:
        if leak:
            with pytest.raises(SeedError, match="CUSTOMER_INTERNAL_DATA_EXPOSED"):
                run_flow(
                    customer,
                    agent,
                    tmp_path / "flow.json",
                    "http://localhost",
                    "syncnotedemo",
                )
            assert len(posts) == 2 and not closed
        else:
            result = run_flow(
                customer,
                agent,
                tmp_path / "flow.json",
                "http://localhost",
                "syncnotedemo",
            )
            again = run_flow(
                customer,
                agent,
                tmp_path / "flow.json",
                "http://localhost",
                "syncnotedemo",
            )
            assert result == again and result["completed"]
            assert len(posts) == 4
