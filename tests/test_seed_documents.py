import json

import httpx
import pytest

from scripts.seed_documents import (
    SeedError,
    apply_plan,
    check_latest,
    load_plan,
    state_lock,
)


def source(root, version, *, folder="docs", status="유효"):
    directory = root / folder
    directory.mkdir(exist_ok=True)
    path = directory / f"DOC-001_v{version}.md"
    path.write_text(
        f"---\ndoc_id: DOC-001\nversion: {version}\ndoc_type: 정책\ntitle: 요금제\nstatus: {status}\ncreated_at: 2026-09-01T00:00:00+09:00\n---\n원본 내용 {version}\n"
    )
    return path


@pytest.fixture
def seed(tmp_path):
    (tmp_path / "company.json").write_text('{"tenant_id":"syncnote"}')
    source(tmp_path, 2, status="폐기")
    source(tmp_path, 10)
    source(tmp_path, 11, folder="demo")
    source(tmp_path, 12, folder="labels")
    return tmp_path


def test_seed_uses_only_documents_and_preserves_source_version_mapping(seed):
    plan = load_plan(seed)
    assert [(e["source_version"], e["api_version"]) for e in plan["entries"]] == [
        (2, 1),
        (10, 2),
    ]
    assert plan["entries"][0]["payload"]["status"] == "폐기"
    assert plan["entries"][1]["payload"]["text"] == "원본 내용 10\n"


def test_invalid_metadata_fails_before_registration(seed):
    path = seed / "docs/DOC-001_v2.md"
    path.write_text(path.read_text().replace("version: 2", "version: 9"))
    with pytest.raises(SeedError, match="SOURCE_ID_MISMATCH"):
        load_plan(seed)


def test_symlink_source_is_not_imported(seed):
    (seed / "docs/DOC-002_v1.md").symlink_to(seed / "demo/DOC-001_v11.md")
    with pytest.raises(SeedError, match="INVALID_SOURCE_FILE"):
        load_plan(seed)


def responder(*, tenant="syncnote", existing=False):
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/auth/me"):
            return httpx.Response(
                200, json={"user_id": "U-2001", "tenant_id": tenant, "role": "agent"}
            )
        if request.method == "GET":
            return httpx.Response(200, json={"items": [{}] if existing else []})
        body = json.loads(request.content)
        return httpx.Response(
            202, json={"doc_id": "DOC-023", "version": body["version"]}
        )

    return calls, handle


def test_import_preserves_mapping_and_resume_does_not_post_again(seed):
    calls, handle = responder()
    state_path = seed / "state.json"
    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(handle)
    ) as client:
        first = apply_plan(client, load_plan(seed), state_path, "http://localhost")
        second = apply_plan(client, load_plan(seed), state_path, "http://localhost")
    posts = [call for call in calls if call.method == "POST"]
    assert len(posts) == 2 and first == second
    assert "doc_id" not in json.loads(posts[0].content)
    assert json.loads(posts[1].content)["doc_id"] == "DOC-023"
    assert first["documents"]["DOC-001_v10.md"]["source_version"] == 10
    assert state_path.stat().st_mode & 0o777 == 0o600


def test_lost_response_reuses_identical_idempotency_key_and_body(seed):
    calls, handle = responder()
    lost = True

    def interrupted(request):
        nonlocal lost
        response = handle(request)
        if request.method == "POST" and lost:
            lost = False
            raise httpx.ReadTimeout("response lost")
        return response

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(interrupted)
    ) as client:
        with pytest.raises(httpx.ReadTimeout):
            apply_plan(client, load_plan(seed), seed / "state.json", "http://localhost")
        apply_plan(client, load_plan(seed), seed / "state.json", "http://localhost")
    posts = [call for call in calls if call.method == "POST"]
    assert posts[0].headers["idempotency-key"] == posts[1].headers["idempotency-key"]
    assert posts[0].content == posts[1].content


@pytest.mark.parametrize(
    "tenant,existing,error",
    [
        ("another", False, "SEED_ACCOUNT_MISMATCH"),
        ("syncnote", True, "EMPTY_DOCUMENT_TENANT_REQUIRED"),
    ],
)
def test_import_rejects_wrong_tenant_and_existing_documents(
    seed, tenant, existing, error
):
    calls, handle = responder(tenant=tenant, existing=existing)
    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(SeedError, match=error):
            apply_plan(client, load_plan(seed), seed / "state.json", "http://localhost")
    assert all(call.method == "GET" for call in calls)


def test_changed_source_cannot_reuse_manifest(seed):
    calls, handle = responder()
    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(handle)
    ) as client:
        apply_plan(client, load_plan(seed), seed / "state.json", "http://localhost")
        path = seed / "docs/DOC-001_v10.md"
        path.write_text(path.read_text() + "변경된 원본")
        with pytest.raises(SeedError, match="STATE_OR_SOURCE_CHANGED"):
            apply_plan(client, load_plan(seed), seed / "state.json", "http://localhost")
    assert len([call for call in calls if call.method == "POST"]) == 2


def test_simultaneous_resume_of_same_manifest_is_rejected(seed):
    with state_lock(seed / "state.json"):
        with pytest.raises(SeedError, match="IMPORT_ALREADY_RUNNING"):
            with state_lock(seed / "state.json"):
                pytest.fail("second importer entered")


def test_status_check_rejects_unexpected_new_version():
    state = {"documents": {"source": {"doc_id": "DOC-001", "version": 1}}}
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={"items": [{"doc_id": "DOC-001", "version": 2}], "has_more": False},
        )
    )
    with httpx.Client(base_url="http://localhost", transport=transport) as client:
        with pytest.raises(SeedError, match="DOCUMENT_CHANGED_OR_MISSING"):
            check_latest(client, state)
