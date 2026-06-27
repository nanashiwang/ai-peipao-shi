#!/usr/bin/env bash
# ============================================================
# 裸服务器「一条命令」一键部署引导
# 作用：在全新 Linux 服务器上自动：装 git/docker → 拉代码 → 起服务
# 用法（root 或 sudo）：
#   方式一（仓库公开，一条命令）：
#     curl -fsSL https://raw.githubusercontent.com/nanashiwang/ai-peipao-shi/main/main/deploy/bootstrap.sh | bash
#   方式二（下载后执行）：
#     bash bootstrap.sh
# 可用环境变量覆盖：REPO_URL / TARGET_DIR / BRANCH
# 部署完成后：进看板「系统设置」在线填入阿里 ARK 密钥（被控端云端定位用）
# ============================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/nanashiwang/ai-peipao-shi.git}"
TARGET_DIR="${TARGET_DIR:-/opt/ai-peipao-shi}"
BRANCH="${BRANCH:-main}"

echo "== 1/3 安装 git =="
if ! command -v git >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then apt-get update -y && apt-get install -y git
  elif command -v dnf >/dev/null 2>&1; then dnf install -y git
  elif command -v yum >/dev/null 2>&1; then yum install -y git
  else echo "ERROR: 未识别的包管理器，请先手动安装 git 后重试"; exit 1; fi
fi
git --version

echo "== 2/3 拉取代码到 $TARGET_DIR =="
if [ -d "$TARGET_DIR/.git" ]; then
  git -C "$TARGET_DIR" pull --ff-only origin "$BRANCH" || true
else
  git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$TARGET_DIR"
fi

echo "== 3/3 执行部署（装 Docker + 起服务）=="
cd "$TARGET_DIR/main"
exec bash deploy/docker-deploy.sh
