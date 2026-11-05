import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from app.core.enums import InquiryCategory, InquiryStatus, InquiryUrgency
from app.db.models import (
    Alert,
    AlertRead,
    GroupMembershipHistory,
    Inquiry,
    InquiryGroup,
    InquirySummaryJob,
    IssueJob,
    SurgeEpisode,
)
from app.schemas.analysis import SimilarInquiries
from app.services.groups import assign_group, evaluate, lock_groups, utcnow
from app.services.alerts import notify

pytestmark = pytest.mark.anyio
BASE = "/api/v1/admin"


def result(
    *, code="E-502", category="기능 오류", similar=None, summary="저장 오류", run=None
):
    return NS(
        run_id=run or "RUN-" + uuid4().hex,
        analysis=NS(
            normalized_error_code=code,
            category=InquiryCategory(category),
            summary=summary,
        ),
        similar_inquiries=similar,
    )


async def add_inquiry(
    factory,
    actor,
    *,
    code="E-502",
    category="기능 오류",
    env=None,
    received=None,
    seed=False,
    similar=None,
    summary="저장 오류",
):
    async with factory() as session:
        inquiry = Inquiry(
            tenant_id=actor["tenant_id"],
            customer_id=actor["internal_id"],
            title="민감한 고객 원문 secret@example.com",
            customer_category=InquiryCategory(category),
            category=InquiryCategory(category),
            urgency=InquiryUrgency.NORMAL,
            env=env or {},
            status=InquiryStatus.RECEIVED,
            received_at=received or utcnow(),
            is_seed=seed,
        )
        session.add(inquiry)
        await session.flush()
        group = await assign_group(
            session,
            inquiry,
            result(code=code, category=category, similar=similar, summary=summary),
        )
        await session.commit()
        return inquiry, group


async def test_demo_counts_and_issue_snapshot(client, customer, agent, session_factory):
    for code, count in (("E-502", 16), ("E-413", 4), ("E-401", 4)):
        for _ in range(count):
            await add_inquiry(
                session_factory,
                customer,
                code=code,
                env={"os": "iOS 18", "app_version": "2.4.0"},
            )
    response = await client.get(BASE + "/groups?surging=true", headers=agent["headers"])
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert len(rows) == 1 and rows[0]["count"] == 16 and rows[0]["group_id"] == "G-001"
    assert (
        rows[0]["issue_creation_status"] == "대기"
        and rows[0]["issue_error_code"] == "GITHUB_INTEGRATION_NOT_READY"
    )
    async with session_factory() as session:
        job = await session.scalar(select(IssueJob))
        assert "문의 8건" in job.payload["title"]
        assert "secret@example.com" not in str(job.payload)
        assert job.payload["labels"] == ["bug", "moa-auto"]
        assert await session.scalar(select(func.count()).select_from(SurgeEpisode)) == 1
        assert (
            await session.scalar(
                select(func.count()).select_from(Alert).where(Alert.type == "문의 급증")
            )
            == 1
        )
    detail = (
        await client.get(BASE + "/groups/G-001?limit=3", headers=agent["headers"])
    ).json()
    assert len(detail["inquiry_ids"]) == 3 and detail["has_more"]
    assert (
        detail["normalized_error_codes"] == ["E-502"] and detail["suspected_code"] == []
    )
    assert (
        len(
            (
                await client.get(
                    BASE + "/groups/G-001",
                    params={"cursor": detail["next_cursor"]},
                    headers=agent["headers"],
                )
            ).json()["inquiry_ids"]
        )
        == 13
    )


async def test_concurrent_group_creation_and_single_episode(customer, session_factory):
    await asyncio.wait_for(
        asyncio.gather(*[add_inquiry(session_factory, customer) for _ in range(12)]),
        timeout=20,
    )
    async with session_factory() as session:
        for model in (InquiryGroup, SurgeEpisode, IssueJob):
            assert await session.scalar(select(func.count()).select_from(model)) == 1
        assert await session.scalar(select(func.count()).select_from(Inquiry)) == 12


async def test_codes_conflict_and_reanalysis_history(customer, session_factory):
    inquiry, group = await add_inquiry(
        session_factory, customer, env={"error_code": "e_502"}
    )
    async with session_factory() as session:
        row = await session.get(Inquiry, inquiry.inquiry_id, with_for_update=True)
        await assign_group(session, row, result(code="E-413", run="RUN-conflict"))
        await session.commit()
        assert row.group_pk is None
        assert (await session.scalar(select(Alert))).error_code == "ERROR_CODE_CONFLICT"
        assert (
            await session.scalar(
                select(func.count()).select_from(GroupMembershipHistory)
            )
            == 2
        )
        job = await session.scalar(
            select(InquirySummaryJob).order_by(InquirySummaryJob.job_id.desc())
        )
        assert job.payload["status"] == "폐기"


@pytest.mark.parametrize(
    "change,joined",
    [
        ("valid", True),
        ("low", False),
        ("foreign", False),
        ("category", False),
        ("code", False),
        ("fake_group", False),
        ("suggested", False),
    ],
)
async def test_similarity_is_verified_against_db(
    customer, foreign_customer, session_factory, change, joined
):
    owner = foreign_customer if change == "foreign" else customer
    first, group = await add_inquiry(
        session_factory,
        owner,
        code="E-502" if change == "code" else None,
        category="계정" if change == "category" else "기능 오류",
    )
    similar = SimilarInquiries(
        items=[
            {
                "inquiry_id": first.public_id,
                "group_id": "G-999" if change == "fake_group" else group.public_id,
                "score": 0.84 if change == "low" else 0.9,
            }
        ],
        suggested_group_id="G-998" if change == "suggested" else group.public_id,
    )
    second, chosen = await add_inquiry(
        session_factory, customer, code=None, similar=similar
    )
    assert (chosen.group_id == group.group_id) is joined


async def test_similarity_tie_and_manual_regroup(
    client, agent, customer, session_factory
):
    a, ga = await add_inquiry(session_factory, customer, code=None)
    b, gb = await add_inquiry(session_factory, customer, code=None)
    similar = SimilarInquiries(
        items=[
            {"inquiry_id": i.public_id, "group_id": g.public_id, "score": 0.9}
            for i, g in ((b, gb), (a, ga))
        ]
    )
    third, chosen = await add_inquiry(
        session_factory, customer, code=None, similar=similar
    )
    assert chosen.group_id == ga.group_id
    response = await client.patch(
        f"{BASE}/inquiries/{third.public_id}/classification",
        headers=agent["headers"],
        json={
            "expected_revision": 1,
            "category": "계정",
            "urgency": "보통",
            "reason": "담당자 판단",
        },
    )
    assert response.status_code == 200, response.text
    async with session_factory() as session:
        row = await session.get(Inquiry, third.inquiry_id)
        assert row.group_pk not in (ga.group_id, gb.group_id)
        jobs = list(
            (
                await session.scalars(
                    select(InquirySummaryJob)
                    .where(InquirySummaryJob.inquiry_id == row.inquiry_id)
                    .order_by(InquirySummaryJob.source_revision)
                )
            ).all()
        )
        assert (
            jobs[-1].version == jobs[0].version
            and jobs[-1].source_revision > jobs[0].source_revision
        )
        assert jobs[-1].payload["metadata"]["category"] == "계정"


async def test_window_boundaries_seeds_late_analysis_and_episode_restart(
    customer, session_factory, monkeypatch
):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr("app.services.groups.utcnow", lambda: now)
    for _ in range(7):
        _, group = await add_inquiry(session_factory, customer, received=now)
    for received, seed in (
        (now - timedelta(minutes=5), False),
        (now, True),
        (now - timedelta(days=1), False),
    ):
        await add_inquiry(session_factory, customer, received=received, seed=seed)
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(SurgeEpisode)) == 0
    await add_inquiry(
        session_factory,
        customer,
        received=now - timedelta(minutes=5) + timedelta(microseconds=1),
    )
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(SurgeEpisode)) == 1
        await lock_groups(s, customer["tenant_id"])
        group = await s.get(InquiryGroup, group.group_id)
        active = await evaluate(s, group, now + timedelta(minutes=6))
        assert active and not active.ended_at
        assert await evaluate(s, group, now + timedelta(minutes=10)) is None
        await s.commit()
    now += timedelta(minutes=11)
    for _ in range(8):
        await add_inquiry(session_factory, customer, received=now)
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(SurgeEpisode)) == 2
        assert await s.scalar(select(func.count()).select_from(IssueJob)) == 2


async def test_restart_closes_quiet_gap_before_new_burst(
    customer, session_factory, monkeypatch
):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr("app.services.groups.utcnow", lambda: now)
    for _ in range(8):
        await add_inquiry(session_factory, customer, received=now)
    now += timedelta(minutes=11)
    for _ in range(8):
        await add_inquiry(session_factory, customer, received=now)
    async with session_factory() as s:
        episodes = list(
            (
                await s.scalars(select(SurgeEpisode).order_by(SurgeEpisode.episode_pk))
            ).all()
        )
        assert len(episodes) == 2 and episodes[0].ended_at == now - timedelta(minutes=1)


async def test_non_bug_surge_has_no_automatic_issue(customer, session_factory):
    for _ in range(8):
        await add_inquiry(session_factory, customer, code="E-401", category="계정")
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(SurgeEpisode)) == 1
        assert await s.scalar(select(func.count()).select_from(IssueJob)) == 0


async def test_alert_reads_are_personal_idempotent_and_isolated(
    client, agent, customer, foreign_agent, make_actor, session_factory
):
    second = await make_actor("secondagent", "agent")

    async def emit():
        async with session_factory() as s:
            alert = await notify(
                s,
                tenant="alpha",
                cause="same",
                kind="분석 실패",
                code="AI_WORKER_ERROR",
            )
            await s.commit()
            return alert.public_id

    ids = await asyncio.gather(emit(), emit())
    assert ids[0] == ids[1]
    path = BASE + "/alerts/" + ids[0]
    first = await client.patch(path, headers=agent["headers"], json={"read": True})
    again = await client.patch(path, headers=agent["headers"], json={"read": True})
    assert first.status_code == 200 and first.json() == again.json()
    assert (await client.get(BASE + "/alerts", headers=agent["headers"])).json()[
        "items"
    ] == []
    assert (
        len(
            (await client.get(BASE + "/alerts", headers=second["headers"])).json()[
                "items"
            ]
        )
        == 1
    )
    assert (
        await client.patch(path, headers=foreign_agent["headers"], json={"read": True})
    ).status_code == 404
    assert (
        await client.patch(path, headers=customer["headers"], json={"read": True})
    ).status_code == 403
    for invalid in (False, 1, "true"):
        assert (
            await client.patch(path, headers=agent["headers"], json={"read": invalid})
        ).status_code == 400


async def test_group_auth_cursor_and_input_validation(
    client, agent, customer, foreign_agent, make_actor, session_factory
):
    for code in ("E-502", "E-413", "E-401"):
        await add_inquiry(session_factory, customer, code=code)
    for path in ("/groups", "/groups/G-001", "/alerts"):
        assert (await client.get(BASE + path)).status_code == 401
        assert (
            await client.get(BASE + path, headers=customer["headers"])
        ).status_code == 403
    assert (
        await client.get(BASE + "/groups/G-001", headers=foreign_agent["headers"])
    ).status_code == 404
    page = (await client.get(BASE + "/groups?limit=1", headers=agent["headers"])).json()
    for params, actor in (
        ({"cursor": page["next_cursor"], "surging": True}, agent),
        ({"cursor": page["next_cursor"]}, foreign_agent),
    ):
        assert (
            await client.get(BASE + "/groups", params=params, headers=actor["headers"])
        ).status_code == 400
    for path in (
        "/groups/1",
        "/groups?limit=101",
        "/groups?cursor=bogus",
        "/alerts?limit=0",
    ):
        assert (
            await client.get(BASE + path, headers=agent["headers"])
        ).status_code == 400


async def test_group_id_exhaustion_rolls_back(customer, session_factory):
    from app.core.ids import IDSpaceExhausted

    async with session_factory() as s:
        await s.execute(text("SELECT setval('public_group_seq', 999)"))
        await s.commit()
    with pytest.raises(IDSpaceExhausted):
        await add_inquiry(session_factory, customer)
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Inquiry)) == 0


async def test_group_migration_preserves_existing_received_time(
    db_engine, migrate, customer, session_factory
):
    row, _ = await add_inquiry(session_factory, customer)
    async with db_engine.begin() as c:
        before = (
            await c.execute(
                text(
                    "SELECT public_id,customer_id,title,env,created_at,updated_at FROM inquiries"
                )
            )
        ).all()
        await c.run_sync(migrate, "downgrade", "0011_answers_automatic")
        assert (
            await c.execute(
                text(
                    "SELECT public_id,customer_id,title,env,created_at,updated_at FROM inquiries"
                )
            )
        ).all() == before
        await c.run_sync(migrate, "upgrade", "head")
        assert (
            await c.execute(
                text(
                    "SELECT public_id,customer_id,title,env,created_at,updated_at FROM inquiries"
                )
            )
        ).all() == before
        assert await c.scalar(
            text(
                "SELECT received_at=created_at AND group_pk IS NULL AND NOT is_seed FROM inquiries"
            )
        )


async def test_alert_cursor_bound_to_user_filter_and_read_keeps_order(
    client, agent, make_actor, session_factory
):
    second = await make_actor("cursoragent", "agent")
    async with session_factory() as s:
        for n in range(3):
            await notify(s, tenant="alpha", cause=f"alert-page:{n}", kind="분석 실패")
        await s.commit()
    first = (
        await client.get(BASE + "/alerts?limit=1", headers=agent["headers"])
    ).json()
    cursor = first["next_cursor"]
    for headers, query in (
        (second["headers"], {}),
        (agent["headers"], {"unread_only": False}),
    ):
        assert (
            await client.get(
                BASE + "/alerts", params={"cursor": cursor, **query}, headers=headers
            )
        ).status_code == 400
    await client.patch(
        BASE + "/alerts/" + first["items"][0]["alert_id"],
        headers=agent["headers"],
        json={"read": True},
    )
    rest = (
        await client.get(
            BASE + "/alerts", params={"cursor": cursor}, headers=agent["headers"]
        )
    ).json()
    assert len(rest["items"]) == 2


async def test_alert_id_exhaustion_rolls_back(session_factory, customer):
    from app.core.ids import IDSpaceExhausted

    async with session_factory() as s:
        await s.execute(text("SELECT setval('public_alert_seq',999999)"))
        await s.commit()
    with pytest.raises(IDSpaceExhausted):
        async with session_factory() as s:
            await notify(s, tenant="alpha", cause="overflow", kind="분석 실패")
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Alert)) == 0


@pytest.mark.parametrize(
    "source,expected",
    [
        ("E502", "E-502"),
        ("e 502", "E-502"),
        ("E-SYNC-502", "E-SYNC-502"),
        ("e sync 502", "E-SYNC-502"),
        ("e_502", "E-502"),
        ("private@example.com", None),
    ],
)
async def test_normalization_matches_ai(source, expected):
    from app.services.groups import normalize_code

    assert normalize_code(source) == expected


async def test_reanalysis_without_eligible_candidate_gets_new_group(
    customer, session_factory
):
    row, group = await add_inquiry(session_factory, customer, code=None)
    async with session_factory() as s:
        inquiry = await s.get(Inquiry, row.inquiry_id, with_for_update=True)
        chosen = await assign_group(s, inquiry, result(code=None))
        await s.commit()
        assert chosen.group_id != group.group_id
        assert (
            await s.scalar(select(func.count()).select_from(GroupMembershipHistory))
            == 2
        )
