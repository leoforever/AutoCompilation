"""站内通知唯一写口（T12，2026-10-28 →references/notify-design-20261028.md）

规则（与 log_audit 同气质）：
- best-effort：吞错只 warn，提醒失败绝不阻断业务；不自行 commit，挂调用方事务
- 自发排除：actor_user_id == 受众时不发（自己取消/自己审批/自己回复不通知自己）
- 合并语义：type=ticket_reply 且同工单同受众存在**未读**通知时，标题 ×N 计数
  +1、刷新 body，而非插新行（防 admin 连发刷屏）
- naive UTC 显式（时区纪律）
"""
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

_COUNT_RE = re.compile(r"（共 (\d+) 条）$")


def notify(db, *, user_id: int, type: str, title: str, body: str = "",
           link: str = "", actor_user_id: "int | None" = None,
           merge_key: "str | None" = None) -> None:
    if actor_user_id is not None and actor_user_id == user_id:
        return  # 自发排除
    try:
        from app.models import Notification
        if type == "ticket_reply" and merge_key:
            # 合并：该受众该工单最近一条未读 ticket_reply → 计数 +1
            exist = (db.query(Notification)
                       .filter(Notification.user_id == user_id,
                               Notification.type == "ticket_reply",
                               Notification.link == merge_key,
                               Notification.read_at.is_(None))
                       .order_by(Notification.id.desc()).first())
            if exist:
                m = _COUNT_RE.search(exist.title)
                n = (int(m.group(1)) if m else 1) + 1
                base = _COUNT_RE.sub("", exist.title)
                exist.title = f"{base}（共 {n} 条）"[:200]
                exist.body = body[:500] if body else exist.body
                return
        db.add(Notification(
            user_id=user_id, type=type,
            title=title[:200], body=(body or "")[:2000],
            link=link[:200], created_at=datetime.utcnow(),
        ))
    except Exception as e:
        logger.warning("[notify] 写入失败 type=%s user=%s: %s", type, user_id, e)


def notify_task_terminal(db, task, status: str, err: str = "") -> None:
    """任务终态通知（task_done）——受众=提交人。四个终态口子统一调这里：
    write_terminal / cancel（自发排除兜住）、看门狗、reconciler 恢复。"""
    try:
        if not task or not task.user_id:
            return
        icon = {"success": "编译成功", "cancelled": "已取消"}.get(status, "编译失败")
        notify(db, user_id=task.user_id, type="task_done",
               title=f"[{icon}] {task.software_name}",
               body=(err or "")[:500],
               link=f"/tasks?focus={task.task_id}")
    except Exception as e:
        logger.warning("[notify] 任务终态通知失败 %s: %s", getattr(task, "task_id", "?"), e)
