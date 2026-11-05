#!/usr/bin/env bash
# 서버(~/moa-backend)에서 실행되는 배포 스크립트. GitHub Actions가 SSH로 호출한다.
set -euo pipefail

APP_DIR="${MOA_APP_DIR:-$HOME/moa-backend}"
SERVICE="moa-backend"

# Git/pip가 SSH stdin을 소비하기 전에 설정 payload를 권한 제한 임시 파일에 보관한다.
if [ "${1:-}" = "--configure-s3" ] || [ "${1:-}" = "--env-file" ]; then
  umask 077
  CONFIG_FILE=$(mktemp)
  trap 'rm -f "$CONFIG_FILE"' EXIT
  cat > "$CONFIG_FILE"
elif [ "$#" -ne 0 ]; then
  echo "지원하지 않는 배포 옵션입니다." >&2
  exit 1
fi

cd "$APP_DIR"
if [ "${1:-}" = "--configure-s3" ] && [ -f .env.systemd ]; then
  echo "ENV_FILE 도입 이후에는 --env-file 방식으로 전체 설정을 전달하세요." >&2
  exit 1
fi
git fetch --quiet origin main
git reset --hard origin/main

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt

ENV_FILE_MODE=0
if [ "${1:-}" = "--env-file" ] || [ -f .env.systemd ]; then
  ENV_FILE_MODE=1
  echo "DB migration start"
  if [ "${1:-}" = "--env-file" ]; then
    .venv/bin/python scripts/configure_env.py < "$CONFIG_FILE"
  else
    .venv/bin/python scripts/configure_env.py < .env
  fi
  echo "DB migration complete"
else
# 기존 수동 배포 모드: systemd drop-in을 우선 사용한다. 파일이 없으면 실행 환경 또는
# app.core.config가 읽는 프로젝트 .env를 사용하되 마이그레이션은 항상 실행한다.
DROPIN="${MOA_SERVICE_ENV_FILE:-/etc/systemd/system/$SERVICE.service.d/db.conf}"
if [ -f "$DROPIN" ]; then
  # export 안에서 명령을 실행하면 sudo/sed 실패가 가려지므로 대입과 export를 분리한다.
  DATABASE_URL=$(sudo sed -n 's/^Environment=DATABASE_URL=//p' "$DROPIN")
  JWT_SECRET_KEY=$(sudo sed -n 's/^Environment=JWT_SECRET_KEY=//p' "$DROPIN")
  export DATABASE_URL JWT_SECRET_KEY
fi

echo "DB migration start"
# 설정 누락·접속 오류·DDL 실패는 set -e로 배포를 중단해 재시작을 막는다.
.venv/bin/alembic upgrade head
echo "DB migration complete"
fi

if [ "${1:-}" = "--configure-s3" ]; then
  .venv/bin/python scripts/configure_s3.py < "$CONFIG_FILE"
fi
if [ "${1:-}" = "--configure-s3" ] || [ "$ENV_FILE_MODE" = "1" ]; then
  sudo systemctl daemon-reload
fi

sudo systemctl restart "$SERVICE"
# 설치되어 실행 중인 worker도 동일한 설정으로 재시작한다. 미설치 worker를 새로 시작하지 않는다.
RESTARTED_WORKERS=""
for worker in moa-documents-worker moa-github-worker; do
  if sudo systemctl is-active --quiet "$worker"; then
    sudo systemctl restart "$worker"
    RESTARTED_WORKERS="$RESTARTED_WORKERS $worker"
  fi
done

# 헬스체크: 기동될 때까지 최대 ~20초 대기
for i in $(seq 1 10); do
  if curl -fsS -o /dev/null http://127.0.0.1:8000/docs; then
    # 고정된 서비스 이름만 저장하므로 Bash 3의 빈 배열/nounset 문제 없이 순회한다.
    for worker in $RESTARTED_WORKERS; do
      if ! sudo systemctl is-active --quiet "$worker"; then
        echo "$worker health check failed" >&2
        exit 1
      fi
    done
    echo "deploy ok ($(git rev-parse --short HEAD))"
    exit 0
  fi
  sleep 2
done

echo "health check failed" >&2
sudo journalctl -u "$SERVICE" -n 30 --no-pager >&2
exit 1
