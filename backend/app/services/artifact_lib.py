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
                # deb 独立 kind（11-17 交付矩阵）：此前 ".deb"→rpm 系误归，货架/视图
                # 无法按格式过滤。⚠ 必须与 config.yaml promote_kinds 的 deb 同笔生效
                # （只改一边=deb 晋升被白名单拒），201/.209 同 commit 落地
                # jar=Java 归档 kind（11-24 nacos 案：loongarch 专版 rocksdbjni jar
                # 被 kind=other 拒晋升；同 deb 教训，promote_kinds 已同笔加 jar）
                ".deb": "deb", ".so": "so", ".a": "a", ".node": "node_binding",
                # doc 类（11-26 A 档）：RUN.md/BUILD_NOTES/PACKAGE.json/patch——
                # 说明文档随包上货架，AI cat INDEX 即见"这软件有运行说明"。
                # ⚠ 必须与 config.yaml promote_kinds 的 doc 同笔生效（deb 教训）
                ".jar": "jar", ".md": "doc", ".patch": "doc"}   # exe 类不列：Linux ELF .exe 由下方 magic 兜底归 binary（保守正确）


def kind_of(filename: str, file_path: "str | None" = None) -> str:
    f = filename.lower()
    for ext, k in _KIND_BY_EXT.items():
        if f.endswith(ext):
            return k
    if "." not in f[1:] or f.endswith((".bin", ".elf")):
        # 裸可执行（无扩展名风格）归 binary——有扩展名但不认识的归 other（不轻易放行）
        return "binary"
    # 11-20（protoc-31.0.0 被拒案）：文件名带点号尾巴（版本号段）走到"不认识"分支
    # 并不一定不是可执行件——扩展名判据失效时用文件头说话：真 ELF magic 归 binary。
    # 无 file_path（如手工上传只有文件名）维持保守 other，宁漏勿误不误放行。
    if file_path:
        try:
            with open(file_path, "rb") as fh:
                if fh.read(4) == b"\x7fELF":
                    return "binary"
        except OSError:
            pass
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
    "rpm": ["glibc"], "deb": ["glibc"], "tgz": ["node", "python", "gcc"], "tool": ["version_any"],
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


def purge_shelf_files(db, row) -> list:
    """物理删货架文件+meta，搬空即收 <norm> 空壳（单件/批量共用，11-06 抽出）。
    cache_path 空=无文件可删返回 []；路径解析越出 cache 根抛 ValueError（端点转 400）。
    ⚠ 只删货架件本体——软删（deleted_at）由调用方负责，本函数不碰账本状态。"""
    root = cache_root().resolve()
    target = (root / row.cache_path).resolve()
    if not str(target).startswith(str(root) + "/"):
        raise ValueError("货架路径异常，拒绝物理删")
    removed = []
    for p in (target, target.with_name(target.name + ".meta.json")):
        if p.is_file():
            p.unlink()
            removed.append(p.name)
    d = target.parent
    if d.is_dir() and not any(d.iterdir()):   # 11-05 实弹遗留空目录教训（X11 锁在案）
        try:
            d.rmdir()
        except OSError:
            pass
    regenerate_index(db, row.system_id)       # 文件没了，货架表同步（软删行已被过滤）
    return removed


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
        kind = kind_of(f.name, str(f))
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
                    "runtime_libs": (meta.get("runtime_libs") if isinstance(meta, dict) else None) or [],
                    "system_id": system_id, "renamed": renamed, "sha256": it_sha})
    return out, rejects


# ── 货架列排序（11-26）：白名单映射，排序字段绝不裸拼；次级键恒 id desc
#    保证并列值（大量 dl=0）翻页稳定不重不漏。非法 sort_by 回调用方默认。
_SHELF_SORT_COLS = {"dl": "dl_count", "hits": "hits", "views": "views",
                    "size": "size_bytes", "created": "created_at",
                    "name": "name", "name_norm": "name_norm"}


def shelf_sort_order(sort_by: str, order: str, model, default):
    col = _SHELF_SORT_COLS.get((sort_by or "").strip())
    if col is None:
        return [default]
    expr = getattr(model, col)
    primary = expr.asc() if (order or "").strip() == "asc" else expr.desc()
    return [primary, model.id.desc()]


# ── INDEX.md（动态查库入口，平台生成；总设计 增补A）─────────────────
# 货架 v2 谓词：AI 侧（INDEX/摘要/挂载视图）只收 audience∉{human}；
# 空串=存量行（迁移默认 both 前也不该漏件），语义并入 both。
def _ai_visible():
    from app.models import ArtifactIndex
    return ArtifactIndex.audience.isnot(None) & (ArtifactIndex.audience != "human")


def _human_visible():
    """人侧（公开列表/下载）只收 audience∉{ai}；同理 "" 并入 both。"""
    from app.models import ArtifactIndex
    return ArtifactIndex.audience.isnot(None) & (ArtifactIndex.audience != "ai")


def migrate_docs_v1(db) -> int:
    """A 档存量迁移（11-26，幂等）：账本里"仅账本"态的说明文档行
    （RUN.md/BUILD_NOTES.md/PACKAGE.json/source-changes.patch）→ 从任务
    output 树复制进 promote_pending → 走 promote_shelved_row 内核入架。
    货架名钉 <组名>- 前缀防撞名；组名取同任务包行 name_norm。
    文件不在盘/指纹变/无包上下文 → 跳过留账本（平台不造货）。"""
    from app.config import artifacts_dir
    from app.models import ArtifactIndex
    rows = db.query(ArtifactIndex).filter(
        ArtifactIndex.deleted_at.is_(None),
        ArtifactIndex.cache_path == "",
        ArtifactIndex.name.in_(tuple(_DOC_FILES.keys())),
    ).order_by(ArtifactIndex.id.asc()).all()
    moved = 0
    for row in rows:
        tdir = artifacts_dir() / (row.source_task_key or "_none")
        src = tdir / "artifacts" / "output" / row.name
        if not src.is_file():
            continue
        if row.sha256 and sha256_of(src) != row.sha256:
            continue                      # 指纹变=留账本人判，不静默上架
        twins = db.query(ArtifactIndex).filter(
            ArtifactIndex.source_task_id == row.source_task_id,
            ArtifactIndex.deleted_at.is_(None),
            ArtifactIndex.cache_path != "",
            ArtifactIndex.kind != "doc",
        ).order_by(ArtifactIndex.size_bytes.desc()).all()
        if not twins:
            continue                      # 无包上下文=文档孤儿
        shelf = f"{twins[0].name_norm}-{row.name}"
        pend = tdir / "promote_pending"
        pend.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(pend / shelf))
        m0 = cache_root() / (twins[0].cache_path + ".meta.json")
        if m0.is_file():
            try:
                mj = json.loads(m0.read_text(encoding="utf-8"))
                mj["kind"] = "doc"
                (pend / (shelf + ".meta.json")).write_text(
                    json.dumps(mj, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        row.name, row.name_norm, row.kind = shelf, twins[0].name_norm, "doc"
        row.version = row.version or twins[0].version or ""
        row.build_env = row.build_env or twins[0].build_env or ""
        row.note = "A档存量文档迁移"
        try:
            promote_shelved_row(db, row)
            moved += 1
            db.commit()
        except Exception:
            db.rollback()
            continue
    return moved


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
        _ai_visible(),
    ).order_by(ArtifactIndex.hits.desc(), ArtifactIndex.created_at.desc()).all()
    d = shelf_dir(system_id)
    d.mkdir(parents=True, exist_ok=True)
    lines = ["# 复用库货架（平台自动生成，勿手改；缺依赖先查此表）",
             f"# 共 {len(rows)} 件，按复用次数排列（同次数新件在前）；行多时工具可能截断显示，"
             f"找不到就 grep /work/cache/INDEX.md 或 ls /work/cache/",
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
        _ai_visible(),                           # 货架v2：仅供下载的件 AI 不可见
    ).order_by(ArtifactIndex.hits.desc(), ArtifactIndex.created_at.desc()).all()
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
def promote_shelved_row(db, row, audience: str = "human") -> str:
    """账本行 pending→approved 的公共内核（v3 拆分：extract 自动上架/端点审批/
    存量迁移三处共用）：文件从 promote_pending move 进货架 + 同名"新版本赢"
    去重 + 状态/用途/路径落值 + 池 INDEX 重生成。返回 cache 相对路径。
    指纹不符抛 ValueError、文件缺抛 FileNotFoundError（调用方定夺）。"""
    from app.config import artifacts_dir
    from app.models import ArtifactIndex
    src = artifacts_dir() / row.source_task_key / "promote_pending" / row.name
    if not src.is_file():
        raise FileNotFoundError(f"待上架文件不在盘：{row.name}")
    if row.sha256 and sha256_of(src) != row.sha256:
        raise ValueError("指纹与入库时不符（文件可能被改动），拒绝上架")
    # ── 去重 D1（11-25 拍板）：同系统 ∧ 同 sha256 ∧ 文件真在架 = 同一文件铁证
    #    → 并槽：账并入既有行、新件即弃、本行软删墓碑（复用史随槽迁移，
    #    同 superseded 姿势）；仅账本行（无文件）不作合并目标——并过去货架
    #    还是没文件，重复生产留给 D2 面板人判。
    if row.sha256:
        twin = None
        cands = db.query(ArtifactIndex).filter(
            ArtifactIndex.system_id == row.system_id,
            ArtifactIndex.sha256 == row.sha256,
            ArtifactIndex.deleted_at.is_(None),
            ArtifactIndex.status.in_(("approved", "")),
            ArtifactIndex.cache_path != "",
            ArtifactIndex.id != row.id,
        ).order_by(ArtifactIndex.hits.desc(), ArtifactIndex.created_at.asc()).all()
        for c in cands:
            if (cache_root() / c.cache_path).is_file():
                twin = c
                break
        if twin is not None:
            twin.hits = (twin.hits or 0) + (row.hits or 0)
            twin.views = (twin.views or 0) + (row.views or 0)
            twin.dl_count = (twin.dl_count or 0) + (row.dl_count or 0)
            row.status = "approved"
            row.deleted_at = datetime.utcnow()
            row.note = ((row.note or "") + f" [dedup→#{twin.id}]").strip()[:300]
            src.unlink(missing_ok=True)
            src.with_name(src.name + ".meta.json").unlink(missing_ok=True)
            return twin.cache_path
    dst_dir = system_cache_dir(row.system_id) / row.name_norm
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst_dir / row.name))
    meta_src = src.with_name(src.name + ".meta.json")
    if meta_src.is_file():
        shutil.move(str(meta_src), str(dst_dir / (row.name + ".meta.json")))
    rel = f"{row.system_id}/{row.name_norm}/{row.name}"
    # 同名覆盖必去重（总设计§3.3"新版本赢"后半段；11-04 R2 实测：物理 move 覆盖
    # 但账本双行 → INDEX 双行、flush_hits 全行计数=翻倍）
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
        row.views = (row.views or 0) + (p.views or 0)
        p.deleted_at = datetime.utcnow()
        p.note = ((p.note or "") + f" [superseded-by #{row.id}]").strip()[:300]
    row.status = "approved"
    row.audience = audience or "human"
    row.cache_path = rel
    row.note = (row.note or "")[:280]
    regenerate_index(db, row.system_id)
    return rel


def approve_row(db, row, operator_id: int, audience: str = "both") -> str:
    """pending → 在架（v2 审批端点保留兼容；v3 起 UI 已无审批面，
    新链路走 extract_promotions 自动上架）。文件/指纹/去重/INDEX
    全在 promote_shelved_row 内核，此处只守 pending 语义与用途校验。"""
    if audience not in ("both", "ai", "human"):
        raise ValueError(f"非法用途: {audience}")
    from app.models import ArtifactIndex
    if row.status != "pending":
        raise ValueError(f"仅 pending 可审批（当前 {row.status or '存量'}）")
    rel = promote_shelved_row(db, row, audience=audience)
    row.note = (row.note or "")[:280]
    return rel


def _rm_pair(p) -> list:
    """删文件+同名 meta（存在才删），返回删除名列表。"""
    gone = []
    for q in (p, p.with_name(p.name + ".meta.json")):
        try:
            if q.is_file():
                q.unlink()
                gone.append(q.name)
        except OSError:
            pass
    return gone


def withdraw_row(db, row, reason: str = "") -> list:
    """下架/驳回统一收口（11-06 简化拍板：六态塌缩成 pending→在架→withdrawn）。
    删货：货架件（purge_shelf_files 带路径守卫/空壳回收/INDEX 刷新）+ 待晋升位（幂等清）。
    账本行不物删——status='withdrawn' + deleted_at：对全部查询/注入面天然隐身（既有
    deleted_at 过滤一网打尽），且是 reindex 防复活桩；hits/views/sha/来源字段留档可考。
    不可逆是设计语义：反悔=重新上传/重编（restore/ledger 端点保留为应急后门，UI 不挂）。
    调用方负责 commit。返回删除文件名列表。"""
    from app.config import artifacts_dir
    if row.cache_path:
        removed = purge_shelf_files(db, row)          # 含 regenerate_index + 空壳收
    else:
        removed = []
    if row.source_task_key and row.name:              # promote_pending 位（驳回场景主删点）
        pend = (artifacts_dir() / row.source_task_key / "promote_pending").resolve()
        t2 = pend / row.name
        if str(t2).startswith(str(pend) + os.sep):
            removed += _rm_pair(t2)
    row.status = "withdrawn"
    if reason:
        row.note = (reason or "驳回")[:300]
    row.deleted_at = datetime.utcnow()
    return removed


def reject_row(db, row, reason: str) -> list:
    """驳回（简化语义）：仅 pending 可驳；清待晋升文件+墓碑留痕。"""
    if row.status != "pending":
        raise ValueError("仅 pending 可驳回")
    return withdraw_row(db, row, reason or "驳回")


# ── hits 被动记账（引擎 chunk 循环调，行为数据非汇报数据）────────
# 货架路径两形态（11-06 .209 2500D1 案：AI 用 `pip install ./cache/torch/x.whl` 相对路径——
# 旧式只认 /work/cache/ 字面量 → hits/views 全漏、depend_tree NULL。容器 working_dir=/work，
# ./cache 即货架 bind，契约背书其语义）：
#   绝对 /work/cache/…   相对 ./cache/…（lookbehind 挡 ~/.cache、某URL…/cache/ 误计）
# 两形态 cache 后强制 "/" → /work/cache_out、./cache_out 天然不匹配（晋升出口不记）
_CACHE_USE_RE = re.compile(r"/work/cache/[\w./\-]*|(?<![\w./\-])\./cache/[\w./\-]*")


def reindex(db) -> dict:
    """1) 扫 artifacts/<task>/artifacts/ 树为历史成功任务建账（cache_path 空=仅账本）；
    2) 对账：账本 cache_path 指向的文件不在盘 → 标 missing 注记（警示，不删行不删文件）。
    幂等：按 source_task_id+name upsert。返回统计。"""
    from app.config import artifacts_dir
    from app.models import ArtifactIndex, CompileTask, TaskStatus
    art = artifacts_dir()
    created = updated = missing = skipped = 0
    tasks = db.query(CompileTask).filter(
        CompileTask.status == TaskStatus.SUCCESS.value,
        CompileTask.deleted_at.is_(None)).all()
    for t in tasks:
        outd = art / t.task_id / "artifacts" / "output"
        scan = outd if outd.is_dir() else (art / t.task_id / "artifacts")
        if not scan.is_dir():
            continue
        # 折叠口径与产物清单 _manifest_from_host 同源（11-06 拍板③：128 行树碎片淹没账本，
        # llama.cpp 一棵树=上百行 .h/.so 明细对任何决策无意义；目录整体一条行）
        entries = []
        for p in sorted(scan.iterdir()):
            if p.is_file() and not p.name.endswith(".meta.json") and p.name != "BUILD_NOTES.md" \
                    and _is_deliverable(p.name):
                entries.append(p)
            elif p.is_dir():
                n_files = sum(len(fns) for _dp, _dd, fns in __import__("os").walk(p))
                entries.append((p, n_files))
        for e in entries:
            f, is_dir = (e, False) if isinstance(e, __import__("pathlib").Path) else (e[0], True)
            nm = f.name + ("/" if is_dir else "")
            row = db.query(ArtifactIndex).filter(
                ArtifactIndex.source_task_id == t.id,
                ArtifactIndex.name == nm).first()
            if row is not None and (row.status == "withdrawn" or row.deleted_at is not None):
                skipped += 1                          # 墓碑不复活不更新（11-06 简化：下架/驳回=终局）
                continue
            if row is None:
                _cid = _reindex_identity(t)
                row = ArtifactIndex(source_task_id=t.id, source_task_key=t.task_id,
                                    name=nm, name_norm=name_norm_of(f.name),
                                    version="" if is_dir else version_of(f.name),
                                    kind="dir" if is_dir else kind_of(f.name, str(f)),
                                    origin="task", status="",
                                    size_bytes=0 if is_dir else f.stat().st_size,
                                    system_id=t.system_id,
                                    sha256="" if is_dir else sha256_of(f),
                                    source_commit=_cid,
                                    note=("存量回填(目录折叠)" if is_dir else "存量回填(无meta,上下文未知)"))
                if is_dir:
                    row.note = f"存量回填(目录折叠,含{e[1]}个文件)"
                db.add(row)
                created += 1
            else:
                if not is_dir:
                    row.size_bytes = f.stat().st_size
                    if not row.sha256:
                        row.sha256 = sha256_of(f)
                if not row.source_commit:
                    row.source_commit = _reindex_identity(t)
                updated += 1

    # 碎片归并一次性清账：历史 rglob 逐文件行（name 含子路径 /）软删留痕
    # （cache_path 恒空=从未上架、零引用方——非货架数据，不触"自动逐出"红线；行不物理删可逆查）
    folded = 0
    for r in db.query(ArtifactIndex).filter(
            ArtifactIndex.deleted_at.is_(None), ArtifactIndex.status == "",
            ArtifactIndex.name.like("%/%")).all():
        r.deleted_at = datetime.utcnow()
        r.status = "withdrawn"   # 与简化态一致（reindex skip 认 withdrawn/deleted 双标）
        r.note = ((r.note or "") + " [树碎片归并至目录条目]").strip()[:300]
        folded += 1
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
    return {"created": created, "updated": updated, "missing_marked": missing,
            "folded": folded, "skipped_withdrawn": skipped}


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
def shelf_sources(db) -> list:
    """在架件来源全集（distinct 任务号 + 有上传件时追加 'upload'）——货架"来源"下拉数据，
    口径不受分页/筛选影响（admin/公开 list 共用，11-06）。"""
    from app.models import ArtifactIndex
    srcs = sorted({x[0] for x in db.query(ArtifactIndex.source_task_key).filter(
        ArtifactIndex.deleted_at.is_(None), ArtifactIndex.status.in_(("approved", "")),
        ArtifactIndex.cache_path != "", ArtifactIndex.origin == "task").distinct().all() if x[0]})
    if db.query(ArtifactIndex.id).filter(
            ArtifactIndex.deleted_at.is_(None), ArtifactIndex.origin == "upload",
            ArtifactIndex.cache_path != "", ArtifactIndex.status.in_(("approved", "")),
            ArtifactIndex.system_id == 0).first():
        srcs.append("upload")
    return srcs


def disk_report(db) -> dict:
    """磁盘简报 c 方案（11-05 拍板"不用那么准"）：statvfs + 账本 SUM，零遍历。
    - 分区用量：O(1) 系统调用，告警主功能（85% 线）与本函数耗时无关
    - 货架合计：账本口径（approved∪存量 ∧未软删∧cache_path）——与页面所见一致，
      且"唯一写库通道"纪律下不走通道的文件本就不算货架；不再 rglob 猜盘上实况
    artifacts 交付区汇总数字退役（中间物归 7/90 天钟与单任务逐项 size，无人依赖此数）"""
    import shutil as _sh
    # 11-12 修：统计对象=产物数据根所在分区，不是写死根分区——数据根配了
    # ARTIFACTS_DIR 挪到独立盘后，警戒线须盯"数据实际在的那块盘"才不误报/漏报；
    # 数据根未挪时二者同盘，行为不变
    from app.config import artifacts_dir as _adir
    du = _sh.disk_usage(str(_adir()))
    pct = round(du.used / du.total * 100, 1)
    from sqlalchemy import func
    from app.models import ArtifactIndex
    cnt, total = db.query(func.count(ArtifactIndex.id),
                          func.coalesce(func.sum(ArtifactIndex.size_bytes), 0)).filter(
        ArtifactIndex.deleted_at.is_(None),
        ArtifactIndex.status.in_(("approved", "")),
        ArtifactIndex.cache_path != "").one()
    warn = int(get_cfg("artifact_cache.disk_warn_pct", 85))
    # 键名 root_* → data_*（11-12 随磁盘对象正名：盯的不是"根分区"而是
    # "数据实际所在分区"；路径一并回传，换数据盘的机器页面可自证盯对了地方）
    return {"data_used_pct": pct, "data_free_gb": round(du.free / 2**30, 1), "data_path": str(_adir()),
            "shelf_count": int(cnt), "cache_mb": int(total) // (1024 * 1024),
            "warn": pct >= warn, "warn_pct": warn}


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
        if p.suffix in (".deb", ".rpm"):
            # 交付矩阵（11-17）：系统包=归档套归档（deb=ar→data.tar.*；rpm=cpio.gz），
            # 现有两跳都没走 → 机提恒空、配给闸对 deb/rpm 形同虚设（nmap/lua 案根因之一）
            with _tf.TemporaryDirectory(prefix="hermes-glibc-") as td:
                tdp = Path(td)
                if p.suffix == ".deb":
                    r = _sp.run(["ar", "x", str(p)], cwd=td, capture_output=True, timeout=60)
                    if r.returncode != 0:
                        return ""
                    data = next((f for f in tdp.glob("data.tar.*") if f.is_file()), None)
                    if not data:
                        return ""
                    r = _sp.run(["tar", "-xf", str(data), "-C", str(tdp)],
                                capture_output=True, timeout=120)
                else:
                    with open(p, "rb") as fin, open(tdp / "x.cpio", "wb") as fout:
                        _sp.run(["rpm2cpio"], stdin=fin, stdout=fout, timeout=120, check=False)
                    r = _sp.run(["cpio", "-idm", "--quiet", "-F", str(tdp / "x.cpio")],
                                cwd=td, capture_output=True, timeout=120)
                if r.returncode != 0:
                    return ""
                return _max_glibc_sym(_elf_members(tdp))
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
                return _max_glibc_sym(_elf_members(tdp))
    except Exception:
        return ""
    return ""


def _elf_members(root: Path):
    """归档解出树里收 ELF 成员（上限 300，防解包炸弹；跳过 meta）。"""
    el = []
    for f in sorted(Path(root).rglob("*")):
        if len(el) >= 300:
            break
        if f.is_file() and not f.name.endswith(".meta.json"):
            try:
                with open(f, "rb") as fh:
                    if fh.read(4) == b"\x7fELF":
                        el.append(f)
            except OSError:
                pass
    return el


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


# 交付矩阵（11-17）：件格式 = 强绑定 kind（rpm/deb 只对同格式系统有意义），
# 其余 kind（tgz/whl/binary/so/a/node_binding/tool）生态自管安装=中性件两边可见
_FMT_BOUND = {"rpm": "rpm", "deb": "deb"}


def system_pkg_format(db, system_id: int) -> str:
    """目标系统登记的包格式（rpm/deb；空=未知=不过滤，老行为）。"""
    try:
        from app.models import TargetSystem
        row = db.query(TargetSystem.pkg_format).filter(
            TargetSystem.system_id == int(system_id)).first()
        return (row[0] or "").strip().lower() if row else ""
    except Exception:
        return ""


def _format_passes(row, fmt: str) -> bool:
    """货架格式闸：系统未登记格式=全放行；件非强绑定 kind=中性放行；
    强绑定件（rpm/deb）仅同格式系统可见——rpm 装不进 apt 机器，反之亦然。"""
    if not fmt:
        return True
    bound = _FMT_BOUND.get((row.kind or "").strip().lower())
    return bound is None or bound == fmt


def _link_pool_into_view(db, pool_id: int, view: Path, baseline: str = "", fmt: str = "") -> int:
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
        _ai_visible(),
    ).all()
    if pool_id == COMMON_SYSTEM_ID:
        rows = [r for r in rows if _common_passes(r, baseline)]   # 三层闸·配给过滤
    if fmt:
        rows = [r for r in rows if _format_passes(r, fmt)]        # 交付矩阵·格式闸
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
    fmt = system_pkg_format(db, system_id)        # 11-17 交付矩阵格式闸（空=不过滤）
    view = Path(container_work) / "cache_view"
    if view.is_dir():
        shutil.rmtree(view, ignore_errors=True)
    view.mkdir(parents=True, exist_ok=True)
    _link_pool_into_view(db, COMMON_SYSTEM_ID, view, baseline, fmt)
    _link_pool_into_view(db, int(system_id), view, baseline, fmt)
    rows = db.query(ArtifactIndex).filter(
        ArtifactIndex.system_id.in_((COMMON_SYSTEM_ID, int(system_id))),
        ArtifactIndex.deleted_at.is_(None),
        ArtifactIndex.status.in_(("approved", "")),
        ArtifactIndex.cache_path != "",
        _ai_visible(),   # 货架v2：视图 INDEX 与硬链接同闸（仅供下载件 AI 两见皆无）
    ).order_by(ArtifactIndex.hits.desc(), ArtifactIndex.created_at.desc()).all()
    rows = [r for r in rows if r.system_id != COMMON_SYSTEM_ID or _common_passes(r, baseline)]
    rows = [r for r in rows if _format_passes(r, fmt)]   # INDEX 与硬链接视图同闸（口径一致）
    lines = ["# 复用库货架（平台自动生成，勿手改；缺依赖先查此表）",
             "# 行首带『通用』= 所有系统可用的工具母包（公共池）；其余为本系统已验证产物",
             f"# 共 {len(rows)} 件，按复用次数排列（同次数新件在前）；行多时工具可能截断显示，"
             f"找不到就 grep /work/cache/INDEX.md 或 ls /work/cache/",
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
def _cache_norms(command_text: str) -> set:
    """一条命令引用的货架件目录名集合（hits/views 共用；段解析只此一份）。
    目录级放宽（11-06 .209 案：`ls -la /work/cache/torch` 旧式 len(parts)>=2 漏 views，
    现在 /work/cache/<norm>[/...] 均计）；裸 /work/cache/ 与 INDEX.md 表头不计。"""
    norms = set()
    for seg in _CACHE_USE_RE.findall(command_text or ""):
        pfx = "/work/cache/" if seg.startswith("/work/cache/") else "./cache/"
        rest = seg[len(pfx):].strip("/")
        if not rest:
            continue
        norm = rest.split("/")[0].lower()
        if norm != "index.md":
            norms.add(norm)
    return norms


def scan_cache_use(state: dict, command_text: str) -> None:
    """扫一条 execute 命令 → state['_cache_hits'/'_cache_views']（延后账，flush_hits 落库）。
    分两账（11-04 拍板"查≠用"）：命令含消费动词（_USE_RE）→ 用(hits)；否则 → 览(views)。
    同命令既有 -tzf 清点又有 cp 交付时从严归用。口径统一在 _cache_norms。"""
    norms = _cache_norms(command_text)
    if not norms:
        return
    key = "_cache_hits" if _USE_RE.search(command_text or "") else "_cache_views"
    state.setdefault(key, set()).update(norms)


_USE_RE = re.compile(
    # 用动词判定（11-05 修正）：tar 三种真形态全收——
    #   tar -C /usr/local -xzf f   （DADECE 案：-C 前置旧式漏判，hits 真丢）
    #   tar -xzf f / tar xzf f     （-x 可藏合写 flag 里：-\w*x\w*）
    #   清点是览：-tzf/-tf 不含 x，天然走 views（-tzf 的 t 不会误配）
    # 包装前缀（11-09 typstyle 案）：nohup/setsid + bash|sh -c ' 后紧跟引号再启动消费动词
    # 必须判"用"——只许这组白名单壳，裸引号进分隔符类会让 echo "cp …货架" 变假 hits（E4 防线）
    r"(?:^|[;&|]|\bsudo)\s*(?:(?:nohup|setsid)\s+)*(?:\b(?:ba)?sh\s+-\w*c\w*\s+)?[\"']?\s*"
    r"(?:cp\b|mv\b|install\b|tar\s+(?:\S+\s+)*?-?\w*x\w*"
    r"|unzip\b|dpkg\s+-i|rpm\s+-i|npm\s+i(?:nstall)?\b|pip\s+install|python\d*\s+-m\s+pip\s+install)"
    r"[^\n]*?(?:/work/cache/|(?<![\w./\-])\./cache/)")


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


# ── 期二：晋升提取（货架 v3，11-25 拍板：编译通过即上货架，默认只在货架=human；
#    进不进 AI 复用库由管理员后台开关——"审批"这道门从人侧整体挪到 AI 侧）────
_DOC_FILES = {"RUN.md": "RUN.md", "BUILD_NOTES.md": "BUILD_NOTES.md",
              "PACKAGE.json": "PACKAGE.json", "source-changes.patch": "source-changes.patch"}


def _adopt_docs(db, task, cache_out: Path) -> list:
    """doc 附件随包上架（11-26 A 档）：cache_out/output 顶层的说明文档复制进
    promote_pending 候选区（复制非搬移——output 树归交付 tar 完整性管），
    meta 从同目录真包借 toolchain（文档与包同次编译产出，构建上下文一致）。
    返回 [(file, meta_dict, 原名)]。"""
    # 容器 /work/output 的宿主机映射 = art_root/artifacts/output
    out_root = cache_out.parent.parent / "artifacts" / "output"
    if not out_root.is_dir():
        return []
    tc, ver = {}, ""
    from app.models import ArtifactIndex
    # 从本任务已上架件借构建上下文（主循环已把 cache_out 搬进池，读池内 meta）
    src_rows = db.query(ArtifactIndex).filter(
        ArtifactIndex.source_task_id == task.id, ArtifactIndex.kind != "doc",
        ArtifactIndex.deleted_at.is_(None), ArtifactIndex.cache_path != "").all()
    for r0 in src_rows:
        mp = cache_root() / (r0.cache_path + ".meta.json")
        m = read_meta(mp) if mp.is_file() else None
        if isinstance(m, dict) and m.get("toolchain"):
            tc = m.get("toolchain") or {}
            ver = m.get("version") or r0.version or ""
            if any(str(v).strip() for v in tc.values()):
                break
    pend_dir = cache_out.parent.parent / "promote_pending"
    got = []
    for doc in sorted(out_root.iterdir()):
        want = _DOC_FILES.get(doc.name)
        if not want or not doc.is_file():
            continue
        if not any(str(v).strip() for v in tc.values()):
            continue                     # 无包上下文=文档孤儿，留账本待人工
        pend_dir.mkdir(parents=True, exist_ok=True)
        dstf = pend_dir / want           # 候选区用原名；最终货架名在循环里钉前缀
        shutil.copy2(str(doc), str(dstf))
        meta = {"name": want, "version": ver, "kind": "doc",
                "toolchain": dict(tc), "runtime_libs": []}
        dstf.with_name(dstf.name + ".meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        got.append((dstf, meta, want))
    return got


def extract_promotions(db, task, state: dict, art_root) -> tuple[list, list]:
    """返回 (已上架名, 拒收原因)。合格件直接 move 进货架池 + 账本 approved/human；
    同名"新版本赢"去重与 INDEX 重生成沿用 promote_shelved_row；门禁拒收留痕不变。"""
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
            log_audit(db, user_id=None, action="artifact_rejected", target_type="compile_task",
                      target_id=task.id, target_key=task.task_id,
                      details=f"全拒收:{rejects}")
            try:
                for adm in db.query(User).filter(User.role == "admin", User.is_active == True,
                                                 User.deleted_at.is_(None)).all():
                    notify(db, user_id=adm.user_id, type="artifact_rejected",
                           title=f"晋升被门禁拒收：{task.task_id}（{len(rejects)} 件）",
                           body="；".join(rejects)[:200], link="")
            except Exception:
                pass
        return [], rejects
    names = []
    for it in items:
        # meta 与文件同目录随迁：promote_shelved_row 按 src 同位找 .meta.json
        meta_src = it["file"].with_name(it["file"].name + ".meta.json")
        if meta_src.is_file() and it["name"] != it["file"].name:
            shutil.move(str(meta_src), str(it["file"].with_name(it["name"] + ".meta.json")))
            meta_src = it["file"].with_name(it["name"] + ".meta.json")
        row = ArtifactIndex(
            name=it["name"], name_norm=it["name_norm"], version=it["version"],
            system_id=it["system_id"], kind=it["kind"], origin="task", status="pending",
            audience="human",
            source_task_id=task.id, source_task_key=task.task_id,
            build_env=json.dumps(it.get("toolchain") or {}, ensure_ascii=False),
            runtime_env=json.dumps(it.get("runtime_libs") or [], ensure_ascii=False),
            image=state.get("docker_image") or "", size_bytes=it["size"],
            source_commit=_source_identity(task, state)[:64],
            sha256=it.get("sha256") or "",
            note="v3自动上架" if not it.get("renamed") else "v3自动上架（货架名规范化）")
        db.add(row)
        db.flush()
        # 文件先落到 promote_pending 惯例位（复用 promote_shelved_row 的搬移+去重+INDEX）
        pend_dir = art_root / "promote_pending"
        pend_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(it["file"]), str(pend_dir / it["name"]))
        if meta_src.is_file():
            shutil.move(str(meta_src), str(pend_dir / (it["name"] + ".meta.json")))
        promote_shelved_row(db, row)
        names.append(it["name"])
    # doc 附件轮（11-26 A 档）：货架名钉 <软件名>- 前缀防撞名（多任务 RUN.md 同坑）；
    # 与真包同闸同轨（promote_shelved_row 内含 sha 去重/INDEX 重生成）
    for dstf, meta, orig in _adopt_docs(db, task, cache_out):
        # name_norm 与包行同键（详情同组/INDEX 聚眼一致），无包行时退 slug
        _pk = db.query(ArtifactIndex).filter(
            ArtifactIndex.source_task_id == task.id, ArtifactIndex.kind != "doc",
            ArtifactIndex.deleted_at.is_(None), ArtifactIndex.cache_path != ""
        ).order_by(ArtifactIndex.size_bytes.desc()).first()
        soft = (_pk.name_norm if _pk else "") or \
            re.sub(r"[^\w.\-+]", "-", (task.software_name or "artifact").strip().lower())[:80]
        shelf = f"{soft}-{orig}"
        row = ArtifactIndex(
            name=shelf, name_norm=soft, version=meta.get("version", ""),
            system_id=int(state["system_id"]), kind="doc", origin="task", status="pending",
            audience="human",
            source_task_id=task.id, source_task_key=task.task_id,
            build_env=json.dumps(meta.get("toolchain") or {}, ensure_ascii=False),
            runtime_env="[]", image=state.get("docker_image") or "",
            size_bytes=dstf.stat().st_size,
            source_commit=_source_identity(task, state)[:64],
            sha256=sha256_of(dstf),
            note="v3自动上架（文档附件）")
        db.add(row)
        db.flush()
        final = dstf.with_name(shelf)
        if final != dstf:
            dstf.rename(final)
            mf = dstf.with_name(dstf.name + ".meta.json")
            if mf.is_file():
                mf.rename(final.with_name(final.name + ".meta.json"))
        promote_shelved_row(db, row)
        names.append(shelf)
    log_audit(db, user_id=None, action="artifact_shelved", target_type="compile_task",
              target_id=task.id, target_key=task.task_id,
              details=f"自动上架={names},拒收={rejects}")
    # 通知全体 admin（T15 best-effort）：v3 语义=已上架，动作变"选入 AI 库"
    try:
        for adm in db.query(User).filter(User.role == "admin", User.is_active == True,
                                         User.deleted_at.is_(None)).all():
            notify(db, user_id=adm.user_id, type="artifact_shelved",
                   title=f"已自动上货架：{task.task_id}（{len(names)} 件）",
                   body="、".join(names)[:200] + "——后台可移入 AI 复用库", link="/admin/artifacts")
    except Exception:
        pass
    return names, rejects


def migrate_shelf_v3(db) -> int:
    """货架 v3 存量迁移（幂等，可反复跑）：全部 pending 件自动上架（默认 human）。
    文件灭/指纹不符的个别件降级处理并留痕，不挡其余。返回成功件数。"""
    from app.config import artifacts_dir
    from app.models import ArtifactIndex
    rows = db.query(ArtifactIndex).filter(ArtifactIndex.status == "pending",
                                          ArtifactIndex.deleted_at.is_(None)).all()
    moved = 0
    for row in rows:
        src = artifacts_dir() / row.source_task_key / "promote_pending" / row.name
        if not src.is_file():
            row.status = "approved"; row.audience = "human"
            row.note = ((row.note or "") + " [v3迁移:文件不在盘,仅账本]").strip()[:300]
            db.flush()
            continue
        if row.sha256 and sha256_of(src) != row.sha256:
            # 转"仅账本"行（不悬 pending——v3 后 UI 无待晋升面）：文件留原位随任务清理，
            # admin 在货架表见"仅账本"徽标，处置=删除
            row.status = "approved"; row.audience = "human"
            row.note = ((row.note or "") + " [v3迁移:指纹不符未上架,文件留任务目录,请人工处置]").strip()[:300]
            continue
        promote_shelved_row(db, row, audience="human")
        moved += 1
    db.commit()
    if rows:
        for sid in {r.system_id for r in rows}:
            regenerate_index(db, sid)
        db.commit()
    return moved
