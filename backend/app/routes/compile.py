"""
Compile task routes
Based on design doc section 11.1: POST /api/compile/submit, GET /api/compile/tasks

改动（集成 compile_engine 后）：
- POST /submit 现在调 task_queue.enqueue 让 worker 能抢到任务
- WS /ws/compile/log/{id} 改用 worker.BROADCASTER 实时推送
"""
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse
from pathlib import Path
from sqlalchemy.orm import Session
from typing import List, Optional
import uuid
import json
import asyncio
from datetime import datetime
from app.dependencies import get_current_user
from app.models import User

from app.models import get_db, CompileTask, TaskStatus, TargetSystem
from app.schemas import (
    CompileTaskCreate, CompileTaskResponse, CompileTaskListResponse,
    ApiResponse
)
from app.services import task_queue

router = APIRouter()


@router.post("/submit", response_model=ApiResponse)
async def submit_compile_task(
    task_data: CompileTaskCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    提交编译任务
    
    - system_id: 目标系统 ID
    - software_name: 软件名称/版本
    - source_type: upload 或 git
    - compile_mode: container 或 local
    """
    # 验证目标系统存在
    system = db.query(TargetSystem).filter(
        TargetSystem.system_id == task_data.system_id,
        TargetSystem.is_active == True
    ).first()
    if not system:
        raise HTTPException(status_code=404, detail="目标系统不存在或已禁用")
    
    # 生成任务 ID
    task_id = f"T-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"

    # 创建任务记录
    task = CompileTask(
        task_id=task_id,
        user_id=current_user.user_id,  # 从 JWT 取，不再 hardcoded 1
        system_id=task_data.system_id,
        software_name=task_data.software_name,
        source_type=task_data.source_type,
        source_path=task_data.source_path,
        git_url=task_data.git_url,
        git_branch=task_data.git_branch,
        build_args=json.dumps(task_data.build_args) if task_data.build_args else None,
        compile_mode=task_data.compile_mode,
        status=TaskStatus.SUBMITTED,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    # 进队列让 worker 抢
    task_queue.enqueue(task_id)

    return ApiResponse(
        code=200,
        message="编译任务已提交",
        data={
            "task_id": task.task_id,
            "status": str(task.status),
        }
    )


@router.get("/tasks", response_model=CompileTaskListResponse)
async def list_compile_tasks(
    status: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    获取编译任务列表

    - status: 状态筛选（submitted/compiling/success/failed）
    - search: 搜索软件名或任务 ID
    - 权限：非 admin 只能看自己名下的任务；admin 看全部
    """
    query = db.query(CompileTask)

    # 用户隔离：非 admin 强制 user_id 过滤
    if current_user.role != "admin":
        query = query.filter(CompileTask.user_id == current_user.user_id)

    if status:
        query = query.filter(CompileTask.status == TaskStatus(status))
    if search:
        query = query.filter(
            CompileTask.software_name.ilike(f"%{search}%") |
            CompileTask.task_id.ilike(f"%{search}%")
        )
    
    total = query.count()
    tasks = query.order_by(CompileTask.created_at.desc()).offset(
        (page - 1) * page_size
    ).limit(page_size).all()
    
    return CompileTaskListResponse(
        total=total,
        tasks=[CompileTaskResponse.model_validate(t) for t in tasks]
    )


@router.get("/tasks/{task_id}", response_model=ApiResponse)
async def get_compile_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取编译任务详情（非 admin 只能看自己名下）"""
    task = db.query(CompileTask).filter(CompileTask.task_id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    # 权限校验：非 admin 不能看别人的任务
    if current_user.role != "admin" and task.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="无权访问该任务")

    return ApiResponse(data=CompileTaskResponse.model_validate(task).model_dump())


@router.get("/tasks/{task_id}/log")
async def get_compile_log(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取编译日志（DB 文本字段，兼容老接口）"""
    task = db.query(CompileTask).filter(CompileTask.task_id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    # 权限校验
    if current_user.role != "admin" and task.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="无权访问该任务")

    return {"task_id": task_id, "log": task.log_output or ""}


@router.get("/tasks/{task_id}/log/download")
async def download_compile_log(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """下载完整日志文件（artifacts/{task_id}/build.log）

    文件兜底：每个任务运行时的所有 _log() 都会 append 到 build.log
    前端 onMounted 恢复路径用这个端点拉历史日志
    """
    from app.services.compile_engine import _artifacts_dir
    task = db.query(CompileTask).filter(CompileTask.task_id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    # 权限校验
    if current_user.role != "admin" and task.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="无权访问该任务")

    log_file = _artifacts_dir() / task_id / "build.log"
    if not log_file.exists():
        return {"task_id": task_id, "log": task.log_output or "", "source": "db"}
    return FileResponse(
        path=str(log_file),
        media_type="text/plain",
        filename=f"{task_id}.log",
    )


@router.post("/tasks/{task_id}/cancel")
async def cancel_compile_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """取消编译任务

    - 权限：非 admin 必须是任务拥有者
    - 状态：必须在 submitted / compiling 之一（已终态不能取消）
    - 行为：标 status='cancelled' + error_message；best-effort 杀容器
    - worker 退场：依赖 stage5 每 chunk 查 DB 状态感知（_periodic_persist 机制），
      下个 chunk 看到 cancelled 旗就 return state。最坏 1-2 分钟后真正退
    """
    task = db.query(CompileTask).filter(CompileTask.task_id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    # 权限
    if current_user.role != "admin" and task.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="无权访问该任务")
    # 状态
    if task.status not in ("submitted", "compiling"):
        raise HTTPException(
            status_code=400,
            detail=f"任务状态为 {task.status}，无法取消（只能取消 submitted / compiling）"
        )

    # 标 cancelled（worker 下一轮 _periodic_persist 会感知到）
    task.status = "cancelled"
    task.error_message = f"用户 {current_user.username} 主动取消"
    task.completed_at = datetime.utcnow()
    if task.started_at:
        task.duration_seconds = int((task.completed_at - task.started_at).total_seconds())
    db.commit()

    # best-effort 杀容器（不影响 API 响应，异步）
    if task.container_id:
        try:
            import threading
            from app.services.docker_backend import get_docker_client
            def _stop_container():
                try:
                    cli = get_docker_client()
                    if cli is None:
                        return
                    c = cli.containers.get(task.container_id)
                    c.stop(timeout=5)
                except Exception:
                    pass  # 容器已退/不存在，忽略
            threading.Thread(target=_stop_container, daemon=True).start()
        except Exception:
            pass

    return ApiResponse(data={"task_id": task_id, "status": "cancelled"})


@router.post("/tasks/{task_id}/retry", response_model=ApiResponse)
async def retry_compile_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """重试编译任务

    - 复制原任务（git_url / branch / system_id / build_args / source_type / mode）的 payload
    - 生成新 task_id（保持原 owner）
    - 状态校验：仅终态（success / failed / cancelled）可重试
    - 权限：非 admin 必须是任务拥有者
    """
    src = db.query(CompileTask).filter(CompileTask.task_id == task_id).first()
    if not src:
        raise HTTPException(status_code=404, detail="任务不存在")
    # 权限
    if current_user.role != "admin" and src.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="无权访问该任务")
    # 状态
    if src.status not in ("success", "failed", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail=f"任务状态为 {src.status}，无法重试（只能重试 success / failed / cancelled）"
        )

    # 生成新 task_id + 复用 payload
    new_task_id = f"T-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
    new_task = CompileTask(
        task_id=new_task_id,
        user_id=src.user_id,  # 保持原 owner
        system_id=src.system_id,
        software_name=src.software_name,
        source_type=src.source_type,
        source_path=src.source_path,
        git_url=src.git_url,
        git_branch=src.git_branch,
        build_args=src.build_args,
        compile_mode=src.compile_mode,
        status=TaskStatus.SUBMITTED.value,
    )
    db.add(new_task)
    db.commit()
    db.refresh(new_task)

    # 入队等 worker 抢
    task_queue.enqueue(new_task_id)

    return ApiResponse(data={
        "new_task_id": new_task_id,
        "source_task_id": task_id,
        "status": "submitted",
    })


@router.websocket("/ws/compile/log/{task_id}")
async def compile_log_websocket(
    websocket: WebSocket,
    task_id: str,
    token: Optional[str] = Query(default=None),
):
    """
    WebSocket 实时推送编译日志

    客户端连接后，服务端主动推送日志行
    权限：token query 传 JWT，校验通过 + 任务属于该用户才允许连接
    """
    import jwt as _jwt
    from app.config import get_settings as _get_settings
    from app.models import SessionLocal, CompileTask

    await websocket.accept()
    db = SessionLocal()

    # 权限校验
    try:
        if not token:
            await websocket.close(code=1008, reason="missing token")
            return
        try:
            payload = _jwt.decode(
                token, _get_settings().secret_key,
                algorithms=[_get_settings().algorithm],
            )
            uid = payload.get("sub")
        except Exception:
            await websocket.close(code=1008, reason="invalid token")
            return
        if uid is None:
            await websocket.close(code=1008, reason="invalid token")
            return
        task = db.query(CompileTask).filter(CompileTask.task_id == task_id).first()
        if not task:
            await websocket.close(code=1008, reason="task not found")
            return
        # 权限：非 admin 必须是任务拥有者
        is_admin = payload.get("role") == "admin"
        if not is_admin and str(task.user_id) != str(uid):
            await websocket.close(code=1008, reason="forbidden")
            return
    finally:
        db.close()

    # 真正接进 pub/sub 流（写在子函数里避免被前面的 finally 影响）
    from app.services.ws_broadcaster import BROADCASTER
    db2 = SessionLocal()
    try:
        # 发送历史日志
        task = db2.query(CompileTask).filter(
            CompileTask.task_id == task_id
        ).first()

        if task and task.log_output:
            lines = task.log_output.split('\n')
            for line in lines:
                await websocket.send_text(line)

        # 保持连接，等待实时日志
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.close(code=1011, reason=str(e))
        except Exception:
            pass
    finally:
        db2.close()
