"""
Dependencies for dependency injection
"""
from fastapi import Depends, HTTPException
from fastapi.security.http import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
import jwt

from app.models import get_db, User
from app.config import get_settings

security = HTTPBearer()
settings = get_settings()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    """获取当前登录用户"""
    token = credentials.credentials
    
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="无效令牌")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="令牌已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="无效令牌")
    
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被禁用")
    
    return user


def require_qa_owner(current_user: User = Depends(get_current_user)) -> User:
    """问答台试用期闸门（2026-10-30 拍板）：功能未完善，仅预置 admin 账号可用。
    放开=main.py 挂载处换回 Depends(require_admin)，一行。"""
    if current_user.username != "admin":
        raise HTTPException(status_code=403, detail="问答台试用阶段，暂未开放")
    return current_user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """要求管理员权限"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return current_user


def log_audit(
    action: str,
    target_type: str = None,
    target_id: int = None,
    details: dict = None
):
    """记录操作日志（异步）"""
    from app.models import AuditLog, get_db
    # TODO: 实现异步日志记录
    pass
