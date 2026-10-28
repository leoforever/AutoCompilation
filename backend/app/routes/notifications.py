"""站内通知读端点（T12）——普通用户可见，user_id 恒取 current_user 无越界面"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from datetime import datetime

from app.models import get_db, Notification, User
from app.schemas import ApiResponse
from app.dependencies import get_current_user

router = APIRouter(prefix="/notifications", tags=["通知"])


@router.get("/unread-count", response_model=ApiResponse)
async def unread_count(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    n = db.query(Notification).filter(
        Notification.user_id == current_user.user_id,
        Notification.read_at.is_(None),
    ).count()
    return ApiResponse(data={"count": n})


@router.get("", response_model=ApiResponse)
async def list_notifications(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (db.query(Notification)
            .filter(Notification.user_id == current_user.user_id)
            .order_by(Notification.id.desc()).limit(limit).all())
    return ApiResponse(data=[{
        "id": r.id, "type": r.type, "title": r.title, "body": r.body,
        "link": r.link,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "unread": r.read_at is None,
    } for r in rows])


@router.post("/read", response_model=ApiResponse)
async def mark_read(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """body: {id: int} 或 {all: true}。只动本人行。"""
    now = datetime.utcnow()
    if body.get("all"):
        q = db.query(Notification).filter(
            Notification.user_id == current_user.user_id,
            Notification.read_at.is_(None))
        n = q.update({Notification.read_at: now}, synchronize_session=False)
    else:
        nid = body.get("id")
        if not isinstance(nid, int):
            raise HTTPException(status_code=422, detail="需要 id 或 all=true")
        n = db.query(Notification).filter(
            Notification.id == nid,
            Notification.user_id == current_user.user_id
        ).update({Notification.read_at: now}, synchronize_session=False)
    db.commit()
    return ApiResponse(data={"marked": n})
