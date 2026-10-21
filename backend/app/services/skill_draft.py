"""
从工单生成 SKILL.md 草稿 → 统一走 experience 审批流

生成方式与「用户编译经验」同一套提示词体系（experience_generator._SYSTEM_PROMPT：
frontmatter slug 校验、description 三前提、layer1/2/3 正文结构、禁止编造命令），
并附工单增强段——基于 admin 已验证的 resolution，正文必须给出：
失败原因 → 可复现的完整编译步骤 → 验证方法。

数据源：
  - task build.log（文件优先，DB 兜底）
  - ticket.title / description / resolution（已验证解法）
  - agent 修改记录（容器 volume 里 .patches/*.json）

LLM 失败/未配置 → 回退模板拼装（不阻塞）。
"""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Ticket, CompileTask, Skill, Experience, AuditLog, TargetSystem, User, TicketMessage


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
    skill_content_override: Optional[str] = None,
) -> int:
    """从工单生成经验草稿 → 写 Experience 表（status=submitted），走审批页流程

    approve 后由 experience 流程统一落 Skill 表 + 磁盘 SKILL.md（与用户提交经验同路）。
    skill_content_override：admin 在预览弹窗编辑过的内容（优先于模板）。
    返回 experience_id
    """
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
        skill_name = f"{_slugify(task.software_name or 'sw')}-{_slugify(sys_name or 'loongarch')}"

    # 2) 内容：优先 admin 编辑过的草稿 → LLM（同用户经验提示词体系）→ 模板兜底
    if skill_content_override and skill_content_override.strip():
        content = skill_content_override
    else:
        try:
            draft = _llm_generate_ticket_draft(db, ticket, task, sys_name or f"system#{task.system_id}")
            content = draft or _build_skill_content(db, ticket, task, skill_name=skill_name)
        except Exception:
            content = _build_skill_content(db, ticket, task, skill_name=skill_name)

    # 3) 写 experiences 表（submitted，等审批）
    # Experience.skill_type 枚举只认 generic|specific（与 submit 接口一致），模板的
    # layer2-lessons 是 frontmatter 概念，DB 列归一成 specific（工单经验都是特定软件）
    db_skill_type = "specific"
    # 同名冲突：不避让后缀——审批 approve 时同名走"覆盖+superseded"逻辑（与用户经验一致）
    exp = Experience(
        task_id=task.id,
        user_id=current_user.user_id,
        skill_name=skill_name,
        skill_type=db_skill_type,
        system_id=task.system_id,
        status="submitted",
        skill_content=content,
        ticket_id=ticket_id,  # 来源工单标识
    )
    db.add(exp)
    db.flush()  # 拿 experience_id

    # 4) 写 audit
    db.add(AuditLog(
        user_id=current_user.user_id,
        action="experience_from_ticket",
        target_type="experience",
        target_id=exp.experience_id,
        details=f"from_ticket={ticket_id}, skill_name={skill_name}, edited={bool(skill_content_override)}",
    ))
    return exp.experience_id


def build_skill_draft_preview(db: Session, ticket: Ticket) -> dict:
    """生成工单经验草稿但**不写 DB**——供 admin 预览编辑，确认后走 POST /generate-skill 提交

    与用户编译经验同一套 LLM 提示词体系（_SYSTEM_PROMPT + 校验 + 剥壳 + 重试 1 次），
    附工单增强上下文（resolution / patches）。LLM 不可用 → 回退模板。
    """
    task = ticket.task
    if not task:
        raise ValueError(f"工单 #{ticket.ticket_id} 没有关联编译任务")
    sys_name = ""
    ts = None
    if task.system_id:
        ts = db.query(TargetSystem).filter(TargetSystem.system_id == task.system_id).first()
        if ts:
            sys_name = ts.name
    fallback_name = f"{_slugify(task.software_name or 'sw')}-{_slugify(sys_name or 'loongarch')}"

    # 1) LLM 生成（同一套提示词体系）
    llm_err = ""
    try:
        draft = _llm_generate_ticket_draft(db, ticket, task, sys_name or f"system#{task.system_id}")
        if draft:
            from app.services.experience_generator import _parse_frontmatter, _infer_skill_type
            fm = _parse_frontmatter(draft) or {}
            return {
                "ticket_id": ticket.ticket_id,
                "skill_name": fm.get("name") or fallback_name,
                "skill_type": _infer_skill_type(fm.get("type", ""), task.software_name or ""),
                "system_id": task.system_id,
                "skill_content": draft,
                "generated_by": "llm",
            }
        llm_err = "LLM 输出未通过校验"
    except Exception as e:
        llm_err = f"{type(e).__name__}: {e}"

    # 2) 模板兜底（不阻塞，预览里标明来源让 admin 心里有数）
    content = _build_skill_content(db, ticket, task, skill_name=fallback_name)
    return {
        "ticket_id": ticket.ticket_id,
        "skill_name": fallback_name,
        "skill_type": "specific",
        "system_id": task.system_id,
        "skill_content": content,
        "generated_by": "template",
        "llm_error": llm_err[:200],
    }


def _collect_ticket_evidence(db: Session, ticket: Ticket, task: CompileTask) -> dict:
    """组装 LLM 输入：

    主体素材 = agent 解决对话里的**成功轨迹**（从 ticket_agents.checkpoint_state 提取：
    admin 指令 + agent 实际执行的命令 + 关键输出 + 最终成功总结——最后一轮通常是成功的）。
    辅助素材 = resolution、patches 修改记录、原失败日志（只取报错特征行做"症状"）。
    """
    # 1) agent 解决轨迹（DB checkpoint 序列化，跨进程还在）
    trajectory = _extract_resolution_trajectory(db, ticket.ticket_id)

    # 2) patches 修改记录（容器 volume）
    patches = []
    try:
        from app.services.compile_engine import _artifacts_dir
        pdir = _artifacts_dir() / f"{task.task_id}-TKT{ticket.ticket_id}" / "container_work" / ".patches"
        if pdir.exists():
            for pf in sorted(pdir.glob("*.json"))[:10]:
                try:
                    patches.append(json.loads(pf.read_text(encoding="utf-8", errors="replace")))
                except Exception:
                    continue
    except Exception:
        pass

    # 3) 原失败日志：只取错误特征行（攻略需要"症状"，不需要全量流程日志）
    log_output = ""
    try:
        from app.services.compile_engine import _artifacts_dir
        log_path = _artifacts_dir() / task.task_id / "build.log"
        if log_path.exists():
            log_output = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not log_output:
        log_output = task.log_output or ""
    err_lines = [ln for ln in log_output.splitlines()
                 if re.search(r"error|Error|ERROR|failed|Failed|FAILED|fatal|Traceback|No such|not found|undefined", ln)][:30]

    # 4) agent 编译现场记录（stage6 落定，与 build.log 同级）——命令实录/踩坑/差异对照，最高密度素材
    build_notes = ""
    try:
        np = _artifacts_dir() / task.task_id / "BUILD_NOTES.md"
        if np.is_file():
            build_notes = np.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass

    return {"trajectory": trajectory, "patches": patches, "error_lines": "\n".join(err_lines),
            "build_notes": build_notes}


def _extract_resolution_trajectory(db: Session, ticket_id: int) -> str:
    """从 ticket_agents.checkpoint_state（langgraph state 序列化）还原解决轨迹文本

    格式：[管理员指令] / $ 命令 → 输出节选 / [agent 结论]
    """
    try:
        from app.models import TicketAgent
        import base64
        rec = db.query(TicketAgent).filter(TicketAgent.ticket_id == ticket_id).first()
        if not rec or not rec.checkpoint_state:
            return ""
        msgs = json.loads(base64.b64decode(rec.checkpoint_state)).get("channel_values", {}).get("messages", [])
    except Exception:
        return ""

    lines = []
    for m in msgs:
        d = m.get("data", {})
        mtype = m.get("type")
        content = str(d.get("content") or "").strip()
        if mtype == "human":
            if content:
                lines.append(f"[管理员指令] {content[:200]}")
        elif mtype == "ai":
            for tc in (d.get("additional_kwargs", {}).get("tool_calls") or []):
                args = tc.get("args") or {}
                cmd = args.get("command") or args.get("file_path") or json.dumps(args, ensure_ascii=False)[:150]
                lines.append(f"$ {tc.get('name','?')}: {str(cmd)[:300]}")
            if content:
                lines.append(f"[agent 结论] {content[:800]}")
        elif mtype == "tool":
            # 只留输出前几行（判断成败用），太长会淹没轨迹
            head = "\n".join(content.splitlines()[:6])
            lines.append(f"  ↳ {head[:400]}")
    text = "\n".join(lines)
    # 上限：整段轨迹 8000 字符（保尾——最后一次成功最关键）
    return text[-8000:]


_TICKET_USER_PROMPT = """你是龙芯 LoongArch 编译平台的攻略整理助手。

背景：软件 {software} 曾编译失败，管理员在容器里和 agent 多轮操作后**已经把它编译成功了**（下方"解决轨迹"就是成功过程实录，最后一轮一定是成功）。
你的任务：把这次成功过程整理成一份**编译攻略 SKILL.md**——给以后编译同类软件的 agent 照着做，一次成功。

注意：SKILL.md 是攻略，不是事故报告。**通篇不要出现"工单/日志不足/管理员"等处理过程词汇**，直接写给编译者：这是什么软件、在什么系统、怎么一步步编出来。

# 事实素材（只基于这些，禁止编造命令/版本/URL）

## 软件与系统
- 软件：{software}
- 目标系统：{system}
- 源码：{git_info}

## 失败症状（原日志的报错特征行，写"踩坑点"时引用原文用）
```
{error_lines}
```

## 编译现场记录 BUILD_NOTES（agent 实操实录：实际命令/踩坑修复/与攻略差异——优先级最高的素材，与轨迹互证）
```
{build_notes}
```

## 成功解决轨迹（核心素材！按时间序，含实际执行过的每条命令和输出）
```
{trajectory}
```

## 容器内文件修改记录
{patches}

## 管理员确认的结论
{resolution}

# 输出要求
严格按 system 规则输出 SKILL.md（frontmatter + 正文），正文按 layer2-build 结构：

## 简要场景
   一两句：{software} 在 {system}（loongarch64）可直接按本攻略编译成功。

## 编译步骤（核心！）
   从"成功解决轨迹"里提炼**从空容器到产物**的完整可复制序列，按编号：
   1. 环境/依赖确认（轨迹里验证过环境的命令）
   2. 源码获取/放置（含轨迹里的真实目录结构，容器内路径以 /work 为准）
   3. 需要的源码修改（来自"容器内文件修改记录"，没修改就写"无需修改"）
   4. 构建命令（用轨迹里**最终成功那次**的命令，不要早期失败的尝试）
   5. 产物安装到 /work/output
   每步给出可直接复制的命令 + 一行说明为什么。

## 踩坑点
   对照"失败症状"：原来卡在哪、现在怎么绕开（引用报错原文 1 行 + 规避命令）。
   轨迹里试错失败后又改正的点也写（如"不能用 X 命令，要用 Y"）。

## 验证方法
   产物路径 + 检查命令（用轨迹里真实的验证手段，如 `file hello` 确认 LoongArch ELF）。

description 字段（检索命中关键，按 system 规则）：{software} 在龙芯 {system} 的完整编译步骤（+ 一两个真实踩坑关键词）。不写"工单""失败处理"字样。

只输出 SKILL.md 内容本身，不要解释、不要开场白。
"""


def _llm_generate_ticket_draft(db: Session, ticket: Ticket, task: CompileTask, system_name: str) -> Optional[str]:
    """工单经验 LLM 草稿：与 experience_generator 同一套校验/剥壳/重试，prompt 加工单增强"""
    from app.services.experience_generator import (
        _SYSTEM_PROMPT, _call_llm, _strip_wrapping, _validate_draft,
    )

    ev = _collect_ticket_evidence(db, ticket, task)
    trajectory = ev["trajectory"]
    if not trajectory:
        # 没有 agent 解决轨迹（工单没走 agent 对话就手工解决了）→ LLM 素材不足，直接走模板兜底
        print("[skill_draft] 工单无 agent 解决轨迹，回退模板")
        return None

    git_info = f"{task.git_url or '(无)'} branch={task.git_branch or '?'}" if task.source_type == "git" \
        else f"用户上传源码包（容器内解压到 /work/src）"
    patches_txt = json.dumps(ev["patches"], ensure_ascii=False)[:3000] if ev["patches"] else "(无文件修改，源码原样编译成功)"

    user_prompt = _TICKET_USER_PROMPT.format(
        software=task.software_name,
        system=system_name,
        git_info=git_info,
        error_lines=ev["error_lines"] or "(无报错特征行)",
        build_notes=(ev.get("build_notes") or "(无现场记录)")[:8000],
        trajectory=trajectory,
        patches=patches_txt,
        resolution=ticket.resolution or "(无)",
    )

    last_reason = ""
    for attempt in (1, 2):
        try:
            # 8192：思考型模型 reasoning 吃输出预算，2048 写长攻略会 content 为空（实测）
            raw = _call_llm(_SYSTEM_PROMPT, user_prompt, temperature=0.3, max_tokens=8192)
            cleaned = _strip_wrapping(raw)
            ok, reason = _validate_draft(cleaned)
            if ok:
                return cleaned
            last_reason = reason
        except Exception as e:
            print(f"[skill_draft] LLM 生成失败 attempt{attempt}: {type(e).__name__}: {e}")
            last_reason = str(e)
            break  # 网络/认证错误不重试
    print(f"[skill_draft] 工单经验 LLM 草稿未通过: {last_reason}")
    return None


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


# ============== 预览异步任务表（进程内存，重启即失，可接受）==============
_PREVIEW_JOBS: dict = {}  # ticket_id -> {status, result, error, started_at}


def start_preview_job(db_factory, ticket_id: int) -> dict:
    """后台起 LLM 草稿生成（同步阻塞调用放 thread 里跑）。重复点返回已有任务"""
    import threading, time as _t
    job = _PREVIEW_JOBS.get(ticket_id)
    if job and job["status"] == "running":
        return job
    job = {"status": "running", "result": None, "error": None, "started_at": _t.time()}
    _PREVIEW_JOBS[ticket_id] = job

    def _run():
        from sqlalchemy.orm import Session
        s: Session = db_factory()
        try:
            ticket = s.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
            if not ticket:
                job["status"], job["error"] = "error", "工单不存在"
                return
            job["result"] = build_skill_draft_preview(s, ticket)
            job["status"] = "done"
        except Exception as e:
            job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"
        finally:
            s.close()

    threading.Thread(target=_run, daemon=True).start()
    return job


def get_preview_job(ticket_id: int) -> Optional[dict]:
    return _PREVIEW_JOBS.get(ticket_id)
