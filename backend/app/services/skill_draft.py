"""
从工单生成 SKILL.md 草稿（走 SkillLib pending 审核流程，不调 LLM）

数据源：
  - ticket.title / description / resolution
  - task.git_url / software_name / system
  - 工单关联的所有 agent 修改记录（容器内 /work/.patches/*.json）
  - 工单对话历史（最近 N 条）
"""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Ticket, CompileTask, Skill, AuditLog, TargetSystem, User, TicketMessage


def _slugify(name: str) -> str:
    """软件名 → skill slug（去掉非法字符）"""
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", name.lower()).strip("-")
    return s or "skill"


def generate_skill_from_ticket(
    db: Session,
    ticket_id: int,
    current_user: User,
    skill_name: Optional[str] = None,
    skill_type: str = "layer2-lessons",
    auto_approve: bool = False,
) -> int:
    """从工单生成 Skill 草稿进 SkillLib pending。返回 skill_id"""
    ticket = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
    if not ticket:
        raise ValueError(f"工单 #{ticket_id} 不存在")
    task = ticket.task

    # 1) 拼 skill_name（默认 slug）—— 必须在拼内容前算好，frontmatter 要用真实 name
    sys_name = ""
    if task.system_id:
        ts = db.query(TargetSystem).filter(TargetSystem.system_id == task.system_id).first()
        if ts:
            sys_name = _slugify(ts.name)
    if not skill_name:
        skill_name = f"{_slugify(task.software_name or 'sw')}-{sys_name or 'loongarch'}"

    # 2) 拼内容（frontmatter 用真实 skill_name）
    content = _build_skill_content(db, ticket, task, skill_name=skill_name)
    description = f"工单 #{ticket_id} 解决方案：{ticket.resolution or '(未填)'}"[:200]

    # 3) 写 skills 表（status=pending）
    # 校验 name 唯一
    existing = db.query(Skill).filter(Skill.name == skill_name).first()
    if existing:
        # 加后缀
        i = 2
        while db.query(Skill).filter(Skill.name == f"{skill_name}-{i}").first():
            i += 1
        skill_name = f"{skill_name}-{i}"

    skill = Skill(
        name=skill_name,
        type=skill_type,
        description=description,
        skill_content=content,  # 字段是 skill_content
        source="ticket",
        is_active=False,  # 草稿，admin approve 后再激活
    )
    db.add(skill)
    db.flush()  # 拿 skill_id

    # 4) 写 audit
    db.add(AuditLog(
        user_id=current_user.user_id,
        action="skill_from_ticket",
        target_type="skill",
        target_id=skill.skill_id,
        details=f"from_ticket={ticket_id}, auto_approve={auto_approve}",
    ))
    return skill.skill_id


def _build_skill_content(db: Session, ticket: Ticket, task: CompileTask,
                         skill_name: str = "") -> str:
    """拼 SKILL.md 完整 frontmatter + body"""
    # 1) 失败日志末 30 行
    log_tail = ""
    try:
        from app.services.compile_engine import _artifacts_dir
        log_path = _artifacts_dir() / task.task_id / "build.log"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            log_tail = "\n".join(lines[-30:])
    except Exception:
        pass

    # 2) 工单描述（用户填的 + 自动附的日志）
    ticket_desc = ticket.description or "(无)"

    # 3) 管理员填的 resolution
    resolution = ticket.resolution or "(未填)"

    # 4) 容器内 .patches/ 记录（agent 修改了哪些文件）
    patches_summary = ""
    try:
        from app.services.ticket_agent import MANAGER
        sess = MANAGER.get(ticket.ticket_id)
        if sess and sess.container_id:
            # 从容器读 .patches/ 列表
            r = sess.docker_backend.execute("ls /work/.patches/ 2>/dev/null | sort")
            files = [f for f in r.output.split() if f.endswith(".json")]
            patch_lines = []
            for f in files[:20]:  # 最多 20 条
                cat = sess.docker_backend.execute(f"cat /work/.patches/{f}")
                if cat.exit_code == 0 and cat.output.strip():
                    try:
                        rec = json.loads(cat.output.strip())
                        path = rec.get("file_path", "?")
                        old = (rec.get("old_string") or "")[:100]
                        new = (rec.get("new_string") or "")[:100]
                        cont = (rec.get("content") or "")[:200]
                        if old or new:
                            patch_lines.append(f"- `{path}`: `-{old}` → `+{new}`")
                        elif cont:
                            patch_lines.append(f"- `{path}`: 写入 {len(cont)} 字符")
                    except Exception:
                        pass
            patches_summary = "\n".join(patch_lines) if patch_lines else "(容器内未检测到修改)"
    except Exception as e:
        patches_summary = f"(读取修改记录失败: {e})"

    # 5) 工单对话历史（最近 5 条）
    msgs = db.query(TicketMessage).filter(
        TicketMessage.ticket_id == ticket.ticket_id,
    ).order_by(TicketMessage.created_at.desc()).limit(5).all()
    msgs.reverse()
    chat_lines = []
    for m in msgs:
        chat_lines.append(f"- **{m.role}**: {m.content[:200]}")
    chat_summary = "\n".join(chat_lines) if chat_lines else "(无对话)"

    # 6) source info
    if task.source_type == "git":
        source_info = f"GIT  `{task.git_url or '?'}`  branch=`{task.git_branch or '(default)'}`"
    else:
        size_mb = "?"
        try:
            if task.source_path and Path(task.source_path).exists():
                size_mb = f"{Path(task.source_path).stat().st_size // 1024 // 1024} MB"
        except Exception:
            pass
        source_info = f"UPLOAD  `{task.source_path or '?'}` ({size_mb})"

    body = f"""## 失败现象

工单 #{ticket.ticket_id}：{ticket.title}

**用户描述**（节选）：
{ticket_desc[:500]}

**失败日志末尾 30 行**：
```
{log_tail or '(无日志)'}
```

## 根因

{ticket.resolution or '(管理员未填解决说明)'}

## 修复步骤

1. **{task.software_name}** 在龙芯平台上的处理步骤
2. 关键命令/修改（来自 agent 在容器内的修改记录）：

{patches_summary}

3. 完整对话摘要（最近 5 条）：

{chat_summary}

## 源码信息

- 软件：{task.software_name}
- 类型：{task.source_type}
- {source_info}

## 触发条件（用于匹配同类任务）

- software_name: `{task.software_name}`
- system_id: `{task.system_id}`
"""
    frontmatter = f"""---
name: {skill_name or _slugify(ticket.title)}
type: { "layer2-lessons" }
description: { json.dumps((ticket.resolution or ticket.title)[:200], ensure_ascii=False) }
allowed-tools: [execute, read_file, ls, grep, edit]
---

"""
    return frontmatter + body
