"""编译引擎 - 实现 6 阶段流水线（阶段 1-4/6 硬编码，阶段 5 DeepAgent）

入口：run_task(task_id: str, on_log: Callable | None = None)

数据流：
  worker dequeue task_id
    → run_task 读 CompileTask (status=submitted → compiling)
    → stage1-6 跑完整流程
    → 每步写 log_output / status 到 DB（通过回调）
    → final status success / failed

依赖：
  app.services.docker_backend   容器协议实现
  app.services.skill_loader     skill 自动发现
  app.models.CompileTask        ORM
"""
from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from typing_extensions import TypedDict

from sqlalchemy.orm import Session

from app.config import get_cfg, get_settings
from app.models import CompileTask, TaskStatus, SessionLocal
from app.services.docker_backend import DockerBackend, get_docker_client
from app.services.skill_loader import get_skills_paths, SKILLS_DIR_HOST, SKILLS_DIR_CONTAINER


# ==================== AI / Engine 配置 ====================
# C-3（2026-10-26）：三件套单源归 config.py（此前本文件与 experience_generator 各
# 一份拷贝、带同一个 agnes 死默认，已删）；消费点函数名保持本地别名零改动
from app.config import ai_model as _ai_model, ai_api_key as _ai_api_key, ai_base_url as _ai_base_url

def _max_retries() -> int:
    return int(get_cfg("compile.max_retries", 4, env_alias="MAX_RETRIES"))

# Q2 步3：解析逻辑归一 config.artifacts_dir()（本函数保留为别名，39 处引用零改动）
from app.config import artifacts_dir as _artifacts_dir


# ==================== 状态定义 ====================
class TaskState(TypedDict, total=False):
    task_id: str
    status: str
    software_name: str
    source_type: str
    git_url: str
    git_branch: str
    commit_hash: str
    system_id: str
    system_name: str
    # stage2 从 DB TargetSystem.base_image 读取的完整镜像名（含 tag）
    docker_image: str
    container_id: str
    work_dir: str
    container_work_dir: str
    host_log_path: str  # 宿主机 build.log 路径（兜底双写用）
    _matched_skills: list  # Skill 命中收口：agent 实际读取过的 SKILL.md 名（有序去重）
    build_logs: list
    current_attempt: int
    last_error: str
    artifacts: list
    output_dir: str
    log_output: str


# ==================== 日志回调 ====================
LogCallback = Callable[[str, str], None]  # (level, message)

# 周期性持久化阈值：每写入 N 行日志就 commit 一次到 DB
# 教训 9/30：run_task 全程不 commit，外部"标 failed"worker 感知不到
_PERIODIC_COMMIT_EVERY = 20


def _log(state: TaskState, level: str, msg: str, on_log: Optional[LogCallback] = None):
    # 源头净化 NUL(\x00)：PG text 字段禁止 0x00。教训（T-20261021163147-223BFD）：
    # agent 跑"看日志尾部"类命令把二进制零带进流 → persist 整块失败 → 成功任务被看门狗误标"worker 崩溃"。
    # 唯一日志入口在此拦，log_output/文件/WS 三路同时干净，日志可读性无损。
    msg = str(msg).replace("\x00", "")
    ts = datetime.now().strftime("%m-%d %H:%M:%S")
    line = f"[{ts}] [{level.upper()}] {msg}"
    print(line)
    # 1) 内存累积（周期 commit 到 DB）
    state.setdefault("log_output", "")
    state["log_output"] += line + "\n"
    # 2) 双写到文件（兜底，不依赖 DB；前端的 onMounted 恢复也将从这里读）
    host_log = state.get("host_log_path")
    if host_log:
        try:
            with open(host_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            # 文件写失败不影响主流程（DB 还在）
            pass
    # 3) 周期 commit：每 N 行把当前 state 写到 DB（如果有 task 引用）
    state["_log_count"] = state.get("_log_count", 0) + 1
    if state["_log_count"] % _PERIODIC_COMMIT_EVERY == 0 and "_task" in state:
        _periodic_persist(state)
    # 4) 实时推 WS
    if on_log:
        try:
            on_log(level, msg)
        except Exception:
            pass


def _periodic_persist(state: TaskState):
    """周期性把 state 写到 DB；顺便检查任务是否已被外部标 failed（如果标了就在 state 里设旗，stage5 会退出）"""
    task = state.get("_task")
    if task is None:
        return
    try:
        # 重查 DB 当前 status（开新 session 避免和 stage 共用）
        from app.models import SessionLocal
        fresh = SessionLocal()
        try:
            cur = fresh.query(CompileTask).filter(CompileTask.task_id == state.get("task_id")).first()
            # 任何终态（包括 cancelled）都让 stage5 自然退出
            terminal_statuses = (
                TaskStatus.FAILED.value,
                TaskStatus.SUCCESS.value,
                "cancelled",  # 用户主动取消
            )
            if cur and cur.status in terminal_statuses:
                # 外部已标终态：让 stage5 自然退出
                state["_external_terminal"] = cur.status
                state["_external_error"] = cur.error_message or ""
        finally:
            fresh.close()
        # 写 log（统一入口：净化+失败静默不连坐，2026-10-21 Step 0）
        from app.models import SessionLocal as _SL
        s = _SL()
        try:
            t = s.query(CompileTask).filter(CompileTask.task_id == state.get("task_id")).first()
            if t:
                from app.services.task_writer import write_log_cache
                write_log_cache(s, t, state.get("log_output", ""))
        finally:
            s.close()
    except Exception:
        # 周期 commit 失败不能影响主流程
        pass


# ==================== 阶段 1 ====================
def stage1_init(task: CompileTask, state: TaskState, on_log: Optional[LogCallback]) -> TaskState:
    state.update({
        "task_id": task.task_id,
        "software_name": task.software_name,
        "source_type": task.source_type or "git",
        "git_url": task.git_url or "",
        # upload 模式：source_path 存宿主机源码包路径，stage4a 读这个解压
        # git 模式：source_path 没用
        "source_path": task.source_path or "",
        # git_branch 留空时由 stage4a 走"不传 --branch"路径，让 git 拉远端默认分支
        "git_branch": task.git_branch or "",
        "commit_hash": "",
        "system_id": task.system_id,
        "container_work_dir": "/work",
        "output_dir": "/work/output",
        "build_logs": [],
        "current_attempt": 0,
        "last_error": "",
        "artifacts": [],
        "log_output": "",
        "status": TaskStatus.COMPILING.value,
        "container_id": "",
    })
    # DEBUG: 排查 source_path 何时为空
    print(f"[DEBUG stage1] task_id={task.task_id} source_type={state['source_type']!r} "
          f"task.source_path={task.source_path!r} state.source_path={state['source_path']!r} "
          f"task.id={task.id}", flush=True)
    # 文件兜底日志：目录 + build.log 从阶段1就建立，保证阶段1/2 的行也进文件
    # （历史 bug：host_log_path 到阶段3才赋值，build.log 缺阶段1/2）
    base_dir = _artifacts_dir() / task.task_id
    base_dir.mkdir(parents=True, exist_ok=True)
    state["work_dir"] = str(base_dir)
    state["host_log_path"] = str(base_dir / "build.log")
    _log(state, "info", f"[阶段1] 任务初始化: {task.task_id} / {task.software_name}", on_log)
    return state


# ==================== 阶段 2 ====================
# 之前是 ENV_BY_NAME 硬编码字典映射 system.name → env_id。
# 2026-10-13 改造：从 DB 读 TargetSystem.base_image 直接用，不再硬编码。
# base_image 字段已含完整 image:tag 字符串（admin 端可改），worker 拿啥用啥。
# env_id 字段保留但含义改为"构建系统标识"，用于 docker exec 时的环境变量注入。
# 系统为空时 stage2 显式报错，admin 知道要去 /admin/systems 配。
DEFAULT_BASE_IMAGE = "loongnix-build:23.2"  # DB 字段为空时的兜底（实际不会走这里）


def stage2_match_env(state: TaskState, on_log: Optional[LogCallback]) -> TaskState:
    """从 DB 读 TargetSystem，按 name 匹配 base_image + 给 env_id 兜底值"""
    from app.models import SessionLocal, TargetSystem
    db = SessionLocal()
    try:
        sys_rec = db.query(TargetSystem).filter(
            TargetSystem.system_id == state.get("system_id")
        ).first()
        if not sys_rec:
            state["status"] = TaskStatus.FAILED.value
            state["last_error"] = (
                f"目标系统 ID {state.get('system_id')} 不存在。"
                f"管理员需在 /admin/systems 添加。"
            )
            _log(state, "error", state["last_error"], on_log)
            return state
        state["system_name"] = sys_rec.name
        # base_image 是 admin 配置的完整镜像名（含 tag）
        state["docker_image"] = sys_rec.base_image or DEFAULT_BASE_IMAGE
        _log(state, "info", f"[阶段2] 环境匹配: {sys_rec.name} (image={state['docker_image']})", on_log)
    finally:
        db.close()
    return state


# ==================== 阶段 3 ====================
def stage3_create_container(state: TaskState, on_log: Optional[LogCallback] = None,
                             existing_container_id: Optional[str] = None) -> TaskState:
    """阶段 3：建容器 / 复用现有容器

    - existing_container_id 传了 + 容器还活着 → 复用（docker start 重新启动）
    - 否则：全新创建
    """
    cli = get_docker_client()
    if cli is None:
        state["status"] = TaskStatus.FAILED.value
        state["last_error"] = "Docker 客户端未连接"
        _log(state, "error", state["last_error"], on_log)
        return state

    base_dir = _artifacts_dir() / state["task_id"]
    base_dir.mkdir(parents=True, exist_ok=True)
    container_work = base_dir / "container_work"
    container_work.mkdir(parents=True, exist_ok=True)
    state["work_dir"] = str(base_dir)
    # 文件兜底日志：每个 task 一份 build.log，_log() 双写（DB 仍写）
    state["host_log_path"] = str(base_dir / "build.log")

    # 复用现有容器：避免 5-10s 的 docker run
    if existing_container_id:
        try:
            existing = cli.containers.get(existing_container_id)
            if existing.status == "running":
                state["container_id"] = existing.id
                _log(state, "info", f"[阶段3] 复用现有容器 {existing.id[:12]}（状态 running）", on_log)
                return state
            # 容器存在但停了，start 它
            existing.start()
            # 重新绑定 volume（容器 stop 时还在）
            state["container_id"] = existing.id
            _log(state, "info", f"[阶段3] 重启容器 {existing.id[:12]}（状态 {existing.status}）", on_log)
            return state
        except Exception as e:
            # 容器不存在/不可用，fallback 全新创建
            _log(state, "warn", f"[阶段3] 复用容器失败 ({e})，将创建新容器", on_log)

    try:
        # 镜像名从 stage2 读 DB 拿（admin 在 /admin/systems 配的）
        # state["docker_image"] 一定有值（DB 字段非空 + DEFAULT 兜底）
        image = state.get("docker_image") or "loongnix-build:23.2"
        _log(state, "info", f"[阶段3] 使用镜像: {image}", on_log)
        volumes = {
            str(container_work): {"bind": state["container_work_dir"], "mode": "rw"},
        }
        if SKILLS_DIR_HOST.exists():
            volumes[str(SKILLS_DIR_HOST)] = {"bind": SKILLS_DIR_CONTAINER, "mode": "ro"}

        container = cli.containers.run(
            image,
            command="sleep infinity",
            detach=True,
            working_dir=state["container_work_dir"],
            volumes=volumes,
            labels={**state.get("extra_labels", {}), "task_id": state["task_id"], "phase": "infrastructure"},
            # 2026-10-19 改：改用 host 网络模式
            # 原因：bridge 容器在 firewalld + Docker daemon iptables 规则异常下外网不通
            # host 模式共享 host 网络栈，无 NAT，localhost = host localhost
            # 副作用：容器能看 host 所有端口（安全权衡）
            network_mode="host",
        )
        state["container_id"] = container.id
        # 落库：cancel API 用 container_id 调 docker stop
        # 立即 commit（不依赖周期 commit 兜底）
        task = state.get("_task")
        if task is not None:
            task.container_id = container.id
            try:
                from app.models import SessionLocal as _SL
                s = _SL()
                t = s.query(CompileTask).filter(CompileTask.task_id == state["task_id"]).first()
                if t:
                    t.container_id = container.id
                    s.commit()
                s.close()
            except Exception:
                pass  # 落库失败不影响主流程（cancel 时无 container_id，仅不能杀容器）
        _log(state, "info", f"[阶段3] 容器创建: {container.id[:12]}", on_log)

        hc = container.exec_run(["echo", "hello"])
        if hc.exit_code != 0:
            raise RuntimeError("容器健康检查失败")
        _log(state, "info", f"[阶段3] 容器健康检查通过", on_log)
    except Exception as e:
        state["status"] = TaskStatus.FAILED.value
        state["last_error"] = f"环境创建失败: {e}"
        _log(state, "error", state["last_error"], on_log)
    return state


# ==================== 阶段 4a ====================
def stage4a_fetch_source_on_host(state: TaskState, on_log: Optional[LogCallback]) -> TaskState:
    """宿主机侧：把源码下载/解压到 container_work/src/

    为什么要宿主机做：
      1. 容器内网络可能受限（防火墙/DNS/代理），宿主机通常可达公网
      2. 下载用 subprocess + timeout，失败可立即报错退出，不会出现容器内 git
         zombie 后容器 exec 永远不返回的卡死现象
      3. 流式输出逐行进 _log，前端 WS 能实时看到 clone 进度

    两条路径：
      - source_type='git'    → git clone（不变）
      - source_type='upload' → 解压 state['git_url'... 不，是 state['source_path'] 的 tar.gz/zip
    """
    work_dir = Path(state["work_dir"])
    src_dir = work_dir / "container_work" / "src"

    if state["source_type"] == "git" and state.get("git_url"):
        # 3f 纵深（2026-10-23）：库内存量脏行/retry 复制路径不过 schema，执行前再拦一道
        import re as _re
        if not _re.match(r"^(https?|git)://", state["git_url"] or ""):
            raise RuntimeError(f"git_url 协议不允许（仅 http(s)/git）：{state['git_url']!r}")
        # git clone（原有逻辑，原样保留）
        # 复用已有 clone（节省时间）
        if src_dir.exists() and (src_dir / ".git").exists():
            _log(state, "info", f"[阶段4a] 源码已存在，复用: {src_dir}", on_log)
        else:
            if src_dir.exists():
                shutil.rmtree(src_dir, ignore_errors=True)
            src_dir.parent.mkdir(parents=True, exist_ok=True)

            branch = state.get("git_branch", "").strip()
            url = state["git_url"]
            # 留空时不传 --branch → git clone 拉远端默认分支（main / master 等）
            if branch:
                cmd = ["git", "clone", "--depth", "1", "--branch", branch, url, str(src_dir)]
            else:
                cmd = ["git", "clone", "--depth", "1", url, str(src_dir)]

            _log(state, "info", f"[阶段4a] 宿主机 git clone: {url} (branch={branch or 'default'})", on_log)
            try:
                # 3f 加固：隔离宿主全局/系统 gitconfig（url.insteadOf 重定向面）
                import os as _os2
                _git_env = {**_os2.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
                proc = subprocess.Popen(
                    cmd,
                    env=_git_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        _log(state, "info", f"[阶段4a]   {line}", on_log)
                ret = proc.wait(timeout=600)  # 10 分钟硬上限
                if ret != 0:
                    raise RuntimeError(f"git clone exit_code={ret}")
            except subprocess.TimeoutExpired:
                try: proc.kill()
                except Exception: pass
                raise RuntimeError("git clone 超时（>10 分钟）")
            except FileNotFoundError:
                raise RuntimeError("宿主机未安装 git")

        # 读 commit hash
        try:
            out = subprocess.check_output(
                ["git", "-C", str(src_dir), "rev-parse", "HEAD"],
                text=True, timeout=10,
            ).strip()
            state["commit_hash"] = out
            _log(state, "info", f"[阶段4a] 完成: commit={out[:8]}", on_log)
        except Exception as e:
            _log(state, "warn", f"[阶段4a] 读 commit hash 失败: {e}", on_log)

        state["host_src_dir"] = str(src_dir)
        return state

    if state["source_type"] == "upload":
        # 解压用户上传的 .tar.gz / .zip
        archive = state.get("source_path") or state.get("git_url")  # 兼容：有些路由可能写到 git_url
        if not archive:
            raise RuntimeError("source_type=upload 但 source_path 为空")
        archive_path = Path(archive)
        if not archive_path.exists():
            raise RuntimeError(f"上传的源码包不存在：{archive_path}")
        # 3a 纵深：包体必须落在 artifacts 区内（同 retry 脏数据兜底）
        import os as _os
        _root = str(_artifacts_dir().resolve())
        _rp = _os.path.realpath(str(archive_path))
        try:
            _inside = _os.path.commonpath([_root, _rp]) == _root
        except ValueError:
            _inside = False
        if not _inside:
            raise RuntimeError(f"source_path 越界（仅允许 artifacts 区内）：{_rp}")

        # 复用已有解压（节省时间）
        if src_dir.exists() and any(src_dir.iterdir()):
            _log(state, "info", f"[阶段4a] 源码已存在，复用: {src_dir}", on_log)
        else:
            if src_dir.exists():
                shutil.rmtree(src_dir, ignore_errors=True)
            src_dir.parent.mkdir(parents=True, exist_ok=True)
            _extract_archive_safely(archive_path, src_dir, on_log, state)

        state["host_src_dir"] = str(src_dir)
        _log(state, "info", f"[阶段4a] 解压完成: {src_dir}", on_log)
        return state

    # 未知 source_type
    raise RuntimeError(f"不支持的 source_type: {state['source_type']}")


def _extract_archive_safely(
    archive_path: Path,
    dst_dir: Path,
    on_log: Optional[LogCallback],
    state: TaskState,
) -> None:
    """解压 .tar.gz / .zip / .tgz 到 dst_dir —— 委托 services/safe_extract（批3 3b：
    commonpath 边界 + 拒 symlink/hardlink/设备成员 + 数量/体积双上限）"""
    from app.services import safe_extract
    name = archive_path.name.lower()
    prog = (lambda n: _log(state, "info", f"[阶段4a] 解压中... {n} 文件", on_log)) if on_log else None
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        safe_extract.safe_extract_tar(archive_path, dst_dir, on_progress=prog)
    elif name.endswith(".zip"):
        safe_extract.safe_extract_zip(archive_path, dst_dir, on_progress=prog)
    else:
        raise RuntimeError(f"不支持的源码包格式：{name}（仅 .tar.gz / .zip / .tgz）")


# ==================== 阶段 4 ====================
def stage4_import_source(state: TaskState, on_log: Optional[LogCallback]) -> TaskState:
    """容器内：校验 stage4a 在宿主机下载好的源码已挂载进容器"""
    cli = get_docker_client()
    container = cli.containers.get(state["container_id"])
    src_dir = f"{state['container_work_dir']}/src"

    try:
        if state["source_type"] == "git" and state["git_url"]:
            chk = container.exec_run(["ls", "-la", src_dir])
            if chk.exit_code != 0:
                raise RuntimeError(f"源码目录不存在: {src_dir} (stage4a 失败?)")
            raw = chk.output
            listing = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
            if ".git" not in listing:
                raise RuntimeError("源码目录缺少 .git，stage4a 未正确下载")
            _log(state, "info", f"[阶段4] 容器内源码已就绪 (commit={state.get('commit_hash', '?')[:8]})", on_log)
        elif state["source_type"] == "upload":
            chk = container.exec_run(["ls", "-la", src_dir])
            if chk.exit_code != 0:
                raise RuntimeError(f"源码目录不存在: {src_dir} (stage4a 解压失败?)")
            _log(state, "info", f"[阶段4] 容器内 upload 源码已就绪: {src_dir}", on_log)
        else:
            raise RuntimeError(f"未知 source_type: {state['source_type']}")
    except Exception as e:
        state["status"] = TaskStatus.FAILED.value
        state["last_error"] = f"源码导入失败: {e}"
        _log(state, "error", state["last_error"], on_log)
    return state


# ==================== 阶段 5 (DeepAgent) ====================
def stage5_run_agent(state: TaskState, on_log: Optional[LogCallback]) -> TaskState:
    cli = get_docker_client()
    container = cli.containers.get(state["container_id"])
    src_dir = f"{state['container_work_dir']}/src"
    output_dir = state["output_dir"]

    container.exec_run(["mkdir", "-p", output_dir])
    backend = DockerBackend(state["container_id"], src_dir)

    # ===== system_prompt（精简 meta，不含地址/命令/流程）=====
    # CPU 限速：服务器 32 物理核 → MAX_WORKERS 个 worker 每个限 32/MAX_WORKERS 核
    # 避免 N 个 worker 同时 make -j32 互相抢
    try:
        _max_workers = int(get_cfg("worker.max_workers", 8, env_alias="MAX_WORKERS"))
    except ValueError:
        _max_workers = 8
    _jobs_per_worker = max(2, 32 // max(1, _max_workers))

    system_prompt = f"""你是一个 LoongArch 编译工程师，工作在龙芯容器环境里。

## 任务
- 软件: {state['software_name']}
- 源码: {src_dir}
- 输出: {output_dir}
- 系统: {state.get('system_id', '')}

## 你能调用的工具
- ls/read_file/write_file/edit_file/grep/glob — 文件操作
- execute — 在容器里跑命令
- task — 拆子任务

## 知识来源（重要）
**不要凭记忆猜工具链地址、编译命令、错误模式**。
所有具体知识从 Skill 读：系统提示下方的 Skill 清单列出了当前可用 Skill，每条含触发场景描述。
命中后用 read_file 读对应路径的 SKILL.md 全文执行。
**编译命令（-j 参数、并行度、工具链版本等）以 Skill 攻略为准，不要在本提示词里找。**

## 作业流程
1. 看 Skills 清单，找最匹配的（专用 Skill → generic 工具链 → generic 错误经验）
2. 命中后 read_file 读全文，按攻略执行
3. 编译失败再读通用错误类 Skill

## 产物输出（强制 — 系统约定）
**所有编译产物必须最终放到 `/work/output/`，这是 worker 收集产物的唯一路径**。
不论 Skill 攻略怎么写，编译完成后**必须**把产物复制过去，否则任务会被判定无产物。
- cmake: `cmake --install build --prefix /work/output` 或 `cp -a build/bin/* /work/output/`
- go build: `go build -o /work/output/<binary> .`
- cargo: `cargo install --path . --root /work/output` 或 `cp -a target/release/* /work/output/`
- make: `make install DESTDIR=/work/output` 或 `cp -a <产物> /work/output/`
- nodejs: `cp -a dist/* /work/output/` 或 `cp -a build/* /work/output/`
- 其他: 找到产物目录后 `mkdir -p /work/output && cp -a <产物>/* /work/output/`
## 边界
- ✅ /work 目录内任意改、装编译依赖
- ❌ /work 外操作、Docker 命令、重启容器
- ❌ 超过最大 {_max_retries()} 次重试
"""

    api_key = _ai_api_key()
    if not api_key:
        state["status"] = TaskStatus.FAILED.value
        state["last_error"] = "AI_API_KEY 未配置"
        _log(state, "error", state["last_error"], on_log)
        return state

    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(
        model=_ai_model(),
        api_key=api_key,
        base_url=_ai_base_url() or None,
        temperature=0.1,
    )

    # 每次启动 stage5 都重扫磁盘（approve/update/delete 后热生效，无需重启后端）
    _skills_paths = get_skills_paths()
    from deepagents import create_deep_agent
    # T9-V 收尾闸门（docs/stage5-verify-discipline-v2.md §七 801346 校准）：检测首次验证
    # 全绿（RESULT pass=N fail=0）后计收尾预算，超限 execute 拒答引导收尾
    # （wrapup_enforce=false 观测模式只记不拦）。write_file/read 永不拦，NOTES 永远写得动。
    _gate_on      = bool(get_cfg("stage5.wrapup_enabled", True))
    _gate_enforce = bool(get_cfg("stage5.wrapup_enforce", False))
    _gate_seconds = int(get_cfg("stage5.post_green_max_seconds", 900))
    _gate_execs   = int(get_cfg("stage5.post_green_max_execs", 15))
    _green_gate = None
    if _gate_on:
        try:
            from app.services.green_gate import GreenGateMiddleware
            _green_gate = GreenGateMiddleware(state, enforce=_gate_enforce,
                                              max_seconds=_gate_seconds, max_execs=_gate_execs,
                                              on_log=on_log)
        except Exception as _ge:
            _log(state, "warn", f"[阶段5] GreenGate 初始化失败（闸门关闭，主流程不受影响）: {_ge}", on_log)
    agent = create_deep_agent(
        model=llm,
        system_prompt=system_prompt,
        backend=backend,
        skills=_skills_paths,
        **({"middleware": [_green_gate]} if _green_gate else {}),
    )

    _log(state, "info", f"[阶段5] DeepAgent 启动, skills={len(_skills_paths)}", on_log)

    state["current_attempt"] = 1
    task_prompt = f"""开始编译 {state['software_name']}。

源码已克隆到 {src_dir}。

请执行完整的编译流程：先用 read_file 读 Skills 清单中匹配的 SKILL.md，再按攻略执行。
完成后输出编译结果摘要（中文，包含"编译成功"或"编译失败"）。

【验证口径】本平台是编译平台，验收只证明"装得上、导入得动、能最小调用"，三件套即可：
① 干净环境安装产物（pip install whl / npm i tgz / 二进制 ldd+file 检查，按产物类型）
② 导入/加载（python -c "import x" / node require / ./bin --version）
③ 一次最小真实调用出结果（一个 matmul / 一个函数 / 一次请求）
【禁止】跑上游全量测试套件（pytest 全量 / test_ops / gradcheck / JIT+compile 全家桶 / benchmark）——
那是 CI 的开发动作，动辄数小时且非验收所需。若攻略里出现"跑完整测试"，只挑上面三件套执行、
其余跳过并在 BUILD_NOTES 注明"全量测试非验收必需，已按轻量口径"。产物验证通过后立即归档收尾。
【验证方式铁律】所有验证命令必须非交互、能自然退出：
- 禁止前台直接运行等待输入的程序（交互式 shell / REPL / TUI / 前台服务器）
- 验证 PTY / 子进程 / 常驻服务类库：用 `timeout 30` 包裹，或"后台起进程 → 断言 → kill"两段式
- 单条验证命令预期 ≤5 分钟；超了就换非交互写法重试，不要干等（教训：node-pty 任务 pty.spawn bash 裸跑，永不退出挂 2.5h 拖到硬闸）
【验证要"薄"（规则⓪）】每项检查 = 一条自包含命令（timeout 包裹、预期 ≤10 秒），直接跑产物二进制。
凡需"搭环境才能测"的检查（chroot rootfs、服务树/监督生命周期、网络服务、特殊 pid1/权限路径）——
默认降级为 NOTES"建议深挖项"，不为其编写测试夹具。例外：服务管理器类产物允许在两三条命令内
起 svscan+svstat 冒烟监督链，禁止为此新建 chroot/设备节点/PATH 框架。
（教训 801346：三件套被做成 22 项检查+百行 chroot 夹具，光调夹具就烧 30 分钟。）

【PASS 即终局（硬约束）】
① 验证脚本必须输出一行机读结果：`RESULT pass=<N> fail=<M>`。首次 fail=0 = 验证通过，
   立即归档 + 写 BUILD_NOTES 收尾；之后禁止以任何理由重写验证脚本或重跑（包括怀疑测试
   "不够真"）——有疑义写进 NOTES"已知限制"节。（教训：两次全绿后推翻考卷重做，白烧 6-15min）
② 出现 FAIL 才允许修复重跑，且 ≤1 轮；第二轮仍 FAIL → 失败项如实记 NOTES，收尾不无限追。
③ 验证脚本自身报错（夹具/PATH/chroot/envdir 语义类）≠产品缺陷：同一夹具问题最多修 1 次，
   修不好把该检查项降级为"已知限制"，继续下一项。
④ 三件套之外的深度测试冲动：写进 NOTES"建议深挖项"（一行一条，带命令示例），本轮不执行。
⑤ 验证脚本幂等自隔离：临时目录带 $$ 后缀（run-$$），禁止 rm -rf 共享路径。
⑥ NOTES 验证节 = 三件套逐项结果 + 失败项原文 + 建议深挖项。写全≠测全：没测的写"未验证"即可。

【长命令纪律】预期 >5 分钟的命令（源码编译/大下载/工具链自举等）一律后台化执行：
`nohup bash -c '命令 > /work/build/xxx.log 2>&1; echo $? > /work/build/xxx.rc' &`，
之后轮询 marker/rc 文件取结果；前台 execute 只跑短命令。
绝对禁止直接替换或伪造系统工具链（/usr/bin/gcc 等）——工具链问题走攻略/工单，勿自造垫片
（教训：59CEF3 自写 gcc 垫片死循环，configure 探测永不返回，烧 13.6h CPU 后仍无产物）

【强制】现场记录 BUILD_NOTES.md：
- 源码就绪后第一件事：read_file 读 /skills/generic/build-notes-standard/SKILL.md，按其模板 write_file 创建 /work/output/BUILD_NOTES.md 骨架（含环境探测结果）
- 每过一个里程碑（依赖/configure/编译/install/打包/验证）重写补全对应节；每次踩坑换路实时记入"踩坑与修复"
- 无论成功失败，收尾前确保该文件完整（失败时重点：死在哪步、试过什么路线）
- 该文件与编译产物同等重要，缺失视为流程未完成（简单小包按其 10 行豁免规则写）"""

    try:
        # 阶段5 上限：避免 AI 死循环调 API（教训：9/30 932B3C 任务卡 stage5 26 分钟）
        # 默认 200 chunk / 30 分钟（ollama cmake --build 要 15+ 分钟，10 分钟不够）
        # 死循环启发式：连续 5 次快速 execute 失败 → 强退（区别真编译 vs 死循环）
        try:
            _max_chunks = int(get_cfg("stage5.max_chunks", 500))
        except ValueError:
            _max_chunks = 500
        try:
            _max_seconds = int(get_cfg("stage5.max_seconds", 10800))  # 3 小时
            # 2026-10-20：1800→10800。教训：torch 源码编译 ninja 一步就 ~30min+，
            # 任务 233 被 1800s 一刀切打死（"超时 2292s"非编译失败）；
            # 死循环仍由 chunk 上限 + 连续快速失败启发式兜底
        except ValueError:
            _max_seconds = 10800
        try:
            _loop_threshold = int(get_cfg("stage5.loop_threshold", 5))  # 连续快速失败次数
        except ValueError:
            _loop_threshold = 5
        try:
            _loop_window = float(get_cfg("stage5.loop_window", 5))  # 快速判定窗口（秒）
        except ValueError:
            _loop_window = 5.0

        start_ts = time.time()
        result = None
        prev_msg_count = 0
        chunk_count = 0
        # 长命令心跳：chunk 间隔 > gap 秒时补一行"仍在跑"（现在 pip install 300s 期间日志静默像卡死）
        try:
            _hb_gap = float(get_cfg("stage5.heartbeat_seconds", 30))
        except ValueError:
            _hb_gap = 30.0
        _last_chunk_ts = start_ts
        # 死循环检测：连续快速失败计数器
        # "快速失败" = 上一条 tool result 距今 < _loop_window 秒 + tool result 内容是 error/traceback
        _recent_fast_fail_count = 0
        _last_tool_ts = start_ts  # 上一条 tool result 的 wall-clock 时间
        # 墙钟看门狗线程（2026-10-23，审查 #12/#14 家族根治）：原超时检查在 chunk 到达时
        # 才执行——agent 卡在一条永不返回的命令里就没有 chunk，10800s 硬闸形同虚设
        # （59CEF3 实锤：卡 make_toolchain 假 gcc 死循环，超时检查饿死 13.6h）。
        # 到点直接 docker stop：exec socket 断流 → execute 返回错误 → 下一 chunk 命中
        # _external_terminal 检查强退；顺带消灭失控进程烧 CPU 的资源泄漏。
        # 宽限 120s：让容器内 timeout（execute 硬超时 A 层）先动作，本线程只兜底。
        _guard_stop = threading.Event()
        def _wall_guard(_st=state, _start=start_ts, _limit=_max_seconds + 120, _stop=_guard_stop):
            while not _stop.wait(60):
                if _st.get("_stage5_done"):  # 流正常收尾/各 return 前置位，看门狗随之退出
                    break
                if time.time() - _start > _limit:
                    _st["_external_terminal"] = TaskStatus.FAILED.value
                    _st["_external_error"] = f"stage5 墙钟硬闸（看门狗线程）：超 {_limit}s 且 chunk 流停滞，已强停容器"
                    _log(_st, "error", f"[阶段5] 墙钟硬闸触发：{int(_limit)}s，docker stop 容器强断（防检查点饿死）", on_log)
                    try:
                        _cli = get_docker_client()
                        if _cli and _st.get("container_id"):
                            _cli.containers.get(_st["container_id"]).stop(timeout=10)
                    except Exception as e:
                        _log(_st, "warn", f"[阶段5] 墙钟硬闸停容器失败: {e}", on_log)
                    break
        threading.Thread(target=_wall_guard, daemon=True).start()
        _warned_at_80 = False
        for chunk in agent.stream(
            {"messages": [{"role": "user", "content": task_prompt}]},
            stream_mode="values",
        ):
            chunk_count += 1
            elapsed = time.time() - start_ts
            # 心跳：距上一 chunk 静默 > _hb_gap 秒 → 补一行（stream 同步阻塞，心跳随下一 chunk 落地；
            # 对 60s 静默 pip install 这类场景，用户至少能看到"刚才静默了 Xs，不是卡死"）
            _gap = time.time() - _last_chunk_ts
            if _gap > _hb_gap:
                _log(state, "info",
                     f"[阶段5] 💓 静默 {_gap:.0f}s 后恢复（长命令执行中，非卡死）",
                     on_log)
            _last_chunk_ts = time.time()
            # 上限检查
            if chunk_count > _max_chunks:
                _log(state, "error",
                     f"[阶段5] 超过 chunk 上限 ({_max_chunks})，强退（疑似 AI 死循环）",
                     on_log)
                state["status"] = TaskStatus.FAILED.value
                state["last_error"] = f"stage5 超时：chunk 计数 {chunk_count} > {_max_chunks}"
                state["_stage5_done"] = True
                return state
            if elapsed > _max_seconds:
                _log(state, "error",
                     f"[阶段5] 超过时长上限 ({_max_seconds}s)，强退",
                     on_log)
                state["status"] = TaskStatus.FAILED.value
                state["last_error"] = f"stage5 超时：耗时 {elapsed:.1f}s > {_max_seconds}s"
                state["_stage5_done"] = True
                return state
            # 软警告：超过 80% 时限发一条提示（用户能看到"在跑"）
            if not _warned_at_80 and elapsed > _max_seconds * 0.8:
                _warned_at_80 = True
                _log(state, "warn",
                     f"[阶段5] 已用 {elapsed:.0f}s / {_max_seconds}s（80%），仍在跑；如非预期可手动取消",
                     on_log)
            # 死循环启发式：扫这一批新增消息里的 tool result
            messages = chunk.get("messages", [])
            new_msgs = messages[prev_msg_count:]
            now = time.time()
            for nm in new_msgs:
                nm_type = getattr(nm, "type", None) or getattr(nm, "role", None) or nm.__class__.__name__
                if nm_type != "tool":
                    continue
                content = nm.content if isinstance(nm.content, str) else str(nm.content or "")
                # T9-V：同一批 ToolMessage 喂收尾闸门做全绿检测（RESULT 机读行）
                if _green_gate is not None:
                    _green_gate.scan_tool_text(content)
                is_fail = "error:" in content.lower() or "traceback" in content.lower()
                # 间隔 = 当前 wall-clock - 上一条 tool result 时间
                gap = now - _last_tool_ts
                if is_fail and gap < _loop_window:
                    _recent_fast_fail_count += 1
                    if _recent_fast_fail_count >= _loop_threshold:
                        _log(state, "error",
                             f"[阶段5] 连续 {_loop_threshold} 次快速失败（间隔 < {_loop_window}s），疑似死循环，强退",
                             on_log)
                        state["status"] = TaskStatus.FAILED.value
                        state["last_error"] = (
                            f"stage5 死循环：连续 {_loop_threshold} 次快速失败"
                        )
                        state["_stage5_done"] = True
                        return state
                else:
                    _recent_fast_fail_count = 0
                _last_tool_ts = now
            # 外部标了 failed/success：_periodic_persist 已经感知，强退
            if state.get("_external_terminal"):
                _log(state, "error",
                     f"[阶段5] 外部已标 {state['_external_terminal']}，强退",
                     on_log)
                state["status"] = state["_external_terminal"]
                state["last_error"] = state.get("_external_error") or state["last_error"]
                state["_stage5_done"] = True
                return state

            for msg in messages[prev_msg_count:]:
                _render_agent_message(msg, state, start_ts, on_log)
            prev_msg_count = len(messages)
            result = chunk
        state["_stage5_done"] = True
        if result is None:
            result = {"messages": []}
        final = result["messages"][-1].content if result["messages"] else ""
        elapsed = time.time() - start_ts
        _log(state, "info", f"[阶段5] DeepAgent 完成: 耗时 {elapsed:.1f}s, chunks={chunk_count}", on_log)

        if "编译成功" in str(final) or "success" in str(final).lower():
            state["status"] = TaskStatus.SUCCESS.value
            _log(state, "success", f"[阶段5] 编译成功", on_log)
        else:
            state["status"] = TaskStatus.FAILED.value
            state["last_error"] = f"编译失败: {_truncate(str(final), 500)}"
            _log(state, "error", state["last_error"], on_log)

        if state["status"] != TaskStatus.FAILED.value:
            src_dir = f"{state['container_work_dir']}/src"
            state["artifacts"] = _collect_artifacts(container, state["output_dir"], src_dir)
            _log(state, "info", f"[阶段5] 产物: {len(state['artifacts'])} 个", on_log)
    except Exception as e:
        state["status"] = TaskStatus.FAILED.value
        state["last_error"] = f"{type(e).__name__}: {e}"
        _log(state, "error", f"[阶段5] 异常: {state['last_error']}", on_log)
        import traceback
        traceback.print_exc()
    return state


# ==================== 阶段 6 ====================
def _rescue_build_notes(state: TaskState, container, on_log=None) -> None:
    """BUILD_NOTES 落定到任务目录固定路径（与 build.log 同级，供工单/经验流消费）。
    成功任务：从 stage6 归档产物树拷；失败任务：容器将删，单独 get_archive 抢救。
    任何异常静默（消费端容忍缺失）。"""
    try:
        task_dir = Path(state.get("work_dir", ""))
        if not task_dir.is_dir():
            return
        dest = task_dir / "BUILD_NOTES.md"
        # 成功路径：归档树里已有（output/BUILD_NOTES.md）
        src = task_dir / "artifacts" / "output" / "BUILD_NOTES.md"
        if src.is_file():
            import shutil
            shutil.copyfile(src, dest)
            _log(state, "info", f"[阶段6] BUILD_NOTES 已落定: {dest} ({dest.stat().st_size}B)", on_log)
            return
        # 失败路径：容器还活着就单独拉一份
        if container is not None:
            chunks, _ = container.get_archive("/work/output/BUILD_NOTES.md")
            with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
                for ch in chunks:
                    tmp.write(ch.encode("utf-8") if isinstance(ch, str) else ch)
                tpath = tmp.name
            with tarfile.open(tpath) as tar:
                member = tar.extractfile(tar.getmember("BUILD_NOTES.md"))
                if member:
                    dest.write_bytes(member.read())
                    _log(state, "info", f"[阶段6] BUILD_NOTES 已从容器抢救: {dest} ({dest.stat().st_size}B)", on_log)
            os.unlink(tpath)
    except Exception as e:
        _log(state, "debug", f"[阶段6] BUILD_NOTES 落定跳过: {type(e).__name__}", on_log)


def stage6_cleanup(state: TaskState, on_log: Optional[LogCallback]) -> TaskState:
    cli = get_docker_client()

    # _stop_only：只停容器，不归档不删除不碰 work_dir（工单 resolved/cleanup-container 复用恢复路径）
    if state.get("_stop_only") and state.get("container_id") and cli:
        try:
            c = cli.containers.get(state["container_id"])
            c.stop(timeout=10)
            _log(state, "info", "[阶段6] 容器已停止（仅停止，可复用）", on_log)
        except Exception as e:
            _log(state, "warn", f"[阶段6] 容器停止失败: {e}", on_log)
        return state

    if state.get("status") == TaskStatus.SUCCESS.value and state.get("container_id"):
        try:
            container = cli.containers.get(state["container_id"])
            artifacts_dir = Path(state["work_dir"]) / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            chunks, _ = container.get_archive(state["output_dir"])
            # 批3-补（2026-10-26 C2/Q2 方案步1）：stage6 是第三处裸 extractall，
            # 与 admin resolve 同信任级（容器产物 tar 可被恶意上游植入越界/symlink
            # 成员二次传导宿主机）——统一走 safe_extract，tmp 补 finally 清理
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
                    for chunk in chunks:
                        if isinstance(chunk, str):
                            chunk = chunk.encode("utf-8")
                        tmp.write(chunk)
                    tmp_path = tmp.name
                from app.services import safe_extract
                safe_extract.safe_extract_tar(tmp_path, artifacts_dir)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            _log(state, "success", f"[阶段6] 产物归档: {artifacts_dir}", on_log)

            # 顺便打一份 tar.gz 整体包（用户点"下载全部产物"时直接返这个文件，避免每次都重打）
            bundle_path = artifacts_dir.parent / f"{artifacts_dir.name}.tar.gz"
            try:
                with tarfile.open(bundle_path, "w:gz") as tar:
                    tar.add(artifacts_dir, arcname=artifacts_dir.name)
                bundle_size = bundle_path.stat().st_size
                _log(state, "info",
                     f"[阶段6] 产物打包: {bundle_path} ({bundle_size // 1024} KB)", on_log)
            except Exception as e:
                _log(state, "warn", f"[阶段6] 产物打包失败（下载时将实时打包）: {e}", on_log)
            # 清单以宿主机归档树为准（与 tar 同源可对账；容器 find 仅前中期过程视图）
            _mf = _manifest_from_host(state.get("work_dir", ""))
            if _mf is not None:
                state["artifacts"] = _mf
                _log(state, "info", f"[阶段6] 产物清单(宿主机对账): {len(_mf)} 项（目录折叠）", on_log)
            # BUILD_NOTES 落定固定路径（从归档树拷）
            _rescue_build_notes(state, None, on_log)
        except Exception as e:
            _log(state, "warn", f"[阶段6] 产物归档失败: {e}", on_log)
    else:
        # 非 success（failed/cancelled）：容器马上被删，先抢救 BUILD_NOTES（工单流原料）
        _fail_c = None
        try:
            if state.get("container_id"):
                _fail_c = cli.containers.get(state["container_id"])
        except Exception:
            _fail_c = None
        _rescue_build_notes(state, _fail_c, on_log)

    if state.get("container_id"):
        try:
            c = cli.containers.get(state["container_id"])
            c.stop(timeout=10)
            c.remove()
            _log(state, "info", f"[阶段6] 容器已删除", on_log)
        except Exception as e:
            _log(state, "warn", f"[阶段6] 容器清理失败: {e}", on_log)

    try:
        work_dir = Path(state.get("work_dir", ""))
        p = work_dir / "container_work"
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass

    _log(state, "info", f"[阶段6] 清理完成: 状态={state.get('status')}", on_log)
    return state


# ==================== 工具函数 ====================
def _truncate(s: str, n: int = 200) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ⏎ ").strip()
    if len(s) <= n:
        return s
    # head 40% + tail 60%：报错/退出码通常在输出尾部，多留尾
    h = int((n - 12) * 0.4)
    t = n - 12 - h
    return f"{s[:h]} …省略{len(s)-h-t}字… {s[-t:]}"


def _classify_tool(name: str) -> str:
    return {
        "execute": "执行命令", "ls": "查看目录",
        "read_file": "读取文件", "write_file": "写入文件",
        "task": "启动子任务",
    }.get(name, name)


# docker exec 输出尾标 [Command failed with exit code N] / [Command succeeded...]
_EXIT_RE = re.compile(r"\[Command (?:succeeded|failed) with exit code (\d+)\]")

# Skill 命中收口（2026-10-20）：agent read_file 读 /skills/{generic|specific}/<name>/SKILL.md 即算命中。
# 命中名单进 state["_matched_skills"]（有序去重）→ WS 实时推 skills_matched 事件 + 终态落 CompileTask.matched_skills
_SKILL_READ_RE = re.compile(r"/skills/(?:generic|specific)/([a-z0-9][a-z0-9-]*)/SKILL\.md")


def _record_skill_match(state: "TaskState", name: str, on_log=None) -> None:
    """登记一次 Skill 命中：内存集合 + 日志行（进 build.log/DB，mount 恢复可解析）+ WS 实时事件"""
    matched = state.setdefault("_matched_skills", [])
    if name in matched:
        return
    matched.append(name)
    _log(state, "info", f"[阶段5] ✅ 命中 Skill: {name}", on_log)
    try:
        from app.services.ws_broadcaster import BROADCASTER
        tid = state.get("task_id")
        if tid:
            BROADCASTER.publish(tid, {"type": "skills_matched", "name": name,
                                      "names": list(matched), "ts": time.time()})
    except Exception:
        pass  # WS 推失败不影响编译主流程（日志行已落，mount 恢复兜底）


def _tool_arg(tool_args: dict, *keys: str, default: str = "") -> str:
    """按优先级从 args 取第一个存在的 key — 兼容 path/file_path/command/cmd 等参数名差异"""
    for k in keys:
        v = tool_args.get(k)
        if v:
            return str(v)
    return default


def _render_agent_message(msg, state: TaskState, start_ts: float, on_log: Optional[LogCallback]):
    ts = time.time() - start_ts
    msg_type = getattr(msg, "type", None) or getattr(msg, "role", None) or msg.__class__.__name__

    if msg_type == "ai":
        content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
        tool_calls = getattr(msg, "tool_calls", None) or []
        if content and content.strip():
            _log(state, "info", f"[+{ts:.1f}s] 💭 AI: {_truncate(content, 160)}", on_log)
        for tc in tool_calls:
            tool_name = tc.get("name", "?")
            tool_args = tc.get("args", {}) or {}
            label = _classify_tool(tool_name)
            # T4.1 配对短 id：并发多命令时 ▶[a3f2]/✓[a3f2] 能对上
            _cid = (tc.get("id") or "")[-4:]
            tag = f"[{_cid}]" if _cid else ""
            if tool_name == "execute":
                preview = f"$ {_truncate(_tool_arg(tool_args, 'command', 'cmd'), 300)}"
            elif tool_name in ("ls", "read_file"):
                # deepagents read_file/ls 参数名是 file_path/path 两种都兼容（历史 bug：只取 path 显示空）
                preview = _tool_arg(tool_args, "file_path", "path") or _truncate(str(tool_args), 120)
                if tool_name == "read_file":
                    _m = _SKILL_READ_RE.search(preview)
                    if _m:
                        _record_skill_match(state, _m.group(1), on_log)
            elif tool_name == "write_file":
                preview = f"→ {_tool_arg(tool_args, 'file_path', 'path')}"
            else:
                preview = _truncate(str(tool_args), 120)
            _log(state, "debug", f"[+{ts:.1f}s] ▶{tag} [{label}] {preview}", on_log)
            state.setdefault("build_logs", []).append({
                "ts": round(ts, 2), "kind": "tool_call",
                "tool": tool_name, "args": tool_args,
            })
    elif msg_type == "tool":
        content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
        tool_name = getattr(msg, "name", "tool")
        _tcid = (getattr(msg, "tool_call_id", "") or "")[-4:]
        tag = f"[{_tcid}]" if _tcid else ""
        # exit code 优先判定（docker exec 尾标 [Command failed with exit code N]），关键词猜仅作兜底
        m = _EXIT_RE.search(content)
        exit_code = int(m.group(1)) if m else None
        if exit_code is not None:
            mark = "✓" if exit_code == 0 else "❌"
            tail = "" if exit_code == 0 else f" → exit={exit_code}"
        else:
            mark = "❌" if ("error:" in content.lower() or "traceback" in content.lower()) else "✓"
            tail = ""
        summary = _truncate(content, 400)
        _log(state, "warn" if mark == "❌" else "info",
             f"[+{ts:.1f}s]   {mark}{tag} {summary}{tail}", on_log)
        state.setdefault("build_logs", []).append({
            "ts": round(ts, 2), "kind": "tool_result",
            "tool": tool_name, "output_len": len(content),
        })


# 排除扩展名（源码 + 中间产物 + 临时文件）
_EXCLUDE_EXTS = (
    # 源码
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx",
    ".go", ".py", ".rs", ".js", ".ts", ".jsx", ".tsx",
    ".java", ".kt", ".swift", ".m", ".mm",
    # 中间产物
    ".o", ".obj", ".a", ".lo", ".la", ".deps", ".libs",
    # 临时 / 调试
    ".log", ".tmp", ".cache", ".swp", ".bak", ".orig",
    # 源码内嵌资源（不算产物）
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml",
    # git 元数据
    ".git", ".gitignore",
)
_EXCLUDE_PATHS = (
    "/.git/", "/.github/", "/test/", "/tests/",
    "/__pycache__/", "/node_modules/", "/.venv/",
)


def _find_files_smart(container, search_dir: str) -> list:
    """在容器里扫 search_dir 下"最近 30 分钟修改 + size > 4KB + 非源码"的产物文件。

    不依赖项目结构，扫所有可能位置 — 治本。
    """
    # 用 find 过滤：-mmin -30 (30 分钟内) + -size +4k (大于 4KB) + 排除路径
    cmd = [
        "find", search_dir, "-type", "f",
        "-mmin", "-30", "-size", "+4k",
        "-not", "-path", "*/.git/*",
    ]
    result = container.exec_run(cmd)
    if result.exit_code != 0 or not result.output:
        return []
    raw = result.output.decode("utf-8", errors="replace") if isinstance(result.output, (bytes, bytearray)) else str(result.output or "")
    files = []
    for line in raw.split("\n"):
        p = line.strip()
        if not p:
            continue
        # 排除扩展名
        if any(p.lower().endswith(ext) for ext in _EXCLUDE_EXTS):
            continue
        # 排除路径
        if any(ex in p for ex in _EXCLUDE_PATHS):
            continue
        files.append(p)
    return files[:50]  # 上限 50 个


def bundle_size_bytes(work_dir: str) -> "int | None":
    """任务产物整包（artifacts.tar.gz）真实字节数=用户下载的实际传输量；无包/未归档返 None"""
    if not work_dir:
        return None
    b = Path(work_dir) / "artifacts.tar.gz"
    try:
        return b.stat().st_size if b.is_file() else None
    except OSError:
        return None


def _manifest_from_host(work_dir: str) -> "list[dict] | None":
    """stage6 归档完成后，以宿主机 artifacts/output/ 为事实源生成产物清单——
    与 tar 打包同树（货/tar/账三方可对账）。2026-10-22 教训（B9ED7E）：旧容器内
    find -type f -maxdepth 2 漏掉 libtorch 整棵目录树（清单 3 项 80M vs 实际 4 项 400M）。
    目录折叠为一个条目（type=dir，只记文件数——目录大小对下载无意义，
    真实传输量以 bundle_size=tar.gz 字节为准）；返回 None=目录不存在，调用方沿用旧兜底。"""
    out_dir = Path(work_dir) / "artifacts" / "output"
    if not out_dir.is_dir():
        return None
    items = []
    for p in sorted(out_dir.iterdir()):
        if p.name in (".", ".."):
            continue
        if p.is_file():
            items.append({"name": p.name, "path": f"/work/output/{p.name}", "size": p.stat().st_size})
        elif p.is_dir():
            n_files = sum(len(fns) for _dp, _dirs, fns in os.walk(p))
            items.append({"name": p.name + "/", "path": f"/work/output/{p.name}",
                          "type": "dir", "files": n_files})  # 不记磁盘和：下载走整包 tar，目录大小无传输意义
    # 排序：文件按大小降序、目录殿后——产物列"首文件名"=主产品（whl/tgz），不被 notes 抢头条
    items.sort(key=lambda a: (a.get("type") == "dir", -a.get("size", 0)))
    return items or None


def _collect_artifacts(container, output_dir: str, source_dir: str = "") -> list:
    """收集产物。
    1) 先看 /work/output
    2) 空就 fallback：扫 source_dir 下"编译期间生成 + 非源码"的文件，自动 cp 到 /work/output
    """
    artifacts = []
    try:
        # 1) 先看 /work/output
        r = container.exec_run(["find", output_dir, "-type", "f", "-maxdepth", "2"])
        if r.exit_code == 0 and r.output:
            raw = r.output.decode("utf-8", errors="replace") if isinstance(r.output, (bytes, bytearray)) else (r.output or "")
            for line in raw.split("\n"):
                if not line.strip() or Path(line).name in (".", ".."):
                    continue
                p = line.strip()
                sr = container.exec_run(["stat", "-c", "%s", p])
                size = 0
                if sr.exit_code == 0 and sr.output:
                    raw = sr.output.decode("utf-8", errors="replace").strip() if isinstance(sr.output, (bytes, bytearray)) else str(sr.output or "").strip()
                    size = int(raw) if raw.isdigit() else 0
                artifacts.append({"name": Path(p).name, "path": p, "size": size})
        # 2) 空就 fallback
        if not artifacts and source_dir:
            # 看 source_dir 存不存在
            check = container.exec_run(["bash", "-c", f"[ -d {source_dir} ] && echo OK || echo MISSING"])
            check_out = check.output.decode("utf-8", errors="replace") if isinstance(check.output, (bytes, bytearray)) else str(check.output or "")
            if "OK" in check_out:
                candidates = _find_files_smart(container, source_dir)
                if candidates:
                    # 自动复制到 /work/output
                    container.exec_run(["mkdir", "-p", output_dir])
                    # 3c（2026-10-23）：原 cmd_str 手拼单引号，文件名含 ' 即逃逸；
                    # 改参数列表直传（exec_run 不经 shell，无转义面），逐个 cp 容错
                    for f in candidates:
                        container.exec_run(["cp", "-a", "--", f, output_dir + "/"])
                    # 重新扫 output_dir
                    r2 = container.exec_run(["find", output_dir, "-type", "f", "-maxdepth", "3"])
                    if r2.exit_code == 0 and r2.output:
                        raw = r2.output.decode("utf-8", errors="replace") if isinstance(r2.output, (bytes, bytearray)) else (r2.output or "")
                        for line in raw.split("\n"):
                            if not line.strip() or Path(line).name in (".", ".."):
                                continue
                            p = line.strip()
                            sr = container.exec_run(["stat", "-c", "%s", p])
                            size = 0
                            if sr.exit_code == 0 and sr.output:
                                raw = sr.output.decode("utf-8", errors="replace").strip() if isinstance(sr.output, (bytes, bytearray)) else str(sr.output or "").strip()
                                size = int(raw) if raw.isdigit() else 0
                            artifacts.append({"name": Path(p).name, "path": p, "size": size})
    except Exception as e:
        # fallback 失败也不致命，返 0
        pass
    return artifacts[:10]


# ==================== 入口 ====================
def run_task(task_id: str, on_log: Optional[LogCallback] = None) -> dict:
    """worker 调用：跑一个完整任务

    返回 dict: {status, log_output, error_log, artifacts}
    """
    state: TaskState = {}
    db: Session = SessionLocal()
    task = None
    # 各阶段耗时（终态摘要用）；name 不带括号，渲染统一加
    _durs: dict = {}
    def _fin(name, fn):
        s0 = time.time()
        try:
            return fn()
        finally:
            _durs[name] = time.time() - s0
            _log(state, "debug", f"[{name}] 完成 (耗时 {time.time()-s0:.1f}s)", on_log)
    def _summary():
        parts = " ".join(f"{k}={v:.1f}s" for k, v in _durs.items())
        _log(state, "info", "════════ 任务总结 ════════", on_log)
        _log(state, "info", f"各阶段耗时: {parts}", on_log)
        if state.get("status") == TaskStatus.FAILED.value:
            _log(state, "error", f"根因: {_truncate(state.get('last_error') or '', 300)}", on_log)
        else:
            _log(state, "success", f"结果: {state.get('status')}，产物 {len(state.get('artifacts') or [])} 个", on_log)
    try:
        task = db.query(CompileTask).filter(CompileTask.task_id == task_id).first()
        if not task:
            return {"status": "failed", "error": f"任务不存在: {task_id}"}

        # 周期 commit / 外部标 failed 感知：把 task 引用塞进 state
        # _periodic_persist() 会按 _PERIODIC_COMMIT_EVERY 自动 commit
        state["_task"] = task
        state["task_id"] = task_id

        _fin("阶段1", lambda: stage1_init(task, state, on_log))
        if state["status"] == TaskStatus.FAILED.value:
            _summary()
            return _persist_and_return(db, task, state)

        _fin("阶段2", lambda: stage2_match_env(state, on_log))
        if state["status"] == TaskStatus.FAILED.value:
            _summary()
            return _persist_and_return(db, task, state)

        _fin("阶段3", lambda: stage3_create_container(state, on_log))
        if state["status"] == TaskStatus.FAILED.value:
            _summary()
            return _persist_and_return(db, task, state)

        _fin("阶段4a", lambda: stage4a_fetch_source_on_host(state, on_log))
        if state["status"] == TaskStatus.FAILED.value:
            try: stage6_cleanup(state, on_log)
            except Exception: pass
            _summary()
            return _persist_and_return(db, task, state)

        _fin("阶段4", lambda: stage4_import_source(state, on_log))
        if state["status"] == TaskStatus.FAILED.value:
            try: stage6_cleanup(state, on_log)
            except Exception: pass
            _summary()
            return _persist_and_return(db, task, state)

        _fin("阶段5", lambda: stage5_run_agent(state, on_log))
        _fin("阶段6", lambda: stage6_cleanup(state, on_log))

        _summary()
        return _persist_and_return(db, task, state)
    except Exception as e:
        state["status"] = TaskStatus.FAILED.value
        state["last_error"] = f"{type(e).__name__}: {e}"
        _log(state, "error", f"[run_task] 异常: {state['last_error']}", on_log)
        import traceback; traceback.print_exc()
        try: stage6_cleanup(state, on_log)
        except Exception: pass
        _summary()
        return _persist_and_return(db, task, state)
    finally:
        db.close()


def _persist_and_return(db: Session, task: Optional[CompileTask], state: TaskState) -> dict:
    """写回 DB 的字段对齐 CompileTask（spec 6.1: status 严格用 TaskStatus 枚举值）

    2026-10-21 Step 0：落库逻辑收编至 task_writer.write_terminal（唯一终态写入口）——
    字段分级（核心状态必须落/日志缓存可放弃）、净化、分块提交、哨兵+WS 告警全在那边。
    """
    if task is not None:
        from app.services.task_writer import write_terminal
        write_terminal(db, task, dict(state))

    return {
        "status": state.get("status"),
        "log_output": state.get("log_output", ""),
        "error_log": state.get("last_error", ""),
        "artifacts": state.get("artifacts", []),
    }


# ============== 报错工单 AI 总结（用于 /api/tickets/draft）==============
def _summarize_log_for_ticket(software_name: str, task_status: str, log_tail: str, build_notes: str = "") -> tuple:
    """
    调 LLM 总结 build.log 末尾，生成工单草稿。
    返回 (title, summary, description) 三元组；LLM 失败时降级到简单模板。
    """
    # 1) 提取结构化信号（exit code / 阶段 / 关键错）—— 模板兜底
    fallback_title = f"[失败] {software_name}"
    fallback_summary = None
    fallback_desc = (
        f"软件名：{software_name}\n"
        f"任务状态：{task_status}\n\n"
        f"日志末尾：\n```\n{log_tail[-3000:]}\n```\n\n"
        f"（AI 总结未启用，请根据上述日志人工描述）"
    )
    if not _ai_api_key():
        return fallback_title, fallback_summary, fallback_desc

    try:
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage, SystemMessage
        llm = ChatOpenAI(
            model=_ai_model(),
            api_key=_ai_api_key(),
            base_url=_ai_base_url() or None,
            temperature=0.0,  # 总结任务要确定
            max_tokens=2048,  # 思考型模型 reasoning 吃预算，600 实测 content 空→静默回退模板（2026-10-21 同 2048 病根）
        )
        # 日志太长截断（保险）
        log_for_llm = log_tail[-6000:] if len(log_tail) > 6000 else log_tail
        # BUILD_NOTES 优先：agent 现场记录（死在哪步/已排除路线）比原始日志尾部信息密度高
        notes_block = (
            f"以下是编译 agent 的现场记录 BUILD_NOTES.md（重点参考其踩坑与修复、步骤、已知限制节）：\n"
            f"{build_notes[:6000]}\n\n"
        ) if build_notes else ""
        prompt = (
            f"你是编译平台的报错分析助手。根据以下内容，"
            f"为用户生成工单草稿。\n\n"
            f"软件名：{software_name}\n"
            f"任务状态：{task_status}\n\n"
            f"{notes_block}"
            f"build.log 末尾：\n```\n{log_for_llm}\n```\n\n"
            f"请严格按以下 JSON 输出（不要任何多余文字）：\n"
            f'{{"title": "一句中文标题，不超过 30 字", '
            f'"summary": "2-4 句中文，定位失败原因/阶段/关键错误，控制在 150 字内", '
            f'"description": "中文完整描述，含：失败阶段、关键错误片段、可能原因建议（200-400 字）"}}'
        )
        resp = llm.invoke([
            SystemMessage(content="你只输出 JSON。"),
            HumanMessage(content=prompt),
        ])
        raw = (resp.content or "").strip()
        # 解析 JSON（容错：去 markdown fence）
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
        import json as _json
        d = _json.loads(raw)
        return (
            d.get("title", fallback_title)[:60],
            d.get("summary"),
            d.get("description", fallback_desc),
        )
    except Exception as e:
        # 失败兜底：返回简单模板，前端可编辑
        return fallback_title, f"AI 总结失败：{type(e).__name__}", fallback_desc
