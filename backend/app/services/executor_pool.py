"""执行机池：连接路由 + 选机 + 冷却（11-26 多执行机实施，设计档 docs/multi-executor-design.md）

纪律：
- 控制机永远在候选池（is_builtin 兜底项）：任何外接机故障，任务回本机，
  平台不会因为"池子全挂"停摆。
- 选机失败/无候选 → 落本机（status=online 的 builtin），绝不抛错阻断任务。
- 冷却语义照搬 LLM 治理层：派失败冷却该台（时长 config 化），重试排除上次
  失败那台；到期自动回池，无需人工。
- 连接缓存：按 docker_host 缓存 DockerClient，断线自动重建一次（ssh 隧道
  半死是常态，ping 不过就换新参建）。
"""
import json
import logging
import threading
from datetime import datetime, timedelta

logger = logging.getLogger("executor_pool")

_lock = threading.Lock()
_clients: dict = {}   # docker_host -> DockerClient


def client_for(host: str = ""):
    """按执行机取 docker 客户端（"" = 本机 from_env 单例走原路径）。
    失败返回 None（调用方决定降级/报错），并把坏缓存踢出。"""
    if not host:
        from app.services.docker_backend import get_docker_client
        return get_docker_client()
    with _lock:
        cli = _clients.get(host)
    if cli is not None:
        try:
            cli.ping()
            return cli
        except Exception:
            with _lock:
                _clients.pop(host, None)
    try:
        import docker
        cli = docker.DockerClient(base_url=host)
        cli.ping()
    except Exception as e:
        logger.warning("执行机连接失败 %s: %s", host, str(e)[:140])
        return None
    with _lock:
        _clients[host] = cli
    return cli


def pick_executor(db, sys_arch: str, exclude_id: int = 0):
    """stage3 起容器前调用：返回 (executor_id, docker_host)。
    过滤链 = 设计档 §三/§八（盘过滤③留阶段二）：status=online ∧ 冷却未到期 ∧
           arch 匹配 ∧（非 builtin）连接活性；排序 = 在跑任务最少。
    images 清单不做否决（偏离设计档一处，有意）：ensure_image 自带 pull 兜底，
    公共镜像现场拉通（.91 实测），登记清单只作面板信息——否决反而把
    "配了系统就能用"拍板打回去。
    无候选语义：arch 匹配不上绝不回落本机（x86 任务扔龙芯=Illegal instruction
    假成功派单）；返回 (0, "") 调用方判——本机恰好是该 arch（内置项 arch 匹配）
    则等于回落本机。"""
    from app.models import Executor
    sys_arch = (sys_arch or "loongarch64").strip().lower()
    now = datetime.utcnow()

    def _builtin():
        return db.query(Executor).filter(Executor.is_builtin.is_(True)).first()

    fb = _builtin()
    cands = [e for e in db.query(Executor).filter(
        Executor.status == "online",
    ).all()
        if (e.arch or "loongarch64").strip().lower() == sys_arch
        and not (e.cooldown_until and e.cooldown_until > now)
        and (not exclude_id or e.id != exclude_id)]
    live = []
    for e in cands:
        if e.is_builtin:
            live.append((e, _running_tasks(db, e.id)))
            continue
        # 非本机：连接活才进候选（client_for 内含重连一次）；死机自动进冷却
        if client_for(e.docker_host) is None:
            _mark_cooldown(db, e, reason="心跳不可达")
            continue
        live.append((e, _running_tasks(db, e.id)))
    if live:
        live.sort(key=lambda kv: (kv[1], 0 if kv[0].is_builtin else 1, kv[0].id))
        e = live[0][0]
        return e.id, (e.docker_host or "")
    # 无在线候选：本机 arch 恰好匹配 → 回落内置（池全灭不停摆）；否则派不了
    if fb and fb.status != "removed" and (fb.arch or "loongarch64").lower() == sys_arch \
            and (not exclude_id or fb.id != exclude_id):
        return fb.id, ""
    return 0, ""


def _running_tasks(db, executor_id: int) -> int:
    from app.models import CompileTask
    return db.query(CompileTask).filter(
        CompileTask.executor_id == executor_id,
        CompileTask.status == "compiling").count()


def _mark_cooldown(db, ex, reason: str = ""):
    from app.config import get_cfg
    mins = int(get_cfg("executors.cooldown_minutes", 10))
    ex.status = "cooldown"
    ex.cooldown_until = datetime.utcnow() + timedelta(minutes=mins)
    ex.note = ((ex.note or "") + f" [冷却:{reason} @{datetime.utcnow():%m-%d %H:%M}]")[-290:]
    db.commit()
    logger.info("执行机 %s 冷却 %d 分钟（%s）", ex.name, mins, reason)


def mark_failure(db, executor_id: int, reason: str = ""):
    """派失败/起容器失败：外接机进冷却（builtin 不冷却——本机没得可换）。
    重试方传 exclude_id 排除上次那台。"""
    from app.models import Executor
    if not executor_id:
        return
    ex = db.query(Executor).filter(Executor.id == executor_id).first()
    if ex is None or ex.is_builtin:
        return
    _mark_cooldown(db, ex, reason or "派发失败")


def executor_of(db, executor_id: int):
    from app.models import Executor
    if not executor_id:
        return None
    return db.query(Executor).filter(Executor.id == executor_id).first()


def probe(db, ex) -> dict:
    """进池自检/面板刷新：ping + docker 版本 + 架构 + 核数 + 盘水位。
    全走该执行机自己的 docker API（不需要 ssh 通道之外的任何东西）。"""
    out = {"ok": False, "reason": "", "version": "", "arch": "", "nproc": 0,
           "disk_free_gb": 0.0, "images": 0}
    cli = client_for(ex.docker_host)
    if cli is None:
        out["reason"] = "连接失败（docker API 不可达）"
        return out
    try:
        info = cli.info()
        out.update(ok=True, version=str(info.get("ServerVersion", "")),
                   nproc=int(info.get("NCPU", 0) or 0),
                   images=len(cli.images.list()))
        # 架构：daemon 上报
        out["arch"] = str(info.get("Architecture", "")) or out["arch"]
        # 盘水位：docker 存储根分区 df（有本地镜像时一次性容器探测，~1s；
        # 无镜像=unknown 不阻断——选机硬过滤③留阶段二，这里只是面板展示）
        try:
            root = str(info.get("DockerRootDir", "/var/lib/docker"))
            img = next((im.tags[0] for im in cli.images.list() if im.tags), "")
            if img:
                # 18.09 实测：run(remove=True) 经 ssh 隧道回空日志——改 detach+logs+清理；
                # ⚠ host 路径必须挂进容器才能 df（直接 df 宿主路径=容器内不存在）
                pc = cli.containers.run(img, ["sh", "-c", "df -BG /probe | tail -1"],
                                        volumes={root: {"bind": "/probe", "mode": "ro"}},
                                        detach=True, network_mode="none")
                try:
                    pc.wait(timeout=30)
                    o = pc.logs()
                finally:
                    pc.remove(force=True)
                parts = o.decode().split()
                if len(parts) >= 4:
                    out["disk_free_gb"] = float(parts[3].rstrip("G"))
        except Exception:
            pass
        ex.last_ping_at = datetime.utcnow()
        if ex.status == "cooldown":       # 自检通了提前回池
            ex.status = "online"
            ex.cooldown_until = None
        if out["nproc"] and not ex.is_builtin:
            ex.nproc = out["nproc"]
        db.commit()
    except Exception as e:
        out["reason"] = str(e)[:150]
    return out
