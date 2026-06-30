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
echo "  ARK 云端定位密钥改为「部署后在看板 → 系统设置 页在线配置」，这里无需填写。"
echo "  默认 APP_ENV=pilot；正式环境请先创建 .env，设置 APP_ENV=production、ADMIN_AUTH_SECRET、ADMIN_USERNAME/ADMIN_PASSWORD、独立数据库口令，并准备 config/ark.json。"
echo ""

echo "== 3/5 构建并启动（api + postgres）=="
docker compose up -d --build --force-recreate api
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
echo ""

echo "== 5/5 完成 =="
docker compose ps
echo ""
echo "✅ 后端已就绪：http://<服务器公网IP>:8000"
echo "   - 总控看板 + 设备监控 + 接入包下载都在这个地址"
echo "   - 【重要】首次部署后，进看板「系统设置」填入阿里 ARK 密钥（被控端云端定位需要）"
echo "   - 被控端 config 里的 api_base_url 要填这个公网地址"
echo ""
echo "常用命令："
echo "   查看日志:  docker compose logs -f api"
echo "   重启:      docker compose restart api"
echo "   停止:      docker compose down"
echo "   更新代码后重新部署:  git pull && sudo bash deploy/docker-deploy.sh"
echo ""
echo "提示：如需对外用 80/443 + 域名，请在前面挂一层 nginx 反代到 127.0.0.1:8000"
echo "      （参考 deploy/nginx.yihe.site.conf）。"
