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
