#!/bin/sh
# =============================================================================
# AgentChat 生产容器入口
#
# 职责只有两件事：
#   1. 校验生产配置已通过挂载就位（缺失时给出可执行的排查提示，而不是让
#      settings.py 静默回退到默认值、跑到连数据库时才炸）；
#   2. exec 启动 uvicorn（用 exec 让 PID 1 变成 uvicorn，SIGTERM 能被正确接收，
#      docker stop 才会优雅退出而不是等 10 秒后 SIGKILL）。
#
# 为什么需要第 1 步（实测依据）：
#   agentchat/settings.py 的 init_app_settings() 用 try/except 包住配置读取，
#   读不到文件只打一行 error 日志就继续启动。于是「忘记挂载配置」的表现是
#   「容器起来了、首页能开、登录 500」，排查方向极易跑偏。
#
# 可用环境变量覆盖（默认值即生产布局，一般无需设置）：
#   APP_DIR          应用根目录，默认 /app
#   AGENTCHAT_CONFIG 配置文件绝对路径，默认 $APP_DIR/agentchat/config.yaml
#   UVICORN_BIN      uvicorn 可执行文件，默认 $APP_DIR/.venv/bin/uvicorn
#   UVICORN_WORKERS  worker 数，默认 2
# =============================================================================
set -eu

APP_DIR="${APP_DIR:-/app}"
CONFIG="${AGENTCHAT_CONFIG:-$APP_DIR/agentchat/config.yaml}"
UVICORN_BIN="${UVICORN_BIN:-$APP_DIR/.venv/bin/uvicorn}"
WORKERS="${UVICORN_WORKERS:-2}"

if [ ! -f "$CONFIG" ]; then
    echo "[FATAL] 缺少生产配置：$CONFIG" >&2
    echo "" >&2
    echo "  该文件由 docker/deploy.sh 从 docker_config.prod.template.yaml + docker/.env" >&2
    echo "  渲染生成，并由 docker-compose.prod.yml 以只读方式挂载到容器内。" >&2
    echo "" >&2
    echo "  排查顺序：" >&2
    echo "    1) docker/ 目录下是否存在 docker_config.prod.generated.yaml" >&2
    echo "    2) 若不存在，执行： cd docker && ./deploy.sh" >&2
    echo "    3) 若存在却仍报此错，检查 volumes 挂载路径是否被改过" >&2
    echo "" >&2
    echo "  ⚠️ 不要用「往镜像里 COPY 一份配置」的方式绕过：那会让镜像带着旧口令/旧 Key，" >&2
    echo "     且改一个 Key 就要重建镜像（uv sync + Chrome 下载，十几分钟）。" >&2
    exit 1
fi

# 配置可读性再确认一次：挂载成目录（源文件不存在时 Docker 会自动创建同名
# 目录）时，-f 判断为假已在上一步拦住；这里兜住「存在但不可读」的权限问题。
if [ ! -r "$CONFIG" ]; then
    echo "[FATAL] 生产配置存在但不可读：$CONFIG（检查文件权限，应为 600 且属主可读）" >&2
    exit 1
fi

echo "[entrypoint] 配置就绪：$CONFIG"
echo "[entrypoint] 启动 uvicorn，workers=$WORKERS"

exec "$UVICORN_BIN" agentchat.main:app \
    --host 0.0.0.0 \
    --port 7860 \
    --workers "$WORKERS" \
    --proxy-headers \
    --forwarded-allow-ips '*'
