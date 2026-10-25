#!/usr/bin/env bash
# =============================================================================
# AgentChat 生产数据备份
#
# 备份范围（对应 docker-compose.prod.yml 的 5 个命名卷）：
#   mysql_data        → mysqldump 逻辑备份（.sql.gz），可跨版本恢复
#   minio_data        → 卷打包（.tar.gz），含头像/附件/知识库原文
#   backend_memory_db → 卷打包（.tar.gz），长期记忆
#   backend_vector_db → 卷打包（.tar.gz），向量库（Chroma）索引
#   redis_data        → 不备份（纯缓存/会话，丢了可重建）
#
# 用法：
#   cd docker
#   ./backup.sh                     # 备份到 ./backup
#   ./backup.sh --dry-run           # 只打印将执行的命令，不写任何文件
#   BACKUP_DIR=/data/backup RETAIN_DAYS=14 ./backup.sh
#
# 建议用 cron 每日执行一次（crontab -e）：
#   30 3 * * * cd /opt/agentchat/docker && ./backup.sh >> /var/log/agentchat-backup.log 2>&1
#
# ⚠️ 为什么 MySQL 用 mysqldump 而不是打包数据目录：
#    直接复制正在运行的 InnoDB 数据目录会得到不一致的快照（需要先 FLUSH/停库）。
#    mysqldump --single-transaction 在事务内取一致快照，且产物可读、可跨小版本恢复。
#
# ⚠️ 备份产物含全部业务数据（含用户对话），BACKUP_DIR 应设置合适的权限，
#    并考虑异地同步（如 rsync 到另一台机器或对象存储）。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

COMPOSE_FILE="docker-compose.prod.yml"
BACKUP_DIR="${BACKUP_DIR:-./backup}"
RETAIN_DAYS="${RETAIN_DAYS:-7}"
DRY_RUN="${DRY_RUN:-0}"
HELPER_IMAGE="${HELPER_IMAGE:-alpine:3.20}"
DB_NAME="${DB_NAME:-agentchat}"

STAMP="$(date +%Y%m%d-%H%M%S)"

# 挂载进辅助容器时必须用绝对路径：BACKUP_DIR 可能是相对路径，
# 而 `-v "相对路径:/backup"` 会被 Docker 当成【命名卷】而不是目录。
case "$BACKUP_DIR" in
  /*)              BACKUP_ABS="$BACKUP_DIR" ;;
  *)               BACKUP_ABS="$SCRIPT_DIR/${BACKUP_DIR#./}" ;;
esac

c_info() { printf '\033[36m[信息]\033[0m %s\n' "$*"; }
c_ok()   { printf '\033[32m[完成]\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m[警告]\033[0m %s\n' "$*"; }
c_err()  { printf '\033[31m[错误]\033[0m %s\n' "$*" >&2; }

# ----------------------------- 参数解析 --------------------------------------
case "${1:-}" in
  --dry-run) DRY_RUN=1 ;;
  --help|-h)
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  "") ;;
  *) c_err "未知参数: $1（可用: --dry-run / --help）"; exit 1 ;;
esac

# 执行包装：dry-run 时只打印，不落盘
run() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '[dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

# ----------------------------- 前置检查 --------------------------------------
preflight() {
  command -v docker >/dev/null 2>&1 || { c_err "未安装 Docker。"; exit 1; }
  docker compose -f "$COMPOSE_FILE" config --quiet 2>/dev/null \
    || { c_err "compose 文件校验失败：$COMPOSE_FILE"; exit 1; }
  [ -f "$COMPOSE_FILE" ] || { c_err "缺少 $COMPOSE_FILE"; exit 1; }

  # 项目名用于推导卷名（compose 里显式写了 name: agentchat-prod）
  PROJECT="$(docker compose -f "$COMPOSE_FILE" config --format json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("name",""))' 2>/dev/null || true)"
  [ -n "$PROJECT" ] || PROJECT="agentchat-prod"

  c_info "备份目录：$BACKUP_DIR（保留 ${RETAIN_DAYS} 天）"
  c_info "compose 项目名：$PROJECT"
  [ "$DRY_RUN" = "1" ] && c_warn "dry-run 模式：不会写入任何文件"
}

# 取某服务某挂载点对应的真实卷名。
# 优先问运行中的容器（改名/换 project 也能拿到正确值），
# 拿不到时回退到「项目名_卷逻辑名」这一 compose 默认命名规则。
resolve_volume() {
  local svc="$1" dest="$2" logical="$3" cid name
  if [ "$DRY_RUN" != "1" ]; then
    cid="$(docker compose -f "$COMPOSE_FILE" ps -q "$svc" 2>/dev/null || true)"
    if [ -n "$cid" ]; then
      name="$(docker inspect -f \
        "{{range .Mounts}}{{if eq .Destination \"$dest\"}}{{.Name}}{{end}}{{end}}" \
        "$cid" 2>/dev/null || true)"
      if [ -n "$name" ]; then
        printf '%s' "$name"
        return 0
      fi
    fi
  fi
  printf '%s' "${PROJECT}_${logical}"
}

# ----------------------------- 备份动作 --------------------------------------
backup_mysql() {
  local out="$BACKUP_DIR/mysql_${DB_NAME}_${STAMP}.sql.gz"
  c_info "备份 MySQL（mysqldump --single-transaction）→ $(basename "$out")"

  if [ "$DRY_RUN" = "1" ]; then
    printf '[dry-run] docker compose -f %s exec -T mysql sh -c %s | gzip > %s\n' \
      "$COMPOSE_FILE" "'exec mysqldump ... $DB_NAME'" "$out"
    return 0
  fi

  # 口令从容器内的 MYSQL_ROOT_PASSWORD 读取，避免在宿主机命令行留痕
  docker compose -f "$COMPOSE_FILE" exec -T mysql \
    sh -c "exec mysqldump -uroot -p\"\$MYSQL_ROOT_PASSWORD\" \
      --single-transaction --quick --routines --events --databases $DB_NAME" \
    2>/dev/null | gzip -c > "$out"

  # gzip 之后必须校验：mysqldump 失败时 pipefail 会中断，
  # 但空文件（表为空）也要能看出来，这里只做基本完整性检查
  gzip -t "$out" || { c_err "备份文件损坏：$out"; exit 1; }
  c_ok "MySQL 备份完成：$(du -h "$out" | cut -f1)"
}

backup_volume() {
  local svc="$1" dest="$2" logical="$3" vol out
  vol="$(resolve_volume "$svc" "$dest" "$logical")"
  out="$BACKUP_DIR/${logical}_${STAMP}.tar.gz"
  c_info "备份卷 $vol → $(basename "$out")"

  run docker run --rm \
    -v "$vol:/data:ro" \
    -v "$BACKUP_ABS:/backup" \
    "$HELPER_IMAGE" \
    tar czf "/backup/${logical}_${STAMP}.tar.gz" -C /data .

  [ "$DRY_RUN" = "1" ] || c_ok "卷备份完成：$(du -h "$out" | cut -f1)"
}

rotate() {
  c_info "清理 ${RETAIN_DAYS} 天前的备份"
  if [ "$DRY_RUN" = "1" ]; then
    printf '[dry-run] find %s -maxdepth 1 -type f -mtime +%s -print -delete\n' "$BACKUP_DIR" "$RETAIN_DAYS"
    return 0
  fi
  find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name 'mysql_*.sql.gz' -o -name 'minio_data_*.tar.gz' \
       -o -name 'backend_memory_db_*.tar.gz' -o -name 'backend_vector_db_*.tar.gz' \) \
    -mtime "+${RETAIN_DAYS}" -print -delete || true
}

# ----------------------------- 主流程 ----------------------------------------
preflight
run mkdir -p "$BACKUP_DIR"

backup_mysql
backup_volume minio    /minio_data    minio_data
backup_volume backend  /app/memory_db backend_memory_db
backup_volume backend  /app/vector_db backend_vector_db

rotate

echo ""
if [ "$DRY_RUN" = "1" ]; then
  c_warn "dry-run 结束：以上命令均未执行。"
else
  c_ok "全部备份完成，产物目录：$BACKUP_DIR"
  ls -lh "$BACKUP_DIR" | tail -n +2 || true
fi
