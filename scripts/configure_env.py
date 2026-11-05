"""ENV_FILE 검증 → 같은 환경으로 migration → 비공개 환경파일 적용. 값은 출력하지 않는다."""
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from dotenv.parser import parse_stream
from pydantic import ValidationError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from app.core.config import Settings
from scripts.configure_s3 import validate_config as validate_s3

REQUIRED_KEYS = ("DATABASE_URL", "JWT_SECRET_KEY", "AWS_REGION", "S3_BUCKET_NAME", "AI_BASE_URL", "AI_SERVICE_TOKEN")
SERVICES = ("moa-backend", "moa-documents-worker")


def parse_environment(content: str) -> dict[str, str]:
    if not content.strip() or len(content.encode("utf-8")) > 65536 or "\x00" in content:
        raise ValueError("환경파일 크기 또는 형식 오류")
    result = {}
    for binding in parse_stream(io.StringIO(content)):
        if binding.error:
            raise ValueError("환경파일 문법 오류")
        if binding.key is None:
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", binding.key) or binding.value is None or binding.key in result:
            raise ValueError("환경파일 키 오류")
        # shell 명령·변수 치환을 실행하지 않는다. $와 %도 Secret의 원문 값이다.
        result[binding.key] = binding.value
    if any(not result.get(key, "").strip() for key in REQUIRED_KEYS):
        raise ValueError("필수 설정 누락")
    return result


def runtime_environment(raw: dict[str, str]) -> dict[str, str]:
    # 모든 Settings 필드를 명시해 배포 프로세스의 과거 환경변수가 검증에 섞이지 않게 한다.
    values = {name: raw.get(name.upper(), field.get_default(call_default_factory=True)) for name, field in Settings.model_fields.items()}
    # Settings의 생성자 인자는 환경 소스와 달리 복합 JSON 문자열을 자동 해석하지 않는다.
    if isinstance(values["auto_response_policies"], str):
        values["auto_response_policies"] = json.loads(values["auto_response_policies"])
    settings = Settings(_env_file=None, **values)
    try:
        url = make_url(settings.database_url.get_secret_value())
    except ArgumentError:
        raise ValueError("DB URL 형식 오류") from None
    if url.drivername != "postgresql+asyncpg" or not url.database:
        raise ValueError("DB URL 형식 오류")
    ai = urlsplit(settings.ai_base_url)
    if ai.scheme not in ("http", "https") or not ai.hostname or ai.username or ai.password or ai.query or ai.fragment:
        raise ValueError("AI URL 형식 오류")
    ai.port  # 잘못된 포트 표현도 검증한다.
    s3 = validate_s3({key: raw.get(key, "") for key in ("AWS_REGION", "S3_BUCKET_NAME", "S3_PRESIGNED_URL_EXPIRE_SECONDS")})
    result = dict(raw)
    json_values = settings.model_dump(mode="json")
    for name in Settings.model_fields:
        value = getattr(settings, name)
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        # systemd와 마이그레이션 프로세스도 같은 JSON 설정을 읽어야 한다.
        if isinstance(value, (dict, list)):
            value = json.dumps(json_values[name], ensure_ascii=False, separators=(",", ":"))
        result[name.upper()] = "" if value is None else str(value)
    result.update(s3)
    return result


def systemd_environment(values: dict[str, str]) -> str:
    # EnvironmentFile은 $/specifier를 확장하지 않는다. 따옴표와 backslash만 인용한다.
    return "".join(key + '=\"' + value.replace("\\", "\\\\").replace('"', '\\"') + '\"\n'
                   for key, value in sorted(values.items()))


def write_private(path: Path, content: str):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install_service_environment(app_dir: Path):
    path = str(app_dir / ".env.systemd")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", path):
        raise ValueError("서비스 환경파일 경로 오류")
    # EnvironmentFile이 기존 db.conf/zz-moa-s3.conf의 Environment 값을 덮어쓴다.
    content = f"[Service]\nEnvironmentFile=\nEnvironmentFile={path}\n"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as temporary:
        temporary.write(content)
        temporary.flush()
        for service in SERVICES:
            directory = Path("/etc/systemd/system") / f"{service}.service.d"
            subprocess.run(["sudo", "install", "-d", "-m", "0755", str(directory)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["sudo", "install", "-m", "0600", temporary.name, str(directory / "zzzz-moa-env-file.conf")],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def apply_environment(app_dir: Path, content: str):
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(app_dir / ".env.systemd")):
        raise ValueError("서비스 환경파일 경로 오류")
    values = runtime_environment(parse_environment(content))
    # 마이그레이션 실패 시 기존 .env와 서비스 설정은 그대로 유지한다.
    subprocess.run([str(app_dir / ".venv/bin/alembic"), "upgrade", "head"], cwd=app_dir,
                   env={**os.environ, **values}, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    write_private(app_dir / ".env", content)
    write_private(app_dir / ".env.systemd", systemd_environment(values))
    install_service_environment(app_dir)


def main() -> int:
    try:
        if sys.argv[1:]:
            raise ValueError("지원하지 않는 옵션")
        apply_environment(Path.cwd().resolve(), sys.stdin.read(65537))
    except (ValueError, ValidationError, OSError, subprocess.CalledProcessError):
        print("ENV_FILE 반영 실패: 필수 설정·dotenv 문법·DB 연결 및 마이그레이션·파일 설치 권한을 확인하세요. 설정 값은 출력하지 않습니다.", file=sys.stderr)
        return 1
    print("ENV_FILE 검증·DB 마이그레이션·서비스 설정 저장 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
