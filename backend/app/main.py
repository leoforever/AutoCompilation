"""
FastAPI application entry point for LoongArch Compile Platform

lifespan 启动时会 spawn worker loop 在后台线程，与 FastAPI 同进程：
- BROADCASTER 在同一进程内，WS 订阅者能收到 worker 的推流
- worker 不再需要单独跑 worker.py（旧的独立进程会因 BROADCASTER 不通而失效）
"""
# anthropic shim 必须在 deepagents 引用前生效
try:
    import anthropic
    if not hasattr(anthropic, "OverloadedError"):
        class _OverloadedError(anthropic.APIStatusError):
            pass
        anthropic.OverloadedError = _OverloadedError
except ImportError:
    pass

from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging
import threading
import os


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# T4.2: uvicorn access log 掩码 WS URL 里的 ?token=***（此前 JWT 明文 90+ 次进过 backend.log）
import re as _re
_TOKEN_QS_RE = _re.compile(r"([?&]token=)[^&\s]+")


class _SensitiveQueryFilter(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
            if "token=" in msg:
                record.msg = _TOKEN_QS_RE.sub(r"\1***", msg)
                record.args = ()
        except Exception:
            pass
        return True


logging.getLogger("uvicorn.access").addFilter(_SensitiveQueryFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时 spawn N 个 worker 后台线程；结束时发停止信号

    并发模型：ThreadPoolExecutor(max_workers=MAX_WORKERS)，每个 worker 跑同一段
    worker_loop(body)。worker_id 用 uuid 区分，便于日志和心跳追踪。

    MAX_WORKERS 从环境变量读，不写死。
    """
    from concurrent.futures import ThreadPoolExecutor
    from app.services import task_queue
    from app.services.compile_engine import run_task

    import uuid as _uuid
    import os as _os
    # 默认 8：服务器 32 物理核，8 worker × 4 核/worker 正好打满
    from app.config import get_cfg as _get_cfg
    max_workers = int(_get_cfg("worker.max_workers", 8, env_alias="MAX_WORKERS"))
    if max_workers < 1:
        max_workers = 1
    if max_workers > 32:
        max_workers = 32  # 安全上限，避免误配拖垮系统

    stop_event = threading.Event()

    def _publish_log(worker_id: str, task_id: str):
        """闭包：构造 on_log 把日志通过 BROADCASTER 推到 WS"""
        from app.services.ws_broadcaster import BROADCASTER
        def cb(level: str, msg: str):
            BROADCASTER.publish(task_id, {
                "type": "log", "level": level,
                "msg": msg, "ts": __import__("time").time(),
            })
        return cb

    def worker_loop(worker_id: str):
        _poll_iv = int(_get_cfg("worker.poll_interval", 5))
        """后台线程主循环：dequeue → run_task → publish status"""
        from app.services.ws_broadcaster import BROADCASTER
        from app.services.compile_engine import run_task
        logger.info(f"[lifespan] worker {worker_id} 启动")
        while not stop_event.is_set():
            try:
                task_id = task_queue.dequeue(worker_id)
                if task_id:
                    logger.info(f"[lifespan] worker {worker_id} 抢到任务 {task_id}")
                    BROADCASTER.publish(task_id, {
                        "type": "status", "status": "compiling",
                        "task_id": task_id, "ts": __import__("time").time(),
                    })
                    on_log = _publish_log(worker_id, task_id)
                    # 心跳线程（2026-10-20 修复）：task_queue.heartbeat() 此前是死代码（从未被调用），
                    # updated_at 只靠日志行到达才前进 → torch submodule/pip 等 30min+ 静默长命令期间
                    # DB 时钟冻结 → dequeue 看门狗误判"worker 崩溃"直接标 failed 杀任务
                    # （T-20261020183119-20EF38 实证：stage5 明明活着，1867s 时被看门狗处死）。
                    # 线程活着 = 任务活着；线程死（进程崩/线程炸）→ 心跳停 → 30min 后看门狗回收，语义回归正确
                    _hb_stop = threading.Event()
                    _hb_iv = int(_get_cfg("worker.heartbeat_interval", 60))
                    def _hb_loop(_tid=task_id, _stop=_hb_stop, _iv=None):
                        while not _stop.wait(_iv):
                            try:
                                task_queue.heartbeat(_tid)
                            except Exception:
                                pass  # 单次心跳失败不影响（下次再来）
                    _hb_thread = threading.Thread(target=_hb_loop, kwargs={"_iv": _hb_iv}, daemon=True)
                    _hb_thread.start()
                    try:
                        result = run_task(task_id, on_log=on_log)
                    finally:
                        _hb_stop.set()
                        _hb_thread.join(timeout=5)
                    BROADCASTER.publish(task_id, {
                        "type": "status", "status": result.get("status"),
                        "task_id": task_id, "ts": __import__("time").time(),
                    })
                    logger.info(f"[lifespan] worker {worker_id} 任务 {task_id} 完成: {result.get('status')}")

                stop_event.wait(timeout=_poll_iv)
            except Exception as e:
                logger.exception(f"[lifespan] worker {worker_id} 异常: {e}")
                stop_event.wait(timeout=_poll_iv)
        logger.info(f"[lifespan] worker {worker_id} 退出")

    # 启动 worker 线程池
    logger.info(f"[lifespan] 启动 worker 线程池: max_workers={max_workers}")
    executor = ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="compile-worker",
    )
    futures = []
    for i in range(max_workers):
        wid = f"w-{i}-{_uuid.uuid4().hex[:6]}"
        fut = executor.submit(worker_loop, wid)
        futures.append(fut)

    # 注：reconcile 不在后台跑，改为前端进入 SkillLib 页面时按需触发
    # （见 admin.py /admin/skills/reconcile 接口 + SkillLib.vue onMounted）
    import asyncio

    # A 方案配套：agent 容器对账 —— 启动跑一次（回收 closed 残留/清 stale），之后每小时兜底
    # （内存 _sessions 重启即丢，孤儿回收必须以 docker/DB 为准）
    async def _sweep_loop():
        import asyncio
        from app.services.ticket_agent import MANAGER
        while True:
            try:
                stats = await MANAGER.sweep_orphans()
                if any(stats.values()):
                    logger.info(f"[sweep] 孤儿对账: {stats}")
            except Exception:
                logger.exception("[sweep] 对账异常")
            # 任务对账自愈（2026-10-21 Step 0）：入库断链滞留的 compiling，按磁盘现场恢复
            try:
                from app.services.task_reconciler import reconcile_stuck_tasks
                # 批2-②（2026-10-26）：纯同步 DB 函数裸跑在事件循环上——DB 打嗝即
                # 全站冻结（generate-draft 同族病）；搬进线程池
                rs = await asyncio.to_thread(reconcile_stuck_tasks)
                if any(k != "at" and v for k, v in rs.items()):
                    logger.info(f"[reconcile] 任务对账: {rs}")
            except Exception:
                logger.exception("[reconcile] 任务对账异常")
            # 草稿 job 表 GC：done/error 超 1h 清掉（防长期不重启内存增长）
            try:
                from app.services.skill_draft import _PREVIEW_JOBS
                from app.services.experience_generator import _DRAFT_JOBS
                cutoff = __import__('time').time() - 3600
                for k in [k for k, v in _PREVIEW_JOBS.items()
                          if v.get('status') in ('done', 'error') and v.get('started_at', 0) < cutoff]:
                    _PREVIEW_JOBS.pop(k, None)
                # 批2-乙 的 task 草稿 job 表同款 GC（昨日漏并，今补——内存不涨）
                for k in [k for k, v in _DRAFT_JOBS.items()
                          if v.get('status') in ('done', 'error') and v.get('started_at', 0) < cutoff]:
                    _DRAFT_JOBS.pop(k, None)
            except Exception:
                pass
            await asyncio.sleep(int(_get_cfg("worker.sweep_interval", 3600)))

    _sweep_task = asyncio.create_task(_sweep_loop())

    try:
        yield
    finally:
        _sweep_task.cancel()
        logger.info("[lifespan] 收到关闭信号，停止所有 worker")
        stop_event.set()
        # 等所有 worker 退出（最多 30s）
        import concurrent.futures
        concurrent.futures.wait(futures, timeout=30)
        executor.shutdown(wait=True, cancel_futures=True)


app = FastAPI(
    title="龙芯 LoongArch 智能编译平台 API",
    description="云端公共编译 + 本地私有编译双模式智能编译平台",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS 配置（#4，2026-10-26）：allow_credentials 关闭——本平台认证走 Bearer JWT
# （localStorage 手动塞 Authorization 头），不依赖 Cookie，credentials=True 只有
# "任意站点可跨源带凭据"的反模式风险而无收益。若将来改用 Cookie 会话，必须同时
# 把 allow_origins 收窄到具体部署域（* + credentials 是规范互斥组合）。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 导入路由（延迟导入避免循环依赖）
from app.routes import auth, compile, tickets, experience, admin, notifications, qa, ws
from app.models import init_db
from app.dependencies import require_admin, require_qa_owner

# 初始化数据库
init_db()

app.include_router(auth.router, prefix="/api", tags=["认证"])
app.include_router(compile.router, prefix="/api/compile", tags=["编译任务"])
app.include_router(tickets.router, prefix="/api/tickets", tags=["工单管理"])
app.include_router(experience.router, prefix="/api/experience", tags=["经验管理"])
app.include_router(notifications.router, prefix="/api", tags=["通知"])
app.include_router(qa.router, prefix="/api", tags=["问答台"], dependencies=[Depends(require_qa_owner)])
# 管理员路由组统一角色守卫（端点级 get_current_user 之上再加一道，漏写的也兜住）
app.include_router(admin.router, prefix="/api/admin", tags=["管理员接口"],
                   dependencies=[Depends(require_admin)])
app.include_router(ws.router, tags=["WebSocket"])
