"""ENV_FILE 검증 → 같은 환경으로 migration → 비공개 환경파일 적용. 값은 출력하지 않는다."""
import io
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


class EnvFileError(ValueError):
    """외부 예외 원문 대신 코드와 코드에서 정한 설명만 공개한다."""
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def parse_environment(content: str) -> dict[str, str]:
    if not content.strip():
        raise EnvFileError("ENV_FILE_EMPTY", "ENV_FILE 내용이 비어 있습니다.")
    if len(content.encode("utf-8")) > 65536 or "\x00" in content:
        raise EnvFileError("ENV_FILE_FORMAT_INVALID", "환경파일 크기 또는 문자 형식을 확인하세요.")
    result = {}
    for binding in parse_stream(io.StringIO(content)):
        if binding.error:
            raise EnvFileError("ENV_FILE_SYNTAX_INVALID", f"dotenv {binding.original.line}번째 줄의 문법을 확인하세요.")
        if binding.key is None:
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", binding.key) or binding.value is None or binding.key in result:
            raise EnvFileError("ENV_FILE_KEY_INVALID", f"dotenv {binding.original.line}번째 줄의 키·중복 여부를 확인하세요.")
        # shell 명령·변수 치환을 실행하지 않는다. $와 %도 Secret의 원문 값이다.
        result[binding.key] = binding.value
    missing = [key for key in REQUIRED_KEYS if not result.get(key, "").strip()]
    if missing:
        raise EnvFileError("ENV_FILE_REQUIRED_MISSING", "필수 설정 누락: " + ", ".join(missing))
    return result


def runtime_environment(raw: dict[str, str]) -> dict[str, str]:
    # 모든 Settings 필드를 명시해 배포 프로세스의 과거 환경변수가 검증에 섞이지 않게 한다.
    values = {name: raw.get(name.upper(), field.default) for name, field in Settings.model_fields.items()}
    try:
        settings = Settings(_env_file=None, **values)
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]).upper() for error in exc.errors(include_input=False, include_url=False)
                         if error["loc"] and error["loc"][0] in Settings.model_fields})
        raise EnvFileError("ENV_FILE_SETTINGS_INVALID", "설정 형식 확인: " + ", ".join(fields)) from None
    try:
        url = make_url(settings.database_url.get_secret_value())
    except ArgumentError:
        raise EnvFileError("ENV_FILE_DATABASE_URL_INVALID", "DATABASE_URL 형식을 확인하세요.") from None
    if url.drivername != "postgresql+asyncpg" or not url.database:
        raise EnvFileError("ENV_FILE_DATABASE_URL_INVALID", "DATABASE_URL은 DB 이름을 포함한 postgresql+asyncpg URL이어야 합니다.")
    ai = urlsplit(settings.ai_base_url)
    if ai.scheme not in ("http", "https") or not ai.hostname or ai.username or ai.password or ai.query or ai.fragment:
        raise EnvFileError("ENV_FILE_AI_URL_INVALID", "AI_BASE_URL은 계정·query·fragment가 없는 HTTP(S) 주소여야 합니다.")
    try:
        ai.port  # 외부 ValueError에는 입력한 포트 값이 들어갈 수 있으므로 원문을 숨긴다.
    except ValueError:
        raise EnvFileError("ENV_FILE_AI_URL_INVALID", "AI_BASE_URL의 포트를 확인하세요.") from None
    try:
        s3 = validate_s3({key: raw.get(key, "") for key in ("AWS_REGION", "S3_BUCKET_NAME", "S3_PRESIGNED_URL_EXPIRE_SECONDS")})
    except ValueError:
        raise EnvFileError("ENV_FILE_S3_INVALID", "AWS_REGION·S3_BUCKET_NAME·S3_PRESIGNED_URL_EXPIRE_SECONDS 형식을 확인하세요.") from None
    result = dict(raw)
    for name in Settings.model_fields:
        value = getattr(settings, name)
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
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
    try:
        subprocess.run([str(app_dir / ".venv/bin/alembic"), "upgrade", "head"], cwd=app_dir,
                       env={**os.environ, **values}, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, text=True, errors="replace")
    except subprocess.CalledProcessError as exc:
        # stderr는 메모리에서 분류만 한다. SQL·접속 문자열·비밀번호는 출력하지 않는다.
        output = exc.stderr or ""
        diagnoses = (
            (("InvalidPasswordError", "InvalidAuthorizationSpecificationError", "password authentication failed"), "DB_AUTH_FAILED", "운영 DATABASE_URL의 계정·비밀번호를 확인하세요."),
            (("ConnectionRefusedError", "Connection refused"), "DB_CONNECTION_REFUSED", "운영 DATABASE_URL의 호스트·포트를 확인하세요."),
            (("TimeoutError", "Timeout expired", "timeout expired"), "DB_CONNECTION_TIMEOUT", "DB 주소·네트워크·보안 그룹을 확인하세요."),
            (("gaierror", "Name or service not known"), "DB_HOST_UNRESOLVED", "DB 호스트 이름을 확인하세요."),
            (("InvalidCatalogNameError",), "DB_NOT_FOUND", "운영 DATABASE_URL의 DB 이름을 확인하세요."),
            (("InsufficientPrivilegeError",), "DB_PERMISSION_DENIED", "마이그레이션 계정의 스키마·테이블 변경 권한을 확인하세요."),
            (("Can't locate revision identified by", "Multiple head revisions"), "DB_REVISION_CONFLICT", "서버 코드와 DB Alembic revision을 확인하세요."),
        )
        for markers, code, detail in diagnoses:
            if any(marker in output for marker in markers):
                raise EnvFileError(code, detail) from None
        raise EnvFileError("DB_MIGRATION_FAILED", f"Alembic이 종료 코드 {exc.returncode}로 실패했습니다. DB 스키마와 revision을 확인하세요.") from None
    except OSError:
        raise EnvFileError("DB_MIGRATION_START_FAILED", "서버의 .venv/bin/alembic 실행 파일·권한을 확인하세요.") from None
    try:
        write_private(app_dir / ".env", content)
        write_private(app_dir / ".env.systemd", systemd_environment(values))
    except OSError:
        raise EnvFileError("ENV_FILE_WRITE_FAILED", "마이그레이션은 성공했지만 환경파일 저장에 실패했습니다. 디렉터리 권한·디스크 공간을 확인하세요.") from None
    try:
        install_service_environment(app_dir)
    except (OSError, subprocess.CalledProcessError):
        raise EnvFileError("SERVICE_ENV_INSTALL_FAILED", "환경파일은 저장되었지만 systemd 설정 설치에 실패했습니다. sudo 권한을 확인하세요.") from None


def main() -> int:
    try:
        if sys.argv[1:]:
            raise ValueError("지원하지 않는 옵션")
        apply_environment(Path.cwd().resolve(), sys.stdin.read(65537))
    except EnvFileError as exc:
        print(f"ENV_FILE 반영 실패 [{exc.code}]: {exc.detail} 설정 값은 출력하지 않습니다.", file=sys.stderr)
        return 1
    except (ValueError, OSError, subprocess.CalledProcessError):
        print("ENV_FILE 반영 실패 [ENV_FILE_INPUT_FAILED]: 입력 형식·경로·파일 접근 권한을 확인하세요. 설정 값은 출력하지 않습니다.", file=sys.stderr)
        return 1
    print("ENV_FILE 검증·DB 마이그레이션·서비스 설정 저장 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
