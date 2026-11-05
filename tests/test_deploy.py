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
with open(os.environ["DEPLOY_TEST_EVENTS"], "a") as events:
    events.write("configure-s3\\n")
sys.argv = sys.argv[1:]
runpy.run_path({str(PROJECT_ROOT / "scripts" / "configure_s3.py")!r}, run_name="__main__")
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
  echo restart >> "$DEPLOY_TEST_EVENTS"
  exit 0
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
