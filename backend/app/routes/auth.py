"""
认证路由：注册、登录、获取当前用户、修改密码
"""
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import os
import jwt
import bcrypt

from app.models import get_db, User
from app.schemas import UserCreate, ApiResponse, TokenResponse, UserOut
from app.config import get_settings
from app.dependencies import get_current_user

router = APIRouter(prefix="/auth", tags=["认证"])

settings = get_settings()


@router.post("/register", response_model=ApiResponse)
async def register(user_data: UserCreate, db: Session = Depends(get_db)):
    """用户注册 — 默认关闭（ALLOW_SELF_REGISTER=1 才开放），账号由管理员在用户管理页创建"""
    if os.environ.get("ALLOW_SELF_REGISTER", "0") != "1":
        raise HTTPException(status_code=403, detail="暂不开放自助注册，请联系管理员创建账号")
    existing = db.query(User).filter(User.username == user_data.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="用户名已被注册")
    
    password_hash = bcrypt.hashpw(
        user_data.password.encode(),
        bcrypt.gensalt(rounds=12)
    ).decode()
    
    user = User(
        username=user_data.username,
        password_hash=password_hash,
        role=user_data.role.value if hasattr(user_data.role, 'value') else str(user_data.role)
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    
    return ApiResponse(data=UserOut.model_validate(user).model_dump())


@router.post("/login", response_model=dict)
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """用户登录，返回JWT token"""
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.deleted_at is not None or not user.password_hash:
        # 软删用户或被恢复但未重置密码的占位账号
        raise HTTPException(status_code=403, detail="账号已注销或未激活，请联系管理员")

    if not bcrypt.checkpw(form_data.password.encode(), user.password_hash.encode()):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被禁用")
    
    user.last_active = datetime.utcnow()
    db.commit()
    
    expire = datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": str(user.user_id),
        "username": user.username,
        "role": user.role,
        "exp": expire
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)
    
    return {
        "code": 200,
        "message": "success",
        "data": {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": settings.access_token_expire_minutes * 60
        },
        "user": UserOut.model_validate(user).model_dump()
    }


@router.get("/me", response_model=ApiResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """获取当前用户信息"""
    return ApiResponse(data=UserOut.model_validate(current_user).model_dump())


@router.post("/change-password", response_model=ApiResponse)
async def change_password(
    old_password: str,
    new_password: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """修改密码"""
    if not bcrypt.checkpw(old_password.encode(), current_user.password_hash.encode()):
        raise HTTPException(status_code=400, detail="原密码错误")
    
    current_user.password_hash = bcrypt.hashpw(
        new_password.encode(),
        bcrypt.gensalt(rounds=12)
    ).decode()
    db.commit()
    
    return ApiResponse(message="密码修改成功")
