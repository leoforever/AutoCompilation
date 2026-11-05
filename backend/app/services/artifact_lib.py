"""产物复用库核心件（P1/P2 共享，11-03 总设计）：纯函数集中，可脱离 docker 单测。

职责边界：本模块只做"账本与货架"的读写逻辑；容器挂载/晋升触发在 compile_engine，
端点鉴权在 routes/admin——分层防耦。DB=权威（artifact_index），文件=交付面
（cache 树 + INDEX.md），两者一致性由 reindex 对账兜底（只报不手）。
"""
import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from app.config import get_cfg

# ── 路径权威（唯一解析处）────────────────────────────────────────
def cache_root() -> Path:
    """库根：<repo>/cache（与 artifacts_dir 同级不同物——artifacts 跟任务走，
    cache 跟系统走、永久；总设计 §3.1）"""
    from app.config import artifacts_dir
    return artifacts_dir().parent / "cache"


COMMON_SYSTEM_ID = 0   # 公共池哨兵（11-05 拍板"上传不选系统=所有系统可用"）：
                       # 物理 cache/0/，账本 system_id=0；任务晋升件永远不进此池（无写入路径）


def system_cache_dir(system_id: int) -> Path:
    return cache_root() / str(int(system_id))


def shelf_dir(system_id: int) -> Path:
    """容器挂载根=系统池目录：/work/cache/<name_norm>/<file>。
    INDEX.md 也生成在池根（AI cat /work/cache/INDEX.md 即指此处）。"""
    return system_cache_dir(system_id)


# ── 归一（账本匹配键；8 例锁定在 verify 脚本）─────────────────────
_EXT_TAILS = [".tar.zst", ".tar.gz", ".tgz", ".tar.xz", ".whl", ".rpm", ".deb",
              ".node", ".so", ".a", ".zip", ".xz", ".gz"]
# kind 归类：扩展名 → 账本 kind（白名单晋升比对口径同源）
_KIND_BY_EXT = {".whl": "whl", ".tgz": "tgz", ".tar.gz": "tgz", ".tar.xz": "tool",
                ".tar.zst": "tool", ".xz": "tool", ".zip": "tool", ".rpm": "rpm",
                ".deb": "rpm", ".so": "so", ".a": "a", ".node": "node_binding"}


def kind_of(filename: str) -> str:
    f = filename.lower()
    for ext, k in _KIND_BY_EXT.items():
        if f.endswith(ext):
            return k
    if "." not in f[1:] or f.endswith((".bin", ".elf")):
        # 裸可执行（无扩展名风格）归 binary——有扩展名但不认识的归 other（不轻易放行）
        return "binary"
    return "other"


def name_norm_of(filename: str) -> str:
    """文件名 → 归一软件名（去版本尾巴/架构尾巴，小写连字符）。
    torch-2.16.0+cpu-cp311-cp311-linux_loongarch64.whl → torch
    node-pty-1.1.0.tgz → node-pty
    node-v24.22.1-linux-loong64.tar.xz → node
    rolldown-binding-linux-loong64-gnu-1.2.7.tgz → rolldown-binding-linux-loong64-gnu
    规则：剥扩展名 → 剥 +tag → 在"首个带点的版本号段"处截断；无点纯数字段也截。
    """
    f = filename
    for ext in _EXT_TAILS:
        if f.lower().endswith(ext):
            f = f[: -len(ext)]
            break
    f = re.sub(r"\+.*$", "", f)                      # +cpu / +local 版本后缀
    # 从右往左找版本号段：-1.2 / -1.2.3 / -v24.22.1 / -12
    m = list(re.finditer(r"-v?(\d+(\.\d+)*)", f))
    if m:
        f = f[: m[0].start()]
    f = f.strip("-_. ")
    return f.lower() or "unnamed"


def version_of(filename: str) -> str:
    """尽力解析版本（首个数字段组），失败空串——账本展示用，不参与匹配硬条件。"""
    m = re.search(r"-v?(\d+(?:\.\d+)+|\d+)", filename)
    return m.group(1) if m else ""


# ── meta 身份卡（晋升校验核心，总设计 §3.3）──────────────────────
_META_REQUIRED = ("name", "version", "kind", "toolchain")
# toolchain 至少一选一：按 kind 侧重（whl 看 python，node_binding/tgz(node系) 看 node，
# binary/so/a/rpm 看 gcc 或 glibc）——"齐"的底线是"能判断兼容性"，全 unknown 则放行但标 unknown
_TC_BY_KIND = {
    "whl": ["python"],
    "node_binding": ["node"],
    "so": ["gcc", "glibc"], "a": ["gcc", "glibc"], "binary": ["glibc", "gcc"],
    "rpm": ["glibc"], "tgz": ["node", "python", "gcc"], "tool": ["version_any"],
}


def read_meta(path: Path) -> dict | None:
    """读 <文件>.meta.json；缺失/坏 JSON → None（调用侧按拒晋升处理）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_meta(meta: dict | None, kind: str) -> tuple[bool, str, dict]:
    """返回 (是否合格, 原因, 规范化后的 toolchain)。不合格拒晋升。"""
    if not isinstance(meta, dict):
        return False, "缺 meta.json", {}
    tc = meta.get("toolchain")
    if not isinstance(tc, dict) or not any(str(v).strip() for v in tc.values()):
        return False, f"meta.toolchain 空（{kind} 类需至少一个构建上下文）", {}
    need = _TC_BY_KIND.get(kind, [])
    if need and need != ["version_any"]:
        if not any(str(tc.get(k, "")).strip() for k in need):
            return False, f"toolchain 缺 {'/'.join(need)} 之一", tc
    return True, "", tc


def is_generic_name(filename: str) -> bool:
    """通用名判定（总设计 §5：货架靠文件名认货）——artifacts.tar.gz 这类不允许上货架。"""
    base = filename
    for ext in _EXT_TAILS:
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    return base.lower() in ("artifacts", "output", "package", "dist", "build", "release",
                            "a", "out", "result", "bundle")


def safe_shelf_name(name: str, fallback_norm: str, version: str, original: str) -> tuple[str, bool]:
    """货架落名：原名太通用 → 按 <名>-<版本>.<原扩展名> 改名。返回 (新名, 是否改过)。"""
    if not is_generic_name(original):
        return original, False
    ext = ""
    for e in _EXT_TAILS:
        if original.lower().endswith(e):
            ext = original[len(original) - len(e):]
            break
    stem = (name or fallback_norm or "artifact").strip()
    stem = re.sub(r"[^\w.\-+]", "-", stem)[:150]
    v = f"-{version}" if version else ""
    return f"{stem}{v}{ext or '.bin'}", True


def _reindex_identity(t) -> str:
    """存量回填的来源标识：upload 包在盘→现算（历史任务 source_sha256 列也空，文件是
    唯一权威）；git 任务从任务日志 commit= 回捞。"""
    if getattr(t, "source_type", "") == "upload" and t.source_path and Path(t.source_path).is_file():
        pfx = "archive:" + sha256_of(t.source_path)[:16]
        if not (getattr(t, "source_sha256", "") or ""):
            t.source_sha256 = sha256_of(t.source_path)   # 顺手补列（幂等，一次算清）
        return pfx
    m = re.search(r"容器内源码已就绪 \(commit=([0-9a-f]{7,40})\)", (t.log_output or ""))
    return f"git:{m.group(1)[:40]}" if m else ""


def _source_identity(task, state: dict) -> str:
    """来源标识（INDEX/prompt 复用对比的锚；11-04 拍板补全 upload 路）：
    git 任务 → git:<stage4 探测 commit>（平台侧，非 AI 自报）
    upload 任务 → archive:<源码包 sha256 前 16>——两路"同源码=同身份"从此可程序化对比"""
    ch = (state.get("commit_hash") or "").strip()
    if ch:
        return f"git:{ch[:40]}"
    pkg_sha = (getattr(task, "source_sha256", "") or "")
    if getattr(task, "source_type", "") == "upload" and pkg_sha:
        return f"archive:{pkg_sha[:16]}"
    return ""


def sha256_of(path) -> str:
    """平台自算文件指纹（身份钉②，11-04）。流式读，晋升件 ≤500MB 秒级。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── 晋升三门禁（stage6 success 调，返回可晋升清单+拒绝原因账）──────
def collect_promotables(cache_out: Path, system_id: int) -> tuple[list[dict], list[str]]:
    """扫 cache_out → [{file, meta_path, name, name_norm, kind, size, meta}] 合格件。
    门禁：kind 白名单 ∧ 单件≤max_item_mb ∧ meta 齐；总量超 per_task 截尾记拒因。
    ⚠ 不做 move——pending 移置与审批入库由调用方/端点执行（本函数纯判定，可测）。

    ⚠ 必须递归（11-05 A95824 事故：AI 把 11MB 归档包放进 cache_out/llama.cpp/ 子目录，
    顶层 iterdir 对它完全失明——无晋升、无拒收、无审计，整件货无声蒸发）。
    拒收/留痕名一律用相对路径（llama.cpp/x.tgz），子目录里的漏网件也能点名。
    """
    out, rejects = [], []
    if not cache_out.is_dir():
        return out, rejects
    kinds = [k.strip() for k in str(get_cfg("artifact_cache.promote_kinds",
                                           "whl,tgz,rpm,binary,node_binding,so,a")).split(",")]
    max_item = int(get_cfg("artifact_cache.max_item_mb", 500)) * 1024 * 1024
    budget = int(get_cfg("artifact_cache.per_task_promote_mb", 1024)) * 1024 * 1024
    used = 0
    for f in sorted(cache_out.rglob("*")):
        if not f.is_file() or f.name.endswith(".meta.json"):
            continue
        rel = str(f.relative_to(cache_out))          # 相对路径：平铺件=原名，子目录件=dir/name
        kind = kind_of(f.name)
        size = f.stat().st_size
        if kind not in kinds:
            rejects.append(f"{rel}: kind={kind} 不在白名单")
            continue
        if size > max_item:
            rejects.append(f"{rel}: 单件 {size // (1024 * 1024)}MB 超上限")
            continue
        meta = read_meta(f.with_name(f.name + ".meta.json"))
        ok, why, tc = check_meta(meta, kind)
        if not ok:
            rejects.append(f"{rel}: {why}")
            continue
        if used + size > budget:
            rejects.append(f"{rel}: 超单任务晋升总量（前件已占满）")
            continue
        used += size
        it_sha = sha256_of(f)
        norm = (meta.get("name") if isinstance(meta, dict) else "") or name_norm_of(f.name)
        shelf, renamed = safe_shelf_name(norm, name_norm_of(f.name),
                                         meta.get("version", "") if isinstance(meta, dict) else "",
                                         f.name)
        out.append({"file": f, "meta": meta, "toolchain": tc, "kind": kind, "size": size,
                    "name": shelf, "name_norm": norm.strip().lower() or name_norm_of(f.name),
                    "version": meta.get("version", "") if isinstance(meta, dict) else "",
                    "system_id": system_id, "renamed": renamed, "sha256": it_sha})
    return out, rejects


# ── INDEX.md（动态查库入口，平台生成；总设计 增补A）─────────────────
def regenerate_index(db, system_id: int) -> str:
    """货架 = approved(含存量"")∪upload 且未软删的行；每次库变更调用（幂等重写）。
    只报事实不做判断——用不用是 AI 的事。

    ⚠ 头部必须 db.flush()：生产 SessionLocal 是 autoflush=False（models.py:72），
    调用方"改行→立即重建 INDEX"（approve 置 status/cache_path、delete 置 deleted_at）
    的脏改动不 flush 则本函数 query 读 DB 见旧值——11-04 真机首审 node-pty 上架后
    INDEX 生成为空的根因（沙箱 autoflush=True 掩盖了它）。flush 同事务内零风险，
    最终 commit/rollback 语义不变。"""
    from app.models import ArtifactIndex
    db.flush()
    rows = db.query(ArtifactIndex).filter(
        ArtifactIndex.system_id == system_id,
        ArtifactIndex.deleted_at.is_(None),
        ArtifactIndex.status.in_(("approved", "")),
        ArtifactIndex.cache_path != "",
    ).order_by(ArtifactIndex.hits.desc()).all()
    d = shelf_dir(system_id)
    d.mkdir(parents=True, exist_ok=True)
    lines = ["# 复用库货架（平台自动生成，勿手改；缺依赖先查此表）",
             "",
             "| 软件名 | 文件 | 版本 | 类型 | 构建上下文 | 大小 | 复用次数 | 来源标识 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        try:
            tc = json.loads(r.build_env or "{}")
        except ValueError:
            tc = {}
        tc_txt = " ".join(f"{k}{v}" for k, v in list(tc.items())[:4]) or "⚠unknown"
        lines.append(f"| {r.name_norm} | {r.name} | {r.version or '-'} | {r.kind} | "
                     f"{tc_txt} | {max(r.size_bytes, 0) // (1024 * 1024)}MB | "
                     f"{r.hits}用/{r.views or 0}览 | {r.source_commit[:24] or '-'} |")
    if not rows:
        lines.append("| （空——暂无已审核产物） | | | | | | | |")
    (d / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(d / "INDEX.md")


def shelf_summary(db, system_id: int, limit: int = 15) -> str:
    """stage5 prompt 注入用的紧凑摘要（账本事前排好，AI 睁眼见货架）；空库返回空串。"""
    from app.models import ArtifactIndex
    rows = db.query(ArtifactIndex).filter(
        ArtifactIndex.system_id.in_((COMMON_SYSTEM_ID, int(system_id))),   # 公共池并进摘要
        ArtifactIndex.deleted_at.is_(None),
        ArtifactIndex.status.in_(("approved", "")),
        ArtifactIndex.cache_path != "",          # 仅账本的回填行不进 AI 摘要（真在架才算货架）
    ).order_by(ArtifactIndex.hits.desc()).all()
    baseline = system_baseline(db, system_id)
    rows = [r for r in rows
            if r.system_id != COMMON_SYSTEM_ID or _common_passes(r, baseline)][:limit]
    if not rows:
        return ""
    items = []
    for r in rows:
        tag = "" if r.build_env else " [上下文未知]"
        items.append(f"- {r.name_norm}/（{r.name}，{r.kind}，"
                     f"复用{r.hits}次{tag}）")
    return "\n".join(items)


# ── 审批动作核心（端点调；事务由调用方 commit）────────────────────
def approve_row(db, row, operator_id: int) -> str:
    """pending → 在架：文件从 promote_pending move 进货架 + cache_path 落值 + INDEX 重生成。
    返回相对路径。源文件不存在 = 数据异常，抛错由端点转 404（不静默）。"""
    from app.config import artifacts_dir
    from app.models import ArtifactIndex
    if row.status != "pending":
        raise ValueError(f"仅 pending 可审批（当前 {row.status or '存量'}）")
    src = artifacts_dir() / row.source_task_key / "promote_pending" / row.name
    if not src.is_file():
        raise FileNotFoundError(f"待晋升文件不在盘：{row.name}")
    if row.sha256 and sha256_of(src) != row.sha256:
        # 身份钉复核：提取→审批窗口内文件变了（篡改/损坏/人换货）=拒绝上架，账本不动
        raise ValueError("指纹与晋升时不符（文件可能被改动），拒绝上架；请驳回重验")
    dst_dir = system_cache_dir(row.system_id) / row.name_norm
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst_dir / row.name))
    meta_src = src.with_name(src.name + ".meta.json")
    if meta_src.is_file():
        shutil.move(str(meta_src), str(dst_dir / (row.name + ".meta.json")))
    rel = f"{row.system_id}/{row.name_norm}/{row.name}"
    # 同名覆盖必去重（总设计§3.3"新版本赢"的后半段；11-04 R2 复验实测暴露：物理文件
    # move 天然覆盖但账本留双行 → INDEX 双行、flush_hits 按 name_norm 全行计数=翻倍）
    prev = db.query(ArtifactIndex).filter(
        ArtifactIndex.system_id == row.system_id,
        ArtifactIndex.name_norm == row.name_norm,
        ArtifactIndex.name == row.name,
        ArtifactIndex.deleted_at.is_(None),
        ArtifactIndex.status.in_(("approved", "")),
        ArtifactIndex.id != row.id,
    ).all()
    for p in prev:
        row.hits = (row.hits or 0) + (p.hits or 0)      # 复用史随货架槽位迁移
        row.views = (row.views or 0) + (p.views or 0)   # views 同步迁（11-04 实测漏迁修正）
        p.deleted_at = datetime.utcnow()
        p.note = ((p.note or "") + f" [superseded-by #{row.id}]").strip()[:300]
    row.status = "approved"
    row.cache_path = rel
    row.note = (row.note or "")[:280]
    regenerate_index(db, row.system_id)
    return rel


def reject_row(db, row, reason: str) -> None:
    """驳回：账本记 rejected+理由留痕；文件随 promote_pending 生命周期清理（不即删——
    留反悔窗口，purge 时一并走）。出注入面：本就不在架，INDEX 无需动。"""
    if row.status != "pending":
        raise ValueError("仅 pending 可驳回")
    row.status = "rejected"
    row.note = (reason or "驳回")[:300]


# ── hits 被动记账（引擎 chunk 循环调，行为数据非汇报数据）────────
_CACHE_USE_RE = re.compile(r"/work/cache/(?!_out)[\w./\-]*")


def record_hits(db, system_id: int, command_text: str) -> int:
    """扫一条 execute 命令文本；命中在架文件/目录 → hits+1、last_used_at=now。
    返回命中行数。cache_out（晋升口）不算复用。"""
    from app.models import ArtifactIndex
    segs = _CACHE_USE_RE.findall(command_text or "")
    if not segs:
        return 0
    n = 0
    now = datetime.utcnow()
    for seg in segs:
        # seg 恒以 /work/cache/ 开头 → 去前缀后首段即 name_norm（parts 索引曾两轮修歪，
        # 教训：切 "work/cache/x" 后 norm 在 [2] 不在 [0]/[1]——统一用前缀剥离，别再数）
        rest = seg[len("/work/cache/"):].strip("/")
        parts = rest.split("/")
        if len(parts) >= 2 and parts[-1] != "INDEX.md":
            row = db.query(ArtifactIndex).filter(
                ArtifactIndex.system_id == system_id,
                ArtifactIndex.name_norm == parts[0].lower(),
                ArtifactIndex.deleted_at.is_(None),
                ArtifactIndex.status.in_(("approved", "")),
            ).first()
            if row:
                row.hits = (row.hits or 0) + 1
                row.last_used_at = now
                n += 1
    if n:
        db.commit()
    return n


# ── reindex：存量回填 + 漂移对账（幂等，只报不手）────────────────
def reindex(db) -> dict:
    """1) 扫 artifacts/<task>/artifacts/ 树为历史成功任务建账（cache_path 空=仅账本）；
    2) 对账：账本 cache_path 指向的文件不在盘 → 标 missing 注记（警示，不删行不删文件）。
    幂等：按 source_task_id+name upsert。返回统计。"""
    from app.config import artifacts_dir
    from app.models import ArtifactIndex, CompileTask, TaskStatus
    art = artifacts_dir()
    created = updated = missing = 0
    tasks = db.query(CompileTask).filter(
        CompileTask.status == TaskStatus.SUCCESS.value,
        CompileTask.deleted_at.is_(None)).all()
    for t in tasks:
        outd = art / t.task_id / "artifacts" / "output"
        scan = outd if outd.is_dir() else (art / t.task_id / "artifacts")
        if not scan.is_dir():
            continue
        for f in scan.rglob("*"):
            if not f.is_file() or f.name.endswith(".meta.json") or f.name == "BUILD_NOTES.md":
                continue
            if not _is_deliverable(f.name):
                continue
            rel = f.relative_to(scan)
            nm = f.name
            row = db.query(ArtifactIndex).filter(
                ArtifactIndex.source_task_id == t.id,
                ArtifactIndex.name == str(rel)).first()
            if row is None:
                _cid = _reindex_identity(t)
                row = ArtifactIndex(source_task_id=t.id, source_task_key=t.task_id,
                                    name=str(rel), name_norm=name_norm_of(nm),
                                    version=version_of(nm), kind=kind_of(nm),
                                    origin="task", status="", size_bytes=f.stat().st_size,
                                    system_id=t.system_id, sha256=sha256_of(f),
                                    source_commit=_cid,
                                    note="存量回填(无meta,上下文未知)")
                db.add(row)
                created += 1
            else:
                row.size_bytes = f.stat().st_size
                if not row.sha256:
                    row.sha256 = sha256_of(f)
                if not row.source_commit:
                    row.source_commit = _reindex_identity(t)
                updated += 1
    for r in db.query(ArtifactIndex).filter(ArtifactIndex.cache_path != "",
                                            ArtifactIndex.deleted_at.is_(None)).all():
        if not (cache_root() / r.cache_path).is_file():
            if "missing" not in (r.note or ""):
                r.note = ((r.note or "") + " [missing:账本指路文件不在盘]").strip()[:300]
                missing += 1
    db.commit()
    # 对账语义补全：INDEX 是账本的确定性投影（"平台生成勿手改"），重扫后刷新涉及系统——
    # 新列(身份钉/双账)格式随 reindex 自动跟上（11-04 存量回填后 INDEX 仍是旧表头暴露）
    for sid in {r.system_id for r in db.query(ArtifactIndex).filter(
            ArtifactIndex.deleted_at.is_(None)).distinct().all() if hasattr(r, "system_id")}:
        try:
            regenerate_index(db, sid)
        except Exception:
            pass
    return {"created": created, "updated": updated, "missing_marked": missing}


def _is_deliverable(name: str) -> bool:
    """交付物粗筛（历史回填用；与引擎 _is_deliverable_artifact 同族但独立——
    那边按整文件/目录名，这里只拿得到 basename，规则子集）。"""
    lower = name.lower()
    if lower.startswith(("verify", "test_", ".venv", "node_modules", "__pycache__")):
        return False
    if lower.endswith((".log", ".tmp", ".pyc", ".meta.json")):
        return False
    return True


# ── 磁盘简报（P3：只报不删，11-03 拍板）───────────────────────────
_disk_cache: dict = {"t": 0.0, "data": None}


def disk_report(ttl: float = 60.0) -> dict:
    """磁盘简报（60s TTL 缓存：分钟级变化数据，连续开页不重复遍历——
    11-05 .209 案：大 artifacts 树 rglob 数十秒，调用方必须 to_thread，
    本缓存只是第二道；ttl=0 强制实时）。"""
    import time as _t
    now = _t.monotonic()
    if ttl and _disk_cache["data"] is not None and now - _disk_cache["t"] < ttl:
        return _disk_cache["data"]
    import shutil as _sh
    du = _sh.disk_usage("/")
    pct = round(du.used / du.total * 100, 1)
    def _dir_mb(p: Path) -> int:
        if not p.is_dir():
            return 0
        total = 0
        for f in _walk_files(p):
            try:
                total += f.stat().st_size
            except OSError:
                pass
        return total // (1024 * 1024)
    from app.config import artifacts_dir
    art = artifacts_dir()
    warn = int(get_cfg("artifact_cache.disk_warn_pct", 85))
    data = {"root_used_pct": pct, "root_free_gb": round(du.free / 2**30, 1),
            "artifacts_mb": _dir_mb(art), "cache_mb": _dir_mb(cache_root()),
            "warn": pct >= warn, "warn_pct": warn}
    _disk_cache["t"] = now
    _disk_cache["data"] = data
    return data


def _walk_files(p: Path):
    """容错遍历（坏文件跳过，不因一个 lstat 炸整个简报）。"""
    try:
        yield from (x for x in p.rglob("*") if x.is_file())
    except OSError:
        return


# ── 期二：容器挂载构造（stage3 调；独立成函数=可单测 ro/rw/降级）──────────
def ver_tuple(v: str) -> tuple:
    """'2.38'→(2,38)；脏值→(0,)（排序沉底）。"""
    try:
        return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)
    except Exception:
        return (0,)


_GLIBC_LINKABLE = (".whl", ".tgz", ".tar.gz", ".tar.xz", ".tar.zst", ".zip", ".so", ".a", ".node")


def extract_glibc_requires(path) -> str:
    """运行需求下限机提（11-05 三层闸·第二层）：ELF 直接 objdump -T；
    tar/zip 归档展开扫成员 ELF 取最大（上限 300 个成员/单成员 200MB，防解包炸弹）。
    判不了返回空串（未知≠拒绝：视图按"未知放行+标注"处理，避免误杀源码包类）。"""
    import subprocess as _sp, tempfile as _tf, zipfile as _zf
    p = Path(path)
    try:
        with open(p, "rb") as f:
            magic = f.read(4)
        if magic == b"\x7fELF":
            return _max_glibc_sym([p])
        if p.suffix in (".whl", ".zip") or p.name.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.zst")):
            with _tf.TemporaryDirectory(prefix="hermes-glibc-") as td:
                tdp = Path(td)
                if p.suffix == ".zip" or p.suffix == ".whl":
                    with _zf.ZipFile(p) as z:
                        for m in z.infolist()[:300]:
                            if not m.is_dir() and m.file_size <= 200 * 1024 * 1024:
                                z.extract(m, tdp)
                else:
                    r = _sp.run(["tar", "-xf", str(p), "-C", td, "--wildcards", "*"],
                                capture_output=True, timeout=120)
                    if r.returncode != 0:
                        return ""
                el = []
                for f in sorted(tdp.rglob("*")):
                    if len(el) >= 300:
                        break
                    if f.is_file() and not f.name.endswith(".meta.json"):
                        try:
                            with open(f, "rb") as fh:
                                if fh.read(4) == b"\x7fELF":
                                    el.append(f)
                        except OSError:
                            pass
                return _max_glibc_sym(el)
    except Exception:
        return ""
    return ""


def _max_glibc_sym(files) -> str:
    """对 ELF 集合 objdump -T 取 GLIBC_x.y.z 最大需求（无需求/静态/非动态=空串）。"""
    import subprocess as _sp
    best = (0,)
    any_sym = False
    for f in files:
        try:
            r = _sp.run(["objdump", "-T", str(f)], capture_output=True, text=True, timeout=30).stdout
        except Exception:
            continue
        for m in re.findall(r"GLIBC_(\d+\.\d+(?:\.\d+)?)", r):
            any_sym = True
            t = ver_tuple(m)
            if t > best:
                best = t
    if not any_sym:
        return ""
    return ".".join(str(x) for x in best)


def system_baseline(db, system_id: int) -> str:
    """目标系统 glibc 基线（配给判据；查不到/空=无基线）。"""
    try:
        from app.models import TargetSystem
        s = db.query(TargetSystem).filter(TargetSystem.system_id == int(system_id)).first()
        return (s.glibc_version or "").strip() if s else ""
    except Exception:
        return ""


def _common_passes(row, baseline: str) -> bool:
    """公共池件对某系统的配给判定（11-05 三层闸·第三层）：
    无基线=不过滤（老行为）；件需求未知=放行（视图仍标 通用，源码包类靠它）；
    已知需求 ≤ 基线=放行；> 基线=不出现在该系统货架（SP2 件不进老 Loongnix）。"""
    if not baseline:
        return True
    req = (row.glibc_requires or "").strip()
    if not req:
        return True
    return ver_tuple(req) <= ver_tuple(baseline)


def _link_pool_into_view(db, pool_id: int, view: Path, baseline: str = "") -> int:
    """把某池（pool_id 含 COMMON=0）在架件硬链接进合并视图，保留 <norm>/<file> 相对结构。
    硬链接而非软链：容器内软链指向宿主机绝对路径=挂载面外死链；同盘 inode 跨 bind 有效。
    账本口径与 regenerate_index 一致（approved∪存量"" ∧未软删∧cache_path 非空）。
    顺序约定：先 COMMON 后 system——同 norm+file 冲突时系统池 unlink 重链=系统件赢。"""
    from app.models import ArtifactIndex
    n = 0
    rows = db.query(ArtifactIndex).filter(
        ArtifactIndex.system_id == pool_id,
        ArtifactIndex.deleted_at.is_(None),
        ArtifactIndex.status.in_(("approved", "")),
        ArtifactIndex.cache_path != "",
    ).all()
    if pool_id == COMMON_SYSTEM_ID:
        rows = [r for r in rows if _common_passes(r, baseline)]   # 三层闸·配给过滤
    for r in rows:
        src = cache_root() / r.cache_path
        if not src.is_file():
            continue                                   # 账实差跳过——对账归 reindex
        prefix = f"{pool_id}/"
        rel = r.cache_path[len(prefix):] if r.cache_path.startswith(prefix) else Path(r.cache_path).name
        dst = view / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            os.link(src, dst)
        except OSError:
            # 跨设备(artifacts 挪独立盘的部署)/权限 → **降级复制**（正确性优先：
            # 静默缺件=INDEX 有行货不在，AI 会迷惑；11-05 探针实测 EXDEV 教训）
            try:
                shutil.copy2(src, dst)
            except OSError:
                continue                               # 复制也失败才弃件
        n += 1
    return n


def build_shelf_view(db, system_id: int, container_work) -> Path:
    """任务级合并货架视图（11-05 公共池）：公共池+系统池 → container_work/cache_view/，
    INDEX.md 按两池账本实时合成（带『通用』前缀标记）。每任务重建=零陈旧；视图在
    container_work 下随 stage6/7 天清扫自然消亡，不养垃圾。异常由调用方兜退化。"""
    from app.models import ArtifactIndex
    baseline = system_baseline(db, system_id)     # 11-05 配给基线（空=不过滤）
    view = Path(container_work) / "cache_view"
    if view.is_dir():
        shutil.rmtree(view, ignore_errors=True)
    view.mkdir(parents=True, exist_ok=True)
    _link_pool_into_view(db, COMMON_SYSTEM_ID, view, baseline)
    _link_pool_into_view(db, int(system_id), view, baseline)
    rows = db.query(ArtifactIndex).filter(
        ArtifactIndex.system_id.in_((COMMON_SYSTEM_ID, int(system_id))),
        ArtifactIndex.deleted_at.is_(None),
        ArtifactIndex.status.in_(("approved", "")),
        ArtifactIndex.cache_path != "",
    ).order_by(ArtifactIndex.hits.desc()).all()
    rows = [r for r in rows if r.system_id != COMMON_SYSTEM_ID or _common_passes(r, baseline)]
    lines = ["# 复用库货架（平台自动生成，勿手改；缺依赖先查此表）",
             "# 行首带『通用』= 所有系统可用的工具母包（公共池）；其余为本系统已验证产物",
             "",
             "| 软件名 | 文件 | 版本 | 类型 | 构建上下文 | 大小 | 复用次数 | 来源标识 |",
             "|---|---|---|---|---|---|---|---|"]
    if not rows:
        lines.append("| （空——暂无已审核产物） | | | | | | | |")
    for r in rows:
        try:
            tc = json.loads(r.build_env or "{}")
        except ValueError:
            tc = {}
        tc_txt = " ".join(f"{k}{v}" for k, v in list(tc.items())[:4]) or "⚠unknown"
        pool = "通用·" if r.system_id == COMMON_SYSTEM_ID else ""
        req = f" 需≥{r.glibc_requires}" if r.system_id == COMMON_SYSTEM_ID and r.glibc_requires else ""
        lines.append(f"| {pool}{r.name_norm} | {r.name} | {r.version or '-'} | {r.kind} | "
                     f"{tc_txt}{req} | {max(r.size_bytes, 0) // (1024 * 1024)}MB | "
                     f"{r.hits}用/{r.views or 0}览 | {r.source_commit[:24] or '-'} |")
    (view / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return view


def cache_volumes(state: dict, container_work, volumes: dict, log_fn=None) -> None:
    """向 volumes 注入 /work/cache(ro **合并视图**：系统池+公共池) + /work/cache_out(rw)。
    任何失败只 warn——复用是增益不是依赖，主流程永不因挂载死。

    ⚠ session 口径（R2 冻结事故教训，不可违）：视图查询用**自建短 session、finally close**，
    绝不接 run_task 的长寿命 db——stage3 在长 session 里 SELECT 不闭合=idle-in-tx 持行锁
    冻 worker 的同款触发面。视图构建失败 → 退化裸系统池（公共池暂缺=11-04 旧行为），warn 不拦。"""
    from app.config import get_cfg
    if not get_cfg("artifact_cache.enabled", True):
        return
    try:
        cdir = system_cache_dir(int(state["system_id"]))
        cdir.mkdir(parents=True, exist_ok=True)
        cout = container_work / "cache_out"
        cout.mkdir(parents=True, exist_ok=True)
        mount = cdir
        try:
            from app.models import SessionLocal
            _vdb = SessionLocal()
            try:
                mount = build_shelf_view(_vdb, int(state["system_id"]), container_work)
            finally:
                _vdb.close()
        except Exception as ve:
            if log_fn:
                log_fn(state, "warn", f"[阶段3] 货架合并视图退化裸池（公共池暂缺）: {ve}")
        volumes[str(mount)] = {"bind": "/work/cache", "mode": "ro"}
        volumes[str(cout)] = {"bind": "/work/cache_out", "mode": "rw"}
    except Exception as e:
        if log_fn:
            log_fn(state, "warn", f"[阶段3] 复用库挂载跳过（不影响编译）: {e}")


# ── 期二：hits 被动记账（chunk 循环调，state 延后账；同 _record_skill_match 先例）──
def scan_cache_use(state: dict, command_text: str) -> None:
    """扫一条 execute 命令：命中 /work/cache/<norm>/<file> → state['_cache_hits']。
    cache_out（晋升口）与 INDEX.md（查表本身）不计。分两账（11-04 用户拍板"查≠用"）：
      用(hits)：cp/mv/install/tar -x*/unzip/直接引用安装 —— 真消费交付
      览(views)：cat/ls/head/tar -t*/grep/find/stat/diff/du —— 查阅行为
    前缀命中"用"即计用（一条命令里既有 tar -tzf 又有 cp 时从严归用）。"""
    for seg in _CACHE_USE_RE.findall(command_text or ""):
        rest = seg[len("/work/cache/"):].strip("/")   # 前缀剥离口径（别再数下标，教训在案）
        parts = rest.split("/")
        if len(parts) >= 2 and parts[-1] != "INDEX.md":
            norm = parts[0].lower()
            if _USE_RE.search(command_text or ""):
                state.setdefault("_cache_hits", set()).add(norm)
            else:
                state.setdefault("_cache_views", set()).add(norm)


_USE_RE = re.compile(
    # 用动词判定（11-05 修正）：tar 三种真形态全收——
    #   tar -C /usr/local -xzf f   （DADECE 案：-C 前置旧式漏判，hits 真丢）
    #   tar -xzf f / tar xzf f     （-x 可藏合写 flag 里：-\w*x\w*）
    #   清点是览：-tzf/-tf 不含 x，天然走 views（-tzf 的 t 不会误配）
    r"(?:^|[;&|]|\bsudo)\s*(?:cp\b|mv\b|install\b|tar\s+(?:\S+\s+)*?-?\w*x\w*"
    r"|unzip\b|dpkg\s+-i|rpm\s+-i|npm\s+i(?:nstall)?\b|pip\s+install|python\d*\s+-m\s+pip\s+install)"
    r"[^\n]*?/work/cache/")


def flush_hits(db, system_id: int, names: set, views: "set | None" = None) -> int:
    """state 延后账落库：仅已上架行计。names→hits(消费)，views→views(查阅)。返回消费命中行数。"""
    from app.models import ArtifactIndex
    if not names and not views:
        return 0
    now = datetime.utcnow()
    n = 0
    for norm in (names or set()):
        rows = db.query(ArtifactIndex).filter(
            ArtifactIndex.system_id.in_((COMMON_SYSTEM_ID, int(system_id))),
            ArtifactIndex.name_norm == norm,
            ArtifactIndex.deleted_at.is_(None),
            ArtifactIndex.status.in_(("approved", "")),
            ArtifactIndex.cache_path != "",
        ).all()
        for r in rows:
            r.hits = (r.hits or 0) + 1
            r.last_used_at = now
            n += 1
    for norm in (views or set()):
        if norm in (names or set()):
            continue          # 同任务同件"用"优先，不重复计览
        rows = db.query(ArtifactIndex).filter(
            ArtifactIndex.system_id.in_((COMMON_SYSTEM_ID, int(system_id))),
            ArtifactIndex.name_norm == norm,
            ArtifactIndex.deleted_at.is_(None),
            ArtifactIndex.status.in_(("approved", "")),
            ArtifactIndex.cache_path != "",
        ).all()
        for r in rows:
            r.views = (r.views or 0) + 1
    return n


# ── 期二：晋升提取（run_task success 调：合格件移 promote_pending + 建 pending 行 + 通知）──
def extract_promotions(db, task, state: dict, art_root) -> tuple[list, list]:
    """返回 (晋升候选名, 拒收原因)。文件移置 promote_pending（随任务生命周期，
    不进 container_work——那里 7 天自动清）；账本 pending 行等审批。"""
    from app.config import get_cfg
    from app.models import ArtifactIndex, User
    from app.services.notify import notify
    from app.services.audit import log_audit
    if not get_cfg("artifact_cache.enabled", True) or state.get("status") != "success":
        return [], []
    cache_out = art_root / "container_work" / "cache_out"
    items, rejects = collect_promotables(cache_out, int(state["system_id"]))
    if not items:
        # 全拒也留痕+通知：管理员看不见"AI 交过货被门禁拒"就永远救不了它（11-04 真机：
        # node-pty 任务 meta.toolchain 空被拒，只有任务 log 知道——信息孤岛）
        if rejects:
            from app.services.audit import log_audit
            from app.services.notify import notify as _nt
            from app.models import User as _U
            log_audit(db, user_id=None, action="artifact_rejected", target_type="compile_task",
                      target_id=task.id, target_key=task.task_id,
                      details=f"全拒收:{rejects}")
            try:
                for adm in db.query(_U).filter(_U.role == "admin", _U.is_active == True,
                                               _U.deleted_at.is_(None)).all():
                    _nt(db, user_id=adm.user_id, type="artifact_rejected",
                        title=f"晋升被门禁拒收：{task.task_id}（{len(rejects)} 件）",
                        body="；".join(rejects)[:200], link="")
            except Exception:
                pass
        return [], rejects
    pend_dir = art_root / "promote_pending"
    pend_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for it in items:
        src = it["file"]
        dst = pend_dir / it["name"]
        import shutil as _sh
        _sh.move(str(src), str(dst))
        meta_src = src.with_name(src.name + ".meta.json")
        if meta_src.is_file():
            _sh.move(str(meta_src), str(pend_dir / (it["name"] + ".meta.json")))
        row = ArtifactIndex(
            name=it["name"], name_norm=it["name_norm"], version=it["version"],
            system_id=it["system_id"], kind=it["kind"], origin="task", status="pending",
            source_task_id=task.id, source_task_key=task.task_id,
            build_env=json.dumps(it.get("toolchain") or {}, ensure_ascii=False),
            image=state.get("docker_image") or "", size_bytes=it["size"],
            source_commit=_source_identity(task, state)[:64],
            sha256=it.get("sha256") or "")
        db.add(row)
        names.append(it["name"])
    log_audit(db, user_id=None, action="artifact_pending", target_type="compile_task",
              target_id=task.id, target_key=task.task_id,
              details=f"候选={names},拒收={rejects}")
    # 通知全体 admin（T15 best-effort，自发排除不适用——系统发起）
    try:
        for adm in db.query(User).filter(User.role == "admin", User.is_active == True,
                                         User.deleted_at.is_(None)).all():
            notify(db, user_id=adm.user_id, type="artifact_pending",
                   title=f"产物待晋升审批：{task.task_id} 提交 {len(names)} 件",
                   body="、".join(names)[:200], link="")
    except Exception:
        pass
    return names, rejects
