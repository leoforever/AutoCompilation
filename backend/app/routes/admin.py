"""
Admin routes
Based on design doc section 11.2: stats, tickets management, skill CRUD, users, systems
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta
import json
import time
import bcrypt

from app.models import get_db, Ticket, Experience, Skill, User, TargetSystem, CompileTask, \
    TicketStatus, ExperienceStatus, UserRole, TaskStatus, AuditLog
from app.schemas import (
    TargetSystemCreate, TargetSystemUpdate, TargetSystemResponse,
    SkillCreate, SkillUpdate, SkillResponse,
    UserPasswordReset, UserAdminCreate, UserAdminUpdate,
    StatsOverview, ApiResponse
)
from app.dependencies import get_current_user
from app.services.audit import log_audit
from app.services.skill_loader import write_skill_to_disk, read_skill_from_disk, remove_skill_from_disk
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


# ==================== 统计看板 ====================

@router.get("/stats/overview", response_model=ApiResponse)
async def get_stats_overview(db: Session = Depends(get_db)):
    """统计看板数据 — 2026-10-20 重构：全部字段来自真实表聚合，无任何写死兜底。

    返回：
    - kpi: 本月/上月成功率+环比、任务总数/本周新增、工单解决率/平均时长、待审经验、Agent 对话本周、活跃用户
    - trend: 近 7 天每日 {date,total,success,failed}（1 次 group by，替代原 60 次循环 SQL；样本少时前端自行标注）
    - top_errors: 失败原因归类 TOP（关键词规则基于真实 error_message 形态；空消息归"未记录错误"）
    - ticket_pipeline: 工单四段状态分布
    - experience_funnel: 工单解决→经验提交→审批通过→Skill 落盘 闭环漏斗
    """
    from pathlib import Path as _P
    from sqlalchemy import func as _f
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_month_start = (month_start - timedelta(days=1)).replace(day=1)
    week_start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    # created_at 是 naive datetime（utcnow 默认），与本地表一致直接用 Python 边界过滤

    def _task_stats(lo, hi):
        total = db.query(CompileTask).filter(CompileTask.created_at >= lo, CompileTask.created_at < hi).count()
        succ = db.query(CompileTask).filter(CompileTask.created_at >= lo, CompileTask.created_at < hi,
                                            CompileTask.status == "success").count()
        return total, succ

    def _rate(total, succ):
        return round(succ / total * 100, 1) if total > 0 else 0.0

    m_total, m_succ = _task_stats(month_start, now + timedelta(days=1))
    p_total, p_succ = _task_stats(prev_month_start, month_start)
    m_rate, p_rate = _rate(m_total, m_succ), _rate(p_total, p_succ)

    total_tasks = db.query(CompileTask).count()
    week_new_tasks = db.query(CompileTask).filter(CompileTask.created_at >= week_start).count()

    # ---- 工单 ----
    t_counts = {str(k): int(v) for k, v in db.query(Ticket.status, _f.count(Ticket.ticket_id)).group_by(Ticket.status).all()}
    t_total = sum(t_counts.values())
    t_solved = t_counts.get("resolved", 0) + t_counts.get("closed", 0)
    # 平均解决时长：resolved_at 有值的工单（秒→小时，1 位小数）
    avg_solve_h = db.query(_f.avg(_f.extract("epoch", Ticket.resolved_at) - _f.extract("epoch", Ticket.created_at))).filter(
        Ticket.resolved_at.isnot(None)).scalar()
    # PG avg 返回 Decimal → 必须转 float，否则 JSON 序列化成字符串 "19.6"
    avg_solve_hours = round(float(avg_solve_h) / 3600, 1) if avg_solve_h else None

    # ---- 经验 / Skill ----
    pending_exp = db.query(Experience).filter(Experience.status == "submitted").count()
    approved_exp = db.query(Experience).filter(Experience.status == "approved").count()
    from app.services.skill_loader import SKILLS_DIR_HOST
    disk_specific = len([d for d in (_P(SKILLS_DIR_HOST) / "specific").iterdir() if d.is_dir()]) if (_P(SKILLS_DIR_HOST) / "specific").is_dir() else 0

    # ---- Agent 活跃度（本周对话轮次 = 两条审计 action 之和）----
    agent_msgs_week = db.query(AuditLog).filter(AuditLog.created_at >= week_start,
                                                AuditLog.action.in_(["ticket_agent_message", "ticket_agent_message_stream"])).count()

    active_users = db.query(User).filter(User.last_active >= week_start, User.is_active == True).count()  # noqa: E712

    # ---- 近 7 天趋势：1 次 group by（date_trunc 用 UTC 与 created_at 存储一致）----
    trend_rows = db.query(
        _f.date_trunc("day", CompileTask.created_at).label("d"),
        _f.count().label("total"),
        _f.count().filter(CompileTask.status == "success").label("succ"),
        _f.count().filter(CompileTask.status == "failed").label("fail"),
    ).filter(CompileTask.created_at >= (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
     ).group_by("d").all()
    by_day = {r.d.strftime("%m-%d"): r for r in trend_rows}
    trend = []
    for i in range(7):
        day = (now - timedelta(days=6 - i)).replace(hour=0, minute=0, second=0, microsecond=0)
        key = day.strftime("%m-%d")
        r = by_day.get(key)
        trend.append({"date": key, "total": (r.total if r else 0), "success": (r.succ if r else 0), "failed": (r.fail if r else 0)})

    # ---- 失败原因归类（关键词规则源自真实 error_message 形态；按优先级首个命中）----
    failed_msgs = [m or "" for (m,) in db.query(CompileTask.error_message).filter(CompileTask.status == "failed").all()]
    _RULES = [
        ("LLM 限流（429/Token Plan）", lambda e: "429" in e or "rate_limit" in e),
        ("编译超时", lambda e: "超时" in e or "timeout" in e.lower()),
        ("源码获取失败（git/下载）", lambda e: "git clone" in e or "仓库" in e or "Repository not found" in e or "下载失败" in e),
        ("内存溢出（OOM）", lambda e: "OOM" in e or "out of memory" in e.lower() or "内存不足" in e),
        ("依赖缺失", lambda e: "devel" in e or "依赖" in e or "not found" in e.lower() and "repo" not in e.lower()),
    ]
    bucket = {}
    for e in failed_msgs:
        label = next((name for name, fn in _RULES if fn(e)), "未记录错误信息" if not e.strip() else "其它")
        bucket[label] = bucket.get(label, 0) + 1
    n_failed = len(failed_msgs)
    top_errors = sorted(
        [{"error_type": k, "count": v, "percentage": round(v / n_failed * 100, 1)} for k, v in bucket.items()],
        key=lambda x: -x["count"])[:5] if n_failed else []

    # 入库健康（Step 0）：persist 失败计数 + 上轮对账结果 + 滞留任务数 —— 消灭静默失败
    from app.services.task_writer import persist_error_stats
    from app.services.task_reconciler import last_result as _recon_last
    _stuck_n = db.query(_f.count(CompileTask.id)).filter(
        CompileTask.status == TaskStatus.COMPILING.value,
        CompileTask.updated_at < now - timedelta(seconds=600)).scalar() or 0
    _health = {
        "persist_errors": persist_error_stats(),
        "last_reconcile": _recon_last,
        "stale_compiling_count": int(_stuck_n),
    }
    return ApiResponse(data={
        "health": _health,
        "kpi": {
            "month_success_rate": m_rate,
            "month_delta": round(m_rate - p_rate, 1),
            "month_has_data": m_total > 0,
            "total_tasks": total_tasks,
            "week_new_tasks": week_new_tasks,
            "ticket_total": t_total,
            "ticket_solve_rate": _rate(t_total, t_solved),
            "ticket_avg_solve_hours": avg_solve_hours,
            "pending_experiences": pending_exp,
            "agent_msgs_week": agent_msgs_week,
            "active_users": active_users,
        },
        "recent_success_rate_trend": trend,
        "top_errors": top_errors,
        "failed_samples": n_failed,  # TOP 归类样本数（<10 前端显示"样本积累中"）
        "ticket_pipeline": [{"status": s, "count": t_counts.get(s, 0)} for s in ("pending", "processing", "resolved", "closed")],
        "experience_funnel": [
            {"stage": "工单解决", "count": t_solved},
            {"stage": "经验提交", "count": db.query(Experience).count()},
            {"stage": "审批通过", "count": approved_exp},
            {"stage": "Skill 落盘", "count": disk_specific},
        ],
        "generated_at": now.isoformat(),
    })


# ==================== 工单管理 ====================

@router.get("/tickets", response_model=ApiResponse)
async def list_tickets(
    status: Optional[str] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """获取工单列表（管理员，含软删用户原名 + 软件名/工单号/描述/原名 多字段搜索）"""
    from sqlalchemy import func as _f, String as _S
    query = db.query(Ticket)

    if status:
        query = query.filter(Ticket.status == TicketStatus(status))
    if search:
        like = f"%{search}%"
        from sqlalchemy import or_ as _or
        query = query.outerjoin(CompileTask, Ticket.task_id == CompileTask.id) \
                     .outerjoin(User, Ticket.user_id == User.user_id)
        query = query.filter(
            _or(
                Ticket.title.ilike(like),
                Ticket.description.ilike(like),
                _f.cast(Ticket.ticket_id, _S).ilike(like),
                CompileTask.software_name.ilike(like),
                User.original_username.ilike(like),
                User.username.ilike(like),
            )
        )

    tickets = query.order_by(Ticket.created_at.desc()).limit(200).all()

    # 软删用户原名回显
    user_cache = {}
    def _uname(uid):
        if uid not in user_cache:
            u = db.query(User).filter(User.user_id == uid).first()
            user_cache[uid] = (u.original_username or u.username) if (u and u.deleted_at) else (u.username if u else "已注销")
        return user_cache[uid]

    return ApiResponse(data=[
        {
            "ticket_id": t.ticket_id,
            "task_id": t.task_id,
            "user_id": t.user_id,
            "username": _uname(t.user_id),
            "source": t.source,
            "status": str(t.status) if not hasattr(t.status, "value") else t.status.value,
            "title": t.title,
            # 解决说明：列表直接可见，不用点详情抽屉（截短防撑爆，完整在详情里）
            "resolution": (t.resolution or "")[:120],
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        }
        for t in tickets
    ])


@router.post("/tickets/{ticket_id}/messages", response_model=ApiResponse)
async def send_ticket_message(
    ticket_id: int,
    content: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """管理员回复工单（接 get_current_user 而非硬编码 admin_id=1）"""
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.status == TicketStatus.CLOSED:
        raise HTTPException(status_code=400, detail="工单已关闭，无法追加消息")
    if not content or not content.strip():
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    from app.models import TicketMessage, AuditLog
    message = TicketMessage(
        ticket_id=ticket_id,
        user_id=current_user.user_id,
        role="admin",
        content=content.strip(),
        is_read=False,  # T8: 管理员回复→用户侧红点
    )
    db.add(message)

    # pending 状态下 admin 首次回复 → processing
    old_status = ticket.status
    if ticket.status == TicketStatus.PENDING:
        ticket.status = TicketStatus.PROCESSING
    db.add(AuditLog(
        user_id=current_user.user_id,
        action="ticket_message",
        target_type="ticket",
        target_id=ticket_id,
        # status 列是 Column[str]，ORM 拉回是裸 str 没有 .value（§2e 同款陷阱；
        # 本端点此前前端零入口从未触发，T8 接通后首爆——str() 兜底两头兼容）
        details=f"status={str(old_status)}→{str(ticket.status)}",
    ))
    db.commit()
    return ApiResponse(message="消息已发送")


@router.post("/tickets/{ticket_id}/resolve", response_model=ApiResponse)
async def resolve_ticket(
    ticket_id: int,
    resolution: str = "",  # admin 必填的解决说明
    auto_generate_skill: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """标记工单为已解决（必填 resolution；可选自动归档 agent 容器产物 + 生成 SKILL.md）

    流程：
    1. 写 ticket.resolution + status=resolved + resolved_at
    2. 若 agent session 还活着：归档 /work/output/ 到 host artifacts/{原 task_id}-R{N}/
    3. 若 auto_generate_skill=true：调 /generate-skill 逻辑
    """
    if not resolution or not resolution.strip():
        raise HTTPException(status_code=400, detail="解决说明 resolution 必填")

    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.status == TicketStatus.RESOLVED:
        return ApiResponse(message="工单已是已解决状态")
    if ticket.status == TicketStatus.CLOSED:
        raise HTTPException(status_code=400, detail="工单已关闭，无法标记")

    from app.models import AuditLog
    old = ticket.status
    ticket.resolution = resolution.strip()
    ticket.status = TicketStatus.RESOLVED
    ticket.resolved_at = datetime.utcnow()

    # 归档 agent 容器产物（如果 session 还活着）
    new_task_id = None
    from app.services.ticket_agent import MANAGER
    # 修复 DetachedInstanceError：上方 db.expunge_all()（或 commit 后 expire）会让
    # 老 ticket 对象访问关系属性爆炸 → 重新取一份绑定当前 session 的
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    sess = MANAGER.get(ticket_id)
    if sess is not None and ticket:
        try:
            from app.config import artifacts_dir as _artifacts_dir  # Q1b：公共权威，不再反向 import 引擎私有
            # sess.task 是接单时那个 request session 的对象，已 Detached →
            # 用当前 session 重新 join 的 ticket.task（同一行，绑定本 session）
            src_task = ticket.task
            # 计算 task_id 后缀 R{N}
            base = src_task.task_id
            existing = db.query(CompileTask).filter(
                CompileTask.task_id.like(f"{base}-R%")
            ).count()
            new_task_id = f"{base}-R{existing + 1}"
            # 拷产物到 host artifacts/{new_task_id}/
            host_out = _artifacts_dir() / new_task_id
            host_out.mkdir(parents=True, exist_ok=True)
            # 用 docker cp 拷 /work/output/ 到 host
            cli = __import__("app.services.compile_engine", fromlist=["get_docker_client"]).get_docker_client()
            if cli:
                try:
                    c = cli.containers.get(sess.container_id)
                    bits, _ = c.get_archive("/work/output")
                    import tempfile
                    from app.services import safe_extract
                    tmp_path = None
                    try:
                        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
                            for chunk in bits:
                                tmp.write(chunk if isinstance(chunk, bytes) else chunk.encode())
                            tmp_path = tmp.name
                        # 批3 3b：产物 tar 同样过安全解包（此前裸 extractall，
                        # 恶意上游可在构建时植入越界/symlink 成员二次传导到宿主机）
                        safe_extract.safe_extract_tar(tmp_path, host_out)
                    finally:
                        if tmp_path:
                            import os as _os
                            _os.unlink(tmp_path)
                except Exception as e:
                    print(f"[resolve] 产物归档失败: {e}")
            # 写新 task 行（继承原 task 的 user_id/system/source）
            new_task = CompileTask(
                task_id=new_task_id,
                user_id=src_task.user_id,
                system_id=src_task.system_id,
                software_name=src_task.software_name,
                source_type=src_task.source_type,
                git_url=src_task.git_url,
                git_branch=src_task.git_branch,
                source_path=src_task.source_path,
                status=TaskStatus.SUCCESS.value if (host_out.exists() and any(host_out.iterdir())) else TaskStatus.FAILED.value,
                artifact_path=str(host_out),
            )
            db.add(new_task)
        except Exception as e:
            print(f"[resolve] 归档新 task 失败: {e}")

    db.add(AuditLog(
        user_id=current_user.user_id,
        action="ticket_resolve",
        target_type="ticket",
        target_id=ticket_id,
        details=f"status={old.value if hasattr(old, 'value') else old}→resolved, new_task={new_task_id or 'none'}",
    ))
    db.commit()

    # resolve 终态：agent 使命完成 → 停容器（stop 不删，重新接单可 docker start 复用）
    # 经验草稿/审计都不依赖活容器；孤儿扫描兜底漏网
    if sess is not None:
        import asyncio
        async def _stop_agent():
            try:
                await MANAGER.destroy(ticket_id, stop_only=True)
            except Exception as e:
                print(f"[resolve] agent 容器停止失败: {e}")
        asyncio.create_task(_stop_agent())

    # 可选：自动生成经验草稿 —— 不再同步调 LLM（60-240s 会撞超时），
    # 起后台预览 job，前端 resolve 成功后直接开预览弹窗（复用 job 秒开进度态）
    draft_job_started = False
    if auto_generate_skill and ticket.resolution:
        from app.services.skill_draft import start_preview_job
        from app.models import SessionLocal
        try:
            start_preview_job(SessionLocal, ticket_id)
            draft_job_started = True
        except Exception as e:
            print(f"[resolve] 草稿 job 启动失败: {e}")

    return ApiResponse(
        data={
            "new_task_id": new_task_id,
            "draft_job_started": draft_job_started,
        },
        message="工单已标记为已解决" + ("，经验草稿生成中，请在预览窗口确认" if draft_job_started else ""),
    )


@router.post("/tickets/{ticket_id}/status", response_model=ApiResponse)
async def admin_change_ticket_status(
    ticket_id: int,
    new_status: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """通用状态变更端点：admin 改工单 status（pending→processing 接单 / →resolved 已解决 / →closed 关闭）

    resolve 端点保留兼容老前端；新代码优先用这个 /status 端点。
    """
    from app.models import AuditLog
    try:
        target = TicketStatus(new_status)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"非法状态: {new_status}（仅支持 pending/processing/resolved/closed）")
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.status == TicketStatus.CLOSED:
        raise HTTPException(status_code=400, detail="工单已关闭，无法变更")
    if ticket.status == target:
        return ApiResponse(message="状态未变化")
    old = ticket.status
    ticket.status = target
    if target == TicketStatus.RESOLVED and not ticket.resolved_at:
        ticket.resolved_at = datetime.utcnow()
    db.add(AuditLog(
        user_id=current_user.user_id,
        action="ticket_status_change",
        target_type="ticket",
        target_id=ticket_id,
        details=f"status={old.value if hasattr(old, 'value') else old}→{target.value}",
    ))
    db.commit()

    # 关闭工单 → 终态回收容器（stop+rm，异步不卡关闭接口）；work_dir/checkpoint/对话记录保留
    if target == TicketStatus.CLOSED:
        import asyncio
        from app.services.ticket_agent import MANAGER
        async def _recycle():
            try:
                r = await MANAGER.recycle(ticket_id)
                print(f"[closed] tkt={ticket_id} 容器回收: {r}")
            except Exception as e:
                print(f"[closed] tkt={ticket_id} 回收异常: {e}")
        asyncio.create_task(_recycle())

    # 接单（pending→processing）→ 异步启动 agent session
    msg_extra = ""
    if (old == TicketStatus.PENDING and target == TicketStatus.PROCESSING):
        import asyncio
        from app.services.ticket_agent import MANAGER
        # 批2-①：闭包不再捕获请求级 ticket/db（原病：响应返回 session 即关，
        # 后台 get_or_create 用死 session/detached ORM）——MANAGER 自建 session 收口
        asyncio.create_task(MANAGER.start_agent_background(ticket.ticket_id))
        msg_extra = "，agent session 正在创建（容器 + 拉源码 + DeepAgent 启动）"

    return ApiResponse(message=f"工单状态已变更为 {target.value}{msg_extra}")


def _task_has_log(t) -> bool:
    """详情懒加载判断：build.log 文件非空 或 DB log_output 非空 → 前端才给"展开日志"入口。
    纯 stat/length 判断不读全文，成本可忽略；任何异常按"无日志"处理（宁缺勿错）。"""
    try:
        from app.config import artifacts_dir as _artifacts_dir  # Q1b：公共权威，不再反向 import 引擎私有
        lf = _artifacts_dir() / t.task_id / "build.log"
        if lf.is_file() and lf.stat().st_size > 0:
            return True
    except Exception:
        pass
    try:
        return bool(t.log_output and t.log_output.strip())
    except Exception:
        return False


@router.get("/tickets/{ticket_id}", response_model=ApiResponse)
async def admin_get_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
):
    """admin 视角工单详情（含消息流、task 完整信息、resolution、agent session 状态）"""
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    from app.models import TicketMessage, TargetSystem
    # 只返 admin/user 之间的对话（agent 内部消息不入库，不返）
    msgs = db.query(TicketMessage).filter(
        TicketMessage.ticket_id == ticket_id,
        TicketMessage.role.in_(["user", "admin"]),  # 不返 agent/tool（不入库）
    ).order_by(TicketMessage.created_at.asc()).all()

    submitter = db.query(User).filter(User.user_id == ticket.user_id).first()
    if submitter and submitter.deleted_at:
        uname = submitter.original_username or submitter.username
    elif submitter:
        uname = submitter.username
    else:
        uname = "已注销"

    # 任务详情（admin 复现需要）
    task_info = None
    if ticket.task:
        t = ticket.task
        sys_name = None
        if t.system_id:
            ts = db.query(TargetSystem).filter(TargetSystem.system_id == t.system_id).first()
            sys_name = ts.name if ts else None
        # source_size（upload 类型）
        source_size_mb = None
        if t.source_type == "upload" and t.source_path:
            try:
                from pathlib import Path
                p = Path(t.source_path)
                if p.exists():
                    source_size_mb = p.stat().st_size // 1024 // 1024
            except Exception:
                pass
        task_info = {
            "task_str_id": t.task_id,  # T-20261016-...
            "db_id": t.id,
            "has_log_content": _task_has_log(t),  # 懒加载用：日志区默认折叠，有此标记才给"展开"按钮
            "software_name": t.software_name,
            "system_id": t.system_id,
            "system_name": sys_name,
            "source_type": t.source_type,
            "git_url": t.git_url,
            "git_branch": t.git_branch,
            "source_path": t.source_path,
            "source_size_mb": source_size_mb,
            "task_status": str(t.status) if not hasattr(t.status, "value") else t.status.value,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "duration_seconds": t.duration_seconds,
        }

    # agent session 是否活跃
    from app.services.ticket_agent import MANAGER
    from app.models import TicketAgent
    sess = MANAGER.get(ticket_id)
    agent_active = sess is not None
    container_short = sess.container_id[:12] if sess else None

    return ApiResponse(data={
        "ticket_id": ticket.ticket_id,
        "task_id": ticket.task_id,
        "user_id": ticket.user_id,
        "username": uname,
        "title": ticket.title,
        "description": ticket.description,
        "resolution": ticket.resolution,
        "status": str(ticket.status) if not hasattr(ticket.status, "value") else ticket.status.value,
        "source": ticket.source,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
        "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
        "task": task_info,  # 新：完整任务详情
        "agent_active": agent_active,  # 新
        "container_id_short": container_short,  # 新
        # T8: 是否曾有 agent 会话（Tab2 可用性：接单建过会话才允许切到工作台看历史指令）
        "agent_session_exists": db.query(TicketAgent).filter(TicketAgent.ticket_id == ticket_id).first() is not None,
        "messages": [
            {
                "id": m.id, "role": m.role, "content": m.content,
                "kind": m.kind or "chat",  # T8: chat=对话线(用户可见) / agent_cmd=工作台指令
                "user_id": m.user_id,
                "username": (
                    (m.user.username if m.user and not m.user.deleted_at
                     else (m.user.original_username if m.user else "已注销"))
                ),
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in msgs
        ],
    })


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

    3e（2026-10-23）：列表 defer 掉 skill_content/script_content 大字段（MB 级 TOAST 读，
    编辑器填充改走 /admin/skills/{skill_id} 详情端点单查）；ORM 未加载列序列化恒 None，
    前端旧消费点形状兼容。
    """
    from sqlalchemy.orm import defer
    query = db.query(Skill).options(defer(Skill.skill_content), defer(Skill.script_content))

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

    # 3e：手搭 dict 而非 model_validate——后者 from_attributes 会 getattr 触发
    # defer 列懒加载，defer 白做（实测击穿）。正文两字段恒 None，编辑器走详情端点
    data = []
    for sk in skills:
        data.append({
            "skill_id": sk.skill_id, "name": sk.name, "type": sk.type,
            "description": sk.description,
            "system_ids": sk.system_ids if isinstance(sk.system_ids, list) else [],
            "version": sk.version, "is_active": sk.is_active, "source": sk.source,
            "skill_content": None, "script_content": None,
            "deleted_at": sk.deleted_at.isoformat() if sk.deleted_at else None,
        })
    return ApiResponse(data=data)


@router.post("/skills", response_model=ApiResponse)
async def create_skill(
    skill_data: SkillCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
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
    # T9批1: 先写盘后 commit（原时序"commit→写盘失败 rollback=no-op"是假回滚）。
    # 新建场景磁盘原无此 skill（name 唯一约束+前面 400 挡过），写盘失败 DB 回滚即全净
    try:
        write_skill_to_disk(
            skill_type=str(skill_data.type),
            skill_name=str(skill_data.name),
            content=str(skill_data.skill_content or ""),
            version=skill_data.version,
        )
    except (ValueError, RuntimeError) as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"SKILL.md 写盘失败（DB 已回滚）: {e}")
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        try:
            remove_skill_from_disk(str(skill_data.type), str(skill_data.name))  # 反向：盘已写 DB 未落
        except Exception as de:
            import logging as _lg; _lg.getLogger(__name__).error("[create_skill] 盘回滚失败: %s", de)
        raise HTTPException(status_code=500, detail=f"DB 写入失败: {e}")
    db.refresh(skill)
    log_audit(db, user_id=current_user.user_id, action="skill_created", target_type="skill",
              target_id=skill.skill_id, target_key=skill.name, request=request,
              details=f"type={skill.type} version={skill.version}")
    db.commit()

    return ApiResponse(data=SkillResponse.model_validate(skill).model_dump())


@router.put("/skills/{skill_id}", response_model=ApiResponse)
async def update_skill(
    skill_id: int,
    skill_data: SkillUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
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

    new_name = str(skill.name)
    # T9批1: 时序=快照旧盘→写新盘→commit→成功后才删旧目录。
    # 原时序两处坑：commit 后写盘失败"回滚"是 no-op；改名先删旧目录、新盘再写失败=旧盘已毁 DB 未提交
    old_disk = read_skill_from_disk(old_type, old_name) if old_name == new_name else None
    try:
        write_skill_to_disk(
            skill_type=old_type,
            skill_name=new_name,
            content=str(skill.skill_content or ""),
            version=str(skill.version) if skill.version else None,
        )
    except (ValueError, RuntimeError) as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"SKILL.md 写盘失败（DB 已回滚）: {e}")
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        try:
            if old_name != new_name:
                remove_skill_from_disk(old_type, new_name)   # 新建的撤掉，旧目录本就未动
            elif old_disk is not None:
                write_skill_to_disk(old_type, old_name, old_disk)  # 恢复快照
        except Exception as de:
            import logging as _lg; _lg.getLogger(__name__).error("[update_skill] 盘回滚失败: %s", de)
        raise HTTPException(status_code=500, detail=f"DB 更新失败: {e}")

    # commit 成功后才清旧目录（改名场景；失败不致命——残留旧目录由 sweep/人工清）
    if old_name != new_name:
        try:
            remove_skill_from_disk(old_type, old_name)
        except RuntimeError as e:
            import logging
            logging.warning("改名时清旧目录失败: %s", e)

    db.refresh(skill)
    _ch = []
    if skill_data.name: _ch.append(f"name={skill_data.name}")
    if skill_data.description: _ch.append("desc")
    if skill_data.system_ids is not None: _ch.append(f"systems={skill_data.system_ids}")
    if skill_data.version: _ch.append(f"ver={skill_data.version}")
    if skill_data.is_active is not None: _ch.append(f"active={skill_data.is_active}")
    if skill_data.skill_content: _ch.append(f"content_len={len(skill_data.skill_content)}")
    log_audit(db, user_id=current_user.user_id, action="skill_updated", target_type="skill",
              target_id=skill_id, target_key=str(skill.name), request=request,
              details="changed:" + ",".join(_ch))
    db.commit()

    return ApiResponse(data=SkillResponse.model_validate(skill).model_dump())


@router.delete("/skills/{skill_id}", response_model=ApiResponse)
async def delete_skill(
    skill_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
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
    log_audit(db, user_id=current_user.user_id, action="skill_deleted", target_type="skill",
              target_id=skill_id, target_key=skill_name, request=request,
              details=f"type={skill_type} cascaded_experiences={related_count}")
    db.commit()
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
async def reconcile_skills(request: Request, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
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
    log_audit(db, user_id=current_user.user_id, action="skill_reconcile", target_type="skill",
              request=request, details=msg)
    return ApiResponse(message=msg, data=stats)


@router.get("/skills/trash", response_model=ApiResponse)
async def list_trash_skills(db: Session = Depends(get_db)):
    """回收站机制已下线（删 = 真删）。保留接口返空，兼容前端可能残留的菜单"""
    return ApiResponse(message="回收站机制已下线，删除即真删", data=[])


@router.get("/skills/{skill_id}", response_model=ApiResponse)
async def get_skill_detail(skill_id: int, db: Session = Depends(get_db)):
    """3e：单 Skill 详情（含正文大字段）——列表已 defer，编辑器/预览从这里取"""
    sk = db.query(Skill).filter(Skill.skill_id == skill_id).first()
    if not sk:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return ApiResponse(data={
        "skill_id": sk.skill_id, "name": sk.name, "type": sk.type,
        "description": sk.description,
        "system_ids": sk.system_ids if isinstance(sk.system_ids, list) else [],
        "version": sk.version, "is_active": sk.is_active, "source": sk.source,
        "skill_content": str(sk.skill_content or ""),
        "script_content": str(sk.script_content or ""),
        "deleted_at": sk.deleted_at.isoformat() if sk.deleted_at else None,
    })


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
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
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
    log_audit(db, user_id=current_user.user_id, action="system_created", target_type="system",
              target_id=system.system_id, target_key=system.name, request=request,
              details=f"base_image={system.base_image}")
    db.commit()

    return ApiResponse(data=TargetSystemResponse.model_validate(system).model_dump())


@router.put("/systems/{system_id}", response_model=ApiResponse)
async def update_system(
    system_id: int,
    system_data: TargetSystemUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
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
    log_audit(db, user_id=current_user.user_id, action="system_updated", target_type="system",
              target_id=system_id, target_key=system.name, request=request,
              details=f"fields={list(system_data.model_dump(exclude_unset=True).keys())}")
    db.commit()

    return ApiResponse(data=TargetSystemResponse.model_validate(system).model_dump())


@router.delete("/systems/{system_id}", response_model=ApiResponse)
async def delete_system(
    system_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
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
    
    log_audit(db, user_id=current_user.user_id, action="system_deleted", target_type="system",
              target_id=system_id, target_key=system.name, request=request,
              details="result=deleted")
    db.delete(system)
    db.commit()
    
    return ApiResponse(message="目标系统已删除")


# ==================== 用户管理 ====================

@router.get("/users", response_model=ApiResponse)
async def list_users(
    include_deleted: bool = False,
    db: Session = Depends(get_db),
):
    """获取用户列表（默认不显示已软删）"""
    query = db.query(User).order_by(User.created_at.desc())
    if not include_deleted:
        query = query.filter(User.deleted_at.is_(None))
    users = query.all()

    return ApiResponse(data=[
        {
            "user_id": u.user_id,
            "username": u.username,
            "original_username": u.original_username,  # 已软删用户回显真实名
            "role": u.role,
            "last_active": u.last_active.isoformat() if u.last_active else None,
            "is_active": u.is_active,
            "deleted_at": u.deleted_at.isoformat() if u.deleted_at else None,
        }
        for u in users
    ])


@router.put("/users/{user_id}/password", response_model=ApiResponse)
async def reset_user_password(
    user_id: int,
    body: UserPasswordReset,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """管理员重置指定用户密码（bcrypt rounds=12，与注册/改密一致）"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")

    target = db.query(User).filter(User.user_id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    # 预置 admin 保护（2026-10-28 口径修正）：仅系统预制 admin 账号口令只能本人
    # 重置（防任何其他人——含其他管理员——顶掉它接管）；其余用户（含 admin 角色）
    # 管理员皆可重置
    if target.username == "admin" and target.user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="预制 admin 账号只能本人重置密码")

    target.password_hash = bcrypt.hashpw(
        body.new_password.encode(),
        bcrypt.gensalt(rounds=12)
    ).decode()
    log_audit(db, user_id=current_user.user_id, action="admin_password_reset", target_type="user",
              target_id=user_id, target_key=target.username, request=request,
              details="result=success")   # 零密码痕迹（T10 假审计转正式行）
    db.commit()

    return ApiResponse(message=f"用户 {target.username} 密码已重置")


@router.post("/users", response_model=ApiResponse)
async def create_user(
    body: UserAdminCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """管理员新增用户"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")

    if db.query(User).filter(
        User.username == body.username,
        User.deleted_at.is_(None),  # 已软删的 username=deleted_* 不算占用（同名可重建）
    ).first():
        raise HTTPException(status_code=400, detail="用户名已被占用")

    user = User(
        username=body.username,
        password_hash=bcrypt.hashpw(body.password.encode(), bcrypt.gensalt(rounds=12)).decode(),
        role=body.role,
    )
    db.add(user)
    db.flush()
    log_audit(db, user_id=current_user.user_id, action="user_created", target_type="user",
              target_id=user.user_id, target_key=body.username, request=request,
              details=f"role={body.role}")
    db.commit()
    db.refresh(user)
    return ApiResponse(data={"user_id": user.user_id}, message=f"用户 {user.username} 已创建")


@router.put("/users/{user_id}", response_model=ApiResponse)
async def update_user(
    user_id: int,
    body: UserAdminUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """管理员编辑用户：改角色 / 启用禁用（也兼容前端原有的 is_active 开关）"""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")

    target = db.query(User).filter(User.user_id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    # 预置 admin 保护（2026-10-28 口径修正）：仅 admin 账号不可编辑（改角色=降权
    # 接管、禁用=全站锁死）；其他管理员角色行可编辑（delete 端点仍护所有 admin 角色）
    if target.username == "admin":
        raise HTTPException(status_code=403, detail="预制 admin 账号不允许编辑（保护策略）")

    if body.role is None and body.is_active is None:
        raise HTTPException(status_code=400, detail="没有需要修改的字段")

    # 最后一个 admin 不可被降级或禁用（防止平台失去管理员）
    admin_count = db.query(User).filter(User.role == "admin").count()
    losing_admin = (target.role == "admin") and (
        (body.role is not None and body.role != "admin") or (body.is_active is False)
    )
    if losing_admin and admin_count <= 1:
        raise HTTPException(status_code=400, detail="必须保留至少一个可用管理员，不能降级/禁用最后一个 admin")

    if body.role is not None:
        target.role = body.role
    if body.is_active is not None:
        target.is_active = body.is_active
    log_audit(db, user_id=current_user.user_id, action="user_updated", target_type="user",
              target_id=user_id, target_key=target.username, request=request,
              details=f"role={body.role} is_active={body.is_active}")
    db.commit()
    return ApiResponse(message=f"用户 {target.username} 已更新")


@router.delete("/users/{user_id}", response_model=ApiResponse)
async def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """管理员软删用户：保留 user 行 + 所有 task/ticket/exp/audit 引用，统计/历史不丢。
    处理：deleted_at=now + is_active=False + 改名 deleted_<uid>_<ts>（释放 unique）+ 清 password_hash"""
    if user_id == current_user.user_id:
        raise HTTPException(status_code=400, detail="不能删除自己")

    target = db.query(User).filter(User.user_id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.deleted_at is not None:
        raise HTTPException(status_code=400, detail="用户已注销")
    if target.role == "admin":
        raise HTTPException(status_code=400, detail=f"用户 {target.username} 是管理员，不可删除")

    now = datetime.utcnow()
    original_name = target.username
    # 改名避开 unique（同一用户名注销后能重新注册）
    target.username = f"deleted_{user_id}_{int(now.timestamp())}"
    target.original_username = original_name
    target.is_active = False
    target.deleted_at = now
    target.deleted_by = current_user.user_id
    # 清 password_hash 防 hash 复用登录
    target.password_hash = ""
    log_audit(db, user_id=current_user.user_id, action="user_deleted", target_type="user",
              target_id=user_id, target_key=original_name, request=request,
              details="mode=soft_delete")
    db.commit()

    return ApiResponse(message=f"用户 {original_name} 已注销（软删，可恢复）")


@router.post("/users/{user_id}/restore", response_model=ApiResponse)
async def restore_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """恢复已软删用户：用户名复位（同名冲突时给出明确报错）"""
    target = db.query(User).filter(User.user_id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.deleted_at is None:
        raise HTTPException(status_code=400, detail="用户未注销，无需恢复")

    restore_name = target.original_username or target.username
    # 冲突检查：同名的另一未删用户
    clash = db.query(User).filter(
        User.username == restore_name,
        User.deleted_at.is_(None),
        User.user_id != user_id,
    ).first()
    if clash:
        raise HTTPException(
            status_code=400,
            detail=f"用户名 {restore_name} 已被其他用户占用，无法恢复；请先改名"
        )

    target.username = restore_name
    target.original_username = None
    target.is_active = True
    target.deleted_at = None
    target.deleted_by = None
    # 密码留着空，让用户走"重置密码"流程
    log_audit(db, user_id=current_user.user_id, action="user_restored", target_type="user",
              target_id=user_id, target_key=restore_name, request=request,
              details="mode=soft_restore")
    db.commit()

    return ApiResponse(message=f"用户 {restore_name} 已恢复，请提醒其走'重置密码'流程")



# ============== 工单 Agent 多轮对话（admin 跟 agent 聊）=============
@router.post("/tickets/{ticket_id}/agent/accept")
async def accept_ticket_and_start_agent(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """接单（pending→processing） + 启动 agent session（建容器 + 拉源码 + DeepAgent）

    接单时一次 Skills 注入：session 创建时 skills 路径一次性加载到 agent。
    """
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.status == "closed":
        raise HTTPException(status_code=400, detail="工单已关闭")
    if ticket.status == "resolved":
        raise HTTPException(status_code=400, detail="工单已解决")
    if ticket.status != "pending":
        return ApiResponse(message=f"工单已在 {ticket.status} 状态")

    ticket.status = "processing"
    from app.models import AuditLog
    db.add(AuditLog(
        user_id=current_user.user_id,
        action="ticket_accept",
        target_type="ticket",
        target_id=ticket_id,
        details="pending→processing",
    ))
    db.commit()

    # 异步启动 agent（不阻塞 admin 立刻看到详情抽屉）
    import asyncio
    from app.services.ticket_agent import MANAGER
    from app.config import artifacts_dir as _artifacts_dir  # Q1b：公共权威，不再反向 import 引擎私有

    # 批2-①：同上；此现场旧版失败只 print 不落 _errors（admin 看不到错误），一并纠
    asyncio.create_task(MANAGER.start_agent_background(ticket_id))

    return ApiResponse(message="工单已接单，agent session 正在创建（容器 + 拉源码 + DeepAgent 启动）")


@router.get("/tickets/{ticket_id}/agent/status")
async def get_agent_status(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """查 agent session 状态（前端轮询）

    返回字段：
    - active: bool（session 是否建起）
    - container_id / created_at: session 起来时返
    - error: 启动失败的错误信息（admin 可见）
    - phase: 当前阶段（pending/build/ready/failed）
    """
    from app.services.ticket_agent import MANAGER
    from app.models import Ticket
    sess = MANAGER.get(ticket_id)
    err = MANAGER.get_error(ticket_id)
    if not sess:
        # 还没建起来或失败 — 区分 3 种情况给前端清晰提示
        if err:
            return ApiResponse(data={"active": False, "error": err, "phase": "failed"})
        # 看 ticket 当前状态判断 "session 丢失" 还是 "真的在建"
        from app.models import SessionLocal as _SL
        s = _SL()
        ticket = s.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
        s.close()
        if ticket and ticket.status == "processing":
            # 内存没有但工单在处理中 → DB 有现场（容器/checkpoint）→ 自动恢复，不再让用户手点"重新启动"
            # 幂等：get_or_create 内部有锁+双重检查；这里再挡并发触发（docker 慢，恢复一次要几十秒）
            import asyncio
            from app.services.ticket_agent import RESTORING
            if ticket_id not in RESTORING:
                RESTORING.add(ticket_id)
                async def _restore():
                    try:
                        ss = _SL()
                        t = ss.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
                        await MANAGER.get_or_create(t, ss)
                        ss.close()
                    except Exception as e:
                        import traceback; traceback.print_exc()
                        MANAGER._errors[ticket_id] = f"{type(e).__name__}: {e}"
                    finally:
                        RESTORING.discard(ticket_id)
                asyncio.create_task(_restore())
            return ApiResponse(data={
                "active": False,
                "phase": "restoring",
                "error": "正在自动恢复 agent 现场（复用容器 + 接回对话），请稍候…",
            })
        # 还没接单 / 正在建
        return ApiResponse(data={"active": False, "phase": "starting"})
    return ApiResponse(data={
        "active": True,
        "container_id": sess.container_id[:12],
        "created_at": sess.created_at,
        "phase": "ready",
    })


@router.get("/tickets/{ticket_id}/agent/history")
async def get_agent_history(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """拉工单对话历史（含 admin / agent / tool / user 角色）"""
    from app.models import TicketMessage
    msgs = db.query(TicketMessage).filter(
        TicketMessage.ticket_id == ticket_id
    ).order_by(TicketMessage.created_at.asc()).all()
    return ApiResponse(data=[
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in msgs
    ])


@router.get("/tickets/{ticket_id}/agent/last-response")
async def get_agent_last_response(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """拉最近一次 stream 完成的 agent 响应（供前端 abort 后兜底轮询）

    返回字段：
    - running: bool（stream 还在跑 → 前端继续轮询）
    - response: str | null（agent 最后响应内容；stream 没跑过返 None）
    - tool_calls: list（最近一轮的工具调用记录）
    - agent_unavailable: bool（兼容前端 field）
    """
    from app.services.ticket_agent import MANAGER
    sess = MANAGER.get(ticket_id)
    if not sess:
        return ApiResponse(data={"running": False, "response": None, "tool_calls": [], "agent_unavailable": False})
    return ApiResponse(data={
        "running": bool(sess.agent_state.get("_stream_running", False)),
        "response": sess.agent_state.get("_last_response"),
        "tool_calls": sess.agent_state.get("_last_tool_calls", []),
        "agent_unavailable": False,
    })


# ============== 工单 Agent 多轮对话（admin 跟 agent 聊）=============
def _b3_stamp_ts(new_msgs: list, old_json: str | None) -> None:
    """T9-U 2026-10-23: B3 每条消息补 ts（ISO）。B3 每轮全量重建 state 消息，
    老消息必须继承上一版里同位置的 ts（首见时刻才可信）；新出现的打当前时刻。"""
    from datetime import datetime as _dt
    old_list = []
    try:
        import json as _j
        _parsed = _j.loads(old_json) if old_json else []
        # T9-U: 轮中 partial 快照形状 {"messages":[..], "_partial":..} → 解内层列表再继承
        if isinstance(_parsed, dict):
            _parsed = _parsed.get("messages") or []
        old_list = _parsed if isinstance(_parsed, list) else []
    except Exception:
        old_list = []
    # naive UTC 全链纪律（10-23 时区三踩教训）：曾用 _dt.now()（本地+08）打戳，
    # 与 TicketMessage.created_at(utcnow) 同屏差 8h——工单 87 时间线错乱实锤
    now_iso = _dt.utcnow().isoformat()
    for i, m in enumerate(new_msgs):
        if i < len(old_list) and old_list[i].get("type") == m.get("type") and old_list[i].get("ts"):
            m["ts"] = old_list[i]["ts"]        # 同位置同类型=同一条消息，继承首见时刻
        else:
            m["ts"] = now_iso                  # 新消息（或历史无 ts 的旧行）打当前时刻


@router.get("/tickets/{ticket_id}/agent/messages-history")
async def get_agent_messages_history(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """B3 2026-10-19：拉工单 agent 历史消息（从 ticket_agents.last_messages_json）

    返回字段：
    - messages: list[dict]（type/content/tool_calls/tool_call_id）—— agent/tool 消息
    - source: "db" | "empty"（db 没存历史返空）
    """
    from app.models import TicketAgent as _TAgent, TicketMessage
    import json as _json
    rec = db.query(_TAgent).filter(_TAgent.ticket_id == ticket_id).first()

    # T9-U 读时归一（10-23 时区修复）：存量 B3 ts 是 _b3_stamp_ts 用 datetime.now()
    # （本地 +08）打的，与 created_at(naive UTC) 混源显示差 8h。用 human↔TicketMessage
    # 配对偏移做系统性探测（6h<off<10h 判为本地戳）：命中则整批平移回 UTC，
    # human 本身以 created_at 为权威覆盖。不写回 DB——读时纯函数，幂等。
    # （比"整批同 ts"探测更通用：覆盖每轮增量保存的混合 ts，如工单 87）
    _partial_info = None
    msgs_raw = _json.loads(str(rec.last_messages_json)) if rec and rec.last_messages_json else []
    # T9-U 轮中快照形状 {"messages":[...], "_partial":{...}}：拆出进度标记，按列表走归一逻辑
    if isinstance(msgs_raw, dict):
        _partial_info = msgs_raw.get("_partial")
        msgs_raw = msgs_raw.get("messages") or []
    if not isinstance(msgs_raw, list):
        msgs_raw = []
    if msgs_raw:
        _adm = db.query(TicketMessage).filter(
            TicketMessage.ticket_id == ticket_id, TicketMessage.role == "admin",
            TicketMessage.kind == "agent_cmd").order_by(TicketMessage.id).all()
        _used, _pair = set(), []
        for _m in msgs_raw:
            if _m.get("type") != "human":
                continue
            _c = (_m.get("content") or "").strip()[:60]
            for _a in _adm:
                if _a.id in _used:
                    continue
                if (_a.content or "").strip()[:60] == _c:
                    _used.add(_a.id)
                    _pair.append((_m, _a))
                    break
        if _pair:
            from datetime import datetime as _dt2, timedelta as _td
            _offs = []
            for _m, _a in _pair:
                if _m.get("ts") and _a.created_at:
                    try:
                        _offs.append(_dt2.fromisoformat(_m["ts"]) - _a.created_at)
                    except ValueError:
                        pass
            _local = [o for o in _offs if _td(hours=6) < o < _td(hours=10)]
            if _local:
                _fix = sum(_local, _td()) / len(_local)
                for _m in msgs_raw:
                    if _m.get("ts"):
                        try:
                            _m["ts"] = (_dt2.fromisoformat(_m["ts"]) - _fix).isoformat()
                        except ValueError:
                            pass
            for _m, _a in _pair:
                if _a.created_at:
                    _m["ts"] = _a.created_at.isoformat()  # human 权威时刻
    if not rec or not rec.last_messages_json:
        return ApiResponse(data={"messages": [], "source": "empty"})
    try:
        return ApiResponse(data={"messages": msgs_raw, "source": "db", "partial": _partial_info})
    except Exception as e:
        return ApiResponse(data={"messages": [], "source": "parse_error", "error": str(e)})


@router.get("/tickets/{ticket_id}/agent/phase")
async def get_agent_phase(
    ticket_id: int,
    since_seq: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """2026-10-19 方案 2：拉工单调试实时阶段（前端轮询用）

    返回字段：
    - events: list[{phase, detail, ts, seq}]（seq > since_seq 的新事件，按 seq 升序）
    - latest_seq: int（当前最大 seq）
    - done: bool（session 已建完，deep_agent_ready 之后）
    - error: str | None（启动失败信息）
    """
    from app.services.ticket_agent import MANAGER
    sess = MANAGER.get(ticket_id)
    err = MANAGER.get_error(ticket_id)
    if not sess:
        return ApiResponse(data={
            "events": [],
            "latest_seq": 0,
            # 无 session 且无 error = 没有可等的启动过程（pending 查看 / resolve·close 后 session 已移除），
            # done=True 让前端退出轮询；接单启动期 session 从 starting 起就存在于 _sessions，不受影响
            "done": err is None,
            "error": err,
        })
    events = [e for e in sess.phase_events if e.get("seq", 0) > since_seq]
    # done: 看到 deep_agent_ready + session active
    has_ready = any(e["phase"] == "deep_agent_ready" for e in sess.phase_events)
    return ApiResponse(data={
        "events": events,
        "latest_seq": sess._phase_seq,
        "done": has_ready,
        "error": err,
    })


@router.post("/tickets/{ticket_id}/agent/message")
async def post_agent_message(
    ticket_id: int,
    content: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """admin 发送一条消息给 agent → agent 内部 state 累积多轮（不入库）

    返回：本轮 agent 响应（前端 localChat 显示）
    """
    import time as _time
    _t0 = _time.time()
    _marker = f"[post_agent_message] ticket={ticket_id}"
    print(f"{_marker} === ENTER === admin_msg_len={len(content.strip())} content={content.strip()[:60]!r}", flush=True)

    from app.models import AuditLog
    if not content or not content.strip():
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    from app.services.ticket_agent import MANAGER
    sess = MANAGER.get(ticket_id)
    if not sess:
        print(f"{_marker} !!! no session, abort 400", flush=True)
        # A 方案：顺手触发自动恢复（内存没 session 但 DB 有现场 → 后台重建），前端提示稍后再发
        ticket_now = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
        if ticket_now and str(getattr(ticket_now.status, "value", ticket_now.status)) == "processing":
            import asyncio
            async def _kick_restore():
                try:
                    from app.services.ticket_agent import RESTORING
                    if ticket_id in RESTORING:
                        return
                    RESTORING.add(ticket_id)
                    try:
                        await MANAGER.get_or_create(ticket_now, db)
                    finally:
                        RESTORING.discard(ticket_id)
                except Exception as e:
                    MANAGER._errors[ticket_id] = f"{type(e).__name__}: {e}"
            asyncio.create_task(_kick_restore())
            raise HTTPException(status_code=400, detail="agent 现场恢复中（后端重启过，自动重建约 30-90 秒），请稍候再发消息")
        raise HTTPException(status_code=400, detail="agent session 未启动，请先接单")

    # T9-U 停止功能·缺口 B 防御：上一轮仍标记 running（含"停止中"未收敛窗口）→ 拒绝并发开新轮
    if sess.agent_state.get("_stream_running"):
        raise HTTPException(status_code=409, detail="上一轮仍在进行或停止收敛中，请稍候再发（或点『停止』）")
    sess.stop_event.clear()  # 清上一轮停止旗标，防污染本轮
    # 存 admin 消息（用户/管理员互相对话需要持久化；agent 内部消息不存）
    from app.models import TicketMessage
    admin_msg = TicketMessage(
        ticket_id=ticket_id,
        user_id=current_user.user_id,
        role="admin",
        content=content.strip(),
        kind="agent_cmd",  # T8: 工作台指令线，用户端不可见
        is_read=True,
    )
    db.add(admin_msg)
    db.commit()
    db.refresh(admin_msg)

    # 调 agent：agent 内部用 langgraph state 保留多轮上下文，不入库
    from langchain_core.messages import HumanMessage, AIMessage
    sess.agent_state.setdefault("_messages", [])

    full_response = ""
    tool_calls_log = []
    agent_unavailable = False
    try:
        # 用 stream + values 模式（与 stage5 完全相同的调用方式 —— 用户编译任务的 agent 用的就是这个）
        # langgraph state 自动累积多轮对话（InMemorySaver + thread_id config）
        print(f"{_marker} >>> agent.stream START (elapsed={_time.time()-_t0:.2f}s)", flush=True)
        # 标记 stream 正在跑（前端 abort 后可轮询 last-response 兜底）
        sess.agent_state["_stream_running"] = True
        last_msgs = []
        _chunk_n = 0
        _stopped = [False]
        # A3 2026-10-19：把同步 stream 扔到默认 ThreadPoolExecutor，不阻塞 uvicorn event loop
        # 参考 stage5 在 lifespan 线程池里跑的方式，但工单路径直接用 default executor（独立于 stage5 的 8 worker）
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()

        def _run_stream():
            """同步跑 stream，收集所有 chunk + last_msgs。返回 (_chunk_n, last_msgs)"""
            nonlocal _chunk_n, last_msgs
            try:
                for chunk in sess.agent.stream(
                    {"messages": [{"role": "user", "content": content.strip()}]},
                    config=sess.config,  # 用 session 的 thread_id config（多轮累积）
                    stream_mode="values",
                ):
                    if sess.stop_event.is_set():
                        _stopped[0] = True
                        break
                    _chunk_n += 1
                    msgs = chunk.get("messages", []) if isinstance(chunk, dict) else []
                    if msgs:
                        last_msgs = msgs
            except Exception as e:
                # 把异常 re-raise 到 async 端 catch
                raise
            return _chunk_n, last_msgs

        try:
            _chunk_n, last_msgs = await loop.run_in_executor(None, _run_stream)
        except AssertionError:
            raise  # 让外层 except AssertionError 抓
        except Exception:
            raise
        print(f"{_marker} <<< agent.stream END chunks={_chunk_n} last_msgs_count={len(last_msgs)} (elapsed={_time.time()-_t0:.2f}s) stopped={_stopped[0]}", flush=True)
        # 标记 stream 跑完（_last_response 等取完 last_msgs 再写）
        sess.agent_state["_stream_running"] = False
        if _stopped[0]:
            # 缺口 A 收口：停止轮直接转正（纯 list，dict-partial 灰条不会假"进行中"）
            try:
                _persist_round(ticket_id, content, "（本轮已被管理员停止；现场与已完成步骤保留）", [], current_user.user_id, stopped=True)
            except Exception as _pe:
                print(f"{_marker} persist-after-stop fail: {_pe}", flush=True)
        # 取最后一条 AIMessage 当 agent 响应（不要取 HumanMessage / ToolMessage）
        for m in last_msgs:
            # AIMessage 在 langchain 是 langchain_core.messages.AIMessage
            from langchain_core.messages import AIMessage as _AIMsg
            if isinstance(m, _AIMsg):
                if isinstance(m.content, str):
                    full_response = m.content
                if m.tool_calls:
                    for tc in m.tool_calls:
                        tool_calls_log.append({
                            "tool": tc.get("name", "?"),
                            "input": tc.get("args", {}),
                        })
        # 取完再写（给前端 abort 兜底轮询用）
        sess.agent_state["_last_response"] = full_response
        sess.agent_state["_last_tool_calls"] = tool_calls_log
        print(f"{_marker} agent_response_len={len(full_response)} tool_calls={len(tool_calls_log)} unavailable={agent_unavailable}", flush=True)
        print(f"{_marker} agent_response_preview={full_response[:120]!r}", flush=True)
    except AssertionError as e:
        # deepagents 0.x + langchain 1.0+ 兼容 bug（_intercept_large_tool_result）
        agent_unavailable = True
        sess.agent_state["_stream_running"] = False
        sess.agent_state["_last_response"] = full_response = (
            f"⚠️ Agent 工具调用暂时不可用（deepagents 0.x 与 langchain 1.0+ 兼容问题）\n\n"
            f"错误：{e}\n\n"
            f"你可以先以 LLM 答疑方式继续对话（admin 提问，agent 凭 SKILL.md 文字答），"
            f"但 agent 不能在容器内真实跑命令 / 改源码。\n\n"
            f"解决方案：等 deepagents 升级 / 切换到 langchain 兼容版本。"
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        full_response = f"[agent 执行出错: {type(e).__name__}: {e}]"
        sess.agent_state["_stream_running"] = False
        sess.agent_state["_last_response"] = full_response

    # agent 消息 / tool_call 消息不写 DB（按产品决策：agent 自己保留上下文，不入库）
    # 仅写 audit_log 留痕
    db.add(AuditLog(
        user_id=current_user.user_id,
        action="ticket_agent_message",
        target_type="ticket",
        target_id=ticket_id,
        details=f"admin→agent: {content.strip()[:100]} | agent→{len(tool_calls_log)}tools | response_len={len(full_response)}",
    ))
    db.commit()

    # 持久化 checkpoint 到 DB（跨进程/重启恢复多轮对话）
    MANAGER.save_checkpoint(ticket_id, db)

    # B3 2026-10-19：把 last_msgs 序列化成 JSON 存 ticket_agents.last_messages_json
    # 让 admin 关闭再打开抽屉时能看到 agent 历史（解决"退出再进 agent 消息丢"问题）
    # 遵循 §62 决策：agent/tool 消息不入库 ticket_messages（user_id NOT NULL 约束），
    # 而是用 ticket_agents 表单独存 JSON blob（不污染 DB ticket_messages）
    try:
        from app.models import TicketAgent as _TAgent
        import json as _json
        from langchain_core.messages import (
            AIMessage as _AIM, HumanMessage as _HM, ToolMessage as _TM
        )
        # 序列化 last_msgs → JSON-friendly
        msgs_for_db = []
        for m in last_msgs:
            entry = {"type": getattr(m, "type", None) or "unknown"}
            content = getattr(m, "content", None)
            entry["content"] = content if isinstance(content, str) else str(content or "")
            tool_calls = getattr(m, "tool_calls", None)
            if tool_calls:
                entry["tool_calls"] = [
                    {"name": tc.get("name", "?"), "args": tc.get("args", {})}
                    for tc in tool_calls
                ]
            tool_call_id = getattr(m, "tool_call_id", None)
            if tool_call_id:
                entry["tool_call_id"] = tool_call_id
            msgs_for_db.append(entry)
        _oldj = None
        _rec0 = db.query(_TAgent).filter(_TAgent.ticket_id == ticket_id).first()
        if _rec0 and _rec0.last_messages_json:
            _oldj = str(_rec0.last_messages_json)
        _b3_stamp_ts(msgs_for_db, _oldj)  # T9-U: 交错时间线需要每条带 ts
        agent_rec = _rec0
        if agent_rec:
            agent_rec.last_messages_json = _json.dumps(msgs_for_db, ensure_ascii=False)
            db.commit()
            print(f"{_marker} B3: saved {len(msgs_for_db)} messages to last_messages_json", flush=True)
    except Exception as e:
        # B3 存失败不阻塞主流程
        print(f"{_marker} B3 save warning: {type(e).__name__}: {e}", flush=True)
        try: db.rollback()
        except: pass

    print(f"{_marker} === EXIT === returning ApiResponse (total_elapsed={_time.time()-_t0:.2f}s)", flush=True)
    return ApiResponse(
        data={
            "admin_msg_id": admin_msg.id,
            "agent_response": full_response,
            "tool_calls": tool_calls_log,
            "agent_active": True,
            "agent_unavailable": agent_unavailable,  # True → 前端显示"agent 不可用"提示
        },
        message="agent 工具不可用（仅 LLM 答疑）" if agent_unavailable else "agent 已回复",
    )


def _b3_serialize_msgs(state_msgs: list) -> list:
    """langgraph 消息对象 → B3 存储条目（轮末与轮中 partial 快照共用，序列化形状单一权威）"""
    out = []
    for m in state_msgs:
        entry = {"type": getattr(m, "type", None) or "unknown"}
        c = getattr(m, "content", None)
        entry["content"] = (c if isinstance(c, str) else str(c or ""))[:4000]
        tcs = getattr(m, "tool_calls", None)
        if tcs:
            entry["tool_calls"] = [
                {"name": tc.get("name", "?"),
                 "id": tc.get("id"),
                 "args": {k: str(v)[:600] for k, v in (tc.get("args") or {}).items()}}
                for tc in tcs
            ]
        tcid = getattr(m, "tool_call_id", None)
        if tcid:
            entry["tool_call_id"] = tcid
        mname = getattr(m, "name", None)
        if mname:
            entry["name"] = mname
        out.append(entry)
    return out


def _b3_partial_snapshot(ticket_id: int, snapshot: dict) -> None:
    """T9-U 2026-10-23 轮中增量快照：一轮 agent 对话跑到一半时（工具调用/结果每有新增，
    且距上次 ≥20s）把进度写 last_messages_json——形状 {"messages":[...], "_partial":{...}}，
    前端识别 _partial 渲染"⏳ 本回合进行中"。轮末 _persist_round 用纯 list 全量覆盖=转正，
    单字段单权威账。旧读取方遇 dict 形状按空史处理（fail-soft，绝不崩）。
    动机（用户报）：中途退出重进，这轮在 DB 里完全隐身——B3 只有轮末两个写点，
    轮中历史只在内存；后端重启则这轮彻底蒸发。"""
    from app.models import SessionLocal as _SL, TicketAgent as _TA
    s = _SL()
    try:
        rec = s.query(_TA).filter(_TA.ticket_id == ticket_id).first()
        if rec:
            rec.last_messages_json = json.dumps(snapshot, ensure_ascii=False)
            s.commit()
    except Exception as se:
        try:
            s.rollback()
        except Exception:
            pass
        _log.debug("[ticket=%s] partial 快照失败(不影响主流程): %s", ticket_id, se)
    finally:
        s.close()


def _persist_round(ticket_id: int, content: str, full_response: str,
                   tool_calls_log: list, user_id: int, stopped: bool = False) -> None:
    """SSE stream 收尾持久化：checkpoint + agent 历史 + 审计（在 worker 线程调用，
    与 HTTP 连接生死无关 —— 客户端断开也不丢本轮）。失败仅记日志不抛。"""
    from app.models import SessionLocal, TicketAgent as _TAgent, AuditLog as _AL
    from app.services.ticket_agent import MANAGER
    _log = logging.getLogger("ticket_agent_stream")
    s = SessionLocal()
    try:
        MANAGER.save_checkpoint(ticket_id, s)
        # B3：agent 完整消息序列（含 ToolMessage 结果 → 重开抽屉时穿插显示）存 JSON blob
        sess = MANAGER.get(ticket_id)
        if sess and sess.config:
            state_msgs = []
            try:
                st = sess.agent.get_state(sess.config)
                state_msgs = (st.values or {}).get("messages", []) if st else []
            except Exception as ge:
                _log.warning(f"[ticket={ticket_id}] get_state for B3 失败: {ge}")
            if state_msgs:
                msgs_for_db = _b3_serialize_msgs(state_msgs)
                rec = s.query(_TAgent).filter(_TAgent.ticket_id == ticket_id).first()
                if rec:
                    _b3_stamp_ts(msgs_for_db, str(rec.last_messages_json) if rec.last_messages_json else None)
                    rec.last_messages_json = json.dumps(msgs_for_db, ensure_ascii=False)
                    _log.info(f"[ticket={ticket_id}] B3: saved {len(msgs_for_db)} msgs to last_messages_json")
        s.add(_AL(
            user_id=user_id,
            action="ticket_agent_message_stream",
            target_type="ticket",
            target_id=ticket_id,
            details=f"stream: {content.strip()[:100]} | tools={len(tool_calls_log)} | len={len(full_response)}" + (" | STOPPED-by-admin" if stopped else ""),
        ))
        s.commit()
        _log.info(f"[ticket={ticket_id}] [persist] round saved (checkpoint+B3)")
    except Exception as e:
        _log.warning(f"[ticket={ticket_id}] persist 失败: {type(e).__name__}: {e}")
        try: s.rollback()
        except Exception: pass
    finally:
        s.close()


@router.post("/tickets/{ticket_id}/agent/stream")
async def stream_agent_message(
    ticket_id: int,
    content: str,
    db: Session = Depends(get_db),  # 用于鉴权，不在 generator 内用
    current_user: User = Depends(get_current_user),
):
    """流式端点：admin 跟 agent 多轮对话 —— SSE 边想边打字

    SSE 事件类型（data: {"type": "...", ...}）：
    - "delta"：AI 输出 token 增量（content 字段是文字片段）
    - "tool"：agent 调工具（tool + input 字段）
    - "done"：流结束（response 字段是完整 agent_response）
    - "error"：出错（message 字段）
    """
    from fastapi.responses import StreamingResponse
    from app.models import TicketMessage, AuditLog
    from app.services.ticket_agent import MANAGER
    from langchain_core.messages import AIMessage as _AIMsg
    # 2026-10-19：静 httpx 噪音（每个 LLM HTTP 请求 1 行 INFO 太啰嗦）
    for _h in ("httpx", "httpx2", "httpcore", "httpcore2", "openai", "openai2"):
        logging.getLogger(_h).setLevel(logging.WARNING)
    _log = logging.getLogger("ticket_agent_stream")  # 命名 logger 方便 grep
    _log.setLevel(logging.INFO)

    sess = MANAGER.get(ticket_id)
    if not sess:
        raise HTTPException(status_code=400, detail="agent session 未启动")

    # 2026-10-19：结构化 log（"记录每次发了什么，收了什么"）
    _log.info(f"[ticket={ticket_id}] [admin→agent] user_msg: {content.strip()[:200]!r}")
    # T9-U 停止功能·缺口 B 防御（SSE 入口同构）
    if sess.agent_state.get("_stream_running"):
        raise HTTPException(status_code=409, detail="上一轮仍在进行或停止收敛中，请稍候再发（或点『停止』）")
    sess.stop_event.clear()

    async def event_gen():
        # 1) 存 admin 消息
        from app.models import SessionLocal
        s = SessionLocal()
        try:
            admin_msg = TicketMessage(
                ticket_id=ticket_id,
                user_id=current_user.user_id,
                role="admin",
                content=content.strip(),
                kind="agent_cmd",  # T8: 工作台指令线，用户端不可见
                is_read=True,
            )
            s.add(admin_msg)
            s.add(AuditLog(
                user_id=current_user.user_id,
                action="ticket_agent_message_stream_start",
                target_type="ticket",
                target_id=ticket_id,
                details=content.strip()[:100],
            ))
            s.commit()
            admin_msg_id = admin_msg.id
        finally:
            s.close()

        yield f"data: {json.dumps({'type': 'admin_msg_id', 'id': admin_msg_id})}\n\n"

        # 2) 流式跑 agent —— 2026-10-19 方案 B：worker 线程 + asyncio.Queue
        # 关键：agent.stream() 是同步 generator，**在 async def 里直接调会阻塞 event loop**。
        # 修法：run_in_executor 扔到 ThreadPoolExecutor，worker 把每个 chunk 推 asyncio.Queue。
        # event_gen 协程 await queue.get() 拿 chunk，不阻塞 event loop。
        # 参考 stage5 编译任务 BROADCASTER 模式（ws_broadcaster.py）。
        import asyncio as _asyncio
        from starlette.requests import Request as _Req
        _q: _asyncio.Queue = _asyncio.Queue(maxsize=256)  # 临时 queue 跨 thread
        full_response = ""
        tool_calls_log = []
        agent_unavailable = False
        _t0 = time.time()
        _n_llm_chunks = 0

        def _stream_worker():
            """worker thread：同步跑 agent.stream()，每 chunk 推 _q
            2026-10-19 修 tool 碎片刷屏：langchain stream_mode="messages" 把一次 tool_call
            按 token 拆成几十个 chunk（name 先出、args 后出）。之前把每个 chunk 都当一次
            调用推 → UI 刷屏 [execute] {} [] {}，看着像死循环（实际 14 次调用被推成几百条）。
            现在：按 tool_call_chunks 的 index 累积 args 分块，args JSON 完整才推 1 次。
            finish_reason 时兜底 flush（id 缺失/JSON 不完整的也不丢）。
            """
            nonlocal full_response, tool_calls_log, agent_unavailable, _n_llm_chunks
            # T9-U：SSE 路径此前从不置 _stream_running —— 409 并发防御/停止收敛/abort 轮询
            # 都以此字段为唯一"在跑"判据，补齐与 /agent/message 路径同构
            sess.agent_state["_stream_running"] = True
            _tool_count: dict[str, int] = {}  # 限速统计（现在按"真实调用次数"计）
            _n_dropped = 0  # 修 QueueFull：丢的 chunk 数（debug 用）
            _tc_acc: dict = {}        # index → {id, name, args_str}（分块累积）
            _tc_emitted: set = set()  # 已推过的 tool_call id（去重）

            # T9-U 轮中快照：每有 tool 进展且距上次 ≥20s，把 langgraph state 已成形消息
            # 以 {"messages":[...], "_partial":{...}} 形状写 B3（轮末 _persist_round 覆盖=转正）。
            # get_state 只读内存 checkpointer，毫秒级；失败静默（下轮再来）。
            _snap_ts = [0.0]
            def _maybe_snapshot():
                _now = time.time()
                if _now - _snap_ts[0] < 20:
                    return
                _snap_ts[0] = _now
                try:
                    st = sess.agent.get_state(sess.config)
                    _msgs = _b3_serialize_msgs((st.values or {}).get("messages", []) if st else [])
                    if _msgs:
                        _b3_partial_snapshot(ticket_id, {
                            "messages": _msgs,
                            "_partial": {"round_started_at": datetime.utcnow().isoformat(),
                                         "admin_content": content.strip()[:500]},
                        })
                except Exception as _se:
                    _log.debug("[ticket=%s] partial 快照异常(跳过): %s", ticket_id, _se)

            def _safe_put(item):
                """put_nowait 满则吞掉 + 计数（不抛 QueueFull 把 worker 弄崩）"""
                nonlocal _n_dropped
                try:
                    _q.put_nowait(item)
                except Exception:
                    _n_dropped += 1
                    if _n_dropped in (1, 10, 100, 1000):
                        _log.warning(f"[ticket={ticket_id}] queue 满，丢 {_n_dropped} chunk（客户端慢/断）")

            def _emit_tool(_name, _args, _tc_id=None):
                """一次真实 tool_call → 1 个 friendly 事件 + 1 条 log（带 id 供前端关联 result）"""
                _cmd = _args.get("command", "") if isinstance(_args, dict) else str(_args)[:80]
                if not _cmd and isinstance(_args, dict):
                    _cmd = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(_args.items())[:3])
                _tool_count[_name or "?"] = _tool_count.get(_name or "?", 0) + 1
                _cnt = _tool_count[_name or "?"]
                if _cnt == 1:
                    _log.info(f"[ticket={ticket_id}] [TOOL] {_name} ({str(_cmd)[:100]})")
                elif _cnt == 5:
                    _log.warning(f"[ticket={ticket_id}] [TOOL] {_name} 重复调用 ≥5 次（可能 agent 绕圈）")
                tool_calls_log.append({"tool": _name, "input": _args})
                _safe_put({
                    "type": "tool",
                    "id": _tc_id,
                    "tool": _name,
                    "input": _args,
                    "content": f"[{_name or '?'}] {_cmd[:120]}" if _cmd else f"[{_name or '?'}] (无参数)",
                })
                _maybe_snapshot()
            _stopped = [False]  # T9-U 停止功能：admin 点停 → chunk 边界 break
            try:
                for chunk in sess.agent.stream(
                    {"messages": [{"role": "user", "content": content.strip()}]},
                    config=sess.config,
                    stream_mode="messages",
                ):
                    if sess.stop_event.is_set():
                        _stopped[0] = True
                        _log.info(f"[ticket={ticket_id}] [STOP] 收到停止信号，break on chunk 边界 (chunks={_n_llm_chunks})")
                        break
                    _n_llm_chunks += 1
                    if isinstance(chunk, tuple) and len(chunk) >= 1:
                        msg = chunk[0]
                    elif isinstance(chunk, dict):
                        msg = chunk.get("messages", [None])[-1] if chunk.get("messages") else None
                    else:
                        continue
                    if msg is None:
                        continue
                    # AI 输出 token
                    if isinstance(msg, _AIMsg):
                        if isinstance(msg.content, str) and msg.content:
                            full_response += msg.content
                            _safe_put({"type": "delta", "content": msg.content})
                        # —— tool_call 按 id 去重累积，完整才推 1 次 ——
                        tccs = getattr(msg, "tool_call_chunks", None) or []
                        for tcc in tccs:
                            idx = tcc.get("index") if tcc.get("index") is not None else 0
                            slot = _tc_acc.setdefault(idx, {"id": None, "name": "", "args_str": ""})
                            if tcc.get("id"):
                                slot["id"] = tcc["id"]
                            if tcc.get("name"):
                                slot["name"] = tcc["name"]  # name 只在首块出现，直接赋值
                            if tcc.get("args"):
                                slot["args_str"] += tcc["args"]
                            # id 已知、args_str 非空、且能解析成完整 JSON → 推 1 次
                            # （关键：args_str 为空时不能当 "{}" 提前推——那才是碎片来源）
                            if slot["id"] and slot["args_str"] and slot["id"] not in _tc_emitted:
                                try:
                                    parsed = json.loads(slot["args_str"])
                                except json.JSONDecodeError:
                                    continue
                                _tc_emitted.add(slot["id"])
                                _emit_tool(slot["name"], parsed, slot["id"])
                        # finish_reason 兜底：flush 未推的 slot（id 缺失或 args 不完整的）
                        rm = getattr(msg, "response_metadata", None) or {}
                        finish = rm.get("finish_reason") if isinstance(rm, dict) else None
                        if finish in ("tool_calls", "stop"):
                            for slot in _tc_acc.values():
                                key = slot["id"] or f"noflag:{slot['name']}:{slot['args_str'][:80]}"
                                if key in _tc_emitted:
                                    continue
                                _tc_emitted.add(key)
                                try:
                                    parsed = json.loads(slot["args_str"] or "{}")
                                except json.JSONDecodeError:
                                    parsed = {"command": slot["args_str"][:200]}
                                _emit_tool(slot["name"], parsed, slot["id"])
                            _tc_acc.clear()
                        # 注：不加 "if not tccs and msg.tool_calls" 兜底——实测
                        # langchain 每个含 tcc 的 AIMessage 都附带"合并至今"的 msg.tool_calls
                        # （args 不完整时为 {}），且流结束后会重发一个**完整**消息；
                        # finish_reason flush 已保证不丢，重复通道只会造成碎片二次推。
                    elif getattr(msg, "type", None) == "tool":
                        # 2026-10-19：ToolMessage 工具结果推给前端（穿插在 tool 气泡后）
                        # 之前"不发避免噪音"导致 UI 只见调用不见输出
                        _tout = msg.content if isinstance(msg.content, str) else str(msg.content or "")
                        _safe_put({
                            "type": "tool_result",
                            "name": getattr(msg, "name", "") or "",
                            "tool_call_id": getattr(msg, "tool_call_id", "") or "",
                            "content": _tout[:1200],
                        })
                        _maybe_snapshot()
            except AssertionError as e:
                agent_unavailable = True
                err = f"⚠️ Agent 工具调用暂时不可用：{e}"
                full_response = err
                _log.warning(f"[ticket={ticket_id}] [agent] AssertionError: {e}")
                _safe_put({"type": "error", "message": err})
            except Exception as e:
                import traceback as _tb
                _tb.print_exc()
                err = f"[agent 执行出错: {type(e).__name__}: {e}]"
                full_response = err
                _log.error(f"[ticket={ticket_id}] [agent] Exception: {type(e).__name__}: {e}")
                _safe_put({"type": "error", "message": err})
            finally:
                # sentinel：标记流结束
                # tool 调用统计（哪些调了 N 次）—— 现在是真实次数（之前是碎片次数）
                _tool_summary = ", ".join(f"{k}×{v}" for k, v in sorted(_tool_count.items(), key=lambda x: -x[1])[:5])
                sess.agent_state["_stream_running"] = False
                if _stopped[0]:
                    _safe_put({"type": "stopped", "message": "已停止（本轮部分内容已保留）"})
                    if not full_response:
                        full_response = "（本轮已被管理员停止；已完成的步骤保留在会话现场，收到新指令后从上一完整状态继续）"
                _safe_put(None)
                _log.info(f"[ticket={ticket_id}] [stream done] elapsed={time.time()-_t0:.2f}s chunks={_n_llm_chunks} tools={len(tool_calls_log)} [{_tool_summary}] resp_len={len(full_response)} agent_unavailable={agent_unavailable} dropped={_n_dropped}")
                # 2026-10-20：持久化挪进 worker 线程收尾（原先在 event_gen 协程里，
                # 客户端断连 → 协程被 cancel → checkpoint/历史不落库，"agent 跑完了但
                # 回来看不见这轮回复"）。sentinel 先发不卡 UI，DB 写慢无所谓。
                try:
                    _persist_round(ticket_id, content, full_response, tool_calls_log,
                                   current_user.user_id, stopped=_stopped[0])
                except Exception as pe:
                    _log.error(f"[ticket={ticket_id}] persist-round 失败: {type(pe).__name__}: {pe}")

        # 在默认 ThreadPoolExecutor 跑 worker（不阻塞 event loop）
        loop = _asyncio.get_event_loop()
        worker_task = loop.run_in_executor(None, _stream_worker)

        # event_gen 协程：从 _q 拿 → yield；客户端断（task cancelled）就退出
        try:
            while True:
                # 让 event loop 喘气 + 处理 client disconnect
                if _asyncio.current_task() and _asyncio.current_task().cancelled():
                    break
                try:
                    item = await _asyncio.wait_for(_q.get(), timeout=1.0)
                except _asyncio.TimeoutError:
                    # 每 1s 检查一次客户端断连 + 让出 loop
                    continue
                if item is None:  # sentinel
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        except (_asyncio.CancelledError, GeneratorExit):
            # 客户端断连 — StreamingResponse 会 cancel 这个协程
            _log.info(f"[ticket={ticket_id}] [SSE] client disconnected, aborting worker")
            # worker 还在 thread 跑，无法 cancel（run_in_executor 没 Future handle 暴露）
            # 但 worker 跑完会把 _q 填满 → 自然 GC；下次新 stream 会创新 _q
            raise

        # 3) 持久化已在 worker 线程收尾完成（_persist_round，2026-10-20 挪出协程避免断连丢历史）

        # 4) 收尾
        yield f"data: {json.dumps({'type': 'done', 'response': full_response, 'tool_calls': tool_calls_log, 'agent_unavailable': agent_unavailable}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.post("/tickets/{ticket_id}/cleanup-container")
async def cleanup_ticket_container(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """admin 手动清理 agent 容器（stop 不删，重新接单可复用；保留磁盘 artifacts/）"""
    from app.services.ticket_agent import MANAGER
    from app.models import AuditLog

    ok = await MANAGER.destroy(ticket_id, stop_only=True)
    if not ok:
        return ApiResponse(message="无活跃 agent session")

    db.add(AuditLog(
        user_id=current_user.user_id,
        action="ticket_container_cleanup",
        target_type="ticket",
        target_id=ticket_id,
        details="manual",
    ))
    db.commit()
    return ApiResponse(message="容器已清理，磁盘产物保留")


@router.post("/tickets/{ticket_id}/agent/stop", response_model=ApiResponse)
async def stop_agent_round(ticket_id: int, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    """T9-U 工单 Agent「停止当前轮」（docs/ticket-agent-stop-design.md 修订版）

    语义：停本轮执行（杀活跃命令+chunk 边界 break），容器/现场/上下文全保留，
    下条指令从上一完整状态续跑。与 cleanup-container/force-cleanup 三级语义不混。
    异步收敛：stopping=true 表示信号已发；轮在下一命令返回/下一 chunk 边界停。
    击杀实现（本日实验两次纠偏）：exec_inspect().Pid 恒 0 宿主击杀不可信 →
    _exec 注入 HBEXEC_TAG 环境标记，容器内按标记杀进程组（docker_backend.kill_active_execs）。
    """
    import threading as _th
    from app.services.ticket_agent import MANAGER
    sess = MANAGER.get(ticket_id)
    if not sess:
        raise HTTPException(status_code=400, detail="agent session 未启动")
    if not sess.agent_state.get("_stream_running"):
        return ApiResponse(message="当前无进行中的轮次", data={"stopping": False})
    sess.stop_event.set()
    # 评审精确化2：全量击杀活跃 exec（含"同一命令并发跑两次"的所有分身）
    try:
        killed = sess.backend.kill_active_execs(15)  # SIGTERM 进程组
    except Exception:
        killed = 0
    # 60s 升级 SIGKILL：只打仍挂在登记簿上的（已自然返回的 finally 已清账=不碰）
    def _escalate():
        try:
            if getattr(sess.backend, "exec_registry", None):
                n2 = sess.backend.kill_active_execs(9)
                if n2:
                    import logging
                    logging.getLogger("ticket_agent_stream").info(
                        f"[ticket={ticket_id}] [STOP] SIGKILL 升级击杀 {n2} 组")
        except Exception:
            pass
    _th.Timer(60, _escalate).start()
    db.add(AuditLog(user_id=current_user.user_id, action="ticket_agent_stop",
                    target_type="ticket", target_id=ticket_id,
                    details=f"stop round; groups_sigterm={killed}"))
    db.commit()
    return ApiResponse(data={"stopping": True, "groups_sigterm": killed})


@router.post("/tickets/{ticket_id}/agent/force-cleanup", response_model=ApiResponse)
async def force_cleanup_ticket_agent(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """admin 强制清理 agent（删 work_dir + DB 记录）

    与 cleanup-container 区别：
    - cleanup: 只停容器，work_dir + checkpoint 保留（下次接单复用）
    - force-cleanup: 删 work_dir + DB 记录（下次接单要重新 git clone + docker run）
    """
    from app.services.ticket_agent import MANAGER
    from app.models import AuditLog, TicketAgent
    import shutil
    from pathlib import Path as _Path  # 别名避免与 SQLAlchemy Path 冲突

    # 1) 停容器（保留内存 session 清理）
    await MANAGER.destroy(ticket_id)

    # 2) 删 work_dir（含 .agent_state.json、.patches/、src/）
    rec = db.query(TicketAgent).filter(TicketAgent.ticket_id == ticket_id).first()
    if rec and rec.work_dir:
        try:
            work_path = _Path(rec.work_dir)
            if work_path.exists():
                shutil.rmtree(work_path, ignore_errors=True)
        except Exception as e:
            print(f"[force-cleanup] 删 work_dir 异常: {e}")
        rec.work_dir = None
        rec.checkpoint_path = None

    # 3) 删 DB 行
    if rec:
        db.delete(rec)

    db.add(AuditLog(
        user_id=current_user.user_id,
        action="ticket_agent_force_cleanup",
        target_type="ticket",
        target_id=ticket_id,
        details="manual force cleanup",
    ))
    db.commit()
    return ApiResponse(message="已强制清理：容器停 + work_dir 删 + DB 记录删。下次接单要重新 git clone。")


@router.post("/tickets/{ticket_id}/agent/retry", response_model=ApiResponse)
async def retry_ticket_agent(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """重试 agent 启动（启动失败时 admin 手动触发）

    流程：
    1. 校验工单存在 + status=processing
    2. 清掉旧 error
    3. 异步重启 _create_session
    """
    from app.services.ticket_agent import MANAGER
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.status != "processing":
        raise HTTPException(status_code=400, detail="工单非处理中，无需重新启动")

    # 清掉旧 error + 已有的 session（如果有但坏了）
    MANAGER._errors.pop(ticket_id, None)
    if MANAGER.get(ticket_id):
        # 已有 session — 保留；让 admin 先清理容器
        return ApiResponse(message="agent session 已存在，如需重建请先清理容器")

    # 异步重试
    import asyncio
    # 批2-①：同上，请求级 ticket/db 不进后台闭包
    asyncio.create_task(MANAGER.start_agent_background(ticket_id))

    return ApiResponse(message="agent 重新启动已触发，请稍候查看状态")


# ============== 生成 SKILL.md（手动 / 自动）=============
@router.post("/tickets/{ticket_id}/generate-skill")
async def generate_skill_endpoint(
    request: Request,
    ticket_id: int,
    skill_name: Optional[str] = None,
    skill_type: str = "layer2-lessons",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """工单经验提交审核（预览确认后走 experience 审批流）

    body（可选）：{"skill_content": "...", "skill_name": "..."}
    - 带 skill_content：admin 在预览弹窗编辑过的草稿，用它提交
    - 不带：后端按模板现生成（resolve+勾选时的快捷路径）
    """
    from app.services.skill_draft import generate_skill_from_ticket
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        pass
    edited_content = payload.get("skill_content")
    edited_name = payload.get("skill_name") or skill_name
    try:
        exp_id = generate_skill_from_ticket(
            db=db, ticket_id=ticket_id, current_user=current_user,
            skill_name=edited_name, skill_type=skill_type,
            skill_content_override=edited_content,
        )
        db.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成失败: {e}")
    return ApiResponse(
        data={"experience_id": exp_id},
        message=f"经验草稿已提交审核（#{exp_id}），请到「审批」页预览确认",
    )


@router.get("/tickets/{ticket_id}/generate-skill/preview")
async def preview_skill_draft(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """预览工单经验草稿（不写 DB）—— 同步版：LLM 要 1-4 分钟，前端一般走 /preview-job 异步轮询"""
    from app.services.skill_draft import build_skill_draft_preview
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    if not ticket.resolution:
        raise HTTPException(status_code=400, detail="工单还没有解决说明，请先标记已解决再生成经验")
    try:
        draft = build_skill_draft_preview(db, ticket)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"草稿生成失败: {e}")
    return ApiResponse(data=draft)


@router.post("/tickets/{ticket_id}/generate-skill/preview-job")
async def start_preview_job(
    ticket_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """异步起 LLM 草稿生成任务（秒回，前端轮询 /preview-job/status）。

    LLM 思考模型生成 60-240s+，同步等待会撞 HTTP 超时；改后台线程 + 轮询。
    """
    from app.services.skill_draft import start_preview_job
    from app.models import SessionLocal
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    if not ticket.resolution:
        raise HTTPException(status_code=400, detail="工单还没有解决说明，请先标记已解决再生成经验")
    job = start_preview_job(SessionLocal, ticket_id)
    return ApiResponse(data={"status": job["status"]}, message="草稿生成中" if job["status"] == "running" else "任务已存在")


@router.get("/tickets/{ticket_id}/generate-skill/preview-job/status")
async def preview_job_status(
    ticket_id: int,
    current_user: User = Depends(get_current_user),
):
    """轮询草稿生成任务：running / done(result) / error(message) / none(未启动)"""
    from app.services.skill_draft import get_preview_job
    job = get_preview_job(ticket_id)
    if not job:
        return ApiResponse(data={"status": "none"})
    return ApiResponse(data={
        "status": job["status"],
        "result": job.get("result"),
        "error": job.get("error"),
        "elapsed": round(__import__("time").time() - job["started_at"], 1),
    })


# ============== 工单详情：join task 返完整信息（admin 复现需要）=============
# 已有 admin_get_ticket，扩展返回 git_url / source_path / system_name


# ==================== T10 操作日志审计（读层） ====================

@router.get("/audit-logs", response_model=ApiResponse)
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user_id: Optional[int] = None,
    action: Optional[str] = None,
    target_type: Optional[str] = None,
    q: Optional[str] = None,               # 模糊：target_key / details
    date_from: Optional[str] = None,       # naive UTC ISO（前端传本地日 0 点换算，或直带 Z）
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """审计日志分页查询（倒序）。只读端点——无删除/更新路径，append-only 语义保持。
    details 列表回显截 200 字符（列表瘦身教训同款），全文点开看。"""
    from app.models import AuditLog as AL
    from sqlalchemy import or_
    query = db.query(AL, User.username).outerjoin(User, AL.user_id == User.user_id)
    if user_id is not None:
        query = query.filter(AL.user_id == user_id)
    if action:
        query = query.filter(AL.action == action)
    if target_type:
        query = query.filter(AL.target_type == target_type)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(AL.target_key.ilike(like), AL.details.ilike(like)))
    for bound, op in ((date_from, "__ge__"), (date_to, "__le__")):
        if bound:
            try:
                dtv = datetime.fromisoformat(bound.replace("Z", "").replace("+00:00", ""))
                query = query.filter(getattr(AL.created_at, op)(dtv))
            except ValueError:
                raise HTTPException(status_code=422, detail="date_from/date_to 需 ISO 格式（如 2026-10-27T00:00）")
    total = query.count()
    rows = (query.order_by(AL.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    items = [{
        "id": a.id,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "user_id": a.user_id,
        "username": uname or (a.target_key if a.user_id is None else "?"),
        "action": a.action,
        "target_type": a.target_type,
        "target_id": a.target_id,
        "target_key": a.target_key,
        "details": (a.details or "")[:200],
        "ip_address": a.ip_address,
    } for a, uname in rows]
    return ApiResponse(data={"total": total, "items": items})
