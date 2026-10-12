"""Skill 经验库自动发现 + 磁盘同步

扫描 compile-platform/skills/ 目录，返回 DeepAgents SkillsMiddleware
所需的容器内 source 路径列表。

约定（与 DeepAgents 期望一致）：
- 每个 SKILL.md 是子目录里的一个 skill
- source 路径 = SKILL.md 的祖父目录（即 source/<skill>/SKILL.md）
- 加新 skill：直接新建子目录 + 写 SKILL.md，**不用改代码**重启即生效

容器内挂载方式（由 compile_engine 调用）：
  宿主机 {SKILLS_DIR_HOST} → 容器 {SKILLS_DIR_CONTAINER} (ro)

写盘约束（write_skill_to_disk / remove_skill_from_disk）：
- skill_name 必须匹配 ^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$（DeepAgents spec）
- 真实路径必须落在 SKILLS_DIR_HOST 下，防 symlink/path traversal 逃逸
- 原子写：先写 .tmp 再 os.replace
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 宿主机路径（指向 compile-platform/skills/）
# compile-platform 仓库根目录结构：backend/app/services/skill_loader.py
# skills 在仓库根目录，所以往上跳两级
SKILLS_DIR_HOST = Path(__file__).resolve().parents[3] / "skills"

# 容器内挂载点（在 stage3 / stage5 创建容器时挂载）
SKILLS_DIR_CONTAINER = "/skills"

# DeepAgents spec: lowercase alphanumeric + hyphens, 1-64 chars
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")
_VALID_TYPES = {"generic", "specific"}


def _validate_skill_name(name: str) -> str:
    """校验 skill_name，返回原值；非法直接 raise ValueError"""
    if not isinstance(name, str) or not _SKILL_NAME_RE.match(name):
        raise ValueError(
            f"skill_name 不合法: {name!r}（须匹配 ^[a-z0-9][a-z0-9-]{{0,62}}[a-z0-9]$）"
        )
    return name


def _validate_type(skill_type: str) -> str:
    if skill_type not in _VALID_TYPES:
        raise ValueError(f"skill_type 不合法: {skill_type!r}（须为 generic/specific）")
    return skill_type


def _resolve_skill_dir(skill_type: str, skill_name: str) -> Path:
    """组装安全路径，并校验真实位置仍落在 SKILLS_DIR_HOST 下

    防 symlink / .. 逃逸：resolve() 后用 is_relative_to 判定
    """
    _validate_type(skill_type)
    _validate_skill_name(skill_name)

    # Path.joinpath 已用 _validate_skill_name 锁住非法字符；再 .resolve 解析 .. 和 symlink
    skill_dir = (SKILLS_DIR_HOST / skill_type / skill_name).resolve()
    skills_root = SKILLS_DIR_HOST.resolve()
    try:
        skill_dir.relative_to(skills_root)
    except ValueError as e:
        raise ValueError(f"skill 路径逃逸 SKILLS_DIR_HOST: {skill_dir}") from e
    return skill_dir


def discover_skill_sources(host_root: Path = None, container_root: str = None) -> list[str]:
    """扫描 skills/ 目录，返回 DeepAgents sources 格式的容器内路径列表

    排序规则：字母序（保证 later-wins 语义稳定）
    """
    host_root = host_root or SKILLS_DIR_HOST
    container_root = container_root or SKILLS_DIR_CONTAINER

    if not host_root.exists():
        return []

    sources = {
        skill_md.parent.parent.relative_to(host_root)
        for skill_md in host_root.rglob("SKILL.md")
    }
    return [f"{container_root}/{s.as_posix()}" for s in sorted(sources)]


def get_skills_paths() -> list[str]:
    """每次调用都重新扫描（替代旧的 module-level cache）

    approve/update/delete skill 后调用一次即可热生效
    性能：rglob 几个文件 < 5ms，可忽略
    """
    return discover_skill_sources()


def write_skill_to_disk(skill_type: str, skill_name: str, content: str,
                        version: str | None = None) -> Path:
    """把 SKILL.md 写到磁盘，返回写入的文件路径

    - 自动 mkdir -p
    - 原子写：先写 SKILL.md.tmp 再 os.replace
    - 文件头加一行注释 `# version: <version>`（如有）便于追溯
    - 失败时抛 RuntimeError（含原始错误）
    """
    skill_dir = _resolve_skill_dir(skill_type, skill_name)
    skill_dir.mkdir(parents=True, exist_ok=True)

    target = skill_dir / "SKILL.md"
    tmp = skill_dir / "SKILL.md.tmp"

    body = content or ""
    if version:
        # YAML frontmatter 风格注释；DeepAgents SkillsMiddleware 会忽略 # 开头的非 YAML 头
        header = f"<!-- synced version: {version} -->\n"
        body = header + body

    try:
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, target)
    except Exception as e:
        # 清理 tmp 残留
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise RuntimeError(f"写 SKILL.md 失败 ({skill_dir}): {type(e).__name__}: {e}") from e
    return target


def remove_skill_from_disk(skill_type: str, skill_name: str) -> bool:
    """删除磁盘上的 skill 目录，返回是否真有删除动作

    不存在视为 False（幂等），但不报错
    """
    skill_dir = _resolve_skill_dir(skill_type, skill_name)
    if not skill_dir.exists():
        return False
    try:
        shutil.rmtree(skill_dir)
    except Exception as e:
        raise RuntimeError(f"删 skill 目录失败 ({skill_dir}): {type(e).__name__}: {e}") from e
    return True


# 向后兼容：保留旧名 SKILLS_PATHS 但改为「首次调用时计算」
# 注意：旧代码 `from skill_loader import SKILLS_PATHS` 仍能拿到一个 list（启动时的快照），
# 但 stage5 启动会重新调 get_skills_paths() 拿最新。详见 compile_engine 改造。
SKILLS_PATHS: list[str] = get_skills_paths()


# ==================== Reconcile（磁盘 → DB 同步）====================
# 设计：磁盘是 source of truth，DB 是 derived cache。
# 触发方式：前端进入 SkillLib 页面时调一次 /admin/skills/reconcile。
# 2 状态机：
#   - disk_only : 磁盘有，DB 没有 → 插入 DB（source='admin'）
#   - db_only   : DB 有，磁盘没  → 删 DB 行（磁盘删了，留 DB 没意义）
# 不做 hash 比对 → admin 编辑保存会先写盘后写 DB，永远不会 drift。
# 如果有人手改磁盘文件，下次进页面会被覆盖（接受此取舍）。

def reconcile_all(db: "Session", *, dry_run: bool = False) -> dict:
    """2 状态 reconcile。返回变更计数。"""
    from app.models import Skill

    # 1. 扫磁盘：拿到所有 (type, name) 集合
    disk_keys: set[tuple[str, str]] = set()
    if SKILLS_DIR_HOST.exists():
        for skill_md in SKILLS_DIR_HOST.rglob("SKILL.md"):
            rel = skill_md.relative_to(SKILLS_DIR_HOST)
            parts = rel.parts
            if len(parts) != 3 or parts[0] not in ("generic", "specific"):
                continue
            disk_keys.add((parts[0], parts[1]))

    # 2. 扫 DB
    db_skills: dict[tuple[str, str], Skill] = {}
    for s in db.query(Skill).all():
        db_skills[(str(s.type), str(s.name))] = s

    disk_only_keys = disk_keys - set(db_skills.keys())
    db_only_keys = set(db_skills.keys()) - disk_keys

    stats = {
        "disk_only": 0,
        "db_only": 0,
        "total_disks": len(disk_keys),
        "total_dbs": len(db_skills),
    }

    # 3. disk_only：插入 DB
    for key in disk_only_keys:
        skill_type, skill_name = key
        try:
            content = (SKILLS_DIR_HOST / skill_type / skill_name / "SKILL.md").read_text(encoding="utf-8")
            if dry_run:
                stats["disk_only"] += 1
                continue
            description = _parse_frontmatter_description(content)
            db.add(Skill(
                name=skill_name,
                type=skill_type,
                description=description,
                system_ids=json.dumps([]),
                version=None,
                skill_content=content,
                source="admin",  # 磁盘导入 = admin 行为
            ))
            db.commit()
            logger.info("[reconcile] disk_only → insert %s/%s", skill_type, skill_name)
            stats["disk_only"] += 1
        except Exception as e:
            db.rollback()
            logger.warning("[reconcile] disk_only 插入失败 %s/%s: %s", skill_type, skill_name, e)

    # 4. db_only：软删（设 deleted_at，DB 行保留作备份）
    # 恢复方式：调用 POST /admin/skills/{id}/restore
    # 永久删除：调用 DELETE /admin/skills/{id}/permanent
    for key in db_only_keys:
        skill_type, skill_name = key
        try:
            skill = db_skills[key]
            if skill.deleted_at is None:
                if not dry_run:
                    skill.deleted_at = datetime.utcnow()
                    db.commit()
                logger.info("[reconcile] db_only → soft delete %s/%s", skill_type, skill_name)
            stats["db_only"] += 1
        except Exception as e:
            db.rollback()
            logger.warning("[reconcile] db_only 标记失败 %s/%s: %s", skill_type, skill_name, e)

    return stats


def _parse_frontmatter_description(content: str) -> str | None:
    """简单 YAML frontmatter 解析（只解 description）"""
    import re
    if not content or not content.startswith("---\n"):
        return None
    end = content.find("\n---", 4)
    if end < 0:
        return None
    fm = content[4:end]
    m = re.search(r"^description:\s*(.+?)\s*$", fm, re.MULTILINE)
    if not m:
        return None
    val = m.group(1).strip()
    if " #" in val:
        val = val.split(" #", 1)[0].strip()
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        val = val[1:-1]
    return val or None
