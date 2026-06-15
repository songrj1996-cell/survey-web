#!/usr/bin/env bash
# survey-web 部署脚本（Ubuntu 20/22，Python 3.10+，systemd）
# 在服务器上 git clone 或 scp 代码后，以 root 运行：sudo ./deploy.sh
set -euo pipefail

# ── 可修改配置 ────────────────────────────────────────────────
DEPLOY_DIR="/opt/survey-web"   # 部署目录
SERVICE_NAME="survey-web"      # systemd 服务名
APP_PORT="18081"               # 监听端口
SERVICE_USER="www-data"        # 运行服务的系统用户
# ─────────────────────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
  echo "请用 root 或 sudo 运行本脚本：sudo ./deploy.sh" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "▶ 源码目录：$SRC_DIR"
echo "▶ 部署目录：$DEPLOY_DIR"

# 1. 系统依赖
echo ""
echo "==> [1/6] 安装系统依赖…"
apt-get update -qq
apt-get install -y python3 python3-venv python3-pip nginx fonts-noto-cjk fonts-wqy-microhei fontconfig
fc-cache -f >/dev/null || true

# 2. 同步代码
echo ""
echo "==> [2/6] 同步代码到 $DEPLOY_DIR…"
mkdir -p "$DEPLOY_DIR"
rsync -a --delete \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='data/' \
  --exclude='.env' \
  "$SRC_DIR/" "$DEPLOY_DIR/"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DEPLOY_DIR"

# 3. Python 虚拟环境 + 依赖
echo ""
echo "==> [3/6] 创建 Python venv 并安装依赖…"
python3 -m venv "$DEPLOY_DIR/.venv"
"$DEPLOY_DIR/.venv/bin/pip" install -q --upgrade pip
"$DEPLOY_DIR/.venv/bin/pip" install -r "$DEPLOY_DIR/requirements.txt"

# 4. 数据目录 & .env
echo ""
echo "==> [4/6] 准备 data 目录和 .env…"
mkdir -p "$DEPLOY_DIR/data"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DEPLOY_DIR/data"

if [[ ! -f "$DEPLOY_DIR/.env" ]]; then
  cp "$DEPLOY_DIR/.env.example" "$DEPLOY_DIR/.env"
  chmod 600 "$DEPLOY_DIR/.env"
  chown "$SERVICE_USER:$SERVICE_USER" "$DEPLOY_DIR/.env"
  echo ""
  echo "  ⚠️  已从 .env.example 创建 $DEPLOY_DIR/.env"
  echo "     请先编辑填入真实的 API Key，再启动服务！"
  echo "     命令：sudo nano $DEPLOY_DIR/.env"
  echo ""
  ENV_NEEDS_EDIT=1
else
  echo "  .env 已存在，跳过创建"
  ENV_NEEDS_EDIT=0
fi

# 5. systemd 服务
echo ""
echo "==> [5/6] 安装 systemd 服务 $SERVICE_NAME…"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Survey Insight Web Platform
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${DEPLOY_DIR}
EnvironmentFile=${DEPLOY_DIR}/.env
ExecStart=${DEPLOY_DIR}/.venv/bin/uvicorn server:app \\
    --host 0.0.0.0 \\
    --port ${APP_PORT} \\
    --workers 2 \\
    --timeout-keep-alive 75
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

if [[ "${ENV_NEEDS_EDIT:-0}" == "1" ]]; then
  echo "  ⚠️  .env 尚未配置，暂不启动服务"
else
  systemctl restart "$SERVICE_NAME"
  sleep 2
  echo ""
  echo "==> 服务状态："
  systemctl status "$SERVICE_NAME" --no-pager -l || true
fi

# 6. Nginx 配置提示
echo ""
echo "==> [6/6] Nginx 配置…"
NGINX_CONF="/etc/nginx/sites-available/${SERVICE_NAME}"
if [[ ! -f "$NGINX_CONF" ]]; then
  cp "$DEPLOY_DIR/nginx-survey.conf" "$NGINX_CONF"
  echo "  已复制 Nginx 配置到 $NGINX_CONF"
  echo "  请根据实际域名/IP 修改后执行："
  echo "    sudo nano $NGINX_CONF"
  echo "    sudo ln -s $NGINX_CONF /etc/nginx/sites-enabled/"
  echo "    sudo nginx -t && sudo systemctl reload nginx"
else
  echo "  Nginx 配置已存在，跳过覆盖"
fi

echo ""
echo "══════════════════════════════════════════════════"
if [[ "${ENV_NEEDS_EDIT:-0}" == "1" ]]; then
  echo "  📋 后续步骤："
  echo ""
  echo "  1. 编辑 .env："
  echo "     sudo nano $DEPLOY_DIR/.env"
  echo ""
  echo "  2. 启动服务："
  echo "     sudo systemctl start $SERVICE_NAME"
  echo ""
  echo "  3. 配置 Nginx 并启用（见下方）"
else
  echo "  ✅ 服务已启动：http://127.0.0.1:${APP_PORT}"
  echo ""
  echo "  📋 后续步骤（Nginx）："
fi
echo ""
echo "  配置 Nginx："
echo "    sudo nano /etc/nginx/sites-available/${SERVICE_NAME}"
echo "    sudo ln -s /etc/nginx/sites-available/${SERVICE_NAME} /etc/nginx/sites-enabled/"
echo "    sudo nginx -t && sudo systemctl reload nginx"
echo ""
echo "  查看日志："
echo "    sudo journalctl -u $SERVICE_NAME -f"
echo "══════════════════════════════════════════════════"
