#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/KeepTouch}"
APP_DIR="${APP_DIR:-$REPO_DIR/chongqing-coach-mvp}"
BRANCH="${BRANCH:-master}"
REMOTE="${REMOTE:-origin}"

cd "$REPO_DIR"

echo "== Pull latest code =="
git fetch "$REMOTE" "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only "$REMOTE" "$BRANCH"

echo "== Check private config =="
if [ ! -f "$APP_DIR/config/ark.json" ]; then
  echo "ERROR: missing $APP_DIR/config/ark.json"
  echo "Create it from config/ark.example.json and fill api_key / endpoint_id."
  exit 1
fi

echo "== Install server dependencies =="
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.server.txt"

echo "== Install service files =="
install -m 0644 "$APP_DIR/deploy/chongqing-coach.service" /etc/systemd/system/chongqing-coach.service
if command -v nginx >/dev/null 2>&1; then
  install -m 0644 "$APP_DIR/deploy/nginx.yihe.site.conf" /etc/nginx/conf.d/yihe.site.conf
fi

echo "== Restart services =="
systemctl daemon-reload
systemctl enable chongqing-coach.service >/dev/null
systemctl restart chongqing-coach.service

echo "== Wait for app health =="
for attempt in {1..20}; do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null; then
    echo "health=ok"
    break
  fi
  if [ "$attempt" -eq 20 ]; then
    echo "ERROR: app health check failed"
    journalctl -u chongqing-coach.service -n 40 --no-pager
    exit 1
  fi
  sleep 1
done

if command -v nginx >/dev/null 2>&1; then
  nginx -t
  systemctl enable nginx >/dev/null
  systemctl reload nginx || systemctl restart nginx
fi

echo "== Status =="
systemctl --no-pager --full status chongqing-coach.service | sed -n '1,12p'
if command -v nginx >/dev/null 2>&1; then
  systemctl --no-pager --full status nginx | sed -n '1,8p'
fi
