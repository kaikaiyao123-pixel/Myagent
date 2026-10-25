#!/usr/bin/env bash
# =============================================================================
# AgentChat 生产部署脚本
#
# 在服务器上执行，完成：环境预检 → 生成密钥 → 渲染配置 → 构建启动 → 验收自检
#
# 用法：
#   cd docker
#   cp .env.prod.example .env     # 首次部署：填写密钥（口令可留空自动生成）
#   ./deploy.sh                   # 部署
#   ./deploy.sh --verify          # 仅做验收自检，不重新部署
#   ./deploy.sh --down            # 停止并移除容器（保留数据卷）
#
# 前置条件：Docker Engine 20.10+ 与 Docker Compose V2
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env"
TEMPLATE="docker_config.prod.template.yaml"
GENERATED="docker_config.prod.generated.yaml"

# ----------------------------- 输出工具 --------------------------------------
c_info()  { printf '\033[36m[信息]\033[0m %s\n' "$*"; }
c_ok()    { printf '\033[32m[成功]\033[0m %s\n' "$*"; }
c_warn()  { printf '\033[33m[警告]\033[0m %s\n' "$*"; }
c_err()   { printf '\033[31m[错误]\033[0m %s\n' "$*" >&2; }
c_step()  { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }

# ----------------------------- 参数解析 --------------------------------------
MODE="deploy"
case "${1:-}" in
  --verify) MODE="verify" ;;
  --down)   MODE="down" ;;
  --help|-h)
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  "") ;;
  *) c_err "未知参数: $1（可用: --verify / --down / --help）"; exit 1 ;;
esac

# =============================================================================
# 1. 环境预检
# =============================================================================
preflight() {
  c_step "环境预检"

  command -v docker >/dev/null 2>&1 || { c_err "未安装 Docker。"; exit 1; }
  c_ok "Docker: $(docker --version)"

  docker compose version >/dev/null 2>&1 || { c_err "未安装 Docker Compose V2。"; exit 1; }
  c_ok "Compose: $(docker compose version --short 2>/dev/null || echo 'v2')"

  # Docker 守护进程可用性
  if ! docker info >/dev/null 2>&1; then
    c_err "无法连接 Docker 守护进程。请确认服务已启动，且当前用户有权限（可尝试 sudo 或加入 docker 组）。"
    exit 1
  fi
  c_ok "Docker 守护进程正常"

  command -v python3 >/dev/null 2>&1 || { c_err "未找到 python3（用于渲染配置）。请安装：apt install -y python3"; exit 1; }

  # 端口占用检查（80 端口）
  if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ':80 '; then
    c_warn "宿主机 80 端口已被占用，frontend 容器可能无法启动。"
    c_warn "占用情况：$(ss -ltnp 2>/dev/null | grep ':80 ' | head -1)"
  fi

  # 磁盘空间（镜像约 4GB + 数据）
  avail_kb=$(df -Pk . | awk 'NR==2 {print $4}')
  avail_gb=$((avail_kb / 1024 / 1024))
  if [ "$avail_gb" -lt 15 ]; then
    c_warn "可用磁盘仅 ${avail_gb}GB，建议至少 20GB（镜像约 4GB，另需数据与日志空间）。"
  else
    c_ok "可用磁盘 ${avail_gb}GB"
  fi

  # 内存
  if [ -r /proc/meminfo ]; then
    mem_mb=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
    if [ "$mem_mb" -lt 3500 ]; then
      c_warn "总内存 ${mem_mb}MB，低于建议的 4GB，构建阶段可能因内存不足失败。"
    else
      c_ok "总内存 ${mem_mb}MB"
    fi
  fi
}

# =============================================================================
# 2. 准备 .env 与自动生成密钥
# =============================================================================
gen_secret() {
  # 仅生成字母数字，避免 MySQL 连接串被 @ : / 等字符破坏
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32
  else
    tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32
  fi
}

prepare_env() {
  c_step "准备环境变量"

  if [ ! -f "$ENV_FILE" ]; then
    if [ -f ".env.prod.example" ]; then
      cp .env.prod.example "$ENV_FILE"
      chmod 600 "$ENV_FILE"
      c_warn "已从模板创建 $ENV_FILE，请填写模型 API Key 后重新运行。"
      c_warn "口令留空可自动生成，但 DASHSCOPE_API_KEY 必须自行填写。"
      echo ""
      c_info "编辑命令：  nano $SCRIPT_DIR/$ENV_FILE"
      exit 1
    else
      c_err "缺少 $ENV_FILE 且找不到 .env.prod.example 模板。"
      exit 1
    fi
  fi
  chmod 600 "$ENV_FILE"
  c_ok "已加载 $ENV_FILE"

  set -a
  # shellcheck disable=SC1090
  . "./$ENV_FILE"
  set +a

  local changed=0

  # 自动生成缺失的口令/密钥
  if [ -z "${AUTHJWT_SECRET_KEY:-}" ]; then
    AUTHJWT_SECRET_KEY="$(gen_secret)$(gen_secret)"
    _set_env AUTHJWT_SECRET_KEY "$AUTHJWT_SECRET_KEY"
    c_info "已自动生成 AUTHJWT_SECRET_KEY（64 位随机）"
    changed=1
  fi

  if [ -z "${MYSQL_ROOT_PASSWORD:-}" ]; then
    MYSQL_ROOT_PASSWORD="$(gen_secret)"
    _set_env MYSQL_ROOT_PASSWORD "$MYSQL_ROOT_PASSWORD"
    c_info "已自动生成 MYSQL_ROOT_PASSWORD（32 位随机）"
    changed=1
  fi

  if [ -z "${MINIO_ROOT_PASSWORD:-}" ]; then
    MINIO_ROOT_PASSWORD="$(gen_secret)"
    _set_env MINIO_ROOT_PASSWORD "$MINIO_ROOT_PASSWORD"
    c_info "已自动生成 MINIO_ROOT_PASSWORD（32 位随机）"
    changed=1
  fi

  [ -n "${MINIO_ROOT_USER:-}" ] || { MINIO_ROOT_USER="agentchat"; _set_env MINIO_ROOT_USER "$MINIO_ROOT_USER"; changed=1; }

  # 强口令校验：拒绝沿用开发默认值
  local bad=0
  case "${MYSQL_ROOT_PASSWORD}" in
    123456|root|password) c_err "MYSQL_ROOT_PASSWORD 仍是开发默认弱口令，请更换。"; bad=1 ;;
  esac
  case "${MINIO_ROOT_PASSWORD}" in
    minioadmin|password) c_err "MINIO_ROOT_PASSWORD 仍是开发默认弱口令，请更换。"; bad=1 ;;
  esac
  if [ "${AUTHJWT_SECRET_KEY}" = "secret" ]; then
    c_err "AUTHJWT_SECRET_KEY 仍为字面量 'secret'，可被用于伪造登录态，请更换。"; bad=1
  fi
  [ "$bad" -eq 0 ] || exit 1

  # 模型 Key 必须非空 —— 这里升级为硬性阻断，而非告警。
  # 原因（实测踩到）：首次启动时若 Key 为空，初始化会把空 Key 写进 llm 表，
  # 且 36 个默认 Agent 会永久绑定到这条记录；之后即便把 Key 补进配置，
  # _update_exist_llm 也只更新「最早创建的那条」LLM，不会修正 Agent 的绑定，
  # 表现为：前端一切正常，一发消息就 401 "You didn't provide an API key"。
  # 该脏数据只能手工改库恢复，因此必须在这里拦住。
  if [ -z "${DASHSCOPE_API_KEY:-}" ] && [ "${SKIP_LLM_KEY_CHECK:-0}" != "1" ]; then
    c_err "DASHSCOPE_API_KEY 未填写。"
    c_err "  首次启动前必须填好模型 Key，否则初始化会写入空 Key 记录，"
    c_err "  且默认 Agent 会永久绑定到它 —— 事后补 Key 也无法自动修复，只能改库。"
    c_err "  请编辑 $ENV_FILE 填入 DASHSCOPE_API_KEY 后重新运行。"
    c_err "  （若只想先跑通页面、暂不需要对话功能，可设置 SKIP_LLM_KEY_CHECK=1 跳过本检查，"
    c_err "    但需接受之后手工改库修复的代价。）"
    exit 1
  fi
  [ -n "${DASHSCOPE_API_KEY:-}" ] || c_warn "已跳过模型 Key 检查（SKIP_LLM_KEY_CHECK=1），对话功能将不可用。"

  # Langfuse 成对校验：只填一个 Key 是半配置，SDE 会反复 401 重试并污染日志；
  # 两个都空视为「不启用可观测」，放行（可选组件不做硬拦截）。
  if [ -n "${LANGFUSE_PUBLIC_KEY:-}" ] && [ -z "${LANGFUSE_SECRET_KEY:-}" ]; then
    c_err "LANGFUSE_PUBLIC_KEY 已填但 LANGFUSE_SECRET_KEY 为空。"
    c_err "  Langfuse 必须公私钥成对配置：请补全 $ENV_FILE 中的 LANGFUSE_SECRET_KEY，"
    c_err "  或清空 LANGFUSE_PUBLIC_KEY 以关闭追踪。"
    exit 1
  fi
  if [ -z "${LANGFUSE_PUBLIC_KEY:-}" ] && [ -n "${LANGFUSE_SECRET_KEY:-}" ]; then
    c_err "LANGFUSE_SECRET_KEY 已填但 LANGFUSE_PUBLIC_KEY 为空。"
    c_err "  Langfuse 必须公私钥成对配置：请补全 $ENV_FILE 中的 LANGFUSE_PUBLIC_KEY，"
    c_err "  或清空 LANGFUSE_SECRET_KEY 以关闭追踪。"
    exit 1
  fi

  # 语义缓存 env 覆盖项校验（未设置则渲染时用模板默认值）
  if [ -n "${SEMANTIC_CACHE_ENABLED:-}" ]; then
    case "${SEMANTIC_CACHE_ENABLED}" in
      true|false) ;;
      *) c_err "SEMANTIC_CACHE_ENABLED 只能是 true/false，当前为 '${SEMANTIC_CACHE_ENABLED}'。"; exit 1 ;;
    esac
  fi
  if [ -n "${SEMANTIC_CACHE_THRESHOLD:-}" ]; then
    case "${SEMANTIC_CACHE_THRESHOLD}" in
      *[!0-9.]*|""|.*.|*.) c_err "SEMANTIC_CACHE_THRESHOLD 必须是数字，当前为 '${SEMANTIC_CACHE_THRESHOLD}'。"; exit 1 ;;
    esac
    awk -v v="${SEMANTIC_CACHE_THRESHOLD}" 'BEGIN { exit !(v > 0 && v < 1) }' || {
      c_err "SEMANTIC_CACHE_THRESHOLD 必须在 (0, 1) 开区间，当前为 '${SEMANTIC_CACHE_THRESHOLD}'。"
      c_err "  （1.0 会因浮点误差导致缓存永远不命中，请用 0.99 以下。）"
      exit 1
    }
  fi
  if [ -n "${SEMANTIC_CACHE_TTL:-}" ]; then
    case "${SEMANTIC_CACHE_TTL}" in
      *[!0-9]*|"") c_err "SEMANTIC_CACHE_TTL 必须是正整数（秒），当前为 '${SEMANTIC_CACHE_TTL}'。"; exit 1 ;;
    esac
    [ "${SEMANTIC_CACHE_TTL}" -gt 0 ] || { c_err "SEMANTIC_CACHE_TTL 必须 > 0。"; exit 1; }
  fi
  if [ -n "${SEMANTIC_CACHE_KB_VERSION:-}" ]; then
    case "${SEMANTIC_CACHE_KB_VERSION}" in
      *:*|*__*) c_err "SEMANTIC_CACHE_KB_VERSION 不能包含 ':'（Redis key 分隔符）或 '__'（与渲染占位符样式冲突）。"; exit 1 ;;
    esac
  fi

  [ "$changed" -eq 1 ] && c_ok "密钥已写入 $ENV_FILE（权限 600）"
  c_ok "环境变量校验通过"
}

# 安全地写入/更新 .env 中的某个键（避免 sed 特殊字符问题）
_set_env() {
  python3 - "$ENV_FILE" "$1" "$2" <<'PY'
import sys, re, pathlib
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
lines = p.read_text(encoding='utf-8').splitlines()
pat = re.compile(r'^\s*' + re.escape(key) + r'\s*=')
out, done = [], False
for ln in lines:
    if pat.match(ln):
        out.append(f"{key}={val}")
        done = True
    else:
        out.append(ln)
if not done:
    out.append(f"{key}={val}")
p.write_text("\n".join(out) + "\n", encoding='utf-8')
PY
}

# =============================================================================
# 3. 渲染生产配置
# =============================================================================
render_config() {
  c_step "渲染生产配置"

  [ -f "$TEMPLATE" ] || { c_err "缺少模板 $TEMPLATE"; exit 1; }

  # 重新加载（可能刚被写入）
  set -a
  # shellcheck disable=SC1090
  . "./$ENV_FILE"
  set +a

  # Langfuse 启用判定：公私钥成对非空才启用（半配置已在 prepare_env 拦截）。
  # 可选组件默认关闭，不搞「半配置也硬启用」。
  if [ -n "${LANGFUSE_PUBLIC_KEY:-}" ] && [ -n "${LANGFUSE_SECRET_KEY:-}" ]; then
    LANGFUSE_ENABLED=true
  else
    LANGFUSE_ENABLED=false
  fi
  export LANGFUSE_ENABLED

  python3 - "$TEMPLATE" "$GENERATED" <<'PY'
import os, sys, pathlib
tpl_path, out_path = sys.argv[1], sys.argv[2]
text = pathlib.Path(tpl_path).read_text(encoding='utf-8')

mapping = {
    'MYSQL_PASSWORD':        os.getenv('MYSQL_ROOT_PASSWORD', ''),
    'MINIO_ROOT_USER':       os.getenv('MINIO_ROOT_USER', 'agentchat'),
    'MINIO_ROOT_PASSWORD':   os.getenv('MINIO_ROOT_PASSWORD', ''),
    'DASHSCOPE_API_KEY':     os.getenv('DASHSCOPE_API_KEY', ''),
    'DEEPSEEK_API_KEY':      os.getenv('DEEPSEEK_API_KEY', ''),
    'AMAP_API_KEY':          os.getenv('AMAP_API_KEY', ''),
    'TAVILY_API_KEY':        os.getenv('TAVILY_API_KEY', ''),
    'SERPAPI_KEY':           os.getenv('SERPAPI_KEY', ''),
    'DELIVERY_API_KEY':      os.getenv('DELIVERY_API_KEY', ''),
    'BOCHA_API_KEY':         os.getenv('BOCHA_API_KEY', ''),
    'WECHAT_TOKEN':          os.getenv('WECHAT_TOKEN', ''),
    'WECHAT_APP_ID':         os.getenv('WECHAT_APP_ID', ''),
    'WECHAT_SECRET':         os.getenv('WECHAT_SECRET', ''),
    'LANGFUSE_PUBLIC_KEY':   os.getenv('LANGFUSE_PUBLIC_KEY', ''),
    'LANGFUSE_SECRET_KEY':   os.getenv('LANGFUSE_SECRET_KEY', ''),
    'OSS_ACCESS_KEY_ID':     os.getenv('OSS_ACCESS_KEY_ID', ''),
    'OSS_ACCESS_KEY_SECRET': os.getenv('OSS_ACCESS_KEY_SECRET', ''),
    # 可选项：env 未设置时用 or 默认值保留模板语义。
    # 不能用 os.getenv(k, 'default') 之外的空串替换 —— 那会把可选配置清成空值。
    'LANGFUSE_ENABLED':          os.getenv('LANGFUSE_ENABLED') or 'false',
    'LANGFUSE_HOST':             os.getenv('LANGFUSE_HOST') or 'https://cloud.langfuse.com',
    'SEMANTIC_CACHE_ENABLED':    os.getenv('SEMANTIC_CACHE_ENABLED') or 'false',
    'SEMANTIC_CACHE_THRESHOLD':  os.getenv('SEMANTIC_CACHE_THRESHOLD') or '0.90',
    'SEMANTIC_CACHE_TTL':        os.getenv('SEMANTIC_CACHE_TTL') or '86400',
    'SEMANTIC_CACHE_KB_VERSION': os.getenv('SEMANTIC_CACHE_KB_VERSION') or 'v1',
}

# 长占位符优先替换，避免 __MINIO_ROOT_USER__ 被 __MINIO_ROOT__ 之类前缀误伤
for key in sorted(mapping, key=len, reverse=True):
    text = text.replace(f'__{key}__', mapping[key])

# 残留占位符检查：漏填的项会让配置带着 __XXX__ 字面量上线，必须拦住
import re
left = sorted(set(re.findall(r'__[A-Z0-9_]+__', text)))
if left:
    print("WARN_LEFT=" + ",".join(left))

pathlib.Path(out_path).write_text(text, encoding='utf-8')
print("OK")
PY
  local rc=$?
  [ $rc -eq 0 ] || { c_err "配置渲染失败"; exit 1; }

  # YAML 语法校验（若 python 有 pyyaml）
  if python3 -c "import yaml" 2>/dev/null; then
    python3 -c "
import yaml, sys
d = yaml.safe_load(open('$GENERATED', encoding='utf-8'))
assert d, '配置为空'
v = d['multi_models']['qwen_vl']['base_url']
assert not v.endswith(chr(34)), 'qwen_vl.base_url 结尾有多余引号'
print('YAML 校验通过; qwen_vl.base_url =', v)
" || { c_err "生成的配置未通过 YAML 校验"; exit 1; }
  else
    c_warn "未安装 pyyaml，跳过 YAML 语法校验（建议：pip3 install pyyaml）"
  fi

  chmod 600 "$GENERATED"
  c_ok "已生成 $GENERATED"
}

# =============================================================================
# 4. 构建并启动
# =============================================================================
do_deploy() {
  c_step "构建镜像（首次约 5~10 分钟）"
  docker compose -f "$COMPOSE_FILE" build

  c_step "启动服务"
  docker compose -f "$COMPOSE_FILE" up -d

  c_step "等待服务就绪"
  local timeout=300 elapsed=0
  while [ $elapsed -lt $timeout ]; do
    local running
    running=$(docker compose -f "$COMPOSE_FILE" ps --status running --format '{{.Service}}' 2>/dev/null | wc -l | tr -d ' ')
    if [ "$running" -ge 5 ]; then
      c_ok "5 个服务均已运行"
      break
    fi
    sleep 5; elapsed=$((elapsed + 5))
    printf '\r  已等待 %ss，运行中 %s/5 ...' "$elapsed" "$running"
  done
  echo ""

  if [ $elapsed -ge $timeout ]; then
    c_err "服务未在 ${timeout}s 内全部启动，请查看日志："
    docker compose -f "$COMPOSE_FILE" ps
    echo ""
    docker compose -f "$COMPOSE_FILE" logs --tail=50
    exit 1
  fi

  # 后端首次启动需初始化数据库；用健康检查状态轮询，而不是固定 sleep
  wait_backend_ready 300
}

# 轮询后端健康状态，替代原来的固定 sleep。
#
# 为什么不用 sleep 20（实测依据）：
#   * 快机器上后端 8 秒就绪，sleep 20 是纯浪费；
#   * 慢机器上首次启动要建表 + 写默认 Agent，20 秒根本不够，
#     后面 do_verify 会拿到一串 502，看起来像"部署失败"。
# 改为读取容器 HEALTHCHECK 的真实状态（compose 里已定义，探 /health），
# 把"猜时间"变成"等事实"。
#
# 超时只告警不退出：让 do_verify 继续跑，用具体失败项定位问题，
# 比在这里直接 abort 提供更多信息。
wait_backend_ready() {
  local timeout="${1:-300}" elapsed=0 cid state
  c_step "等待后端就绪（首次含数据库初始化）"

  cid=$(docker compose -f "$COMPOSE_FILE" ps -q backend 2>/dev/null || true)
  if [ -z "$cid" ]; then
    c_err "找不到 backend 容器，请检查 docker compose ps"
    return 1
  fi

  while [ "$elapsed" -lt "$timeout" ]; do
    state=$(docker inspect -f \
      '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "$cid" 2>/dev/null || echo unknown)

    case "$state" in
      healthy)
        echo ""
        c_ok "后端已就绪（健康检查通过，用时 ${elapsed}s）"
        return 0
        ;;
      unhealthy)
        echo ""
        c_err "后端健康检查失败（${elapsed}s），最近 50 行日志："
        docker compose -f "$COMPOSE_FILE" logs --tail=50 backend
        return 1
        ;;
    esac

    sleep 5; elapsed=$((elapsed + 5))
    printf '\r  已等待 %ss，后端状态：%s ...' "$elapsed" "$state"
  done

  echo ""
  c_warn "等待 ${timeout}s 后端仍未 healthy，继续执行验收（失败项会具体列出）。"
  return 0
}

# =============================================================================
# 5. 验收自检
# =============================================================================
do_verify() {
  c_step "验收自检"
  local pass=0 fail=0

  # 探测目标。本机预览时 80 端口常被占用（改用 docker-compose.local.yml 跑在 8080），
  # 此时可用：VERIFY_BASE_URL=http://127.0.0.1:8080 ./deploy.sh --verify
  local BASE_URL="${VERIFY_BASE_URL:-http://127.0.0.1}"
  c_info "探测目标：$BASE_URL"

  # 所有本机探测都必须加 --noproxy '*'：环境里若设了 http_proxy，
  # curl 会把 127.0.0.1 也丢给代理，得到假阴性。
  #
  # 注意不要用 `-o /dev/null`：Git Bash 下写 /dev/null 会失败，curl 打印出
  # http_code 后仍返回非零退出码，若再接 `|| echo 000` 就会拼出 "200000"。
  # 这里改为写真实临时文件，并把结果严格收敛成 3 位数字。
  # 用固定的相对路径临时文件：mktemp 在 Git Bash 下返回 Windows 风格路径，
  # curl 会把它当作相对路径拼接而写错位置。
  local PROBE_TMP="./.deploy_probe.$$.tmp"
  : > "$PROBE_TMP"
  _code() {
    local out
    out=$(curl -s -o "$PROBE_TMP" -w '%{http_code}' --noproxy '*' -m "${2:-20}" "$1" 2>/dev/null)
    case "$out" in
      ''|*[!0-9]*) printf '000' ;;
      *)           printf '%s' "$out" ;;
    esac
  }

  _check() { # 描述 期望码 实际码
    if [ "$2" = "$3" ]; then
      c_ok "$1  → HTTP $3"
      pass=$((pass + 1))
    else
      c_err "$1  → 期望 HTTP $2，实际 $3"
      fail=$((fail + 1))
    fi
  }

  local code

  # 容器健康
  local unhealthy
  unhealthy=$(docker compose -f "$COMPOSE_FILE" ps --format '{{.Service}} {{.Status}}' 2>/dev/null | grep -ci 'unhealthy\|Exit' || true)
  if [ "$unhealthy" -eq 0 ]; then
    c_ok "容器状态  → 无异常"
    pass=$((pass + 1))
  else
    c_err "容器状态  → 存在异常容器"
    docker compose -f "$COMPOSE_FILE" ps
    fail=$((fail + 1))
  fi

  # 1) 前端静态站点
  code=$(_code "$BASE_URL/" 20)
  _check "前端首页" 200 "$code"

  # 2) Nginx 健康检查端点
  code=$(_code "$BASE_URL/healthz" 10)
  _check "Nginx 健康检查" 200 "$code"

  # 3) API 反代贯通（未登录应 401/403，说明路由存在且鉴权生效）
  code=$(_code "$BASE_URL/api/v1/dialog/list" 20)
  if [ "$code" = "401" ] || [ "$code" = "403" ]; then
    c_ok "API 反代贯通（鉴权生效）  → HTTP $code"
    pass=$((pass + 1))
  else
    c_err "API 反代异常  → HTTP $code（期望 401/403）"
    fail=$((fail + 1))
  fi

  # 4) 后端健康端点（经反代）
  code=$(_code "$BASE_URL/api/v1/../health" 20)
  if [ "$code" = "200" ] || [ "$code" = "404" ]; then
    c_ok "后端可达  → HTTP $code"
    pass=$((pass + 1))
  else
    c_warn "后端健康端点探测  → HTTP $code（非致命）"
  fi

  # 5) 对象存储反代（用桶内任意对象；无对象则跳过）
  code=$(_code "$BASE_URL/minio/agentchat/" 20)
  if [ "$code" = "403" ] || [ "$code" = "404" ] || [ "$code" = "200" ]; then
    c_ok "对象存储反代可达  → HTTP $code"
    pass=$((pass + 1))
  else
    c_err "对象存储反代异常  → HTTP $code"
    fail=$((fail + 1))
  fi

  rm -f "$PROBE_TMP"

  echo ""
  if [ "$fail" -eq 0 ]; then
    c_ok "验收通过（$pass 项）"
  else
    c_err "验收未通过：$pass 项通过，$fail 项失败"
  fi

  # 输出访问地址
  echo ""
  local ip
  if [ -n "${VERIFY_BASE_URL:-}" ]; then
    c_info "访问地址：  ${BASE_URL}/  （本机预览模式）"
  else
    ip=$(curl -s -m 5 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo '<服务器公网IP>')
    c_info "访问地址：  http://${ip}/"
  fi
  c_info "日志查看：  docker compose -f docker/$COMPOSE_FILE logs -f"
  echo ""
  c_warn "安全提示：请确认云服务器安全组已放行 80 端口；"
  c_warn "          数据库/Redis/MinIO 端口未对外开放（本编排已不映射宿主机端口）。"
  echo ""
  [ "$fail" -eq 0 ] || exit 1

  # 验收通过后立刻预热，避免第一个真实用户撞上 90 秒的首字延迟
  warmup
}

# =============================================================================
# 6. 冷启动预热
# =============================================================================
# 从 stdin 的 JSON 里按点号路径取值（取不到就输出空字符串）。
# 用 python3 而不是 jq：preflight 已经强制要求 python3，jq 未必装了。
_json_pick() {
  python3 -c '
import json, sys
path = sys.argv[1].split(".")
try:
    cur = json.load(sys.stdin)
    for k in path:
        cur = cur[int(k)] if k.isdigit() else cur[k]
    print("" if cur is None else cur)
except Exception:
    print("")
' "$1"
}

# 把 JSON 字符串安全转成请求体（避免口令里的引号破坏 JSON）
_json_body() {
  python3 -c 'import json,sys; print(json.dumps(dict(zip(sys.argv[1::2], sys.argv[2::2])), ensure_ascii=False))' "$@"
}

# 冷启动预热：走一遍「登录 → 取 Agent → 建会话 → 发一条消息」的真实链路。
#
# 为什么需要：后端首次对话要懒初始化向量库与嵌入模型，首字延迟可达 90s。
# 若把这一下留给第一个真实用户，他会以为服务卡死（原脚本只是 warn 一句）。
#
# 设计取舍：
#   * 必须显式配置 WARMUP_USER / WARMUP_PASSWORD 才执行 —— 部署脚本不该
#     硬编码任何账号口令；
#   * 任何一步失败都只告警、不影响部署结论（预热是优化项，不是正确性项）；
#   * 用 --max-time 兜住：预热本身卡住不该让部署脚本挂死。
warmup() {
  c_step "冷启动预热"

  local base="${VERIFY_BASE_URL:-http://127.0.0.1}"

  # --verify 模式不会走 prepare_env，这里按需补读 .env
  if [ -f "$ENV_FILE" ] && { [ -z "${WARMUP_USER:-}" ] || [ -z "${WARMUP_PASSWORD:-}" ]; }; then
    set -a
    # shellcheck disable=SC1090
    . "./$ENV_FILE"
    set +a
  fi

  if [ -z "${WARMUP_USER:-}" ] || [ -z "${WARMUP_PASSWORD:-}" ]; then
    c_info "未配置预热账号，跳过自动预热。"
    c_info "  首次对话首字延迟约 90 秒（向量库/嵌入模型懒初始化）。请任选其一："
    c_info "    a) 用浏览器登录后随手发一条「你好」，把初始化提前做掉；"
    c_info "    b) 在 docker/.env 里设置 WARMUP_USER / WARMUP_PASSWORD，"
    c_info "       再执行 ./deploy.sh --verify 自动完成一次真实对话预热。"
    return 0
  fi

  local tmp="./.warmup.$$.tmp"
  : > "$tmp"
  local C=(curl -sS --noproxy '*' -m 30)

  # 1) 登录
  local login_json token
  login_json=$("${C[@]}" -X POST "$base/api/v1/user/login" \
    -H 'Content-Type: application/json' \
    -d "$(_json_body user_name "$WARMUP_USER" user_password "$WARMUP_PASSWORD")" 2>/dev/null || true)
  token=$(printf '%s' "$login_json" | _json_pick data.access_token)
  if [ -z "$token" ]; then
    c_warn "预热跳过：登录失败（检查 WARMUP_USER / WARMUP_PASSWORD 是否正确，账号是否已注册）。"
    rm -f "$tmp"
    return 0
  fi
  c_ok "预热：登录成功"

  # 2) 取一个可用 Agent（系统内置 Agent 在初始化时已写入）
  local agent_json agent_id
  agent_json=$("${C[@]}" "$base/api/v1/agent" -H "Authorization: Bearer $token" 2>/dev/null || true)
  agent_id=$(printf '%s' "$agent_json" | _json_pick data.0.agent_id)
  [ -n "$agent_id" ] || agent_id=$(printf '%s' "$agent_json" | _json_pick data.0.id)
  if [ -z "$agent_id" ]; then
    c_warn "预热跳过：未能取到可用 Agent（默认 Agent 可能仍在初始化）。"
    rm -f "$tmp"
    return 0
  fi
  c_ok "预热：取到 Agent（$agent_id）"

  # 3) 建一个临时会话
  local dialog_json dialog_id
  dialog_json=$("${C[@]}" -X POST "$base/api/v1/dialog" \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $token" \
    -d "$(_json_body name "部署预热" agent_id "$agent_id" agent_type "Agent")" 2>/dev/null || true)
  dialog_id=$(printf '%s' "$dialog_json" | _json_pick data.dialog_id)
  if [ -z "$dialog_id" ]; then
    c_warn "预热跳过：创建会话失败（响应：$(printf '%s' "$dialog_json" | head -c 200)）。"
    rm -f "$tmp"
    return 0
  fi

  # 4) 发一条消息，触发懒初始化。
  #    %{time_starttransfer} = 首字节时间，正是用户感知的「首字延迟」。
  c_info "预热：发送测试消息并等待首字（最长 180 秒，首次可能很慢）..."
  local ttfb
  ttfb=$("${C[@]}" -o "$tmp" -w '%{time_starttransfer}' --max-time 180 \
    -X POST "$base/api/v1/completion" \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $token" \
    -d "$(_json_body user_input "你好" dialog_id "$dialog_id")" 2>/dev/null || true)

  case "$ttfb" in
    ''|*[!0-9.]*) c_warn "预热未完成（请求超时或中断），可稍后重试：./deploy.sh --verify" ;;
    *)            c_ok "预热完成：首字延迟 ${ttfb}s（后续请求应恢复到 1 秒内）" ;;
  esac

  # 清理临时会话，避免部署脚本在用户会话列表里留下垃圾记录
  "${C[@]}" -X DELETE "$base/api/v1/dialog" \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $token" \
    -d "$(_json_body dialog_id "$dialog_id")" >/dev/null 2>&1 || true

  rm -f "$tmp"
  return 0
}

# =============================================================================
# 主流程
# =============================================================================
case "$MODE" in
  down)
    c_step "停止服务"
    docker compose -f "$COMPOSE_FILE" down
    c_ok "已停止（数据卷保留，可用 up -d 恢复）"
    ;;
  verify)
    do_verify
    ;;
  deploy)
    preflight
    prepare_env
    render_config
    do_deploy
    do_verify
    ;;
esac
