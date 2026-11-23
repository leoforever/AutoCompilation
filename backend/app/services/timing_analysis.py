"""任务归因分析（11-20 方案2）：全 log 时间轴的确定性解析，产 ~3KB 摘要卡。

设计立场：解析器看全文、模型看结论——20 万字全喂又贵又吵，而"时间浪费在哪"
需要的恰恰是锚点抽段丢掉的全时间轴。纯字符串处理：无模型、无执行、无 DB。

输入格式=平台 build log 实录（阶段行 [阶段N] 完成 (耗时 Xs)；工具行
[+Ns] ▶[tag]/✓[tag]/❌[tag]；心跳行 静默 Ns 后恢复；总结行 各阶段耗时）。
"""
import re

_TS = re.compile(r"^\[\d\d-\d\d [\d:]+\] \[\w+\] ")
_ELAP = re.compile(r"\[\+(\d+(?:\.\d+)?)s\] ")
_STAGE_DONE = re.compile(r"\[(阶段[\w-]+)\] 完成 \(耗时 ([\d.]+)s\)")
_HB = re.compile(r"静默 (\d+)s 后恢复")
_ERR = re.compile(r"(❌|Command failed|exit=(?!0)|Traceback|error:|Error:|FAILED)", re.I)
_TOOL = re.compile(r"([▶✓❌💭])\[(\w+)\]\s?(.*)", re.S)
_SUMMARY = re.compile(r"各阶段耗时: (.+)$")


def analyze_timing(log_text: str) -> str:
    """全 log → 归因摘要卡（阶段账/时间去向/重复模式/错误清单/心跳长块）。"""
    lines = log_text.splitlines()
    stages: list[tuple[str, float]] = []
    hb_silence = 0.0                     # 心跳补发的静默总时长（=长命令总时长）
    calls: dict[str, tuple[float, str]] = {}   # tag -> (起始elapsed, 命令摘录)
    done_calls: list[tuple[float, float, str, bool]] = []  # (耗时, 起, 命令, 是否失败)
    errors: list[tuple[int, str]] = []
    last_elap = 0.0
    for idx, ln in enumerate(lines, 1):
        m = _STAGE_DONE.search(ln)
        if m:
            stages.append((m.group(1), float(m.group(2))))
        for h in _HB.finditer(ln):
            hb_silence += float(h.group(1))
        e = _ELAP.search(ln)
        el = float(e.group(1)) if e else None
        if el is not None:
            last_elap = max(last_elap, el)
        t = _TOOL.search(_TS.sub("", ln)) if _TS.match(ln) else None
        if t and el is not None:
            mark, tag, body = t.group(1), t.group(2), t.group(3)[:90].replace("\n", "⏎")
            if mark == "▶":
                calls[tag] = (el, body)
            elif mark in ("✓", "❌") and tag in calls:
                start, cmd = calls.pop(tag)
                done_calls.append((el - start, start, cmd, mark == "❌"))
        if _ERR.search(ln) and len(errors) < 40:
            errors.append((idx, ln.strip()[:200]))
    sm = _SUMMARY.search(log_text)

    out = []
    # 1) 阶段账（总结行优先——阶段"完成"行在 stage 内嵌打点，两处取全）
    if sm:
        out.append("【阶段耗时(任务总结行)】 " + sm.group(1)[:400])
    elif stages:
        out.append("【阶段耗时】 " + "  ".join(f"{n}={s:.0f}s" for n, s in stages))
    # 2) 时间去向 top10（配对成功的调用）
    if done_calls:
        top = sorted(done_calls, key=lambda x: -x[0])[:10]
        out.append("【工具调用耗时 Top10（配对 ▶→✓/❌）】")
        for dur, st, cmd, failed in top:
            out.append(f"  {dur:7.0f}s  @{st:7.0f}s  {'❌' if failed else '✓'}  {cmd[:80]}")
        acct = sum(d for d, *_ in done_calls)
        out.append(f"  （配对调用 {len(done_calls)} 条合计 {acct:.0f}s；"
                   f"心跳静默合计 {hb_silence:.0f}s——长命令多计入此项）")
    # 3) 重复命令模式（同摘录 ≥2 次=重跑/重试信号）
    from collections import Counter
    norm = lambda c: re.sub(r"\s+", " ", c)[:50]
    rep = Counter(norm(c[2]) for c in done_calls)
    reps = [(n, c) for c, n in rep.items() if n >= 2]
    if reps:
        out.append("【重复执行的命令（≥2 次，疑似试错/重跑）】")
        for n, c in sorted(reps, key=lambda x: -x[0])[:5]:
            out.append(f"  ×{n}  {c}")
    # 4) 错误行清单
    if errors:
        out.append(f"【错误/告警行（{len(errors)} 条，截前 15）】")
        for i, ln in errors[:15]:
            out.append(f"  L{i}: {ln[:160]}")
    # 5) AI 思考抽样里含"失败/为什么/改"的决策句（归因语义最浓的一手材料）
    thinks = [re.sub(_TS, "", l) for l in lines if "💭 AI:" in l]
    hot = [t for t in thinks if re.search(r"失败|报错|原因|不通过|改用|换个|退回", t)][:6]
    if hot:
        out.append("【AI 关键决策句（原文摘录）】")
        for t in hot:
            out.append("  " + t[:150])
    if last_elap:
        out.append(f"（末次打点 elapsed={last_elap:.0f}s，log 共 {len(lines)} 行）")
    card = "\n".join(out)
    return card[:5200]
