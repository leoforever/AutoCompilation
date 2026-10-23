"""stage5 收尾闸门（docs/stage5-verify-discipline-v2.md）

背景（801346 完整日志校准）：验证段合法部分 16min，此后出血来自"全绿后推翻考卷重做"
（6min/11 calls）等机制。原则：提示词管动机，程序管赖着不走——检测首次验证全绿
（ToolMessage 里 `RESULT pass=N fail=0`）后给收尾预算，超预算的 execute 工具调用
被拒答（返回引导性错误文本，agent 自然转向写 NOTES 收尾），不中断 stream、不改判成败。

两档模式（stage5.wrapup_enforce）：
  false（观测期默认）= 只记日志/统计，不拒答——用真实任务确认预算不误伤后开启
  true = 超限拒答
宽容带：拒答后再放行 post_green_grace_execs 次 execute（归档确实可能差一条命令）；
write_file/read_file/edit 永不计数永不拒（BUILD_NOTES 永远写得动）。
"""
import logging
import re
import time

from langchain.agents.middleware import AgentMiddleware

_log = logging.getLogger("green_gate")

# 机读全绿行：`RESULT pass=22 fail=0`（前后可有 ===/其它文本）
GREEN_RE = re.compile(r"RESULT\s+pass=(\d+)\s+fail=(\d+)")


class GreenGateMiddleware(AgentMiddleware):
    """全绿后收尾预算闸。挂在 create_deep_agent(middleware=[...]) 上；
    引擎侧同实例的 on_tool_message() 负责喂 ToolMessage 做全绿检测。"""

    def __init__(self, state: dict, *, enforce: bool, max_seconds: int,
                 max_execs: int, grace_execs: int = 5, on_log=None):
        self.state = state
        self.enforce = enforce
        self.max_seconds = max_seconds
        self.max_execs = max_execs
        self.grace_execs = grace_execs
        self.on_log = on_log
        self.green_ts: float | None = None   # 首次全绿时刻
        self.execs_after_green = 0
        self.denied = 0

    # ---- 检测侧（引擎循环喂 ToolMessage 文本） ----
    def scan_tool_text(self, text: str) -> None:
        if self.green_ts is not None:
            return  # 只认首次全绿
        m = GREEN_RE.search(text or "")
        if m and m.group(2) == "0" and int(m.group(1)) > 0:
            self.green_ts = time.time()
            self._emit("info", f"[阶段5] ✅ 检测到验证全绿（pass={m.group(1)}），进入收尾预算期"
                              f"（{self.max_seconds}s / {self.max_execs} execs"
                              f"{'，闸门生效' if self.enforce else '，观测模式：只记不拦'}）")

    # ---- 拦截侧（wrap_tool_call 必须 return call(request)——deepagents 铁律） ----
    def wrap_tool_call(self, request, call):
        tc = getattr(request, "tool_call", None) or {}
        tool_name = (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")) or ""
        if self.green_ts is None or tool_name not in ("execute", "bash", "shell"):
            return call(request)
        self.execs_after_green += 1
        elapsed = time.time() - self.green_ts
        over = elapsed > self.max_seconds or self.execs_after_green > self.max_execs
        if over:
            self.denied += 1
            _args = (tc.get("args") if isinstance(tc, dict) else None) or {}
            self._emit("warn", f"[阶段5] ⏳ 收尾超限 exec#{self.execs_after_green} "
                              f"(elapsed={elapsed:.0f}s, enforce={self.enforce}): "
                              f"{str(_args.get('command', ''))[:80]}")
            if self.enforce and self.denied > self.grace_execs:
                from langchain_core.messages import ToolMessage
                return ToolMessage(
                    content=(
                        "⏹ 收尾预算已用尽（全绿后超过时间/次数闸门）。验证与编译到此为止。"
                        "现在只做三件事：1) 归档现有产物到 /work/output（不重编不重验）；"
                        "2) 按模板补全 /work/output/BUILD_NOTES.md——已完成项如实写，"
                        "没测的检查写进『建议深挖项』或『已知限制』，不补测；"
                        "3) 输出最终摘要。禁止重跑验证脚本、禁止新路线、禁止深挖。"
                    ),
                    tool_call_id=tc.get("id", "") if isinstance(tc, dict) else "",
                    name=tool_name,
                    status="error",
                )
        return call(request)

    def _emit(self, level: str, msg: str) -> None:
        getattr(_log, level, _log.info)(msg)
        try:
            if self.on_log:
                self.on_log(level.upper(), msg)
        except Exception:
            pass
