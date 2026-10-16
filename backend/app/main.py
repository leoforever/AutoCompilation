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
from fastapi import FastAPI
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
    max_workers = int(_os.environ.get("MAX_WORKERS", "8"))
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
                    result = run_task(task_id, on_log=on_log)
                    BROADCASTER.publish(task_id, {
                        "type": "status", "status": result.get("status"),
                        "task_id": task_id, "ts": __import__("time").time(),
                    })
                    logger.info(f"[lifespan] worker {worker_id} 任务 {task_id} 完成: {result.get('status')}")

                stop_event.wait(timeout=5)
            except Exception as e:
                logger.exception(f"[lifespan] worker {worker_id} 异常: {e}")
                stop_event.wait(timeout=5)
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

    try:
        yield
    finally:
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

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 导入路由（延迟导入避免循环依赖）
from app.routes import auth, compile, tickets, experience, admin, ws
from app.models import init_db

# 初始化数据库
init_db()

app.include_router(auth.router, prefix="/api", tags=["认证"])
app.include_router(compile.router, prefix="/api/compile", tags=["编译任务"])
app.include_router(tickets.router, prefix="/api/tickets", tags=["工单管理"])
app.include_router(experience.router, prefix="/api/experience", tags=["经验管理"])
app.include_router(admin.router, prefix="/api/admin", tags=["管理员接口"])
app.include_router(ws.router, tags=["WebSocket"])
