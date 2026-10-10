"""Skill 经验库自动发现

扫描 compile-platform/skills/ 目录，返回 DeepAgents SkillsMiddleware
所需的容器内 source 路径列表。

约定（与 DeepAgents 期望一致）：
- 每个 SKILL.md 是子目录里的一个 skill
- source 路径 = SKILL.md 的祖父目录（即 source/<skill>/SKILL.md）
- 加新 skill：直接新建子目录 + 写 SKILL.md，**不用改代码**重启即生效

容器内挂载方式（由 compile_engine 调用）：
  宿主机 {SKILLS_DIR_HOST} → 容器 {SKILLS_DIR_CONTAINER} (ro)
"""
from pathlib import Path

# 宿主机路径（指向 compile-platform/skills/）
# compile-platform 仓库根目录结构：backend/app/services/skill_loader.py
# skills 在仓库根目录，所以往上跳两级
SKILLS_DIR_HOST = Path(__file__).resolve().parents[3] / "skills"

# 容器内挂载点（在 stage3 / stage5 创建容器时挂载）
SKILLS_DIR_CONTAINER = "/skills"


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


# 启动时一次扫描，缓存结果
SKILLS_PATHS: list[str] = discover_skill_sources()
