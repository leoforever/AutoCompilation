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
class LlmStreamIdleTimeout(RuntimeError):
    """流空闲超时（11-24）：idle 秒内没有新的**已解析 chunk**到达。

    专治"网关 keep-alive 假滴字"挂相——底层 socket 每几十秒有字节（注释行/
    padding），读超时永不触发；但这些字节在 SDK 解析层就被丢掉，永远到不了
    业务层。所以业务计时器只被合法 chunk 重置：格式无关，任何网关通吃，
    无需识别 padding 长什么样。按瞬时错误处理 → 切槽重发（is_transient 名单）。"""

    def __init__(self, message: str, *, idle_seconds: float = 0.0,
                 call_site: str = "", slot_id: str = ""):
        super().__init__(message)
        self.idle_seconds = idle_seconds
        self.call_site = call_site
        self.slot_id = slot_id


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
    if isinstance(e, LlmStreamIdleTimeout):
        return True   # 11-24：假滴字挂起等同供应商抖——换槽重发整请求（consume 全量缓冲，无半截风险）
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
                 min_interval_ms: int = 1500, max_input_tokens: "int | None" = None):
        self.id = slot_id
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.priority = priority
        self.enabled = enabled
        # 11-23 deepagents 上下文压缩口径：模型窗口（token）。配了才给 ChatOpenAI
        # 传 profile={"max_input_tokens":N} → deepagents 摘要阈值走 fraction 分支
        # (0.85×窗口触发)；不配=profile None → 保守 170K token 硬阈值。
        # 注意这是"告诉库窗口多大"的声明值，不是网关参数——填模型真实窗口。
        self.max_input_tokens = max_input_tokens
        self.throttle = LlmThrottle(max_concurrent, min_interval_ms / 1000.0)
        self.stats = {"calls": 0, "throttled_429": 0, "waited_total": 0.0, "failures": 0}
        self._fail_streak = 0
        self._circuit_until = 0.0
        # ── 11-25 配额网格（定版：TPM=分钟窗，429 语义是"配额用完"非"模型坏了"）──
        # _ep={"anchor":绝对时刻,"stage":已过档数,"next":下个探测时刻,"state":throttled|exhausted}
        # 网格点=anchor+backoff[stage]（默认 60/90/120，绝对时刻非累计睡眠→单请求
        # 最多被扣到 anchor+120）。全池共享：所有请求查同一本账，晚归的在飞请求
        # 不再各自为政烧 30/60/120 阶梯。试尽→exhausted：允许改道备胎，每
        # quota_probe_after_exhaust 秒开一个探测窗，真流量撞窗即探针，成功即全场回血。
        self._ep = None
        self._ep_lock = threading.Lock()
        self._billing_until = 0.0   # 402=账号级：充值前切过去只会送死，长熔断

    def circuit_open(self, now: float = None) -> bool:
        now = now if now is not None else time.monotonic()
        return self._circuit_until > now

    def record_failure(self, counts_only: bool = False) -> None:
        """counts_only：只记账不推熔断——429 配额抖专用（11-25 网格接管其行为；
        熔断继续为 5xx/连接类硬故障服务，语义不混）。"""
        self._fail_streak += 1
        self.stats["failures"] += 1
        if counts_only:
            return
        threshold = int(get_cfg("llm.circuit_threshold", 3))
        if self._fail_streak >= threshold:
            secs = int(get_cfg("llm.circuit_cooldown", 120))
            self._circuit_until = time.monotonic() + secs

    def record_success(self) -> None:
        self._fail_streak = 0
        self._circuit_until = 0.0
        self._ep = None          # 配额网格：任一成功=窗口已过，清零回血
        self._billing_until = 0.0

    # ── 配额网格 API（11-25）────────────────────────────
    def _grid(self) -> list:
        return backoff_schedule()

    def note_429(self, now: float = None) -> None:
        """记一枪 429。只有"探测点上的失败"才推档；hold 期间晚归的在飞请求
        失败不推档（同一窗口内的重复证据，推档会被并发请求瞬间打满）。"""
        if now is None:
            now = time.monotonic()
        g = self._grid()
        probe_gap = float(get_cfg("llm.quota_probe_after_exhaust", 120))
        with self._ep_lock:
            ep = self._ep
            if ep is None:
                self._ep = {"anchor": now, "stage": 0,
                            "next": now + (g[0] if g else 60.0), "state": "throttled"}
                return
            if now >= ep["next"] - 1.0:            # 这枪发生在探测窗 = 有效证据
                if ep["state"] == "exhausted":
                    ep["next"] = now + probe_gap   # 探测窗未过：下个周期再来
                    return
                ep["stage"] += 1
                if ep["stage"] >= len(g):
                    ep["state"] = "exhausted"
                    ep["next"] = now + probe_gap
                else:
                    ep["next"] = ep["anchor"] + g[ep["stage"]]
            # else: hold 期内晚归失败——账已记（episode 在），不推档不改 next

    def note_billing(self) -> None:
        """402 欠费：账号级不可用，冷却到下个充值探测（默认 30min）。"""
        self._billing_until = time.monotonic() + float(get_cfg("llm.billing_cooldown", 1800))

    def episode_state(self) -> str:
        ep = self._ep
        return ep["state"] if ep else ""

    def hold_seconds(self, now: float = None) -> float:
        """离本槽下个可用时刻还差几秒（0=现在可发/探测窗开着）。"""
        if now is None:
            now = time.monotonic()
        h = 0.0
        if self._ep and now < self._ep["next"]:
            h = self._ep["next"] - now
        if self._billing_until > now:
            h = max(h, self._billing_until - now)
        return h

    def quota_snapshot(self) -> dict:
        ep = self._ep or {}
        return {"episode": ep.get("state") or None, "stage": ep.get("stage"),
                "next_probe_in": round(self.hold_seconds(), 1),
                "billing_block_in": round(max(0.0, self._billing_until - time.monotonic()), 1)}

    def snapshot(self) -> dict:
        return {
            "id": self.id, "provider": self.provider, "model": self.model,
            "base_url": self.base_url, "enabled": self.enabled, "priority": self.priority,
            "circuit_state": "open" if self.circuit_open() else "closed",
            "fail_streak": self._fail_streak,
            "quota": self.quota_snapshot(),
            **self.stats,
        }


# ==================== 协议适配层 ====================
def build_base_llm(slot: ModelSlot, *, temperature: float, max_tokens: Optional[int]):
    """按 provider 协议构造底层客户端（加新协议 = 加一个分支）。"""
    provider = (slot.provider or "openai_compatible").lower()
    if provider in ("openai_compatible", "openai", "compatible"):
        from langchain_openai import ChatOpenAI
        kwargs: dict = dict(
            model=slot.model, api_key=slot.api_key, base_url=slot.base_url or None,
            temperature=temperature,
            # 思考型模型（MiniMax M3 / Qwen3.8）reasoning 计入输出预算：
            # 用 max_tokens 会让 content 空（2026-10-21 实测：600 空 / 2048 正常）
            max_completion_tokens=max_tokens,
            max_retries=0,          # 不变式③：SDK 自带重试关掉，我们独管
            # W3(11-24)：显式读超时——网关"完全不滴字"挂相（真断流/半开连接）
            # 靠它抛 APITimeoutError 走瞬时错误切备胎；keep-alive 假滴字型
            # 挂起它管不住，归看门狗 stall 检测兜（勿在此加大数值）
            timeout=float(get_cfg("llm.request_timeout", 900)),
        )
        # 11-23 窗口声明（每 slot 独立配置）：配了 max_input_tokens 才传 profile，
        # deepagents 摘要阈值即从保守 170K 硬阈值切到 fraction(0.85×窗口)；
        # 不配 = profile None = 维持旧行为，零波及
        if getattr(slot, "max_input_tokens", None):
            kwargs["profile"] = {"max_input_tokens": int(slot.max_input_tokens)}
        return ChatOpenAI(**kwargs)
    if provider in ("anthropic", "claude"):
        from langchain_anthropic import ChatAnthropic
        # 11-23 统一口径：不传=不发送该参数、吃网关默认（与 openai 分支同语义；
        # 原 `or 4096` 会把"没传"变成"传小值"，正是当天拆掉 65536 兜底的同病）
        return ChatAnthropic(
            model=slot.model, api_key=slot.api_key,
            base_url=slot.base_url or None, temperature=temperature,
            max_tokens=max_tokens, max_retries=0,
            timeout=float(get_cfg("llm.request_timeout", 900)),   # 同 W3 口径
        )
    if provider in ("google_genai", "gemini", "google"):
        # 现池无调用方；timeout 透传参数名待真实接入时实测再补（11-24 注）
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=slot.model, google_api_key=slot.api_key, temperature=temperature,
            max_output_tokens=max_tokens, max_retries=0,
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
    profile: Any = None              # 11-23 断链修复：抄自 base_model.profile——
                                     # deepagents 工厂/预算闸读的是包装层，不转发则
                                     # max_input_tokens 配置对 agent 会话永远不可见
    task_id: Optional[int] = None      # 非 None=编译任务专属实例（账按 model 分桶）
    task_label: str = ""               # 11-25 日志归因：T-xxx 业务号（""=非任务流量）
    call_seq: int = 0                  # 本实例第几枪 LLM 调用（日志用）
    acc: dict = {}                     # {model_id: {calls,in,out,rea,cache,sec,wait_s,zero_streak}}
    _wait_base: dict = {}              # id(throttle)->上次 waited_total 基线（增量归属用）

    @property
    def _llm_type(self) -> str:
        return "throttled-chat-model"

    # ── 用量账（11-23 设计档 token-stats-design.md）──────────────────
    # 只记 task_id 实例（=compile.stage5）：qa/exp/ticket 不入账（用户拍板）。
    # 池级聚合不在内存做——任务行都在库里，一句 SQL 的事（KISS）。
    # 采集在四个入口成功路径；失败调用无 usage 可记（协议如此）。
    def _llm_log(self, slot: "ModelSlot", msg: str, level: str = "info") -> None:
        """11-25 调用归因日志（定版字段序）：来源列=logger 名 app.llm，
        消息体 [T-xxx|-] site=… slot=… #枪号 + 事件——时间戳/级别由 formatter
        供给，[llm] 旧标记随三通道统一去重。grep 业务号一铲到底；
        httpx/httpx2 匿名行已降 WARNING，本行是 LLM 流量唯一现场记录。"""
        import logging as _lg
        _lg.getLogger("app.llm").log(
            _lg.INFO if level == "info" else _lg.WARNING,
            "[%s] site=%s slot=%s #%d %s",
            self.task_label or "-", self.call_site,
            getattr(slot, "id", "?"), self.call_seq, msg)

    def _record_usage(self, resp, seconds: float) -> None:
        if self.task_id is None:
            return
        um = None
        try:
            if isinstance(resp, ChatResult) and resp.generations:
                um = getattr(resp.generations[0].message, "usage_metadata", None)
            elif isinstance(resp, (list, tuple)):        # consume=True 的 chunk 列表
                for c in reversed(list(resp)):
                    um = getattr(c, "usage_metadata", None)
                    if um:
                        break
            else:
                um = getattr(resp, "usage_metadata", None)
        except Exception:
            um = None
        um = dict(um or {})
        det = dict(um.get("output_token_details") or {})
        ind = dict(um.get("input_token_details") or {})
        row = dict(calls=1,
                   inp=int(um.get("input_tokens", 0) or 0),
                   out=int(um.get("output_tokens", 0) or 0),
                   rea=int(det.get("reasoning", 0) or 0),
                   cache=int(ind.get("cache_read", 0) or 0))
        mid = getattr(self.slot, "id", "?")
        # 节流等待：闸内 waited_total 是全局累计，任务只取"自上次记账以来的增量"
        wait_delta = 0.0
        try:
            tw = float(self.slot.throttle.stats["waited_total"])
            key = id(self.slot.throttle)
            wait_delta = max(0.0, tw - self._wait_base.get(key, tw))
            self._wait_base[key] = tw
        except Exception:
            pass
        with _ACC_LOCK:
            t = self.acc.setdefault(mid, dict(calls=0, inp=0, out=0, rea=0, cache=0,
                                              sec=0.0, wait_s=0.0, zero_streak=0))
            for k in ("calls", "inp", "out", "rea", "cache"):
                t[k] += row[k]
            t["sec"] += seconds
            t["wait_s"] += max(0.0, wait_delta)
            # 连续零 usage 计数（黄标判据：网关不回账时防"没数"被读成"没花钱"）
            t["zero_streak"] = t["zero_streak"] + 1 if (row["inp"] == 0 and row["out"] == 0) else 0

    def task_stats_snapshot(self) -> dict:
        """引擎 flush 用：{model_id: row副本}（wait 基线已消费式更新，可重复取）"""
        with _ACC_LOCK:
            return {k: dict(v) for k, v in self.acc.items()}

    # ── 核心：过闸 + 退避 + 兜底 ──
    def _decide_after_429(self, e: Exception, slot: ModelSlot, st: dict):
        """一次尝试 429 后的统一决策（同步/异步/流式三路共用，防语义漂移）。

        st 原地更新 {"waited": float, "attempts": int}。
        返回 ("switch", 新slot)（退避耗尽/超上限但有备用）| ("sleep", 秒数)（本模型继续退避）。
        放弃时抛 LlmThrottledError。
        """
        slot.stats["throttled_429"] += 1
        slot.throttle.stats["throttled"] += 1
        slot.record_failure(counts_only=True)   # 429 不推熔断——行为归配额网格管
        _st_before = slot.episode_state()
        slot.note_429()                          # 11-25：全池共享的探测网格
        _st_after = slot.episode_state()
        st["attempts"] += 1
        if not _st_before:
            _tail = " · 网格开档 探测+60s"
        elif _st_after == "exhausted" and _st_before != "exhausted":
            _tail = " · 网格试尽(exhausted)"
        elif _st_before == _st_after:
            _tail = " · 窗内重复证据(不推档)"
        else:
            _tail = ""
        self._llm_log(slot, f"→ 429 第{st['attempts']}次尝试受挫" + _tail, "warning")
        nxt = self._next_untried_slot(slot, st)
        ra = retry_after_seconds(e)
        hold = slot.hold_seconds()
        if ra:
            hold = max(hold, float(ra))          # 网关明说等待时长 → 听网关的
        if st["attempts"] > self.throttle_retries:
            # 单请求有界（任务端点不可无限延迟）：本请求也烧满重试数时，
            # 有备胎改道、无备胎如实抛——网格管全场节奏，此账管这一枪寿命
            if nxt is not None:
                self._llm_log(slot, f"单请求烧满 → 改道 {slot.id}→{nxt.id}", "warning")
                return ("switch", nxt)
            self._llm_log(slot, "单请求烧满且无备胎 → 抛限流(网格留账,后续继续探针)", "warning")
            raise LlmThrottledError(
                THROTTLE_MSG, waited=st["waited"], attempts=st["attempts"],
                call_site=self.call_site, slot_id=slot.id) from e
        if slot.episode_state() == "exhausted" and nxt is not None:
            # 主力试尽（anchor+120 网格走满仍 429=不是窗口抖）→ 改道备胎
            self._llm_log(slot, f"网格试尽 → 改道 {slot.id}→{nxt.id}", "warning")
            return ("switch", nxt)
        if st["waited"] + hold > self.throttle_max_wait:
            if nxt is not None:
                self._llm_log(slot, f"hold {hold:.0f}s 超上限 → 改道 {nxt.id}", "warning")
                return ("switch", nxt)
            raise LlmThrottledError(
                THROTTLE_MSG, waited=st["waited"], attempts=st["attempts"],
                call_site=self.call_site, slot_id=slot.id) from e
        self._llm_log(slot, f"退避：睡 {hold:.0f}s 待下个网格探测点", "warning")
        return ("sleep", hold)

    def _call_with_throttle(self, fn, slot: ModelSlot, consume: bool = False):
        """fn() 发一次真实请求。consume=True 时 fn 返回生成器——在本层消费成 list
        再返回（流式 429 藏在 generator 迭代里，建连阶段拦不住，11-03 T2 实测）。
        平台无 token 级 UI（log 按 node 边界整段渲染），消费完整样返回。"""
        st = {"waited": 0.0, "attempts": 0}
        _t0 = time.monotonic()   # 用量账计时起点（含闸内等待，wait_s 另有单列）
        _pre = slot.hold_seconds()   # 11-25 定版：主力 throttled 期新请求先睡到探测点
        if _pre > 0:               # （共享账，晚到的请求与在飞请求同点汇合）
            time.sleep(_pre); st["waited"] += _pre
        _pre_logged = False
        while True:
            slot.throttle.acquire()
            slot.stats["calls"] += 1
            self.call_seq += 1
            if not _pre_logged:
                _pre_logged = True
                self._llm_log(slot, "start" if _pre <= 0 else f"start(共享网格 pre-hold {_pre:.0f}s 至探测点)")
            _had_ep = slot.episode_state()      # 成功行要能说"探针终结了 episode"
            try:
                out = fn()
                if consume:
                    # 11-24：假滴字解药——只有已解析 chunk 重置业务计时，
                    # 超 llm.stream_idle_seconds 无块 → LlmStreamIdleTimeout
                    # → 下方瞬时分流自动切槽重发（consume 全量缓冲，重发无半截风险）
                    out = consume_stream_guarded(
                        out, float(get_cfg("llm.stream_idle_seconds", 600)),
                        call_site=self.call_site, slot_id=slot.id)
                slot.record_success()
                self._llm_log(slot, f"→ 200 {time.monotonic() - _t0:.1f}s"
                              + (" · 探针成功网格清零" if _had_ep else ""))
                slot.throttle.release()   # 成功也要释放：槽只在"在飞"期间占用
                self._record_usage(out, time.monotonic() - _t0)   # 11-23 用量账
                if consume:
                    return iter(out)
                return out
            except Exception as e:
                if not is_rate_limited(e):
                    slot.record_failure()
                    _sc = getattr(e, "status_code", None)
                    if _sc:
                        self._llm_log(slot, f"→ HTTP {_sc} ({type(e).__name__})", "warning")
                    if _sc == 402:
                        slot.note_billing()   # 11-25：欠费=账号级，30min 内不再送人头
                        self._llm_log(slot, "402 欠费 → 该槽拉黑 30min(选槽/改道均绕开)", "warning")
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
                _hold = slot.hold_seconds()
                if _hold > 0:   # 网格：睡到本槽下个探测点（全池共享，不再各烧各的）
                    time.sleep(_hold); st["waited"] += _hold

    async def _call_with_throttle_async(self, fn, slot: ModelSlot, consume: bool = False):
        """异步路径的过闸+退避（reserve 纯计算 + asyncio.sleep，绝不阻塞事件循环）。
        429 决策复用 _decide_after_429（与同步/流式同一状态机，语义不漂移）。"""
        st = {"waited": 0.0, "attempts": 0}
        _t0 = time.monotonic()   # 用量账计时起点（异步路 caller 现无，账备而路通）
        _pre = slot.hold_seconds()   # 11-25 定版：主力 throttled 期新请求先睡到探测点
        if _pre > 0:               # （共享账，与在飞请求同点汇合；asyncio 不阻塞事件循环）
            await asyncio.sleep(_pre); st["waited"] += _pre
        _pre_logged = False
        while True:
            wait = slot.throttle.reserve()          # 预约放行时刻（纯计算，不阻塞）
            if wait > 0:
                await asyncio.sleep(wait)           # A1 修复：预约了就得等，否则最小间隔形同虚设
            slot.stats["calls"] += 1
            self.call_seq += 1
            if not _pre_logged:
                _pre_logged = True
                self._llm_log(slot, "start" if _pre <= 0 else f"start(共享网格 pre-hold {_pre:.0f}s 至探测点)")
            _had_ep = slot.episode_state()
            try:
                out = await fn()
                if consume:
                    out = await consume_stream_guarded_async(
                        out, float(get_cfg("llm.stream_idle_seconds", 600)),
                        call_site=self.call_site, slot_id=slot.id)
                slot.record_success()
                self._llm_log(slot, f"→ 200 {time.monotonic() - _t0:.1f}s"
                              + (" · 探针成功网格清零" if _had_ep else ""))
                self._record_usage(out, time.monotonic() - _t0)   # 11-23 用量账
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
                _hold = slot.hold_seconds()
                if _hold > 0:   # 11-25 网格：睡到共享探测点（异步路同款语义）
                    await asyncio.sleep(_hold); st["waited"] += _hold

    def _next_slot(self, current: ModelSlot):
        """池内下一个可用模型（跳过 disabled/熔断中的）。"""
        for s in sorted(self.fallback_slots or [], key=lambda x: x.priority):
            if s is current or not s.enabled or s.circuit_open() \
               or s._billing_until > time.monotonic():   # 402 账号级黑名单（11-25）
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
            if s is current or s.id in tried or not s.enabled or s.circuit_open() \
               or s._billing_until > time.monotonic():   # 402 黑名单（11-25）
                continue
            return s
        return None

    def _switch_slot(self, nxt: ModelSlot) -> None:
        """切换到备用模型（重建底层客户端；profile 声明随新家窗口重抄，11-23）。"""
        self.base_model = build_base_llm(nxt, temperature=self.temperature, max_tokens=self.max_tokens)
        self.profile = getattr(self.base_model, "profile", None)
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
_ACC_LOCK = threading.Lock()   # 11-23 用量账 acc 跨线程保护（并行任务/流式消费）


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
                    max_input_tokens=int(v) if (v := m.get("max_input_tokens")) else None,
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
                    max_input_tokens=int(os.environ["AI_MAX_INPUT_TOKENS"])
                    if os.environ.get("AI_MAX_INPUT_TOKENS", "").isdigit() else None,
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
def consume_stream_guarded(it, idle_seconds: float, *, call_site: str = "", slot_id: str = "") -> list:
    """把流迭代器消费成 list，带业务空闲超时（11-24 假滴字挂起解药）。

    feeder 线程拉底层 next() → Queue(maxsize=1)；主线程 get(timeout=idle)。
    只有能成为 chunk 的字节才重置计时；idle 秒无 chunk → LlmStreamIdleTimeout。
    finally 置 stop + close 底层生成器，feeder 不死挂（daemon + put 带超时轮询）。
    底层异常原样上抛（保留 openai 异常类型给 429/瞬时分流，语义不降级）。
    """
    import queue as _q
    box = _q.Queue(maxsize=1)
    stop = threading.Event()

    def _feeder():
        try:
            for c in it:
                while not stop.is_set():
                    try:
                        box.put(("c", c), timeout=0.5)
                        break
                    except _q.Full:
                        pass
                if stop.is_set():
                    return
            if not stop.is_set():
                box.put(("d", None), timeout=2)
        except BaseException as e:  # noqa: BLE001  # 底层异常交主线程原样重抛
            if not stop.is_set():
                try:
                    box.put(("e", e), timeout=2)
                except Exception:
                    pass

    th = threading.Thread(target=_feeder, daemon=True)
    th.start()
    out: list = []
    try:
        while True:
            try:
                kind, payload = box.get(timeout=idle_seconds)
            except _q.Empty:
                raise LlmStreamIdleTimeout(
                    f"LLM 流空闲超时：{idle_seconds:.0f}s 无新 chunk（疑似网关假滴字挂起）"
                    f" site={call_site} slot={slot_id}",
                    idle_seconds=idle_seconds, call_site=call_site, slot_id=slot_id)
            if kind == "d":
                return out
            if kind == "e":
                raise payload
            out.append(payload)
    finally:
        stop.set()
        try:
            it.close()   # 正常/异常收尾关流；注意：若底层正卡在 next()（假滴字本尊），
        except Exception:  # close 抛 ValueError 被吞——feeder 随 daemon 存活到进程级回收，
            pass         # 单次超时漏一条 socket，重试上限封顶（11-24 已知边界，如实记）


async def consume_stream_guarded_async(ait, idle_seconds: float, *,
                                       call_site: str = "", slot_id: str = "") -> list:
    """异步同款：wait_for 包 __anext__（超时自动 cancel 内部挂起点）。"""
    out: list = []
    try:
        while True:
            try:
                c = await asyncio.wait_for(ait.__anext__(), timeout=idle_seconds)
            except asyncio.TimeoutError:
                raise LlmStreamIdleTimeout(
                    f"LLM 流空闲超时(异步)：{idle_seconds:.0f}s 无新 chunk"
                    f" site={call_site} slot={slot_id}",
                    idle_seconds=idle_seconds, call_site=call_site, slot_id=slot_id)
            except StopAsyncIteration:
                return out
            out.append(c)
    finally:
        try:
            await ait.aclose()
        except Exception:
            pass


def _pick_slot(slots: list, *, exclude_ids: "set | None" = None,
               now: float = None) -> "ModelSlot | None":
    """11-25 网格选槽（定版语义：撞 429 后新请求【hold 在主力】，试尽才改道）。

    按 priority 升序逐家过堂：
      就绪(hold=0) → 取之（主力探测窗点自动夺回：探针即真请求，成功即全场回血）
      限流中(throttled) → 仍取之——调用方 pre-hold 睡到探测点，不换家
      试尽(exhausted)/熔断/402 黑名单 → 才轮到下一家
    全不可选 → enabled 最低 priority 兜底（调用方自行退避报错）。"""
    if now is None:
        now = time.monotonic()
    excl = exclude_ids or set()
    for x in sorted(slots, key=lambda s: s.priority):
        if not x.enabled or x.id in excl or x.circuit_open(now) \
           or x._billing_until > now:
            continue
        if x.hold_seconds(now) == 0 or x.episode_state() == "throttled":
            return x
    cand = [x for x in sorted(slots, key=lambda s: s.priority) if x.enabled]
    return cand[0] if cand else None


def make_llm(*, temperature: float = 0.1, max_tokens: Optional[int] = None,
             call_site: str = "", prefer: Optional[str] = None,
             model_id: Optional[str] = None, task_id: Optional[int] = None,
             task_label: str = "") -> ThrottledChatModel:
    """所有 LLM 调用的唯一入口：过闸 + 退避 + 池内兜底。

    task_id（11-23 用量账）：仅 compile.stage5 传——实例=任务（每任务新建），
    该实例全部成功调用的 usage 计入 per-task 账；其余调用点不传=只进池级。"""
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
        chosen = _pick_slot(slots)
    base = build_base_llm(chosen, temperature=temperature, max_tokens=max_tokens)
    return ThrottledChatModel(
        base_model=base, slot=chosen, call_site=call_site,
        throttle_retries=int(get_cfg("llm.throttle_retries", 3)),
        throttle_max_wait=float(get_cfg("llm.throttle_max_wait", 300)),
        throttle_backoff=backoff_schedule(),
        fallback_slots=slots,
        temperature=temperature, max_tokens=max_tokens,
        profile=getattr(base, "profile", None),
        task_id=task_id,
        task_label=task_label,
    )


def llm_stats() -> dict:
    """池状态快照（admin /api/admin/llm/stats 用）。"""
    from app.services import agent_compat
    slots = load_model_slots()
    return {
        "slots": [s.snapshot() for s in slots],
        "agent_obs": agent_compat.stats(),   # 11-23 自愈事件（溢出/压缩 offload）
        "global": {
            "total_calls": sum(s.stats["calls"] for s in slots),
            "total_throttled": sum(s.stats["throttled_429"] for s in slots),
            "total_waited": round(sum(s.stats["waited_total"] for s in slots), 1),
            "total_failures": sum(s.stats["failures"] for s in slots),
        },
    }
