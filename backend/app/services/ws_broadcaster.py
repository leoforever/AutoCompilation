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
        for q in queues:
            try:
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
