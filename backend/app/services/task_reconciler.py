"""编译任务对账自愈（2026-10-21 Step 0）

治的病：worker 把活干完了，但"死亡证明"（终态写库）没填下去——任务滞留 compiling，
半小时后被看门狗补刀成"worker 崩溃 failed"（223BFD 事故：145MB 产物在盘，DB 说失败）。

原则（DB=权威数据源，磁盘=现场证据）：
- 只碰"冻结的非终态"：status=compiling 且 updated_at 停更 > STALE_S（心跳 60s/次，留 10min 余量）
- 恢复=有现场证据才动手：哨兵(.persist_error status_wanted=success) 或 build.log"结果: success"+产物在盘
- 自动档（默认 RECONCILE_AUTO=1）：自动标 success + 留审计（哨兵改名 .persist_error.reconciled，
  不删除——磁盘审计）；无产物的冻结任务不动（那是真崩溃，留给看门狗/人判断）
- 报告档（RECONCILE_AUTO=0）：只 log + WS 通知，不改 DB
- 每轮结果存 last_result 供 /admin/stats/overview health 字段消费
"""
import json
import logging
import os
import re

logger = logging.getLogger(__name__)
from datetime import datetime, timedelta
from pathlib import Path

from app.config import get_cfg as _get_cfg
STALE_S = _get_cfg("worker.stale_threshold", 600, env_alias="WORKER_STALE_THRESHOLD")  # updated_at 冻结阈值
SUCCESS_LOG_RE = re.compile(r"结果: success")

last_result: dict = {"checked": 0, "restored": 0, "reported": 0, "at": None}


def _auto_enabled() -> bool:
    v = os.environ.get("RECONCILE_AUTO")
    if v is not None:
        return v != "0"
    return bool(_get_cfg("auth.reconcile_auto", True))


def _ws_notify(task_id: str, msg: str) -> None:
    try:
        from app.services.ws_broadcaster import BROADCASTER
        BROADCASTER.publish(task_id, {
            "type": "log", "level": "warn",
            "msg": f"[对账] {msg}", "ts": datetime.now().strftime("%m-%d %H:%M:%S"),
        })
    except Exception:
        pass


def _read_sentinel(task_dir: Path):
    p = task_dir / ".persist_error"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _rebuild_artifacts(output_dir: Path) -> list:
    """从磁盘产物目录重建 artifact_path JSON（对齐 stage6 收集格式：/work/output 视角）"""
    out = []
    for item in sorted(output_dir.iterdir()):
        if item.is_file():
            out.append({"name": item.name, "path": f"/work/output/{item.name}", "size": item.stat().st_size})
        elif item.is_dir():
            size = sum((Path(dp) / fn).stat().st_size for dp, _, fns in os.walk(item) for fn in fns)
            out.append({"name": item.name, "path": f"/work/output/{item.name}", "size": size})
    return out




# ── P3 保留策略（11-03 总设计 §4；拍板4：自动删仅限这两类中间物，货架永不自动动）──
def cleanup_intermediates() -> dict:
    """1) container_work mtime 超 ttl 天且任务非在途 → 整目录删（纯中间物：src clone/
       构建残留，任何可下载内容不在其中）；
       2) 成功任务 artifacts.tar.gz 龄 >90 天 → 删 bundle 留树（下载端点本就实时
       重打包兜底，删包零风险；新任务两份都留，下载秒回）。
       只删这两类——货架(cache/)、产物树、promote_pending 一律不碰（人工管理）。"""
    import time
    import shutil
    from app.config import get_cfg, artifacts_dir
    from app.models import CompileTask, TaskStatus, SessionLocal
    stats = {"container_work": 0, "bundle": 0, "mb_freed": 0}
    ttl_days = int(get_cfg("artifact_cache.container_work_ttl_days", 7))
    if ttl_days <= 0:
        return stats
    art = artifacts_dir()
    now = time.time()
    db = SessionLocal()
    try:
        live = {t.task_id for t in db.query(CompileTask).filter(
            CompileTask.status.in_((TaskStatus.SUBMITTED.value, TaskStatus.COMPILING.value))
        ).all()}
        for d in art.iterdir():
            if not d.is_dir() or d.name in ("sources",):
                continue
            tid = d.name
            cw = d / "container_work"
            if cw.is_dir() and tid not in live:
                try:
                    if now - cw.stat().st_mtime > ttl_days * 86400:
                        mb = _dir_mb(cw)
                        shutil.rmtree(cw, ignore_errors=True)
                        stats["container_work"] += 1
                        stats["mb_freed"] += mb
                except OSError:
                    pass
            # bundle 90 天瘦身（树在包删；下载走实时重打包兜底）
            bundle = d / "artifacts.tar.gz"
            tree = d / "artifacts"
            if bundle.is_file() and tree.is_dir() and tid not in live \
               and now - bundle.stat().st_mtime > 90 * 86400:
                try:
                    stats["mb_freed"] += bundle.stat().st_size // (1024 * 1024)
                    bundle.unlink()
                    stats["bundle"] += 1
                except OSError:
                    pass
        if any((stats["container_work"], stats["bundle"])):
            logger.info("[cleanup_intermediates] %s", stats)
    except Exception:
        logger.exception("[cleanup_intermediates] 异常（不影响对账主流程）")
    finally:
        db.close()
    return stats


def _dir_mb(p) -> int:
    total = 0
    try:
        for x in p.rglob("*"):
            if x.is_file():
                total += x.stat().st_size
    except OSError:
        pass
    return total // (1024 * 1024)


def reconcile_stuck_tasks() -> dict:
    from app.models import SessionLocal, CompileTask, TaskStatus
    from app.services.compile_engine import _artifacts_dir

    db = SessionLocal()
    global last_result
    stats = {"checked": 0, "restored": 0, "reported": 0, "at": datetime.now().isoformat()}
    try:
        cutoff = datetime.utcnow() - timedelta(seconds=STALE_S)
        stuck = db.query(CompileTask).filter(
            CompileTask.status == TaskStatus.COMPILING.value,
            CompileTask.updated_at < cutoff,
        ).all()
        now = datetime.utcnow()
        for task in stuck:
            stats["checked"] += 1
            task_dir = _artifacts_dir() / task.task_id
            if not task_dir.is_dir():
                continue  # 连工作目录都没有=没起跑或已清理，不猜
            sentinel = _read_sentinel(task_dir)
            log_path = task_dir / "build.log"
            log_says_success = False
            if log_path.is_file():
                try:
                    log_says_success = bool(SUCCESS_LOG_RE.search(log_path.read_text(encoding="utf-8", errors="replace")[-4000:]))
                except Exception:
                    pass
            output_dir = task_dir / "artifacts" / "output"
            has_artifacts = output_dir.is_dir() and any(output_dir.iterdir())
            bundle = task_dir / "artifacts.tar.gz"
            has_bundle = bundle.is_file()

            # —— 分流：成功证据齐 → 恢复；证据不足 → 不动（真崩溃留给看门狗/人）——
            wanted_success = bool(sentinel and sentinel.get("status_wanted") == "success") or log_says_success
            if not (wanted_success and (has_artifacts or has_bundle)):
                continue

            detail = f"哨兵{sentinel.get('block','?') if sentinel else '无'}+日志成功行+产物{'bundle' if has_bundle else '目录'}在盘"
            if not _auto_enabled():
                stats["reported"] += 1
                _ws_notify(task.task_id, f"对账发现疑似入库断链（{detail}），AUTO=0 仅报告不改库")
                continue

            completed = datetime.utcfromtimestamp(log_path.stat().st_mtime) if log_path.is_file() else now
            task.status = "success"
            task.error_message = None
            task.completed_at = completed
            if task.started_at:
                task.duration_seconds = int((completed - task.started_at).total_seconds())
            if not task.artifact_path and has_artifacts:
                task.artifact_path = json.dumps(_rebuild_artifacts(output_dir), ensure_ascii=False)
            from app.services.notify import notify_task_terminal
            notify_task_terminal(db, task, "success", "（对账自愈入库）")
            db.commit()
            # 磁盘审计：哨兵改名留档，不删
            try:
                if sentinel is not None and (task_dir / ".persist_error").is_file():
                    (task_dir / ".persist_error").rename(task_dir / ".persist_error.reconciled")
            except Exception:
                pass
            stats["restored"] += 1
            _ws_notify(task.task_id, f"对账已自动恢复为成功（入库断链：{detail}）；审计存 .persist_error.reconciled")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
        last_result = stats
    # P3 搭对账周期顺风车（main.py sweep_loop 每小时级）；独立异常域
    try:
        stats["cleanup"] = cleanup_intermediates()
    except Exception:
        logger.exception("[reconcile] cleanup 异常")
    return stats
