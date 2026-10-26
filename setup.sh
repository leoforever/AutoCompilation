#!/bin/bash
# 龙芯智能编译平台 - 安装启动脚本
# 用法: bash setup.sh [install|init|start [dev|prod]|stop|restart [dev|prod]|status|dev|prod]
#
#   start         生产模式启动（默认；uvicorn 无 --reload）
#   start dev     开发模式启动（uvicorn --reload，改 .py 自动热加载）
#   dev / prod    等同 'start dev' / 'start prod'
#   restart       重启（保持当前模式，可用 restart dev/prod 切换）
#   restart dev   切到开发模式重启
#   restart prod  切到生产模式重启
#   stop          按端口探活后停掉（不依赖 PID 文件）
#   install       一次性环境准备（装 PG、建库装表、装 Python/前端依赖、默认账号）
#   init          仅做"建表 + 默认 admin 账号"，便于重置或首次启动
#   status        按端口探活看后端/前端/PG 状态

set -e
# 路径：默认是 setup.sh 所在目录的父目录（即仓库根），可被环境变量覆盖
# 用法：COMPILE_PLATFORM_DIR=/your/path bash setup.sh install
: "${COMPILE_PLATFORM_DIR:=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PROJECT_DIR="$COMPILE_PLATFORM_DIR"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/frontend"
LOG_DIR="$PROJECT_DIR/logs"
PID_DIR="$LOG_DIR/pids"
ENV_FILE="$BACKEND_DIR/.env.compile"
CONFIG_FILE="$BACKEND_DIR/config.yaml"
BACKEND_PORT=8000
FRONTEND_PORT=3000

mkdir -p "$LOG_DIR" "$PID_DIR"

# 颜色输出
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# 通用探活：按端口找监听 PID
port_pid() {
    local port=$1
    ss -tlnp 2>/dev/null \
        | awk -v p=":$port" '$4 ~ p {print $0}' \
        | grep -oP 'pid=\K[0-9]+' \
        | head -1
}

# 杀端口上跑的东西（杀 pid 本身 + 递归子进程）
kill_port() {
    local port=$1
    local label=$2
    local pid
    pid=$(port_pid "$port")
    if [ -z "$pid" ]; then
        info "$label 未运行（端口 $port 空闲）"
        return 0
    fi
    info "停止 $label（端口 $port，PID $pid）"

    # 收集所有要杀的 PID：监听进程 + 它的子进程（递归）+ 同组其它进程
    local pids_to_kill="$pid"
    local frontier="$pid"
    while [ -n "$frontier" ]; do
        local next=""
        local p
        for p in $frontier; do
            local children
            children=$(pgrep -P "$p" 2>/dev/null | tr '\n' ' ' || true)
            next="$next $children"
        done
        pids_to_kill="$pids_to_kill $next"
        frontier=$(echo "$next" | tr -s ' \n' ' ' | sed 's/^ *//;s/ *$//')
    done
    # 去重
    pids_to_kill=$(echo "$pids_to_kill" | tr -s ' \n' ' ' | sed 's/^ *//;s/ *$//')

    # TERM 整个树
    for p in $pids_to_kill; do
        kill -TERM "$p" 2>/dev/null || true
    done
    # 30s 优雅期：lifespan 自身会 wait futures 30s，worker 跑长编译也该 30s 内回
    # 教训 9/30：worker 卡 agent.stream() 时 lifespan 等到 30s 后 uvicorn 不死，5s 不够
    local grace=30
    for _ in $(seq 1 "$grace"); do
        sleep 1
        if [ -z "$(port_pid "$port")" ]; then
            info "$label 已停止"
            return 0
        fi
    done
    warn "$label ${grace}s 内未退出，强制 SIGKILL（lifespan 卡住走 fallback）"
    # 杀整个进程组（kill_port 拿到的 pid 是监听进程；进程组还包括 worker thread / child）
    local pgid
    pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)
    if [ -n "$pgid" ] && [ "$pgid" != "0" ]; then
        kill -KILL "-$pgid" 2>/dev/null || true
    fi
    # 再 KILL 一遍树里剩余的 pid（pgid 没覆盖到子进程 / worker thread 的）
    for p in $pids_to_kill; do
        kill -KILL "$p" 2>/dev/null || true
    done
    sleep 1
    if [ -z "$(port_pid "$port")" ]; then
        info "$label 已停止（SIGKILL 收尾）"
    else
        error "$label SIGKILL 后仍占用端口 $port（pid=$(port_pid "$port")）"
    fi
}

# 真探活：HTTP 探一下，200/401/403 都算活
http_alive() {
    local url=$1
    local code
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "$url" 2>/dev/null || echo "000")
    [ "$code" != "000" ] && [ "$code" != "404" ]
}

# 后端健康探针（必须先 source env，让 settings 可用）
# 输出 "READY:<code>" / "FAIL:<code>"，code=000 表示连不上
backend_healthcheck() {
    local code
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 \
        -X POST "http://localhost:$BACKEND_PORT/api/auth/login" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "username=__hc__&password=__hc__" 2>/dev/null)
    : "${code:=000}"
    if [ "$code" = "000" ] || [ "$code" = "404" ]; then
        echo "FAIL:$code"
    else
        echo "READY:$code"
    fi
}

# 列本机所有非 loopback / 非 link-local 的 IPv4（用于访问地址展示）
# 多网卡环境会显示多个 IP（用户挑选）
_local_ipv4s() {
    # 优先 ip 命令（多数系统都有）
    if command -v ip >/dev/null 2>&1; then
        ip -4 -o addr show scope global 2>/dev/null \
            | awk '{print $4}' | cut -d/ -f1
        return
    fi
    # 回退 hostname -I（busybox / alpine 也支持）
    if command -v hostname >/dev/null 2>&1 && hostname -I >/dev/null 2>&1; then
        hostname -I | tr ' ' '\n' | grep -v '^$'
        return
    fi
    # 实在不行用 ifconfig（macOS 之类）
    if command -v ifconfig >/dev/null 2>&1; then
        ifconfig 2>/dev/null | awk '/inet / && !/127.0.0.1/ {print $2}'
        return
    fi
    # 全部失败：列出 localhost（最后兜底）
    echo "localhost"
}

# ========== 安装（一次性） ==========
do_install() {
    # .env.compile 不入库（真实 AI key/Docker 配置），首次安装从模板复制
    # 复制后立刻停在这里等用户编辑，避免后面跑 init 才发现 AI key 是空
    if [ ! -f "$ENV_FILE" ]; then
        if [ ! -f "${ENV_FILE}.example" ]; then
            error "缺少 ${ENV_FILE}.example 模板（git 应入库），无法初始化 .env.compile"
        fi
        cp "${ENV_FILE}.example" "$ENV_FILE"
        info "已从模板创建 $ENV_FILE（AI 三件套必填；运维参数在 config.yaml）"
        # C1（2026-10-26）：JWT 密钥自动生成——fail-fast 配套，操作员无需手造
        if ! grep -q "^export JWT_SECRET_KEY=\".\+\"" "$ENV_FILE" 2>/dev/null; then
            _jwt="$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
            [ -n "$_jwt" ] || error "生成 JWT_SECRET_KEY 失败（需 openssl 或 /dev/urandom）"
            grep -q "^export JWT_SECRET_KEY" "$ENV_FILE" || echo "" >> "$ENV_FILE"
            sed -i "s|^export JWT_SECRET_KEY=.*|export JWT_SECRET_KEY="$_jwt"|" "$ENV_FILE"
            info "已自动生成 JWT_SECRET_KEY（32B 随机；缺失则拒绝启动）"
        fi
        warn "请立刻编辑 $ENV_FILE，至少填好以下 3 项："
        warn "  1) AI_API_KEY  —— LLM API 密钥（AI 调用必需）"
        warn "  2) AI_BASE_URL —— LLM API base URL"
        warn "  3) AI_MODEL   —— 模型名"
        if [ -t 0 ]; then
            read -p "编辑好 .env.compile 后按回车继续（Ctrl+C 中止安装）: " _ || true
        else
            error "非交互式终端：手动编辑 $ENV_FILE 后再跑 bash setup.sh install"
        fi
    fi

    # config.yaml 随 git 入库（参数无保密性，不再维护 .example 模板）
    if [ ! -f "$CONFIG_FILE" ]; then
        error "缺少 $CONFIG_FILE（本应随 git 入库；若被 --purge 删过可 git checkout 恢复）"
    fi

    info "检查 PostgreSQL..."
    if ! command -v psql &>/dev/null; then
        warn "PostgreSQL 未安装，正在安装..."
        dnf install -y postgresql-server postgresql-contrib \
            || yum install -y postgresql-server postgresql-contrib
        # 幂等：重复跑 install 时 DB 已初始化（initdb 报错"already initialized"）属正常
        if sudo -u postgres /usr/bin/postgresql-setup --initdb 2>/dev/null; then
            info "PostgreSQL 已初始化"
        else
            info "PostgreSQL 已初始化（initdb 跳过 — 重复运行 install 时属正常）"
        fi
        sudo systemctl enable postgresql 2>/dev/null || true
        sudo systemctl start postgresql 2>/dev/null || true
        sleep 2
    else
        info "PostgreSQL 已安装"
        # 已安装但服务可能未启动（机器重启后）— 探活 + 启
        if ! pg_isready -q 2>/dev/null; then
            sudo systemctl start postgresql 2>/dev/null || true
            sleep 2
        fi
        info "PostgreSQL 服务运行中"
    fi

    info "安装 postgresql-devel（编译/构建 PG 扩展需要；多数 pip PG 包推荐装）..."
    # 即使 psycopg2-binary 不需要 devel，未来若装 psycopg2 源码版 / 其他 PG 扩展会用上
    # 幂等：已装 dnf 会跳过
    dnf install -y postgresql-devel 2>/dev/null || \
        yum install -y postgresql-devel 2>/dev/null || \
        info "postgresql-devel 安装失败（可能是离线环境），跳过"

    info "检查 git（worker 克隆源码用）..."
    if ! command -v git &>/dev/null; then
        warn "git 未安装，正在安装..."
        dnf install -y git || yum install -y git
    else
        info "git 已安装"
    fi

    info "检查 docker daemon（worker 编译沙箱依赖）..."
    if ! command -v docker &>/dev/null; then
        warn "docker 未安装"
        warn "编译平台 worker 通过 Docker 起容器，缺 docker 后端能起但编译任务全挂"
        warn "请手动安装 docker（https://docs.docker.com/engine/install/）后重跑"
        # 不强制退出 —— 让后端能起来，编译功能等 docker 装好再 work
    else
        info "docker 已安装"
    fi

    # T-9（2026-10-26）：口令不再硬编码——.env.compile 已有 DB_PASSWORD 用之，
    # 没有则生成并写入（与 rotate-db-password 同一把钥匙），再建用户
    ensure_db_password
    info "创建数据库用户和数据库...（口令取自 .env.compile，不回显）"
    ( set -a; source "$ENV_FILE"; set +a
      sudo -u postgres psql -c "CREATE USER compile_user WITH PASSWORD '$DB_PASSWORD';" 2>/dev/null \
        || warn "CREATE USER 失败（local md5 下 sudo -u postgres 免密不可用属预期）——全新环境请 sudo -u postgres psql 手工建 user/db，口令取自 $ENV_FILE 的 DB_PASSWORD" )
    sudo -u postgres psql -c "CREATE DATABASE compile_platform OWNER compile_user;" 2>/dev/null || true

    info "配置 PG 认证：pg_hba.conf 把 host 行的 ident 改 md5（修复 Ident 认证失败）..."
    # 默认 PG 是 peer 认证（基于 OS 用户），应用通过 tcp 连接时密码不参与验证
    # 改：把所有 host 行的 ident 改成 md5（密码认证，包括 replication 行）
    HBA_FILE=$(sudo -u postgres psql -tAc "SHOW hba_file;" 2>/dev/null)
    if [ -z "$HBA_FILE" ] || [ ! -f "$HBA_FILE" ]; then
        warn "无法定位 pg_hba.conf 路径（SHOW 失败），跳过认证配置"
        warn "若应用连不上 DB，请手动执行下面命令："
        warn "  HBA=/var/lib/pgsql/data/pg_hba.conf"
        warn "  cp \$HBA \$HBA.bak && sudo sed -i.bak '/^host\\s.*ident$/s/ident\$/md5/' \$HBA && sudo systemctl reload postgresql"
    else
        info "pg_hba.conf: $HBA_FILE"
        # 检查是否已转换过（幂等）
        HBA_MARK="# --- compile-platform managed: ident converted to md5 ---"
        if sudo grep -q "$HBA_MARK" "$HBA_FILE" 2>/dev/null; then
            info "pg_hba.conf 已含 compile-platform managed 配置，跳过"
        else
            # 备份原文件
            sudo cp "$HBA_FILE" "${HBA_FILE}.compile-platform.bak"
            info "已备份原 pg_hba.conf 到 ${HBA_FILE}.compile-platform.bak"
            # sed 替换 host 行末尾的 ident → md5（只改 host 行，不动 local 行；local 是 peer 没问题）
            sudo sed -i.bak '/^host\s.*ident$/s/ident$/md5/' "$HBA_FILE"
            # 在文件开头加标记（用 # 注释，避免匹配规则影响）
            sudo sed -i "1i $HBA_MARK" "$HBA_FILE"
            # reload 让 pg_hba.conf 生效（无需 restart）
            if sudo systemctl reload postgresql 2>/dev/null; then
                info "PG 配置已 reload（host 行已从 ident 改 md5）"
            else
                warn "systemctl reload 失败（systemd 不可用），请手动 pg_ctl reload"
            fi
        fi
    fi

    info "安装 Python 依赖（fastapi / sqlalchemy / psycopg2-binary / python-jose / passlib / langchain-openai 等）..."
    cd "$BACKEND_DIR"
    pip install -r requirements.txt -q

    info "安装前端依赖..."
    cd "$FRONTEND_DIR"
    # npm install 本身幂等（已装会 skip）；加 --no-audit --no-fund 加速
    npm install --no-audit --no-fund --silent 2>&1 | tail -20
    # 不主动拉 docker 镜像：每个目标系统的镜像地址 / tag 由部署者配 DOCKER_IMAGE，
    # 第一次编译任务提交时 worker 会自动 docker pull。强制 install 阶段拉只会拖慢。

    info "建表 + 默认管理员账号..."
    do_init
}

# ========== 后端解释器探测（init 用；需 sqlalchemy+bcrypt） ==========
# PATH 的 python3 可能没装依赖（本机 3.13 无 sqlalchemy/bcrypt，3.11 有），逐个试取第一个可用的
detect_init_pybin() {
    local cand
    for cand in python3 /usr/bin/python3.11 /usr/bin/python3.12 /usr/bin/python3; do
        if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sqlalchemy, bcrypt' >/dev/null 2>&1; then
            echo "$cand"
            return 0
        fi
    done
    echo python3
}

# ========== 初始化（建表 + 幂等迁移 + 默认账号） ==========
# T-9（2026-10-26）：DB 口令生成与轮换——口令唯 .env.compile 的 DB_PASSWORD（git 不养凭据）
ensure_db_password() {
    # 已有非空 DB_PASSWORD → 不动；否则生成写入（install/init 自愈路径，幂等）
    [ -f "$ENV_FILE" ] || return 0
    if ! grep -q "^export DB_PASSWORD=\".\+\"" "$ENV_FILE" 2>/dev/null; then
        _dbp="$(openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
        grep -q "^export DB_PASSWORD" "$ENV_FILE" || echo "" >> "$ENV_FILE"
        sed -i "s|^export DB_PASSWORD=.*|export DB_PASSWORD="$_dbp"|" "$ENV_FILE"
        unset _dbp
        warn "已在 $ENV_FILE 生成 DB_PASSWORD（首次升级自动执行；库侧口令需 setup.sh rotate-db-password 对齐）"
    fi
}

do_rotate_db_password() {
    # 一条命令闭环：备份→生成→ALTER USER→写 env→验证新口令→提示 restart
    # （"只挪文件不轮换=没修"——本命令让轮换成为可执行动作而非手工 SQL）
    [ -f "$ENV_FILE" ] || { error "缺少 $ENV_FILE"; exit 1; }
    set -a; source "$ENV_FILE"; set +a
    local old_pass="${DB_PASSWORD:-}"
    local new_pass
    new_pass="$(openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    [ -n "$new_pass" ] || { error "生成失败（需 openssl 或 /dev/urandom）"; exit 1; }

    info "① 备份整库（pg_dump → cache/pre-rotate-latest.sql.gz）"
    mkdir -p "$BACKEND_DIR/cache"
    if [ -n "$old_pass" ] && PGPASSWORD="$old_pass" psql -h localhost -U compile_user -d compile_platform -t -A -c "SELECT 1" >/dev/null 2>&1; then
        PGPASSWORD="$old_pass" pg_dump -h localhost -U compile_user compile_platform | gzip > "$BACKEND_DIR/cache/pre-rotate-latest.sql.gz"
        info "  备份完成：$BACKEND_DIR/cache/pre-rotate-latest.sql.gz ($(du -h "$BACKEND_DIR/cache/pre-rotate-latest.sql.gz" | cut -f1))"
    else
        warn "  旧口令连不上（可能已轮换过/env 与库不一致），跳过备份继续"
    fi

    info "② ALTER USER compile_user（PG 侧改密，数据/权限零变化）"
    # 注：本机 pg_hba local 已被改 md5，sudo -u postgres 裸 socket 会要密码——
    # 用旧口令自连执行 ALTER（rotate 的前提就是 env 里旧口令可用，此路必通）
    if ! PGPASSWORD="$old_pass" psql -h localhost -U compile_user -d compile_platform \
          -c "ALTER USER compile_user WITH PASSWORD '$new_pass';" >/dev/null 2>&1; then
        error "ALTER 失败（旧口令连不上？）。手工执行：psql -U compile_user 后 ALTER USER ... PASSWORD '<新串>'，env 已预写新值请保持一致"
        exit 1
    fi

    info "③ 写入 .env.compile"
    grep -q "^export DB_PASSWORD" "$ENV_FILE" || echo "" >> "$ENV_FILE"
    sed -i "s|^export DB_PASSWORD=.*|export DB_PASSWORD="$new_pass"|" "$ENV_FILE"
    chmod 600 "$ENV_FILE"

    info "④ 验证新口令可登录"
    if PGPASSWORD="***" psql -h localhost -U compile_user -d compile_platform -t -A -c "SELECT 1" | grep -q 1; then
        info "  新口令验证 OK —— 请 bash setup.sh restart 让后端带新凭据重启（数据零影响；仅重启窗 ~1min）"
    else
        error "  新口令验证失败！手工回滚：sudo -u postgres psql -c 'ALTER USER compile_user WITH PASSWORD ...' 重置回旧值；env 此时未被修改仍是旧口令"
        exit 1
    fi
}

do_init() {
    [ -f "$ENV_FILE" ] || error "缺少 $ENV_FILE，worker/数据库配置会缺"
    # C1 现网升级路径：老 .env.compile 没有 JWT_SECRET_KEY → 补生成一次（此后固定）
    if ! grep -q "^export JWT_SECRET_KEY=\".\+\"" "$ENV_FILE" 2>/dev/null; then
        _jwt="$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
        echo "export JWT_SECRET_KEY="$_jwt"" >> "$ENV_FILE"
        warn "已在 $ENV_FILE 补生成 JWT_SECRET_KEY（首次升级自动执行；全员旧 token 失效需重登）"
    fi
    # T-9 同款：DB 口令缺失补生成（仅写 env；库侧对齐用 rotate-db-password）
    ensure_db_password
    cd "$BACKEND_DIR"
    set -a; source "$ENV_FILE"; set +a

    # 密码策略（T2）：ADMIN_INIT_PASSWORD 环境变量显式传入；
    # 未设时交互式终端 read -s 输入；非交互 fail-fast（拒绝弱默认密码建库）
    local admin_user="${ADMIN_INIT_USERNAME:-admin}"
    if [ -z "${ADMIN_INIT_PASSWORD:-}" ]; then
        if [ -t 0 ]; then
            local pw1 pw2
            read -sp "设置管理员($admin_user)密码: " pw1; echo
            read -sp "确认密码: " pw2; echo
            [ ${#pw1} -ge 8 ] || error "密码至少 8 位"
            [ "$pw1" = "$pw2" ] || error "两次输入不一致"
            ADMIN_INIT_PASSWORD="$pw1"
        else
            error "必须显式传入管理员密码：ADMIN_INIT_PASSWORD='<密码>' bash setup.sh init"
        fi
    fi
    export ADMIN_INIT_PASSWORD ADMIN_INIT_USERNAME

    # T1：走 models.init_db() —— create_all + _safe_add/drop_column 幂等迁移 + 种子，
    # 与 lifespan(main.py) 同一函数；pg_dump 恢复的旧库也能对齐到最新表结构
    "$(detect_init_pybin)" <<'PY'
import os
from app.models import init_db, engine
from sqlalchemy import text
import bcrypt

init_db()
print('  ✅ 建表 + 幂等迁移 + 种子完成（init_db 全链路）')

with engine.connect() as conn:
    r = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public';"))
    tables = [row[0] for row in r.fetchall()]
    print(f'  ✅ 表列表: {tables}')

    r = conn.execute(text("SELECT COUNT(*) FROM users;"))
    count = r.scalar()
    if count == 0:
        admin = os.environ["ADMIN_INIT_USERNAME"]
        password_hash = bcrypt.hashpw(os.environ["ADMIN_INIT_PASSWORD"].encode(), bcrypt.gensalt(rounds=12)).decode()
        conn.execute(
            text("INSERT INTO users (username, password_hash, role, is_active) "
                 "VALUES (:u, :p, :r, :a)"),
            {'u': admin, 'p': password_hash, 'r': 'admin', 'a': True}
        )
        conn.commit()
        print(f'  ✅ 默认管理员账号已创建 ({admin})，密码取自环境变量 ADMIN_INIT_PASSWORD')
    else:
        print('  ⏭  管理员账号已存在，跳过')
PY
}

# ========== 启动 ==========
do_start() {
    # COMPILE_MODE 控制 dev / prod：
    #   dev  → uvicorn --reload（改 .py 自动重启；开发用）
    #   prod → uvicorn 无 --reload（生产用，性能优先）
    # 默认 prod（生产优先），显式传 dev 或 env COMPILE_MODE=dev 才走 dev
    local mode="${1:-${COMPILE_MODE:-prod}}"
    # 1) 如果端口已被占，先按"已运行"语义处理（避免重复拉起导致 bind 失败）
    if [ -n "$(port_pid "$BACKEND_PORT")" ]; then
        warn "后端已在运行（端口 $BACKEND_PORT 已被占用，PID $(port_pid "$BACKEND_PORT")）"
    else
        if [ ! -f "$ENV_FILE" ]; then
            error "缺少 $ENV_FILE，无法 source 环境变量"
        fi
        info "启动后端 (mode=$mode)..."
        cd "$BACKEND_DIR"
        set -a; source "$ENV_FILE"; set +a
        # 选解释器：PATH 的 python3 可能没装依赖（本机 3.13 无 uvicorn，3.11 有）
        # 逐个试 import uvicorn，取第一个能用的；全都不行才回退 python3（报原来的错）
        PYBIN=""
        for cand in python3 /usr/bin/python3.11 /usr/bin/python3.12 /usr/bin/python3; do
            if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import uvicorn' >/dev/null 2>&1; then
                PYBIN="$cand"; break
            fi
        done
        [ -z "$PYBIN" ] && PYBIN=python3
        info "后端解释器: $PYBIN ($($PYBIN --version 2>&1))"
        # 按模式决定是否加 --reload
        if [ "$mode" = "dev" ]; then
            nohup "$PYBIN" -m uvicorn app.main:app \
                --host 0.0.0.0 --port "$BACKEND_PORT" --reload \
                --reload-dir app \
                >> "$LOG_DIR/backend.log" 2>&1 &
        else
            nohup "$PYBIN" -m uvicorn app.main:app \
                --host 0.0.0.0 --port "$BACKEND_PORT" \
                >> "$LOG_DIR/backend.log" 2>&1 &
        fi
        disown || true
        # 等最多 15 秒探活
        for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
            sleep 1
            result=$(backend_healthcheck)
            if [[ "$result" == READY:* ]]; then
                info "后端启动成功（HTTP ${result#READY:}）"
                break
            fi
            if [ "$i" = "15" ]; then
                error "后端启动失败（最后一次探活 $result），请看 $LOG_DIR/backend.log"
            fi
        done
    fi

    if [ -n "$(port_pid "$FRONTEND_PORT")" ]; then
        warn "前端已在运行（端口 $FRONTEND_PORT 已被占用，PID $(port_pid "$FRONTEND_PORT")）"
    else
        info "启动前端..."
        cd "$FRONTEND_DIR"
        nohup npm run dev -- --host 0.0.0.0 \
            >> "$LOG_DIR/frontend.log" 2>&1 &
        disown || true
        for i in 1 2 3 4 5 6 7 8 9 10; do
            sleep 1
            if [ -n "$(port_pid "$FRONTEND_PORT" || true)" ] \
                && http_alive "http://localhost:$FRONTEND_PORT/"; then
                info "前端启动成功"
                break
            fi
            if [ "$i" = "10" ]; then
                warn "前端未在 10 秒内就绪，请看 $LOG_DIR/frontend.log"
            fi
        done
    fi

    echo ""
    info "访问地址:"
    # 只列主 IP（default route 对应的网卡 IP），3 行清爽
    # 回退链：ip route get → 所有非 loopback 的第一个 → localhost
    local main_ip
    main_ip=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
    if [ -z "$main_ip" ] || [ "$main_ip" = "1.1.1.1" ]; then
        main_ip=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
    fi
    if [ -z "$main_ip" ]; then
        main_ip="localhost"
    fi
    info "  前端: http://$main_ip:$FRONTEND_PORT/"
    info "  API:  http://$main_ip:$BACKEND_PORT/docs"
    info "  默认账号: admin（密码为 init 时 ADMIN_INIT_PASSWORD 传入值，勿用默认弱口令）"
    info "  提示：其他网卡 IP 可手动拼接 http://<ip>:$FRONTEND_PORT/"
}

# ========== 卸载（数据级重置，清数据留骨架） ==========
# 语义：跑完 uninstall 再 bash setup.sh install = 全新部署。
# 分级：
#   uninstall          默认档：停服务+清平台容器+DROP DB+删 artifacts/logs/cache（备份先行；保留 镜像/skills/配置）
#   uninstall --purge  追加：自制镜像、skills/、.env.compile、config.yaml、dist、node_modules、PG 账号
#   uninstall --yes    跳过总确认（--purge 的镜像删除仍单独二次确认）
# 影子测试：DB_NAME=<库> + ART_DIR/LOG_DIR/CACHE_DIR 覆盖，勿在生产跑
do_uninstall() {
    local purge=0 assume_yes=0
    local a
    for a in "$@"; do
        case "$a" in
            --purge) purge=1 ;;
            --yes|-y) assume_yes=1 ;;
            *) warn "忽略未知参数: $a" ;;
        esac
    done

    # DB 参数（config.yaml 为权威；影子测试可用 DB_NAME 覆盖）
    local db_host db_port db_user db_name db_pass
    db_host=$(awk '/postgresql:/{f=1;next} f&&/host:/{print $2;exit}' "$CONFIG_FILE" 2>/dev/null)
    db_port=$(awk '/postgresql:/{f=1;next} f&&/port:/{print $2;exit}' "$CONFIG_FILE" 2>/dev/null)
    db_user=$(awk '/postgresql:/{f=1;next} f&&/username:/{print $2;exit}' "$CONFIG_FILE" 2>/dev/null)
    db_name=$(awk '/postgresql:/{f=1;next} f&&/database:/{print $2;exit}' "$CONFIG_FILE" 2>/dev/null)
    db_pass=$(awk '/postgresql:/{f=1;next} f&&/password:/{print $2;exit}' "$CONFIG_FILE" 2>/dev/null)
    db_host="${db_host:-localhost}"; db_port="${db_port:-5432}"
    db_user="${db_user:-compile_user}"; db_name="${DB_NAME:-${db_name:-compile_platform}}"
    # T-9：config.yaml 已无 password 行——env DB_PASS > .env.compile DB_PASSWORD > 报错
    [ -z "$db_pass" ] && [ -f "$ENV_FILE" ] && db_pass="$(source "$ENV_FILE" 2>/dev/null && echo "$DB_PASSWORD")"
    db_pass="${DB_PASS:-$db_pass}"
    # 统一走 -h（TCP）+ 密码：pg_hba 把 local 也设成了 md5，sudo -u postgres 裸 socket 会 fe_sendauth
    local PSQLU="psql -h $db_host -p $db_port -U $db_user"
    export PGPASSWORD="***"

    # 路径（UNINSTALL_* 前缀覆盖，影子测试指向 /tmp 假目录；
    # 注意不能用 LOG_DIR 裸名——setup.sh 顶部无条件赋值会覆盖外部 env，撞车教训）
    local skills_dir="${SKILLS_DIR:-$PROJECT_DIR/skills}"
    local dist_dir="$FRONTEND_DIR/dist"
    local nm_dir="$FRONTEND_DIR/node_modules"
    local art_dir="${UNINSTALL_ART_DIR:-$PROJECT_DIR/artifacts}"
    local log_dir="${UNINSTALL_LOG_DIR:-$PROJECT_DIR/logs}"
    local cache_dir="${UNINSTALL_CACHE_DIR:-$PROJECT_DIR/cache}"

    info "===== uninstall${purge:+（--purge 全清档）} 目标 ====="
    echo "  数据库: $db_name@$db_host:$db_port（pg_dump 备份后 DROP）"
    echo "  容器: 平台 task_id label 容器 + 本项目镜像派生容器（仅 Exited 逐个确认）"
    echo "  目录: $art_dir、$log_dir、$cache_dir"
    if [ "$purge" = "1" ]; then
        echo "  --purge 追加: 自制镜像、skills/、.env.compile、config.yaml、dist、node_modules、PG 账号 $db_user"
    else
        echo "  保留: docker 镜像（自制重建代价高）、skills/、.env.compile、config.yaml、源码/git"
    fi
    if [ -d "$art_dir" ]; then
        echo "  磁盘占用: $(du -sh "$art_dir" 2>/dev/null | cut -f1) (artifacts)"
    fi

    if [ "$assume_yes" != "1" ]; then
        if [ ! -t 0 ]; then
            error "非交互终端拒绝执行卸载；确认请加 --yes（如: bash setup.sh uninstall --yes）"
        fi
        printf "确认执行以上删除？输入 yes 继续: "
        local ans; read -r ans
        [ "$ans" = "yes" ] || { info "已取消"; return 0; }
    fi

    # 1) 停服务（SKIP_STOP=1 跳过：影子测试用，避免误停生产服务）
    if [ "${SKIP_STOP:-0}" = "1" ]; then
        info "[1/6] SKIP_STOP=1，不停服务"
    else
        info "[1/6] 停止前后端 + 清理 legacy worker..."
        do_stop
    fi

    # 2) 备份（先于一切删除；--purge 同样强制）
    local backup_dir="$HOME/compile-platform-backup-$(date +%Y%m%d-%H%M%S)"
    info "[2/6] 备份 → $backup_dir"
    mkdir -p "$backup_dir"
    if command -v pg_dump >/dev/null 2>&1; then
        if pg_dump -h "$db_host" -p "$db_port" -U "$db_user" -d "$db_name" > "$backup_dir/db.sql" 2>/dev/null && [ -s "$backup_dir/db.sql" ]; then
            info "  ✅ db 备份 $(du -sh "$backup_dir/db.sql" | cut -f1)"
        else
            rm -f "$backup_dir/db.sql"
            warn "  pg_dump 失败（库可能不存在）——跳过"
        fi
    else
        warn "  无 pg_dump，跳过 DB 备份"
    fi
    if [ -d "$skills_dir" ]; then
        tar czf "$backup_dir/skills.tar.gz" -C "$PROJECT_DIR" skills && info "  ✅ skills/ 已 tar（skills/ 不在 git，唯一副本）"
    fi

    # 3) docker 容器（镜像默认不动）
    if command -v docker >/dev/null 2>&1; then
        info "[3/6] 清理平台容器..."
        # label 口径（引擎 stage3 建容器统一带）；grep -v 防无匹配时把整仓库容器全清
        local ids
        ids=$(docker ps -aq --filter 'label=task_id' --filter 'label=phase=infrastructure' 2>/dev/null | grep -v '^$' || true)
        ids=$(echo "$ids" $(docker ps -aq --filter 'label=ticket_id' 2>/dev/null || true) | tr ' ' '\n' | grep -v '^$' | sort -u || true)
        if [ -n "$ids" ]; then
            echo "$ids" | xargs -r docker rm -f >/dev/null && info "  ✅ 删除 label 容器 $(echo "$ids" | wc -l) 个"
        else
            info "  无 label 容器"
        fi
        if [ "$purge" = "1" ]; then
            if [ ! -t 0 ]; then
                error "--purge 删自制镜像必须交互式二次确认（加 --yes 也不行）"
            fi
            warn "即将删除自制镜像（重建需重新构建/重新 save-load）："
            docker images --format '  {{.Repository}}:{{.Tag}}  {{.Size}}' | grep -E 'loongnix-build|openeuler.*SP4' || true
            printf "输入 yes 确认删镜像: "; local ans2; read -r ans2
            if [ "$ans2" = "yes" ]; then
                docker images --format '{{.Repository}}:{{.Tag}}' | grep -E 'loongnix-build|openeuler.*SP4' | xargs -r docker rmi -f >/dev/null && info "  ✅ 镜像已删"
            else
                warn "  跳过镜像删除"
            fi
        fi
    else
        info "[3/6] 无 docker，跳过容器清理"
    fi

    # 4) 数据库
    info "[4/6] DROP DATABASE $db_name..."
    if $PSQLU -d postgres -c "DROP DATABASE IF EXISTS \"$db_name\";" >/dev/null 2>&1; then
        info "  ✅ 已 DROP（重装由 install/init 重建）"
    else
        warn "  DROP 失败（PG 未运行/库本不存在）——检查 $backup_dir/db.sql 是否在"
    fi

    # 5) 运行数据目录（守门：仓库根直属子目录 或 /tmp 影子目录，其余拒绝）
    info "[5/6] 删运行数据目录..."
    rm_safe() {
        local target
        for target in "$@"; do
            [ -n "$target" ] || { error "拒绝删除空路径"; }
            if [[ ( "$target" == "$PROJECT_DIR"/* && "${target#$PROJECT_DIR/}" != */* && -n "${target#$PROJECT_DIR/}" ) || "$target" == /tmp/* ]]; then
                rm -rf "$target" && info "  ✅ 删除 $target"
            else
                error "路径越界，拒绝删除: $target"
            fi
        done
    }
    rm_safe "$art_dir" "$log_dir" "$cache_dir"
    # 6) --purge 追加
    if [ "$purge" = "1" ]; then
        info "[6/6] --purge 清理..."
        rm_safe "$skills_dir"
        rm -f "$ENV_FILE" "$CONFIG_FILE" && info "  ✅ 删 .env.compile / config.yaml"
        rm -rf "$dist_dir" "$nm_dir" && info "  ✅ 删 dist / node_modules"
        # 删 PG 账号仍需超管（compile_user 无此权限）；本机 pg_hba local=md5 时需带 postgres 密码
        sudo -u postgres psql -c "DROP OWNED BY \"$db_user\"; DROP USER IF EXISTS \"$db_user\";" >/dev/null 2>&1 && info "  ✅ 删 PG 账号 $db_user" || warn "  删 PG 账号失败（可 sudo -u postgres psql 手动 DROP USER）"
    else
        info "[6/6] 非 --purge，无追加清理"
    fi

    info "uninstall 完成。备份在 $backup_dir（含 db.sql / skills.tar.gz）"
    info "从头部署：bash setup.sh install"
}

# ========== 清理孤儿编译 worker（worker.py 独立进程，已废弃架构） ==========
# 2026-10-16 教训：手工 nohup 起的 worker.py 不占端口、PPID=1，kill_port 按端口杀够不到它。
# 它与 uvicorn 内嵌 lifespan worker 同时轮询队列会抢任务——孤儿进程揣着启动时的旧 AI key，
# 换模型/改 .env.compile 后它不会感知，抢到任务就 429/用错模型（T-20261016090418-F81929 实例）。
# 因此 stop/restart 必须把它一并清掉。
kill_legacy_workers() {
    local pids
    # [w]orker 方括号写法防止 pgrep 匹配到自身命令行
    pids=$(pgrep -f '[w]orker\.py' 2>/dev/null | tr '\n' ' ' || true)
    if [ -z "${pids// /}" ]; then
        return 0
    fi
    warn "发现残留 worker.py 独立进程（已废弃架构，会抢任务用旧配置）: PID $pids — 清理"
    for p in $pids; do
        kill -TERM "$p" 2>/dev/null || true
    done
    # 5s 优雅期，然后 SIGKILL 收尾
    for _ in 1 2 3 4 5; do
        sleep 1
        pids=$(pgrep -f '[w]orker\.py' 2>/dev/null | tr '\n' ' ' || true)
        [ -z "${pids// /}" ] && { info "worker.py 残留进程已清理"; return 0; }
    done
    for p in $pids; do
        kill -KILL "$p" 2>/dev/null || true
    done
    sleep 1
    pids=$(pgrep -f '[w]orker\.py' 2>/dev/null || true)
    if [ -n "$pids" ]; then
        error "worker.py 残留进程未能完全清理: $pids"
    else
        info "worker.py 残留进程已清理（SIGKILL 收尾）"
    fi
}

# ========== 停止 ==========
do_stop() {
    kill_port "$FRONTEND_PORT" "前端"
    kill_port "$BACKEND_PORT"  "后端"
    # 手工 nohup 起的孤儿 worker.py 不在端口进程树里，必须显式清理（见上）
    kill_legacy_workers
    # 清理陈旧 PID 文件（探活不再依赖它，留着只是干扰）
    rm -f "$PID_DIR/backend.pid" "$PID_DIR/frontend.pid"
}

# ========== 重启 ==========
# 用法: do_restart [dev|prod] — 默认保持 COMPILE_MODE env；不传则用当前 env
do_restart() {
    do_stop
    do_start "$@"
}

# ========== 状态 ==========
do_status() {
    echo "=== 服务状态（按端口探活，不依赖 PID 文件）==="
    local bp fp
    bp=$(port_pid "$BACKEND_PORT")
    fp=$(port_pid "$FRONTEND_PORT")
    if [ -n "$bp" ]; then
        echo "  后端: 运行中 (端口 $BACKEND_PORT, PID $bp)"
    else
        echo "  后端: 已停止"
    fi
    if [ -n "$fp" ]; then
        echo "  前端: 运行中 (端口 $FRONTEND_PORT, PID $fp)"
    else
        echo "  前端: 已停止"
    fi
    echo ""
    echo "=== 监听端口 ==="
    ss -tlnp 2>/dev/null | grep -E ":($BACKEND_PORT|$FRONTEND_PORT|5432)\b" | awk '{printf "  %-30s %s\n", $4, $6}'
}

# ========== 主入口 ==========
case "${1:-start}" in
    install) do_install ;;
    init)    do_init ;;
    start)   do_start prod ;;    # 默认 prod（生产优先）
    stop)    do_stop ;;
    restart) do_restart "$2" ;;  # 透传模式参数；不传 = 保持 COMPILE_MODE
    status)  do_status ;;
    uninstall) do_uninstall "${@:2}" ;;   # 可选 --purge / --yes
    rotate-db-password) do_rotate_db_password ;;  # T-9：DB 口令一键轮换（备份→ALTER→env→验证）
    dev)     do_start dev ;;     # 显式 dev：uvicorn --reload
    prod)    do_start prod ;;    # 显式 prod：无 --reload
    *)
        echo "用法: $0 {install|init|start [dev|prod]|stop|restart [dev|prod]|status|uninstall [--purge] [--yes]|rotate-db-password|dev|prod}"
        echo
        echo "  start         生产模式启动（默认；无 --reload）"
        echo "  start dev     开发模式启动（uvicorn --reload，改 .py 自动重启）"
        echo "  dev / prod    等同 'start dev' / 'start prod'"
        echo "  restart       重启（保持当前模式，可用 restart dev/prod 切换）"
        echo "  restart dev   切到开发模式重启"
        echo "  restart prod  切到生产模式重启"
        echo "  uninstall     数据级重置（清容器/DB/artifacts，先备份；镜像/skills 保留）"
        echo "  uninstall --purge  连自制镜像/skills/配置全清；--yes 跳过总确认（镜像仍单独确认）"
        exit 1
        ;;
esac
