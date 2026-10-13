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
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


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
    """删除 Skill（真删）：DB 硬删 Skill 行 + 删磁盘 SKILL.md + 级联清关联 Experience 行

    一致性保证：磁盘 = source of truth，DB = derived cache。删 Skill 后：
      - 磁盘 SKILL.md 删
      - DB Skills 行删
      - 关联的 Experience 行（同 skill_name 且 status='approved'/'synced'）一并删
        避免下次有人提交同 task 时 400 报"已有经验"但实际 Skill 已被删

    旧版设计有"软删 + 回收站"，已废弃（接口保留返回 404/空避免前端点错）
    """
    from app.models import Experience, ExperienceStatus

    skill = db.query(Skill).filter(Skill.skill_id == skill_id).first()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill 不存在")

    skill_type = str(skill.type)
    skill_name = str(skill.name)

    # 1) 级联清关联 Experience 行（approved/synced 状态；submitted/rejected 留给用户后续走完）
    related_exps = db.query(Experience).filter(
        Experience.skill_name == skill_name,
        Experience.status.in_([ExperienceStatus.APPROVED.value, ExperienceStatus.SYNCED.value]),
    ).all()
    related_count = len(related_exps)
    for e in related_exps:
        db.delete(e)
    if related_count:
        logger.info("[delete_skill] 级联清 %d 条关联 Experience 行（skill_name=%s）", related_count, skill_name)

    # 2) DB 硬删 Skill 行
    db.delete(skill)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB 删 Skill 失败: {e}")

    # 3) 同步删磁盘（失败不致命 — DB 已删；磁盘残留下次 reconcile 会清）
    try:
        remove_skill_from_disk(skill_type, skill_name)
    except RuntimeError as e:
        logger.warning("清磁盘失败（DB 已删）: %s", e)

    msg = f"Skill 已删除：{skill_name}"
    if related_count:
        msg += f"（同时清理 {related_count} 条关联经验）"
    return ApiResponse(message=msg)


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
    """回收站机制已下线（删 = 真删）。保留接口返空，兼容前端可能残留的菜单"""
    return ApiResponse(message="回收站机制已下线，删除即真删", data=[])


@router.post("/skills/{skill_id}/restore", response_model=ApiResponse)
async def restore_skill_from_trash(
    skill_id: int,
    db: Session = Depends(get_db)
):
    """回收站机制已下线，永远 410"""
    raise HTTPException(status_code=410, detail="回收站机制已下线，无法恢复；如需重新入库请走 experience 流程")


@router.delete("/skills/{skill_id}/permanent", response_model=ApiResponse)
async def permanently_delete_skill(
    skill_id: int,
    db: Session = Depends(get_db)
):
    """兼容保留：调 DELETE /skills/{id} 即真删（包括磁盘），permanent 端点不再额外做事

    旧版语义：从 DB 硬删（磁盘文件不管）。新版 DELETE /skills/{id} 已包含磁盘删。
    留这个接口兼容老前端，但功能已被 DELETE 覆盖。
    raise HTTPException 让前端明确：本路由该 skill 不存在即可
    """
    skill = db.query(Skill).filter(Skill.skill_id == skill_id).first()
    if skill:
        # 走 DELETE /skills/{id} 的逻辑：DB 硬删 + 磁盘删 + 级联清 Experience
        # 这里直接调 delete_skill 实现（避免重复代码）
        return await delete_skill(skill_id, db)
    raise HTTPException(status_code=404, detail="Skill 不存在（请用 DELETE /admin/skills/{id}）")


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
    # 唯一性检查（schema 不会报，但 DB unique constraint 会让 commit 抛 IntegrityError）
    existing = db.query(TargetSystem).filter(TargetSystem.name == system_data.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"系统名已存在: {system_data.name}")
    system = TargetSystem(
        name=system_data.name,
        base_image=system_data.base_image,
    )
    db.add(system)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB 写入失败: {e}")
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

    # 改名时检查新名不冲突
    if system_data.name and system_data.name != system.name:
        clash = db.query(TargetSystem).filter(
            TargetSystem.name == system_data.name,
            TargetSystem.system_id != system_id,
        ).first()
        if clash:
            raise HTTPException(status_code=400, detail=f"系统名已存在: {system_data.name}")
        system.name = system_data.name
    if system_data.base_image:
        system.base_image = system_data.base_image
    if system_data.is_active is not None:
        system.is_active = system_data.is_active

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"DB 更新失败: {e}")
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
