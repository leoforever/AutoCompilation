"""
WebSocket routes for real-time compile log streaming

接入 worker.BROADCASTER（同进程）：worker 推送 → async queue → WS 转发。

注意：
- 当前实现依赖 worker.py 与 fastapi uvicorn 在**同进程**内
- 生产环境应改为 Redis pub/sub，让 worker / api 可跨进程
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
import asyncio
import json
import logging
from typing import Optional

import jwt as _jwt

from app.services.ws_broadcaster import BROADCASTER
from app.config import get_settings
from app.models import SessionLocal, CompileTask

router = APIRouter()
logger = logging.getLogger(__name__)


@router.websocket("/ws/compile/log/{task_id}")
async def compile_log_websocket(
    websocket: WebSocket,
    task_id: str,
    token: Optional[str] = Query(default=None),
):
    """实时推送编译日志：worker.BROADCASTER.publish() → 这里转发

    权限：token query 传 JWT，校验通过 + 任务属于该用户才允许订阅
    """
    # 权限校验（在 accept 前关闭，避免 403 后还留个空连接）
    if not token:
        await websocket.close(code=1008, reason="missing token")
        return
    try:
        settings = get_settings()
        payload = _jwt.decode(
            token, settings.secret_key, algorithms=[settings.algorithm],
        )
        uid = payload.get("sub")
        role = payload.get("role")
    except Exception as e:
        await websocket.close(code=1008, reason=f"invalid token: {e}")
        return
    if uid is None:
        await websocket.close(code=1008, reason="invalid token")
        return

    db = SessionLocal()
    try:
        task = db.query(CompileTask).filter(CompileTask.task_id == task_id).first()
        if not task:
            await websocket.close(code=1008, reason="task not found")
            return
        # 权限：非 admin 必须是任务拥有者
        if role != "admin" and str(task.user_id) != str(uid):
            await websocket.close(code=1008, reason="forbidden")
            return
    finally:
        db.close()

    await websocket.accept()
    queue = BROADCASTER.subscribe(task_id)
    logger.info(f"[WS] client connected for {task_id}, total subs={BROADCASTER.count(task_id)}")
    try:
        # 先发一条 hello，告诉客户端连接已建立
        await websocket.send_text(json.dumps({
            "type": "connected",
            "task_id": task_id,
        }, ensure_ascii=False))

        # 同时启动两个协程：监听 client 消息（cancel 等）+ 监听 broadcaster
        sender_task = asyncio.create_task(_pump(task_id, queue, websocket))
        try:
            while True:
                # 阻塞等 client 心跳 / 命令
                msg = await websocket.receive_text()
                # 现在不处理任何客户端命令；保留接口
                logger.debug(f"[WS] recv from {task_id}: {msg[:80]}")
        except WebSocketDisconnect:
            pass
        finally:
            sender_task.cancel()
            try:
                await sender_task
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        BROADCASTER.unsubscribe(task_id, queue)
        logger.info(f"[WS] client disconnected from {task_id}, total subs={BROADCASTER.count(task_id)}")


async def _pump(task_id: str, queue: asyncio.Queue, websocket: WebSocket):
    """从 BROADCASTER 队列读消息，转发到 WS"""
    try:
        while True:
            msg = await queue.get()
            await websocket.send_text(json.dumps(msg, ensure_ascii=False))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[WS] pump error for {task_id}: {e}")


# 兼容老 API（被外部代码可能引用）
async def push_log_to_task(task_id: str, log_line: str):
    BROADCASTER.publish(task_id, {"type": "log", "msg": log_line})
