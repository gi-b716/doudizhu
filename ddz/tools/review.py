"""复盘对局：**在能算清的残局里，谁把必胜打丢了**。

    uv run python -m ddz.tools.review                 # 扫全部对局，列出每一处"打丢必胜"
    uv run python -m ddz.tools.review --game 36103    # 只看某一局
    uv run python -m ddz.tools.review --at 36103 23   # 看某一手的详细对比

判据是硬的：残局求解器能证明"这个局面我方必胜"，而实际打出的那一手**不在能赢的着法里**
——那就是一个真的失误，不是"我觉得他打得不好"。求解器算不动的局面（牌多、或看不到三家
手牌）一律跳过，绝不拿估的当结论。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import cards as C
from ..agents.registry import make
from ..data.archive import Archive
from ..engine.bidding import tier_label
from ..engine.endgame import Limits, solve, solvable

#: 复盘跑在离线、没人等：预算给宽，免得忙起来"没搜完"就什么都不说
REVIEW_LIMITS = Limits(time_budget_ms=5000.0, rank_budget_ms=1000.0)
from ..explain import describe_pattern
from ..patterns import classify
from ..record import GameRecord, iter_decisions, landlord_seat
from .eval_records import action_key

__all__ = ["scan", "detail", "skeleton", "frame_view", "decision_indexes",
           "verdict_of", "panel_summary", "load_records"]


def load_records(path: Path) -> list[GameRecord]:
    return list(Archive(path).records())


def describe_cards(cards) -> str:
    """把一堆牌说成人话（``"对子 9"`` / ``"不出"``）。

    网页和命令行都用它——中文牌型只在 ``explain`` 里有一份，不在 JS 里再抄一遍。
    """
    if not cards:
        return "不出"
    try:
        return describe_pattern(classify(list(cards))[0])
    except Exception:  # 万一是非法牌组（脏档案），退化成牌面，别让整页崩掉
        return _cards(cards)


def decision_indexes(record: GameRecord) -> list[int]:
    """可以点开分析的**事件序号**：出牌/不出，且座位有效（和 ``iter_decisions`` 同一批）。

    别拿 ``range(len(events))`` 当护栏：对着 ``bid`` 事件求解会返回一份"看着挺像"的
    分析（引擎照样给建议、牌型字段全空），对着终局前那一帧甚至会给出已经结束的局面的
    结论——**都不是报错，是假结论**，比报错更糟。
    """
    return [i for i, entry in enumerate(record.events)
            if entry.is_action() and entry.seat in (0, 1, 2)]


def verdict_of(solved, cards) -> dict:
    """这一手和求解器结论的关系——**中文结论只在这里生成**，网页与命令行念同一段话。

    三种"有结论"的情形必须分开：证明能赢却没保住（打丢了）、证明本来就赢不了（必败）、
    没搜完（不表态）。压成一个布尔值的话，"本来就必败"会被念成"打丢了必胜"。
    """
    if solved is None:
        return {"code": "not_solvable", "text": "算不清（牌太多，或看不到三家手牌）"}
    if not solved.proved:
        return {"code": "unknown", "text": "没搜完（超预算），不表态"}
    if not solved.winning:
        return {"code": "lose", "text": "本来就必败——对方不出错就赢不了"}
    best = {None if m is None else action_key(m.cards) for m in (solved.best_moves or [])}
    if action_key(cards) in best:
        tail = "（就算队友乱打也照样赢）" if getattr(solved, "solo", False) else ""
        return {"code": "held", "text": "保住了" + tail}
    return {"code": "threw_win", "text": "打丢了必胜：这一手不在能赢的着法里"}


def scan(records: list[GameRecord], *, game: str | None = None) -> list[dict]:
    """找出每一处"求解器证明能赢、实际却打丢"的决策点。"""
    findings: list[dict] = []
    for record in records:
        if game is not None and str(record.game_id) != str(game):
            continue
        # 报出去的手数用**事件序号**（和 record.events / replay / --at 同一套口径），
        # 不然"第几手"在 scan 和 detail 里会是两个不同的数
        positions = {id(entry): i for i, entry in enumerate(record.events)}
        for decision in iter_decisions(record):
            index = positions[id(decision.entry)]
            state = decision.state
            if not solvable(state):
                continue
            solved = solve(state, limits=REVIEW_LIMITS)
            if verdict_of(solved, decision.move.cards if decision.move else None)["code"] != "threw_win":
                continue
            findings.append(
                {
                    "game_id": record.game_id,
                    "index": index,
                    "seat": decision.seat,
                    "is_me": decision.seat == record.seat_of_interest,
                    "landlord": state.landlord,
                    "hand": list(state.hand_for(decision.seat) or ()),
                    "played": list(decision.move.cards) if decision.move else [],
                    "best": [list(m.cards) if m else [] for m in (solved.best_moves or [])],
                    "nodes": solved.nodes,
                }
            )
    return findings


def detail(record: GameRecord, index: int, *, agent_for=None) -> dict:
    """某一手的详细对比：三家手牌、实际出的、各引擎想出的、求解器的结论。

    ``agent_for`` 是"按名字拿引擎"的来源：命令行不传（现做现用），服务传
    :meth:`Advisor.agent_for`——**它带进程级缓存**。不传又走服务那条路的话，
    每点开一手就会重新装载一次 DouZero 权重（点十手装十次，还每次刷一行日志）。
    """
    from ..record import replay

    if agent_for is None:  # 默认：现做现用（命令行一次跑完就退出，不亏）
        def agent_for(spec):
            return make(spec), spec

    state = replay(record, index)
    entry = record.events[index]
    seat = entry.seat
    view = state.for_seat(seat)

    engines = {}
    for spec in ("heuristic", "douzero:WP"):
        try:
            agent, name = agent_for(spec)
        except Exception as exc:
            engines[spec] = {"error": str(exc)}
            continue
        if agent is None:
            # **没就绪时绝不许装作有第二票**：那种 agent 内部会回落到启发式，
            # 两行结果一模一样——那是在骗看复盘的人（实测过）。
            engines[spec] = {"error": f"{name} 未就绪（没装 torch 或缺权重）"}
            continue
        try:
            candidate = agent.act(view)
            engines[spec] = {
                "cards": list(candidate.cards),
                "win_prob": candidate.win_prob,
                "reason": candidate.reasons[0] if candidate.reasons else "",
                "text": describe_cards(candidate.cards),
            }
        except Exception as exc:  # 引擎自己坏了也不影响复盘
            engines[spec] = {"error": str(exc)}

    solved = solve(view, limits=REVIEW_LIMITS) if solvable(view, REVIEW_LIMITS) else None
    return {
        "game_id": record.game_id,
        "index": index,
        "seat": seat,
        "is_me": seat == record.seat_of_interest,
        "role": "landlord" if seat == state.landlord else "farmer",
        "hands": {s: list(state.hand_for(s) or ()) for s in (0, 1, 2)},
        "landlord": state.landlord,
        "to_beat": view.to_beat.key() if view.to_beat else None,
        "played": {
            "cards": list(entry.cards) if entry.cards else [],
            "pattern": entry.pattern if isinstance(entry.pattern, str) else None,
            "kind": entry.kind,          # 光看 cards 分不出"不出"和"没有牌"
            "text": describe_cards(entry.cards),
        },
        "engines": engines,
        "solver": None if solved is None else {
            "winning": solved.winning,
            "proved": solved.proved,
            "nodes": solved.nodes,
            "best": [list(m.cards) if m else [] for m in (solved.best_moves or [])],
            "best_text": [describe_cards(m.cards) if m else "不出" for m in (solved.best_moves or [])],
            "solo": bool(getattr(solved, "solo", False)),
            "worlds": getattr(solved, "worlds", None),
        },
        "verdict": verdict_of(solved, entry.cards or None),
    }


def _table_entry(entry) -> dict | None:
    if entry is None:
        return None
    return {
        "seat": entry.seat,
        "kind": entry.kind,
        "cards": list(entry.cards),
        "pattern": entry.pattern.key() if entry.pattern else None,
        "text": describe_cards(entry.cards) if entry.cards else "不出",
    }


def frame_view(frame) -> dict:
    """一帧（``iter_frames`` 的第 k 帧 = ``events[k]`` **执行之前**）→ 网页看得懂的 JSON。

    三家手牌直接发全：复盘要的就是全信息，一局四十来帧也就十几 KB，换来的是页面上
    跳步、回退都不用自己维护状态（自己减牌迟早会减错）。**不发 events 前缀**——
    整局的事件表在 :func:`skeleton` 里发一次就够，逐帧再带一遍体积要涨七倍。
    """
    return {
        "index": frame.version,          # == 帧号 == 下一个要执行的事件序号
        "deal_index": frame.deal_index,  # 流局重发时靠它分副
        "phase": frame.phase,
        "turn": frame.turn,
        "leading": frame.leading,
        "landlord": frame.landlord,
        "high_bid": frame.high_bid,
        "must_bid": frame.must_bid,
        "redeals": frame.redeals,
        "multiplier": frame.multiplier,
        "bombs": frame.bombs,
        "counts": list(frame.counts),
        "hands": [list(frame.hand_for(s) or ()) for s in (0, 1, 2)],
        "to_beat": frame.to_beat.key() if frame.to_beat else None,
        # 桌面那一手"要压的"（页面靠它显示中文牌型，而不是让你去读 single:8:1）
        "last": _table_entry(frame.last),
        "bottom": list(frame.bottom) if frame.bottom else None,
        "table": [_table_entry(e) for e in frame.table],
        "result": frame.result,
    }


def skeleton(record: GameRecord) -> dict:
    """网页牌谱要的全部静态数据：抬头、每副牌的起手牌与底牌、事件表、逐帧、可分析的手。

    ``frames[k]`` 是 ``events[k]`` 执行**之前**的局面（``record.replay`` 的老规矩），
    所以帧比事件多一条；页面上"第 k 手"就是这个 k。
    """
    from ..record import iter_frames

    frames = list(iter_frames(record))
    decisions = []
    for index in decision_indexes(record):
        seat = record.events[index].seat
        view = frames[index].for_seat(seat)
        decisions.append({
            "index": index,
            "seat": seat,
            "kind": record.events[index].kind,
            # 能不能算：门槛之外的一手点开只会得到"算不清"，先标出来，
            # 省得用户白点（**必要不充分**：过了门槛也可能因为超预算没搜完）
            "solvable": solvable(view, REVIEW_LIMITS),
        })

    result = record.result
    return {
        "game_id": str(record.game_id),
        "captured_at": record.captured_at,
        "players": list(record.players),
        "my_seat": record.seat_of_interest,
        "landlord": landlord_seat(record),
        "source": record.source,
        "result": None if result is None else result.to_dict(),
        "panel_summary": panel_summary(record),
        "deals": [{"index": d.index, "first_bidder": d.first_bidder,
                   "hands": [list(h) for h in d.hands()], "bottom": list(d.bottom())}
                  for d in record.deals],
        "events": [e.to_dict() for e in record.events],
        "frames": [frame_view(f) for f in frames],
        "decisions": decisions,
    }


def panel_summary(record: GameRecord) -> str:
    """**浮窗自述**的一行摘要：引擎、模式、开关、它出手几次、被平台托管几次。

    "面板出手 N 次 / 我实际出手 M 次"就是"它跟没跟上"的直接答案——
    托管次数来自平台（`players[].auto`），一旦接管，叫分和出牌就不是我们的建议了。
    """
    panel = record.panel
    if not panel:
        return "（这一局没有浮窗自述：脚本版本较老，或归档时没带）"
    mine = sum(1 for e in record.events
               if e.kind in ("play", "pass") and e.seat == record.seat_of_interest)
    acted = panel.get("auto_actions", 0)
    passes = panel.get("auto_passes")
    # 0.7.1 之前只记「出牌」、不记「不出」，于是"面板出手 3/8"看着像没跟上，其实是漏记。
    # 口径不同的旧记录必须标出来，免得又照着假数字判"是不是工具的锅"。
    auto_txt = (f"面板出手 {acted}+{passes}不出 / 共 {mine}" if passes is not None
                else f"面板出手 {acted}/{mine}（≤0.7.0 只记出牌）")
    parts = [
        f"脚本 {panel.get('script', '?')}",
        f"引擎 {panel.get('engine', '?')}",
        # 0.7.4 起才带 bid；更老的记录拿不到档位，宁可不说也别猜一档
        *([f"叫分 {tier_label(panel['bid'])}"] if panel.get("bid") else []),
        f"残局 {panel.get('endgame_mode', '?')}",
        f"自动{'开' if panel.get('auto') else '关'}",
        auto_txt,
    ]
    if panel.get("hosted"):
        # ※ 而不是 ⚠：Windows 控制台默认 GBK，⚠(U+26A0) 编不出来，print 会崩
        parts.append(f"※ 平台托管 {panel['hosted']} 次")
    return " · ".join(parts)


def _cards(cards) -> str:
    """给人看的牌面（按点数，如 ``10 10``）——``format_cards`` 那个是给平台的牌 id。"""
    if not cards:
        return "不出"
    return " ".join(C.RANK_NAMES[C.rank(c)] for c in C.sort_cards(cards))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ddz.tools.review", description="复盘：谁把必胜打丢了")
    parser.add_argument("--records", default="data/games.jsonl")
    parser.add_argument("--game", default=None, help="只看某一局")
    parser.add_argument("--at", nargs=2, metavar=("GAME", "INDEX"), default=None,
                        help="看某一手的详细对比")
    args = parser.parse_args(argv)

    path = Path(args.records)
    if not path.exists():
        print(f"没有对局库：{path}", file=sys.stderr)
        return 2
    records = load_records(path)
    if not records:
        print("对局库是空的。", file=sys.stderr)
        return 2

    if args.at:
        record = next((r for r in records if str(r.game_id) == str(args.at[0])), None)
        if record is None:
            print(f"没有这一局：{args.at[0]}", file=sys.stderr)
            return 2
        info = detail(record, int(args.at[1]))
        print(f"═══ 第 {info['game_id']} 局 · 第 {info['index']} 手 · "
              f"{info['seat']} 号位（{info['role']}{'，我' if info['is_me'] else ''}）═══")
        for seat in (0, 1, 2):
            who = "地主" if seat == info["landlord"] else "农民"
            mark = " ←" if seat == info["seat"] else "  "
            print(f"  {seat} 号[{who}]{mark} {_cards(info['hands'][seat])}")
        print(f"  要压：{info['to_beat'] or '（他领出）'}")
        print(f"  浮窗：{panel_summary(record)}")
        print(f"\n  实际出手 : {_cards(info['played']['cards'])}  [{info['played']['text']}]")
        for name, data in info["engines"].items():
            if "error" in data:
                print(f"  {name:10s}: {data['error']}")
            else:
                prob = f"  估值 {data['win_prob']:.2f}" if data["win_prob"] is not None else ""
                if prob and prob.strip() in data["reason"]:
                    prob = ""    # DouZero 的理由里已经带了估值，别写两遍
                print(f"  {name:10s}: {_cards(data['cards']):18s}{prob}  "
                      f"{data['text']}  {data['reason']}")
        solver = info["solver"]
        if solver is None:
            print("  残局求解  : 不适用（牌太多，或看不到三家手牌）")
        elif not solver["proved"]:
            print("  残局求解  : 没搜完（超预算），不表态")
        else:
            verdict = "我方必胜" if solver["winning"] else "我方必败"
            print(f"  残局求解  : {verdict}（{solver['nodes']} 节点）")
            if solver["winning"]:
                print(f"              能赢的着法：{'、'.join(solver['best_text'][:8])}")
        # 这一行与网页上显示的是**同一段话**（review.verdict_of），两个入口不许各说各的
        print(f"  判定      : {info['verdict']['text']}")
        return 0

    findings = scan(records, game=args.game)
    games = len({f["game_id"] for f in findings})
    # 报"真扫了几局"：--game 只扫一局，写 118 是在说假话
    scanned = 1 if args.game else len(records)
    print(f"扫了 {scanned} 局；发现「求解器证明能赢、实际却打丢」{len(findings)} 处，"
          f"分布在 {games} 局里\n")
    for f in findings:
        who = f"{f['seat']} 号位" + ("（我）" if f["is_me"] else "")
        best = "、".join(_cards(c) or "不出" for c in f["best"][:5])
        print(f"  局 {f['game_id']} 第 {f['index']:3d} 手 {who}：手牌 {_cards(f['hand'])}")
        print(f"      实际 {_cards(f['played'])}   ←→   能赢的：{best}")
    if findings:
        print("\n（看某一手的完整对比：--at <局号> <手数>）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
