#!/usr/bin/env bash
# 서버(~/moa-backend)에서 실행되는 배포 스크립트. GitHub Actions가 SSH로 호출한다.
set -euo pipefail

APP_DIR="${MOA_APP_DIR:-$HOME/moa-backend}"
SERVICE="moa-backend"

cd "$APP_DIR"
git fetch --quiet origin main
git reset --hard origin/main

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt

# 기존 서버의 systemd drop-in을 우선 사용한다. 파일이 없으면 실행 환경 또는
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

sudo systemctl restart "$SERVICE"

# 헬스체크: 기동될 때까지 최대 ~20초 대기
for i in $(seq 1 10); do
  if curl -fsS -o /dev/null http://127.0.0.1:8000/docs; then
    echo "deploy ok ($(git rev-parse --short HEAD))"
    exit 0
  fi
  sleep 2
done

echo "health check failed" >&2
sudo journalctl -u "$SERVICE" -n 30 --no-pager >&2
exit 1
