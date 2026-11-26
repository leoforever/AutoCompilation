"""产物货架公共端点（11-04 拍板：普通用户可见/可查/可下载在架件，管理动作零）

与 admin 侧分工：这里只回答"货架上有什么、给我一份"；审批/上传/对账/下架恒在
routes/admin.py（require_admin）。安全边界在服务端 SQL 层钉死，不靠前端隐藏：
  货架v2（11-25）可见/可下 = (approved ∧ cache_path 非空 ∧ audience∉{ai}) ∪ pending。
  旧纪律"非在架不透露状态"有意部分放弃（pending 公开=拍板的试用窗口），记档设计档 §7。
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
from app.schemas import ApiResponse, ArtifactBatchRequest
from app.services.artifact_lib import cache_root

router = APIRouter()


def _onshelf_query(db: Session):
    """人侧可见唯一口径（货架v3）：approved ∧ cache_path 非空 ∧ audience∉{ai}。
    存量 status="" 的行维持原样不在公开面（账本回填行，非货架语义）。"""
    from app.services.artifact_lib import _human_visible
    return db.query(ArtifactIndex).filter(
        ArtifactIndex.deleted_at.is_(None),
        ArtifactIndex.status == "approved",
        ArtifactIndex.cache_path != "",
        _human_visible(),
    )


@router.get("", response_model=ApiResponse)
async def public_list_artifacts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str = "",
    system_id: int | None = None,
    kind: str = "",
    source: str = "",
    sort_by: str = "",
    order: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """货架公开目录：全员登录可查，仅返回在架件（服务端钉死，无 status 入口）。
    source 筛选与来源聚合：只回任务号字符串（无用户身份链，不违公开口径）。
    sort_by 白名单复用 admin 的 shelf_sort_order（默认 hits desc=机器账优先）。"""
    from app.services.artifact_lib import shelf_sort_order
    q = _onshelf_query(db)
    if source == "upload":
        q = q.filter(ArtifactIndex.origin == "upload")
    elif source:
        q = q.filter(ArtifactIndex.source_task_key == source)
    if search:
        like = f"%{search.strip()}%"
        q = q.filter(or_(ArtifactIndex.name.ilike(like), ArtifactIndex.name_norm.ilike(like)))
    if system_id:
        q = q.filter(ArtifactIndex.system_id == system_id)
    if kind:
        q = q.filter(ArtifactIndex.kind == kind)
    total = q.count()
    rows = (q.order_by(*shelf_sort_order(sort_by, order, ArtifactIndex,
                                         default=ArtifactIndex.hits.desc()))
             .offset((page - 1) * page_size).limit(page_size).all())
    from app.services.artifact_lib import shelf_sources
    return ApiResponse(data={"total": total, "sources": shelf_sources(db), "items": [{
        "id": r.id, "name": r.name, "name_norm": r.name_norm, "version": r.version,
        "system_id": r.system_id, "kind": r.kind, "origin": r.origin,
        "build_env": r.build_env, "image": r.image, "size_bytes": r.size_bytes,
        "hits": r.hits, "views": r.views, "dl_count": r.dl_count or 0,
        "source_commit": r.source_commit,
        "status": r.status,                   # 货架v2：前端徽标（在架/待审批）
        "audience": r.audience or "both",
        "glibc_requires": r.glibc_requires,   # 11-05 公共池需求（系统0 行=通用件）
        "runtime_env": r.runtime_env,          # RUN.md 运行依赖（一期二期，前端展示）
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
    if not row or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="该产物不在货架上")
    if row.status == "approved" and row.cache_path and (row.audience or "both") != "ai":
        root = cache_root().resolve()
        fp = (root / row.cache_path).resolve()
        if not str(fp).startswith(str(root) + "/") or fp.is_dir():
            raise HTTPException(status_code=403, detail="产物路径非法")
    else:
        # rejected/已下架/仅供复用（audience=ai）对普通用户=不存在
        raise HTTPException(status_code=404, detail="该产物不在货架上")
    if not fp.is_file():
        raise HTTPException(status_code=410, detail="货架文件已灭失（曾上架，可能经手工清理），请联系管理员")
    # 下载名：货架名一般已含软件名前缀（node-pty-1.1.0.tgz）——重复钉会变
    # "node-pty-node-pty-…"；裸短名（lsxdemo）才补 name_norm 前缀防撞名
    fname = row.name if row.name.lower().startswith(row.name_norm.lower()) \
        else f"{row.name_norm}-{row.name}"
    # 人气账（11-25）：过完全部闸门、文件确认在盘才 +1——404/410/403 不算下载
    from datetime import datetime as _dt
    row.dl_count = (row.dl_count or 0) + 1
    row.dl_last_at = _dt.utcnow()
    db.commit()
    return FileResponse(str(fp), filename=fname, media_type="application/octet-stream")


@router.post("/batch-download")
async def batch_download_artifacts(
    body: ArtifactBatchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """按来源批量下载在架件 → tar.gz（附 manifest 行带 sha256，收方对账用）。
    语义（11-06 拍板）：≤2GB 否则拒分批；逐件三闸预检，任一不合格整单拒（不出半包）；
    读型交付不入审计（同单件下载口径，拍板3）；打包 to_thread 不冻事件循环（.209 教训）。
    ⚠ 路由声明顺序：无 /{id} 冲突（batch-download 在动态段之前注册同名单件规则一致惯例）。"""
    import asyncio as _aio
    import os as _os
    import tarfile as _tf
    import tempfile as _tmpf
    from app.models import ArtifactIndex
    from app.services.artifact_lib import cache_root, get_cfg
    rows = []
    bad = []
    for i in body.ids[:100]:
        r = db.query(ArtifactIndex).filter(ArtifactIndex.id == i,
                                           ArtifactIndex.deleted_at.is_(None)).first()
        # 货架v3：批量与单件同闸（pending 分支随"编译即上架"退役）
        if r and r.status in ("approved", "") and r.cache_path and (r.audience or "both") != "ai":
            f = cache_root() / r.cache_path
        else:
            bad.append(f"#{i} 不在架"); continue
        if not f.is_file():
            bad.append(f"#{i} {r.name} 文件不在盘"); continue
        rows.append((r, f))
    if bad:
        raise HTTPException(status_code=400, detail="批量下载整单拒（不出半包）：" + "；".join(bad[:6]))
    if not rows:
        raise HTTPException(status_code=404, detail="没有可下载的在架件")
    limit_mb = int(get_cfg("artifact_cache.batch_dl_max_mb", 2048))
    total = sum(f.stat().st_size for _r, f in rows)
    if total > limit_mb * 1024 * 1024:
        raise HTTPException(status_code=413,
                            detail=f"合计 {total // (1024 * 1024)}MB 超批量下载上限 {limit_mb}MB——请分批次下载")

    def _pack() -> str:
        out = _tmpf.NamedTemporaryFile(suffix=".tar.gz", delete=False)
        out.close()
        with _tf.open(out.name, "w:gz") as t:
            lines = ["# 货架批量下载清单（sha256 供收方对账）"]
            for r, f in rows:
                t.add(f, arcname=f"{r.name_norm}/{r.name}")
                lines.append(f"{r.sha256 or '-'}  {r.name_norm}/{r.name}  {r.size_bytes}B  "
                             f"status={r.status or '-'} audience={r.audience or 'both'}")
            man = _tmpf.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
            man.write("\n".join(lines) + "\n"); man.close()
            t.add(man.name, arcname="manifest.txt")
            _os.unlink(man.name)
        return out.name

    path = await _aio.to_thread(_pack)
    # 人气账（11-25）：整包已成形即将发出=包内每件各记 1 次（同事务）
    from datetime import datetime as _dt
    _now = _dt.utcnow()
    for _r, _f in rows:
        _r.dl_count = (_r.dl_count or 0) + 1
        _r.dl_last_at = _now
    db.commit()
    fname = f"shelf-batch-{len(rows)}items.tar.gz"
    return _fr_with_cleanup(path, fname)


def _fr_with_cleanup(path: str, fname: str):
    """FileResponse + 发完即删临时包（BackgroundTask 在 starlette FileResponse 内置用法）。"""
    from starlette.background import BackgroundTask
    from fastapi.responses import FileResponse as _FR
    return _FR(path, filename=fname, media_type="application/gzip",
               background=BackgroundTask(_cleanup_quiet, path))


def _cleanup_quiet(p: str):
    import os as _o
    try:
        _o.unlink(p)
    except OSError:
        pass


