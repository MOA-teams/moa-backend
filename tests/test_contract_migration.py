import hashlib
from datetime import datetime, timedelta, timezone
import jwt
import pytest
from sqlalchemy import text
from app.core.config import get_settings

pytestmark = pytest.mark.anyio


async def test_existing_data_and_refresh_survive_contract_migration(
    db_engine, migrate, client
):
    token = "legacy-refresh"
    async with db_engine.begin() as c:
        await c.run_sync(migrate, "downgrade", "0005_add_source_customer_id")
        await c.execute(
            text("INSERT INTO tenants VALUES ('syncnote','싱크노트',now(),now())")
        )
        await c.execute(
            text(
                "INSERT INTO users(email,password,role,tenant_id) VALUES ('old@example.com','unchanged-hash','customer','syncnote')"
            )
        )
        await c.execute(
            text(
                "INSERT INTO refresh_tokens(user_id,token_hash,expires_at) VALUES (1,:hash,now()+interval '1 day')"
            ),
            {"hash": hashlib.sha256(token.encode()).hexdigest()},
        )
        await c.execute(
            text(
                "INSERT INTO inquiries(tenant_id,customer_id,title,customer_category,env,status,revision,last_message_sequence) VALUES ('syncnote',1,'old','bug','{}','received',1,1)"
            )
        )
        await c.execute(
            text(
                "INSERT INTO messages(inquiry_id,sequence,sender,message_type,text) VALUES (1,1,'customer','customer_message','old')"
            )
        )
        await c.execute(
            text(
                "INSERT INTO attachments(tenant_id,uploader_id,message_id,storage_key,file_name,mime,size) VALUES ('syncnote',1,1,'old/key','x.png','image/png',10)"
            )
        )
        await c.execute(
            text(
                "INSERT INTO idempotency_records(user_id,path,key_hash,request_hash,response) VALUES (1,'/inquiries',:key,:hash,'{}')"
            ),
            {"key": hashlib.sha256(b"legacy").hexdigest(), "hash": "a" * 64},
        )
        before = (
            await c.execute(text("SELECT password,created_at,updated_at FROM users"))
        ).one()
    async with db_engine.begin() as c:
        await c.run_sync(migrate, "upgrade", "head")
        assert (
            await c.execute(text("SELECT password,created_at,updated_at FROM users"))
        ).one() == before
        assert await c.scalar(text("SELECT public_id FROM users")) == "U-2001"
        assert await c.scalar(text("SELECT public_id FROM inquiries")) == "Q-1001"
        assert await c.scalar(text("SELECT public_id FROM messages")) == "MSG-000001"
        assert (
            await c.execute(
                text(
                    "SELECT public_id,storage_key,message_id,uploader_id FROM attachments"
                )
            )
        ).one() == ("ATT-0001", "old/key", 1, 1)
        assert await c.scalar(text("SELECT status::text FROM inquiries")) == "접수"
    refreshed = await client.post("/api/v1/auth/refresh", json={"refresh_token": token})
    assert refreshed.status_code == 200
    headers = {"Authorization": "Bearer " + refreshed.json()["access_token"]}
    assert (await client.get("/api/v1/auth/me", headers=headers)).json()[
        "user_id"
    ] == "U-2001"
    replay = await client.post(
        "/api/v1/inquiries",
        headers={**headers, "Idempotency-Key": "legacy"},
        json={"message": "old"},
    )
    assert (
        replay.status_code == 409
        and replay.json()["error"]["code"] == "LEGACY_IDEMPOTENCY_KEY"
    )
    async with db_engine.begin() as c:
        await c.run_sync(migrate, "downgrade", "0005_add_source_customer_id")
        assert (
            await c.execute(text("SELECT password,created_at,updated_at FROM users"))
        ).one() == before
        assert await c.scalar(text("SELECT status::text FROM inquiries")) == "received"
        assert await c.scalar(text("SELECT storage_key FROM attachments")) == "old/key"


async def test_legacy_numeric_token_and_paths_rejected(client, customer):
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(customer["internal_id"]),
            "role": "customer",
            "token_type": "access",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": "old",
        },
        get_settings().jwt_secret_key.get_secret_value(),
        algorithm="HS256",
    )
    assert (
        await client.get(
            "/api/v1/auth/me", headers={"Authorization": "Bearer " + token}
        )
    ).status_code == 401
    assert (
        await client.get("/api/v1/inquiries/1", headers=customer["headers"])
    ).status_code == 400


async def test_id_exhaustion_is_503_and_no_partial_inquiry(
    client, customer, session_factory
):
    async with session_factory() as s:
        await s.execute(text("SELECT setval('public_message_seq',999999,true)"))
        await s.commit()
    response = await client.post(
        "/api/v1/inquiries",
        headers={**customer["headers"], "Idempotency-Key": "full"},
        json={"message": "hi"},
    )
    assert (
        response.status_code == 503
        and response.json()["error"]["code"] == "ID_SPACE_EXHAUSTED"
    )
    async with session_factory() as s:
        assert await s.scalar(text("SELECT count(*) FROM inquiries")) == 0
        assert await s.scalar(text("SELECT count(*) FROM idempotency_records")) == 0
