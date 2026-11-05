from conftest import get_row
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from app.core.enums import UserRole
from app.db.base import Base
from app.db.models import User

pytestmark = pytest.mark.anyio


async def test_server_timestamps_and_no_update_on_read(session_factory):
    async with session_factory() as session:
        user = User(email="timestamp@example.com", password="test-only", role=UserRole.CUSTOMER)
        session.add(user)
        await session.commit()
        user_id = user.user_id
        created, updated = user.created_at, user.updated_at
        assert created.utcoffset() is not None and updated.utcoffset() is not None
        assert created == updated
    async with session_factory() as session:
        user = await get_row(session, User, user_id)
        user.email = user.email
        await session.commit()
        await session.refresh(user)
        assert user.created_at == created and user.updated_at == updated


async def test_orm_update_preserves_created_at_and_advances_updated_at(session_factory):
    async with session_factory() as session:
        user = User(email="before@example.com", password="test-only", role=UserRole.AGENT)
        session.add(user)
        await session.commit()
        user_id = user.user_id
        created, updated = user.created_at, user.updated_at
    async with session_factory() as session:
        user = await get_row(session, User, user_id)
        user.email = "after@example.com"
        await session.commit()
        await session.refresh(user)
        assert user.created_at == created
        assert user.updated_at > updated


async def test_raw_sql_has_no_implicit_timestamp_trigger(session_factory):
    async with session_factory() as session:
        user = User(email="raw@example.com", password="test-only", role=UserRole.ADMIN)
        session.add(user)
        await session.commit()
        user_id, updated = user.user_id, user.updated_at
    async with session_factory() as session:
        await session.execute(text("UPDATE users SET email=:email WHERE user_id=:user_id"), {"email": "raw-changed@example.com", "user_id": user_id})
        await session.commit()
        assert (await get_row(session, User, user_id)).updated_at == updated


async def test_migration_matches_models_and_supports_downgrade(db_engine, migrate):
    async with db_engine.begin() as connection:
        differences = await connection.run_sync(lambda conn: compare_metadata(MigrationContext.configure(conn), Base.metadata))
        assert differences == []
        tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
        assert set(tables) == {"users", "refresh_tokens", "tenants", "inquiries", "messages", "attachments", "idempotency_records", "analysis_requests", "analysis_runs", "analysis_drafts", "classification_history", "alembic_version", "documents", "document_versions", "document_counters", "document_jobs", "inquiry_session_events"}
        columns = await connection.run_sync(lambda conn: inspect(conn).get_columns("users"))
        identity = next(column for column in columns if column["name"] == "user_id")
        assert identity["identity"]["start"] == 1
        assert next(column for column in columns if column["name"] == "created_at")["type"].timezone
        labels = (await connection.execute(text("SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname=current_schema() AND t.typname='user_role' ORDER BY e.enumsortorder"))).scalars().all()
        assert labels == ["customer", "agent", "admin"]
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "base")
        assert not await connection.run_sync(lambda conn: inspect(conn).has_table("users"))
        assert not await connection.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname=current_schema() AND t.typname='user_role')"))
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "upgrade", "head")
        assert await connection.run_sync(lambda conn: inspect(conn).has_table("users"))


async def test_refresh_migration_preserves_existing_users(db_engine, migrate):
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "0001_create_users")
        await connection.execute(text("INSERT INTO users(email,password,role) VALUES ('preserved@example.com','test-only','agent')"))
        before = (await connection.execute(text("SELECT user_id,email,password,role,created_at,updated_at FROM users"))).one()
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "upgrade", "head")
        after = (await connection.execute(text("SELECT user_id,email,password,role,created_at,updated_at FROM users"))).one()
        assert before == after
        assert await connection.run_sync(lambda conn: inspect(conn).has_table("refresh_tokens"))
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "0001_create_users")
        assert not await connection.run_sync(lambda conn: inspect(conn).has_table("refresh_tokens"))
        assert (await connection.execute(text("SELECT user_id,email,password,role,created_at,updated_at FROM users"))).one() == before


async def test_tenant_migration_preserves_accounts_and_tokens(db_engine, migrate):
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "0002_create_refresh_tokens")
        await connection.execute(text("INSERT INTO users(email,password,role) VALUES ('tenant-preserve@example.com','test-only','admin')"))
        await connection.execute(text("INSERT INTO refresh_tokens(user_id,token_hash,expires_at) VALUES (1,:token_hash,now() + interval '7 days')"), {"token_hash": "a" * 64})
        users_before = (await connection.execute(text("SELECT user_id,email,password,role,created_at,updated_at FROM users"))).all()
        tokens_before = (await connection.execute(text("SELECT * FROM refresh_tokens"))).all()
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "upgrade", "head")
        assert (await connection.execute(text("SELECT user_id,email,password,role,created_at,updated_at FROM users"))).all() == users_before
        assert (await connection.execute(text("SELECT * FROM refresh_tokens"))).all() == tokens_before
        assert await connection.scalar(text("SELECT tenant_id FROM users")) is None
        assert await connection.scalar(text("SELECT count(*) FROM tenants")) == 0
        await connection.execute(text("INSERT INTO tenants(tenant_id,name) VALUES ('syncnote','싱크노트')"))
        await connection.execute(text("UPDATE users SET tenant_id='syncnote'"))
        # 소속 연결 전후 보존 검사는 명시적으로 선택한 기본 계정 필드를 대상으로 한다.
        linked_users = (await connection.execute(text("SELECT user_id,email,password,role,created_at,updated_at FROM users"))).all()
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "0002_create_refresh_tokens")
        assert not await connection.run_sync(lambda conn: inspect(conn).has_table("tenants"))
        assert (await connection.execute(text("SELECT user_id,email,password,role,created_at,updated_at FROM users"))).all() == linked_users
        assert (await connection.execute(text("SELECT * FROM refresh_tokens"))).all() == tokens_before


async def test_inquiry_migration_preserves_auth_data_and_removes_only_new_schema(db_engine, migrate):
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "0003_create_tenants")
        await connection.execute(text("INSERT INTO tenants(tenant_id,name) VALUES ('alpha','Alpha')"))
        await connection.execute(text("INSERT INTO users(email,password,role,tenant_id) VALUES ('phase3@example.com','test-only','customer','alpha')"))
        await connection.execute(text("INSERT INTO refresh_tokens(user_id,token_hash,expires_at) VALUES (1,:hash,now() + interval '7 days')"), {"hash": "b" * 64})
        users = (await connection.execute(text("SELECT user_id,email,password,role,tenant_id,created_at,updated_at FROM users"))).all()
        tenants = (await connection.execute(text("SELECT * FROM tenants"))).all()
        tokens = (await connection.execute(text("SELECT * FROM refresh_tokens"))).all()
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "upgrade", "head")
        assert (await connection.execute(text("SELECT user_id,email,password,role,tenant_id,created_at,updated_at FROM users"))).all() == users
        assert (await connection.execute(text("SELECT * FROM tenants"))).all() == tenants
        assert (await connection.execute(text("SELECT * FROM refresh_tokens"))).all() == tokens
        for enum, expected in {
            "inquiry_category": ["이용 방법", "계정", "결제", "기능 오류", "개선 요청", "기타"],
            "inquiry_urgency": ["높음", "보통", "낮음"],
            "inquiry_status": ["접수", "담당자 확인 중", "추가 정보 필요", "답변 완료", "상담 종료"],
            "message_sender": ["customer", "agent", "ai_auto"],
            "message_type": ["고객 메시지", "접수 안내", "정보 수집 질문", "최종 답변"],
        }.items():
            labels = (await connection.execute(text("SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname=current_schema() AND t.typname=:name ORDER BY e.enumsortorder"), {"name": enum})).scalars().all()
            assert labels == expected
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "0003_create_tenants")
        tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
        assert set(tables) == {"users", "refresh_tokens", "tenants", "alembic_version"}
        assert (await connection.execute(text("SELECT user_id,email,password,role,tenant_id,created_at,updated_at FROM users"))).all() == users
        assert (await connection.execute(text("SELECT * FROM refresh_tokens"))).all() == tokens
        assert not await connection.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname=current_schema() AND t.typname='inquiry_status')"))
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "upgrade", "head")


async def test_source_customer_migration_preserves_existing_inquiry_and_ownership(db_engine, migrate):
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "0004_create_inquiries")
        await connection.execute(text("INSERT INTO tenants(tenant_id,name) VALUES ('syncnote','싱크노트')"))
        await connection.execute(text("INSERT INTO users(email,password,role,tenant_id) VALUES ('source@example.com','test-only','customer','syncnote')"))
        await connection.execute(text("INSERT INTO inquiries(tenant_id,customer_id,title,customer_category,env,status,revision,last_message_sequence) VALUES ('syncnote',1,'기존 문의','bug','{\"error_code\":\"E-502\"}'::jsonb,'received',1,0)"))
        before = (await connection.execute(text("SELECT inquiry_id,tenant_id,customer_id,title,env,created_at,updated_at FROM inquiries"))).one()
        user_before = (await connection.execute(text("SELECT user_id,email,password,role,tenant_id,created_at,updated_at FROM users"))).one()
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "upgrade", "head")
        assert (await connection.execute(text("SELECT inquiry_id,tenant_id,customer_id,title,env,created_at,updated_at FROM inquiries"))).one() == before
        assert await connection.scalar(text("SELECT source_customer_id FROM inquiries")) is None
        columns = await connection.run_sync(lambda conn: inspect(conn).get_columns("inquiries"))
        external = next(column for column in columns if column["name"] == "source_customer_id")
        assert external["nullable"] and external["type"].length == 128
        await connection.execute(text("UPDATE inquiries SET source_customer_id='U-2001'"))
        assert (await connection.execute(text("SELECT user_id,email,password,role,tenant_id,created_at,updated_at FROM users"))).one() == user_before
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "0004_create_inquiries")
        assert (await connection.execute(text("SELECT inquiry_id,tenant_id,customer_id,title,env,created_at,updated_at FROM inquiries"))).one() == before
        columns = await connection.run_sync(lambda conn: inspect(conn).get_columns("inquiries"))
        assert not any(column["name"] == "source_customer_id" for column in columns)
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "upgrade", "head")
        assert (await connection.execute(text("SELECT user_id,email,password,role,tenant_id,created_at,updated_at FROM users"))).one() == user_before


async def test_document_migration_preserves_existing_attachments(db_engine, migrate):
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "0005_add_source_customer_id")
        await connection.execute(text("INSERT INTO tenants(tenant_id,name) VALUES ('alpha','Alpha')"))
        await connection.execute(text("INSERT INTO users(email,password,role,tenant_id) VALUES ('document-migration@example.com','test-only','customer','alpha')"))
        await connection.execute(text("INSERT INTO attachments(tenant_id,uploader_id,storage_key,file_name,mime,size) VALUES ('alpha',1,'test/key','old.pdf','application/pdf',10)"))
        before = (await connection.execute(text("SELECT attachment_id,tenant_id,uploader_id,storage_key,created_at FROM attachments"))).one()
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "upgrade", "head")
        assert await connection.scalar(text("SELECT purpose FROM attachments")) == "inquiry"
        assert (await connection.execute(text("SELECT attachment_id,tenant_id,uploader_id,storage_key,created_at FROM attachments"))).one() == before
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "downgrade", "0005_add_source_customer_id")
        assert (await connection.execute(text("SELECT attachment_id,tenant_id,uploader_id,storage_key,created_at FROM attachments"))).one() == before
        assert not await connection.run_sync(lambda conn: inspect(conn).has_table("documents"))
    async with db_engine.begin() as connection:
        await connection.run_sync(migrate, "upgrade", "head")
