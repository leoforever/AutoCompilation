"""统一任务终态写入器（2026-10-21 设计定稿 Step 0）

背景教训（T-20261021163147-223BFD）：agent 把含 NUL 的日志带进流 → persist 一锅端事务
被 PG 拒（text 禁 \x00）→ status/duration/artifact_path 全没落库 → 任务停 compiling
→ 30min 后被看门狗误标"worker 崩溃"。145MB 产物在盘，DB 认为失败。

三条铁律：
1. 字段分级：status/duration/artifacts=DB 权威（必须落，重试+告警）；log_output=磁盘
   build.log 权威（DB 列只是缓存，写不进就放弃，绝不连坐状态）。
2. 失败必吭声：每块失败落哨兵 artifacts/<task_id>/.persist_error（JSON）+ WS 推 [WARN]
   行进既有日志流 + 进程内计数供 /admin/stats 查询。stdout 黑洞时代结束。
3. 净化在入口：NUL/孤立代理项在写库前剔除；哨兵/reconcile 都信任净化后的数据。
"""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---- 进程内健康计数（stats 消费）----
_persist_errors = {"core": 0, "log": 0, "log_degraded": 0}


def persist_error_stats() -> dict:
    return dict(_persist_errors)


def sanitize_text(s: Optional[str]) -> str:
    """PG text 合法化：剔 NUL；encode/replace 杀孤立代理项（surrogate 会让 utf-8 编码炸）。
    其余字符原样（控制字符 PG 允许，日志可读性优先）。"""
    if not s:
        return ""
    s = s.replace("\x00", "")
    try:
        s.encode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        s = s.encode("utf-8", "replace").decode("utf-8", "replace")
    return s


def _sentinel_path(work_dir: str) -> Optional[Path]:
    if not work_dir:
        return None
    p = Path(work_dir)
    return p if p.is_dir() else None


def write_sentinel(task_id: str, work_dir: str, block: str, err: Exception, extra: "dict | None" = None) -> None:
    """哨兵文件：artifacts/<task_id>/.persist_error（reconcile/看门狗分流/人查的证据源）"""
    try:
        d = _sentinel_path(work_dir)
        if d is None:
            return
        rec = {
            "task_id": task_id,
            "block": block,                       # core / log
            "error": f"{err.__class__.__name__}: {str(err)[:300]}",
            "at": datetime.utcnow().isoformat(),
        }
        if extra:
            rec.update(extra)
        (d / ".persist_error").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass  # 哨兵本身写不进，只说明磁盘也凉了，不再递归报错


def clear_sentinel(work_dir: str) -> None:
    try:
        p = Path(work_dir) / ".persist_error" if work_dir else None
        if p and p.is_file():
            p.unlink()
    except Exception:
        pass


def _ws_warn(task_id: str, msg: str) -> None:
    """WS 推送：复用 log 消息通道（前端零改动），[WARN] 级别进用户可见日志流"""
    try:
        from app.services.ws_broadcaster import BROADCASTER
        BROADCASTER.publish(task_id, {
            "type": "log",
            "level": "warn",
            "msg": f"[WARN] {msg}",
            "ts": datetime.now().strftime("%m-%d %H:%M:%S"),
        })
    except Exception:
        pass


def write_terminal(db, task, state: dict) -> None:
    """终态落库。核心块与日志块分两次 commit：脏日志永不连坐状态。

    调用方：compile_engine._persist_and_return（唯一终态写入口，勿新开）。
    state 约定键：status/last_error/_matched_skills/artifacts/work_dir/log_output/host_log_path
    """
    work_dir = state.get("work_dir", "") or ""
    tid = state.get("task_id", "")
    task_id_str = tid if isinstance(tid, str) else (getattr(task, "task_id", "") if task else "")

    # ── 块1：核心终态（DB 权威，必须落）──
    try:
        task.status = state.get("status", "failed")
        task.error_message = sanitize_text(state.get("last_error", "")) or None
        _ms = state.get("_matched_skills") or []
        if _ms:
            task.matched_skills = json.dumps(_ms, ensure_ascii=False)
        artifacts = state.get("artifacts", [])
        task.artifact_path = json.dumps(artifacts, ensure_ascii=False) if artifacts else (task.artifact_path or None)
        now = datetime.utcnow()
        if task.started_at is None:
            task.started_at = now
        task.completed_at = now
        if task.started_at:
            task.duration_seconds = int((task.completed_at - task.started_at).total_seconds())
        db.commit()
        if work_dir and not (Path(work_dir) / ".persist_error").is_file():
            clear_sentinel(work_dir)  # 干净收尾不留陈旧哨兵
    except Exception as e:
        db.rollback()
        _persist_errors["core"] += 1
        write_sentinel(task_id_str, work_dir, "core", e,
                       extra={"status_wanted": state.get("status")})
        _ws_warn(task_id_str, f"任务终态入库失败（{e.__class__.__name__}），状态可能滞留，管理员页将见对账提示")

    # ── 块2：log_output（磁盘 build.log 才是权威，DB 列只是缓存）──
    log_out = sanitize_text(state.get("log_output", ""))[-200_000:]  # 3d：终态同样只存尾 200KB
    try:
        task.log_output = log_out
        db.commit()
    except Exception as e:
        db.rollback()
        try:
            task.log_output = log_out[-200_000:]
            db.commit()
            _persist_errors["log_degraded"] += 1
            write_sentinel(task_id_str, work_dir, "log", e, extra={"degraded": "tail-200K"})
        except Exception as e2:
            db.rollback()
            _persist_errors["log"] += 1
            write_sentinel(task_id_str, work_dir, "log", e2, extra={"note": "缓存放弃；build.log 为准"})


def write_log_cache(db, task, log_output: str) -> None:
    """周期提交的日志缓存刷新：净化后写，失败静默（不连坐、不哨兵刷屏——
    周期写失败下轮还会再试；终态哨兵才是结论性事件）。"""
    try:
        # 3d（2026-10-23）：DB 列只是缓存，磁盘 build.log 为权威——周期写只留尾 200KB
        # （torch 级任务全量日志数 MB，整列重写是接口肥大/O(n²) 源头）
        task.log_output = sanitize_text(log_output[-200_000:])
        db.commit()
    except Exception:
        db.rollback()
