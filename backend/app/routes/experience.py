"""
Experience and Skill routes
Based on design doc section 11.1: POST /api/experience/submit, GET /api/experience/my
"""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional

from app.models import get_db, Experience, Skill, CompileTask, TargetSystem, ExperienceStatus
from app.schemas import (
    ExperienceSubmit, ExperienceResponse, SkillCreate, SkillUpdate, SkillResponse,
    ApiResponse
)
from app.services.skill_loader import write_skill_to_disk

router = APIRouter()


@router.post("/submit", response_model=ApiResponse)
async def submit_experience(
    exp_data: ExperienceSubmit,
    db: Session = Depends(get_db),
    current_user_id: int = 1  # TODO: 从 session 获取
):
    """提交编译经验"""
    # 验证任务存在且成功
    task = db.query(CompileTask).filter(CompileTask.id == exp_data.task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="关联任务不存在")
    if task.status.value != "success":
        raise HTTPException(status_code=400, detail="只有成功的编译任务才能提交经验")
    
    # 检查是否已提交过
    existing = db.query(Experience).filter(
        Experience.task_id == exp_data.task_id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="该任务已提交过经验")
    
    # 创建经验记录
    experience = Experience(
        task_id=exp_data.task_id,
        user_id=current_user_id,
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
    current_user_id: int = 1  # TODO: 从 session 获取
):
    """获取我的经验提交记录"""
    experiences = db.query(Experience).filter(
        Experience.user_id == current_user_id
    ).order_by(Experience.submitted_at.desc()).all()
    
    return ApiResponse(data=[
        ExperienceResponse.model_validate(e).model_dump()
        for e in experiences
    ])


@router.get("/pending", response_model=ApiResponse)
async def list_pending_experiences(
    db: Session = Depends(get_db)
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
    reviewer_id: int = 1  # TODO: 从 session 获取
):
    """通过经验审核

    流程：
    1. 写 DB（Skill 表）+ 计算 version
    2. 同步写盘到 skills/<type>/<name>/SKILL.md（SKILLS_DIR_HOST 宿主机目录）
    3. 写盘失败 → 整个 approve 回滚（DB 也回滚），返回 500
    """
    experience = db.query(Experience).filter(Experience.experience_id == experience_id).first()
    if not experience:
        raise HTTPException(status_code=404, detail="经验不存在")

    experience.status = ExperienceStatus.APPROVED
    experience.reviewed_at = datetime.utcnow()

    # 创建或更新 Skill（DB）
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

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB 写入失败: {e}")

    # 同步写盘（这步失败要回滚 DB，避免 DB 与磁盘不一致）
    try:
        write_skill_to_disk(
            skill_type=str(experience.skill_type),
            skill_name=str(experience.skill_name),
            content=str(experience.skill_content or ""),
            version=version,
        )
    except (ValueError, RuntimeError) as e:
        # 写盘失败 → 把 DB 改回原状
        db.rollback()
        raise HTTPException(status_code=500, detail=f"SKILL.md 写盘失败（已回滚 DB）: {e}")

    db.commit()

    return ApiResponse(message="经验已通过审核，SKILL.md 已同步到磁盘")


@router.post("/{experience_id}/reject", response_model=ApiResponse)
async def reject_experience(
    experience_id: int,
    reason: Optional[str] = None,
    db: Session = Depends(get_db),
    reviewer_id: int = 1  # TODO: 从 session 获取
):
    """驳回经验审核"""
    experience = db.query(Experience).filter(Experience.experience_id == experience_id).first()
    if not experience:
        raise HTTPException(status_code=404, detail="经验不存在")
    
    experience.status = ExperienceStatus.REJECTED
    experience.rejection_reason = reason
    experience.reviewed_at = datetime.utcnow()
    
    db.commit()
    
    return ApiResponse(message="经验已驳回")


import json
