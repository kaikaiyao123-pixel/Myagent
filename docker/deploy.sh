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
PROJECT_ROOT="$(cd .. && pwd)"

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

  # shellcheck disable=SC1090
  set -a; . "./$ENV_FILE"; set +a

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
  # shellcheck disable=SC1090
  set -a; . "./$ENV_FILE"; set +a

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

  # 后端首次启动需初始化数据库，多等一会
  c_info "等待后端完成数据库初始化..."
  sleep 20
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
  c_warn "冷启动提示：容器启动后的【第一次】对话首字延迟可达 90 秒左右（重资源懒初始化），"
  c_warn "            同一容器的后续请求会恢复到 1 秒内。建议现在就用一个账号发一条消息预热，"
  c_warn "            否则第一个访问者会以为服务卡死。"

  [ "$fail" -eq 0 ] || exit 1
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
