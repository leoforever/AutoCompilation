"""AI 后置总结 — 根据已完成 task 生成经验草稿（用户点按钮触发，不自动）

输入：task_id（int）
输出：dict { skill_name, skill_type, draft_content }

调用方式（复用 stage5 的 LLM 配置，不引入新 provider）：
  from app.services.compile_engine import _ai_api_key, _ai_base_url, _ai_model

校验：
  - 必须含 YAML frontmatter（`---\\n...\\n---\\n`）
  - 必须有 `name:` 和 `description:` 字段
  - name 必须匹配 slug 规则（和 skill_loader 一致）
  不合法 → 重试 1 次；仍不合法 → 抛 RuntimeError
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING
from langchain_core.messages import HumanMessage, SystemMessage

# LLM 配置：复用 compile_engine 的环境变量约定（AI_MODEL / AI_API_KEY / AI_BASE_URL）
# 不 import compile_engine 是因为它会触发 docker_backend → deepagents 连锁 import
# （deepagents 当前和 anthropic 版本不兼容，会让本 service 不可 import）
def _ai_model() -> str:
    import os
    return os.environ.get("AI_MODEL", "agnes-2.5-flash")

def _ai_api_key() -> str:
    import os
    return os.environ.get("AI_API_KEY", "")

def _ai_base_url() -> str:
    import os
    return os.environ.get("AI_BASE_URL", "")

from app.services.skill_loader import _SKILL_NAME_RE

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 日志截断长度（避免 prompt 爆炸）
_LOG_TRUNCATE_CHARS = 12000

# 单次 LLM 输出上限
# 思考型模型（qwen3.8-flash 等）reasoning 计入 max_completion_tokens 预算：
# 2048 实测 finish=length、content=0（写长文档必空）→ 默认给足（2026-10-21 复现实验：
# 同 prompt 2048→content_len=0 / 8192→14102）。skill_draft 早已显式 8192，这里补齐默认值。
# 2026-10-21 用户拍板 8192*8=65536：长攻略+大思考量场景留足头寸（API 实测接受 65536）。
_MAX_TOKENS = 65536


# ==================== Prompt ====================

_SYSTEM_PROMPT = """你是龙芯 LoongArch 编译平台的经验整理助手。

任务：把一次完整编译任务的日志 + 错误信息整理成一段 SKILL.md，供平台后续用户参考。

# 关键事实约束（必须遵守）
1. 只基于下方提供的日志和错误信息，禁止编造命令、链接、版本号、URL、文件名
2. 不能编造"修复方法"——日志里没出现的步骤不要写
3. 错误信息原文如果关键要保留（如 "undefined reference to `X`"），不要改写
4. 如果日志太短看不出有价值信息，frontmatter description 里写"日志不足以总结"，正文可以只写一段说明 + 建议用户重跑并扩大日志

# 输出格式（严格遵守）

必须以 YAML frontmatter 开头，格式如下：
```
---
name: <slug>      # 必须是 [a-z0-9][a-z0-9-]{0,62}[a-z0-9]，如 ollama-openeuler-2403
type: layer1-infra | layer2-build | layer3-troubleshoot
allowed-tools: [execute, read_file]
description: |
  <见下面 description 字段规则>
---
```

## description 字段规则（关键，决定 SKILL.md 能不能被搜到）

description 必须含三件前提：
  - **编译什么**（软件 / 工具 / 资源名）
  - **在哪个系统 + 发行版**（如 龙芯 openEuler-24.03 LTS）——不可省
  - **架构**：默认 loongarch64 可省

句式：[资源/能力一句话] + [系统 + 触发关键词，用顿号串]。

description 不写"怎么修"或"怎么用"——那是正文干的活。

正例（命中好）：
  ✓ "Go 工具链：龙芯 4 个版本白名单与安装命令。装 Go、go install、编译 Go 项目时读这条。"
  ✓ "Python 包源（lcr.loongnix.cn 等）。pip install、装带 C 扩展的 Python 包时读这条。"
  ✓ "ollama 在龙芯 openEuler-24.03 上的完整编译步骤（Go 1.55 / libstdc++ 依赖）。编译 ollama、撞到 collect2 链接报错时读这条。"

反例（命中差，**不要这样写**）：
  ✗ "ollama 编译失败缺少 libstdc++" ← 只写问题，没说系统/架构，也没说本 SKILL 提供什么
  ✗ "Tauri 编译攻略" ← 没说前提，泛用

## 正文结构（按 type 分支）

按 frontmatter 的 type 选正文结构，**不要四段一刀切**。

- type: layer1-infra（通用基础设施 / 工具链 / 镜像源）
    ## 简介（是什么）
    ## 安装/使用方法
    ## 注意事项
    ## 不要做

- type: layer2-build（单软件专属构建流程）
    ## 简要场景（软件 / 系统 / 结果）
    ## 构建步骤
    ## 踩坑点（仅写日志里实际出现的 error/警告 + 真实可行的规避）
    ## 验证方法

- type: layer3-troubleshoot（特定错误的应对方法）
    ## 错误症状（贴 1-2 行原日志）
    ## 触发场景（什么软件 / 什么环境时会出现）
    ## 真实根因（仅基于日志事实，不要推断）
    ## 规避方法（具体可验证的命令）

# 不要做
- 不要写"完整"攻略，只挑本次日志里出现的关键事实
- 不要写版本白名单之类的清单（那是 toolchain skill 的事，不是单任务经验）
- 不要加 "注意事项" 这种空话段（layer1-infra 类的"注意事项"是真实的，按正文结构要求保留）
- 不要使用 markdown 代码块包裹整个 frontmatter 区域
"""


def _sample_log_material(log: str, budget: int = _LOG_TRUNCATE_CHARS) -> str:
    """长日志智能采样（替代纯尾截断）。

    教训（2026-10-21 用户报）：尾部截断把前段的"主编译命令"掐掉了，LLM 只能产出
    "主编译命令不可见"的半截攻略。采样三段保序：
      ① 工具调用行（▶[执行命令]/[写入文件] 等——实际命令实录，全留）
      ② 错误特征行（error/failed/❌/Traceback）
      ③ 尾部 1/3 预算（结果汇总/产物清单）
    """
    if not log:
        return ""
    if len(log) <= budget:
        return log
    import re as _re
    cmd_re = _re.compile(r"▶\[[0-9a-f]{4}\]|\[(执行命令|写入文件|读取文件|查看目录|启动子任务)\]")
    err_re = _re.compile(r"error|failed|fatal|traceback|❌", _re.IGNORECASE)
    lines = log.splitlines()
    tail_budget = max(budget // 3, 500)
    # ③ 尾段
    tail: list[str] = []
    acc = 0
    for ln in reversed(lines):
        remaining = tail_budget - acc
        if remaining <= 0:
            break
        if len(ln) + 1 > remaining:
            # 超长行按剩余预算切，防尾段单行爆预算（ad-hoc 边界用例逮到的）
            tail.insert(0, ln[:max(remaining - 10, 50)] + " …[截]")
            acc = tail_budget
            break
        tail.insert(0, ln)
        acc += len(ln) + 1
    tail_start_idx = len(lines) - len(tail)
    head_budget = budget - acc - 200
    # ①② 前中段按行序采样（跳过已进尾段的行）
    picked: list[str] = []
    used = 0
    for ln in lines[:tail_start_idx]:
        if cmd_re.search(ln) or err_re.search(ln):
            cut = ln if len(ln) <= 300 else ln[:300] + " …[截]"
            if used + len(cut) + 1 > head_budget:
                picked.append("...[前中段超预算，后续执行行从略]...")
                break
            picked.append(cut)
            used += len(cut) + 1
    out = "\n".join(picked)
    out += "\n...[中段省略，以下为尾部]...\n" if tail_start_idx else ""
    out += "\n".join(tail)
    return out


def _build_user_prompt(
    software_name: str,
    system_name: str,
    git_url: str,
    git_branch: str,
    status: str,
    matched_skills: str,
    error_message: str,
    log_output: str,
    build_notes: str = "",
) -> str:
    # 智能采样（执行行+错误行+尾段），不再纯尾截断丢主编译命令
    log_truncated = _sample_log_material(log_output or "")

    return f"""# 任务元信息
- 软件：{software_name}
- 目标系统：{system_name}
- 源码：{git_url or "(未提供)"} (branch={git_branch or "?"})
- 最终状态：{status}
- 命中的 Skill：{matched_skills or "(无)"}
- 错误摘要：{error_message or "(无)"}

# 编译现场记录 BUILD_NOTES（agent 实操实录：命令/踩坑/与攻略差异——优先级最高的素材）
```
{(build_notes or "(无现场记录)")[:8000]}
```

# 完整日志（可能已截断尾部）
```
{log_truncated}
```

请按 system prompt 的格式输出 SKILL.md。只输出 SKILL.md 内容本身，不要加任何解释、不要加 "以下是..." 这种开场白。
"""


# ==================== Frontmatter 解析（仅服务校验，不入库）====================

def _parse_frontmatter(content: str) -> dict | None:
    """极简 frontmatter 解析：只取顶层 key: value（value 不带引号列表即可）"""
    if not content.startswith("---\n"):
        return None
    end = content.find("\n---\n", 4)
    if end < 0:
        # 容错：有的 LLM 漏了结尾换行
        end = content.find("\n---", 4)
        if end < 0:
            return None
    fm = content[4:end]
    out = {}
    for line in fm.split("\n"):
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _validate_draft(content: str) -> tuple[bool, str]:
    """校验 LLM 输出的 SKILL.md

    返回 (ok, reason)。reason 非空 = 校验失败的具体原因。
    """
    if not content or not content.strip():
        return False, "返回内容为空"

    fm = _parse_frontmatter(content)
    if fm is None:
        return False, "缺少 YAML frontmatter（必须以 --- 开头）"

    name = fm.get("name", "")
    if not name:
        return False, "frontmatter 缺 name 字段"
    if not _SKILL_NAME_RE.match(name):
        return False, f"name 不符合 slug 规则: {name!r}"

    description = fm.get("description", "")
    if not description:
        return False, "frontmatter 缺 description 字段"

    # type 不强制（LLM 可能用 layer1-infra 等自定义值），但要存在
    if "type" not in fm:
        return False, "frontmatter 缺 type 字段"

    # 正文长度下限（避免 LLM 只输出 frontmatter）
    body = content[content.find("---", 4) + 4:].strip()
    if len(body) < 30:
        return False, "正文过短（< 30 字）"

    return True, ""


# ==================== LLM 调用 ====================

def _call_llm(system: str, user: str, temperature: float = 0.3,
              max_tokens: int | None = None) -> str:
    """调 LLM，返回文本输出。无 key 则抛 RuntimeError

    max_tokens：输出上限。思考型模型（reasoning token 计入输出预算）写长文档时
    2048 会被 reasoning 吃光导致 content 为空——攻略生成用更大值。
    """
    from langchain_openai import ChatOpenAI  # lazy：避免 module-load 时拉无关依赖

    key = _ai_api_key()
    if not key:
        raise RuntimeError("AI_API_KEY 未配置（请检查后端环境变量）")

    llm = ChatOpenAI(
        model=_ai_model(),
        api_key=key,
        base_url=_ai_base_url() or None,
        temperature=temperature,
        max_completion_tokens=max_tokens or _MAX_TOKENS,
    )
    resp = llm.invoke([
        SystemMessage(content=system),
        HumanMessage(content=user),
    ])
    # ChatOpenAI 返回 AIMessage，content 可能是 str 或 list
    content = resp.content
    if isinstance(content, list):
        # 拼装多段 content
        parts = []
        for p in content:
            if isinstance(p, dict) and "text" in p:
                parts.append(p["text"])
            elif isinstance(p, str):
                parts.append(p)
        content = "".join(parts)
    return str(content).strip()


def _strip_wrapping(content: str) -> str:
    """剥掉 LLM 常见的多余包装

    处理 4 种情况：
    1. ```markdown ... ``` 围栏
    2. ``` ... ``` 围栏（无语言标签）
    3. <think>...</think> 推理段（MiniMax-M2.7 等带思考模式的模型会输出）
    4. 头部空白 + 推理段
    """
    s = content.strip()

    # 1) 去掉 <think>...</think> 块（支持多段，含跨行）
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL).strip()

    # 2) ```markdown / ```md / ``` 围栏
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*?)\n```\s*$", s, re.DOTALL)
    if m:
        s = m.group(1).strip()

    return s


# ==================== 主入口 ====================

def generate_draft(db: "Session", task_id: int) -> dict:
    """生成经验草稿（用户手动调，无确认后重试 1 次）

    Returns: { skill_name, skill_type, draft_content }
    Raises:
      LookupError       - task 不存在
      PermissionError   - task 不属于调用者（前置路由校验）
      RuntimeError      - LLM 调用失败或生成不合法
    """
    from app.models import CompileTask, TargetSystem, TaskStatus

    task = db.query(CompileTask).filter(CompileTask.id == task_id).first()
    if not task:
        raise LookupError(f"task 不存在: id={task_id}")

    # 终态校验（草稿只能基于已完成任务生成）
    finished = {TaskStatus.SUCCESS.value, TaskStatus.FAILED.value}
    if task.status not in finished:
        raise RuntimeError(
            f"任务未结束（status={task.status}），无法生成经验草稿"
        )

    # 拼装 system 上下文
    sys_rec = db.query(TargetSystem).filter(TargetSystem.system_id == task.system_id).first()
    system_name = sys_rec.name if sys_rec else f"system#{task.system_id}"

    matched_skills = ""
    try:
        ms = task.matched_skills
        if ms:
            # 2026-10-20 起该列=JSON list[str]（agent 读过的 SKILL.md）；老数据可能是别的形态，解析失败不炸
            try:
                parsed = json.loads(ms) if isinstance(ms, str) else ms
                if isinstance(parsed, list):
                    matched_skills = ", ".join(str(s) for s in parsed)
                else:
                    matched_skills = str(ms)[:200]
            except (ValueError, TypeError):
                matched_skills = str(ms)[:200]
    except Exception:
        pass

    # agent 现场记录（stage6 落定于任务目录；缺失回退旧行为）
    build_notes = ""
    try:
        from app.services.compile_engine import _artifacts_dir
        np = _artifacts_dir() / task.task_id / "BUILD_NOTES.md"
        if np.is_file():
            build_notes = np.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass

    user_prompt = _build_user_prompt(
        software_name=task.software_name,
        system_name=system_name,
        git_url=task.git_url or "",
        git_branch=task.git_branch or "",
        status=task.status,
        matched_skills=matched_skills,
        error_message=task.error_message or "",
        log_output=task.log_output or "",
        build_notes=build_notes,
    )

    # 调 LLM（重试 1 次）
    last_err = ""
    draft = None
    for attempt in (1, 2):
        try:
            raw = _call_llm(_SYSTEM_PROMPT, user_prompt, temperature=0.3)
            cleaned = _strip_wrapping(raw)
            ok, reason = _validate_draft(cleaned)
            if ok:
                draft = cleaned
                break
            last_err = reason
            logger.info("[generate_draft] attempt %d validation failed: %s", attempt, reason)
        except Exception as e:
            last_err = f"LLM 调用失败: {type(e).__name__}: {e}"
            logger.warning("[generate_draft] attempt %d raised: %s", attempt, last_err)
            break  # 网络/认证错误别重试，无意义

    if draft is None:
        raise RuntimeError(f"AI 生成草稿失败（{last_err}），请重试或手动填写")

    # 拆 skill_name / skill_type 给前端预填
    fm = _parse_frontmatter(draft)
    skill_name = fm.get("name", f"{task.software_name.lower()}-{system_name.lower().replace('.', '').replace(' ', '-')}")[:100]
    skill_type = _infer_skill_type(fm.get("type", ""), task.software_name)

    return {
        "skill_name": skill_name,
        "skill_type": skill_type,
        "draft_content": draft,
    }


def _infer_skill_type(llm_type: str, software_name: str) -> str:
    """把 LLM 的 type 映射成平台两分类：generic / specific

    平台只有这两个枚举值（SkillCreate schema 限定）。
    LLM 可能返回 layer1-infra / layer2-build / layer3-troubleshoot 等。
    """
    lt = llm_type.lower()
    # layer3-troubleshoot 或 troubleshoot / error → generic（通用错误经验）
    if "troubleshoot" in lt or "error" in lt or "fix" in lt:
        return "generic"
    # layer1-infra（工具链/环境）→ generic
    if "infra" in lt or "env" in lt or "toolchain" in lt:
        return "generic"
    # layer2-build（单软件编译）→ specific
    if "build" in lt or "compile" in lt or "install" in lt:
        return "specific"
    # 兜底：默认 specific（用户提交时可改）
    return "specific"