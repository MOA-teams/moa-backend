"""시연 급증 데이터를 현재 문의 API로 접수한다. 기본 실행은 HTTP 없는 dry-run이다.

준비: 별도 시연 기업에 속한 customer의 access token을 MOA_CUSTOMER_ACCESS_TOKEN에 둔다.
실행: python scripts/burst_replay.py --seed-file ../moa-seed/seed/demo/burst_inquiries.json
      --base-url https://backend.example --tenant syncnote --state /tmp/moa-burst.json --execute
중단 후 같은 명령과 state 파일로 재개한다. 새 state 파일은 새 실행이므로 중복 투입에 주의한다.
과거 시드·정답 라벨은 적재하지 않는다. 실제 소유자는 인증 고객이며 시드 customer_id는 참고값이다.
AI·GitHub 작업은 서버 설정에 따라 이어지므로 실제 실행은 시연 기업에서만 한다.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.schemas.inquiries import InquiryCreateRequest, InquiryEnvironment
from app.core.enums import InquiryCategory


class ReplayError(Exception):
    """외부 본문·인증정보가 아닌 고정 오류 분류만 출력한다."""


class BurstItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    burst_id: str = Field(pattern=r"^B-[0-9]{3}$")
    offset_seconds: float = Field(ge=0, allow_inf_nan=False)
    customer_id: str = Field(min_length=1, max_length=128)
    title: str
    message: str
    customer_category: InquiryCategory
    env: InquiryEnvironment
    attachments: list[str] = Field(default_factory=list, max_length=0)


def load_plan(path):
    if "labels" in path.resolve().parts:
        raise ReplayError("LABELS_NOT_ALLOWED")
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows or len(rows) > 200:
        raise ReplayError("INVALID_BURST_FILE")
    plan = []
    for raw in rows:
        item = BurstItem.model_validate(raw)
        body = InquiryCreateRequest(
            message=item.message,
            source_customer_id=item.customer_id,
            customer_category=item.customer_category,
            env=item.env,
            attachments=[],
        ).model_dump(mode="json")
        plan.append(
            {"burst_id": item.burst_id, "offset": item.offset_seconds, "body": body}
        )
    if len({row["burst_id"] for row in plan}) != len(plan):
        raise ReplayError("DUPLICATE_BURST_ID")
    return sorted(plan, key=lambda row: (row["offset"], row["burst_id"]))


def normalize_base(value):
    url = urlsplit(value)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path.rstrip("/") not in {"", "/backend"}
    ):
        raise ReplayError("INVALID_API_BASE_URL")
    return value.rstrip("/")


def plan_hash(plan):
    return hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def save_state(path, state):
    # HTTP 전 run ID를 영속화한다. 응답 유실·프로세스 종료 후에도 같은 멱등키를 사용한다.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_state(path, expected, plan):
    if not path.exists():
        state = {**expected, "run_id": str(uuid4()), "results": {}}
        save_state(path, state)
        return state
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or any(
        state.get(k) != v for k, v in expected.items()
    ):
        raise ReplayError("REPLAY_STATE_MISMATCH")
    try:
        UUID(state["run_id"])
        results = state["results"]
        known = {row["burst_id"] for row in plan}
        if (
            not isinstance(results, dict)
            or not results.keys() <= known
            or any(
                not isinstance(v, str) or not re.fullmatch(r"Q-[0-9]{4}", v)
                for v in results.values()
            )
        ):
            raise ValueError
        if len(set(results.values())) != len(results):
            raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ReplayError("INVALID_REPLAY_STATE") from None
    return state


def response_json(response, expected):
    if response.status_code != expected:
        # 서버의 오류 원문에는 고객 정보가 섞일 수 있다. HTTP 상태만 남긴다.
        raise ReplayError(f"HTTP_{response.status_code}")
    try:
        value = response.json()
    except ValueError:
        raise ReplayError("INVALID_SERVER_RESPONSE") from None
    if not isinstance(value, dict):
        raise ReplayError("INVALID_SERVER_RESPONSE")
    return value


def replay(
    client,
    plan,
    *,
    base,
    tenant,
    state_path,
    duration,
    sleep=time.sleep,
    clock=time.monotonic,
    emit=print,
):
    actor = response_json(client.get("/api/v1/auth/me"), 200)
    if (
        actor.get("role") != "customer"
        or actor.get("tenant_id") != tenant
        or not re.fullmatch(r"U-[0-9]{4}", str(actor.get("user_id", "")))
    ):
        raise ReplayError("CUSTOMER_TENANT_MISMATCH")
    state = load_state(
        state_path,
        {
            "schema": 1,
            "base_url": base,
            "tenant_id": tenant,
            "user_id": actor["user_id"],
            "plan_hash": plan_hash(plan),
        },
        plan,
    )
    pending = [row for row in plan if row["burst_id"] not in state["results"]]
    start = clock()
    first_offset = pending[0]["offset"] if pending else 0
    span = max(row["offset"] for row in plan) or 1
    for row in pending:
        wait = (row["offset"] - first_offset) * duration / span - (clock() - start)
        if wait > 0:
            sleep(wait)
        key = f"burst:{state['run_id']}:{row['burst_id']}"
        response = response_json(
            client.post(
                "/api/v1/inquiries",
                json=row["body"],
                headers={"Idempotency-Key": key},
            ),
            201,
        )
        inquiry_id = response.get("inquiry_id")
        if not isinstance(inquiry_id, str) or not re.fullmatch(
            r"Q-[0-9]{4}", inquiry_id
        ):
            raise ReplayError("INVALID_SERVER_RESPONSE")
        state["results"][row["burst_id"]] = inquiry_id
        save_state(state_path, state)
        emit(f"OK {row['burst_id']} → {inquiry_id}")
    emit(
        f"COMPLETE {len(state['results'])}/{len(plan)} (접수 완료, AI 분석 완료 여부는 별도 확인)"
    )
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--seed-file", required=True, type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--tenant", default="syncnote")
    parser.add_argument("--duration", type=float, default=120)
    parser.add_argument(
        "--state", type=Path, help="같은 실행을 재개할 때 반드시 같은 파일 사용"
    )
    parser.add_argument(
        "--execute", action="store_true", help="생략하면 HTTP·파일 쓰기 없는 dry-run"
    )
    args = parser.parse_args(argv)
    try:
        if (
            not math.isfinite(args.duration)
            or args.duration < 0
            or not re.fullmatch(r"[a-z]{1,64}", args.tenant)
        ):
            raise ReplayError("INVALID_ARGUMENT")
        plan = load_plan(args.seed_file)
        if not args.execute:
            span = max(row["offset"] for row in plan) or 1
            for row in plan:
                print(
                    f"DRY {row['burst_id']} +{row['offset'] * args.duration / span:.1f}s"
                )
            print(
                f"DRY {len(plan)}건: 인증 고객 소유로 접수, 외부 고객 ID·환경 보존, 정답 보충 없음"
            )
            return 0
        if not args.base_url or not args.state:
            raise ReplayError("BASE_URL_AND_STATE_REQUIRED")
        base = normalize_base(args.base_url)
        token = os.environ.get("MOA_CUSTOMER_ACCESS_TOKEN", "").strip()
        if not token or any(c.isspace() for c in token):
            raise ReplayError("CUSTOMER_ACCESS_TOKEN_REQUIRED")
        # 같은 회차를 두 프로세스가 겹쳐 실행하지 않도록 로컬 기록도 잠근다.
        with args.state.with_suffix(args.state.suffix + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReplayError("REPLAY_ALREADY_RUNNING") from None
            with httpx.Client(
                base_url=base,
                timeout=15,
                follow_redirects=False,
                trust_env=False,
                headers={"Authorization": f"Bearer {token}"},
            ) as client:
                replay(
                    client,
                    plan,
                    base=base,
                    tenant=args.tenant,
                    state_path=args.state,
                    duration=args.duration,
                )
        return 0
    except ReplayError as exc:
        print(f"STOP {exc}. 같은 state 파일로 재개하세요.", file=sys.stderr)
    except (OSError, ValueError, ValidationError):
        print(
            "STOP INVALID_INPUT_OR_STATE. 파일·실행 기록을 확인하세요.", file=sys.stderr
        )
    except httpx.HTTPError:
        print(
            "STOP NETWORK_ERROR. 결과가 유실됐을 수 있으므로 같은 state 파일로 재개하세요.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
