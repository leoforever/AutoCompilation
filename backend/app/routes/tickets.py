"""
工单路由：用户提交编译失败工单 + AI 总结草稿 + admin 列表/详情/改状态 + 对话消息

设计（2026-10-16 报错工单 P0）：
- POST /api/tickets/draft  LLM 总结日志（不入库，返回草稿）
- POST /api/tickets/report  用户提交（已有，改为真接 get_current_user）
- GET  /api/tickets/my      用户查自己工单
- GET  /api/tickets/{id}    工单详情（自己/admin）
- POST /api/tickets/{id}/message   对话消息（自己/admin）
- POST /api/tickets/{id}/close     用户关闭自己 pending/processing 工单
- GET  /api/admin/tickets   admin 列表（4 tab）
- GET  /api/admin/tickets/{id}  admin 详情
- POST /api/admin/tickets/{id}/status  admin 改状态
"""
import os
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime

from app.models import get_db, Ticket, TicketMessage, CompileTask, User, TicketStatus
from app.services.audit import log_audit
from app.schemas import TicketCreate, ApiResponse
from app.dependencies import get_current_user

router = APIRouter()


# ============== 共享：ORM Ticket → dict（避开 Pydantic 对 ORM 关系字段的校验陷阱）==============
def _ticket_to_dict(t: "Ticket", include_messages: bool = False, db: "Session | None" = None) -> dict:
    """把 Ticket ORM 对象序列化成 dict（手动组装，绕开 model_validate(orm) 对 relationship 字段的炸）

    为什么不用 TicketResponse.model_validate(t)（见 §55 Pydantic 500/200 陷阱 #1）：
    - TicketResponse.messages: List[dict] 期望 list[dict]
    - Ticket.messages 是 relationship，拉回是 list[TicketMessage] ORM 对象
    - Pydantic 看到 ORM 对象喂给 dict 字段 → ValidationError → 500

    include_messages=False：list 端点用，避免 N+1（前端 Tickets.vue 也不读 messages）
    include_messages=True：详情端点用，需传 db 拿 user.username
    """
    # §55 #2: Column[str] + enum 双继承时 DB 拉回是裸 str，.value 会 AttributeError → hasattr 兜底
    status_str = str(t.status) if not hasattr(t.status, "value") else t.status.value

    result = {
        "ticket_id": t.ticket_id,
        "task_id": t.task_id,
        "user_id": t.user_id,
        "source": t.source,
        "status": status_str,
        "title": t.title,
        "description": t.description,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
        "messages": [],
    }
    if include_messages:
        # 详情端点：直接读 t.messages（已 eager load，无 N+1）
        result["messages"] = [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "user_id": m.user_id,
                "username": m.user.username if m.user else "已注销",
                # T8: is_read 是"用户已读"语义——用户自己发的消息恒 True，仅 admin 回复会 False
                "is_read": True if m.role != "admin" else bool(m.is_read),
                "kind": m.kind or "chat",
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in t.messages
            if (m.kind or "chat") != "agent_cmd"
        ]
    return result


def _unread_map(db: "Session", ticket_ids: list) -> dict:
    """T8: 批量算每个工单的管理员未读回复数（列表红点用，一次查询防 N+1）"""
    if not ticket_ids:
        return {}
    from sqlalchemy import func as _f
    rows = (db.query(TicketMessage.ticket_id, _f.count(TicketMessage.id))
            .filter(TicketMessage.ticket_id.in_(ticket_ids),
                    TicketMessage.role == "admin",
                    TicketMessage.kind == "chat",   # T8: 指令线(agent_cmd)不算用户未读
                    TicketMessage.is_read == False)  # noqa: E712
            .group_by(TicketMessage.ticket_id).all())
    return {tid: n for tid, n in rows}


# ============== 共享：读 build.log 末尾 ==============
def _tail_log(log_file_path, max_lines: int = 200) -> str:
    """读 build.log 最后 N 行（AI 总结 / 详情页用）"""
    try:
        with open(log_file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:]) if lines else ""
    except Exception as e:
        return f"[日志读取失败: {e}]"


# ============== 1) AI 草稿（不入库）==============
@router.post("/draft", response_model=ApiResponse)
async def draft_ticket_from_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """读 task 失败日志，调用 LLM 生成问题描述草稿（不入库，给前端填到对话框）"""
    task = db.query(CompileTask).filter(CompileTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="只能对自己任务生成草稿")

    # 读失败日志（artifacts/{task_id}/build.log）——Q1b 归一：公共权威不再反向 import 引擎私有
    from app.config import artifacts_dir as _artifacts_dir
    log_path = _artifacts_dir() / task.task_id / "build.log"
    log_text = _tail_log(log_path, 200)
    if not log_text:
        return ApiResponse(data={
            "title": f"[失败] {task.software_name}",
            "description": "（无日志可总结，请手动描述）",
            "ai_summary": None,
            "task_id": task.id,
            "task_status": task.status,
        }, message="无日志，已返回空草稿")

    # 调 LLM（复用 compile_engine 已配的 MiniMax key）
    # agent 现场记录（stage6 落定，与 build.log 同级）；缺失传空串回退旧行为
    notes_path = _artifacts_dir() / task.task_id / "BUILD_NOTES.md"
    build_notes = ""
    try:
        if notes_path.is_file():
            build_notes = notes_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    from app.services.compile_engine import _summarize_log_for_ticket
    title, summary, description = _summarize_log_for_ticket(
        software_name=task.software_name,
        task_status=task.status or "failed",
        log_tail=log_text,
        build_notes=build_notes,
    )

    return ApiResponse(data={
        "title": title,
        "summary": summary,
        "description": description,  # 用户可改
        "task_id": task.id,
        "task_status": task.status,
    })


# ============== 2) 提交工单 ==============
@router.post("/report", response_model=ApiResponse)
async def report_ticket(
    ticket_data: TicketCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """用户提交报错工单（基于 AI 草稿 + 用户编辑）"""
    task = db.query(CompileTask).filter(CompileTask.id == ticket_data.task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="关联任务不存在")
    if task.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="只能为自己的任务提工单")

    ticket = Ticket(
        task_id=ticket_data.task_id,
        user_id=current_user.user_id,
        source=ticket_data.source,
        title=ticket_data.title,
        description=ticket_data.description,
        status=TicketStatus.PENDING,
    )
    db.add(ticket)
    db.flush()  # 先 flush 拿 ticket_id

    # 工单日志口径（2026-10-27 决策）：工单域自包含、全文归任务页——上报时附
    # 末 50 行进 description；磁盘 build.log 优先，缺失回退 DB log_output 尾段
    # （看门狗腰斩单往往只剩 DB 尾巴）；两源皆无/读失败不再静默 pass——
    # description 显式注记，提单人与 admin 当场知道交的是"无日志工单"
    include_log = getattr(ticket_data, "include_log_tail", True)
    _tail_state = "not_requested"
    if include_log:
        from app.config import artifacts_dir as _artifacts_dir  # Q1b 归一：不再反向 import 引擎私有
        tail = ""
        log_path = _artifacts_dir() / task.task_id / "build.log"
        try:
            if log_path.exists():
                tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-50:])
            elif task.log_output:
                tail = "\n".join(task.log_output.splitlines()[-50:])
        except Exception:
            tail = ""
        if tail:
            _tail_state = "attached"
            log_block = f"\n\n--- build.log 末尾 50 行（自动附）---\n```\n{tail}\n```"
            desc = (ticket_data.description or "").strip()
            ticket.description = (desc + log_block) if desc else f"（用户未填描述，自动附 build.log 末 50 行）\n```\n{tail}\n```"
        else:
            _tail_state = "no_log_source"
            note = "\n\n（注：未能附日志尾部——任务目录已清理，或该任务无运行日志记录）"
            ticket.description = ((ticket.description or "") + note).strip() or note.strip()
        db.flush()

    log_audit(db, user_id=current_user.user_id, action="ticket_report", target_type="ticket",
              target_id=ticket.ticket_id, target_key=f"#{ticket.ticket_id}", request=request,
              details=f"task={task.task_id} software={task.software_name} "
                      f"log_tail={_tail_state} source={ticket_data.source}")
    db.commit()
    db.refresh(ticket)
    return ApiResponse(
        data={"ticket_id": ticket.ticket_id, "status": str(ticket.status)},
        message="工单已提交，等待管理员处理",
    )


# ============== 3) 我的工单 ==============
@router.get("/my", response_model=ApiResponse)
async def list_my_tickets(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(Ticket).filter(Ticket.user_id == current_user.user_id)
    if status:
        q = q.filter(Ticket.status == TicketStatus(status))
    tickets = q.order_by(Ticket.created_at.desc()).all()
    unread = _unread_map(db, [t.ticket_id for t in tickets])
    data = []
    for t in tickets:
        d = _ticket_to_dict(t)
        d["admin_unread"] = unread.get(t.ticket_id, 0)
        data.append(d)
    return ApiResponse(data=data)


# ============== 4) 工单详情 ==============
@router.get("/{ticket_id}", response_model=ApiResponse)
async def get_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.user_id != current_user.user_id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="无权查看此工单")

    return ApiResponse(data={
        **_ticket_to_dict(ticket, include_messages=True, db=db),
        "admin_unread": _unread_map(db, [ticket.ticket_id]).get(ticket.ticket_id, 0),
    })


# ============== 4.5) T8: 用户打开对话面板 → admin 回复标已读（红点清零） ==============
@router.post("/{ticket_id}/read", response_model=ApiResponse)
async def mark_ticket_read(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.user_id != current_user.user_id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="无权操作此工单")
    db.query(TicketMessage).filter(
        TicketMessage.ticket_id == ticket_id,
        TicketMessage.role == "admin",
        TicketMessage.kind == "chat",   # 只清对话线
        TicketMessage.is_read == False,  # noqa: E712
    ).update({"is_read": True})
    db.commit()
    return ApiResponse(message="已读")


# ============== 5) 追加消息（用户/admin） ==============
@router.post("/{ticket_id}/message", response_model=ApiResponse)
async def add_ticket_message(
    ticket_id: int,
    content: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """用户/admin 追加消息。role 字段按当前用户角色写"""
    if not content or not content.strip():
        raise HTTPException(status_code=400, detail="消息内容不能为空")
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.user_id != current_user.user_id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="无权回复此工单")
    if ticket.status == TicketStatus.CLOSED:
        raise HTTPException(status_code=400, detail="工单已关闭，无法追加消息")

    role = "admin" if current_user.role == "admin" else "user"
    msg = TicketMessage(
        ticket_id=ticket_id,
        user_id=current_user.user_id,
        role=role,
        content=content.strip(),
        is_read=(role != "admin"),  # T8: admin 回复=用户未读；用户自己发的恒已读
    )
    db.add(msg)
    if role == "user":
        log_audit(db, user_id=current_user.user_id, action="ticket_message_user",
                  target_type="ticket", target_id=ticket_id, target_key=f"#{ticket_id}",
                  request=request, details=f"len={len(content.strip())}")
    # pending 状态下用户回复 = 重新激活，admin 看得到
    if ticket.status == TicketStatus.PENDING and role == "admin":
        ticket.status = TicketStatus.PROCESSING
    db.commit()
    return ApiResponse(data={"id": msg.id}, message="已发送")


# ============== 6) 用户关闭自己的工单 ==============
@router.post("/{ticket_id}/close", response_model=ApiResponse)
async def close_my_ticket(
    ticket_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="只能关闭自己的工单")
    if ticket.status == TicketStatus.CLOSED:
        raise HTTPException(status_code=400, detail="工单已关闭")
    pre_st = ticket.status
    ticket.status = TicketStatus.CLOSED
    log_audit(db, user_id=current_user.user_id, action="ticket_closed", target_type="ticket",
              target_id=ticket_id, target_key=f"#{ticket_id}", request=request,
              details=f"pre_status={str(pre_st)}")
    db.commit()
    # 终态回收 agent 容器（异步；work_dir/checkpoint/记录保留）
    import asyncio
    from app.services.ticket_agent import MANAGER
    async def _recycle():
        try:
            r = await MANAGER.recycle(ticket_id)
            print(f"[closed] tkt={ticket_id} 容器回收: {r}")
        except Exception as e:
            print(f"[closed] tkt={ticket_id} 回收异常: {e}")
    asyncio.create_task(_recycle())
    return ApiResponse(message="工单已关闭")
