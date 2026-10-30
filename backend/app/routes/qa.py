"""T16 问答台端点（挂 /api/admin/qa，router 级 require_admin 守卫）

安全口径：只开 admin（上下文含全体工单/日志，路由守卫是唯一那道墙——设计档
"将来开用户端=重新设计不是加开关"）；权=job/会话按 user_id 属主自取，无跨人读口。
"""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import get_cfg
from app.dependencies import get_current_user
from app.models import get_db, User, Experience, ExperienceStatus, CompileTask
from app.schemas import ApiResponse
from app.services.audit import log_audit
from app.services import qa as qa_svc

router = APIRouter(prefix="/admin/qa", tags=["问答台"])


class QaAsk(BaseModel):
    question: str = Field(..., min_length=2, max_length=4000)
    task_db_id: int | None = None      # 挂任务上下文（build.log 尾+BUILD_NOTES）
    paste_text: str = Field(default="", max_length=20000)  # P0 报错片段粘贴区（LOG:paste 源）


class QaDraft(BaseModel):
    skill_name: str = Field(..., min_length=1, max_length=100)
    skill_type: str = Field(..., pattern="^(generic|specific)$")
    system_id: int
    task_db_id: int
    content: str = Field(..., min_length=20)


@router.post("/ask", response_model=ApiResponse)
async def qa_ask(body: QaAsk, request: Request, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    if not get_cfg("qa.enabled", True):
        raise HTTPException(status_code=503, detail="问答台已停用（qa.enabled=false）")
    cap = int(get_cfg("qa.max_per_day", 30))
    used = qa_svc.quota_used_today(db, current_user.user_id)
    if used >= cap:
        raise HTTPException(status_code=429,
                            detail=f"今日问答配额已用完（{used}/{cap}），明日重置；"
                                   "统计类问题请直接用任务页/审计页筛选")
    job = qa_svc.get_qa_job(current_user.user_id)
    if job and job["status"] == "running":
        return ApiResponse(data={"job": "running", "started_at": job["started_at"]})
    if body.task_db_id:
        t = db.query(CompileTask).filter(CompileTask.id == body.task_db_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="挂载任务不存在")
    # 审计先行：提问即计数（配额账本以 qa_ask 行数为准，失败也消耗——防重试刷）
    log_audit(db, user_id=current_user.user_id, action="qa_ask",
              target_type="qa", target_key=str(body.task_db_id or ""),
              request=request, details=f"q={body.question[:200]}")
    db.commit()
    loop = asyncio.get_running_loop()
    from app.models import SessionLocal
    qa_svc.start_qa_job(SessionLocal, current_user.user_id,
                        body.question, body.task_db_id, loop, body.paste_text)
    return ApiResponse(data={"job": "started"})


@router.get("/quota", response_model=ApiResponse)
async def qa_quota(db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    used = qa_svc.quota_used_today(db, current_user.user_id)
    cap = int(get_cfg("qa.max_per_day", 30))
    return ApiResponse(data={"used": used, "cap": cap, "left": max(cap - used, 0)})


@router.get("/status", response_model=ApiResponse)
async def qa_status(db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    job = qa_svc.get_qa_job(current_user.user_id)
    if not job:
        return ApiResponse(data={"status": "none"})
    return ApiResponse(data={"status": job["status"],
                             "result": job.get("result"),
                             "error": job.get("error")})


@router.get("/stream")
async def qa_stream(current_user: User = Depends(get_current_user)):
    """SSE 等终态（draft stream 同款双腿，轮询 status 兜底）。
    事件：done(result)/error(message)/none/timeout(6min→前端降级轮询)。"""
    from fastapi.responses import StreamingResponse
    job = qa_svc.get_qa_job(current_user.user_id)
    if not job:
        async def _none():
            yield 'data: {"type": "none"}\n\n'
        return StreamingResponse(_none(), media_type="text/event-stream")
    if job["status"] in ("done", "error"):   # 进来先送终态：断线重连天然覆盖
        async def _now():
            yield "data: " + json.dumps({"type": job["status"],
                                         "result": job.get("result"),
                                         "message": job.get("error")},
                                        ensure_ascii=False) + "\n\n"
        return StreamingResponse(_now(), media_type="text/event-stream")

    q = qa_svc.register_qa_waiter(current_user.user_id)

    async def gen():
        try:
            snap = await asyncio.wait_for(q.get(), timeout=360)
            yield "data: " + json.dumps({"type": snap["status"],
                                         "result": snap.get("result"),
                                         "message": snap.get("error")},
                                        ensure_ascii=False) + "\n\n"
        except asyncio.TimeoutError:
            yield 'data: {"type": "timeout"}\n\n'
        finally:
            qa_svc.unregister_qa_waiter(current_user.user_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/reset", response_model=ApiResponse)
async def qa_reset(current_user: User = Depends(get_current_user)):
    """清空本用户会话窗口（前端"清空重问"钮）。jobs 不动。"""
    qa_svc.reset_session(current_user.user_id)
    return ApiResponse(data={"reset": True})


@router.post("/save-draft", response_model=ApiResponse)
async def qa_save_draft(body: QaDraft, request: Request, db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """问答结论 → Experience(submitted)，接既有审批链（终点闸：人工审批挡投毒）。
    task_db_id 必填=Experience.task_id NOT NULL 的现实约束（MVP 不开无任务草稿口子）。"""
    t = db.query(CompileTask).filter(CompileTask.id == body.task_db_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="需关联一个真实任务（经验库血缘要求）")
    dup = db.query(Experience).filter(
        Experience.skill_name == body.skill_name,
        Experience.status.in_([ExperienceStatus.SUBMITTED.value,
                               ExperienceStatus.UNDER_REVIEW.value]),
    ).first()
    if dup:
        raise HTTPException(status_code=400, detail=f"同名草稿已在审批队列（#{dup.experience_id}）")
    exp = Experience(task_id=t.id, user_id=current_user.user_id,
                    skill_name=body.skill_name, skill_type=body.skill_type,
                    system_id=body.system_id, skill_content=body.content,
                    status=ExperienceStatus.SUBMITTED.value)
    db.add(exp)
    db.flush()
    log_audit(db, user_id=current_user.user_id, action="qa_save_draft",
              target_type="experience", target_id=exp.experience_id,
              target_key=body.skill_name, request=request,
              details=f"from_qa task={t.task_id}")
    db.commit()
    return ApiResponse(data={"experience_id": exp.experience_id})
