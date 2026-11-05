"""7·8단계 병합의 발송 차단과 기존 GitHub 마이그레이션 데이터 보존."""

import pytest
from sqlalchemy import select, text

from app.db.models import GitHubIntegration, GitHubJob, GitHubSource, Tenant
from test_documents import rag  # noqa: F401
from test_pipeline import ai  # noqa: F401
from test_automatic_responses import auto_case, assert_review  # noqa: F401

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("status", ["동기화 중", "동기화 실패"])
@pytest.mark.parametrize("previous_binding", [False, True])
async def test_automatic_answer_waits_for_bound_github_sync(
    client, agent, session_factory, auto_case, status, previous_binding
):
    # 동기화 접수 이후 생성된 분석은 현재 변경번호와 같을 수 있다.
    # 기존 연결이 제거된 경우도 pending_bindings의 차단 확인이 끝나야 한다.
    binding = [{"path": "billing/", "doc_id": "DOC-001"}]
    async with session_factory() as session:
        session.add(
            GitHubIntegration(
                integration_id="github_alpha",
                tenant_id="alpha",
                status=status,
                document_bindings=[] if previous_binding else binding,
                pending_bindings=binding if previous_binding else [],
            )
        )
        await session.commit()
    await assert_review(
        client, agent, session_factory, auto_case, "GITHUB_SYNC_PENDING"
    )


async def test_released_github_branch_upgrade_and_downgrade_preserve_sources(
    db_engine, session_factory, migrate
):
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "base")
        await connection.run_sync(migrate, "upgrade", "0011_github")
    async with session_factory() as session:
        session.add(Tenant(tenant_id="alpha", name="Alpha"))
        await session.flush()
        session.add(GitHubIntegration(integration_id="github_alpha", tenant_id="alpha"))
        await session.flush()
        session.add(GitHubJob(integration_id="github_alpha", generation=1))
        session.add(
            GitHubSource(
                integration_id="github_alpha",
                source_id="github:22:code:path",
                version="a" * 40,
                path="billing/pricing.ts",
                blob_sha="b" * 40,
                payload={"text": "보존할 마스킹된 원본"},
            )
        )
        await session.commit()
    tables = ["tenants", "github_integrations", "github_jobs", "github_sources"]
    async with db_engine.begin() as connection:
        before = {
            table: (
                await connection.execute(text(f"SELECT * FROM {table} ORDER BY 1"))
            ).all()
            for table in tables
        }
        await connection.run_sync(migrate, "upgrade", "head")
        assert (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalars().all() == ["0014_inquiry_activity_merge"]
        for table in tables:
            assert (
                await connection.execute(text(f"SELECT * FROM {table} ORDER BY 1"))
            ).all() == before[table]
        assert (
            await connection.scalar(text("SELECT count(*) FROM automatic_responses"))
            == 0
        )
        await connection.run_sync(migrate, "downgrade", "0011_github")
        for table in tables:
            assert (
                await connection.execute(text(f"SELECT * FROM {table} ORDER BY 1"))
            ).all() == before[table]
