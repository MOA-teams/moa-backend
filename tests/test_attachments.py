from conftest import get_row
import asyncio
import io
import logging
import threading
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.stub import ANY, Stubber
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import Headers, UploadFile

from app.core.config import get_settings
from app.core.errors import APIError
from app.core.storage import S3Storage, get_s3_storage
from app.db.models import Attachment, Inquiry, Message
from app.main import app
from app.schemas.auth import AuthenticatedUser
from app.schemas.inquiries import InquiryCreateRequest
from app.services.attachments import MAX_FILE_SIZE, upload_attachment
from app.services.inquiries import create_inquiry

pytestmark = pytest.mark.anyio
BASE = "/api/v1/attachments"
PNG = b"\x89PNG\r\n\x1a\n" + b"test-content"


async def upload(client, actor, data=PNG, name="error.png", mime="image/png"):
    return await client.post(BASE, headers=actor["headers"], files={"file": (name, data, mime)})


async def test_upload_and_refresh_download_url_keep_only_key_in_db(client, customer, fake_storage, session_factory):
    before = datetime.now(timezone.utc)
    response = await upload(client, customer)
    assert response.status_code == 201
    body = response.json()
    assert body["attachment_id"] == "ATT-0001"
    assert body["mime"] == "image/png" and body["size"] == len(PNG) and body["file_name"] == "error.png"
    expiry = datetime.fromisoformat(body["expires_at"])
    assert 299 <= (expiry - before).total_seconds() <= 305
    assert not {"storage_key", "uploader_id", "tenant_id"} & set(body)
    async with session_factory() as session:
        row = await get_row(session, Attachment, body["attachment_id"])
        assert row.storage_key.startswith(f"attachments/alpha/{customer['internal_id']}/")
        assert row.storage_key.endswith(".png") and "error.png" not in row.storage_key
        assert row.message_id is None
        assert fake_storage.objects[row.storage_key] == PNG
    refreshed = await client.get(f"{BASE}/{body['attachment_id']}", headers=customer["headers"])
    assert refreshed.status_code == 200 and refreshed.json()["url"].startswith("https://")


@pytest.mark.parametrize("name,mime,data,expected", [
    ("x.png", "image/png", b"", 400),
    ("../x.png", "image/png", PNG, 400),
    ("x\\x.png", "image/png", PNG, 400),
    ("x.exe", "image/png", PNG, 415),
    ("x.png", "text/plain", PNG, 415),
    ("x.jpg", "image/jpeg", PNG, 415),
    ("x.png", "image/png", b"forged", 415),
    ("x.pdf", "application/pdf", b"%PDF-1.7\ncontent", 201),
    ("x.jpeg", "image/jpeg", b"\xff\xd8\xffcontent", 201),
    ("x.PNG", "image/png", PNG, 201),
])
async def test_file_validation(client, customer, fake_storage, session_factory, name, mime, data, expected):
    response = await upload(client, customer, data, name, mime)
    assert response.status_code == expected
    if expected != 201:
        assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
        assert not fake_storage.calls
        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(Attachment)) == 0


@pytest.mark.parametrize("extra,expected", [(0, 201), (1, 413)])
async def test_size_limit_boundary(client, customer, fake_storage, extra, expected):
    data = PNG + b"x" * (MAX_FILE_SIZE - len(PNG) + extra)
    response = await upload(client, customer, data)
    assert response.status_code == expected


async def test_attachment_ownership_and_staff_access_after_link(client, customer, other_customer, foreign_customer, agent, foreign_agent, make_actor, fake_storage):
    admin = await make_actor("admin", "admin")
    body = (await upload(client, customer)).json()
    path = f"{BASE}/{body['attachment_id']}"
    for actor in (other_customer, foreign_customer, agent, admin, foreign_agent):
        assert (await client.get(path, headers=actor["headers"])).status_code == 404
    response = await client.post("/api/v1/inquiries", headers={**customer["headers"], "Idempotency-Key": "attach"},
                                 json={"message": "첨부 문의", "attachments": [body["attachment_id"]]})
    assert response.status_code == 201
    for actor in (customer, agent, admin):
        assert (await client.get(path, headers=actor["headers"])).status_code == 200
    for actor in (other_customer, foreign_customer, foreign_agent):
        assert (await client.get(path, headers=actor["headers"])).status_code == 404
    chat = (await client.get(f"/api/v1/inquiries/{response.json()['inquiry_id']}", headers=customer["headers"])).json()
    metadata = chat["messages"][0]["attachments"][0]
    assert metadata == {field: body[field] for field in ("attachment_id", "file_name", "mime", "size")}
    assert "url" not in metadata and "storage_key" not in metadata


async def test_cannot_link_another_customers_or_tenants_file(client, customer, other_customer, foreign_customer, fake_storage, session_factory):
    for actor in (other_customer, foreign_customer):
        attachment = (await upload(client, actor)).json()
        response = await client.post("/api/v1/inquiries", headers={**customer["headers"], "Idempotency-Key": actor["role"] + str(actor["user_id"])},
                                     json={"message": "forged", "attachments": [attachment["attachment_id"]]})
        assert response.status_code == 404
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Inquiry)) == 0


async def test_concurrent_linking_has_one_message_owner(client, customer, fake_storage, session_factory):
    attachment = (await upload(client, customer)).json()
    responses = await asyncio.gather(*[
        client.post("/api/v1/inquiries", headers={**customer["headers"], "Idempotency-Key": f"link-{i}"},
                    json={"message": f"문의{i}", "attachments": [attachment["attachment_id"]]}) for i in range(2)
    ])
    assert sorted(response.status_code for response in responses) == [201, 409]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Inquiry)) == 1
        assert (await get_row(session, Attachment, attachment["attachment_id"])).message_id is not None


async def test_message_attachment_link_and_replay(client, customer, inquiry, fake_storage, session_factory):
    body = (await upload(client, customer)).json()
    path = f"/api/v1/inquiries/{inquiry['inquiry_id']}/messages"
    headers = {**customer["headers"], "Idempotency-Key": "file-message"}
    payload = {"text": "화면", "attachments": [body["attachment_id"]]}
    response = await client.post(path, headers=headers, json=payload)
    assert response.status_code == 201
    replay = await client.post(path, headers=headers, json=payload)
    assert replay.status_code == 201 and replay.json() == response.json()
    async with session_factory() as session:
        assert (await get_row(session, Attachment, body["attachment_id"])).message_id == (await get_row(session, Message, response.json()["message_id"])).message_id
    again = await client.post(path, headers={**headers, "Idempotency-Key": "new-key"}, json=payload)
    assert again.status_code == 409


async def test_failed_inquiry_keeps_attachment_unlinked(client, customer, fake_storage, session_factory):
    body = (await upload(client, customer)).json()
    async with session_factory() as session:
        await session.execute(text("ALTER TABLE messages ADD CONSTRAINT test_reject_ack CHECK (message_type <> '접수 안내')"))
        await session.commit()
    async with session_factory() as session:
        user = AuthenticatedUser(user_id=customer["user_id"], internal_user_id=customer["internal_id"], email="customer@example.com", role="customer", tenant_id="alpha")
        with pytest.raises(IntegrityError):
            await create_inquiry(session, user=user, payload=InquiryCreateRequest(message="atomic", attachments=[body["attachment_id"]]), key="retry")
        assert (await get_row(session, Attachment, body["attachment_id"])).message_id is None
        assert await session.scalar(select(func.count()).select_from(Message)) == 0


@pytest.mark.parametrize("failure", ["put", "sign"])
async def test_storage_failure_cleans_object_and_does_not_write_metadata(client, customer, fake_storage, session_factory, failure):
    setattr(fake_storage, f"fail_{failure}", True)
    response = await upload(client, customer)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STORAGE_UNAVAILABLE"
    assert not fake_storage.objects
    assert any(call[0] == "delete" for call in fake_storage.calls)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Attachment)) == 0
    # 첨부 없는 문의는 저장소 장애에도 접수한다.
    response = await client.post("/api/v1/inquiries", headers={**customer["headers"], "Idempotency-Key": "storage-down"}, json={"message": "hi"})
    assert response.status_code == 201


async def test_database_failure_compensates_s3_and_logs_failed_cleanup(client, customer, fake_storage, session_factory, caplog):
    async with session_factory() as session:
        await session.execute(text("ALTER TABLE attachments ADD CONSTRAINT test_reject_attachment CHECK (size < 0)"))
        await session.commit()
    user = AuthenticatedUser(user_id=customer["user_id"], internal_user_id=customer["internal_id"], email="customer@example.com", role="customer", tenant_id="alpha")
    for fail_delete in (False, True):
        fake_storage.fail_delete = fail_delete
        file = UploadFile(io.BytesIO(PNG), filename="x.png", headers=Headers({"content-type": "image/png"}))
        async with session_factory() as session:
            with caplog.at_level(logging.ERROR), pytest.raises(IntegrityError):
                await upload_attachment(session, user=user, file=file, storage=fake_storage, request_id="REQ-cleanup")
            assert await session.scalar(select(func.count()).select_from(Attachment)) == 0
        await file.close()
        if not fail_delete:
            assert not fake_storage.objects
    assert "attachment_cleanup_failed request_id=REQ-cleanup storage_key=attachments/" in caplog.text
    assert "X-Amz-Signature" not in caplog.text and "test-content" not in caplog.text


async def test_upload_roles_missing_file_and_authentication(client, customer, make_actor, fake_storage):
    assert (await client.post(BASE, files={"file": ("x.png", PNG, "image/png")})).status_code == 401
    assert (await client.post(BASE, headers=customer["headers"])).status_code == 400
    for role in ("agent", "admin"):
        actor = await make_actor(role, role)
        assert (await upload(client, actor)).status_code == 403
    actor = await make_actor("unassigned", tenant_id=None)
    assert (await upload(client, actor)).status_code == 403


async def test_missing_storage_configuration_does_not_break_app(client, customer, monkeypatch):
    monkeypatch.setenv("S3_BUCKET_NAME", "")
    monkeypatch.setenv("AWS_REGION", "")
    get_settings.cache_clear()
    get_s3_storage.cache_clear()
    assert (await client.get("/health")).status_code == 200
    response = await upload(client, customer)
    assert response.status_code == 503
    assert (await client.get("/openapi.json")).status_code == 200
    get_s3_storage.cache_clear()


async def test_storage_runs_in_worker_thread(client, customer, fake_storage, monkeypatch):
    loop_thread = threading.get_ident()
    original = fake_storage.put
    threads = []

    def recording(*args):
        threads.append(threading.get_ident())
        return original(*args)
    monkeypatch.setattr(fake_storage, "put", recording)
    assert (await upload(client, customer)).status_code == 201
    assert threads and all(thread != loop_thread for thread in threads)


async def test_s3_adapter_uses_private_put_delete_and_sigv4_url():
    # 실제 HTTP 요청 없이 SDK의 요청 파라미터와 서명 생성까지 확인한다.
    from botocore.config import Config

    client = boto3.client("s3", region_name="ap-northeast-2", aws_access_key_id="test-access-key",
                          aws_secret_access_key="test-secret-key", config=Config(signature_version="s3v4"))
    storage = S3Storage(client, "moa-test-bucket", 300)
    with Stubber(client) as stub:
        stub.add_response("put_object", {}, {"Bucket": "moa-test-bucket", "Key": "attachments/test.png", "Body": ANY, "ContentType": "image/png", "ContentLength": len(PNG)})
        storage.put("attachments/test.png", io.BytesIO(PNG), "image/png", len(PNG))
        stub.add_response("delete_object", {}, {"Bucket": "moa-test-bucket", "Key": "attachments/test.png"})
        storage.delete("attachments/test.png")
        stub.assert_no_pending_responses()
    signed = storage.download_url("attachments/test.png")
    query = parse_qs(urlparse(signed).query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["300"]
    with Stubber(client) as stub:
        stub.add_client_error("put_object", service_error_code="AccessDenied", service_message="sensitive-aws-message")
        with pytest.raises(APIError) as error:
            storage.put("attachments/test.png", io.BytesIO(PNG), "image/png", len(PNG))
        assert error.value.status_code == 503
        assert "sensitive-aws-message" not in str(error.value)
    client.close()


async def test_raw_control_character_filename_is_rejected():
    from app.services.attachments import validate_upload

    # HTTPX는 NUL을 %00으로 인코딩하므로 검증 경계에 원래 파일명을 직접 전달한다.
    file = UploadFile(io.BytesIO(PNG), filename="x\x00.png", headers=Headers({"content-type": "image/png"}))
    try:
        with pytest.raises(APIError) as error:
            await validate_upload(file)
        assert error.value.status_code == 400
    finally:
        await file.close()
