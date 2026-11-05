import asyncio

import pytest

from app.core.config import get_settings
from app.core.auto_response import AutoResponsePolicy
from app.services.response_settings import resolve_policy

pytestmark = pytest.mark.anyio
PATH = "/api/v1/admin/settings/auto-response"


@pytest.fixture
async def admin(make_actor):
    return await make_actor("policyadmin", "admin")


async def test_read_and_save_persist_independent_switches(
    client, admin, session_factory
):
    initial = await client.get(PATH, headers=admin["headers"])
    assert initial.status_code == 200
    assert initial.json()["revision"] == 0
    response = await client.patch(
        PATH,
        headers=admin["headers"],
        json={
            "expected_revision": 0,
            "questions_enabled": True,
            "answers_enabled": False,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "database"
    assert response.json()["questions_enabled"] is True
    assert response.json()["answers_enabled"] is False
    assert response.json()["revision"] == 1
    assert (await client.get(PATH, headers=admin["headers"])).json() == response.json()
    async with session_factory() as session:
        policy = await resolve_policy(session, "alpha")
        assert policy.questions_enabled and not policy.answers_enabled
        assert policy.allow_current_public_documents
    updated = await client.patch(
        PATH,
        headers=admin["headers"],
        json={
            "expected_revision": 1,
            "answers_enabled": True,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["questions_enabled"] and updated.json()["answers_enabled"]


async def test_auth_and_roles_are_enforced(client, customer, agent, admin):
    assert (await client.get(PATH)).status_code == 401
    assert (await client.get(PATH, headers=customer["headers"])).status_code == 403
    assert (await client.get(PATH, headers=agent["headers"])).status_code == 200
    body = {"expected_revision": 0, "questions_enabled": True}
    for headers in [customer["headers"], agent["headers"]]:
        assert (await client.patch(PATH, headers=headers, json=body)).status_code == 403


async def test_tenant_is_derived_from_authenticated_account(client, admin, make_actor):
    foreign = await make_actor("otherpolicyadmin", "admin", tenant_id="beta")
    await client.patch(
        PATH,
        headers=admin["headers"],
        json={"expected_revision": 0, "questions_enabled": True},
    )
    other = await client.get(PATH, headers=foreign["headers"])
    assert other.json()["tenant_id"] == "beta" and other.json()["revision"] == 0
    assert (
        await client.patch(
            PATH,
            headers=admin["headers"],
            json={
                "expected_revision": 1,
                "questions_enabled": False,
                "tenant_id": "beta",
            },
        )
    ).status_code == 400
    no_tenant = await make_actor("nopolicytenant", "admin", tenant_id=None)
    assert (await client.get(PATH, headers=no_tenant["headers"])).status_code == 403


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"questions_enabled": None},
        {"answers_enabled": "true"},
        {"max_questions": 0},
        {"max_questions": 4},
    ],
)
async def test_invalid_settings_are_rejected(client, admin, patch):
    result = await client.patch(
        PATH, headers=admin["headers"], json={"expected_revision": 0, **patch}
    )
    assert result.status_code == 400


async def test_revision_prevents_lost_updates(client, admin):
    first = await client.patch(
        PATH,
        headers=admin["headers"],
        json={"expected_revision": 0, "questions_enabled": True},
    )
    assert first.status_code == 200
    results = await asyncio.gather(
        *[
            client.patch(
                PATH,
                headers=admin["headers"],
                json={"expected_revision": 1, "answers_enabled": value},
            )
            for value in [True, False]
        ]
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    assert (await client.get(PATH, headers=admin["headers"])).json()["revision"] == 2


async def test_database_settings_override_environment_and_survive_cache_reset(
    client, admin, session_factory
):
    settings = get_settings()
    settings.auto_response_policies["alpha"] = AutoResponsePolicy(
        questions_enabled=True, answers_enabled=True
    )
    saved = await client.patch(
        PATH,
        headers=admin["headers"],
        json={
            "expected_revision": 0,
            "questions_enabled": False,
            "answers_enabled": False,
        },
    )
    assert saved.status_code == 200
    get_settings.cache_clear()
    async with session_factory() as session:
        policy = await resolve_policy(session, "alpha")
        assert not policy.questions_enabled and not policy.answers_enabled
