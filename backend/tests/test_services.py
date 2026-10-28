"""test_services.py - 覆盖本轮 8 个改动文件"""
import asyncio
import os
import uuid
from pathlib import Path

import pytest


# 仓库根：tests/test_services.py → 往上 2 层到 backend/，再 1 层到仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "backend" / "app"


def _path(*parts) -> str:
    """根据仓库根拼路径（替代原绝对路径硬编码）"""
    return str(APP_ROOT.joinpath(*parts))


# ===================== skill_loader =====================
def test_skill_loader_path():
    from app.services.skill_loader import SKILLS_DIR_HOST
    # SKILLS_DIR_HOST 推断自 skill_loader.py 的 __file__，应该是仓库根 / skills
    import re
    assert re.search(r'/skills$', str(SKILLS_DIR_HOST)), f"应指向 */skills: {SKILLS_DIR_HOST}"


def test_skill_loader_count():
    from app.services.skill_loader import SKILLS_DIR_HOST
    md = list(SKILLS_DIR_HOST.rglob("SKILL.md"))
    # 曾硬编码 ==5，新增 skill 即碎 → 校验下限 + 已知 skill 全覆盖（数量随库增长自适应）
    names = {p.parent.name for p in md}
    assert len(md) >= 5, f"got {len(md)}: {md}"
    # 只校验 generic 工具链类（平台基建，稳定）；specific 随审批流增删改名，进名单必碎
    # （2026-10-21 教训：ollama/llama-cpp 被审批删除、pytorch 重命名后本测试即挂）
    for k in ("go-toolchain", "nodejs-toolchain", "python-environment", "python-package"):
        assert k in names, f"缺失 skill: {k}"


def test_skill_loader_sources():
    from app.services.skill_loader import SKILLS_PATHS
    assert SKILLS_PATHS == ["/skills/generic", "/skills/specific"]


def test_skill_loader_later_wins_order():
    """generic 在前，specific 在后 → last-wins 让 specific 覆盖 generic"""
    from app.services.skill_loader import SKILLS_PATHS
    assert SKILLS_PATHS.index("/skills/generic") < SKILLS_PATHS.index("/skills/specific")


def test_discover_dynamic():
    """加新 SKILL.md 会被自动发现（不改代码）"""
    import shutil
    from app.services import skill_loader
    new_dir = skill_loader.SKILLS_DIR_HOST / "generic" / "__pytest_temp_skill"
    try:
        new_dir.mkdir(parents=True, exist_ok=True)
        (new_dir / "SKILL.md").write_text(
            "---\nname: __pytest_temp\ndescription: temp\n---\nbody\n"
        )
        discovered = skill_loader.discover_skill_sources()
        assert "/skills/generic" in discovered
    finally:
        if new_dir.exists():
            shutil.rmtree(new_dir, ignore_errors=True)


# ===================== docker_backend =====================
def test_docker_backend_class_attrs():
    """所有 sandbox 协议方法 + async stub 都存在"""
    from app.services.docker_backend import DockerBackend
    required = ["execute", "ls", "read", "write", "edit", "delete",
                "glob", "grep", "download_files",
                "adelete", "adownload_files", "aedit", "aexecute",
                "aglob", "agrep", "als", "aread", "aupload_files", "awrite"]
    for m in required:
        assert hasattr(DockerBackend, m), f"missing {m}"


def test_docker_client_connected():
    from app.services.docker_backend import get_docker_client
    assert get_docker_client() is not None


# ===================== compile_engine =====================
def test_compile_engine_imports():
    from app.services.compile_engine import (
        run_task, stage1_init, stage2_match_env, stage3_create_container,
        stage4a_fetch_source_on_host, stage4_import_source,
        stage5_run_agent, stage6_cleanup,
    )
    assert callable(run_task)
    assert all(callable(f) for f in [
        stage1_init, stage2_match_env, stage3_create_container,
        stage4a_fetch_source_on_host, stage4_import_source,
        stage5_run_agent, stage6_cleanup,
    ])
    # ENV_BY_NAME 已随 stage2 改造为 DB 驱动（TargetSystem.base_image）删除，不再断言


def test_stage4a_uses_subprocess_not_docker_exec():
    """stage4a 必须在宿主机跑（subprocess），不能在容器内跑（exec_run）"""
    import inspect
    from app.services.compile_engine import stage4a_fetch_source_on_host
    src = inspect.getsource(stage4a_fetch_source_on_host)
    assert "subprocess" in src, "stage4a 应该用 subprocess 在宿主机跑 git"
    assert "exec_run" not in src, "stage4a 不应该走容器 exec_run（那是旧实现的卡死根源）"


def test_stage4a_has_timeout():
    """宿主机 git clone 必须有 timeout 兜底（避免无限挂起）"""
    import inspect
    from app.services.compile_engine import stage4a_fetch_source_on_host
    src = inspect.getsource(stage4a_fetch_source_on_host)
    assert "timeout=" in src, "stage4a 必须有 timeout 参数"


def test_stage4a_clones_into_container_work_src(tmp_path):
    """端到端：stage4a 在宿主机 clone 小仓库，落盘到 container_work/src/"""
    from app.services.compile_engine import stage4a_fetch_source_on_host, TaskState
    work_dir = tmp_path / "work"
    (work_dir / "container_work").mkdir(parents=True)

    state: TaskState = {
        "task_id": "TEST",
        "source_type": "git",
        "git_url": "https://github.com/octocat/Hello-World.git",
        "git_branch": "master",
        "work_dir": str(work_dir),
    }
    logs: list = []
    stage4a_fetch_source_on_host(state, lambda lvl, msg: logs.append(msg))

    src_dir = work_dir / "container_work" / "src"
    assert src_dir.exists(), f"宿主机源码目录应被创建: {src_dir}"
    assert (src_dir / ".git").exists(), "应该有 .git"
    assert state.get("commit_hash"), "应拿到 commit hash"
    assert state.get("host_src_dir") == str(src_dir)
    # 至少一行 stage4a 日志
    assert any("[阶段4a]" in m for m in logs), f"应输出 stage4a 日志: {logs}"


def test_stage4a_failure_does_not_hang(tmp_path):
    """错误路径：仓库不存在时 stage4a 应快速失败（抛异常），不无限挂起"""
    import time
    from app.services.compile_engine import stage4a_fetch_source_on_host, TaskState
    work_dir = tmp_path / "work"
    (work_dir / "container_work").mkdir(parents=True)
    state: TaskState = {
        "task_id": "TEST-FAIL",
        "source_type": "git",
        "git_url": "https://github.com/this-org-does-not-exist-xyz123/never.git",
        "git_branch": "main",
        "work_dir": str(work_dir),
    }
    t0 = time.time()
    with pytest.raises(RuntimeError, match="git clone"):
        stage4a_fetch_source_on_host(state, lambda lvl, msg: None)
    elapsed = time.time() - t0
    # git clone 失败应 < 30s（不要 10 分钟 timeout 才暴露）
    assert elapsed < 30, f"stage4a 失败应快速返回，实际 {elapsed:.1f}s"


def test_compile_engine_no_hardcoded_url():
    """system_prompt 不再硬编码 golang 等下载 URL"""
    path = _path("services", "compile_engine.py")
    with open(path) as f:
        code = f.read()
    assert "go.dev/dl/go1.27.2" not in code
    assert "## 编译流程\n1. 分析源码结构" not in code


def test_compile_engine_skill_prompt_present():
    """system_prompt 含 '从 Skill 读' 指引"""
    path = _path("services", "compile_engine.py")
    with open(path) as f:
        code = f.read()
    assert "Skill 读" in code or "从 Skill 读" in code


# ===================== task_queue =====================
def test_task_queue_constants():
    from app.services.task_queue import HEARTBEAT_TIMEOUT
    # 30 分钟（1800s）：8 worker 并发下放宽超时，避免跑大编译时任务被误判死亡回收
    # 教训 9/30 lesson 3：setup.sh stop SIGKILL fallback 需要 HEARTBEAT_TIMEOUT 配合
    assert HEARTBEAT_TIMEOUT == 1800


def test_task_queue_actual_flow():
    """真实入库 + enqueue + dequeue 端到端"""
    from app.models import SessionLocal, CompileTask, TaskStatus, TargetSystem, User
    from app.services.task_queue import enqueue, dequeue

    db = SessionLocal()
    try:
        sys_rec = db.query(TargetSystem).first()
        if not sys_rec:
            sys_rec = TargetSystem(name="oe-pytest", arch="loongarch64",
                                   base_image="loongnix-build:23.2", is_active=True)
            db.add(sys_rec); db.commit(); db.refresh(sys_rec)
        user_rec = db.query(User).first()
        if not user_rec:
            user_rec = User(username="pytest_user", password_hash="x", role="user")
            db.add(user_rec); db.commit(); db.refresh(user_rec)

        tid = f"PYTEST-{uuid.uuid4().hex[:8].upper()}"
        task = CompileTask(
            task_id=tid, user_id=user_rec.user_id,
            system_id=sys_rec.system_id, software_name="pytest-software",
            source_type="git", git_url="https://example.com/repo.git",
            compile_mode="container", status=TaskStatus.SUBMITTED.value,
        )
        db.add(task); db.commit()

        # 1. enqueue 幂等返回 True
        assert enqueue(tid) is True

        # 2. dequeue 抢到本任务
        grabbed = dequeue("pytest-worker")
        assert grabbed == tid

        # 3. 状态转 compiling
        t = db.query(CompileTask).filter(CompileTask.task_id == tid).first()
        assert t.status == TaskStatus.COMPILING.value

        # 4. 清理
        db.delete(t); db.commit()
    finally:
        db.close()


# ===================== ws_broadcaster =====================
def test_ws_broadcaster_singleton():
    from app.services.ws_broadcaster import BROADCASTER
    assert BROADCASTER is not None


def test_ws_broadcaster_pubsub():
    """publish → subscribe 联通"""
    from app.services.ws_broadcaster import BROADCASTER

    async def run():
        q = BROADCASTER.subscribe("PYTEST-TASK")
        BROADCASTER.publish("PYTEST-TASK", {"type": "log", "msg": "ping"})
        msg = await asyncio.wait_for(q.get(), timeout=2)
        BROADCASTER.unsubscribe("PYTEST-TASK", q)
        return msg

    msg = asyncio.run(run())
    assert msg == {"type": "log", "msg": "ping"}


# worker.py / run.py 已删（C-7/C-5，2026-10-26：废弃独立 worker 进程与零引用启动
# 脚本；对应 test_worker_constants 同删——防"55 绿变红"必须先清断言）


# ===================== routes/compile =====================
def test_route_compile_enqueues():
    """POST /submit 末尾调 task_queue.enqueue"""
    with open(_path("routes", "compile.py")) as f:
        src = f.read()
    assert "from app.services import task_queue" in src
    assert "task_queue.enqueue(task_id)" in src


def test_route_compile_preserves_api():
    """API 路径未改"""
    with open(_path("routes", "compile.py")) as f:
        src = f.read()
    assert '@router.post("/submit"' in src
    assert '@router.get("/tasks"' in src


# ===================== routes/ws =====================
def test_route_ws_uses_broadcaster():
    with open(_path("routes", "ws.py")) as f:
        src = f.read()
    assert "from app.services.ws_broadcaster import BROADCASTER" in src
    assert "BROADCASTER.subscribe" in src
    assert "BROADCASTER.publish" in src
    assert "/ws/compile/log/{task_id}" in src


# ===================== 全链路 =====================
def test_app_load():
    """FastAPI app + 全部 routers 注册成功（数集合不数个数——写死计数每加一个
    router 就假红，T12 notifications 上线时踩过；且本 FastAPI 版 _IncludedRouter
    不展平路径（计数/属性两条老路都走不通），取 openapi paths 视图）"""
    from app.main import app
    joined = " ".join(app.openapi().get("paths", {}).keys())
    # WebSocket 路由不进 openapi，也不在顶层展平（藏在 _IncludedRouter 内）——
    # 直接探 ws router 自身注册面
    from app.routes.ws import router as _wsr
    ws_ok = any(getattr(r, "path", "").startswith("/ws") for r in _wsr.routes)
    for need in ("/api/auth", "/api/compile", "/api/tickets", "/api/experience",
                 "/api/admin", "/api/notifications"):
        assert need in joined, f"router 缺失: {need}"
    assert ws_ok, "ws router 缺失"


def test_skills_middleware_in_container():
    import docker
    from app.services.docker_backend import DockerBackend
    from app.services.skill_loader import SKILLS_DIR_HOST, SKILLS_PATHS
    from deepagents.middleware.skills import SkillsMiddleware

    cli = docker.from_env()
    c = cli.containers.run(
        os.environ.get("DOCKER_IMAGE", "loongnix-build:23.2"),
        command="sleep 60", detach=True,
        volumes={str(SKILLS_DIR_HOST): {"bind": "/skills", "mode": "ro"}},
    )
    try:
        backend = DockerBackend(c.id, "/work")
        sm = SkillsMiddleware(backend=backend, sources=SKILLS_PATHS)
        res = sm.before_agent(
            {"skills_metadata": None, "skills_load_errors": []},
            type("R", (), {})(), {},
        )
        loaded = sorted(s["name"] for s in res["skills_metadata"])
        # 期望名单从磁盘推导（曾硬编码 5 个旧名字，llama-cpp 更名/新增 skill 即碎）
        # 若有 skill 因 frontmatter 坏被 middleware skip，磁盘集合 ⊃ loaded 集合 → 本断言会失败报警
        expected = sorted({p.parent.name for p in SKILLS_DIR_HOST.rglob("SKILL.md")})
        assert loaded == expected, f"got {loaded}"
        assert res["skills_load_errors"] == []
    finally:
        c.kill()


# ===================== submit 路径回归 =====================
def test_submit_status_returns_string():
    """POST /submit 返回 dict["status"] 必须是字符串（不是 enum）"""
    with open(_path("routes", "compile.py")) as f:
        src = f.read()
    # 修过的代码用 str(task.status)，不能再用 .value（str 没 .value 属性）
    assert '"status": str(task.status),' in src, \
        "POST /submit 应该用 str(task.status) 而非 task.status.value"


def test_submit_returns_task_id():
    """POST /submit 返回值含 task_id + status 两个键"""
    with open(_path("routes", "compile.py")) as f:
        src = f.read()
    assert '"task_id": task.task_id' in src
    assert '"status": str(task.status)' in src
