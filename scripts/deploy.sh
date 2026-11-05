#!/usr/bin/env bash
# 서버(~/moa-backend)에서 실행되는 배포 스크립트. GitHub Actions가 SSH로 호출한다.
set -euo pipefail

APP_DIR="$HOME/moa-backend"
SERVICE="moa-backend"

cd "$APP_DIR"
git fetch --quiet origin main
git reset --hard origin/main

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt

# DB 스키마 마이그레이션. alembic은 app.core.config를 통해 DATABASE_URL/JWT_SECRET_KEY를
# 읽는데, 이 스크립트는 사용자 셸이라 해당 값이 환경에 없다. 서비스에 주입된
# systemd drop-in에서 그대로 읽어와 적용한다.
DROPIN=/etc/systemd/system/"$SERVICE".service.d/db.conf
if [ -f "$DROPIN" ]; then
  export DATABASE_URL=$(sudo sed -n 's/^Environment=DATABASE_URL=//p' "$DROPIN")
  export JWT_SECRET_KEY=$(sudo sed -n 's/^Environment=JWT_SECRET_KEY=//p' "$DROPIN")
  .venv/bin/alembic upgrade head
else
  echo "warning: $DROPIN 없음 - 마이그레이션을 건너뛴다" >&2
fi

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
