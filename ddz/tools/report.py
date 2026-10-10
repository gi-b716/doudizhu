"""战绩战报：一条命令回答"最近怎么老输"。

    uv run python -m ddz.tools.report                 # 全部对局
    uv run python -m ddz.tools.report --recent 30     # 最近 30 局另列一张表
    uv run python -m ddz.tools.report --records x.jsonl

为什么单列一个工具：这个问题的答案**全在切分维度上**。同一批牌按「角色 × 底分」切，
才看得出"亏损集中在当农民、对手叫 3 分那一格"；只看总胜率只能得出"运气不好"。
2026-10-07 复盘 118 局时就是这么找到的（农民底分3：68 局 35% −108 分，其余各格打平或小赚），
当时是手搓临时脚本算的——所以固化下来，下次不用再算一遍。

它只做**便宜的聚合**（不跑求解器、不跑引擎）。"哪一手把必胜打丢了"是
:mod:`ddz.tools.review` 的事，这里只在结尾指路。
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from ..record import GameRecord, landlord_seat  # noqa: F401  （landlord_seat 从这里转出去给老调用方）
from .review import load_records, panel_summary

__all__ = ["summarize", "report", "landlord_seat", "role_of"]


def role_of(record: GameRecord) -> str:
    """我在这一局是地主还是农民。**旁观**（没有"我"）单独算一类——它不是地主也不是农民。"""
    if record.seat_of_interest is None:
        return "旁观"
    return "地主" if record.seat_of_interest == landlord_seat(record) else "农民"


def _my_actions(record: GameRecord) -> int:
    """我在这一局实际出手几次（出牌 + 不出），用来和浮窗自述对账。"""
    return sum(1 for e in record.events
               if e.kind in ("play", "pass") and e.seat == record.seat_of_interest)


def _my_bid(record: GameRecord) -> int | None:
    for entry in record.events:
        if entry.kind == "bid" and entry.seat == record.seat_of_interest:
            return entry.value
    return None


def _brief(panel: dict | None) -> str | None:
    """最近这张表里一行放得下的自述：出手几次、被托管几次、什么引擎。"""
    if not panel:
        return None
    text = f"面板 {panel.get('auto_actions', 0)}出牌"
    if panel.get("auto_passes") is not None:
        text += f"+{panel['auto_passes']}不出"
    if panel.get("hosted"):
        text += f" / ※托管 {panel['hosted']}"
    # 0.7.5 起浮窗会点「取消托管」把控制权要回来：要回来几次也记，否则复盘时
    # 分不清"这局是机器人替我们打的"还是"我们自己打回来的"。
    if panel.get("hosting_cancels"):
        text += f"（要回 {panel['hosting_cancels']}）"
    return f"{text} / {panel.get('engine', '?')}"


def summarize(records: list[GameRecord], *, recent: int = 20) -> dict:
    """把对局库聚合成战报要的每一个数——渲染与判断都从这里拿，别再各算各的。"""
    done = sorted((r for r in records if r.result), key=lambda r: r.captured_at or 0)
    # **旁观记录**（`seat_of_interest is None`，比如开着脚本围观别人的牌局）没有"我的输赢"，
    # 不进账——但要点出来，免得总局数和档案对不上。以前这里直接 `deltas[None]` 崩掉过。
    played = [r for r in done if r.seat_of_interest is not None]
    skipped = len(done) - len(played)

    buckets: dict[tuple[str, int], list[int]] = defaultdict(list)
    bids: Counter = Counter()
    panel_games = plays = passes = passes_known = panel_mine = solver_uses = 0
    hosting_cancels = 0
    dec_plays = dec_passes = 0
    hosted: list[tuple[str, int]] = []
    rows: list[dict] = []

    for record in played:
        result = record.result
        seat = record.seat_of_interest
        delta = result.deltas[seat]
        role = role_of(record)
        buckets[(role, result.base)].append(delta)

        bid = _my_bid(record)
        if bid is not None:
            bids[bid] += 1

        panel = record.panel
        if panel:
            panel_games += 1
            plays += panel.get("auto_actions", 0)
            passes += panel.get("auto_passes") or 0
            if panel.get("auto_passes") is not None:
                passes_known += 1
            # 建议的口径从 decisions 里数：**这是唯一两边都有的分母**。
            # （拿"我实际出手"当分母是错的——里面混着浮窗压根没给建议的回合。）
            for d in panel.get("decisions", []):
                if d.get("cards"):
                    dec_plays += 1
                else:
                    dec_passes += 1
                if d.get("solver"):
                    solver_uses += 1
            panel_mine += _my_actions(record)
            if panel.get("hosted"):
                hosted.append((str(record.game_id), panel["hosted"]))
            hosting_cancels += panel.get("hosting_cancels") or 0

        rows.append({
            "at": record.captured_at, "game_id": str(record.game_id), "delta": delta,
            "role": role, "base": result.base, "multiplier": result.multiplier,
            "landlord": landlord_seat(record),
            "panel": panel_summary(record) if panel else None,
            "panel_brief": _brief(panel),
            "hosted": (panel or {}).get("hosted", 0),
        })

    deltas = [r["delta"] for r in rows]
    n = len(deltas)
    net = sum(deltas)
    mean = net / n if n else 0.0
    sd = statistics.stdev(deltas) if n > 1 else 0.0
    se = sd / n ** 0.5 if n else 0.0
    return {
        "games": n,
        "skipped": skipped,
        "wins": sum(1 for d in deltas if d > 0),
        "net": net,
        "mean": mean,
        "sd": sd,
        "t": (mean / se) if se else 0.0,
        "ci": (mean - 1.96 * se, mean + 1.96 * se) if se else (0.0, 0.0),
        "buckets": dict(buckets),
        "bids": dict(bids),
        "landlord_games": sum(1 for r in rows if r["role"] == "地主"),
        "panel": {"games": panel_games, "plays": plays, "passes": passes,
                  "passes_known": passes_known, "dec_plays": dec_plays,
                  "dec_passes": dec_passes, "mine": panel_mine,
                  "solver": solver_uses, "hosted": hosted,
                  "hosting_cancels": hosting_cancels},
        "recent": rows[-recent:] if recent else [],
        "span": (rows[0]["at"], rows[-1]["at"]) if rows else (None, None),
    }


def _bucket_lines(buckets: dict[tuple[str, int], list[int]]) -> list[str]:
    worst = min(buckets, key=lambda k: sum(buckets[k])) if buckets else None
    lines = []
    for key in sorted(buckets, key=lambda k: (k[0] != "农民", k[1])):
        role, base = key
        xs = buckets[key]
        net = sum(xs)
        mark = "   ← 亏损集中在这一格" if key == worst and net < 0 else ""
        lines.append(
            f"  {role} · 底分{base}  {len(xs):>3} 局   胜率 {sum(1 for x in xs if x > 0)/len(xs):>3.0%}"
            f"   净 {net:>+5}   {net/len(xs):>+6.2f}/局{mark}"
        )
    return lines


def report(records: list[GameRecord], *, recent: int = 20, source: str = "") -> str:
    s = summarize(records, recent=recent)
    if not s["games"]:
        return "对局库是空的。"

    def stamp(t):
        return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "?"

    out = [f"对局库 {source}：{s['games']} 局（{stamp(s['span'][0])} ~ {stamp(s['span'][1])}）", ""]
    if s["skipped"]:
        out.append(f"（另有 {s['skipped']} 局是旁观，没有我的输赢，未计入）\n")
    lo, hi = s["ci"]
    out.append(f"总账   {s['games']} 局   胜 {s['wins']} ({s['wins']/s['games']:.0%})   净 {s['net']:+d}   "
               f"平均 {s['mean']:+.2f}/局")
    out.append(f"       标准差 {s['sd']:.2f}   t = {s['t']:+.2f}   95% 区间 {lo:+.2f} ~ {hi:+.2f}/局")
    out.append("")
    out.append("按「角色 × 底分」切（**亏损在哪一格，一眼可见**）")
    out.extend(_bucket_lines(s["buckets"]))
    out.append("")

    if s["recent"]:
        out.append(f"最近 {len(s['recent'])} 局")
        for row in s["recent"]:
            brief = f"   {row['panel_brief']}" if row["panel_brief"] else ""
            out.append(f"  {stamp(row['at'])} {row['game_id']:>7} {row['delta']:>+3} "
                       f"{row['role']} 底{row['base']}×{row['multiplier']}{brief}")
        out.append("")

    p = s["panel"]
    bids = s["bids"]
    bid_txt = "、".join(f"{k}分×{v}" for k, v in sorted(bids.items())) or "—"
    out.append(f"叫分   {s['landlord_games']}/{s['games']} 局当上地主（{s['landlord_games']/s['games']:.0%}）；"
               f"我的叫分：{bid_txt}")
    if p["games"]:
        rate = f"{p['plays']/p['dec_plays']:.0%}" if p["dec_plays"] else "—"
        out.append(f"浮窗   {p['games']}/{s['games']} 局有自述；建议里该出牌 {p['dec_plays']} 次，"
                   f"面板出手 {p['plays']} 次（{rate}）")
        pass_note = (f"（只有 0.7.1 以上的脚本才记「不出」，这样的局有 {p['passes_known']} 局）"
                     if p["passes_known"] < p["games"] else "")
        out.append(f"       该不出 {p['dec_passes']} 次，面板出手 {p['passes']} 次{pass_note}")
        if p["solver"]:
            out.append(f"       残局求解在实盘出手 {p['solver']} 次"
                       f"（实盘价值≈0，它的用武之地是复盘——见 README）")
        for gid, n in p["hosted"]:
            # 用 ※ 不用 ⚠：Windows 控制台默认 GBK，⚠(U+26A0) 不在 GBK 里，print 会直接崩
            out.append(f"       ※ 局 {gid} 被平台托管 {n} 次——那几手是平台自己的机器人在打")
        if p["hosting_cancels"]:
            out.append(f"       其中 {p['hosting_cancels']} 次被浮窗点「取消托管」要了回来（0.7.5 起）")
    else:
        out.append("浮窗   （还没有带自述的对局：脚本要 0.6.0 以上）")
    out.append("")
    # 不写 `uv run` 前缀：一键包里没有 uv（那儿看 说明.txt 给的 python 路径）
    out.append("要看「哪一手把必胜打丢了」：python -m ddz.tools.review")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ddz.tools.report", description="战绩战报")
    parser.add_argument("--records", default="data/games.jsonl")
    parser.add_argument("--recent", type=int, default=20, help="最近 N 局单列一张表（0 = 不列）")
    args = parser.parse_args(argv)

    path = Path(args.records)
    if not path.exists():
        print(f"没有对局库：{path}", file=sys.stderr)
        return 2
    records = load_records(path)
    print(report(records, recent=args.recent, source=str(path)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
