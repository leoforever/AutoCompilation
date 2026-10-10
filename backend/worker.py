"""编译 worker - 独立进程入口

启动：
    cd /root/.openclaw/workspace/compile-platform/backend
    python worker.py

工作循环：
    loop:
        task_id = task_queue.dequeue(worker_id=WID)
        if not task_id: sleep(5); continue
        on_log(level, msg): 写到 DB 同步 + 通过 in-memory broadcaster 推 WS
        compile_engine.run_task(task_id, on_log=cb)
"""
from __future__ import annotations

import os
import sys
import signal
import socket
import threading
import time
import uuid
from pathlib import Path

# 让 worker 能 import app.*
sys.path.insert(0, str(Path(__file__).resolve().parent))

# anthropic shim（与 langgraph_main.py 同）
try:
    import anthropic
    if not hasattr(anthropic, "OverloadedError"):
        class _OverloadedError(anthropic.APIStatusError):
            pass
        anthropic.OverloadedError = _OverloadedError
except ImportError:
    pass


# ========== 配置 ==========
WORKER_ID = f"worker-{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
POLL_INTERVAL = 5  # 秒
HEARTBEAT_INTERVAL = 60  # 秒


# ========== 配置 ==========
WORKER_ID = f"worker-{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
POLL_INTERVAL = 5  # 秒
HEARTBEAT_INTERVAL = 60  # 秒


# ========== 日志回调 ==========
def make_log_callback(task_id: str):
    """worker 内部用的 on_log(level, msg)

    push 到共享的 BROADCASTER，让 WS handler 转发（同进程前提）。
    """
    from app.services.ws_broadcaster import BROADCASTER

    def cb(level: str, msg: str):
        BROADCASTER.publish(task_id, {
            "type": "log",
            "level": level,
            "msg": msg,
            "ts": time.time(),
        })
    return cb


# ========== 主循环 ==========
def main_loop():
    from app.services import task_queue
    from app.services.compile_engine import run_task
    from app.services.ws_broadcaster import BROADCASTER

    running = True

    def stop(*_):
        nonlocal running
        running = False
        print(f"\n[{WORKER_ID}] 收到停止信号，退出中...")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(f"[{WORKER_ID}] worker 启动")
    print(f"  poll_interval = {POLL_INTERVAL}s")
    print(f"  heartbeat_interval = {HEARTBEAT_INTERVAL}s")
    print()

    last_heartbeat = 0.0
    current_task_id = None

    while running:
        try:
            # 1) 抢任务
            task_id = task_queue.dequeue(WORKER_ID)
            if task_id:
                current_task_id = task_id
                BROADCASTER.publish(task_id, {
                    "type": "status",
                    "status": "compiling",
                    "task_id": task_id,
                    "ts": time.time(),
                })
                print(f"[{WORKER_ID}] 抢到任务 {task_id}，开始跑")
                result = run_task(task_id, on_log=make_log_callback(task_id))
                BROADCASTER.publish(task_id, {
                    "type": "status",
                    "status": result.get("status"),
                    "task_id": task_id,
                    "ts": time.time(),
                })
                print(f"[{WORKER_ID}] 任务 {task_id} 完成: {result.get('status')}")
                current_task_id = None
                last_heartbeat = time.time()
                continue

            # 2) 无任务，心跳当前任务
            if current_task_id and time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
                task_queue.heartbeat(current_task_id)
                last_heartbeat = time.time()

            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            stop()
        except Exception as e:
            import traceback
            print(f"[{WORKER_ID}] 主循环异常: {type(e).__name__}: {e}")
            traceback.print_exc()
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main_loop()
