"""LLM 客户端层：共享节流闸 + 429 阶梯退避 + 协议无关判定 + 多模型池（2026-11-03）

设计档：references/llm-multimodel-throttle-design-20261103.md（权威）
单模型版：references/llm-429-throttle-design-20261102.md（三个不变式的原始论证）

三个不变式（改这套代码时必须守住）：
  ① 预约制间隔——锁内算"我的放行时刻"并写回，出锁后各自 sleep。
     朴素 check-then-sleep 有竞态：两线程同睡同醒 → 最小间隔名存实亡。
  ② 退避必须释放并发槽——release → sleep → re-acquire。
     否则被限流的请求占着槽不放，比不限流更糟。
  ③ SDK 重试只能开一层——SDK 秒级退避对 60 秒级 TPM 窗口无效；两层=双倍打点。

故障域：只提供同步入口，且调用点必须在 worker 线程（绝不能进事件循环线程 sleep）。

实现形态：**子类化 BaseChatModel**（不是组合包装）——
langchain 的 chat model 是 Pydantic 模型，deepagents 的 `model` 参数要求
`str | BaseChatModel`（deepagents L324 `isinstance(model, str)` 佐证），
普通包装对象会被拒。故本类继承 BaseChatModel，内部委托给按协议构造的底层实例。
"""
import asyncio
import os
import threading
import time
from typing import Any, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.config import get_cfg

THROTTLE_MSG = "AI 服务限流（配额不足），非编译问题；请稍后重试或联系管理员提额"


# ==================== 异常 ====================
class LlmThrottledError(RuntimeError):
    """LLM 限流（429/quota）退避耗尽仍未成功。与业务错误严格区分。"""

    def __init__(self, message: str, *, waited: float = 0.0, attempts: int = 0,
                 call_site: str = "", status_code: int = 429, slot_id: str = ""):
        super().__init__(message)
        self.waited = waited
        self.attempts = attempts
        self.call_site = call_site
        self.status_code = status_code
        self.slot_id = slot_id


# ==================== 429 判定（协议无关三路）====================
def is_rate_limited(e: Exception) -> bool:
    """判定是否限流。状态码优先（HTTP 429 跨厂商一致），文案仅兜底。

    实测依据（2026-11-03，MiniMax 探针）：
      e.status_code / e.response.status_code / e.response.headers 三路可用；
      body 形态 {"type":"error","error":{...},"http_code":...,"request_id":...}
    """
    if getattr(e, "status_code", None) == 429:
        return True
    resp = getattr(e, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True
    body = getattr(e, "body", None)
    if isinstance(body, dict) and body.get("http_code") == 429:
        return True
    t = str(e).lower()
    return any(k in t for k in ("429", "rate limit", "rate_limit", "too many requests",
                                "insufficient_quota", "quota exceeded", "throttl"))


def is_transient_provider_error(e: Exception) -> bool:
    """供应商侧瞬时错误：5xx / 连接断 / 超时（不含 429，不含 4xx 参数/鉴权类）。

    11-03 用户拍板：这类错误不整单失败——当次请求直接切备用模型继续
    （5xx 大概率是该后端实例/网关抖，换池即时恢复，无需退避等待）。
    """
    sc = getattr(e, "status_code", None)
    if sc is None:
        sc = getattr(getattr(e, "response", None), "status_code", None)
    if isinstance(sc, int) and sc >= 500:
        return True
    # SDK 异常类名跨厂商稳定（openai/anthropic 同名）
    if type(e).__name__ in ("APIConnectionError", "APITimeoutError", "ConnectTimeout",
                            "ReadTimeout", "ConnectError", "InternalServerError",
                            "ServiceUnavailableError", "BadGateway"):
        return True
    return isinstance(e, (ConnectionError, TimeoutError))


def retry_after_seconds(e: Exception) -> Optional[float]:
    """取 Retry-After（秒）。缺失返回 None。"""
    resp = getattr(e, "response", None)
    hdrs = getattr(resp, "headers", None)
    if not hdrs:
        return None
    try:
        v = hdrs.get("retry-after") or hdrs.get("Retry-After")
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ==================== 节流器 ====================
class LlmThrottle:
    """共享节流器（线程域）。每模型一个实例 → 配额池隔离。"""

    def __init__(self, max_concurrent: int = 2, min_interval: float = 1.5):
        self._sem = threading.Semaphore(max(1, int(max_concurrent)))
        self._lock = threading.Lock()
        self._last_grant = 0.0
        self._min_interval = max(0.0, float(min_interval))
        self.stats = {"calls": 0, "waited_total": 0.0, "throttled": 0, "failures": 0}

    def acquire(self) -> float:
        """预约放行时刻 → 出锁后 sleep → 占槽。返回本次等待秒数。

        预约制（而非 check-then-sleep）：锁内算出"我的放行时刻"并写回 _last_grant，
        出锁后各自 sleep 到点。锁窗口内只做算术（无 IO、无 sleep）→ 不串行化请求本身。
        """
        now = time.monotonic()
        with self._lock:
            earliest = self._last_grant + self._min_interval
            grant_at = max(now, earliest)
            self._last_grant = grant_at          # 写回 = 预约
            wait = grant_at - now
        if wait > 0:
            time.sleep(wait)                      # ← 必须发生在 worker 线程，不得在事件循环
        self._sem.acquire()
        self.stats["calls"] += 1
        self.stats["waited_total"] += wait
        return wait

    def release(self) -> None:
        self._sem.release()

    def reserve(self) -> float:
        """只预约放行时刻（纯计算，不 sleep、不占槽）——异步路径用。

        返回需等待秒数；调用方用 asyncio.sleep 等（不阻塞事件循环）。
        """
        now = time.monotonic()
        with self._lock:
            earliest = self._last_grant + self._min_interval
            grant_at = max(now, earliest)
            self._last_grant = grant_at
            wait = grant_at - now
        self.stats["calls"] += 1
        self.stats["waited_total"] += wait
        return wait

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.stats)


def backoff_schedule() -> list:
    """llm.throttle_backoff="30,60,120" → [30.0, 60.0, 120.0]"""
    raw = get_cfg("llm.throttle_backoff", "30,60,120")
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit():
            out.append(float(part))
    return out or [30.0, 60.0, 120.0]


# ==================== 模型槽位（多模型池）====================
class ModelSlot:
    """池中的一个模型：配置 + 专属节流器（独立配额池）+ 统计 + 熔断状态。"""

    def __init__(self, slot_id: str, provider: str, model: str, base_url: str, api_key: str,
                 priority: int = 1, enabled: bool = True, max_concurrent: int = 2,
                 min_interval_ms: int = 1500):
        self.id = slot_id
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.priority = priority
        self.enabled = enabled
        self.throttle = LlmThrottle(max_concurrent, min_interval_ms / 1000.0)
        self.stats = {"calls": 0, "throttled_429": 0, "waited_total": 0.0, "failures": 0}
        self._fail_streak = 0
        self._circuit_until = 0.0

    def circuit_open(self, now: float = None) -> bool:
        now = now if now is not None else time.monotonic()
        return self._circuit_until > now

    def record_failure(self) -> None:
        self._fail_streak += 1
        self.stats["failures"] += 1
        threshold = int(get_cfg("llm.circuit_threshold", 3))
        if self._fail_streak >= threshold:
            secs = int(get_cfg("llm.circuit_cooldown", 120))
            self._circuit_until = time.monotonic() + secs

    def record_success(self) -> None:
        self._fail_streak = 0
        self._circuit_until = 0.0

    def snapshot(self) -> dict:
        return {
            "id": self.id, "provider": self.provider, "model": self.model,
            "base_url": self.base_url, "enabled": self.enabled, "priority": self.priority,
            "circuit_state": "open" if self.circuit_open() else "closed",
            "fail_streak": self._fail_streak,
            **self.stats,
        }


# ==================== 协议适配层 ====================
def build_base_llm(slot: ModelSlot, *, temperature: float, max_tokens: Optional[int]):
    """按 provider 协议构造底层客户端（加新协议 = 加一个分支）。"""
    provider = (slot.provider or "openai_compatible").lower()
    if provider in ("openai_compatible", "openai", "compatible"):
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=slot.model, api_key=slot.api_key, base_url=slot.base_url or None,
            temperature=temperature,
            # 思考型模型（MiniMax M3 / Qwen3.8）reasoning 计入输出预算：
            # 用 max_tokens 会让 content 空（2026-10-21 实测：600 空 / 2048 正常）
            max_completion_tokens=max_tokens,
            max_retries=0,          # 不变式③：SDK 自带重试关掉，我们独管
        )
    if provider in ("anthropic", "claude"):
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=slot.model, api_key=slot.api_key,
            base_url=slot.base_url or None, temperature=temperature,
            max_tokens=max_tokens or 4096, max_retries=0,
        )
    if provider in ("google_genai", "gemini", "google"):
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=slot.model, google_api_key=slot.api_key, temperature=temperature,
            max_output_tokens=max_tokens or 4096, max_retries=0,
        )
    raise ValueError(f"不支持的 provider 协议: {provider}")


# ==================== 节流包装的 chat model ====================
class ThrottledChatModel(BaseChatModel):
    """BaseChatModel 子类：过闸 + 429 阶梯退避 + 模型池兜底。

    为什么子类化而非组合：langchain 的 chat model 是 Pydantic 模型，deepagents 的
    `model` 参数要求 `str | BaseChatModel`（组合包装对象会被拒）。
    """

    # Pydantic 字段（BaseChatModel 子类必须声明可配置项）
    base_model: Any = None
    slot: Any = None
    call_site: str = ""
    throttle_retries: int = 3
    throttle_max_wait: float = 300.0
    throttle_backoff: list = []
    fallback_slots: list = []
    temperature: float = 0.1
    max_tokens: Optional[int] = None

    @property
    def _llm_type(self) -> str:
        return "throttled-chat-model"

    # ── 核心：过闸 + 退避 + 兜底 ──
    def _decide_after_429(self, e: Exception, slot: ModelSlot, st: dict):
        """一次尝试 429 后的统一决策（同步/异步/流式三路共用，防语义漂移）。

        st 原地更新 {"waited": float, "attempts": int}。
        返回 ("switch", 新slot)（退避耗尽/超上限但有备用）| ("sleep", 秒数)（本模型继续退避）。
        放弃时抛 LlmThrottledError。
        """
        slot.stats["throttled_429"] += 1
        slot.throttle.stats["throttled"] += 1
        slot.record_failure()
        st["attempts"] += 1
        # tried 登记与瞬时切换同一语义（防主↔备乒乓；切出时记当前）
        nxt = self._next_untried_slot(slot, st)
        if st["attempts"] > self.throttle_retries or \
           (st["waited"] and st["waited"] >= self.throttle_max_wait):
            if nxt is not None:
                return ("switch", nxt)
            raise LlmThrottledError(
                THROTTLE_MSG, waited=st["waited"], attempts=st["attempts"],
                call_site=self.call_site, slot_id=slot.id) from e
        delay = self.throttle_backoff[min(st["attempts"] - 1, len(self.throttle_backoff) - 1)] \
            if self.throttle_backoff else 30.0
        ra = retry_after_seconds(e)
        if ra:
            delay = max(delay, float(ra))
        if st["waited"] + delay > self.throttle_max_wait:
            if nxt is not None:
                return ("switch", nxt)
            raise LlmThrottledError(
                THROTTLE_MSG, waited=st["waited"], attempts=st["attempts"],
                call_site=self.call_site, slot_id=slot.id) from e
        return ("sleep", delay)

    def _call_with_throttle(self, fn, slot: ModelSlot, consume: bool = False):
        """fn() 发一次真实请求。consume=True 时 fn 返回生成器——在本层消费成 list
        再返回（流式 429 藏在 generator 迭代里，建连阶段拦不住，11-03 T2 实测）。
        平台无 token 级 UI（log 按 node 边界整段渲染），消费完整样返回。"""
        st = {"waited": 0.0, "attempts": 0}
        while True:
            slot.throttle.acquire()
            slot.stats["calls"] += 1
            try:
                out = fn()
                if consume:
                    out = list(out)
                slot.record_success()
                slot.throttle.release()   # 成功也要释放：槽只在"在飞"期间占用
                if consume:
                    return iter(out)
                return out
            except Exception as e:
                if not is_rate_limited(e):
                    slot.record_failure()
                    slot.throttle.release()
                    # 11-03 用户拍板：5xx/连接/超时=瞬时错误 → 当次切备用继续；
                    # 4xx 参数/鉴权类与未知错误维持原样抛（换模型也不会更好，且防计费放大）
                    if is_transient_provider_error(e):
                        nxt = self._next_untried_slot(slot, st)
                        if nxt is not None:
                            self._switch_slot(nxt)
                            slot = nxt
                            continue
                    raise
                action, val = self._decide_after_429(e, slot, st)
                # 不变式②：退避/切换期间不占并发槽
                slot.throttle.release()
                if action == "switch":
                    self._switch_slot(val)
                    slot = val
                    st = {"waited": 0.0, "attempts": 0, "tried": st.get("tried", set())}
                    continue
                time.sleep(val)
                st["waited"] += val

    async def _call_with_throttle_async(self, fn, slot: ModelSlot, consume: bool = False):
        """异步路径的过闸+退避（reserve 纯计算 + asyncio.sleep，绝不阻塞事件循环）。
        429 决策复用 _decide_after_429（与同步/流式同一状态机，语义不漂移）。"""
        st = {"waited": 0.0, "attempts": 0}
        while True:
            slot.throttle.reserve()          # 预约放行时刻（纯计算，不阻塞）
            slot.stats["calls"] += 1
            try:
                out = await fn()
                if consume:
                    out = [c async for c in out]
                slot.record_success()
                if consume:
                    return _aiter_list(out)
                return out
            except Exception as e:
                if not is_rate_limited(e):
                    slot.record_failure()
                    if is_transient_provider_error(e):   # 与同步路同一口径（11-03 拍板）
                        nxt = self._next_untried_slot(slot, st)
                        if nxt is not None:
                            self._switch_slot(nxt); slot = nxt
                            continue
                    raise
                action, val = self._decide_after_429(e, slot, st)
                if action == "switch":
                    self._switch_slot(val); slot = val
                    st = {"waited": 0.0, "attempts": 0, "tried": st.get("tried", set())}
                    continue
                await asyncio.sleep(val)
                st["waited"] += val

    def _next_slot(self, current: ModelSlot):
        """池内下一个可用模型（跳过 disabled/熔断中的）。"""
        for s in sorted(self.fallback_slots or [], key=lambda x: x.priority):
            if s is current or not s.enabled or s.circuit_open():
                continue
            return s
        return None

    def _next_untried_slot(self, current: ModelSlot, st: dict):
        """瞬时错误切换专用：跳过本请求已试过的模型（防 A↔B 乒乓无限环）。

        tried 集合在切出时登记；全池试尽返回 None → 调用方原样抛错。
        """
        tried = st.setdefault("tried", set())
        tried.add(current.id)
        for s in sorted(self.fallback_slots or [], key=lambda x: x.priority):
            if s is current or s.id in tried or not s.enabled or s.circuit_open():
                continue
            return s
        return None

    def _switch_slot(self, nxt: ModelSlot) -> None:
        """切换到备用模型（重建底层客户端）。"""
        self.base_model = build_base_llm(nxt, temperature=self.temperature, max_tokens=self.max_tokens)
        self.slot = nxt

    # ── 四个入口覆写（langchain 标准接口）──
    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        def _fn():
            return self.base_model._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        return self._call_with_throttle(_fn, self.slot)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        """异步入口：reserve() 纯计算预约 + asyncio.sleep 等待——绝不在事件循环
        线程做 time.sleep/阻塞 acquire（批2 事故同款死法：一冻全站停摆）。
        并发上限由"最小间隔"软控；429 决策与同步路共用 _decide_after_429（语义不漂移）。
        """
        async def _fn():
            return await self.base_model._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        return await self._call_with_throttle_async(_fn, self.slot)

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        # consume=True：429 可能在 generator 迭代中途才抛（建连阶段拦不住），
        # 必须在消费侧整体重试（T2 实测坐实）；平台按 node 边界整段渲染，无 token 流损失
        def _fn():
            return self.base_model._stream(messages, stop=stop, run_manager=run_manager, **kwargs)
        return self._call_with_throttle(_fn, self.slot, consume=True)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        async def _fn():
            return self.base_model._astream(messages, stop=stop, run_manager=run_manager, **kwargs)
        # 流式 429 在迭代中途抛——异步同样消费式重试（与 _stream 对齐）
        return await self._call_with_throttle_async(_fn, self.slot, consume=True)

    # ── 便捷：直接 invoke（QA/草稿走这条）──
    def invoke(self, input, config=None, *, stop=None, **kwargs):
        return super().invoke(input, config, stop=stop, **kwargs)

    # ── bind_tools 必须转发（2026-11-03 真机事故）────────────────────────
    # langchain 的 agent factory 会调 model.bind_tools(tools)（agents/factory.py:1434
    # → langchain_core/language_models/chat_models.py:2383 基类默认 raise NotImplementedError）。
    # 包装类不转发 → deepagents/ticket_agent 一接单就炸（真机：工单 98 NotImplementedError）。
    # 返回的是"绑好工具的新实例"——不能原地改 self（会污染共享 client），
    # 故用 copy(deep=False) 复制本包装层，底层 base_model 由底层自己 bind_tools 产出。
    def bind_tools(self, tools, **kwargs):
        """工具绑定必须"绑在 self 上"（与 BaseChatModel 同构），不能把底层 binding
        塞回 base_model 再返回 self——RunnableBinding 没有 _generate，其 __getattr__
        会把 _generate 代理到内层 ChatOpenAI，tools 合并被绕过 → 模型收不到工具
        （11-03 真机二次事故：]<]minimax[>[<invoke 原始标记，agent 空转失败）。

        正确链路：base.bind_tools 完成 tool→provider 格式转换 → 取其 binding kwargs
        → self.bind(**kwargs) → invoke 时 RunnableBinding 把 tools 合并进
        ThrottledChatModel._generate → _fn 透传给 base_model._generate（与裸路径同构）。
        """
        bound = self.base_model.bind_tools(tools, **kwargs)
        merged = dict(getattr(bound, "kwargs", None) or {})
        if not merged:
            return bound
        return self.bind(**merged)

    # with_structured_output 基类同样 raise NotImplementedError（langchain_core
    # chat_models.py:2383 同族）。平台现无调用方（deepagents/QA 都走 bind_tools/invoke），
    # 直通底层保功能正确；节流在该路径缺失，注释声明之。
    def with_structured_output(self, schema, **kwargs):
        return self.base_model.with_structured_output(schema, **kwargs)


def _aiter_list(items):
    """把已消费的 list 包成异步迭代器（_astream 消费式重试用）。"""
    class _AIter:
        def __init__(self, seq): self._it = iter(seq)
        def __aiter__(self): return self
        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration
    return _AIter(items)


def _run_coroutine(coro):
    """同步入口里跑协程。已在事件循环中 → 明确报错（避免死锁）。"""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("LLM 同步入口被事件循环调用：请改用异步入口或移到 worker 线程")


# ==================== 模型池加载 ====================
_slots_lock = threading.Lock()
_slots_cache: Optional[list] = None


def load_model_slots() -> list:
    """加载模型池。yaml 有 llm.models 则池化；否则用旧三 env 造单模型池（行为等价）。"""
    global _slots_cache
    with _slots_lock:
        if _slots_cache is not None:
            return _slots_cache
        cfg = get_cfg("llm.models", None)
        slots = []
        if cfg:
            for m in cfg:
                key_env = m.get("api_key_env") or ""
                key = os.environ.get(key_env, "") or ""
                sid = m.get("id") or m.get("model") or "default"
                if not key:
                    _log(f"模型 {sid} 缺 key（env {key_env}）→ 跳过")
                    continue
                slots.append(ModelSlot(
                    slot_id=sid,
                    provider=m.get("provider") or "openai_compatible",
                    model=m.get("model") or "",
                    base_url=m.get("base_url") or "",
                    api_key=key,
                    priority=int(m.get("priority") or 99),
                    enabled=bool(m.get("enabled", True)),
                    max_concurrent=int(m.get("max_concurrent") or get_cfg("llm.max_concurrent", 2)),
                    min_interval_ms=int(m.get("min_interval_ms") or get_cfg("llm.min_interval_ms", 1500)),
                ))
        if not slots:
            from app.config import ai_model, ai_api_key, ai_base_url
            key = ai_api_key()
            if key:
                slots.append(ModelSlot(
                    slot_id="default", provider="openai_compatible", model=ai_model(),
                    base_url=ai_base_url(), api_key=key, priority=1, enabled=True,
                    max_concurrent=int(get_cfg("llm.max_concurrent", 2)),
                    min_interval_ms=int(get_cfg("llm.min_interval_ms", 1500)),
                ))
        slots.sort(key=lambda s: s.priority)
        _slots_cache = slots
        return slots


def reset_slots_cache() -> None:
    """配置热更新后清缓存（admin 改配置时调用）。"""
    global _slots_cache
    with _slots_lock:
        _slots_cache = None


def _log(msg: str) -> None:
    try:
        import logging
        logging.getLogger(__name__).warning("[llm_client] %s", msg)
    except Exception:
        pass


# ==================== 工厂（唯一入口）====================
def make_llm(*, temperature: float = 0.1, max_tokens: Optional[int] = None,
             call_site: str = "", prefer: Optional[str] = None,
             model_id: Optional[str] = None) -> ThrottledChatModel:
    """所有 LLM 调用的唯一入口：过闸 + 退避 + 池内兜底。"""
    slots = load_model_slots()
    if not slots:
        raise RuntimeError("AI_API_KEY 未配置（请检查后端环境变量）")
    chosen = None
    for want in (prefer, model_id):
        if not want:
            continue
        for s in slots:
            if s.id == want and s.enabled:
                chosen = s
                break
        if chosen:
            break
    if chosen is None:
        usable = [s for s in slots if s.enabled and not s.circuit_open()] or \
                 [s for s in slots if s.enabled]
        chosen = sorted(usable, key=lambda s: s.priority)[0]
    base = build_base_llm(chosen, temperature=temperature, max_tokens=max_tokens)
    return ThrottledChatModel(
        base_model=base, slot=chosen, call_site=call_site,
        throttle_retries=int(get_cfg("llm.throttle_retries", 3)),
        throttle_max_wait=float(get_cfg("llm.throttle_max_wait", 300)),
        throttle_backoff=backoff_schedule(),
        fallback_slots=slots,
        temperature=temperature, max_tokens=max_tokens,
    )


def llm_stats() -> dict:
    """池状态快照（admin /api/admin/llm/stats 用）。"""
    slots = load_model_slots()
    return {
        "slots": [s.snapshot() for s in slots],
        "global": {
            "total_calls": sum(s.stats["calls"] for s in slots),
            "total_throttled": sum(s.stats["throttled_429"] for s in slots),
            "total_waited": round(sum(s.stats["waited_total"] for s in slots), 1),
            "total_failures": sum(s.stats["failures"] for s in slots),
        },
    }
