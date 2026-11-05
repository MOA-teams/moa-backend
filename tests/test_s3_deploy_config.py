import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from app.core.config import PROJECT_ROOT
from scripts import configure_s3

TEST_CONFIG = {"AWS_REGION": "ap-northeast-1", "S3_BUCKET_NAME": "moa-test-only-bucket", "S3_PRESIGNED_URL_EXPIRE_SECONDS": "300"}


def test_config_uses_default_expiry_and_normalizes_copy_whitespace():
    assert configure_s3.validate_config({"AWS_REGION": " ap-northeast-1\n", "S3_BUCKET_NAME": "moa-test-only-bucket"}) == TEST_CONFIG
    assert configure_s3.validate_config({**TEST_CONFIG, "S3_PRESIGNED_URL_EXPIRE_SECONDS": ""}) == TEST_CONFIG


@pytest.mark.parametrize("config", [
    {}, [], None,
    {**TEST_CONFIG, "AWS_REGION": ""},
    {**TEST_CONFIG, "AWS_REGION": "ap-northeast-1\nEnvironment=BAD=value"},
    {**TEST_CONFIG, "AWS_REGION": "$(touch /tmp/untrusted)"},
    {**TEST_CONFIG, "S3_BUCKET_NAME": "bad bucket"},
    {**TEST_CONFIG, "S3_BUCKET_NAME": 'bad"bucket'},
    {**TEST_CONFIG, "S3_BUCKET_NAME": "bad%nbucket"},
    {**TEST_CONFIG, "S3_BUCKET_NAME": "ab"},
    {**TEST_CONFIG, "S3_BUCKET_NAME": "bad..bucket"},
    {**TEST_CONFIG, "S3_PRESIGNED_URL_EXPIRE_SECONDS": "0"},
    {**TEST_CONFIG, "S3_PRESIGNED_URL_EXPIRE_SECONDS": "3601"},
    {**TEST_CONFIG, "S3_PRESIGNED_URL_EXPIRE_SECONDS": False},
    {**TEST_CONFIG, "S3_PRESIGNED_URL_EXPIRE_SECONDS": 0},
    {**TEST_CONFIG, "AWS_ACCESS_KEY_ID": "not-allowed"},
])
def test_invalid_or_injected_values_are_rejected(config):
    with pytest.raises(ValueError):
        configure_s3.validate_config(config)


def test_install_config_uses_private_temp_file_and_no_values_in_command_arguments(monkeypatch):
    commands = []
    temporary_paths = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["check"]
        assert kwargs["stdout"] is subprocess.DEVNULL and kwargs["stderr"] is subprocess.DEVNULL
        assert not any(value in argument for value in TEST_CONFIG.values() for argument in command)
        if command[:4] == ["sudo", "install", "-m", "0600"]:
            source = Path(command[4])
            temporary_paths.append(source)
            assert source.stat().st_mode & 0o777 == 0o600
            content = source.read_text()
            assert content.startswith("[Service]\n")
            assert 'Environment="S3_BUCKET_NAME=moa-test-only-bucket"' in content
            assert "DATABASE_URL" not in content and "JWT_SECRET_KEY" not in content
    monkeypatch.setattr(configure_s3.subprocess, "run", run)
    configure_s3.install_config(TEST_CONFIG)
    assert commands[0] == ["sudo", "install", "-d", "-m", "0755", str(configure_s3.DROPIN_DIRECTORY)]
    assert commands[1][-1] == str(configure_s3.DROPIN_FILE)
    assert temporary_paths and all(not path.exists() for path in temporary_paths)


def test_export_mode_serializes_only_allowed_settings():
    env = {**os.environ, **TEST_CONFIG, "AWS_SECRET_ACCESS_KEY": "not-to-be-transferred"}
    result = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/configure_s3.py"), "--export"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0 and not result.stderr
    assert json.loads(result.stdout) == TEST_CONFIG
    assert "not-to-be-transferred" not in result.stdout


def test_export_missing_secrets_fails_without_exposing_other_values():
    env = {**os.environ, **TEST_CONFIG, "AWS_REGION": ""}
    result = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/configure_s3.py"), "--export"],
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0 and not result.stdout
    assert TEST_CONFIG["S3_BUCKET_NAME"] not in result.stderr


def test_server_rejects_payload_without_running_sudo():
    result = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/configure_s3.py")],
                            input=json.dumps({**TEST_CONFIG, "S3_BUCKET_NAME": 'invalid"bucket'}), capture_output=True, text=True)
    assert result.returncode != 0
    assert 'invalid"bucket' not in result.stderr + result.stdout
    assert "Traceback" not in result.stderr


def test_workflow_passes_secrets_via_ssh_stdin_not_arguments(tmp_path):
    workflow = yaml.load((PROJECT_ROOT / ".github/workflows/deploy.yml").read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["deploy"]
    assert job["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert job["steps"][0]["uses"] == "actions/checkout@v4"
    step = job["steps"][-1]
    for key in configure_s3.CONFIG_KEYS:
        assert step["env"][key] == "${{ secrets." + key + " }}"
    # 실제 workflow의 전달 부분만 실행한다. SSH·키 파일·실제 서버는 사용하지 않는다.
    pipeline = step["run"][step["run"].index("python3 scripts/configure_s3.py --export"):]
    command_dir = tmp_path / "commands"
    command_dir.mkdir()
    capture = tmp_path / "payload.json"
    arguments = tmp_path / "arguments"
    fake_ssh = command_dir / "ssh"
    fake_ssh.write_text('#!/bin/bash\nset -eu\nprintf "%s\\n" "$@" > "$S3_TEST_ARGUMENTS"\ncat > "$S3_TEST_PAYLOAD"\n')
    fake_ssh.chmod(0o700)
    env = {**os.environ, **TEST_CONFIG, "SSH_USER": "test-user", "SSH_HOST": "example.test",
           "S3_TEST_PAYLOAD": str(capture), "S3_TEST_ARGUMENTS": str(arguments),
           "PATH": str(command_dir) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", pipeline], cwd=PROJECT_ROOT,
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(capture.read_text()) == TEST_CONFIG
    assert "--configure-s3" in arguments.read_text()
    assert TEST_CONFIG["S3_BUCKET_NAME"] not in arguments.read_text() + result.stdout + result.stderr
    assert "AWS_SECRET_ACCESS_KEY" not in capture.read_text()
