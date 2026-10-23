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
    from app.services.experience_generator import generate_draft

    task_id = body.get("task_id") if isinstance(body, dict) else None
    if not isinstance(task_id, int):
        raise HTTPException(status_code=422, detail="body.task_id 必须是整数")

    task = db.query(CompileTask).filter(CompileTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="task 不存在")
    if current_user.role != "admin" and task.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="无权访问该任务")

    try:
        result = generate_draft(db, task_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        # 任务未结束 / LLM 失败 / 草稿不合法
        msg = str(e)
        if "未结束" in msg:
            raise HTTPException(status_code=400, detail=msg)
        raise HTTPException(status_code=500, detail=msg)

    return ApiResponse(
        message="草稿生成成功",
        data=result,
    )


import json
