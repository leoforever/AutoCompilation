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
import shutil
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from typing_extensions import TypedDict

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CompileTask, TaskStatus, SessionLocal
from app.services.docker_backend import DockerBackend, get_docker_client
from app.services.skill_loader import get_skills_paths, SKILLS_DIR_HOST, SKILLS_DIR_CONTAINER


# ==================== AI / Engine 配置（按 spec：不写死，从环境变量读）====================
def _ai_model() -> str:
    return os.environ.get("AI_MODEL", "agnes-2.5-flash")

def _ai_api_key() -> str:
    return os.environ.get("AI_API_KEY", "")

def _ai_base_url() -> str:
    return os.environ.get("AI_BASE_URL", "")

def _max_retries() -> int:
    return int(os.environ.get("MAX_RETRIES", "4"))

def _artifacts_dir() -> Path:
    cfg = get_settings()
    base = Path(os.environ.get("ARTIFACTS_DIR") or cfg.artifact_dir)
    if not base.is_absolute():
        # 从本文件位置推断仓库根（services/compile_engine.py → 往上一层到 backend/app/services → 上 2 层到 backend → 上 3 层到仓库根）
        project_root = Path(__file__).resolve().parents[3]
        base = project_root / base
    base.mkdir(parents=True, exist_ok=True)
    return base


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
    ts = datetime.now().strftime("%H:%M:%S")
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
        # 写 log
        from app.models import SessionLocal as _SL
        s = _SL()
        try:
            t = s.query(CompileTask).filter(CompileTask.task_id == state.get("task_id")).first()
            if t:
                t.log_output = state.get("log_output", "")
                s.commit()
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
        "git_branch": task.git_branch or "main",
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
def stage3_create_container(state: TaskState, on_log: Optional[LogCallback]) -> TaskState:
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
            labels={"task_id": state["task_id"], "phase": "infrastructure"},
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
        # git clone（原有逻辑，原样保留）
        # 复用已有 clone（节省时间）
        if src_dir.exists() and (src_dir / ".git").exists():
            _log(state, "info", f"[阶段4a] 源码已存在，复用: {src_dir}", on_log)
        else:
            if src_dir.exists():
                shutil.rmtree(src_dir, ignore_errors=True)
            src_dir.parent.mkdir(parents=True, exist_ok=True)

            branch = state.get("git_branch") or "main"
            url = state["git_url"]
            cmd = ["git", "clone", "--depth", "1", "--branch", branch, url, str(src_dir)]

            _log(state, "info", f"[阶段4a] 宿主机 git clone: {url} (branch={branch})", on_log)
            try:
                proc = subprocess.Popen(
                    cmd,
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
    """解压 .tar.gz / .zip 到 dst_dir

    安全防护：
      - 解压文件数 ≤ 50,000（防 zip bomb 大量小文件）
      - 解压后总字节 ≤ 10 GB（防 zip bomb 单文件超大）
      - 文件路径必须落到 dst_dir 内（防 zip slip）
    """
    MAX_FILES = 50_000
    MAX_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB

    suffix = archive_path.suffix.lower()
    if archive_path.name.lower().endswith(".tar.gz") or suffix == ".tgz":
        import tarfile
        members = 0
        total_bytes = 0
        dst_resolved = dst_dir.resolve()
        with tarfile.open(archive_path, "r:gz") as tf:
            for member in tf:
                members += 1
                if members > MAX_FILES:
                    raise RuntimeError(f"压缩包内文件数超过 {MAX_FILES}，疑似 zip bomb")
                # zip slip 检查：解压后路径必须在 dst_dir 内
                target_path = (dst_dir / member.name).resolve()
                if not str(target_path).startswith(str(dst_resolved)):
                    raise RuntimeError(f"非法解压路径（zip slip?）：{member.name}")
                total_bytes += member.size or 0
                if total_bytes > MAX_BYTES:
                    raise RuntimeError(f"解压后总字节超过 {MAX_BYTES // (1024**3)} GB")
                if on_log and members % 200 == 0:
                    _log(state, "info", f"[阶段4a] 解压中... {members} 文件", on_log)
            tf.extractall(dst_dir)
    elif suffix == ".zip":
        import zipfile
        members = 0
        total_bytes = 0
        dst_resolved = dst_dir.resolve()
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                members += 1
                if members > MAX_FILES:
                    raise RuntimeError(f"压缩包内文件数超过 {MAX_FILES}，疑似 zip bomb")
                target_path = (dst_dir / info.filename).resolve()
                if not str(target_path).startswith(str(dst_resolved)):
                    raise RuntimeError(f"非法解压路径（zip slip?）：{info.filename}")
                total_bytes += info.file_size or 0
                if total_bytes > MAX_BYTES:
                    raise RuntimeError(f"解压后总字节超过 {MAX_BYTES // (1024**3)} GB")
                if on_log and members % 200 == 0:
                    _log(state, "info", f"[阶段4a] 解压中... {members} 文件", on_log)
            zf.extractall(dst_dir)
    else:
        raise RuntimeError(f"不支持的源码包格式：{suffix}（仅 .tar.gz / .zip / .tgz）")


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
        _max_workers = int(os.environ.get("MAX_WORKERS", "8"))
    except ValueError:
        _max_workers = 8
    _jobs_per_worker = max(2, 32 // max(1, _max_workers))

    system_prompt = f"""你是一个 LoongArch 编译工程师，工作在龙芯容器环境里。

## 任务
- 软件: {state['software_name']}
- 源码: {src_dir}
- 输出: {output_dir}
- 系统: {state.get('system_id', '')}

## 并发限制（重要）
本服务器启用了多 worker 并行处理任务（当前 {_max_workers} 个 worker 共用 32 物理核）。
为避免 worker 之间互相抢占 CPU 拖慢整体，**编译时强制限速**：
- make: `make -j{_jobs_per_worker}`
- cmake: `-j{_jobs_per_worker}` 参数
- go build: `go build -p {_jobs_per_worker}` 或 `GOMAXPROCS={_jobs_per_worker}`
- ninja: `-j{_jobs_per_worker}`
- cargo: `cargo build -j {_jobs_per_worker}`
不要用 `$(nproc)` 或不指定 -j（会默认吃满 32 核，跟其他 worker 抢）

## 你能调用的工具
- ls/read_file/write_file/edit_file/grep/glob — 文件操作
- execute — 在容器里跑命令
- task — 拆子任务

## 知识来源（重要）
**不要凭记忆猜工具链地址、编译命令、错误模式**。
所有具体知识从 Skill 读：系统提示下方的 Skill 清单列出了当前可用 Skill，每条含触发场景描述。
命中后用 read_file 读对应路径的 SKILL.md 全文执行。

## 作业流程
1. 看 Skills 清单，找最匹配的（专用 Skill → generic 工具链 → generic 错误经验）
2. 命中后 read_file 读全文，按攻略执行
3. 编译失败再读通用错误类 Skill
4. 产物生成后移动到输出目录
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
    agent = create_deep_agent(
        model=llm,
        system_prompt=system_prompt,
        backend=backend,
        skills=_skills_paths,
    )

    _log(state, "info", f"[阶段5] DeepAgent 启动, skills={len(_skills_paths)}", on_log)

    state["current_attempt"] = 1
    task_prompt = f"""开始编译 {state['software_name']}。

源码已克隆到 {src_dir}。

请执行完整的编译流程：先用 read_file 读 Skills 清单中匹配的 SKILL.md，再按攻略执行。
完成后输出编译结果摘要（中文，包含"编译成功"或"编译失败"）。"""

    try:
        # 阶段5 上限：避免 AI 死循环调 API（教训：9/30 932B3C 任务卡 stage5 26 分钟）
        # 默认 200 chunk / 30 分钟（ollama cmake --build 要 15+ 分钟，10 分钟不够）
        # 死循环启发式：连续 5 次快速 execute 失败 → 强退（区别真编译 vs 死循环）
        try:
            _max_chunks = int(os.environ.get("STAGE5_MAX_CHUNKS", "200"))
        except ValueError:
            _max_chunks = 200
        try:
            _max_seconds = int(os.environ.get("STAGE5_MAX_SECONDS", "1800"))  # 30 分钟
        except ValueError:
            _max_seconds = 1800
        try:
            _loop_threshold = int(os.environ.get("STAGE5_LOOP_THRESHOLD", "5"))  # 连续快速失败次数
        except ValueError:
            _loop_threshold = 5
        try:
            _loop_window = float(os.environ.get("STAGE5_LOOP_WINDOW", "5"))  # 快速判定窗口（秒）
        except ValueError:
            _loop_window = 5.0

        start_ts = time.time()
        result = None
        prev_msg_count = 0
        chunk_count = 0
        # 死循环检测：连续快速失败计数器
        # "快速失败" = 上一条 tool result 距今 < _loop_window 秒 + tool result 内容是 error/traceback
        _recent_fast_fail_count = 0
        _last_tool_ts = start_ts  # 上一条 tool result 的 wall-clock 时间
        _warned_at_80 = False
        for chunk in agent.stream(
            {"messages": [{"role": "user", "content": task_prompt}]},
            stream_mode="values",
        ):
            chunk_count += 1
            elapsed = time.time() - start_ts
            # 上限检查
            if chunk_count > _max_chunks:
                _log(state, "error",
                     f"[阶段5] 超过 chunk 上限 ({_max_chunks})，强退（疑似 AI 死循环）",
                     on_log)
                state["status"] = TaskStatus.FAILED.value
                state["last_error"] = f"stage5 超时：chunk 计数 {chunk_count} > {_max_chunks}"
                return state
            if elapsed > _max_seconds:
                _log(state, "error",
                     f"[阶段5] 超过时长上限 ({_max_seconds}s)，强退",
                     on_log)
                state["status"] = TaskStatus.FAILED.value
                state["last_error"] = f"stage5 超时：耗时 {elapsed:.1f}s > {_max_seconds}s"
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
                return state

            for msg in messages[prev_msg_count:]:
                _render_agent_message(msg, state, start_ts, on_log)
            prev_msg_count = len(messages)
            result = chunk
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
            state["artifacts"] = _collect_artifacts(container, output_dir)
            _log(state, "info", f"[阶段5] 产物: {len(state['artifacts'])} 个", on_log)
    except Exception as e:
        state["status"] = TaskStatus.FAILED.value
        state["last_error"] = f"{type(e).__name__}: {e}"
        _log(state, "error", f"[阶段5] 异常: {state['last_error']}", on_log)
        import traceback
        traceback.print_exc()
    return state


# ==================== 阶段 6 ====================
def stage6_cleanup(state: TaskState, on_log: Optional[LogCallback]) -> TaskState:
    cli = get_docker_client()

    if state.get("status") == TaskStatus.SUCCESS.value and state.get("container_id"):
        try:
            container = cli.containers.get(state["container_id"])
            artifacts_dir = Path(state["work_dir"]) / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            chunks, _ = container.get_archive(state["output_dir"])
            with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
                for chunk in chunks:
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    tmp.write(chunk)
                tmp_path = tmp.name
            with tarfile.open(tmp_path) as tar:
                tar.extractall(path=artifacts_dir)
            os.unlink(tmp_path)
            _log(state, "success", f"[阶段6] 产物归档: {artifacts_dir}", on_log)
        except Exception as e:
            _log(state, "warn", f"[阶段6] 产物归档失败: {e}", on_log)

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
    h = (n - 5) // 2
    return f"{s[:h]} ... {s[-h:]}"


def _classify_tool(name: str) -> str:
    return {
        "execute": "执行命令", "ls": "查看目录",
        "read_file": "读取文件", "write_file": "写入文件",
        "task": "启动子任务",
    }.get(name, name)


def _render_agent_message(msg, state: TaskState, start_ts: float, on_log: Optional[LogCallback]):
    ts = time.time() - start_ts
    msg_type = getattr(msg, "type", None) or getattr(msg, "role", None) or msg.__class__.__name__

    if msg_type == "ai":
        content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
        tool_calls = getattr(msg, "tool_calls", None) or []
        if content and content.strip():
            _log(state, "info", f"[+{ts:5.1f}s] 💭 AI: {_truncate(content, 160)}", on_log)
        for tc in tool_calls:
            tool_name = tc.get("name", "?")
            tool_args = tc.get("args", {})
            label = _classify_tool(tool_name)
            if tool_name == "execute":
                preview = f"$ {_truncate(tool_args.get('command', ''), 200)}"
            elif tool_name in ("ls", "read_file"):
                preview = tool_args.get("path", "")
            elif tool_name == "write_file":
                preview = f"→ {tool_args.get('path', '')}"
            else:
                preview = _truncate(str(tool_args), 120)
            _log(state, "info", f"[+{ts:5.1f}s] ▶ [{label}] {preview}", on_log)
            state.setdefault("build_logs", []).append({
                "ts": round(ts, 2), "kind": "tool_call",
                "tool": tool_name, "args": tool_args,
            })
    elif msg_type == "tool":
        content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
        tool_name = getattr(msg, "name", "tool")
        summary = _truncate(content, 200)
        mark = "❌" if ("error:" in content.lower() or "traceback" in content.lower()) else "✓"
        _log(state, "info", f"[+{ts:5.1f}s]   {mark} {summary}", on_log)
        state.setdefault("build_logs", []).append({
            "ts": round(ts, 2), "kind": "tool_result",
            "tool": tool_name, "output_len": len(content),
        })


def _collect_artifacts(container, output_dir: str) -> list:
    artifacts = []
    try:
        r = container.exec_run(["find", output_dir, "-type", "f", "-maxdepth", "2"])
        if r.exit_code == 0 and r.output:
            raw = r.output
            output = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else (raw or "")
            for line in output.split("\n"):
                if not line.strip() or Path(line).name in (".", ".."):
                    continue
                p = line.strip()
                sr = container.exec_run(["stat", "-c", "%s", p])
                size = 0
                if sr.exit_code == 0 and sr.output:
                    raw = sr.output
                    s = raw.decode("utf-8", errors="replace").strip() if isinstance(raw, (bytes, bytearray)) else str(raw or "").strip()
                    size = int(s) if s.isdigit() else 0
                artifacts.append({"name": Path(p).name, "path": p, "size": size})
    except Exception:
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
    try:
        task = db.query(CompileTask).filter(CompileTask.task_id == task_id).first()
        if not task:
            return {"status": "failed", "error": f"任务不存在: {task_id}"}

        # 周期 commit / 外部标 failed 感知：把 task 引用塞进 state
        # _periodic_persist() 会按 _PERIODIC_COMMIT_EVERY 自动 commit
        state["_task"] = task
        state["task_id"] = task_id

        stage1_init(task, state, on_log)
        if state["status"] == TaskStatus.FAILED.value:
            return _persist_and_return(db, task, state)

        stage2_match_env(state, on_log)
        if state["status"] == TaskStatus.FAILED.value:
            return _persist_and_return(db, task, state)

        stage3_create_container(state, on_log)
        if state["status"] == TaskStatus.FAILED.value:
            return _persist_and_return(db, task, state)

        stage4a_fetch_source_on_host(state, on_log)
        if state["status"] == TaskStatus.FAILED.value:
            try: stage6_cleanup(state, on_log)
            except Exception: pass
            return _persist_and_return(db, task, state)

        stage4_import_source(state, on_log)
        if state["status"] == TaskStatus.FAILED.value:
            try: stage6_cleanup(state, on_log)
            except Exception: pass
            return _persist_and_return(db, task, state)

        stage5_run_agent(state, on_log)
        stage6_cleanup(state, on_log)

        return _persist_and_return(db, task, state)
    except Exception as e:
        state["status"] = TaskStatus.FAILED.value
        state["last_error"] = f"{type(e).__name__}: {e}"
        _log(state, "error", f"[run_task] 异常: {state['last_error']}", on_log)
        import traceback; traceback.print_exc()
        try: stage6_cleanup(state, on_log)
        except Exception: pass
        return _persist_and_return(db, task, state)
    finally:
        db.close()


def _persist_and_return(db: Session, task: Optional[CompileTask], state: TaskState) -> dict:
    """写回 DB 的字段对齐 CompileTask（spec 6.1: status 严格用 TaskStatus 枚举值）"""
    if task is not None:
        try:
            task.status = state.get("status", TaskStatus.FAILED.value)
            task.log_output = state.get("log_output", "")
            task.error_message = state.get("last_error", "")
            artifacts = state.get("artifacts", [])
            task.artifact_path = json.dumps(artifacts, ensure_ascii=False) if artifacts else (task.artifact_path or None)
            now = datetime.utcnow()
            if task.started_at is None:
                task.started_at = now
            task.completed_at = now
            if task.started_at:
                task.duration_seconds = int((task.completed_at - task.started_at).total_seconds())
            db.commit()
        except Exception as e:
            db.rollback()
            print(f"[persist error] {e}")

    return {
        "status": state.get("status"),
        "log_output": state.get("log_output", ""),
        "error_log": state.get("last_error", ""),
        "artifacts": state.get("artifacts", []),
    }
