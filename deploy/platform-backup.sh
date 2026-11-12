#!/usr/bin/env bash
# 平台备份/恢复一体脚本（一台一条链：backup → 拷 tar → 新机拉最新代码 → restore）
# 完整说明（备份清单/恢复顺序/口径边界）见同目录 backup-restore.md
#
# 用法：
#   bash platform-backup.sh backup [输出目录或完整.tar.gz路径]
#       默认输出 /root；盘紧张时给路径，如：backup /data/backups 或
#       backup /mnt/nas/platform-2026-11-11.tar.gz；空间不足自动报错不写半包
#   EXCLUDE_ARTIFACTS=1 bash platform-backup.sh backup  # 快档：不含产物树（体积小）
#   bash platform-backup.sh restore <tar.gz>            # 恢复到备份状态（含库/数据/凭据/证书）
#   bash platform-backup.sh restore <tar.gz> --dry-run  # 只打印将做什么，不动任何东西
#
# 恢复口径（用户拍定的语义："拉最新代码 + 回到备份时的运行状态"）：
#   · 机器状态（git 不管的）一律还原：.env.compile、数据库整库、artifacts/、cache/（货架）、
#     backend/cache/、/etc/nginx/ssl 证书、主 nginx.conf（include 注入行）
#   · 代码与入库模板保持"最新代码"版（git pull 结果）：nginx 平台 conf 由 setup.sh install 重拷
#   · skills/config.yaml 若备份时比 git 新且有 RESTORE_GITTRACKED=1 才覆盖
#   · 尾步自动跑 setup.sh install（幂等修 pg_hba/建表）+ restart

set -euo pipefail

# ── 项目根定位：显式 PROJECT_DIR > 当前目录含 setup.sh > 两个已知部署路径 ──
pick_dir() {
    if [ -n "${PROJECT_DIR:-}" ] && [ -f "$PROJECT_DIR/setup.sh" ]; then echo "$PROJECT_DIR"; return; fi
    [ -f "./setup.sh" ] && { echo "$(pwd)"; return; }
    for d in /root/AutoCompilation /root/.openclaw/workspace/compile-platform; do
        [ -f "$d/setup.sh" ] && { echo "$d"; return; }
    done
    echo ""; return 1
}
ROOT="$(pick_dir)" || { echo "找不到项目根（需含 setup.sh），可 PROJECT_DIR=/path bash $0 ..."; exit 1; }
ENV_FILE="$ROOT/backend/.env.compile"
BACKUP_DIR="${BACKUP_DIR:-/root}"

# ── 数据根解析（与后端 config.artifacts_dir / setup.sh B-8b 同口径）──
# 优先级：进程 env ARTIFACTS_DIR > .env.compile 内该行 > config.yaml compile.artifact_dir > ./artifacts
# 货架=产物根的同级 cache（后端 cache_root() 语义：artifacts_dir().parent/"cache"）
ART_DIR="${ARTIFACTS_DIR:-}"
if [ -z "$ART_DIR" ] && [ -f "$ENV_FILE" ]; then
    ART_DIR=$(grep -E '^(export )?ARTIFACTS_DIR=' "$ENV_FILE" | tail -1 \
              | sed -E 's/^(export )?ARTIFACTS_DIR=//; s/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/')
fi
if [ -z "$ART_DIR" ]; then
    ART_DIR=$(awk '/compile:/{f=1;next} f&&/artifact_dir:/{print $2;exit}' "$ROOT/backend/config.yaml" 2>/dev/null)
fi
ART_DIR="${ART_DIR:-./artifacts}"
case "$ART_DIR" in /*) : ;; *) ART_DIR="$ROOT/${ART_DIR#./}" ;; esac
CACHE_DIR="$(dirname "$ART_DIR")/cache"

# 库参数读 config.yaml（口令永远只从 env 来，本脚本不回显任何密钥）
db_param() {
    awk "/$1:/{f=1;next} f&&/$2:/{gsub(/\"/,\"\",$2);print $2;exit}" "$ROOT/backend/config.yaml" 2>/dev/null || true
}
DB_USER_ACT="${DB_USER:-$(db_param postgresql username)}";  DB_USER_ACT="${DB_USER_ACT:-compile_user}"
DB_NAME_ACT="${DB_NAME:-$(db_param postgresql database)}";   DB_NAME_ACT="${DB_NAME_ACT:-compile_platform}"
DB_HOST_ACT="${DB_HOST:-$(db_param postgresql host)}";       DB_HOST_ACT="${DB_HOST_ACT:-localhost}"

psql_super() { sudo -u postgres psql -tA -c "$1"; }

# ════════════════════════ backup ════════════════════════
do_backup() {
    [ -f "$ENV_FILE" ] || { echo "缺 $ENV_FILE——先按 setup.sh 提示配好环境再备份"; exit 1; }
    local TS OUT STAGE OUTDIR DEST="${1:-}"
    TS=$(date +%Y%m%d-%H%M%S)
    # 输出位置：参数优先（可给目录，也可给完整 .tar.gz 文件路径）；次选 $BACKUP_DIR；默认 /root
    case "$DEST" in
        *.tar.gz) OUT="$DEST" ;;
        "")       OUTDIR="${BACKUP_DIR:-/root}"; OUT="$OUTDIR/platform-backup-$(hostname -s)-$TS.tar.gz" ;;
        *)        OUTDIR="$DEST"; OUT="$OUTDIR/platform-backup-$(hostname -s)-$TS.tar.gz" ;;
    esac
    OUTDIR=$(dirname "$OUT")
    mkdir -p "$OUTDIR" || { echo "输出目录不可建: $OUTDIR"; exit 1; }
    # 空间预检：峰值 = 中转目录(未压缩) + 成品包 ≈ 2 倍数据量；要求 avail ≥ 2*need+1GB
    local need avail
    need=$(du -sm "$ART_DIR" "$CACHE_DIR" "$ROOT/backend/cache" 2>/dev/null | awk '{s+=$1} END{print s+50}')
    [ "${EXCLUDE_ARTIFACTS:-0}" = "1" ] && need=200
    avail=$(df -m --output=avail "$OUTDIR" 2>/dev/null | tail -1 | tr -d ' ')
    if [ -n "$avail" ] && [ "$avail" -lt $((need * 2 + 1024)) ]; then
        echo "输出盘空间不足：本档需约 $((need * 2 + 1024))MB（中转+成品双倍），${OUTDIR} 所在分区仅剩 ${avail}MB"
        echo "换个位置：bash $0 backup /data/backups   或   bash $0 backup /mnt/nas/xx.tar.gz"
        exit 1
    fi
    STAGE=$(mktemp -d "${OUTDIR}/.pbackup.XXXXXX" 2>/dev/null || mktemp -d /tmp/pbackup.XXXXXX)
    echo "== 备份项目根: $ROOT"

    # 1) 凭据与机器配置
    mkdir -p "$STAGE/state"
    cp "$ENV_FILE" "$STAGE/state/env.compile"
    [ -f "$ROOT/backend/config.yaml" ] && cp "$ROOT/backend/config.yaml" "$STAGE/state/config.yaml"
    [ -d "$ROOT/skills" ] && cp -a "$ROOT/skills" "$STAGE/state/skills"

    # 2) 元信息（备份时的代码版本 + 未提交改动清单，恢复后对账用）
    {
        echo "backup_time=$TS host=$(hostname -s)"
        echo "root=$ROOT"
        echo "git_head=$(git -C "$ROOT" log -1 --pretty='%h %s' 2>/dev/null || echo '(无git)')"
        echo "git_dirty=$(git -C "$ROOT" status --short 2>/dev/null | wc -l) 项未提交"
        git -C "$ROOT" status --short 2>/dev/null | head -20
    } > "$STAGE/state/meta.txt"

    # 3) 数据库整库（pg_dump 走 env 口令，口令不出屏）
    echo "== 导出数据库 $DB_NAME_ACT …"
    set -a; source "$ENV_FILE"; set +a
    if PGPASSWORD="${DB_PASSWORD:-}" pg_dump -h "$DB_HOST_ACT" -U "$DB_USER_ACT" -d "$DB_NAME_ACT" \
         --no-owner --no-privileges -f "$STAGE/state/db.sql"; then
        echo "   db.sql $(du -h "$STAGE/state/db.sql" | cut -f1)"
    else
        rm -f "$STAGE/state/db.sql"
        echo "!! pg_dump 失败（env 与库口令可能脱钩）——继续备文件部分，DB 需另行处理"; fi

    # 4) 运行数据：产物树 + 货架 + 轮换备份缓存
    if [ "${EXCLUDE_ARTIFACTS:-0}" != "1" ]; then
        echo "== 打包数据目录（产物 $ART_DIR / 货架 $CACHE_DIR，较慢可 Ctrl+C 后加 EXCLUDE_ARTIFACTS=1 重跑）…"
        mkdir -p "$STAGE/data"
        [ -d "$ART_DIR" ] && cp -a "$ART_DIR" "$STAGE/data/artifacts"
        [ -d "$CACHE_DIR" ] && cp -a "$CACHE_DIR" "$STAGE/data/cache"
        [ -d "$ROOT/backend/cache" ] && cp -a "$ROOT/backend/cache" "$STAGE/data/backend_cache"
    fi

    # 5) HTTPS 证书与主 nginx.conf（机器状态；平台 conf 模板走 git 不备）
    if [ -d /etc/nginx/ssl ]; then
        sudo tar czf "$STAGE/state/nginx-ssl.tar.gz" -C /etc/nginx ssl && sudo chmod 644 "$STAGE/state/nginx-ssl.tar.gz"
    fi
    [ -f /etc/nginx/nginx.conf ] && sudo cp /etc/nginx/nginx.conf "$STAGE/state/nginx.conf" && sudo chmod 644 "$STAGE/state/nginx.conf"

    # 6) 归拢成单包（含密钥，权限收紧）
    tar czf "$OUT" -C "$STAGE" state data 2>/dev/null || tar czf "$OUT" -C "$STAGE" state
    rm -rf "$STAGE"
    chmod 600 "$OUT"
    echo "== 完成: $OUT ($(du -h "$OUT" | cut -f1))"
    echo "   内容清单："
    tar tzf "$OUT" | grep -vE "^data/artifacts/.+/.+$|^data/cache/" | sed 's/^/     /' | head -14
    echo "   传到目标机后：cd 目标项目 && bash $0 restore $OUT"
}

# ════════════════════════ restore ════════════════════════
do_restore() {
    local TARBALL="${1:?用法: restore <tar.gz> [--dry-run]}"
    local DRY=0; [ "${2:-}" = "--dry-run" ] && DRY=1
    [ -f "$TARBALL" ] || { echo "找不到 $TARBALL"; exit 1; }

    local TMP; TMP=$(mktemp -d /tmp/prestore.XXXXXX)
    tar xzf "$TARBALL" -C "$TMP"
    echo "== 备份包信息："; sed 's/^/   /' "$TMP/state/meta.txt" | head -8
    echo "== 项目根: $ROOT  ${DRY:+(dry-run 模式，只报不改)}"

    act() { # 展示并（非 dry-run）执行
        echo "   • $1"
        [ "$DRY" = "1" ] && return 0
        shift; eval "$@"
    }

    # 0) 停服务（避免 drop 库时反复重连；无 stop 子命令则忽略失败）
    act "停后端" "bash '$ROOT/setup.sh' stop >/dev/null 2>&1 || true"

    # 1) 凭据
    act "恢复 backend/.env.compile（现有文件先转存 .pre-restore）" \
        "[ -f '$ENV_FILE' ] && cp '$ENV_FILE' '$ENV_FILE.pre-restore'; cp '$TMP/state/env.compile' '$ENV_FILE'; chmod 600 '$ENV_FILE'"

    # 2) 数据库：口令对齐 → 清库重建 → 灌回 → 计数验证
    if [ -f "$TMP/state/db.sql" ]; then
        act "ALTER USER 口令与备份 env 对齐（经 postgres 超级通道）" "
            set -a; source '$ENV_FILE'; set +a
            psql_super \"ALTER USER $DB_USER_ACT WITH PASSWORD '\\''\${DB_PASSWORD}'\\'';\""
        act "drop/create $DB_NAME_ACT（旧库数据将被备份内容替换）" "
            psql_super \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DB_NAME_ACT';\" || true
            psql_super \"DROP DATABASE IF EXISTS $DB_NAME_ACT;\"
            psql_super \"CREATE DATABASE $DB_NAME_ACT OWNER $DB_USER_ACT;\""
        act "灌入备份数据（pg_dump 还原）" "
            set -a; source '$ENV_FILE'; set +a
            PGPASSWORD=\"\${DB_PASSWORD:-}\" psql -h '$DB_HOST_ACT' -U '$DB_USER_ACT' -d '$DB_NAME_ACT' -q -v ON_ERROR_STOP=1 -f '$TMP/state/db.sql'"
        [ "$DRY" = "1" ] || act "验证行数对账" "
            set -a; source '$ENV_FILE'; set +a
            N=\$(PGPASSWORD=\"\${DB_PASSWORD:-}\" psql -h '$DB_HOST_ACT' -U '$DB_USER_ACT' -d '$DB_NAME_ACT' -tA -c 'select count(*) from compile_tasks')
            echo \"     compile_tasks 行数=\$N（恢复成功）\""
    else
        echo "   • 备份包不含 db.sql——跳过数据库（文件照常恢复）"
    fi

    # 3) 运行数据：产物树 / 货架 / 轮换缓存（rsync --delete 回到备份时点）
    #    落点=解析出的当前生效路径（ART_DIR/CACHE_DIR）——目标机换了数据根也放对地方
    for pair in "artifacts:$ART_DIR" "cache:$CACHE_DIR" "backend_cache:$ROOT/backend/cache"; do
        src="${pair%%:*}"; dst="${pair#*:}"
        [ -d "$TMP/data/$src" ] || continue
        act "数据目录 $dst ← 备份（多余文件删除）" \
            "mkdir -p '$dst'; rsync -a --delete '$TMP/data/$src/' '$dst/'"
    done

    # 4) HTTPS 证书与主 nginx.conf
    [ -f "$TMP/state/nginx-ssl.tar.gz" ] && act "恢复 /etc/nginx/ssl" \
        "sudo tar xzf '$TMP/state/nginx-ssl.tar.gz' -C /etc/nginx"
    [ -f "$TMP/state/nginx.conf" ] && act "恢复 /etc/nginx/nginx.conf（现版转存 .pre-restore）" \
        "sudo cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.pre-restore 2>/dev/null || true; sudo cp '$TMP/state/nginx.conf' /etc/nginx/nginx.conf"

    # 5) 可选：覆盖 git 跟踪的机器改动（默认不——代码保持最新拉取版）
    if [ "${RESTORE_GITTRACKED:-0}" = "1" ]; then
        [ -d "$TMP/state/skills" ] && act "覆盖 skills/（RESTORE_GITTRACKED=1）" "rsync -a '$TMP/state/skills/' '$ROOT/skills/'"
        [ -f "$TMP/state/config.yaml" ] && act "覆盖 backend/config.yaml（RESTORE_GITTRACKED=1）" "cp '$TMP/state/config.yaml' '$ROOT/backend/config.yaml'"
    fi

    # 6) 收口：幂等 install（pg_hba/建表/nginx 模板/缺 JWT 自愈）+ 重启
    act "setup.sh install（幂等自愈）" "[ \"$DRY\" = 1 ] || bash '$ROOT/setup.sh' install"
    act "setup.sh restart"             "[ \"$DRY\" = 1 ] || bash '$ROOT/setup.sh' restart"

    rm -rf "$TMP"   # 含 dry-run：解出来的临时目录一律回收
    echo "== 恢复流程结束${DRY:+（dry-run：以上均未实际执行）}"
}

case "${1:-}" in
    backup)  do_backup "${2:-}" ;;
    restore) shift; do_restore "${1:-}" "${2:-}" ;;
    *) grep '^# 用法' -A5 "$0" | head -8; exit 1 ;;
esac
