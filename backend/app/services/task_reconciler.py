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
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

STALE_S = 600  # updated_at 冻结阈值（心跳 60s，10 倍余量仍冻=写链断了）
SUCCESS_LOG_RE = re.compile(r"结果: success")

last_result: dict = {"checked": 0, "restored": 0, "reported": 0, "at": None}


def _auto_enabled() -> bool:
    return os.environ.get("RECONCILE_AUTO", "1") != "0"


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
    return stats
