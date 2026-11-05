"""별도 검증 기업에서 실제 AI 초안→수정 발송→종료→평가를 재현한다."""

import argparse
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.seed_documents import SeedError, digest, request, save_state, state_lock

QUESTION = "[통합 검증용 가상 문의] 싱크노트 Pro 요금제를 검토하고 있습니다. 월간 요금과 연간 결제 요금, 포함된 저장 공간을 알려 주세요."
EDIT = "\n\n추가로 궁금한 점이 있으시면 말씀해 주세요."


def operation(client, state, state_path, name, path, body, expected):
    operations = state.setdefault("operations", {})
    if name not in operations:
        operations[name] = {"path": path, "body": body, "expected": expected}
        save_state(state_path, state)
    item = operations[name]
    if item["path"] != path or item["expected"] != expected:
        raise SeedError("OPERATION_CHANGED")
    if "response" not in item:
        item["response"] = request(
            client,
            "POST",
            path,
            expected=expected,
            json=item["body"],
            headers={"Idempotency-Key": f"demo-{state['run_id']}-{name}"},
        )
        save_state(state_path, state)
    return item["response"]


def answer_payload(detail):
    draft = detail.get("draft")
    if (
        detail.get("draft_result") != "초안 가능"
        or not draft
        or (detail.get("verification") or {}).get("status") != "통과"
        or not draft["evidence"]["docs"]
        or draft["evidence"]["code"]
    ):
        raise SeedError("VERIFIED_DOCUMENT_DRAFT_REQUIRED")
    return {
        "draft_id": draft["draft_id"],
        "draft_revision": draft["revision"],
        "expected_inquiry_revision": detail["revision"],
        "expected_last_customer_message_id": detail["latest_customer_message_id"],
        "expected_backend_knowledge_revision": detail["backend_knowledge_revision"],
        "answer_text": draft["answer_text"] + EDIT,
        "evidence": draft["evidence"],
    }


def run_flow(customer, agent, state_path, origin, tenant, *, timeout=180):
    customer_me = request(customer, "GET", "/api/v1/auth/me")
    agent_me = request(agent, "GET", "/api/v1/auth/me")
    if (
        customer_me["role"] != "customer"
        or agent_me["role"] not in ("agent", "admin")
        or customer_me["tenant_id"] != tenant
        or agent_me["tenant_id"] != tenant
    ):
        raise SeedError("DEMO_ACCOUNT_MISMATCH")
    binding = {
        "origin": origin,
        "tenant_id": tenant,
        "customer_id": customer_me["user_id"],
        "agent_id": agent_me["user_id"],
        "question_hash": digest(QUESTION),
    }
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state.get("binding") != binding:
            raise SeedError("STATE_OR_ACCOUNT_CHANGED")
    else:
        state = {"binding": binding, "run_id": uuid4().hex, "operations": {}}
        save_state(state_path, state)
    created = operation(
        customer,
        state,
        state_path,
        "inquiry",
        "/api/v1/inquiries",
        {"message": QUESTION, "customer_category": "결제"},
        201,
    )
    inquiry = created["inquiry_id"]
    admin_path = f"/api/v1/admin/inquiries/{inquiry}"
    if "answer" not in state["operations"]:
        deadline = time.monotonic() + timeout
        while True:
            detail = request(agent, "GET", admin_path)
            if detail["run_status"] == "실패":
                raise SeedError("ANALYSIS_FAILED")
            if detail["run_status"] == "완료":
                break
            if time.monotonic() >= deadline:
                raise SeedError("ANALYSIS_TIMEOUT")
            time.sleep(2)
        state["analysis"] = {
            "run_id": detail["latest_run_id"],
            "draft_result": detail["draft_result"],
            "verification": (detail.get("verification") or {}).get("status"),
        }
        save_state(state_path, state)
        body = answer_payload(detail)
    else:
        body = None  # 응답 유실 재시도는 저장된 기준 revision·본문을 사용한다.
    answer = operation(
        agent, state, state_path, "answer", admin_path + "/answers", body, 201
    )
    chat_path = f"/api/v1/inquiries/{inquiry}"
    chat = request(customer, "GET", chat_path)
    sent = [
        message
        for message in chat["messages"]
        if message["answer_id"] == answer["answer_id"]
    ]
    if (
        len(sent) != 1
        or sent[0]["text"] != state["operations"]["answer"]["body"]["answer_text"]
    ):
        raise SeedError("CUSTOMER_MESSAGE_MISMATCH")
    if any(key in chat for key in ("draft", "verification", "answers", "analysis")):
        raise SeedError("CUSTOMER_INTERNAL_DATA_EXPOSED")
    for message in chat["messages"]:
        if any(
            key in message
            for key in (
                "evidence",
                "original_draft",
                "original_verification",
                "approval",
            )
        ):
            raise SeedError("CUSTOMER_INTERNAL_DATA_EXPOSED")
    closed = operation(
        agent,
        state,
        state_path,
        "close",
        admin_path + "/close",
        {
            "expected_inquiry_revision": answer["inquiry_revision"],
            "resolution_status": "미확인",
        },
        200,
    )
    feedback = operation(
        customer,
        state,
        state_path,
        "feedback",
        chat_path + "/feedback",
        {"rating": 5, "resolved": True, "comment": "통합 검증용 가상 평가"},
        201,
    )
    final = request(agent, "GET", admin_path)
    if (
        closed["status"] != "상담 종료"
        or not final["feedback_submitted"]
        or final["resolution_status"] != "해결 확인"
        or feedback["answer_id"] != answer["answer_id"]
    ):
        raise SeedError("FINAL_STATE_MISMATCH")
    audits = [
        item for item in final["answers"] if item["answer_id"] == answer["answer_id"]
    ]
    if (
        len(audits) != 1
        or not audits[0]["original_draft"]
        or not audits[0]["original_verification"]
        or audits[0]["agent_edit_ratio"] is None
    ):
        raise SeedError("ANSWER_AUDIT_MISSING")
    report = {
        "completed": True,
        "inquiry_id": inquiry,
        "answer_id": answer["answer_id"],
        "analysis": state["analysis"],
        "status": final["status"],
        "resolution_status": final["resolution_status"],
        "feedback_submitted": final["feedback_submitted"],
        "customer_message_matches": True,
        "approval_audit_present": True,
    }
    state["result"] = report
    save_state(state_path, state)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "tenant_id": args.tenant,
                    "message": QUESTION,
                    "steps": [
                        "문의",
                        "실제 AI 분석",
                        "검수 통과 초안 수정 발송",
                        "고객 조회",
                        "종료",
                        "평가",
                        "관리자 확인",
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0
    try:
        url = urlsplit(args.base_url)
        if (
            url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
            or not url.hostname
            or (
                url.scheme != "https"
                and not (
                    url.scheme == "http"
                    and url.hostname in ("127.0.0.1", "localhost", "::1")
                )
            )
        ):
            raise SeedError("HTTPS_OR_LOOPBACK_REQUIRED")
        tokens = [
            os.environ.get(name) for name in ("MOA_CUSTOMER_TOKEN", "MOA_ADMIN_TOKEN")
        ]
        if not all(tokens) or not args.state_file:
            raise SeedError("TOKENS_AND_STATE_REQUIRED")
        origin = args.base_url.rstrip("/")
        with state_lock(args.state_file), httpx.Client(
            base_url=origin,
            headers={"Authorization": "Bearer " + tokens[0]},
            timeout=30,
            trust_env=False,
            follow_redirects=False,
        ) as customer, httpx.Client(
            base_url=origin,
            headers={"Authorization": "Bearer " + tokens[1]},
            timeout=30,
            trust_env=False,
            follow_redirects=False,
        ) as agent:
            print(
                json.dumps(
                    run_flow(customer, agent, args.state_file, origin, args.tenant),
                    ensure_ascii=False,
                )
            )
            return 0
    except SeedError as error:
        print(json.dumps({"error": str(error)}))
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        print(json.dumps({"error": "INPUT_OR_TRANSPORT_ERROR"}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
