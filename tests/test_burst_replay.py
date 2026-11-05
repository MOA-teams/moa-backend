from copy import deepcopy
from functools import partial
import json

import anyio
import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from scripts.burst_replay import ReplayError, load_plan, main, normalize_base, replay
from app.db.models import AnalysisRequest, Inquiry, Message


@pytest.fixture
def seed_file(tmp_path):
    rows = [
        {
            "burst_id": f"B-{i:03d}",
            "offset_seconds": offset,
            "customer_id": f"U-{1900 + i}",
            "title": "합성 문의",
            "message": "저장 문제가 있습니다.",
            "customer_category": "기능 오류",
            "env": {"os": "iOS 18", "app_version": "2.4.0", "error_code": code},
            "attachments": [],
        }
        for i, offset, code in [(1, 0, "E-502"), (2, 300, None), (3, 600, "E-413")]
    ]
    path = tmp_path / "burst_inquiries.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return path


def test_source_ids_are_metadata_and_no_code_is_inferred_from_labels(seed_file):
    plan = load_plan(seed_file)
    assert plan[1]["body"]["env"]["error_code"] is None
    for row in plan:
        assert set(row["body"]) == {
            "message",
            "source_customer_id",
            "customer_category",
            "env",
            "attachments",
        }
        assert row["body"]["source_customer_id"].startswith("U-")
        assert row["body"]["env"]["os"] == "iOS 18"


@pytest.mark.parametrize(
    "change", ["labels", "extra", "attachment", "duplicate", "invalid_env"]
)
def test_unexpected_seed_fields_fail_before_http(seed_file, change):
    rows = json.loads(seed_file.read_text())
    if change == "labels":
        new = seed_file.parent / "labels" / seed_file.name
        new.parent.mkdir()
        seed_file = new
    elif change == "extra":
        rows[0]["group_id"] = "G-001"
    elif change == "attachment":
        rows[0]["attachments"] = ["ATT-0001"]
    elif change == "duplicate":
        rows.append(rows[0])
    else:
        rows[0]["env"]["error_code"] = "x" * 101
    seed_file.write_text(json.dumps(rows))
    with pytest.raises((ReplayError, ValidationError)):
        load_plan(seed_file)


def test_dry_run_needs_no_auth_and_writes_no_state(seed_file, tmp_path, capsys):
    state = tmp_path / "state.json"
    assert main(["--seed-file", str(seed_file), "--state", str(state)]) == 0
    assert "DRY 3건" in capsys.readouterr().out
    assert not state.exists() and not state.with_suffix(".json.lock").exists()


@pytest.mark.parametrize(
    "base",
    [
        "https://user:password@host",
        "https://host/api/v1",
        "https://host?secret=x",
        "file:///tmp/x",
    ],
)
def test_invalid_api_base_is_rejected(base):
    with pytest.raises(ReplayError, match="INVALID_API_BASE_URL"):
        normalize_base(base)


def test_schedule_resume_and_changed_input_rejection(seed_file, tmp_path):
    plan = load_plan(seed_file)
    state = tmp_path / "state.json"
    now, posts = [0.0], []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"user_id": "U-2001", "tenant_id": "syncnote", "role": "customer"},
            )
        posts.append((now[0], request.headers["Idempotency-Key"]))
        return httpx.Response(201, json={"inquiry_id": f"Q-{1000 + len(posts)}"})

    def sleep(seconds):
        now[0] += seconds

    with httpx.Client(
        base_url="https://demo.test", transport=httpx.MockTransport(handler)
    ) as client:
        kwargs = dict(
            base="https://demo.test",
            tenant="syncnote",
            state_path=state,
            duration=120,
            sleep=sleep,
            clock=lambda: now[0],
        )
        replay(client, plan, **kwargs)
        assert [t for t, _ in posts] == [0, 60, 120]
        replay(client, plan, **kwargs)
        assert len(posts) == 3
        changed = deepcopy(plan)
        changed[0]["body"]["message"] = "바뀐 내용"
        with pytest.raises(ReplayError, match="REPLAY_STATE_MISMATCH"):
            replay(client, changed, **kwargs)
        assert len(posts) == 3


@pytest.mark.parametrize(
    "actor",
    [
        {"user_id": "agent_01", "tenant_id": "syncnote", "role": "agent"},
        {"user_id": "U-2001", "tenant_id": "other", "role": "customer"},
        {"user_id": "U-2001", "tenant_id": None, "role": "customer"},
    ],
)
def test_wrong_actor_never_creates_inquiry_or_run_state(seed_file, tmp_path, actor):
    state = tmp_path / "state.json"

    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json=actor)

    with httpx.Client(
        base_url="https://demo.test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ReplayError, match="CUSTOMER_TENANT_MISMATCH"):
            replay(
                client,
                load_plan(seed_file),
                base="https://demo.test",
                tenant="syncnote",
                state_path=state,
                duration=0,
            )
    assert not state.exists()


@pytest.mark.anyio
async def test_lost_api_response_resumes_once_with_real_postgres_contract(
    client,
    customer,
    session_factory,
    seed_file,
    tmp_path,
):
    plan, state = load_plan(seed_file), tmp_path / "state.json"

    class Bridge:
        lose_response = True
        keys = []

        def get(self, path):
            return anyio.from_thread.run(
                partial(client.get, path, headers=customer["headers"])
            )

        def post(self, path, *, json, headers):
            self.keys.append(headers["Idempotency-Key"])
            response = anyio.from_thread.run(
                partial(
                    client.post,
                    path,
                    json=json,
                    headers={**customer["headers"], **headers},
                )
            )
            assert response.status_code == 201, response.text
            if self.lose_response:
                self.lose_response = False
                raise httpx.ReadTimeout("response lost after commit")
            return response

    bridge = Bridge()
    run = partial(
        replay,
        bridge,
        plan,
        base="http://test",
        tenant="alpha",
        state_path=state,
        duration=0,
    )
    with pytest.raises(httpx.ReadTimeout):
        await anyio.to_thread.run_sync(run)
    assert json.loads(state.read_text())["results"] == {}
    await anyio.to_thread.run_sync(run)
    assert bridge.keys[0] == bridge.keys[1]
    saved = json.loads(state.read_text())
    assert len(saved["results"]) == 3
    assert "Bearer" not in state.read_text() and "저장" not in state.read_text()
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Inquiry)) == 3
        assert await session.scalar(select(func.count()).select_from(Message)) == 6
        assert (
            await session.scalar(select(func.count()).select_from(AnalysisRequest)) == 3
        )
        rows = list(
            (await session.scalars(select(Inquiry).order_by(Inquiry.inquiry_id))).all()
        )
        assert all(
            row.customer_id == customer["internal_id"] and row.tenant_id == "alpha"
            for row in rows
        )
        assert [r.source_customer_id for r in rows] == [
            p["body"]["source_customer_id"] for p in plan
        ]
        assert [r.env for r in rows] == [p["body"]["env"] for p in plan]
        assert all(not row.is_seed for row in rows)
