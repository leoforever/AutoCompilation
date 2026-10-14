"""
Compile task routes
Based on design doc section 11.1: POST /api/compile/submit, GET /api/compile/tasks

改动（集成 compile_engine 后）：
- POST /submit 现在调 task_queue.enqueue 让 worker 能抢到任务
- WS /ws/compile/log/{id} 改用 worker.BROADCASTER 实时推送
- POST /upload 新增：上传源码包 + 创建 task（source_type=upload，git 路径不变）
"""
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query, UploadFile, File, Form
from fastapi.responses import FileResponse
from pathlib import Path
from sqlalchemy.orm import Session
from typing import List, Optional
import uuid
import json
import asyncio
import re
import os
from datetime import datetime
from app.dependencies import get_current_user
from app.models import User

from app.models import get_db, CompileTask, TaskStatus, TargetSystem
from app.schemas import (
    CompileTaskCreate, CompileTaskResponse, CompileTaskListResponse,
    ApiResponse
)
from app.services import task_queue
from app.config import get_settings

router = APIRouter()


# 上传源码包允许的扩展名（白名单）
_ALLOWED_EXT = {".tar.gz", ".zip", ".tgz"}
_ALLOWED_NAME_RE = re.compile(r"^[\w\-. ]+\.(tar\.gz|zip|tgz)$", re.IGNORECASE)


def _system_names_for_tasks(db: Session, system_ids: list[int]) -> dict[int, str]:
    """批量取 system_id → name 映射（一次查询，避免 N+1）"""
    if not system_ids:
        return {}
    rows = db.query(TargetSystem.system_id, TargetSystem.name).filter(
        TargetSystem.system_id.in_(set(system_ids))
    ).all()
    return {sid: name for sid, name in rows}


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


@router.post("/upload", response_model=ApiResponse)
async def upload_and_submit(
    file: UploadFile = File(..., description=".tar.gz / .zip / .tgz 源码包"),
    system_id: int = Form(...),
    software_name: str = Form(...),
    git_branch: str = Form("main"),
    compile_mode: str = Form("container"),
    build_args: Optional[str] = Form(None, description='JSON 字符串，如 {"KEY":"VAL"}'),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    上传源码包 + 提交编译任务

    - multipart/form-data：file + system_id + software_name + 可选 git_branch/compile_mode/build_args
    - 文件保存到 artifacts/sources/{task_id}.tar.gz（实际扩展名按文件原样）
    - worker stage4a 会按 source_type='upload' 解压到容器 /work/src

    与 POST /submit (git 路径) 是并行路径，互不影响
    """
    # 1. 校验文件名 + 扩展名
    original_name = file.filename or "upload.bin"
    if not _ALLOWED_NAME_RE.match(original_name):
        raise HTTPException(
            status_code=400,
            detail=f"不支持的源码包格式：{original_name}（仅接受 .tar.gz / .zip / .tgz）",
        )

    # 2. 校验目标系统
    system = db.query(TargetSystem).filter(
        TargetSystem.system_id == system_id,
        TargetSystem.is_active == True,
    ).first()
    if not system:
        raise HTTPException(status_code=404, detail="目标系统不存在或已禁用")

    # 3. 校验 compile_mode
    if compile_mode not in ("container", "local"):
        raise HTTPException(status_code=400, detail="compile_mode 必须是 .container 或 .local")

    # 4. 生成 task_id + 写文件到 artifacts/sources/
    settings = get_settings()
    artifact_dir = Path(os.environ.get("ARTIFACTS_DIR") or settings.artifact_dir)
    if not artifact_dir.is_absolute():
        artifact_dir = Path("/root/.openclaw/workspace/compile-platform/backend").parent / artifact_dir
    sources_dir = artifact_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)

    task_id = f"T-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"

    # 用 task_id 命名避免路径冲突（保留原扩展名）
    # 注意 .tar.gz 有两个点，Path.suffix 只取最后一个（.gz），会丢 .tar
    name_lower = original_name.lower()
    if name_lower.endswith(".tar.gz"):
        suffix = ".tar.gz"
    elif name_lower.endswith(".tgz"):
        suffix = ".tar.gz"  # 内部统一存为 .tar.gz
    else:
        suffix = Path(original_name).suffix.lower()  # .zip 等
    stored_name = f"{task_id}{suffix}"
    stored_path = sources_dir / stored_name

    # 写文件（限制大小）
    max_size = int(os.environ.get("UPLOAD_MAX_SIZE", settings.max_upload_size))
    bytes_written = 0
    try:
        with open(stored_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)  # 1MB chunks
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > max_size:
                    # 清理已写部分
                    f.close()
                    stored_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"源码包过大：>{max_size // (1024*1024)} MB",
                    )
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        stored_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"保存源码包失败：{e}")

    # 5. 解析 build_args
    parsed_build_args = None
    if build_args:
        try:
            parsed_build_args = json.loads(build_args)
        except json.JSONDecodeError:
            stored_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="build_args 必须是合法 JSON")

    # 6. 创建 CompileTask 记录
    try:
        task = CompileTask(
            task_id=task_id,
            user_id=current_user.user_id,
            system_id=system_id,
            software_name=software_name,
            source_type="upload",
            source_path=str(stored_path),
            git_url=None,
            git_branch=git_branch,
            build_args=json.dumps(parsed_build_args) if parsed_build_args else None,
            compile_mode=compile_mode,
            status=TaskStatus.SUBMITTED,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
    except Exception as e:
        # DB 失败回滚 + 删文件
        db.rollback()
        stored_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"创建任务失败：{e}")

    # 7. 入队列让 worker 抢
    task_queue.enqueue(task_id)

    return ApiResponse(
        code=200,
        message="源码包已上传，编译任务已提交",
        data={
            "task_id": task.task_id,
            "status": str(task.status),
            "source_path": str(stored_path),
            "bytes": bytes_written,
        },
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

    # 批量取 system_name（一次 join 查所有 task 的 system_id 对应的 name）
    sys_names = _system_names_for_tasks(db, [t.system_id for t in tasks])

    return CompileTaskListResponse(
        total=total,
        tasks=[
            CompileTaskResponse(
                system_name=sys_names.get(t.system_id),
                **CompileTaskResponse.model_validate(t).model_dump(exclude={"system_name"}),
            )
            for t in tasks
        ],
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

    sys_names = _system_names_for_tasks(db, [task.system_id])
    return ApiResponse(data=CompileTaskResponse(
        system_name=sys_names.get(task.system_id),
        **CompileTaskResponse.model_validate(task).model_dump(exclude={"system_name"}),
    ).model_dump())


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


@router.get("/tasks/{task_id}/artifacts/download")
async def download_artifacts_bundle(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """下载全部产物（一个 .tar.gz 包）

    优先级：
      1. 优先用 stage6 预打的 artifacts/{task_id}.tar.gz（CPU 0 成本，瞬时返）
      2. 缺失则实时打包（兜底，stage6 失败时也能下载）
    """
    from app.services.compile_engine import _artifacts_dir

    task = db.query(CompileTask).filter(CompileTask.task_id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if current_user.role != "admin" and task.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="无权访问该任务")

    artifacts_root = _artifacts_dir()
    bundle_path = artifacts_root / f"{task_id}.tar.gz"

    if not bundle_path.exists():
        # 兜底：实时打包（stage6 失败时也支持下载）
        artifacts_dir = artifacts_root / task_id
        if not artifacts_dir.exists() or not any(artifacts_dir.iterdir()):
            raise HTTPException(
                status_code=404,
                detail=f"产物不存在（任务 {task_id} 未成功或产物已被清理）",
            )
        import tarfile
        with tarfile.open(bundle_path, "w:gz") as tar:
            tar.add(artifacts_dir, arcname=artifacts_dir.name)

    return FileResponse(
        path=str(bundle_path),
        media_type="application/gzip",
        filename=f"{task_id}-artifacts.tar.gz",
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
