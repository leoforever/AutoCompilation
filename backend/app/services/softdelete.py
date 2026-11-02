"""软删单一权威（admin 删除功能 2026-10-30 拍板：预置 admin 专属闸在路由层，
本模块只管语义）。

行保留 + deleted_at：外键零破坏，引用"已删对象"自然变"查得到但展示面不可见"。
purge 只清磁盘/容器（DB 行恒保留——硬删要拆引用网不值当，且丢历史统计）。
执行链（compile_engine/task_queue/reconciler）**不过滤** deleted_at：删除闸门
拒绝在途对象（submitted/compiling/processing），引擎路径无需感知；加了反而把
取消/终态写回弄断。展示/消费面过滤清单见 routes。
"""
import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import CompileTask, Ticket, TaskStatus

# 活态 = 不可删（与 active_work_gate 同构语义）
TASK_LIVE = (TaskStatus.SUBMITTED.value, TaskStatus.COMPILING.value)
TICKET_LIVE = ("processing",)

_TASK_STEM = re.compile(r"^(T-\d{14}-[0-9A-Z]{6})(-R\d+)?$")


def task_guard(task):
    """None=可操作；否则拒删/不存在话术（端点 raise 用）"""
    if task is None:
        return "任务不存在"
    if task.deleted_at is not None:
        return "任务已删除"
    if task.status in TASK_LIVE:
        return f"任务{task.status}中不可删除，请先到任务页取消或等待完成"
    return None


def ticket_guard(ticket):
    if ticket is None:
        return "工单不存在"
    if ticket.deleted_at is not None:
        return "工单已删除"
    if ticket.status in TICKET_LIVE:
        return "工单处理中不可删除，请先完成或关闭工单"
    return None


def _soft_delete_tasks(db: Session, tasks) -> list:
    now = datetime.utcnow()
    touched = []
    for t in tasks:
        if t is not None and t.deleted_at is None and t.status not in TASK_LIVE:
            t.deleted_at = now
            touched.append(t)
    return touched


def r_siblings(db: Session, task):
    """同 stem 的 R 系合成任务（resolve 产物）。10-27 教训：单独悬着永远成孤儿。"""
    if task.deleted_at is not None:
        return []
    base = _TASK_STEM.match(task.task_id or "")
    if not base or base.group(2):
        return []  # R 任务自身不再递归找 R
    return db.query(CompileTask).filter(
        CompileTask.task_id.like(f"{base.group(1)}-R%"),
        CompileTask.deleted_at.is_(None)).all()


def soft_delete_task(db: Session, task) -> list:
    """任务软删 + 连带同 stem R 系（10-27 孤儿 R 教训）。调用方 commit。
    不连带其上工单——拍板口径（11-02 回退我私自加的"对称级联"）：
    删任务后工单引用保留，详情显示"任务已删除"态；级联方向只有 ticket→task。
    （曾按对称方案上线即引发 500：ticket→task→sibling-tickets 传递闭包把
    Ticket 混进 purge 清单；且"删一张工单连坐同任务其他工单"非用户意图。）"""
    siblings = r_siblings(db, task)
    touched = _soft_delete_tasks(db, [task] + siblings)
    return [t for t in touched if t is not task]


def soft_delete_ticket(db: Session, ticket) -> list:
    """工单软删；级联其任务+R 系。工单离开任务无意义；任务离开工单仍是
    编译记录——方向只有 ticket→task。返回连带任务清单。"""
    ticket.deleted_at = datetime.utcnow()
    task = db.query(CompileTask).filter(CompileTask.id == ticket.task_id).first()
    return soft_delete_task(db, task)
