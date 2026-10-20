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
        info "已从模板创建 $ENV_FILE（AI key / Docker 镜像 / DB 配置都在里面）"
        warn "请立刻编辑 $ENV_FILE，至少填好以下 3 项："
        warn "  1) AI_API_KEY  —— LLM API 密钥（AI 调用必需）"
        warn "  2) AI_BASE_URL —— LLM API base URL"
        warn "  3) AI_MODEL   —— 模型名"
        warn "其它项（MAX_WORKERS、STAGE5_MAX_SECONDS 等）有合理默认值，可暂不改"
        if [ -t 0 ]; then
            read -p "编辑好 .env.compile 后按回车继续（Ctrl+C 中止安装）: " _ || true
        else
            error "非交互式终端：手动编辑 $ENV_FILE 后再跑 bash setup.sh install"
        fi
    fi

    # config.yaml 同理：缺则从 .example 复制
    if [ ! -f "$CONFIG_FILE" ]; then
        if [ ! -f "${CONFIG_FILE}.example" ]; then
            error "缺少 ${CONFIG_FILE}.example 模板（git 应入库），无法初始化 config.yaml"
        fi
        cp "${CONFIG_FILE}.example" "$CONFIG_FILE"
        info "已从模板创建 $CONFIG_FILE（默认配置：PostgreSQL + dev/test 账号）"
        info "生产部署请手动编辑 $CONFIG_FILE（改 secret_key 等）"
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

    info "创建数据库用户和数据库..."
    sudo -u postgres psql -c "CREATE USER compile_user WITH PASSWORD 'compile123';" 2>/dev/null || true
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
    info "  默认账号: admin / admin123"
    info "  提示：其他网卡 IP 可手动拼接 http://<ip>:$FRONTEND_PORT/"
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
    dev)     do_start dev ;;     # 显式 dev：uvicorn --reload
    prod)    do_start prod ;;    # 显式 prod：无 --reload
    *)
        echo "用法: $0 {install|init|start [dev|prod]|stop|restart [dev|prod]|status|dev|prod}"
        echo
        echo "  start         生产模式启动（默认；无 --reload）"
        echo "  start dev     开发模式启动（uvicorn --reload，改 .py 自动重启）"
        echo "  dev / prod    等同 'start dev' / 'start prod'"
        echo "  restart       重启（保持当前模式，可用 restart dev/prod 切换）"
        echo "  restart dev   切到开发模式重启"
        echo "  restart prod  切到生产模式重启"
        exit 1
        ;;
esac
