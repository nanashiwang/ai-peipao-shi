#!/usr/bin/env bash
# ============================================================
# 服务器一键部署（Docker 版）
# 作用：自动安装 Docker → 检查私密配置 → 构建并启动后端+数据库 → 健康检查
# 用法：在项目 main/ 目录下执行
#       sudo bash deploy/docker-deploy.sh
# 适用：Ubuntu / Debian / CentOS 等主流 Linux（需 root 或 sudo）
# ============================================================
set -euo pipefail

# 切到 main/ 目录（脚本在 main/deploy/ 下）
cd "$(cd "$(dirname "$0")/.." && pwd)"
echo "项目目录: $(pwd)"
echo ""

echo "== 1/5 检查 / 安装 Docker =="
if ! command -v docker >/dev/null 2>&1; then
  echo "未检测到 Docker，使用官方脚本安装..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker || true
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: 缺少 docker compose v2（需要 Docker 20.10+）。请升级 Docker 后重试。"
  exit 1
fi
docker --version
echo ""

echo "== 2/5 准备配置目录 =="
mkdir -p config
touch .env
chmod 600 .env || true

random_hex_secret() {
  local first second
  first="$(tr -d '-' </proc/sys/kernel/random/uuid)"
  second="$(tr -d '-' </proc/sys/kernel/random/uuid)"
  printf '%s%s' "$first" "$second"
}

env_value() {
  local key="$1"
  grep -E "^${key}=" .env | tail -n 1 | cut -d= -f2-
}

replace_env_key() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp)"
  awk -v key="$key" -v value="$value" '
    BEGIN { replaced = 0 }
    index($0, key "=") == 1 {
      if (!replaced) {
        print key "=" value
        replaced = 1
      }
      next
    }
    { print }
    END {
      if (!replaced) print key "=" value
    }
  ' .env > "$tmp"
  mv "$tmp" .env
}

ensure_env_key() {
  local key="$1"
  local value="$2"
  if ! grep -qE "^${key}=" .env; then
    printf '%s=%s\n' "$key" "$value" >> .env
    echo "  已生成 ${key}"
  fi
}

old_db_password=""
ensure_env_key "ADMIN_AUTH_REQUIRED" "true"
ensure_env_key "ADMIN_AUTH_SECRET" "$(random_hex_secret)"
if ! grep -qE "^POSTGRES_PASSWORD=" .env; then
  old_db_password="coach"
  ensure_env_key "POSTGRES_PASSWORD" "$(random_hex_secret)"
else
  current_db_password="$(env_value POSTGRES_PASSWORD)"
  if [ "$current_db_password" = "coach" ] || [ "$current_db_password" = "change-me-before-production" ] || [ "$current_db_password" = "change-me" ]; then
    old_db_password="$current_db_password"
    replace_env_key "POSTGRES_PASSWORD" "$(random_hex_secret)"
    echo "  已轮换弱 POSTGRES_PASSWORD"
  fi
fi
echo "  ARK 云端定位密钥改为「部署后在看板 → 系统设置 页在线配置」，这里无需填写。"
echo "  默认 APP_ENV=pilot；正式环境请在 .env 设置 APP_ENV=production、ADMIN_USERNAME/ADMIN_PASSWORD，并通过 ARK_* 环境变量或 config/ark.json 配置模型。"
echo ""

echo "== 3/5 构建并启动（api + postgres）=="
docker compose up -d postgres
if [ -n "$old_db_password" ]; then
  echo "  检测到历史弱数据库口令，尝试旋转 PostgreSQL 用户密码..."
  for i in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -U "${POSTGRES_USER:-coach}" -d "${POSTGRES_DB:-coach_mvp}" >/dev/null 2>&1; then break; fi
    sleep 2
  done
  new_db_password="$(env_value POSTGRES_PASSWORD)"
  if docker compose exec -T -e PGPASSWORD="$old_db_password" postgres psql \
    -U "${POSTGRES_USER:-coach}" \
    -d "${POSTGRES_DB:-coach_mvp}" \
    -c "ALTER USER \"${POSTGRES_USER:-coach}\" WITH PASSWORD '$new_db_password';" >/dev/null 2>&1; then
    echo "  PostgreSQL 用户密码已轮换"
  else
    echo "  未能用历史口令连接数据库；若这是全新库可忽略，否则请手动检查 PostgreSQL 密码。"
  fi
fi
docker compose up -d --build --force-recreate api tls-proxy health-probe
echo ""

echo "== 4/5 等待后端就绪 =="
ok=""
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
if [ -z "$ok" ]; then
  echo "❌ 健康检查失败，最近日志："
  docker compose logs --tail=50 api
  exit 1
fi
echo "health = ok"
tls_port="${TLS_HTTPS_PORT:-9443}"
if curl -kfsS "https://127.0.0.1:${tls_port}/health" >/dev/null 2>&1; then
  echo "tls health = ok (https://127.0.0.1:${tls_port})"
else
  echo "WARN: TLS 入口健康检查未通过，请检查 tls-proxy 日志和端口占用。"
fi
echo ""

echo "== 5/5 完成 =="
docker compose ps
echo ""
echo "✅ 后端已就绪：https://<服务器公网IP>:${tls_port}"
echo "   - 总控看板 + 设备监控 + 接入包下载都在这个地址"
echo "   - 【重要】首次部署后，进看板「系统设置」填入阿里 ARK 密钥（被控端云端定位需要）"
echo "   - 被控端 config 里的 api_base_url 要填这个公网地址"
echo ""
echo "常用命令："
echo "   查看日志:  docker compose logs -f api"
echo "   查看 TLS 反代日志: docker compose logs -f tls-proxy"
echo "   查看健康探针日志: docker compose logs -f health-probe"
echo "   重启:      docker compose restart api"
echo "   停止:      docker compose down"
echo "   更新代码后重新部署:  git pull && sudo bash deploy/docker-deploy.sh"
echo ""
echo "提示：默认 FastAPI 明文 8000 仅监听 127.0.0.1，公网请走 TLS 入口。"
echo "      如需标准 443 + 可信证书，请先把域名解析到服务器并释放 443，再调整 TLS_HTTPS_PORT/反代配置。"
