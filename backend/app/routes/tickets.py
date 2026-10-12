"""
Ticket routes
Based on design doc section 11.1: POST /api/tickets/report, GET /api/tickets/my
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime

from app.models import get_db, Ticket, TicketMessage, CompileTask, User, TicketStatus
from app.schemas import (
    TicketCreate, TicketResponse, ApiResponse
)

router = APIRouter()


@router.post("/report", response_model=ApiResponse)
async def report_ticket(
    ticket_data: TicketCreate,
    db: Session = Depends(get_db),
    current_user_id: int = 1  # TODO: 从 session 获取
):
    """上报问题工单"""
    # 验证任务存在
    task = db.query(CompileTask).filter(CompileTask.id == ticket_data.task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="关联任务不存在")
    
    # 创建工单
    ticket = Ticket(
        task_id=ticket_data.task_id,
        user_id=current_user_id,
        source=ticket_data.source,
        title=ticket_data.title,
        description=ticket_data.description,
        status=TicketStatus.PENDING
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    
    return ApiResponse(
        code=200,
        message="工单已提交",
        data={"ticket_id": ticket.ticket_id, "status": ticket.status.value}
    )


@router.get("/my", response_model=ApiResponse)
async def list_my_tickets(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user_id: int = 1  # TODO: 从 session 获取
):
    """获取我的工单列表"""
    query = db.query(Ticket).filter(Ticket.user_id == current_user_id)
    
    if status:
        query = query.filter(Ticket.status == TicketStatus(status))
    
    tickets = query.order_by(Ticket.created_at.desc()).all()
    
    return ApiResponse(data=[TicketResponse.model_validate(t).model_dump() for t in tickets])


@router.get("/{ticket_id}", response_model=ApiResponse)
async def get_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user_id: int = 1  # TODO: 从 session 获取
):
    """获取工单详情（含对话消息）"""
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    
    # 权限检查：用户只能查看自己的工单，管理员可查看所有
    if ticket.user_id != current_user_id:
        # TODO: 检查管理员权限
        pass
    
    # 获取消息列表
    messages = db.query(TicketMessage).filter(
        TicketMessage.ticket_id == ticket_id
    ).order_by(TicketMessage.created_at.asc()).all()
    
    result = TicketResponse.model_validate(ticket).model_dump()
    result["messages"] = [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat()
        }
        for m in messages
    ]
    
    return ApiResponse(data=result)
