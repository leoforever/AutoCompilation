"""产物货架公共端点（11-04 拍板：普通用户可见/可查/可下载在架件，管理动作零）

与 admin 侧分工：这里只回答"货架上有什么、给我一份"；审批/上传/对账/下架恒在
routes/admin.py（require_admin）。安全边界在服务端 SQL 层钉死，不靠前端隐藏：
  仅 deleted_at IS NULL ∧ status='approved' ∧ cache_path 非空 的行可见/可下。
不泄露：note（内部备注）、uploader/source_task_key（他人身份链）、sha 全值。
记账纪律：人下载不动 hits/views（那是"被编译复用"的账）；读不录审计（拍板3）。
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.dependencies import get_current_user
from app.models import ArtifactIndex, User, get_db
from app.schemas import ApiResponse
from app.services.artifact_lib import cache_root

router = APIRouter()


def _onshelf_query(db: Session):
    """在架三条件唯一口径（list/download 共用，防两处漂移）"""
    return db.query(ArtifactIndex).filter(
        ArtifactIndex.deleted_at.is_(None),
        ArtifactIndex.status == "approved",
        ArtifactIndex.cache_path != "",
    )


@router.get("", response_model=ApiResponse)
async def public_list_artifacts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str = "",
    system_id: int | None = None,
    kind: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """货架公开目录：全员登录可查，仅返回在架件（服务端钉死，无 status 入口）。"""
    q = _onshelf_query(db)
    if search:
        like = f"%{search.strip()}%"
        q = q.filter(or_(ArtifactIndex.name.ilike(like), ArtifactIndex.name_norm.ilike(like)))
    if system_id:
        q = q.filter(ArtifactIndex.system_id == system_id)
    if kind:
        q = q.filter(ArtifactIndex.kind == kind)
    total = q.count()
    rows = (q.order_by(ArtifactIndex.hits.desc(), ArtifactIndex.id.desc())
             .offset((page - 1) * page_size).limit(page_size).all())
    return ApiResponse(data={"total": total, "items": [{
        "id": r.id, "name": r.name, "name_norm": r.name_norm, "version": r.version,
        "system_id": r.system_id, "kind": r.kind, "origin": r.origin,
        "build_env": r.build_env, "image": r.image, "size_bytes": r.size_bytes,
        "hits": r.hits, "views": r.views, "source_commit": r.source_commit,
        "glibc_requires": r.glibc_requires,   # 11-05 公共池需求（系统0 行=通用件）
        "sha256": (r.sha256 or "")[:16], "created_at": r.created_at,
    } for r in rows]})


@router.get("/{artifact_db_id}/download")
async def public_download_artifact(
    artifact_db_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """在架件下载。四闸：行在 ∧ 未软删 ∧ approved ∧ cache_path 在；
    路径解析必须落在 cache/ 根下（防账本脏值 ../ 越界）；文件灭=410。"""
    row = db.query(ArtifactIndex).filter(ArtifactIndex.id == artifact_db_id).first()
    if not row or row.deleted_at is not None or row.status != "approved" or not row.cache_path:
        # 非在架件对普通用户=不存在（pending/仅账本/已下架统一 404，不透露状态细节）
        raise HTTPException(status_code=404, detail="该产物不在货架上")
    root = cache_root().resolve()
    fp = (root / row.cache_path).resolve()
    if not str(fp).startswith(str(root) + "/") or fp.is_dir():
        raise HTTPException(status_code=403, detail="产物路径非法")
    if not fp.is_file():
        raise HTTPException(status_code=410, detail="货架文件已灭失（曾上架，可能经手工清理），请联系管理员")
    # 下载名：货架名一般已含软件名前缀（node-pty-1.1.0.tgz）——重复钉会变
    # "node-pty-node-pty-…"；裸短名（lsxdemo）才补 name_norm 前缀防撞名
    fname = row.name if row.name.lower().startswith(row.name_norm.lower()) \
        else f"{row.name_norm}-{row.name}"
    return FileResponse(str(fp), filename=fname, media_type="application/octet-stream")
