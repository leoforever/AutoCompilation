"""WebSocket broadcaster - 同一进程内 worker→WS 转发（开发用）

生产用 Redis pub/sub 替代。当前实现：worker.py 与 routes/compile.py 必须在同进程。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Dict, List


class WSBroadcaster:
    def __init__(self):
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._lock = threading.Lock()

    def subscribe(self, task_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        # 批2-③（2026-10-26）：记下队列所属事件循环（WS handler 在 uvicorn 主循环
        # 里调本方法）。引擎在 run_in_executor 工作线程 publish，asyncio.Queue 非
        # 线程安全——跨线程 put_nowait 不唤醒循环内挂起的 getter，症状=WS 日志
        # 卡一段后成批涌出（历史"订阅没反应"疑案成分）
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        with self._lock:
            self._subscribers.setdefault(task_id, []).append(q)
        return q

    def unsubscribe(self, task_id: str, q: asyncio.Queue):
        with self._lock:
            if task_id in self._subscribers:
                try:
                    self._subscribers[task_id].remove(q)
                except ValueError:
                    pass
                if not self._subscribers[task_id]:
                    self._subscribers.pop(task_id, None)

    def publish(self, task_id: str, message: dict):
        with self._lock:
            queues = list(self._subscribers.get(task_id, []))
        loop = getattr(self, "_loop", None)
        try:
            cur = asyncio.get_running_loop()
        except RuntimeError:
            cur = None
        for q in queues:
            try:
                if loop is not None and cur is not loop:
                    # 非循环线程 → 投递回循环线程执行（唤醒 getter）
                    loop.call_soon_threadsafe(q.put_nowait, message)
                else:
                    q.put_nowait(message)
            except Exception:
                pass

    def count(self, task_id: str = None) -> int:
        with self._lock:
            if task_id is None:
                return sum(len(q) for q in self._subscribers.values())
            return len(self._subscribers.get(task_id, []))


# 单例 — 同一进程内所有模块共享
BROADCASTER = WSBroadcaster()
