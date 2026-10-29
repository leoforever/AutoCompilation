#!/bin/bash
# 龙芯智能编译平台 - 安装启动脚本
# 用法: bash setup.sh [install|init|start [dev|prod]|stop|restart [dev|prod]|status|dev|prod]
#
#   start         生产模式启动（默认；uvicorn 无 --reload）
#   start dev     开发模式启动（uvicorn --reload，改 .py 自动热加载）
#   dev / prod    切换模式重启（B-4 修：旧版端口被占时静默 no-op，现走 restart
#                 语义——活跃编译/工单会被 A2 闸门拦下确认）
#   restart       重启（保持当前模式，可用 restart dev/prod 切换）
#   restart dev   切到开发模式重启
#   restart prod  切到生产模式重启
#   stop          按端口探活后停掉（不依赖 PID 文件）
#   install       一次性环境准备（装 PG、建库装表、装 Python/前端依赖、默认账号）
#   init          仅做"建表 + 默认 admin 账号"，便于重置或首次启动
#   status        按端口探活看后端/前端/PG 状态
#   rotate-db-password   DB 口令一键轮换（备份→ALTER→验证→写 env→提示 restart）
#   reset-admin-password 管理员登录密码一键重置（忘密码出路；重登生效，token 面不受扰）
#   FORCE_RESTART=1 restart   有活跃编译/工单轮次时跳过闸门强停（默认拦截并确认）

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

# A1 修（2026-10-27）：env 键值统一"写带引号、测带引号"同口径 helper。
# 旧 bug：sed 写成 export KEY=*** *未再包引号），检测却要求
#   ^export KEY="[^"]+"$ —— 永检不到 → 每次 install/init 重复生成覆写：
#   DB_PASSWORD 与库侧脱钩（下次起后端连不上 DB）、JWT 每次换钥（全员 token
#   静默作废）。根因=写/测口径漂移，从此写测必须同源。
env_set_quoted() {  # FILE KEY VALUE
    local f="$1" k="$2" v="$3"
    grep -Eq "^export ${k}=" "$f" 2>/dev/null || printf 'export %s=""\n' "$k" >> "$f"
    sed -i "s|^export ${k}=.*|export ${k}=\"${v}\"|" "$f"
}
env_has_value() {   # FILE KEY → 0 当且仅当存在非空带引号值
    grep -Eq "^export $2=\"[^\"]+\"" "$1" 2>/dev/null
}
gen_secret() {      # [$字节数] → hex 随机串（openssl 首选，urandom 兜底）
    local n="${1:-32}"
    openssl rand -hex "$n" 2>/dev/null \
        || head -c "$n" /dev/urandom | od -An -tx1 | tr -d ' \n'
}

# 通用探活：按端口找监听 PID
port_pid() {
    # C3（2026-10-27）：awk 匹配行尾锚定 :$port$，防 :8000 前缀误命中 :80001
    local port=$1
    ss -tlnp 2>/dev/null \
        | awk -v p=":$port" '$4 ~ (p"$") {print $0}' \
        | grep -oP 'pid=\K[0-9]+' \
        | sort -u
}

# 杀端口上跑的东西（杀 pid 本身 + 递归子进程）
kill_port() {
    local port=$1
    local label=$2
    local pid
    pid=$(port_pid "$port" | tr '\n' ' ' | sed 's/^ *//;s/ *$//')   # B-11：双栈多根全收
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
    # B-2：setsid 后每根服务自成一组——逐根取 pgid 连坐（组内只有自家树），
    # 不再拿第一根的组赌全部；setsid 缺位的极老环境此路径仍保底
    local one pgids=""
    for one in $pids_to_kill; do
        local g
        g=$(ps -o pgid= -p "$one" 2>/dev/null | tr -d ' ' || true)
        [ -n "$g" ] && [ "$g" != "0" ] && pgids="$pgids $g"
    done
    for g in $(echo "$pgids" | tr ' ' '\n' | sort -u | grep -v '^$'); do
        kill -KILL "-$g" 2>/dev/null || true
    done
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
    # B-9 修（2026-10-27）：旧版每次探活 POST 假用户 login——高频轮询制造成堆失败
    # 登录审计，且后端防时序攻击会做 dummy bcrypt（~250ms/次白烧 CPU）。
    # 改探无鉴权静态路由 /docs：200=进程+路由+中间件全活，零副作用
    local code
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 \
        "http://localhost:$BACKEND_PORT/docs" 2>/dev/null)
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

# ========== nginx 生产前端 ==========
# conf 入库 deploy/；主 conf 注入两行 include（openEuler nginx 默认不 include conf.d，
# 不动用户主 conf 的其余内容，幂等判据=compile-platform-managed 标记）。
# root 指 /srv/compile-platform/dist——发布目录方案两个红利：
# ① 仓库若在 /root 下（权限 700）nginx worker 读不进去，/srv 免疫；
# ② 仓库目录搬迁/改名不影响 serving（rsync 每次同步）。
install_nginx_conf() {
    # HTTPS 方案（2026-10-29 →docs/https-8443-plan.md 修订版）：双模板 servers+locations，
    # 3000(HTTP 过渡)/8443(HTTPS)/8080(→301) 三 server；旧单文件 compile-platform.conf 退役
    local srv_src="$PROJECT_DIR/deploy/compile-platform.servers.conf"
    local loc_src="$PROJECT_DIR/deploy/compile-platform.locations.conf"
    if [ ! -f "$srv_src" ] || [ ! -f "$loc_src" ]; then
        warn "缺 deploy/ nginx 模板，跳过配置（prod 将回退 vite preview）"; return 0
    fi
    if ! command -v nginx >/dev/null 2>&1; then
        warn "nginx 未安装——prod 将回退 vite preview。建议: sudo dnf install -y nginx"
        return 0
    fi
    sudo cp "$srv_src" /etc/nginx/compile-platform-servers.conf
    sudo cp "$loc_src" /etc/nginx/compile-platform-locations.conf
    # 证书两段式（CA 存在即复用=客户端信任不失效；叶子按当前 IP 每次重签，SAN 含 127.0.0.1
    # ——首切实测教训：缺 IP:127.0.0.1 时本机严格校验失败）
    bash "$PROJECT_DIR/deploy/gen-certs.sh" || warn "证书生成失败——8443 块将过不了 nginx -t"

    # 主 conf include：存量旧行迁移 / 全新注入单行（判据都用 include 行本身——比标记
    # 注释不易错位；只注入单行，openEuler 主 conf 不通配 conf.d：openclaw-https.conf
    # 与内联 443 server 重复，通配会 duplicate listen 炸 nginx -t）
    sudo cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.compile-platform.bak
    if sudo grep -q "include /etc/nginx/compile-platform\.conf;" /etc/nginx/nginx.conf; then
        sudo sed -i 's|include /etc/nginx/compile-platform\.conf;|include /etc/nginx/compile-platform-servers.conf;|' /etc/nginx/nginx.conf
        info "主 conf include 已迁移 → compile-platform-servers.conf"
    elif ! sudo grep -q "include /etc/nginx/compile-platform-servers.conf" /etc/nginx/nginx.conf; then
        sudo sed -i '0,/http {/s//http {\n    # compile-platform-managed\n    include \/etc\/nginx\/compile-platform-servers.conf;/' /etc/nginx/nginx.conf
        info "已注入主 conf include（servers 版）"
    fi

    if ! sudo nginx -t >/dev/null 2>&1; then
        sudo nginx -t 2>&1 | tail -3
        error "nginx -t 失败——回滚：sudo cp /etc/nginx/nginx.conf.compile-platform.bak /etc/nginx/nginx.conf && sudo nginx -s reload"
    fi
    sudo mkdir -p /srv/compile-platform
    sudo systemctl enable nginx >/dev/null 2>&1 || true
    info "nginx 前端就绪：https://<ip>:8443（CA 分发：/platform-ca.crt）；$FRONTEND_PORT 为 HTTP 过渡期"
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
        # A1 修：走 env_set_quoted/env_has_value，写测同口径（防重复覆写换钥）
        if ! env_has_value "$ENV_FILE" JWT_SECRET_KEY; then
            _jwt="$(gen_secret 32)"
            [ -n "$_jwt" ] || error "生成 JWT_SECRET_KEY 失败（需 openssl 或 /dev/urandom）"
            env_set_quoted "$ENV_FILE" JWT_SECRET_KEY "$_jwt"
            unset _jwt
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

    # 免密①（2026-10-27）：local 行 md5→peer——openEuler 模板 local all=all 带 md5
    # （当年只改了 host 行），sudo -u postgres 裸 socket 从此要一个从未设过的密码：
    # install 的 CREATE USER/DATABASE 一直死等输入或静默失败。SHOW hba_file 自身
    # 要认证（鸡生蛋）→用固定路径。peer 安全边界=OS 层（仅 root/sudo 化身
    # postgres）；host 行不动，应用仍 TCP+env 口令（T-9 治理不受影响）。幂等：已 peer 则跳
    HBA_STD="/var/lib/pgsql/data/pg_hba.conf"
    if [ -f "$HBA_STD" ] && sudo grep -qE '^local[[:space:]]+all[[:space:]]+all[[:space:]]+md5' "$HBA_STD"; then
        sudo sed -i 's/^local\(\s\+\)all\(\s\+\)all\(\s\+\)md5/local\1all\2all\3peer/' "$HBA_STD"
        sudo systemctl reload postgresql 2>/dev/null \
          || sudo -u postgres pg_ctl reload -D /var/lib/pgsql/data 2>/dev/null || true
        info "pg_hba local 行已回 peer（sudo -u postgres 裸 socket 免密恢复）"
    fi

    # T-9（2026-10-26）：口令不再硬编码——.env.compile 已有 DB_PASSWORD 用之，
    # 没有则生成并写入（与 rotate-db-password 同一把钥匙），再建用户
    ensure_db_password
    info "创建数据库用户和数据库...（口令取自 .env.compile，不回显）"
    ( set -a; source "$ENV_FILE"; set +a
      # C1 修（2026-10-27）：口令经 stdin 进 psql，不落 ps 命令行（旧 -c 全进程表可见）
      # C1b 补（同日，实锤于卡死 install 的 ps 现场）：-v pw="$..." 又把口令放回
      # argv——heredoc 非引号即 bash 展开，先 SQL 转义（'→''）再内插；PGPASSWORD
      # 走 env（/proc/…/environ 0400，仅属主/root 可读，是标准可接受面）
      # B-10：空口令拒绝（占位空串未填就建库=埋匿名账号）
      [ -n "${DB_PASSWORD:-}" ] || { warn "DB_PASSWORD 为空（$ENV_FILE 未填）——跳过 CREATE USER"; false; } && true
      _pwq="${DB_PASSWORD//\'/\'\'}"   # SQL 字面量转义：单引号翻倍
      sudo -u postgres psql -v ON_ERROR_STOP=1 >/dev/null 2>&1 <<SQL || warn "CREATE USER 失败（local md5 下 sudo -u postgres 免密不可用属预期）——全新环境请 sudo -u postgres psql 手工建 user/db，口令取自 $ENV_FILE 的 DB_PASSWORD"
CREATE USER compile_user WITH PASSWORD '${_pwq}';
SQL
    )
    # 静默 || true 改出声（u9 演练实锤过"库没建成无人知"）；已存在=正常
    sudo -u postgres psql -c "CREATE DATABASE compile_platform OWNER compile_user;" 2>/dev/null \
      || warn "CREATE DATABASE 未执行（库已存在=正常；全新环境请 \du \l 核对）"

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
    # B-5（2026-10-27）：装进取向统一——优先"已具备后端运行时依赖"的解释器；
    # 一个都没有则认 pip 自己的宿主 python（pip --version 同源），杜绝装进 A 跑 B
    _py="$(detect_pybin)"
    if ! "$_py" -c 'import uvicorn' >/dev/null 2>&1; then
        _pipowner="$(pip --version 2>/dev/null | grep -oP 'python \d+\.\d+' | head -1)"
        [ -n "$_pipowner" ] && command -v "python$_pipowner" >/dev/null 2>&1 && _py="python$_pipowner"
    fi
    info "  依赖安装目标解释器: $_py"
    "$_py" -m pip install -r requirements.txt -q

    # nginx 生产前端（2026-10-27）：静态托管 dist + /api /ws 反代，3000 沿用；
    # serving 路径不再有常驻 node（preview/esbuild 死亡类事故根除）
    install_nginx_conf

    info "安装前端依赖..."
    cd "$FRONTEND_DIR"
    # npm install 本身幂等（已装会 skip）；加 --no-audit --no-fund 加速
    # A3 修（2026-10-27）：`npm … | tail` 管道退出码=tail 的 0，npm 失败被吞、
    # 一路"成功"到启动才爆。重定向日志+查返回值，失败带尾部上下文退出
    if ! npm install --no-audit --no-fund --silent > "$LOG_DIR/npm-install.log" 2>&1; then
        tail -20 "$LOG_DIR/npm-install.log"
        error "npm install 失败（详见 $LOG_DIR/npm-install.log）"
    fi
    # 不主动拉 docker 镜像：每个目标系统的镜像地址 / tag 由部署者配 DOCKER_IMAGE，
    # 第一次编译任务提交时 worker 会自动 docker pull。强制 install 阶段拉只会拖慢。

    info "建表 + 默认管理员账号..."
    do_init
}

# ========== 后端解释器探测（init 用；需 sqlalchemy+bcrypt） ==========
# PATH 的 python3 可能没装依赖（本机 3.13 无 sqlalchemy/bcrypt，3.11 有），逐个试取第一个可用的
# B-5（2026-10-27）：旧版三处解释器逻辑互不知情（install 裸 pip / init 判 sqlalchemy /
# start 判 uvicorn），本机 python3=3.13 与 pip→3.11 已分家——统一判据=uvicorn+
# sqlalchemy+bcrypt 三依赖齐活，三调用点共用本函数（init 兼容别名见下）
detect_pybin() {
    local cand
    for cand in python3 /usr/bin/python3.11 /usr/bin/python3.12 /usr/bin/python3; do
        if command -v "$cand" >/dev/null 2>&1 \
           && "$cand" -c 'import uvicorn, sqlalchemy, bcrypt' >/dev/null 2>&1; then
            echo "$cand"
            return 0
        fi
    done
    echo python3
}
detect_init_pybin() { detect_pybin; }

# ========== 初始化（建表 + 幂等迁移 + 默认账号） ==========
# T-9（2026-10-26）：DB 口令生成与轮换——口令唯 .env.compile 的 DB_PASSWORD（git 不养凭据）
ensure_db_password() {
    # 已有非空 DB_PASSWORD → 不动；否则生成写入（install/init 自愈路径，幂等）
    # A1 修：检测/写入同口径后此函数真正幂等——旧版永判"缺失"每次 init 覆写新值，
    # 与 PG 库侧脱钩（后端连不上 DB）；C6：生成结果断非空
    [ -f "$ENV_FILE" ] || return 0
    if ! env_has_value "$ENV_FILE" DB_PASSWORD; then
        _dbp="$(gen_secret 16)"
        [ -n "$_dbp" ] || error "生成 DB_PASSWORD 失败（需 openssl 或 /dev/urandom）"
        env_set_quoted "$ENV_FILE" DB_PASSWORD "$_dbp"
        unset _dbp
        warn "已在 $ENV_FILE 生成 DB_PASSWORD（首次升级自动执行；库侧口令需 setup.sh rotate-db-password 对齐）"
    fi
}

# B2 修（2026-10-27）：DB 连接参数统一读取——install/rotate/uninstall 同一份口径
# （旧版 rotate 硬编码 localhost/compile_user/compile_platform、uninstall 自带 awk 五连，
#  影子测试/非默认部署必打错库）；env 同名变量优先（DB_HOST/PORT/USER/NAME）
read_db_params() {
    db_host=$(awk '/postgresql:/{f=1;next} f&&/host:/{print $2;exit}' "$CONFIG_FILE" 2>/dev/null)
    db_port=$(awk '/postgresql:/{f=1;next} f&&/port:/{print $2;exit}' "$CONFIG_FILE" 2>/dev/null)
    db_user=$(awk '/postgresql:/{f=1;next} f&&/username:/{print $2;exit}' "$CONFIG_FILE" 2>/dev/null)
    db_name=$(awk '/postgresql:/{f=1;next} f&&/database:/{print $2;exit}' "$CONFIG_FILE" 2>/dev/null)
    db_host="${DB_HOST:-${db_host:-localhost}}"; db_port="${DB_PORT:-${db_port:-5432}}"
    db_user="${DB_USER:-${db_user:-compile_user}}"; db_name="${DB_NAME:-${db_name:-compile_platform}}"
}

do_rotate_db_password() {
    # 一条命令闭环：备份→生成→ALTER→验证新口令→写 env→提示 restart
    # （"只挪文件不轮换=没修"——本命令让轮换成为可执行动作而非手工 SQL）
    # C2 修：验证先于写 env——旧顺序"验证失败"提示"env 未修改"与事实不符（③已写）
    [ -f "$ENV_FILE" ] || { error "缺少 $ENV_FILE"; exit 1; }
    set -a; source "$ENV_FILE"; set +a
    read_db_params
    local old_pass="${DB_PASSWORD:-}"
    local new_pass
    new_pass="$(gen_secret 16)"
    [ -n "$new_pass" ] || { error "生成失败（需 openssl 或 /dev/urandom）"; exit 1; }

    info "① 备份整库（pg_dump → cache/pre-rotate-latest.sql.gz）"
    mkdir -p "$BACKEND_DIR/cache"
    if [ -n "$old_pass" ] && PGPASSWORD="$old_pass" psql -h "$db_host" -p "$db_port" -U "$db_user" -d "$db_name" -t -A -c "SELECT 1" >/dev/null 2>&1; then
        # A3 修：pg_dump|gzip 管道中 pg_dump 失败 gzip 仍 0 收尾——"备份完成"可能是假的；
        # 查 PIPESTATUS[0]，失败清半成品并中止（备份先行=承诺不是装饰）
        PGPASSWORD="$old_pass" pg_dump -h "$db_host" -p "$db_port" -U "$db_user" "$db_name" 2>/dev/null | gzip > "$BACKEND_DIR/cache/pre-rotate-latest.sql.gz"
        if [ "${PIPESTATUS[0]}" != "0" ]; then
            rm -f "$BACKEND_DIR/cache/pre-rotate-latest.sql.gz"
            error "pg_dump 备份失败，轮换中止（env/库均未改动）"
        fi
        info "  备份完成：$BACKEND_DIR/cache/pre-rotate-latest.sql.gz ($(du -h "$BACKEND_DIR/cache/pre-rotate-latest.sql.gz" | cut -f1))"
    else
        warn "  旧口令连不上（可能已轮换过/env 与库不一致），跳过备份继续"
    fi

    info "② ALTER USER $db_user（PG 侧改密，数据/权限零变化）"
    # 注：本机 pg_hba local 已改 md5，sudo -u postgres 裸 socket 要密码——
    # 用旧口令自连执行 ALTER（rotate 前提=env 旧口令可用，此路必通）
    # C1 修：ALTER 经 stdin（heredoc）——新旧口令不再进 ps 进程命令行
    _npq="${new_pass//\'/\'\'}"   # C1b：hex 无引号仍统一转义；口令不出 argv
    if ! PGPASSWORD="$old_pass" psql -h "$db_host" -p "$db_port" -U "$db_user" -d "$db_name" >/dev/null 2>&1 <<SQL
ALTER USER "$db_user" WITH PASSWORD '${_npq}';
SQL
    then
        error "ALTER 失败（旧口令连不上？）。手工执行：psql -U $db_user 后 ALTER ...；env 尚未改动仍是旧值"
        exit 1
    fi

    info "③ 验证新口令可登录（先验证后落 env——C2）"
    if PGPASSWORD="$new_pass" psql -h "$db_host" -p "$db_port" -U "$db_user" -d "$db_name" -t -A -c "SELECT 1" 2>/dev/null | grep -q 1; then
        info "  新口令验证 OK"
    else
        error "  新口令验证失败！回滚：PGPASSWORD=\"$old_pass\" psql -h $db_host -U $db_user 执行 ALTER USER \"$db_user\" WITH PASSWORD '<旧值>'；env 未写入，重启即恢复旧链"
        exit 1
    fi

    info "④ 写入 .env.compile"
    env_set_quoted "$ENV_FILE" DB_PASSWORD "$new_pass"   # A1：写测同口径
    chmod 600 "$ENV_FILE"
    info "  完成 —— 请 bash setup.sh restart 让后端带新凭据重启（数据零影响；仅重启窗 ~1min）"
}

do_reset_admin_password() {
    # 免密③（2026-10-27）：已部署环境忘 admin 密码的一键出路（此前只能手工 psql）
    # bcrypt(12) 与 init 建号同源；只改 hash——旧 token 不失效，重登即用新密码
    [ -f "$ENV_FILE" ] || { error "缺少 $ENV_FILE"; exit 1; }
    set -a; source "$ENV_FILE"; set +a
    read_db_params
    local target="${ADMIN_INIT_USERNAME:-admin}" pw1 pw2
    pw1="${ADMIN_NEW_PASSWORD:-}"
    if [ -z "$pw1" ]; then
        if [ -t 0 ]; then
            read -sp "为 $target 设置新密码: " pw1; echo
            read -sp "确认新密码: " pw2; echo
            [ "$pw1" = "$pw2" ] || error "两次输入不一致"
        else
            error "交互执行，或 ADMIN_NEW_PASSWORD='<密码>' bash setup.sh reset-admin-password"
        fi
    fi
    [ ${#pw1} -ge 8 ] || error "密码至少 8 位"
    export RESET_TARGET="$target"
    cd "$BACKEND_DIR"   # python 段 import app.*，缺 cd 必 ModuleNotFound（终验抓到）
    PGPASSWORD="${DB_PASSWORD:-}" RESET_PW="$pw1" "$(detect_pybin)" <<'PY'
import os
from app.models import engine
from sqlalchemy import text
import bcrypt
with engine.connect() as conn:
    h = bcrypt.hashpw(os.environ["RESET_PW"].encode(), bcrypt.gensalt(rounds=12)).decode()
    r = conn.execute(text("UPDATE users SET password_hash=:p WHERE username=:u"),
                     {"p": h, "u": os.environ["RESET_TARGET"]})
    conn.commit()
    if r.rowcount == 0:
        raise SystemExit("用户不存在: " + os.environ["RESET_TARGET"])
print("  ✅ 密码已重置（旧 token 不失效，重新登录生效）")
PY
}

do_init() {
    [ -f "$ENV_FILE" ] || error "缺少 $ENV_FILE，worker/数据库配置会缺"
    # C1 现网升级路径：老 .env.compile 没有 JWT_SECRET_KEY → 补生成一次（此后固定）
    # A1 修：旧版 echo >> 追加且写入不带引号——每次 init 追加一行+重生成=静默全员换
    # token；改走同口径 helper（有值即跳过，绝不重复追加）
    if ! env_has_value "$ENV_FILE" JWT_SECRET_KEY; then
        _jwt="$(gen_secret 32)"
        [ -n "$_jwt" ] || error "生成 JWT_SECRET_KEY 失败（需 openssl 或 /dev/urandom）"
        env_set_quoted "$ENV_FILE" JWT_SECRET_KEY "$_jwt"
        unset _jwt
        warn "已在 $ENV_FILE 补生成 JWT_SECRET_KEY（首次升级自动执行；全员旧 token 失效需重登）"
    fi
    # T-9 同款：DB 口令缺失补生成（仅写 env；库侧对齐用 rotate-db-password）
    ensure_db_password
    cd "$BACKEND_DIR"
    set -a; source "$ENV_FILE"; set +a

    # 密码策略（T2 + 免密② 2026-10-27）：只有"真要建 admin"时才问——旧版问在
    # 顶部，已部署环境每次 init/install 被白问一遍（答错还 fail 全流程）。
    # 先经 TCP+env 口令探测 users 计数：>0 跳过询问；==0/查不到才走 env→交互→fail-fast
    local admin_user="${ADMIN_INIT_USERNAME:-admin}"
    read_db_params
    local _ucount
    _ucount=$(PGPASSWORD="${DB_PASSWORD:-}" psql -h "$db_host" -p "$db_port" -U "$db_user" -d "$db_name" \
              -t -A -c "SELECT count(*) FROM users;" 2>/dev/null || echo "")
    if [ -n "$_ucount" ] && [ "$_ucount" != "0" ]; then
        info "  管理员账号已存在（users=${_ucount}），本次无需设置密码"
    elif [ -z "${ADMIN_INIT_PASSWORD:-}" ]; then
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
        PYBIN="$(detect_pybin)"   # B-5：与 install/init 同一探测
        info "后端解释器: $PYBIN ($($PYBIN --version 2>&1))"
        # 按模式决定是否加 --reload
        if [ "$mode" = "dev" ]; then
            # B-2（2026-10-27）：setsid 使守护自立进程组——nohup 不脱组，无 job control
            # 环境（cron/远程 sh）下服务继承调用终端 pgid，kill_port 的 -pgid 兜底连坐
            setsid nohup "$PYBIN" -m uvicorn app.main:app \
                --host 0.0.0.0 --port "$BACKEND_PORT" --reload \
                --reload-dir app \
                >> "$LOG_DIR/backend.log" 2>&1 9>&- &   # C5修：fd9 不随 exec 继承给守护
        else
            setsid nohup "$PYBIN" -m uvicorn app.main:app \
                --host 0.0.0.0 --port "$BACKEND_PORT" \
                --proxy-headers --forwarded-allow-ips='127.0.0.1' \
                >> "$LOG_DIR/backend.log" 2>&1 9>&- &   # B-2 setsid   # C5修：同上
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
        cd "$FRONTEND_DIR"
        # B1 修（2026-10-27）：prod 模式不再把 vite dev server 当生产服务——dev 常驻
        # 有 esbuild 子服务死亡→页面随机崩的既往事故（第二套部署实测）；改
        # `vite preview` 静态服务（复用已构建 dist，代理走 vite.config preview.proxy）。
        # dist 缺失则先 build（npm run build 失败带日志退出，不静默）
        if [ "$mode" = "dev" ]; then
            # dev 与 nginx 同抢 3000：开发模式先停 nginx（restart prod 时自动恢复）
            if systemctl is-active nginx >/dev/null 2>&1; then
                info "dev 模式：停 nginx 让出 $FRONTEND_PORT 端口"
                sudo systemctl stop nginx
            fi
            info "启动前端（dev server：HMR，仅开发用）..."
            setsid nohup npm run dev -- --host 0.0.0.0 \
                >> "$LOG_DIR/frontend.log" 2>&1 9>&- &   # B-2 setsid   # C5修：fd9 不继承
        else
            if [ ! -f "dist/index.html" ]; then
                info "dist 缺失，先构建（npm run build）..."
                if ! npm run build > "$LOG_DIR/frontend-build.log" 2>&1; then
                    tail -20 "$LOG_DIR/frontend-build.log"
                    error "前端构建失败（详见 $LOG_DIR/frontend-build.log）"
                fi
            fi
            # 新鲜度提示：源码比 dist 新只提醒不阻断（发布时机由人决定）
            if [ -n "$(find src index.html vite.config.ts -newer dist/index.html 2>/dev/null | head -1)" ]; then
                warn "dist 比 src/index.html/vite.config 旧——展示的是旧版界面；更新请: cd frontend && npx vite build"
            fi
            if [ -f "/etc/nginx/compile-platform-servers.conf" ] && command -v nginx >/dev/null 2>&1; then
                # nginx 生产托管：build 检查在上（dist 可能刚重建）→ rsync 发布 → reload
                info "启动前端（prod：nginx 静态托管 + /api /ws 反代）..."
                sudo mkdir -p /srv/compile-platform
                sudo rsync -a --delete "$FRONTEND_DIR/dist/" /srv/compile-platform/dist/
                if sudo nginx -t >/dev/null 2>&1; then
                    sudo systemctl restart nginx
                else
                    sudo nginx -t 2>&1 | tail -2
                    error "nginx -t 失败，未启动前端（/etc/nginx/compile-platform.conf 或主 conf 需修正；备份 nginx.conf.compile-platform.bak）"
                fi
            else
                info "启动前端（prod：vite preview 静态服务——nginx 未配置，回退）..."
                sudo mkdir -p /srv/compile-platform && sudo rsync -a --delete "$FRONTEND_DIR/dist/" /srv/compile-platform/dist/ 2>/dev/null || true
                setsid nohup npm run preview -- --host 0.0.0.0 \
                    >> "$LOG_DIR/frontend.log" 2>&1 9>&- &   # B-2 setsid   # C5修：fd9 不继承
            fi
        fi
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
        main_ip=$(_local_ipv4s | head -1)   # B-7：复用统一探测，消重复实现
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
    # B-8d 修：口令链路显式化——config/env 都取不到 DB_PASSWORD 时直接报错指路，
    # 不再让后面 psql 认证失败被 2>/dev/null + || warn 吞成"无声退出"
    if [ -z "${db_pass:-}" ] && [ -f "$ENV_FILE" ]; then
        db_pass="$(grep -oP '^export DB_PASSWORD="\K[^"]+' "$ENV_FILE" 2>/dev/null)"
    fi
    [ -n "${db_pass:-}" ] || error "拿不到 DB_PASSWORD（config/env 均无）——uninstall 中止；影子测试请显式 DB_PASS=xxx 传入"
    # 统一走 -h（TCP）+ 密码：pg_hba 把 local 也设成了 md5，sudo -u postgres 裸 socket 会 fe_sendauth
    local PSQLU="psql -h $db_host -p $db_port -U $db_user"
    export PGPASSWORD="***"

    # 路径（UNINSTALL_* 前缀覆盖，影子测试指向 /tmp 假目录；
    # 注意不能用 LOG_DIR 裸名——setup.sh 顶部无条件赋值会覆盖外部 env，撞车教训）
    local skills_dir="${SKILLS_DIR:-$PROJECT_DIR/skills}"
    local dist_dir="$FRONTEND_DIR/dist"
    local nm_dir="$FRONTEND_DIR/node_modules"
    # B-8b 修：artifacts 根此前只认 $PROJECT_DIR/artifacts——若部署用 ARTIFACTS_DIR/
    # config.yaml 指到别处就漏删；与后端 config.artifacts_dir() 同口径解析
    local art_dir="${UNINSTALL_ART_DIR:-}"
    if [ -z "$art_dir" ]; then
        art_dir=$(awk '/compile:/{f=1;next} f&&/artifact_dir:/{print $2;exit}' "$CONFIG_FILE" 2>/dev/null)
        art_dir="${ARTIFACTS_DIR:-${art_dir:-./artifacts}}"
        case "$art_dir" in /*) : ;; *) art_dir="$PROJECT_DIR/${art_dir#./}" ;; esac
    fi
    local log_dir="${UNINSTALL_LOG_DIR:-$PROJECT_DIR/logs}"
    # B-8a 修（2026-10-27）：cache 实际在 backend/cache/（sqlite/备份都落那），
    # 旧版指 $PROJECT_DIR/cache 是不存在的顶层目录=删了个寂寞
    if [ -d "$BACKEND_DIR/cache" ]; then
        local cache_dir="${UNINSTALL_CACHE_DIR:-$BACKEND_DIR/cache}"
    else
        local cache_dir="${UNINSTALL_CACHE_DIR:-$PROJECT_DIR/cache}"
    fi

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
        # B-8c 修（2026-10-27）：工单会话容器的 label 实为 compile.ticket（引擎实况
        # ticket_agent.py:373）——旧过滤键 label=ticket_id 永不匹配=工单容器全漏网
        ids=$(echo "$ids" $(docker ps -aq --filter 'label=compile.ticket' 2>/dev/null || true) | tr ' ' '\n' | grep -v '^$' | sort -u || true)
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

# A2 修（2026-10-27）：stop/restart 活跃作业闸门——kill_port 30s 优雅期后 SIGKILL，
# 进行中的编译任务（合法 1~3 小时）与工单 Agent 轮次必被腰斩；此前靠"重启前手查
# DB"的操作纪律，现内建为脚本行为。查询失败（库未 init/连不上）按无活跃放行——
# 反正后端也起不来，拦了没意义。强停逃生口：FORCE_RESTART=1（非交互必备）。
active_work_gate() {
    [ -f "$ENV_FILE" ] || return 0
    local db_host db_port db_user db_name out
    set -a; source "$ENV_FILE" 2>/dev/null; set +a
    read_db_params
    out=$(PGPASSWORD="${DB_PASSWORD:-}" psql -h "$db_host" -p "$db_port" -U "$db_user" -d "$db_name" -t -A -F, \
        -c "SELECT (SELECT count(*) FROM compile_tasks WHERE status IN ('compiling','submitted')), (SELECT count(*) FROM tickets WHERE status='processing');" 2>/dev/null) || return 0
    [[ "$out" =~ ^[0-9]+,[0-9]+$ ]] || return 0
    local ntask="${out%%,*}" nticket="${out##*,}"
    [ "$((ntask + nticket))" -eq 0 ] && return 0
    warn "检测到活跃作业：编译任务 $ntask 个（compiling/submitted）、工单 Agent 轮次 $nticket 个（processing）"
    warn "停止会腰斩它们（编译靠 reconcile 自愈、Agent 轮次将中断且 partial 快照留存）"
    if [ "${FORCE_RESTART:-0}" = "1" ]; then
        warn "FORCE_RESTART=1 → 跳过确认继续"
        return 0
    fi
    if [ -t 0 ]; then
        read -p "仍要停止? 输入 yes 继续: " ans
        [ "$ans" = "yes" ] || error "已取消（活跃作业保护；确要强停用 FORCE_RESTART=1 bash setup.sh ${CMD:-restart}）"
    else
        error "非交互环境拒绝强停活跃作业。确知安全则：FORCE_RESTART=1 bash setup.sh ${CMD:-restart}"
    fi
}

# ========== 停止 ==========
do_stop() {
    active_work_gate   # A2：先闸后杀
    if systemctl is-active nginx >/dev/null 2>&1 && [ -f /etc/nginx/compile-platform-servers.conf ]; then
        info "停止 前端（nginx systemctl）"
        sudo systemctl stop nginx
    else
        kill_port "$FRONTEND_PORT" "前端"
    fi
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
    # B-12 修：头注释声称"探 PG"而旧版未探——补 pg_isready（名实相符）
    if command -v pg_isready >/dev/null 2>&1; then
        if pg_isready -q 2>/dev/null; then echo "  PostgreSQL: 运行中"; else echo "  PostgreSQL: 不可达"; fi
    fi
    echo ""
    echo "=== 监听端口 ==="
    ss -tlnp 2>/dev/null | grep -E ":($BACKEND_PORT|$FRONTEND_PORT|5432)\b" | awk '{printf "  %-30s %s\n", $4, $6}'
}

# ========== 主入口 ==========
# C5 修：变更类命令加文件锁（flock fd=9，脚本退出自动释放）——此前两人同跑
# install/rotate 会互踩（env 双写、DB 双 ALTER、restart 交叉起停）；status 只读不锁
CMD="${1:-start}"
case "$CMD" in
    status) : ;;
    install|init|start|stop|restart|uninstall|dev|prod|rotate-db-password)
        exec 9>"$PID_DIR/setup.lock"
        flock -n 9 || error "另一个 setup.sh 正在运行（锁: $PID_DIR/setup.lock）——若确认是残留锁可删后重试"
        ;;
esac
case "${1:-start}" in
    install) do_install ;;
    init)    do_init ;;
    start)   do_start prod ;;    # 默认 prod（生产优先）
    stop)    do_stop ;;
    restart) do_restart "$2" ;;  # 透传模式参数；不传 = 保持 COMPILE_MODE
    status)  do_status ;;
    uninstall) do_uninstall "${@:2}" ;;   # 可选 --purge / --yes
    rotate-db-password) do_rotate_db_password ;;
    reset-admin-password) do_reset_admin_password ;;  # T-9：DB 口令一键轮换（备份→ALTER→env→验证）
    # B-4（2026-10-27）：旧版直调 do_start——端口被占只 warn 跳过，"切模式"静默
    # no-op。语义即切换 → 走 restart（顺带受 A2 活跃作业闸门保护）
    dev)     do_restart dev ;;
    prod)    do_restart prod ;;
    *)
        echo "用法: $0 {install|init|start [dev|prod]|stop|restart [dev|prod]|status|uninstall [--purge] [--yes]|rotate-db-password|dev|prod}"
        echo
        echo "  start         生产模式启动（默认；无 --reload）"
        echo "  start dev     开发模式启动（uvicorn --reload，改 .py 自动重启）"
        echo "  dev / prod    切换模式重启（端口占用时不再静默跳过；受活跃作业闸门保护）"
        echo "  restart       重启（保持当前模式，可用 restart dev/prod 切换）"
        echo "  restart dev   切到开发模式重启"
        echo "  restart prod  切到生产模式重启"
        echo "  uninstall     数据级重置（清容器/DB/artifacts，先备份；镜像/skills 保留）"
        echo "  uninstall --purge  连自制镜像/skills/配置全清；--yes 跳过总确认（镜像仍单独确认）"
        exit 1
        ;;
esac
