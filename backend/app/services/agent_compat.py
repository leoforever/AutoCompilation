"""deepagents 适配补丁（11-23 上下文超限治理 + 自愈事件观测）。

平台唯一允许碰第三方库行为的地方——集中此处、可整体摘除，理由各附案号：

1. 溢出识别 marker 追加（install）
   通义网关上下文超限回包文案是 "Range of input length should be
   [1, N]"，不在 deepagents _is_context_overflow 的 13 条 marker 名单——
   真实事故 2026-11-23：MySQL 编译任务 stage5 撞网关硬墙 400，
   deepagents 现成的"裁尾重试"兜底因不识别而装死，400 炸穿任务 failed。
   本补丁把该文案追加进识别面（原逻辑命中仍走原路，纯增量），
   并在此咽喉点记事件日志+计数——自愈机制此前完全静默。

2. 观测提级过滤器（install 内第二段）
   deepagents 的压缩/offload 关键痕迹是 DEBUG 级，root=INFO 全吞，
   "这单被压缩过几次"事后无从考证。给该库 logger 开 DEBUG 采集并挂
   白名单 Filter：INFO+ 照常、DEBUG 只放行关键事件（Offloaded/summariz/
   truncate 等），其余 debug 噪音丢弃——不污染日志量。
"""
import logging

_log = logging.getLogger("agent_compat")
_PATCHED = False

# 平台侧要认的上下文超限文案（除 deepagents 自带名单外）
_EXTRA_MARKERS = (
    "range of input length",      # 通义 qwen token-plan 网关（MY-2026-11-23）
)

# DEBUG 提级白名单（子串，小写匹配）：压缩/offload/截断/裁尾类事件
_PROMOTE_PATTERNS = ("offloaded", "summariz", "truncat", "compact", "clip", "overflow")

# 池级自愈事件计数（内存；llm_stats() 透出。按任务的 DB 归属列被裁——只为展示
# 而不展示的列不加，需要时再议）
OBS = {"overflow_events": 0, "summarizations": 0}


def stats() -> dict:
    return dict(OBS, patched=_PATCHED)


class _PromoteFilter(logging.Filter):
    """root=INFO 前提下放行库的关键 DEBUG 事件，其余 debug 丢弃。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.INFO:
            return True
        text = record.getMessage().lower()
        hit = any(p in text for p in _PROMOTE_PATTERNS)
        if hit and "offloaded" in text:
            OBS["summarizations"] += 1
        return hit


def install() -> None:
    """幂等：marker 补丁 + 观测提级。须在首个 create_deep_agent 前（lifespan 调）。

    模块全局查找生效已实测：summarization.py 四处调用均为 `_is_context_overflow(exc)`
    全局名引用，替换模块属性即全覆盖（含异步路）。"""
    global _PATCHED
    if _PATCHED:
        return
    try:
        import deepagents.middleware.summarization as _sm
        orig = _sm._is_context_overflow

        def patched(exc):
            if orig(exc):
                OBS["overflow_events"] += 1
                return True
            text = str(exc).lower()
            if any(m in text for m in _EXTRA_MARKERS):
                OBS["overflow_events"] += 1
                _log.info("[agent_compat] 识别网关上下文硬墙 400（Range of input "
                          "length）→ 进入 deepagents 裁尾恢复（原样炸穿已防）")
                return True
            return False

        _sm._is_context_overflow = patched

        # O4：恢复耗尽的异常文案补处置指引（原句不含原因/动作，用户看不懂）
        # 注意：staticmethod 经 Cls.attr 访问会被解包——判类型必须走 __dict__
        _md = _sm._DeepAgentsSummarizationMiddleware
        _raw = _md.__dict__.get("_raise_recovery_exhausted")
        if isinstance(_raw, staticmethod):
            def _raise_exhausted(error, _orig=_raw.__func__):
                try:
                    _orig(error)
                except Exception as e:
                    e.args = (str(e) + "（触发原因：上下文超模型窗口且裁尾后仍超；"
                              "处置：调小 llm.models.max_input_tokens 或减少单次读入的大文件）",)
                    raise
            _md._raise_recovery_exhausted = staticmethod(_raise_exhausted)  # type: ignore[method-assign]

        # 观测提级：库 logger 开 DEBUG 采集，Filter 白名单放行关键事件
        lib = logging.getLogger("deepagents.middleware.summarization")
        lib.setLevel(logging.DEBUG)
        lib.addFilter(_PromoteFilter())
        _PATCHED = True
        _log.info("[agent_compat] 补丁已装（+%d 条网关文案；压缩/offload 事件提级观测）",
                  len(_EXTRA_MARKERS))
    except Exception as e:  # deepagents 结构升级失配 → 降级为不补丁，不拖垮启动
        _log.warning("[agent_compat] 补丁安装失败（降级裸跑）: %s", e)
