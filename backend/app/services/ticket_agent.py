"""
工单答疑 Agent：复用 stage5 的 DeepAgent + DockerBackend 框架，
每个工单（接单时）启动一个容器 + agent session，admin 与 agent 多轮对话，
agent 真实在容器里跑命令/改源码，记录所有修改到 .patches/，最终生成 SKILL.md 草稿。

Skills 注入：接单时一次（agent session 创建时 skills=paths 一次性加载）。
"""
import os
import asyncio
import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List, AsyncIterator, Any, Callable

from sqlalchemy.orm import Session

from app.services.compile_engine import (
    _artifacts_dir,
    _ai_api_key, _ai_model, _ai_base_url,
    stage3_create_container, stage4a_fetch_source_on_host, stage6_cleanup,
)
from app.services.skill_loader import get_skills_paths, _parse_frontmatter_description
from app.models import Ticket, CompileTask, TicketMessage, User, AuditLog
from langchain.agents.middleware import AgentMiddleware


# ============== 单例 ==============
class _Session:
    def __init__(self, ticket_id: int, container_id: str, work_dir: str,
                 agent, agent_state: dict, docker_backend, task: CompileTask,
                 ticket: Ticket, created_at: float, config: Optional[dict] = None):
        self.ticket_id = ticket_id
        self.container_id = container_id
        self.work_dir = work_dir
        self.agent = agent
        self.agent_state = agent_state  # stage TaskState 兼容
        self.backend = docker_backend
        self.task = task
        self.ticket = ticket
        self.created_at = created_at
        self.lock = asyncio.Lock()  # 多轮串行
        self.config = config or {}  # langgraph thread_id config（多轮累积 key）
        # 2026-10-19 方案 2：工单调试实时阶段提示（轮询用，不靠 callback）
        self.phase_events: list = []  # [{"phase":..., "detail":..., "ts":...}, ...]
        self._phase_seq = 0  # 递增序号，前端轮询拿 since_seq 拿新增


class TicketAgentManager:
    def __init__(self):
        self._sessions: Dict[int, _Session] = {}
        self._errors: Dict[int, str] = {}  # 启动失败信息（admin 端可见）
        self._lock = asyncio.Lock()

    async def get_or_create(self, ticket: Ticket, db: Session,
                            on_event: Optional[Callable] = None) -> _Session:
        """接单时调用：建容器 + 拉源码 + 启动 agent（接单时一次 Skills 注入）

        失败时抛异常，调用方（admin.py）应捕获并记录到 _errors
        """
        if ticket.ticket_id in self._sessions:
            return self._sessions[ticket.ticket_id]
        async with self._lock:
            if ticket.ticket_id in self._sessions:
                return self._sessions[ticket.ticket_id]
            sess = await self._create_session(ticket, db, on_event)
            self._sessions[ticket.ticket_id] = sess
            # 启动成功，清除之前的错误
            self._errors.pop(ticket.ticket_id, None)
            return sess

    def get(self, ticket_id: int) -> Optional[_Session]:
        return self._sessions.get(ticket_id)

    def get_error(self, ticket_id: int) -> Optional[str]:
        """获取 agent 启动错误（admin 端可见）"""
        return self._errors.get(ticket_id)

    def save_checkpoint(self, ticket_id: int, db: Session) -> bool:
        """把当前 session 的 langgraph state 序列化到 DB"""
        print(f"[save_checkpoint] ticket_id={ticket_id} called")
        sess = self._sessions.get(ticket_id)
        if not sess or not sess.config:
            print(f"[save_checkpoint] ticket_id={ticket_id} sess={sess} config={sess and sess.config}")
            return False
        try:
            from app.models import TicketAgent
            import base64
            config = sess.config

            # 优先用 agent.get_state(config) 拿最新 state（最可靠）
            ckpt_data = None
            if hasattr(sess.agent, "get_state"):
                try:
                    state_obj = sess.agent.get_state(config)
                    # state_obj.values 是 dict（含 messages）
                    if state_obj and hasattr(state_obj, "values"):
                        values = state_obj.values
                        # messages 可能有 AIMessage / HumanMessage 等不能 JSON 序列化
                        # 序列化为 LangChain 自己的格式（list of dicts）
                        try:
                            from langchain_core.messages import messages_from_dict, messages_to_dict
                            msgs_dict = messages_to_dict(values.get("messages", []))
                        except Exception:
                            msgs_dict = []
                            for m in values.get("messages", []):
                                if hasattr(m, "content"):
                                    role = getattr(m, "type", "assistant")
                                    msgs_dict.append({
                                        "type": role,
                                        "data": {"content": m.content, "id": getattr(m, "id", "")},
                                    })
                        # 构造 checkpoint
                        import uuid as _uuid
                        ckpt_data = {
                            "v": 1,
                            "id": getattr(state_obj, "config", {}).get("configurable", {}).get("thread_id", "") or str(_uuid.uuid4()),
                            "ts": None,
                            "channel_values": {
                                "messages": msgs_dict,
                            },
                            "channel_versions": {"messages": len(msgs_dict)},
                            "versions_seen": {},
                        }
                except Exception as e:
                    print(f"[ticket_agent] get_state 失败: {e}")

            if not ckpt_data:
                # fallback: 从 checkpointer 拿
                cp = sess.agent.checkpointer if hasattr(sess.agent, "checkpointer") else None
                if cp is None:
                    return False
                ckpt_tuple = None
                if hasattr(cp, "get_tuple"):
                    ckpt_tuple = cp.get_tuple(config)
                elif hasattr(cp, "get"):
                    ckpt_tuple = cp.get(config)
                if not ckpt_tuple:
                    return False
                ckpt = ckpt_tuple.checkpoint if hasattr(ckpt_tuple, "checkpoint") else ckpt_tuple
                ckpt_data = {
                    "v": getattr(ckpt, "v", 1),
                    "id": getattr(ckpt, "id", None),
                    "ts": getattr(ckpt, "ts", None),
                    "channel_values": getattr(ckpt, "channel_values", {}),
                    "channel_versions": getattr(ckpt, "channel_versions", {}),
                    "versions_seen": getattr(ckpt, "versions_seen", {}),
                }

            # 序列化（处理可能的 datetime / UUID）
            blob = json.dumps(ckpt_data, ensure_ascii=False, default=str).encode("utf-8")
            b64 = base64.b64encode(blob).decode("ascii")
            # 写 DB
            rec = db.query(TicketAgent).filter(TicketAgent.ticket_id == ticket_id).first()
            if not rec:
                rec = TicketAgent(ticket_id=ticket_id)
                db.add(rec)
            rec.checkpoint_state = b64
            db.commit()
            return True
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[ticket_agent] save_checkpoint 失败: {e}")
            return False

    async def destroy(self, ticket_id: int, stop_only: bool = False) -> bool:
        """销毁 in-memory session。

        - stop_only=True（resolve 场景）：只停容器，容器对象保留 → 重新接单可 docker start 复用
        - stop_only=False（cleanup 场景）：stage6 停+删容器（历史行为）
        两种都保留 work_dir / checkpoint / DB 记录（对话历史要能恢复）
        """
        async with self._lock:
            sess = self._sessions.pop(ticket_id, None)
        if not sess:
            return False
        # 容器 stop（不删 work_dir）
        try:
            cli_sess = _Session.__new__(_Session)
            cli_sess.work_dir = sess.work_dir
            cli_sess.container_id = sess.container_id
            cli_sess.agent_state = sess.agent_state or {}
            cli_sess.agent_state["status"] = "success"  # 不影响 cleanup 路径
            if stop_only:
                cli_sess.agent_state["_stop_only"] = True
            stage6_cleanup(sess.agent_state, on_log=lambda *a, **k: None)
        except Exception as e:
            print(f"[ticket_agent] 容器清理异常: {e}")
        # DB 同步：清 container_id（标记容器已停），保留 work_dir + checkpoint
        try:
            from app.models import SessionLocal, TicketAgent
            s = SessionLocal()
            rec = s.query(TicketAgent).filter(TicketAgent.ticket_id == ticket_id).first()
            if rec:
                if stop_only:
                    pass  # 容器对象还在（只 stopped）→ 保留 container_id 让 stage3 复用（docker start）
                else:
                    rec.container_id = None  # 容器已删
                s.commit()
            s.close()
        except Exception as e:
            print(f"[ticket_agent] destroy DB 同步异常: {e}")
        return True

    async def recycle(self, ticket_id: int) -> str:
        """工单关闭终态回收：删容器（stop+rm）+ 清 DB container_id；保留 work_dir/checkpoint/对话记录。
        没有内存 session 也能回收（从 DB 拿 container_id 直删）→ 解决孤儿容器。
        返回描述：'no-container' / 'removed' / 'remove-failed: ...'"""
        # 1) 内存 session（可能没有——重启后就是孤儿）
        async with self._lock:
            sess = self._sessions.pop(ticket_id, None)
        # 2) DB 记录拿容器 ID（内存没有时兜底）
        from app.models import SessionLocal, TicketAgent, AuditLog
        s = SessionLocal()
        container_id = (sess.container_id if sess else None)
        rec = s.query(TicketAgent).filter(TicketAgent.ticket_id == ticket_id).first()
        if not container_id and rec:
            container_id = rec.container_id
        result = "no-container"
        if container_id:
            try:
                cli = __import__("app.services.compile_engine", fromlist=["get_docker_client"]).get_docker_client()
                if cli:
                    c = cli.containers.get(container_id)
                    try:
                        c.stop(timeout=5)
                    except Exception:
                        pass
                    c.remove()
                    result = "removed"
            except Exception as e:
                result = f"remove-failed: {e}"
        if rec:
            rec.container_id = None
            s.commit()
        s.close()
        return result

    # ============== 孤儿扫描（启动 + 定期） ==============
    async def sweep_orphans(self) -> dict:
        """对账 docker ↔ DB：
        1) DB 引用但容器已不存在 → 清 container_id（下次重建走全新路径）
        2) 容器还活着/残留 但工单已 closed → remove（回收）
        3) 工单 resolved 且容器 Up → stop（resolve 时应已 stop，漏网兜底）
        4) 带 compile.ticket label 但 DB 无记录（force-cleanup 漏删）→ closed 才删，其余只记日志
        返回 {checked, removed, stopped, stale_cleared}
        """
        from app.models import SessionLocal, TicketAgent, Ticket
        stats = {"checked": 0, "removed": 0, "stopped": 0, "stale_cleared": 0}
        cli = __import__("app.services.compile_engine", fromlist=["get_docker_client"]).get_docker_client()
        if cli is None:
            return stats
        s = SessionLocal()
        try:
            recs = s.query(TicketAgent).filter(TicketAgent.container_id.isnot(None)).all()
            for rec in recs:
                stats["checked"] += 1
                t = s.query(Ticket).filter(Ticket.ticket_id == rec.ticket_id).first()
                status = t.status if t else "closed"
                if hasattr(status, "value"):
                    status = status.value
                try:
                    c = cli.containers.get(rec.container_id)
                    live = c.status == "running"
                except Exception:
                    c = None
                    live = False
                if c is None:
                    # 容器已被外部删除 → DB 记 stale
                    rec.container_id = None
                    s.commit()
                    stats["stale_cleared"] += 1
                    continue
                if status == "closed":
                    try:
                        if live:
                            c.stop(timeout=5)
                        c.remove()
                        stats["removed"] += 1
                    except Exception as e:
                        print(f"[sweep] tkt={rec.ticket_id} remove 失败: {e}")
                    rec.container_id = None
                    s.commit()
                elif status == "resolved" and live:
                    try:
                        c.stop(timeout=5)
                        stats["stopped"] += 1
                    except Exception as e:
                        print(f"[sweep] tkt={rec.ticket_id} stop 失败: {e}")
            # 带 ticket label 但 DB 无记录：force-cleanup 删了记录但容器 remove 失败漏网 → 只删终态的
            try:
                for c in cli.containers.list(all=True):
                    lbl = (c.labels or {}).get("compile.ticket")
                    if not lbl or not lbl.isdigit():
                        continue
                    tid = int(lbl)
                    if s.query(TicketAgent).filter(TicketAgent.ticket_id == tid).first():
                        continue
                    t = s.query(Ticket).filter(Ticket.ticket_id == tid).first()
                    st = t.status if t else "closed"
                    if hasattr(st, "value"):
                        st = st.value
                    if st in ("closed", "resolved"):
                        try:
                            if c.status == "running":
                                c.stop(timeout=5)
                            c.remove()
                            stats["removed"] += 1
                        except Exception:
                            pass
            except Exception as e:
                print(f"[sweep] label 扫描异常: {e}")
        finally:
            s.close()
        if any(stats[k] for k in stats):
            print(f"[sweep] 孤儿对账: {stats}")
        return stats


# ============== 内部：建 session ==============
    async def _create_session(self, ticket: Ticket, db: Session,
                              on_event: Optional[Callable] = None) -> _Session:
        task: CompileTask = ticket.task  # relationship

        # 1) 准备 state 字典（stage3/4a 用的格式）
        # 先查 TargetSystem 拿 docker_image
        from app.models import TargetSystem as _TS, TicketAgent
        ts = db.query(_TS).filter(_TS.system_id == task.system_id).first()
        docker_image = (ts.base_image if ts else None) or "loongnix-build:23.2"

        # work_dir 用 ticket_id 区分（不要污染原 task 的容器）
        work_dir = str(_artifacts_dir() / f"{task.task_id}-TKT{ticket.ticket_id}")
        Path(work_dir).mkdir(parents=True, exist_ok=True)

        # state 模拟 compile_engine 的 TaskState（足够 stage3/4a 用）
        # 注意：None 要替换成默认值，否则 state.get(k, "") 会返 None（dict.get 只在 key 缺失才用 default）
        state: dict = {
            "task_id": f"{task.task_id}-TKT{ticket.ticket_id}",
            "software_name": task.software_name,
            "source_type": task.source_type,
            "git_url": task.git_url or "",  # None → ""（避免后续 .strip 炸）
            "git_branch": task.git_branch or "",
            "source_path": task.source_path or "",
            "work_dir": work_dir,
            "container_work_dir": "/work",
            "docker_image": docker_image,
            "status": "submitted",
            "extra_labels": {"compile.ticket": str(ticket.ticket_id)},  # 孤儿扫描按工单归属反查
            "_task": None,  # 不复用 task 行
        }
        log_cb = lambda level, msg: on_event and on_event({"type": "log", "level": level, "msg": msg}) if on_event else None
        # 2026-10-19 方案 2：工单调试实时阶段提示（推 role="system" 消息到前端 chat）
        # phase_cb 同时做两件事：1) 回调（如果有） 2) in-memory 缓存供前端轮询
        _sess_ref = {"sess": None}  # 占位，_create_session 后赋值
        def phase_cb(phase: str, detail: str = ""):
            entry = {"phase": phase, "detail": detail, "ts": __import__("time").time()}
            if on_event:
                on_event({"type": "phase", **entry})
            s = _sess_ref["sess"]
            if s is not None:
                s._phase_seq += 1
                entry["seq"] = s._phase_seq
                s.phase_events.append(entry)
            else:
                # sess 还没建好（前 6 个 phase_cb 调到这里），先 buffer，等 sess 建好后回填
                phase_cb._buffered = getattr(phase_cb, "_buffered", []) + [entry]

        # 1.5) 查 DB：上次的容器 / work_dir / checkpoint（解决"每次重启重建"问题）
        existing_container_id = None
        agent_record = db.query(TicketAgent).filter(TicketAgent.ticket_id == ticket.ticket_id).first()
        if agent_record and agent_record.container_id:
            existing_container_id = agent_record.container_id
            log_cb("info", f"[复用] DB 记录容器 {existing_container_id[:12]}，尝试复用")
            phase_cb("reuse_container", f"复用容器 {existing_container_id[:12]}")

        # 2) 阶段 3：建容器 / 复用
        # ⚠ 同步重活必须 to_thread：直接 await 调用会在事件循环线程跑 docker/clone，
        #   冻结全站（2026-10-22 事故：接单 pytorch clone 期间 /api/health 35s 无响应，
        #   管理页"接单卡住/查看超时"）。编译引擎同款问题当年靠 executor 线程修的，这里对齐。
        phase_cb("create_container", f"启动镜像 {docker_image}...")
        state = await asyncio.to_thread(  # type: ignore[assignment]  # TaskState 运行时=普通 dict（total=False），未声明键透传无碍
            stage3_create_container, state, on_log=log_cb, existing_container_id=existing_container_id)
        if state.get("status") == "failed":
            raise RuntimeError(f"容器创建失败: {state.get('last_error')}")
        phase_cb("container_ready", f"容器 {state['container_id'][:12]} 已就绪")

        # 3) 阶段 4a：拉源码（已存在则跳过 git clone —— 复用之前的源码）
        src_dir = Path(state["work_dir"]) / "container_work" / "src"
        if src_dir.exists() and (src_dir / ".git").exists():
            phase_cb("reuse_source", f"复用源码 {src_dir}")
        else:
            phase_cb("fetch_source", f"拉取源码 {state.get('git_url') or state.get('source_path')}")
        try:
            state = await asyncio.to_thread(stage4a_fetch_source_on_host, state, on_log=log_cb)  # type: ignore[assignment]
        except Exception as e:
            await self._kill_container(state.get("container_id"))
            raise RuntimeError(f"拉源码失败: {e}")
        phase_cb("source_ready", f"源码就绪 commit={state.get('commit_hash', '?')[:8]}")

        # 4) 创建 DockerBackend（容器内操作）
        phase_cb("init_backend", "初始化容器执行后端...")
        from app.services.docker_backend import DockerBackend
        backend = DockerBackend(
            container_id=state["container_id"],
            work_dir=state["container_work_dir"],
        )

        # 5) 启动 DeepAgent（接单时一次 Skills 注入）
        phase_cb("init_llm", f"初始化 LLM client ({_ai_model()})...")
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            model=_ai_model(),
            api_key=_ai_api_key(),
            base_url=_ai_base_url() or None,
            temperature=0.1,
        )
        # 接管 mkdir 容器内 .patches/（记录修改）
        backend.execute("mkdir -p /work/.patches")

        # system_prompt 注入工单上下文 + Skills 摘要
        skills_paths = get_skills_paths()
        # Skills 摘要：列每个 skill 的描述（前 100 字）给 agent 看
        skills_summary = self._build_skills_summary(skills_paths)
        phase_cb("build_prompt", f"构造 system_prompt（skills={len(skills_paths)}）...")

        sys_prompt = self._build_sys_prompt(ticket, task, state, skills_summary)

        # 挂中间件：记录 edit/write_file 到 /work/.patches/
        patch_mw = _PatchRecorderMiddleware(backend)

        from langgraph.checkpoint.memory import InMemorySaver
        from deepagents import create_deep_agent
        # checkpointer 让 langgraph state 跨多次 stream 累积
        # 2026-10-19：显式传 checkpointer=cp —— 否则 deepagents 用内部默认（None），
        # 导致 sess.agent.get_state() 报 "No checkpointer set"，save_checkpoint 失败
        cp = InMemorySaver()
        phase_cb("create_deep_agent", f"启动 DeepAgent（{len(skills_paths)} skills 注入，可能 5-30s）...")
        agent = create_deep_agent(
            model=llm,
            system_prompt=sys_prompt,
            backend=backend,
            skills=skills_paths,
            middleware=[patch_mw],
            checkpointer=cp,  # 修复："No checkpointer set" 错误
        )
        phase_cb("deep_agent_ready", "DeepAgent 启动完成")
        # thread_id 每 ticket 唯一，跨 stream 用同一 thread 累积 messages
        thread_id = f"ticket-{ticket.ticket_id}"
        config = {"configurable": {"thread_id": thread_id}}

        # 恢复历史 checkpoint：跨进程/重启后 admin 跟 agent 继续对话
        from app.models import TicketAgent
        rec = db.query(TicketAgent).filter(TicketAgent.ticket_id == ticket.ticket_id).first()
        if rec and rec.checkpoint_state:
            try:
                import base64
                blob = base64.b64decode(rec.checkpoint_state.encode("ascii"))
                # langgraph InMemorySaver.put(config, checkpoint) 接受 dict
                # 把序列化 state 当作 initial checkpoint 灌回
                restored = json.loads(blob.decode("utf-8"))
                # state schema: {v: 1, id: <uuid>, ts: ..., channel_values: {messages: [...], ...}}
                # InMemorySaver.put(config, checkpoint, metadata, new_versions)
                # checkpoint 字段要含 channel_values
                if isinstance(restored, dict) and "channel_values" in restored:
                    import uuid as _uuid
                    from langgraph.checkpoint.base import Checkpoint
                    ckpt = Checkpoint(
                        v=restored.get("v", 1),
                        id=restored.get("id") or str(_uuid.uuid4()),
                        ts=restored.get("ts"),
                        channel_values=restored["channel_values"],
                        channel_versions=restored.get("channel_versions", {}),
                        versions_seen=restored.get("versions_seen", {}),
                    )
                    cp.put(config, ckpt, {}, {})
            except Exception as e:
                print(f"[ticket_agent] 恢复 checkpoint 失败: {e}")

        sess = _Session(
            ticket_id=ticket.ticket_id,
            container_id=state["container_id"],
            work_dir=work_dir,
            agent=agent,
            agent_state=state,
            docker_backend=backend,
            task=task,
            ticket=ticket,
            created_at=time.time(),
            config=config,  # langgraph thread_id（多轮累积 key）
        )
        # 2026-10-19 方案 2：把 _sess_ref 指向新 sess，后续 phase_cb 写入新 sess 的 phase_events
        # 并回填之前 6 个 phase_cb 阶段（容器/源码/LLM/agent），给前端一个连贯历史
        _sess_ref["sess"] = sess
        for ph in getattr(phase_cb, "_buffered", []):
            sess._phase_seq += 1
            entry = dict(ph, seq=sess._phase_seq)
            sess.phase_events.append(entry)
        phase_cb._buffered = []  # 清缓冲
        # 持久化到 DB（container_id + work_dir）—— 下次接单可复用
        if not rec:
            rec = TicketAgent(ticket_id=ticket.ticket_id)
            db.add(rec)
        rec.container_id = state["container_id"]
        rec.work_dir = work_dir
        rec.checkpoint_path = f"{work_dir}/.agent_state.json"
        rec.last_error = None  # 启动成功 → 清错
        try:
            db.commit()
        except Exception:
            db.rollback()  # 持久化失败不阻塞主流程

        if on_event:
            on_event({"type": "session_created", "container_id": state["container_id"][:12]})
        return sess

    def _build_skills_summary(self, skills_paths: List[str]) -> str:
        """接单时一次：读所有 skill 的 frontmatter description，列给 agent 看"""
        lines = []
        for p in skills_paths:
            try:
                txt = Path(p).read_text(encoding="utf-8", errors="replace")
                desc = _parse_frontmatter_description(txt) or ""
                # 截短
                if len(desc) > 200:
                    desc = desc[:200] + "..."
                lines.append(f"- `{Path(p).name}`: {desc}")
            except Exception:
                continue
        return "\n".join(lines) if lines else "(暂无可用 skill)"

    def _build_sys_prompt(self, ticket: Ticket, task: CompileTask,
                          state: dict, skills_summary: str) -> str:
        # 读原 task 的 build.log 末 200 行
        log_path = Path(_artifacts_dir()) / task.task_id / "build.log"
        log_tail = ""
        if log_path.exists():
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                log_tail = "\n".join(lines[-200:])
            except Exception:
                log_tail = "(日志读取失败)"

        source_info = (
            f"GIT  {task.git_url or '?'}  branch={task.git_branch or '(default)'}"
            if task.source_type == "git"
            else f"UPLOAD  {task.source_path or '?'}"
        )

        return f"""你是龙芯 LoongArch 平台编译排错 + 实战编译 agent。
你在容器内工作，工具调用就是真实命令，不是模拟。

## 工单 #{ticket.ticket_id}
- 标题: {ticket.title}
- 描述: {ticket.description or '(用户没填描述)'}

## 关联任务
- task_id: {task.task_id} (T-{task.task_id})
- 软件: {task.software_name}
- 系统: id={task.system_id}
- 源码: {source_info}
- 源码已克隆/解压到容器: /work/src
- 产物输出目录: /work/output

## 原任务失败日志末尾 200 行
```
{log_tail or '(无日志)'}
```

## 可用 Skills 摘要（接单时一次注入，过程中不变）
{skills_summary}
（每个 skill 的详细攻略用 read_file 读，路径在工具列表里）

## 你能用的工具
- execute: 跑 shell 命令（真实改容器状态）
- read_file / ls / grep / glob: 读源码
- write_file / edit: 改源码（**自动记录到 /work/.patches/**，最终生成经验用）
- task: 拆子任务

## 工作流程建议
1. ls /work/src 看源码结构
2. read go.mod / Makefile / configure 找关键版本要求
3. 装缺的工具（dnf install / wget）
4. 改源码（如果 SKILL.md 或对话提示了）
5. 跑编译（go build / make / cmake）
6. 验证产物：ls -lh /work/output/

## 处理记录（强制，经验沉淀的主料）
开工后第一时间在 `/work/output/BUILD_NOTES.md` 建立骨架（先 write_file 写入软件/环境探测/计划），
每过一个里程碑就更新对应节：症状 → 排查路线（含试错与被否的方案）→ 修复动作（贴实际命令原文）
→ 验证方式与结果 → 可复用步骤清单。你写的每个文件都在宿主机留档（bind 挂载），关容器不丢，
生成攻略会全文引用它——写得像"给下一个人的操作手册"，别只当聊天草稿。

## 边界
- ✅ /work 目录内任意改、装编译依赖
- ❌ /work 外操作、Docker 命令、重启容器
- ❌ git push（不会污染上游）
- ❌ 不要删除容器

## 成功判定
- 产物存在 /work/output/ 目录里
- agent 输出"编译成功"或类似总结
- **不要**自动标记工单已解决——只有 admin 手动点才改状态

回复要求：简洁、可执行、每步带验证方式。
"""

    async def _kill_container(self, container_id: Optional[str]):
        if not container_id:
            return
        try:
            from app.services.compile_engine import get_docker_client
            cli = get_docker_client()
            if cli:
                c = cli.containers.get(container_id)
                c.stop(timeout=5)
                c.remove()
        except Exception:
            pass


# ============== 修改记录中间件 ==============
class _PatchRecorderMiddleware(AgentMiddleware):
    """拦截 agent 的 edit/write_file，写 .patches/NNN.json 到容器"""

    def __init__(self, backend):
        self.backend = backend
        self.counter = 0

    def wrap_tool_call(self, request, call):
        try:
            tool_name = getattr(call, "name", None) or (call.tool if hasattr(call, "tool") else "")
            if tool_name in ("edit", "write_file", "edit_file", "write"):
                self.counter += 1
                inp = call.input if hasattr(call, "input") else call.args
                patch = {
                    "n": self.counter,
                    "tool": tool_name,
                    "file_path": inp.get("file_path") or inp.get("path"),
                    "ts": datetime.utcnow().isoformat() + "Z",
                }
                # 截短避免撑大
                if "old_string" in inp:
                    patch["old_string"] = str(inp["old_string"])[:500]
                if "new_string" in inp:
                    patch["new_string"] = str(inp["new_string"])[:500]
                if "content" in inp:
                    patch["content"] = str(inp["content"])[:500]
                rec = json.dumps(patch, ensure_ascii=False)
                # 写到容器 .patches/
                self.backend.execute(
                    f"mkdir -p /work/.patches && cat > /work/.patches/patch_{self.counter:03d}.json << 'PATCH_EOF'\n{rec}\nPATCH_EOF"
                )
        except Exception:
            pass
        # 关键：return call(request) 而不是 call（call 是 handler 函数本身）
        return call(request)


# 全局单例
MANAGER = TicketAgentManager()

# 正在自动恢复中的工单（防 status 轮询并发重复触发 get_or_create）
RESTORING: set = set()
