"""
Admin routes
Based on design doc section 11.2: stats, tickets management, skill CRUD, users, systems
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta
import json

from app.models import get_db, Ticket, Experience, Skill, User, TargetSystem, CompileTask, \
    TicketStatus, ExperienceStatus, UserRole
from app.schemas import (
    TargetSystemCreate, TargetSystemUpdate, TargetSystemResponse,
    SkillCreate, SkillUpdate, SkillResponse,
    StatsOverview, ApiResponse
)
from app.services.skill_loader import write_skill_to_disk, remove_skill_from_disk

router = APIRouter()


# ==================== 统计看板 ====================

@router.get("/stats/overview", response_model=ApiResponse)
async def get_stats_overview(db: Session = Depends(get_db)):
    """获取统计看板数据"""
    # 本月编译成功率
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    total_tasks = db.query(CompileTask).count()
    success_tasks = db.query(CompileTask).filter(
        CompileTask.status.value == "success"
    ).count()
    
    # 近30天趋势
    trend = []
    for i in range(30):
        day = now - timedelta(days=29-i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        day_total = db.query(CompileTask).filter(
            CompileTask.created_at >= day_start,
            CompileTask.created_at <= day_end
        ).count()
        day_success = db.query(CompileTask).filter(
            CompileTask.created_at >= day_start,
            CompileTask.created_at <= day_end,
            CompileTask.status == "success"
        ).count()
        
        rate = (day_success / day_total * 100) if day_total > 0 else 0
        trend.append({
            "date": day.strftime("%m-%d"),
            "rate": round(rate, 1),
            "total": day_total
        })
    
    # 活跃用户数（最近7天有活动的用户）
    week_ago = now - timedelta(days=7)
    active_users = db.query(User).filter(
        User.last_active >= week_ago,
        User.is_active == True
    ).count()
    
    return ApiResponse(data={
        "success_rate": round(success_tasks / total_tasks * 100, 1) if total_tasks > 0 else 0,
        "total_tasks": total_tasks,
        "total_experiences": db.query(Experience).count(),
        "active_users": active_users,
        "recent_success_rate_trend": trend[-7:],  # 最近7天
        "top_errors": [],  # TODO: 实现错误统计
        "cluster_nodes": [],  # TODO: 实现节点状态
        "skill_sync_summary": {}  # TODO: 实现 Skill 同步统计
    })


# ==================== 工单管理 ====================

@router.get("/tickets", response_model=ApiResponse)
async def list_tickets(
    status: Optional[str] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """获取工单列表（管理员）"""
    query = db.query(Ticket)
    
    if status:
        query = query.filter(Ticket.status == TicketStatus(status))
    if search:
        query = query.filter(
            Ticket.title.ilike(f"%{search}%") |
            Ticket.ticket_id.ilike(f"%{search}%")
        )
    
    tickets = query.order_by(Ticket.created_at.desc()).all()
    
    return ApiResponse(data=[
        {
            "ticket_id": t.ticket_id,
            "task_id": t.task_id,
            "user_id": t.user_id,
            "source": t.source,
            "status": t.status.value,
            "title": t.title,
            "created_at": t.created_at.isoformat(),
            "updated_at": t.updated_at.isoformat()
        }
        for t in tickets
    ])


@router.post("/tickets/{ticket_id}/messages", response_model=ApiResponse)
async def send_ticket_message(
    ticket_id: int,
    content: str,
    db: Session = Depends(get_db),
    admin_id: int = 1  # TODO: 从 session 获取
):
    """发送工单消息（管理员回复）"""
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    
    from app.models import TicketMessage
    message = TicketMessage(
        ticket_id=ticket_id,
        user_id=admin_id,
        role="admin",
        content=content
    )
    db.add(message)
    
    # 更新工单状态为处理中
    if ticket.status == TicketStatus.PENDING:
        ticket.status = TicketStatus.PROCESSING
    
    db.commit()
    
    return ApiResponse(message="消息已发送")


@router.post("/tickets/{ticket_id}/resolve", response_model=ApiResponse)
async def resolve_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    admin_id: int = 1  # TODO: 从 session 获取
):
    """标记工单为已解决"""
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    
    ticket.status = TicketStatus.RESOLVED
    ticket.resolved_at = datetime.utcnow()
    db.commit()
    
    return ApiResponse(message="工单已标记为已解决")


# ==================== Skill 管理 ====================

@router.get("/skills", response_model=ApiResponse)
async def list_skills(
    skill_type: Optional[str] = None,
    is_active: Optional[bool] = None,
    search: Optional[str] = None,
    include_deleted: bool = False,
    db: Session = Depends(get_db)
):
    """获取 Skill 列表（按 name/description 模糊匹配）

    默认不显示已软删的（deleted_at IS NOT NULL），传 include_deleted=true 显示全部
    """
    query = db.query(Skill)

    if not include_deleted:
        query = query.filter(Skill.deleted_at.is_(None))
    if skill_type:
        query = query.filter(Skill.type == skill_type)
    if is_active is not None:
        query = query.filter(Skill.is_active == is_active)
    if search:
        # 用 ilike 做大小写不敏感模糊匹配（PostgreSQL/SQLite 都支持）
        from sqlalchemy import or_
        like = f"%{search}%"
        query = query.filter(or_(Skill.name.ilike(like), Skill.description.ilike(like)))

    skills = query.order_by(Skill.created_at.desc()).all()

    return ApiResponse(data=[SkillResponse.model_validate(s).model_dump() for s in skills])


@router.post("/skills", response_model=ApiResponse)
async def create_skill(
    skill_data: SkillCreate,
    db: Session = Depends(get_db)
):
    """新增 Skill（同步写盘到 skills/<type>/<name>/SKILL.md）"""
    # 检查名称是否已存在
    existing = db.query(Skill).filter(Skill.name == skill_data.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Skill 名称已存在")

    # skill_name 必须合法（slug 校验交给 write_skill_to_disk，这里提前校验友好报错）
    from app.services.skill_loader import _validate_skill_name, _validate_type
    try:
        _validate_skill_name(skill_data.name)
        _validate_type(skill_data.type)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    skill = Skill(
        name=skill_data.name,
        type=skill_data.type,
        description=skill_data.description,
        system_ids=json.dumps(skill_data.system_ids),
        version=skill_data.version,
        skill_content=skill_data.skill_content,
        script_content=skill_data.script_content,
        source="admin",  # 管理页面创建
    )
    db.add(skill)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB 写入失败: {e}")

    # 同步写盘
    try:
        write_skill_to_disk(
            skill_type=str(skill_data.type),
            skill_name=str(skill_data.name),
            content=str(skill_data.skill_content or ""),
            version=skill_data.version,
        )
    except (ValueError, RuntimeError) as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"SKILL.md 写盘失败（已回滚 DB）: {e}")

    db.commit()
    db.refresh(skill)

    return ApiResponse(data=SkillResponse.model_validate(skill).model_dump())


@router.put("/skills/{skill_id}", response_model=ApiResponse)
async def update_skill(
    skill_id: int,
    skill_data: SkillUpdate,
    db: Session = Depends(get_db)
):
    """更新 Skill（同步刷新磁盘 SKILL.md）"""
    skill = db.query(Skill).filter(Skill.skill_id == skill_id).first()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill 不存在")

    # 改名时校验新名 slug（type 不允许改 — SkillUpdate schema 无该字段）
    if skill_data.name and skill_data.name != skill.name:
        from app.services.skill_loader import _validate_skill_name
        try:
            _validate_skill_name(skill_data.name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    old_type = str(skill.type)
    old_name = str(skill.name)

    if skill_data.name:
        skill.name = skill_data.name
    if skill_data.description:
        skill.description = skill_data.description
    if skill_data.system_ids is not None:
        skill.system_ids = json.dumps(skill_data.system_ids)
    if skill_data.version:
        skill.version = skill_data.version
    if skill_data.is_active is not None:
        skill.is_active = skill_data.is_active
    if skill_data.skill_content:
        skill.skill_content = skill_data.skill_content

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB 更新失败: {e}")

    # 如果改名 → 先清旧目录
    new_name = str(skill.name)
    if old_name != new_name:
        try:
            remove_skill_from_disk(old_type, old_name)
        except RuntimeError as e:
            # 旧目录清不掉不致命，记录即可
            import logging
            logging.warning("改名时清旧目录失败: %s", e)

    # 同步写盘（disk 上的 type 始终是当前 DB 的 type — SkillUpdate 不允许改 type）
    try:
        write_skill_to_disk(
            skill_type=old_type,
            skill_name=new_name,
            content=str(skill.skill_content or ""),
            version=str(skill.version) if skill.version else None,
        )
    except (ValueError, RuntimeError) as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"SKILL.md 写盘失败（已回滚 DB）: {e}")

    db.commit()
    db.refresh(skill)

    return ApiResponse(data=SkillResponse.model_validate(skill).model_dump())


@router.delete("/skills/{skill_id}", response_model=ApiResponse)
async def delete_skill(
    skill_id: int,
    db: Session = Depends(get_db)
):
    """删除 Skill（软删：清磁盘 + DB 标 deleted_at，备份用）

    恢复方式：POST /admin/skills/{id}/restore
    永久删除：DELETE /admin/skills/{id}/permanent
    """
    skill = db.query(Skill).filter(Skill.skill_id == skill_id).first()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    if skill.deleted_at is not None:
        # 已经是软删状态，不重复设
        return ApiResponse(message="Skill 已在回收站")

    skill_type = str(skill.type)
    skill_name = str(skill.name)

    # 软删：设 deleted_at
    skill.deleted_at = datetime.utcnow()
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB 标记失败: {e}")

    # 同步清磁盘（失败不致命 — DB 已标，磁盘残留下次 reconcile 也会清）
    try:
        remove_skill_from_disk(skill_type, skill_name)
    except RuntimeError as e:
        import logging
        logging.warning("清磁盘失败（DB 已软删）: %s", e)

    return ApiResponse(message="Skill 已删除（回收站可恢复）")


def _parse_frontmatter_field(content: str, field: str) -> str | None:
    """简单 YAML frontmatter 解析（只解顶层 key: value 或 key: [list]）

    不依赖 PyYAML（保持后端 minimal）
    返回字段值；不存在返回 None
    """
    import re
    if not content.startswith("---\n"):
        return None
    end = content.find("\n---", 4)
    if end < 0:
        return None
    fm = content[4:end]
    # 匹配 `field: value` 或 `field: [a, b]`
    m = re.search(rf"^{re.escape(field)}:\s*(.+?)\s*$", fm, re.MULTILINE)
    if not m:
        return None
    val = m.group(1).strip()
    if val.startswith("[") and val.endswith("]"):
        # 列表语法 [a, b] 留给上层处理，这里只返回原始字符串
        return val
    # 去掉行尾注释
    if " #" in val:
        val = val.split(" #", 1)[0].strip()
    # 去掉引号
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        val = val[1:-1]
    return val


@router.post("/skills/reconcile", response_model=ApiResponse)
async def reconcile_skills(db: Session = Depends(get_db)):
    """手动触发 reconcile（磁盘 → DB 同步）

    2 状态机：
    - disk_only : 磁盘有，DB 没有 → 插入 DB
    - db_only   : DB 有，磁盘没  → 软删（设 deleted_at），DB 行保留作备份

    前端在进入 SkillLib 页面时自动调一次。
    """
    from app.services.skill_loader import reconcile_all
    stats = reconcile_all(db)
    msg = (
        f"reconcile 完成：磁盘 {stats['total_disks']} / DB {stats['total_dbs']}，"
        f"新增 {stats['disk_only']}，软删 {stats['db_only']}"
    )
    return ApiResponse(message=msg, data=stats)


@router.get("/skills/trash", response_model=ApiResponse)
async def list_trash_skills(db: Session = Depends(get_db)):
    """列出回收站里的 skill（已软删，可恢复）"""
    skills = db.query(Skill).filter(Skill.deleted_at.isnot(None)).order_by(Skill.deleted_at.desc()).all()
    return ApiResponse(data=[SkillResponse.model_validate(s).model_dump() for s in skills])


@router.post("/skills/{skill_id}/restore", response_model=ApiResponse)
async def restore_skill_from_trash(
    skill_id: int,
    db: Session = Depends(get_db)
):
    """从回收站恢复 skill：写盘 + 清 deleted_at

    如果磁盘上已经有同名 skill 文件，直接覆盖（disk 改了就改了）
    """
    from app.services.skill_loader import write_skill_to_disk

    skill = db.query(Skill).filter(Skill.skill_id == skill_id).first()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    if skill.deleted_at is None:
        raise HTTPException(status_code=400, detail="Skill 未在回收站，无需恢复")

    try:
        write_skill_to_disk(
            skill_type=str(skill.type),
            skill_name=str(skill.name),
            content=str(skill.skill_content or ""),
            version=str(skill.version) if skill.version else None,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=500, detail=f"写盘失败: {e}")

    skill.deleted_at = None
    db.commit()
    db.refresh(skill)
    return ApiResponse(
        message=f"已恢复 {skill.name} 到磁盘",
        data=SkillResponse.model_validate(skill).model_dump(),
    )


@router.delete("/skills/{skill_id}/permanent", response_model=ApiResponse)
async def permanently_delete_skill(
    skill_id: int,
    db: Session = Depends(get_db)
):
    """永久删除 skill（从 DB 硬删，不可恢复）

    磁盘文件不管（用户可能想保留）
    """
    skill = db.query(Skill).filter(Skill.skill_id == skill_id).first()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill 不存在")

    skill_name = str(skill.name)
    db.delete(skill)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB 硬删失败: {e}")

    return ApiResponse(message=f"已永久删除 {skill_name}（不可恢复）")


# ==================== 目标系统管理 ====================

@router.get("/systems", response_model=ApiResponse)
async def list_systems(
    is_active: Optional[bool] = None,
    db: Session = Depends(get_db)
):
    """获取目标系统列表"""
    query = db.query(TargetSystem)
    
    if is_active is not None:
        query = query.filter(TargetSystem.is_active == is_active)
    
    systems = query.order_by(TargetSystem.created_at.desc()).all()
    
    return ApiResponse(data=[TargetSystemResponse.model_validate(s).model_dump() for s in systems])


@router.post("/systems", response_model=ApiResponse)
async def create_system(
    system_data: TargetSystemCreate,
    db: Session = Depends(get_db)
):
    """新增目标系统"""
    system = TargetSystem(
        name=system_data.name,
        arch=system_data.arch,
        base_image=system_data.base_image,
        tag=system_data.tag or "latest"
    )
    db.add(system)
    db.commit()
    db.refresh(system)
    
    return ApiResponse(data=TargetSystemResponse.model_validate(system).model_dump())


@router.put("/systems/{system_id}", response_model=ApiResponse)
async def update_system(
    system_id: int,
    system_data: TargetSystemUpdate,
    db: Session = Depends(get_db)
):
    """更新目标系统配置"""
    system = db.query(TargetSystem).filter(TargetSystem.system_id == system_id).first()
    if not system:
        raise HTTPException(status_code=404, detail="目标系统不存在")
    
    if system_data.name:
        system.name = system_data.name
    if system_data.base_image:
        system.base_image = system_data.base_image
    if system_data.tag is not None:
        system.tag = system_data.tag
    if system_data.is_active is not None:
        system.is_active = system_data.is_active
    
    db.commit()
    db.refresh(system)
    
    return ApiResponse(data=TargetSystemResponse.model_validate(system).model_dump())


@router.delete("/systems/{system_id}", response_model=ApiResponse)
async def delete_system(
    system_id: int,
    db: Session = Depends(get_db)
):
    """删除目标系统"""
    system = db.query(TargetSystem).filter(TargetSystem.system_id == system_id).first()
    if not system:
        raise HTTPException(status_code=404, detail="目标系统不存在")
    
    # 检查是否有正在运行的任务
    active_tasks = db.query(CompileTask).filter(
        CompileTask.system_id == system_id,
        CompileTask.status.in_(["submitted", "compiling"])
    ).count()
    
    if active_tasks > 0:
        raise HTTPException(status_code=400, detail=f"该系统有 {active_tasks} 个进行中任务，无法删除")
    
    db.delete(system)
    db.commit()
    
    return ApiResponse(message="目标系统已删除")


# ==================== 用户管理 ====================

@router.get("/users", response_model=ApiResponse)
async def list_users(
    db: Session = Depends(get_db)
):
    """获取用户列表"""
    users = db.query(User).order_by(User.created_at.desc()).all()
    
    return ApiResponse(data=[
        {
            "user_id": u.user_id,
            "username": u.username,
            "role": u.role,
            "last_active": u.last_active.isoformat() if u.last_active else None,
            "is_active": u.is_active
        }
        for u in users
    ])
