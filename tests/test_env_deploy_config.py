import io
import os
from pathlib import Path
import subprocess

import pytest
from dotenv import dotenv_values

from scripts import configure_env

PAYLOAD = """DATABASE_URL=postgresql+asyncpg://test:fake@127.0.0.1/test_only
JWT_SECRET_KEY='test-only-jwt-key-at-least-32-bytes-long'
AWS_REGION=ap-northeast-1
S3_BUCKET_NAME=moa-test-only-bucket
AI_BASE_URL=http://127.0.0.1:8001
AI_SERVICE_TOKEN='test-only-$token-%value'
"""


def test_environment_preserves_special_characters_without_interpolation(monkeypatch):
    monkeypatch.setenv("ALREADY_SET", "must-not-expand")
    payload = PAYLOAD + 'CUSTOM="${ALREADY_SET} $(touch ignored) `ignored` %n \\"quoted\\" \\\\path"\n'
    raw = configure_env.parse_environment(payload)
    assert "${ALREADY_SET}" in raw["CUSTOM"] and "must-not-expand" not in raw["CUSTOM"]
    values = configure_env.runtime_environment(raw)
    serialized = configure_env.systemd_environment(values)
    assert dotenv_values(stream=io.StringIO(serialized), interpolate=False) == values
    assert values["S3_PRESIGNED_URL_EXPIRE_SECONDS"] == "300"


@pytest.mark.parametrize("change", [
    lambda p: "", lambda p: p + "\x00", lambda p: p + "X='unterminated",
    lambda p: p + "JWT_SECRET_KEY=duplicate\n", lambda p: p + "NOT_SET\n",
    lambda p: p.replace("AWS_REGION=ap-northeast-1", "AWS_REGION="),
    lambda p: p.replace("http://127.0.0.1:8001", "file:///private/config"),
    lambda p: p.replace("postgresql+asyncpg", "sqlite"),
    lambda p: p.replace("postgresql+asyncpg://test:fake@127.0.0.1/test_only", "private-invalid-database-url"),
    lambda p: p.replace("moa-test-only-bucket", "invalid bucket"),
    lambda p: p.replace("test-only-jwt-key-at-least-32-bytes-long", "short"),
])
def test_invalid_configuration_fails_before_external_commands(change, monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("검증 전에 외부 명령을 실행하면 안 됩니다.")
    monkeypatch.setattr(configure_env.subprocess, "run", forbidden)
    with pytest.raises(ValueError):
        configure_env.apply_environment(tmp_path, change(PAYLOAD))
    assert not (tmp_path / ".env").exists()


def test_old_environment_cannot_supply_missing_secrets(monkeypatch):
    monkeypatch.setenv("AI_SERVICE_TOKEN", "stale-secret")
    payload = "\n".join(line for line in PAYLOAD.splitlines() if not line.startswith("AI_SERVICE_TOKEN="))
    with pytest.raises(ValueError):
        configure_env.parse_environment(payload)


def test_runtime_defaults_override_stale_systemd_values(monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_SECONDS", "99999")
    monkeypatch.setenv("S3_PRESIGNED_URL_EXPIRE_SECONDS", "99999")
    values = configure_env.runtime_environment(configure_env.parse_environment(PAYLOAD))
    assert values["ACCESS_TOKEN_EXPIRE_SECONDS"] == "3600"
    assert values["S3_PRESIGNED_URL_EXPIRE_SECONDS"] == "300"


def test_migration_uses_new_values_and_files_are_private(monkeypatch, tmp_path):
    commands, dropins = [], []
    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert "test-only-" not in " ".join(command)
        if command[-2:] == ["upgrade", "head"]:
            assert kwargs["stderr"] is subprocess.PIPE
            assert not (tmp_path / ".env").exists()
            assert kwargs["env"]["AI_SERVICE_TOKEN"] == "test-only-$token-%value"
        elif command[:4] == ["sudo", "install", "-m", "0600"]:
            assert kwargs["stderr"] is subprocess.DEVNULL
            dropins.append(command[-1])
            source = Path(command[4])
            assert source.stat().st_mode & 0o777 == 0o600
            content = source.read_text()
            assert "EnvironmentFile=\nEnvironmentFile=" in content
            assert str(tmp_path / ".env.systemd") in content
            assert "test-only" not in content
    monkeypatch.setattr(configure_env.subprocess, "run", run)
    configure_env.apply_environment(tmp_path, PAYLOAD)
    assert len(dropins) == 2 and any("moa-documents-worker" in path for path in dropins)
    assert (tmp_path / ".env").read_text() == PAYLOAD
    assert all((tmp_path / name).stat().st_mode & 0o777 == 0o600 for name in (".env", ".env.systemd"))


def test_migration_failure_keeps_old_files(monkeypatch, tmp_path):
    for name in (".env", ".env.systemd"):
        (tmp_path / name).write_text("old-config")
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(configure_env.subprocess, "run", fail)
    with pytest.raises(configure_env.EnvFileError, match="DB_MIGRATION_FAILED"):
        configure_env.apply_environment(tmp_path, PAYLOAD)
    assert all((tmp_path / name).read_text() == "old-config" for name in (".env", ".env.systemd"))


def test_private_file_replacement_does_not_follow_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("preserved")
    path = tmp_path / ".env"
    path.symlink_to(target)
    configure_env.write_private(path, "new-config")
    assert target.read_text() == "preserved" and not path.is_symlink()
    assert path.read_text() == "new-config" and path.stat().st_mode & 0o777 == 0o600


def test_cli_invalid_database_does_not_print_values_or_traceback(tmp_path):
    import sys
    payload = PAYLOAD.replace("postgresql+asyncpg://test:fake@127.0.0.1/test_only", "private-invalid-database-url")
    result = subprocess.run([sys.executable, str(configure_env.PROJECT_ROOT / "scripts/configure_env.py")],
                            cwd=tmp_path, input=payload, capture_output=True, text=True)
    assert result.returncode == 1
    assert "private-invalid-database-url" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert "ENV_FILE_DATABASE_URL_INVALID" in result.stderr


@pytest.mark.parametrize("detail,code", [
    ("asyncpg.exceptions.InvalidPasswordError: password authentication failed for user private-user", "DB_AUTH_FAILED"),
    ("asyncpg.exceptions.InvalidAuthorizationSpecificationError: private-user", "DB_AUTH_FAILED"),
    ("ConnectionRefusedError: private-host", "DB_CONNECTION_REFUSED"),
    ("TimeoutError: private-connection", "DB_CONNECTION_TIMEOUT"),
    ("socket.gaierror: private-host", "DB_HOST_UNRESOLVED"),
    ("InvalidCatalogNameError: private-database", "DB_NOT_FOUND"),
    ("InsufficientPrivilegeError: private-table", "DB_PERMISSION_DENIED"),
    ("Can't locate revision identified by private-revision", "DB_REVISION_CONFLICT"),
    ("private-sql private-password unexpected failure", "DB_MIGRATION_FAILED"),
])
def test_migration_failure_reports_only_safe_category(detail, code, monkeypatch, tmp_path):
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr=detail)
    monkeypatch.setattr(configure_env.subprocess, "run", fail)
    with pytest.raises(configure_env.EnvFileError) as error:
        configure_env.apply_environment(tmp_path, PAYLOAD)
    assert error.value.code == code
    assert "private-" not in str(error.value)
    assert not (tmp_path / ".env").exists()


def test_missing_fields_and_syntax_errors_are_actionable_without_values():
    with pytest.raises(configure_env.EnvFileError) as missing:
        configure_env.parse_environment("DATABASE_URL=private-url\n")
    assert missing.value.code == "ENV_FILE_REQUIRED_MISSING"
    assert "JWT_SECRET_KEY" in missing.value.detail and "AI_SERVICE_TOKEN" in missing.value.detail
    assert "private-url" not in str(missing.value)
    with pytest.raises(configure_env.EnvFileError) as syntax:
        configure_env.parse_environment("# comment\nTOKEN='private-secret\n")
    assert syntax.value.code == "ENV_FILE_SYNTAX_INVALID"
    assert "2번째" in syntax.value.detail and "private-secret" not in str(syntax.value)


def test_settings_field_error_does_not_expose_invalid_value():
    raw = configure_env.parse_environment(PAYLOAD)
    raw["ACCESS_TOKEN_EXPIRE_SECONDS"] = "private-invalid-value"
    with pytest.raises(configure_env.EnvFileError) as error:
        configure_env.runtime_environment(raw)
    assert error.value.code == "ENV_FILE_SETTINGS_INVALID"
    assert "ACCESS_TOKEN_EXPIRE_SECONDS" in error.value.detail
    assert "private-invalid-value" not in str(error.value)


def test_service_install_failure_is_distinct_from_migration(monkeypatch, tmp_path):
    def run(command, **kwargs):
        if command[0] == "sudo":
            raise subprocess.CalledProcessError(1, command, stderr="private-systemd-value")
    monkeypatch.setattr(configure_env.subprocess, "run", run)
    with pytest.raises(configure_env.EnvFileError) as error:
        configure_env.apply_environment(tmp_path, PAYLOAD)
    assert error.value.code == "SERVICE_ENV_INSTALL_FAILED"
    assert "private-systemd-value" not in str(error.value)
    assert (tmp_path / ".env").is_file()
