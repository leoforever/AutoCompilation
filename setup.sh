#!/bin/bash
# 龙芯智能编译平台 - 安装启动脚本
# 用法: bash setup.sh [install|init|start|stop|restart|status]
#
# 职责:
#   install  一次性环境准备（装 PG、建库装表、装 Python/前端依赖、默认账号）
#   init     仅做"建表 + 默认 admin 账号"，便于重置或首次启动
#   start    启动后端 + 前端（不做任何初始化）
#   stop     按端口探活后停掉（不依赖 PID 文件）
#   restart  stop + start
#   status   探活报告实际运行情况

set -e
PROJECT_DIR="/root/.openclaw/workspace/compile-platform"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/frontend"
LOG_DIR="$PROJECT_DIR/logs"
PID_DIR="$LOG_DIR/pids"
ENV_FILE="$BACKEND_DIR/.env.compile"
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

# ========== 安装（一次性） ==========
do_install() {
    info "检查 PostgreSQL..."
    if ! command -v psql &>/dev/null; then
        warn "PostgreSQL 未安装，正在安装..."
        dnf install -y postgresql-server postgresql-contrib \
            || yum install -y postgresql-server postgresql-contrib
        sudo -u postgres /usr/bin/postgresql-setup --initdb \
            || warn "postgresql-setup --initdb 失败（可能已初始化），继续"
        sudo systemctl enable postgresql 2>/dev/null || true
        sudo systemctl start postgresql 2>/dev/null || true
        sleep 2
    fi

    info "创建数据库用户和数据库..."
    sudo -u postgres psql -c "CREATE USER compile_user WITH PASSWORD 'compile123';" 2>/dev/null || true
    sudo -u postgres psql -c "CREATE DATABASE compile_platform OWNER compile_user;" 2>/dev/null || true

    info "安装 Python 依赖..."
    cd "$BACKEND_DIR"
    pip install -r requirements.txt -q

    info "安装前端依赖..."
    cd "$FRONTEND_DIR"
    npm install

    info "建表 + 默认管理员账号..."
    do_init
}

# ========== 初始化（建表 + 默认账号） ==========
do_init() {
    [ -f "$ENV_FILE" ] || error "缺少 $ENV_FILE，worker/数据库配置会缺"
    cd "$BACKEND_DIR"
    set -a; source "$ENV_FILE"; set +a

    python3 <<'PY'
from app.models import Base, engine, User
from sqlalchemy import text
import bcrypt

Base.metadata.create_all(bind=engine)
print('  ✅ 表创建完成')

with engine.connect() as conn:
    r = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public';"))
    tables = [row[0] for row in r.fetchall()]
    print(f'  ✅ 表列表: {tables}')

    r = conn.execute(text("SELECT COUNT(*) FROM users;"))
    count = r.scalar()
    if count == 0:
        password_hash = bcrypt.hashpw(b'admin123', bcrypt.gensalt(rounds=12)).decode()
        conn.execute(
            text("INSERT INTO users (username, password_hash, role, is_active) "
                 "VALUES (:u, :p, :r, :a)"),
            {'u': 'admin', 'p': password_hash, 'r': 'admin', 'a': True}
        )
        conn.commit()
        print('  ✅ 默认管理员账号已创建 (admin / admin123)')
    else:
        print('  ⏭  管理员账号已存在，跳过')
PY
}

# ========== 启动 ==========
do_start() {
    # 1) 如果端口已被占，先按"已运行"语义处理（避免重复拉起导致 bind 失败）
    if [ -n "$(port_pid "$BACKEND_PORT")" ]; then
        warn "后端已在运行（端口 $BACKEND_PORT 已被占用，PID $(port_pid "$BACKEND_PORT")）"
    else
        if [ ! -f "$ENV_FILE" ]; then
            error "缺少 $ENV_FILE，无法 source 环境变量"
        fi
        info "启动后端..."
        cd "$BACKEND_DIR"
        set -a; source "$ENV_FILE"; set +a
        nohup python3 -m uvicorn app.main:app \
            --host 0.0.0.0 --port "$BACKEND_PORT" \
            >> "$LOG_DIR/backend.log" 2>&1 &
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
    info "  前端: http://10.40.65.201:$FRONTEND_PORT/"
    info "  API:  http://10.40.65.201:$BACKEND_PORT/docs"
    info "  默认账号: admin / admin123"
}

# ========== 停止 ==========
do_stop() {
    kill_port "$FRONTEND_PORT" "前端"
    kill_port "$BACKEND_PORT"  "后端"
    # 清理陈旧 PID 文件（探活不再依赖它，留着只是干扰）
    rm -f "$PID_DIR/backend.pid" "$PID_DIR/frontend.pid"
}

# ========== 重启 ==========
do_restart() {
    do_stop
    do_start
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
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_restart ;;
    status)  do_status ;;
    *)
        echo "用法: $0 {install|init|start|stop|restart|status}"
        exit 1
        ;;
esac
