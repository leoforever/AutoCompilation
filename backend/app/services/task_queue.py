"""编译任务队列 - 基于 DB 轮询的简单实现

设计：
- 入队：把 submitted 状态的 CompileTask 视为"在队列里"
- 出队：SELECT ... FOR UPDATE / BEGIN IMMEDIATE 抢占
- 心跳：worker 每 N 秒更新 claimed_at；超过 3 分钟未心跳则视为死亡可重新抢占

依赖 spec 5.1: 状态机 submitted → compiling → success/failed
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.models import CompileTask, TaskStatus, SessionLocal


# 任务心跳超时（秒）
# 多 worker 并发下，单 worker 跑大编译时不会主动发心跳，所以放宽到 30 分钟。
# 真正死掉的 worker 留下的任务，由"超时未心跳的 compiling 也可抢"分支回收。
from app.config import get_cfg as _get_cfg
# 2026-10-23 配置链修通：env WORKER_HEARTBEAT_TIMEOUT / WORKER_HEARTBEAT_SECONDS > yaml worker.heartbeat_timeout > 1800
HEARTBEAT_TIMEOUT = _get_cfg("worker.heartbeat_timeout", 1800, env_alias=["WORKER_HEARTBEAT_TIMEOUT", "WORKER_HEARTBEAT_SECONDS"])


def enqueue(task_id: str) -> bool:
    """标记任务进入 submitted 状态（其实 routes/compile.py 入库时就设置了）

    这里是个幂等操作：把"提交后还没 worker 抢的"任务标记为可被消费
    """
    db = SessionLocal()
    try:
        task = db.query(CompileTask).filter(CompileTask.task_id == task_id).first()
        if task and task.status == TaskStatus.SUBMITTED.value:
            return True
        return False
    finally:
        db.close()


def dequeue(worker_id: str) -> Optional[str]:
    """抢占一个 submitted 任务（超时回收的 compiling 也可抢）

    多 worker 并发下必须用 SELECT ... FOR UPDATE SKIP LOCKED 原子抢占：
      - PostgreSQL: 行级锁，被锁的行其他 worker 看不见
      - SQLite: SQLAlchemy 自动忽略 (no-op)，单线程够用

    返回 task_id 或 None
    """
    db = SessionLocal()
    try:
        # 1) 抢占 submitted（优先）
        # SKIP LOCKED 防止多个 worker 抢到同一行
        task = db.query(CompileTask).filter(
            CompileTask.status == TaskStatus.SUBMITTED.value
        ).with_for_update(skip_locked=True).order_by(
            CompileTask.created_at.asc()
        ).first()

        if task is None:
            # 2) 抢占超时未心跳的 compiling（崩溃 worker 留下的）
            # 教训：超时直接标 failed + last_error，不让新 worker 接力跑一个
            # 不知道啥状态的任务（旧 worker 死时 stage 可能在 container exec / agent.stream）
            threshold = datetime.utcnow() - timedelta(seconds=HEARTBEAT_TIMEOUT)
            stale = db.query(CompileTask).filter(
                CompileTask.status == TaskStatus.COMPILING.value,
            ).with_for_update(skip_locked=True).order_by(
                CompileTask.updated_at.asc()
            ).first()
            if stale and stale.updated_at and stale.updated_at < threshold:
                # 看门狗分流（2026-10-21 Step 0 教训 223BFD）：
                # "写库断了"（有 .persist_error 哨兵）≠ "进程死了"——前者留给 task_reconciler
                # 按磁盘现场恢复，绝不能再补刀"worker 崩溃"；后者才标 failed。
                has_sentinel = False
                try:
                    from pathlib import Path
                    from app.services.compile_engine import _artifacts_dir
                    if stale.task_id:
                        has_sentinel = (_artifacts_dir() / stale.task_id / ".persist_error").is_file()
                except Exception:
                    pass
                if has_sentinel:
                    pass  # 不动 status：交给下一轮对账自愈（RECONCILE_AUTO）
                else:
                    stale.status = TaskStatus.FAILED.value
                    stale.error_message = (
                        f"任务超时无心跳（{HEARTBEAT_TIMEOUT}s 无更新），"
                        f"worker 崩溃，自动标记失败"
                    )
                    from app.services.notify import notify_task_terminal
                    notify_task_terminal(db, stale, "failed", stale.error_message)
                    db.commit()
                # 不返回这个 task，让 worker 继续 dequeue 新任务
                task = None
            else:
                task = None

        if task is None:
            return None

        task.status = TaskStatus.COMPILING.value
        task.started_at = task.started_at or datetime.utcnow()
        task.updated_at = datetime.utcnow()
        db.commit()
        return task.task_id
    except OperationalError:
        db.rollback()
        return None
    except Exception:
        db.rollback()
        return None
    finally:
        db.close()


def heartbeat(task_id: str):
    """worker 跑任务期间周期性调用，确保不会因超时被误判死亡"""
    db = SessionLocal()
    try:
        task = db.query(CompileTask).filter(CompileTask.task_id == task_id).first()
        if task:
            task.updated_at = datetime.utcnow()
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def get_queue_stats() -> dict:
    """监控用：各状态任务数"""
    db = SessionLocal()
    try:
        rows = db.execute(text(
            "SELECT status, COUNT(*) FROM compile_tasks GROUP BY status"
        )).fetchall()
        return {row[0]: row[1] for row in rows}
    except Exception:
        return {}
    finally:
        db.close()
