#!/usr/bin/env bash
# =============================================================================
# AgentChat 公网隧道启动脚本（零安装，仅依赖 ssh）
#
# 用途：把本机 AgentChat 前端（默认 8090）通过 SSH 反向隧道暴露到公网。
#
# 用法：
#   ./start_tunnel.sh                     # serveo，自动重连
#   ./start_tunnel.sh pinggy              # 换用 pinggy.io
#   ./start_tunnel.sh localhostrun        # 换用 localhost.run
#   SUBDOMAIN=agentchat ./start_tunnel.sh # serveo 固定子域名（需先登记 SSH 公钥）
#   PORT=8090 ./start_tunnel.sh           # 指定本地端口
#
# ⚠️ 重要：免费隧道的地址是**短时**的
#   - serveo    : 端口转发约 10~30 分钟后 expire
#   - pinggy    : 免费隧道 60 分钟过期
#   - localhost.run : 中国大陆网络下边缘节点常返回 403，不可用
#   因此本脚本内置**自动重连**：断开后自动重建，新地址会打印在下方并写入
#   同目录的 tunnel_url.txt。若需要**固定不变**的地址，请见文末说明。
# =============================================================================
set -uo pipefail

PORT="${PORT:-8090}"
SERVICE="${1:-serveo}"
SUBDOMAIN="${SUBDOMAIN:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
URL_FILE="${SCRIPT_DIR}/tunnel_url.txt"

SSH_COMMON=(
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
)

banner() {
  echo "=============================================="
  echo " AgentChat 公网隧道"
  echo " 本地目标: http://localhost:${PORT}"
  echo " 隧道服务: ${SERVICE}"
  [ -n "$SUBDOMAIN" ] && echo " 固定子域: ${SUBDOMAIN}"
  echo " 地址会写入: ${URL_FILE}"
  echo " 按 Ctrl+C 停止"
  echo "=============================================="
}

# 从隧道服务输出中提取公网地址
extract_url() {
  case "$SERVICE" in
    serveo)       grep -aoE "https://[a-zA-Z0-9._-]+\.serveousercontent\.com" ;;
    localhostrun) grep -aoE "https://[a-zA-Z0-9._-]+\.lhr\.life" ;;
    pinggy)       grep -aoE "https://[a-zA-Z0-9._-]+\.(pinggy\.net|pinggy-free\.link)" ;;
  esac | head -1
}

# 构造 ssh 参数
build_args() {
  case "$SERVICE" in
    serveo)
      if [ -n "$SUBDOMAIN" ]; then
        echo "-R" "${SUBDOMAIN}:80:localhost:${PORT}" "serveo.net"
      else
        echo "-R" "80:localhost:${PORT}" "serveo.net"
      fi
      ;;
    localhostrun) echo "-R" "80:localhost:${PORT}" "nokey@localhost.run" ;;
    pinggy)       echo "-p" "443" "-R0:localhost:${PORT}" "a.pinggy.io" ;;
  esac
}

banner
echo ""

if ! curl -s -o /dev/null --noproxy '*' --max-time 5 "http://127.0.0.1:${PORT}" 2>/dev/null; then
  echo "[警告] http://127.0.0.1:${PORT} 无响应。"
  echo "       请先启动容器: cd docker && docker compose up -d"
  echo ""
fi

if [ "$SERVICE" = "serveo" ]; then
  echo "提示: serveo 免费版首次访问会显示一个浏览器提示页，点击继续即可。"
fi
if [ "$SERVICE" = "pinggy" ]; then
  echo "提示: pinggy 免费隧道 60 分钟后过期（本脚本会自动重连）。"
fi
echo ""

trap 'echo ""; echo "已停止隧道。"; exit 0' INT

# ---------- 自动重连主循环 ----------
ROUND=0
while true; do
  ROUND=$((ROUND + 1))
  echo "[$(date +%H:%M:%S)] 第 ${ROUND} 次建立隧道..."

  # 逐行读取 ssh 输出，实时打印并捕获地址
  # shellcheck disable=SC2046
  ssh "${SSH_COMMON[@]}" $(build_args) 2>&1 | while IFS= read -r line; do
    echo "$line"
    url="$(printf '%s' "$line" | extract_url)"
    if [ -n "$url" ]; then
      printf '%s\n' "$url" > "$URL_FILE"
      echo ""
      echo "  >>> 公网地址: ${url}"
      echo "  >>> 已写入: ${URL_FILE}"
      echo ""
    fi
  done

  echo "[$(date +%H:%M:%S)] 隧道已断开（免费版转发有时长限制）。5 秒后自动重连..."
  echo ""
  sleep 5
done

# =============================================================================
# 如何获得「固定不变」的公网地址
#
# serveo 支持保留子域名，但需要先把你的 SSH 公钥登记到 serveo：
#   1) 生成密钥（若已有可跳过）: ssh-keygen -t ed25519
#   2) 查看公钥:                cat ~/.ssh/id_ed25519.pub
#   3) 到 https://console.serveo.net/ 登记该公钥
#   4) 用固定子域启动:          SUBDOMAIN=你的名字 ./start_tunnel.sh
#      之后地址恒为 https://你的名字.serveousercontent.com
#
# 需要「长期稳定、可对外正式使用」的地址，正确做法是部署到云服务器，
# 而不是依赖免费隧道。
# =============================================================================
