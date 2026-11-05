"""moa-seed 문서를 기존 K01 API로 등록한다. 기본은 네트워크 없는 사전 점검이다."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.schemas.documents import DocumentCreateRequest


class SeedError(Exception):
    pass


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def load_plan(seed_dir, *, tenant_override=None):
    """고정 docs 경로만 읽어 demo 변경 자료와 labels 정답의 혼입을 방지한다."""
    tenant = (
        tenant_override
        or json.loads((seed_dir / "company.json").read_text())["tenant_id"]
    )
    if not re.fullmatch(r"[a-z]{1,64}", tenant):
        raise SeedError("INVALID_TENANT")
    entries, versions = [], {}
    for path in sorted((seed_dir / "docs").glob("*.md")):
        match = re.fullmatch(r"(DOC-[0-9]{3})_v([1-9][0-9]*)\.md", path.name)
        if not match or path.is_symlink():
            raise SeedError("INVALID_SOURCE_FILE")
        content = path.read_text(encoding="utf-8")
        parts = content.split("---\n", 2)
        if len(parts) != 3 or parts[0]:
            raise SeedError("INVALID_FRONTMATTER")
        metadata = {}
        for line in parts[1].splitlines():
            key, separator, value = line.partition(":")
            if not separator or key in metadata:
                raise SeedError("INVALID_FRONTMATTER")
            metadata[key] = value.strip()
        if set(metadata) != {
            "doc_id",
            "version",
            "doc_type",
            "title",
            "status",
            "created_at",
        }:
            raise SeedError("INVALID_FRONTMATTER")
        source_id, source_version = match[1], int(match[2])
        if (
            metadata["doc_id"] != source_id
            or int(metadata["version"]) != source_version
        ):
            raise SeedError("SOURCE_ID_MISMATCH")
        entries.append(
            {
                "source_file": path.name,
                "source_id": source_id,
                "source_version": source_version,
                "metadata": metadata,
                "text": parts[2],
            }
        )
    entries.sort(key=lambda item: (item["source_id"], item["source_version"]))
    if not entries:
        raise SeedError("NO_DOCUMENTS")
    for item in entries:
        versions[item["source_id"]] = versions.get(item["source_id"], 0) + 1
        # K01은 1부터 연속 발급한다. 원본 번호는 manifest에 별도로 보존한다.
        item["api_version"] = versions[item["source_id"]]
        data = {
            k: item["metadata"][k]
            for k in ("title", "doc_type", "status", "created_at")
        }
        data.update(version=1, text=item["text"])
        DocumentCreateRequest.model_validate(data)
        item["payload"] = {**data, "version": item["api_version"]}
        del item["text"], item["metadata"]
    return {"tenant_id": tenant, "entries": entries}


def save_state(path, state):
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        os.fchmod(stream.fileno(), 0o600)
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def state_lock(path):
    if path.is_symlink():
        raise SeedError("INVALID_STATE_FILE")
    with open(str(path) + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SeedError("IMPORT_ALREADY_RUNNING") from None
        yield


def request(client, method, path, expected=200, **kwargs):
    response = client.request(method, path, **kwargs)
    if response.status_code != expected:
        # 원격 본문에는 입력값·내부 정보가 포함될 수 있으므로 HTTP 상태만 표시한다.
        raise SeedError(f"API_HTTP_{response.status_code}")
    return response.json()


def apply_plan(client, plan, state_path, origin):
    me = request(client, "GET", "/api/v1/auth/me")
    if me["tenant_id"] != plan["tenant_id"] or me["role"] not in ("agent", "admin"):
        raise SeedError("SEED_ACCOUNT_MISMATCH")
    binding = {
        "origin": origin,
        "tenant_id": me["tenant_id"],
        "user_id": me["user_id"],
        "plan_hash": digest(plan),
    }
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if state.get("binding") != binding:
            raise SeedError("STATE_OR_SOURCE_CHANGED")
    else:
        existing = request(
            client, "GET", "/api/v1/admin/documents", params={"limit": 1}
        )
        if existing["items"]:
            raise SeedError("EMPTY_DOCUMENT_TENANT_REQUIRED")
        state = {"binding": binding, "import_id": uuid4().hex, "documents": {}}
        # 첫 요청의 응답이 유실되어도 같은 멱등키를 재사용하도록 먼저 저장한다.
        save_state(state_path, state)
    mapped = {}
    for entry in plan["entries"]:
        name, source_id = entry["source_file"], entry["source_id"]
        record = state["documents"].get(name)
        if record is None:
            payload = dict(entry["payload"])
            if entry["api_version"] > 1:
                payload["doc_id"] = mapped[source_id]
            response = request(
                client,
                "POST",
                "/api/v1/admin/documents",
                expected=202,
                json=payload,
                headers={"Idempotency-Key": f"seed-{state['import_id']}-{name}"},
            )
            record = {
                "source_id": source_id,
                "source_version": entry["source_version"],
                "doc_id": response["doc_id"],
                "version": response["version"],
            }
            state["documents"][name] = record
            save_state(state_path, state)
        mapped[source_id] = record["doc_id"]
    return state


def check_latest(client, state):
    latest = {}
    for record in state["documents"].values():
        latest[record["doc_id"]] = max(
            latest.get(record["doc_id"], 0), record["version"]
        )
    items, cursor = {}, None
    while True:
        page = request(
            client,
            "GET",
            "/api/v1/admin/documents",
            params={"limit": 100, **({"cursor": cursor} if cursor else {})},
        )
        items.update({item["doc_id"]: item for item in page["items"]})
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    report = []
    for doc_id, version in latest.items():
        item = items.get(doc_id)
        if not item or item["version"] != version:
            raise SeedError("DOCUMENT_CHANGED_OR_MISSING")
        report.append(
            {
                key: item[key]
                for key in (
                    "doc_id",
                    "version",
                    "status",
                    "indexing_status",
                    "searchable",
                )
            }
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed-dir", type=Path, required=True, help="moa-seed의 seed 디렉터리"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--tenant", help="별도 검증 기업. 생략하면 company.json의 기업 사용"
    )
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=0)
    args = parser.parse_args()
    try:
        plan = load_plan(args.seed_dir, tenant_override=args.tenant)
        if not args.apply:
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "tenant_id": plan["tenant_id"],
                        "plan_hash": digest(plan),
                        "documents": [
                            {k: v for k, v in item.items() if k != "payload"}
                            for item in plan["entries"]
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        endpoint = urlsplit(args.base_url)
        if (
            endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.path not in ("", "/")
            or not endpoint.hostname
            or (
                endpoint.scheme != "https"
                and not (
                    endpoint.scheme == "http"
                    and endpoint.hostname in ("localhost", "127.0.0.1", "::1")
                )
            )
        ):
            raise SeedError("HTTPS_OR_LOOPBACK_REQUIRED")
        token = os.environ.get("MOA_ADMIN_TOKEN")
        if not token or not args.state_file or not 0 <= args.wait_seconds <= 600:
            raise SeedError("TOKEN_STATE_AND_VALID_TIMEOUT_REQUIRED")
        origin = args.base_url.rstrip("/")
        with state_lock(args.state_file), httpx.Client(
            base_url=origin,
            headers={"Authorization": "Bearer " + token},
            timeout=30,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            state = apply_plan(client, plan, args.state_file, origin)
            deadline = time.monotonic() + args.wait_seconds
            while True:
                report = check_latest(client, state)
                ready = all(
                    item["indexing_status"] == "완료"
                    and (
                        item["searchable"]
                        if item["status"] == "유효"
                        else not item["searchable"]
                    )
                    for item in report
                )
                if (
                    ready
                    or time.monotonic() >= deadline
                    or any(item["indexing_status"] == "실패" for item in report)
                ):
                    print(
                        json.dumps(
                            {"accepted": True, "ready": ready, "documents": report},
                            ensure_ascii=False,
                        )
                    )
                    return 0 if ready or args.wait_seconds == 0 else 1
                time.sleep(3)
    except SeedError as error:
        print(json.dumps({"error": str(error)}))
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        print(json.dumps({"error": "INPUT_OR_TRANSPORT_ERROR"}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
