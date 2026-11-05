"""실제 배포 스크립트를 명령 대역으로 실행한다. 서버·업무 DB에는 접속하지 않는다."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import PROJECT_ROOT


@pytest.fixture
def deployment(tmp_path):
    app_dir = tmp_path / "app"
    commands = tmp_path / "commands"
    commands.mkdir()
    venv_bin = app_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    events = tmp_path / "events"
    dropin = tmp_path / "db.conf"
    env = dict(os.environ)
    for name in ("DATABASE_URL", "JWT_SECRET_KEY"):
        env.pop(name, None)
    env.update({
        "PATH": f"{commands}:{env['PATH']}", "MOA_APP_DIR": str(app_dir),
        "MOA_SERVICE_ENV_FILE": str(dropin), "DEPLOY_TEST_EVENTS": str(events),
        "DEPLOY_TEST_S3_OUTPUT": str(tmp_path / "installed-s3.conf"),
    })

    def executable(path, content):
        path.write_text(content)
        path.chmod(0o700)

    executable(commands / "git", '''#!/bin/bash
set -eu
if [ "$1" = "rev-parse" ]; then
  echo test-commit
else
  echo "git $*" >> "$DEPLOY_TEST_EVENTS"
fi
''')
    executable(venv_bin / "pip", '''#!/bin/bash
set -eu
echo install >> "$DEPLOY_TEST_EVENTS"
exit "${DEPLOY_TEST_PIP_EXIT:-0}"
''')
    executable(venv_bin / "alembic", f'''#!{sys.executable}
import os
import sys
from pathlib import Path

with open(os.environ["DEPLOY_TEST_EVENTS"], "a") as events:
    events.write("migration " + " ".join(sys.argv[1:]) + "\\n")
if os.environ.get("DEPLOY_TEST_MIGRATION_EXIT"):
    sys.exit(int(os.environ["DEPLOY_TEST_MIGRATION_EXIT"]))

# 앱과 동일한 Pydantic Settings를 사용하고 dotenv 참조 위치만 격리한다.
sys.path.insert(0, {str(PROJECT_ROOT)!r})
from app.core.config import Settings
try:
    settings = Settings(_env_file=Path.cwd() / ".env")
    if settings.database_url.get_secret_value() != os.environ.get("DEPLOY_TEST_EXPECTED_DB"):
        sys.exit(5)
    if settings.jwt_secret_key.get_secret_value() != os.environ.get("DEPLOY_TEST_EXPECTED_KEY"):
        sys.exit(5)
except Exception:
    print("test migration: configuration unavailable", file=sys.stderr)
    sys.exit(2)
''')
    executable(venv_bin / "python", f'''#!{sys.executable}
import os
import runpy
import sys
script_name = os.path.basename(sys.argv[1])
with open(os.environ["DEPLOY_TEST_EVENTS"], "a") as events:
    events.write(("configure-env" if script_name == "configure_env.py" else "configure-s3") + "\\n")
sys.argv = sys.argv[1:]
runpy.run_path(os.path.join({str(PROJECT_ROOT / "scripts")!r}, script_name), run_name="__main__")
''')
    executable(commands / "sudo", '''#!/bin/bash
set -eu
if [ "$1" = "sed" ]; then
  echo read-config >> "$DEPLOY_TEST_EVENTS"
  if [ "${DEPLOY_TEST_READ_FAILURE:-0}" = "1" ]; then
    exit 1
  fi
  shift
  exec sed "$@"
fi
if [ "$1" = "install" ]; then
  if [ "$2" = "-d" ]; then
    echo s3-directory >> "$DEPLOY_TEST_EVENTS"
    exit 0
  fi
  echo s3-install >> "$DEPLOY_TEST_EVENTS"
  if [ "${DEPLOY_TEST_INSTALL_FAILURE:-0}" = "1" ]; then
    exit 1
  fi
  [ "$2" = "-m" ] && [ "$3" = "0600" ]
  cp "$4" "$DEPLOY_TEST_S3_OUTPUT"
  exit 0
fi
if [ "$1" = "systemctl" ] && [ "$2" = "daemon-reload" ]; then
  echo daemon-reload >> "$DEPLOY_TEST_EVENTS"
  exit 0
fi
if [ "$1" = "systemctl" ] && [ "$2" = "restart" ]; then
  if [ "$3" = "moa-documents-worker" ]; then
    echo worker-restart >> "$DEPLOY_TEST_EVENTS"
  elif [ "$3" = "moa-groups-worker" ]; then
    echo groups-worker-restart >> "$DEPLOY_TEST_EVENTS"
  else
    echo restart >> "$DEPLOY_TEST_EVENTS"
  fi
  exit 0
fi
if [ "$1" = "systemctl" ] && [ "$2" = "is-active" ]; then
  [ "${DEPLOY_TEST_WORKER_ACTIVE:-0}" = "1" ]
  exit $?
fi
if [ "$1" = "journalctl" ]; then
  echo journal >> "$DEPLOY_TEST_EVENTS"
  exit 0
fi
exit 1
''')
    executable(commands / "curl", '''#!/bin/bash
set -eu
echo health >> "$DEPLOY_TEST_EVENTS"
exit "${DEPLOY_TEST_HEALTH_EXIT:-0}"
''')
    executable(commands / "sleep", "#!/bin/bash\nexit 0\n")

    class Deployment:
        database_url = "postgresql+asyncpg://deploy_test:fake-password@127.0.0.1/test_only"
        key = "deploy-test-only-signing-key-1234567890"

        def configure(self, source):
            env["DEPLOY_TEST_EXPECTED_DB"] = self.database_url
            env["DEPLOY_TEST_EXPECTED_KEY"] = self.key
            if source == "dropin":
                dropin.write_text(f"[Service]\nEnvironment=DATABASE_URL={self.database_url}\nEnvironment=JWT_SECRET_KEY={self.key}\n")
            elif source == "environment":
                env.update(DATABASE_URL=self.database_url, JWT_SECRET_KEY=self.key)
            elif source == "dotenv":
                (app_dir / ".env").write_text(f"DATABASE_URL={self.database_url}\nJWT_SECRET_KEY={self.key}\n")
            else:
                raise ValueError(source)

        def run(self, *, payload=None, args=(), **overrides):
            result = subprocess.run(["bash", str(PROJECT_ROOT / "scripts" / "deploy.sh"), *args],
                                    env={**env, **overrides}, input=payload, capture_output=True, text=True)
            log = events.read_text().splitlines() if events.exists() else []
            # 테스트도 설정 값을 출력하지 않으며 stdout/stderr 누출을 검증한다.
            assert self.database_url not in result.stdout + result.stderr
            assert self.key not in result.stdout + result.stderr
            return result, log

        def s3_config(self):
            return (tmp_path / "installed-s3.conf").read_text()

        def env_file(self, name=".env"):
            return app_dir / name

        def env_payload(self):
            env["DEPLOY_TEST_EXPECTED_DB"] = self.database_url
            env["DEPLOY_TEST_EXPECTED_KEY"] = self.key
            return (f"DATABASE_URL={self.database_url}\nJWT_SECRET_KEY={self.key}\n"
                    "AWS_REGION=ap-northeast-1\nS3_BUCKET_NAME=moa-test-only-bucket\n"
                    "AI_BASE_URL=http://127.0.0.1:8001\nAI_SERVICE_TOKEN=test-only-service-token\n")

    return Deployment()


@pytest.mark.parametrize("source", ["dropin", "environment", "dotenv"])
def test_deploy_always_migrates_before_restart(deployment, source):
    deployment.configure(source)
    result, events = deployment.run()
    assert result.returncode == 0, result.stderr
    assert events[:2] == ["git fetch --quiet origin main", "git reset --hard origin/main"]
    assert events.index("install") < events.index("migration upgrade head") < events.index("restart") < events.index("health")
    assert "DB migration complete" in result.stdout and "deploy ok" in result.stdout
    assert events.count("migration upgrade head") == 1


def test_missing_configuration_fails_without_restarting(deployment):
    result, events = deployment.run()
    assert result.returncode != 0
    assert "migration upgrade head" in events
    assert "restart" not in events and "health" not in events
    assert "DB migration complete" not in result.stdout


def test_migration_failure_stops_before_restart(deployment):
    deployment.configure("dropin")
    result, events = deployment.run(DEPLOY_TEST_MIGRATION_EXIT="9")
    assert result.returncode == 9
    assert "migration upgrade head" in events
    assert "restart" not in events and "health" not in events


def test_dependency_installation_failure_stops_before_migration(deployment):
    deployment.configure("environment")
    result, events = deployment.run(DEPLOY_TEST_PIP_EXIT="7")
    assert result.returncode == 7
    assert "migration upgrade head" not in events and "restart" not in events


def test_unreadable_dropin_stops_before_migration(deployment):
    deployment.configure("dropin")
    result, events = deployment.run(DEPLOY_TEST_READ_FAILURE="1")
    assert result.returncode != 0
    assert "read-config" in events
    assert "migration upgrade head" not in events and "restart" not in events


def test_health_failure_is_not_reported_as_success(deployment):
    deployment.configure("environment")
    result, events = deployment.run(DEPLOY_TEST_HEALTH_EXIT="1")
    assert result.returncode == 1
    assert events.index("migration upgrade head") < events.index("restart")
    assert events.count("health") == 10 and "journal" in events
    assert "deploy ok" not in result.stdout and "health check failed" in result.stderr


def test_redeploy_still_runs_alembic_upgrade_head(deployment):
    deployment.configure("dotenv")
    first, _ = deployment.run()
    second, events = deployment.run()
    assert first.returncode == second.returncode == 0
    assert events.count("migration upgrade head") == 2
    assert events.count("restart") == 2



def test_s3_config_is_installed_and_reloaded_before_restart(deployment):
    import json

    deployment.configure("dropin")
    payload = {"AWS_REGION": "ap-northeast-1", "S3_BUCKET_NAME": "moa-private-test-bucket"}
    result, events = deployment.run(payload=json.dumps(payload), args=("--configure-s3",))
    assert result.returncode == 0, result.stderr
    assert events.index("migration upgrade head") < events.index("configure-s3") < events.index("s3-install") < events.index("daemon-reload") < events.index("restart")
    content = deployment.s3_config()
    assert 'Environment="AWS_REGION=ap-northeast-1"' in content
    assert 'Environment="S3_BUCKET_NAME=moa-private-test-bucket"' in content
    assert 'Environment="S3_PRESIGNED_URL_EXPIRE_SECONDS=300"' in content
    assert "DATABASE_URL" not in content and "JWT_SECRET_KEY" not in content
    assert "moa-private-test-bucket" not in result.stdout + result.stderr


def test_s3_install_failure_stops_restart(deployment):
    import json

    deployment.configure("dotenv")
    result, events = deployment.run(payload=json.dumps({"AWS_REGION": "ap-northeast-1", "S3_BUCKET_NAME": "moa-test-bucket"}),
                                    args=("--configure-s3",), DEPLOY_TEST_INSTALL_FAILURE="1")
    assert result.returncode != 0
    assert "s3-install" in events
    assert "daemon-reload" not in events and "restart" not in events


@pytest.mark.parametrize("payload", ["", "invalid-json", '{"AWS_REGION":"ap-northeast-1"}'])
def test_invalid_s3_payload_stops_restart(deployment, payload):
    deployment.configure("environment")
    result, events = deployment.run(payload=payload, args=("--configure-s3",))
    assert result.returncode != 0
    assert "configure-s3" in events
    assert "s3-install" not in events and "restart" not in events


def test_env_file_migration_and_restart_share_new_settings(deployment):
    deployment.configure("dropin")
    payload = deployment.env_payload()
    result, events = deployment.run(args=("--env-file",), payload=payload, DEPLOY_TEST_WORKER_ACTIVE="1",
                                    DATABASE_URL="postgresql+asyncpg://wrong/old", JWT_SECRET_KEY="old-test-only-key" * 3)
    assert result.returncode == 0, result.stderr
    assert "read-config" not in events
    assert events.index("configure-env") < events.index("migration upgrade head") < events.index("s3-install") < events.index("daemon-reload") < events.index("restart") < events.index("worker-restart")
    assert deployment.env_file().read_text() == payload
    assert deployment.env_file().stat().st_mode & 0o777 == 0o600
    assert deployment.env_file(".env.systemd").stat().st_mode & 0o777 == 0o600
    assert 'AI_BASE_URL="http://127.0.0.1:8001"' in deployment.env_file(".env.systemd").read_text()
    assert "test-only-service-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("payload", ["", "DATABASE_URL='unterminated", "AI_SERVICE_TOKEN=private-invalid-input"])
def test_bad_env_file_preserves_existing_configuration(deployment, payload):
    deployment.configure("dotenv")
    before = deployment.env_file().read_text()
    result, events = deployment.run(args=("--env-file",), payload=payload)
    assert result.returncode != 0
    assert deployment.env_file().read_text() == before
    assert not deployment.env_file(".env.systemd").exists()
    assert "migration upgrade head" not in events and "restart" not in events
    assert "private-invalid-input" not in result.stdout + result.stderr


def test_env_file_failed_migration_preserves_existing_files(deployment):
    deployment.configure("dotenv")
    before = deployment.env_file().read_text()
    result, events = deployment.run(args=("--env-file",), payload=deployment.env_payload(), DEPLOY_TEST_MIGRATION_EXIT="9")
    assert result.returncode != 0
    assert deployment.env_file().read_text() == before
    assert not deployment.env_file(".env.systemd").exists()
    assert "migration upgrade head" in events and "s3-install" not in events and "restart" not in events


def test_manual_redeploy_after_env_file_does_not_return_to_stale_dropin(deployment):
    deployment.configure("dropin")
    first, _ = deployment.run(args=("--env-file",), payload=deployment.env_payload())
    assert first.returncode == 0
    second, events = deployment.run()
    assert second.returncode == 0
    assert events.count("configure-env") == 2 and "read-config" not in events
    assert events.count("migration upgrade head") == 2


def test_legacy_s3_mode_cannot_override_new_env_file(deployment):
    first, _ = deployment.run(args=("--env-file",), payload=deployment.env_payload())
    assert first.returncode == 0
    second, events = deployment.run(args=("--configure-s3",), payload="{}")
    assert second.returncode != 0
    assert events.count("migration upgrade head") == 1


def test_running_groups_worker_restarts_after_migration(deployment):
    deployment.configure("environment")
    result, events = deployment.run(DEPLOY_TEST_WORKER_ACTIVE="1")
    assert result.returncode == 0, result.stderr
    assert events.index("migration upgrade head") < events.index("groups-worker-restart") < events.index("health")
