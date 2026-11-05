import pytest
from sqlalchemy import func, select

from app.core.auto_response import safe_answer
from app.core.customer_content import contains_internal_content
from app.db.models import Answer, Inquiry, Message
from test_answers import body_from, detail, send, with_draft
from test_pipeline import ai as ai


@pytest.mark.parametrize(
    "text",
    [
        "```typescript\nconst secret = 1;\n```",
        "~~~python\nprint('internal')\n~~~",
        "<code>internal()</code>",
        "&lt;pre&gt;internal()&lt;/pre&gt;",
        "sync/saveHandler.ts의 flushBatch를 확인하세요.",
        "preview/androidImageEngine.kt를 바꾸세요.",
        "ｓｙｎｃ／ｓａｖｅＨａｎｄｌｅｒ．ｔｓ",
        "C:\\app\\billing\\pricing.ts",
        "설정은 .env.production을 확인하세요.",
    ],
)
def test_explicit_code_is_blocked_for_manual_and_automatic_delivery(text):
    assert contains_internal_content(text)
    assert not safe_answer(text)


@pytest.mark.parametrize(
    "text",
    [
        "E-502 오류를 확인 중입니다. 앱 2.4.0에서 발생했나요?",
        "Free는 10MB, Pro는 100MB까지 첨부할 수 있습니다.",
        "비밀번호를 재설정한 뒤 다시 로그인해 주세요.",
        "공개 안내: https://syncnote.example/help/refund",
        "요금제는 Free / Pro / Team입니다. 첨부 안내.pdf를 확인해 주세요.",
    ],
)
def test_normal_customer_instructions_are_not_blocked(text):
    assert not contains_internal_content(text)


@pytest.mark.anyio
@pytest.mark.parametrize("use_draft", [False, True])
async def test_rejected_answer_rolls_back_and_corrected_request_can_be_sent(
    client,
    customer,
    agent,
    inquiry,
    session_factory,
    ai,
    use_draft,
):
    if use_draft:
        view, original = await with_draft(client, agent, inquiry, session_factory, ai)
    else:
        view = await detail(client, agent, inquiry)
    body = body_from(view, answer_text="sync/saveHandler.ts 내부 코드를 확인하세요.")
    response = await send(client, agent, inquiry, body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CUSTOMER_CONTENT_UNSAFE"
    assert "saveHandler" not in response.text
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Answer)) == 0
        assert await session.scalar(select(func.count()).select_from(Message)) == 2
        row = await session.scalar(select(Inquiry))
        assert row.revision == view["revision"]
    body["answer_text"] = "저장 문제를 확인 중입니다. 잠시 후 다시 안내드리겠습니다."
    accepted = await send(client, agent, inquiry, body)  # 거절된 멱등 기록도 롤백됨
    assert accepted.status_code == 201
    replay = await send(client, agent, inquiry, body)
    assert replay.json() == accepted.json()
    chat = await client.get(
        f"/api/v1/inquiries/{inquiry['inquiry_id']}", headers=customer["headers"]
    )
    assert chat.json()["messages"][-1]["text"] == body["answer_text"]


def test_swagger_explains_customer_content_rejection():
    from app.main import app

    response = app.openapi()["paths"]["/api/v1/admin/inquiries/{inquiry_id}/answers"][
        "post"
    ]["responses"]["400"]
    assert "CUSTOMER_CONTENT_UNSAFE" in str(response)
