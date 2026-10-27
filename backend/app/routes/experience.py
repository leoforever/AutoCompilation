"""
Experience and Skill routes
Based on design doc section 11.1: POST /api/experience/submit, GET /api/experience/my
"""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional

from app.models import get_db, Experience, Skill, CompileTask, TargetSystem, ExperienceStatus, User
from app.schemas import (
    ExperienceSubmit, ExperienceResponse, SkillCreate, SkillUpdate, SkillResponse,
    ApiResponse
)
from app.services.skill_loader import write_skill_to_disk, read_skill_from_disk, remove_skill_from_disk
from app.dependencies import get_current_user, require_admin
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/submit", response_model=ApiResponse)
async def submit_experience(
    exp_data: ExperienceSubmit,
    db: Session = Depends(get_db),
    # T9批1: 原 `current_user_id: int = 1` 默认参数——token 被无视，全员经验记在 admin 名下
    current_user: User = Depends(get_current_user)
):
    """提交编译经验"""
    # 验证任务存在
    task = db.query(CompileTask).filter(CompileTask.id == exp_data.task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="关联任务不存在")
    # 允许 success 和 failed 状态（CompileTask.status 是 String(20)，存的是 str）
    # failed 经验比成功经验更宝贵（告诉后续用户"别这样编"）
    if task.status not in ("success", "failed"):
        raise HTTPException(status_code=400, detail="只有已完成（成功或失败）的编译任务才能提交经验")
    
    # 检查是否已有 pending 中的经验（submitted / under_review）
    # 允许 rejected → 用户可修改后重新提
    # 允许 superseded → 已被取代，旧 approve 行被清掉后用户再提
    # 阻止 approved / synced → 当前 Skill 还在 DB + 磁盘，重复提等于覆盖；走 admin 改
    existing = db.query(Experience).filter(
        Experience.task_id == exp_data.task_id,
        Experience.status.in_([
            ExperienceStatus.SUBMITTED.value,
            ExperienceStatus.UNDER_REVIEW.value,
            ExperienceStatus.APPROVED.value,
            ExperienceStatus.SYNCED.value,
        ]),
    ).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"该任务已有 {existing.status} 经验（eid={existing.experience_id}, "
                f"skill={existing.skill_name}），请勿重复提交。"
                f"如需更新 Skill 内容，请到「经验库管理」修改对应 SKILL.md 后重新提交"
            ),
        )
    
    # 创建经验记录
    experience = Experience(
        task_id=exp_data.task_id,
        user_id=current_user.user_id,
        skill_name=exp_data.skill_name,
        skill_type=exp_data.skill_type,
        system_id=exp_data.system_id,
        status=ExperienceStatus.SUBMITTED,
        skill_content=exp_data.skill_content
    )
    db.add(experience)
    db.commit()
    db.refresh(experience)
    
    return ApiResponse(
        code=200,
        message="经验已提交，等待审核",
        data=ExperienceResponse.model_validate(experience).model_dump()
    )


@router.get("/my", response_model=ApiResponse)
async def list_my_experiences(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取我的经验提交记录"""
    experiences = db.query(Experience).filter(
        Experience.user_id == current_user.user_id
    ).order_by(Experience.submitted_at.desc()).all()
    
    return ApiResponse(data=[
        ExperienceResponse.model_validate(e).model_dump()
        for e in experiences
    ])


@router.get("/pending", response_model=ApiResponse)
async def list_pending_experiences(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    """获取待审核经验列表（管理员）"""
    experiences = db.query(Experience).filter(
        Experience.status.in_([ExperienceStatus.SUBMITTED, ExperienceStatus.UNDER_REVIEW])
    ).order_by(Experience.submitted_at.desc()).all()
    
    return ApiResponse(data=[
        ExperienceResponse.model_validate(e).model_dump()
        for e in experiences
    ])


@router.post("/{experience_id}/approve", response_model=ApiResponse)
async def approve_experience(
    experience_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    """通过经验审核

    T9批1 时序（原"先 commit 再写盘，写盘失败 rollback 是 no-op"= 假回滚，DB/磁盘永久分叉）：
    1. 快照磁盘旧内容
    2. 写盘（失败 → DB 尚未 commit，rollback 丢弃全部改动，天然回滚）
    3. commit DB（失败 → 把磁盘恢复快照/删除新建，防反向不一致）
    """
    experience = db.query(Experience).filter(Experience.experience_id == experience_id).first()
    if not experience:
        raise HTTPException(status_code=404, detail="经验不存在")

    # 清掉同名 skill 的旧 approved/synced 行（保证"approved 唯一对应有效 Skill"）
    # 这些行的内容已被新 approve 覆盖，留着会脏数据 + 触发虚假 400
    old_approved = db.query(Experience).filter(
        Experience.skill_name == experience.skill_name,
        Experience.status.in_([ExperienceStatus.APPROVED.value, ExperienceStatus.SYNCED.value]),
        Experience.experience_id != experience_id,
    ).all()
    for old in old_approved:
        old.status = "superseded"
        old.reviewed_at = datetime.utcnow()
    if old_approved:
        logger.info("[approve_experience] skill_name=%s 标 superseded %d 行",
                    experience.skill_name, len(old_approved))

    experience.status = ExperienceStatus.APPROVED
    experience.reviewed_at = datetime.utcnow()
    experience.reviewer_id = current_user.user_id

    # 创建或更新 Skill（DB）——全部改动保持未 commit，写盘成功才提交
    skill = db.query(Skill).filter(Skill.name == experience.skill_name).first()
    version = f"v{datetime.now().strftime('%Y%m%d')}"
    if skill:
        skill.skill_content = experience.skill_content
        skill.version = version
        # 已存在的不改 source（保留首次入库的来源）
    else:
        skill = Skill(
            name=experience.skill_name,
            type=experience.skill_type,
            system_ids=json.dumps([experience.system_id]),
            skill_content=experience.skill_content,
            version=version,
            source="experience",  # 用户提交经验通过审核入库
        )
        db.add(skill)

    disk_type, disk_name = str(experience.skill_type), str(experience.skill_name)
    old_disk = read_skill_from_disk(disk_type, disk_name)  # None=磁盘此前无该 skill

    try:
        write_skill_to_disk(skill_type=disk_type, skill_name=disk_name,
                            content=str(experience.skill_content or ""), version=version)
    except (ValueError, RuntimeError) as e:
        db.rollback()  # 此刻事务尚未 commit——真回滚（supersede/APPROVED/Skill 行全部丢弃）
        raise HTTPException(status_code=500, detail=f"SKILL.md 写盘失败（DB 已回滚）: {e}")

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        # commit 炸了盘已写——尽力恢复快照（失败只告警，磁盘新内容无害于 stage5）
        try:
            if old_disk is None:
                remove_skill_from_disk(disk_type, disk_name)
            else:
                write_skill_to_disk(disk_type, disk_name, old_disk)
        except Exception as de:
            logger.error("[approve] commit 失败后磁盘恢复也失败: %s", de)
        raise HTTPException(status_code=500, detail=f"DB 写入失败: {e}")

    return ApiResponse(message="经验已通过审核，SKILL.md 已同步到磁盘")


@router.post("/{experience_id}/reject", response_model=ApiResponse)
async def reject_experience(
    experience_id: int,
    reason: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    """驳回经验审核"""
    experience = db.query(Experience).filter(Experience.experience_id == experience_id).first()
    if not experience:
        raise HTTPException(status_code=404, detail="经验不存在")
    
    experience.status = ExperienceStatus.REJECTED
    experience.rejection_reason = reason
    experience.reviewed_at = datetime.utcnow()
    experience.reviewer_id = current_user.user_id

    db.commit()

    return ApiResponse(message="经验已驳回")


@router.post("/generate-draft", response_model=ApiResponse)
async def generate_experience_draft(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """手动触发：根据已完成 task 生成经验草稿（AI 后置总结）

    Body: { "task_id": int }
    - 仅 SUCCESS / FAILED 状态可触发（其它状态直接 400）
    - 调用方必须为 task 所有者或管理员，否则 403
    - 草稿不入 DB，前端拿到后由用户编辑再走 /experience/submit
    - LLM 配置复用 compile_engine：AI_API_KEY / AI_BASE_URL / AI_MODEL
    """
    from app.services.experience_generator import generate_draft  # noqa: F401 终态校验等语义复用

    task_id = body.get("task_id") if isinstance(body, dict) else None
    if not isinstance(task_id, int):
        raise HTTPException(status_code=422, detail="body.task_id 必须是整数")

    task = db.query(CompileTask).filter(CompileTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="task 不存在")
    if current_user.role != "admin" and task.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="无权访问该任务")

    # 批2-乙（2026-10-26）：不再同步等 LLM（1~4min 占死事件循环）——
    # 起后台线程秒回，前端轮询 /generate-draft/status。前置校验（存在/终态/属主）
    # （存在/属主）在本请求内；终态校验在线程内 generate_draft 兜（error 经 status 透传）。
    from app.services.experience_generator import start_draft_job
    from app.models import SessionLocal
    job = start_draft_job(SessionLocal, task_id)
    return ApiResponse(
        data={"status": job["status"]},
        message="草稿生成中" if job["status"] == "running" else "生成任务已存在",
    )


@router.get("/generate-draft/stream")
async def generate_draft_stream(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SSE 终态推送（2026-10-26 方案 A）：人盯着等时的快通道，替代 3s 轮询空窗。
    骨架照 /agent/stream（thread→loop 投递用批2-③ 的 call_soon_threadsafe 姿势），
    但草稿无 token 流——SSE 退化为"挂着的单次推送"：done/error 一个终态事件即关流。

    事件（data: {"type": ...}）：
      done(result) / error(message) / none(未起 job，前端应改 POST) /
      timeout(超 6min，前端降级轮询)。
    进来先查终态：done/error 直接送——断线重连、关窗再来天然覆盖。
    /generate-draft/status 保留为降级兜底通道（推拉结合）。
    """
    import asyncio, json
    from fastapi.responses import StreamingResponse
    from app.services.experience_generator import (
        get_draft_job, register_draft_waiter, unregister_draft_waiter)
    from app.models import CompileTask

    task = db.query(CompileTask).filter(CompileTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="task 不存在")
    if current_user.role != "admin" and task.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="无权访问该任务")

    async def event_gen():
        def _ev(**kw):
            return "data: " + json.dumps(kw, ensure_ascii=False) + "\n\n"
        # 顺序=先注册再查状态：堵"查得 running → 注册前 worker 投递终态 → 无人收件
        # 白等 6min"的窗口；注册后先排空收件箱，再看 job 快照
        q = register_draft_waiter(task_id)
        try:
            drained = []
            while True:
                try:
                    drained.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if drained:
                d0 = drained[-1]
                yield _ev(type=d0["status"], result=d0.get("result"),
                          message=d0.get("error"))
                return
            job = get_draft_job(task_id)
            if not job:
                yield _ev(type="none")
                return
            if job["status"] != "running":
                yield _ev(type=job["status"], result=job.get("result"),
                          message=job.get("error"))
                return
            deadline = 360.0  # LLM 1~4min 常规，超 6min 视为异常→让前端降级轮询
            while True:
                try:
                    snap = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield _ev(type=snap["status"], result=snap.get("result"),
                              message=snap.get("error"))
                    return
                except asyncio.TimeoutError:
                    deadline -= 15.0
                    if deadline <= 0:
                        yield _ev(type="timeout")
                        return
                    yield ": ping\n\n"   # SSE 注释心跳，防代理闲置断连
        finally:
            unregister_draft_waiter(task_id, q)

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/generate-draft/status", response_model=ApiResponse)
async def generate_draft_status(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """轮询 AI 草稿任务：running / done(result) / error(message) / none(未启动)
    （批2-乙，工单 preview-job/status 同款；属主校验与触发生成同规则）"""
    from app.services.experience_generator import get_draft_job
    task = db.query(CompileTask).filter(CompileTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="task 不存在")
    if current_user.role != "admin" and task.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="无权访问该任务")
    job = get_draft_job(task_id)
    if not job:
        return ApiResponse(data={"status": "none"})
    return ApiResponse(data={
        "status": job["status"],
        "result": job.get("result"),
        "error": job.get("error"),
        "elapsed": round(__import__("time").time() - job["started_at"], 1),
    })


import json
