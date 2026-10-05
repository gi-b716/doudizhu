"""会话与编排：把一份平台 state 变成一条建议。

职责分开得很清楚，方便单独测：

- **会话**：按 ``session`` 记住上一份 :class:`~ddz.state.GameState`。``unchanged`` 只刷新计时，
  不重建（客户端 ``accept()`` 同口径）；version 跳跃不做增量假设。
- **换局即重置**：``version`` 只在**一局之内**单调递增，换一局会从 0 重新开始（客户端自己也是
  这么用的）。所以缓存键必须带上局标识，否则第二把的 v5 会命中第一把 v5 的建议——整个面板
  都会显示上一局的牌。局标识优先用页面给的 ``game_id``（``#ddz`` 的 ``data-game``），
  拿不到就靠 **version 回退** 来判定换局。
- **永远给得出的东西**：记牌器、威胁、手牌分析在任何回合都给——这样"没轮到我"时界面也有内容，
  而不是一片空白。
- **建议缓存**：键 ``(version, seat, agent)``。同一个局面被反复问（浮窗重绘、多开标签页）只算一次。
- **超时**：agent 调用丢进线程池，``deadline_ms`` 到了就放弃并回退启发式（PLAN §3.4：Windows
  没有 SIGALRM，只能靠 ``future.result(timeout=...)``）。
- **逻辑问题也是 200**：旁观 / 没轮到 / 解析失败 / 引擎不可用都返回 ``ok: false`` + ``reason``，
  脚本只看 ``ok`` 就行，不用区分 HTTP 状态码。

延迟预算（PLAN §3.5）：解析 + 生成 + 启发式合计约 5ms 量级，远小于 15 秒出牌时限。
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Mapping

from .. import __version__
from ..agents.registry import make
from ..counter import counter_snapshot, threats
from ..data.archive import Archive
from ..engine.base import Agent, Candidate
from ..engine.heuristic import analyze_hand
from ..explain import describe_move
from ..record import from_finished_state
from ..state import (
    PHASE_BIDDING, PHASE_FINISHED, GameState, StateParseError, from_platform_state,
)

__all__ = ["AdviseOptions", "Advisor", "Session"]

log = logging.getLogger("ddz.advisor")

#: 每个会话最多缓存多少条建议（按 version 从旧到新淘汰）。
_CACHE_LIMIT = 64
#: 最多同时记多少个会话（超出按最近使用淘汰）。
_SESSION_LIMIT = 16


@dataclass(slots=True)
class AdviseOptions:
    """``/advise`` 的 ``options``。"""

    agent: str = "auto"
    top_k: int = 3
    deadline_ms: int = 400
    for_seat: int | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "AdviseOptions":
        data = dict(payload or {})
        for_seat = data.get("for_seat")
        return cls(
            agent=str(data.get("agent") or "auto"),
            top_k=int(data.get("top_k") or 3),
            deadline_ms=int(data.get("deadline_ms") or 400),
            for_seat=None if for_seat is None else int(for_seat),
        )


@dataclass(slots=True)
class Session:
    """一个浮窗（或一个标签页）的会话状态。"""

    key: str
    game_id: str | None = None
    state: GameState | None = None
    cache: dict[tuple[int, int, str], dict] = field(default_factory=dict)
    seen_at: float = 0.0

    def reset(self, game_id: str | None = None) -> None:
        """换局：丢掉上一局的局面与**全部**缓存（version 跨局会重来，缓存必须一起清）。"""
        self.game_id = game_id
        self.state = None
        self.cache.clear()

    def is_new_game(self, game_id: str | None, version: int) -> bool:
        """这一份 state 是不是新的一局？

        两个判据：页面明确报了不同的 ``game_id``；或者 ``version`` 退回去了
        （version 只在一局之内单调递增，退回去只能是换局）。
        """
        if game_id is not None and game_id != self.game_id:
            return True
        return self.state is not None and version < self.state.version

    def remember(self, key: tuple[int, int, str], value: dict) -> None:
        self.cache[key] = value
        if len(self.cache) > _CACHE_LIMIT:
            for old in sorted(self.cache, key=lambda item: item[0])[: len(self.cache) - _CACHE_LIMIT]:
                self.cache.pop(old, None)


class Advisor:
    """无状态 HTTP 层与有状态引擎之间的那层。"""

    def __init__(
        self,
        *,
        archive: Archive | None = None,
        default_agent: str = "auto",
        auto_preference: tuple[str, ...] = ("douzero:WP",),
        top_k: int = 3,
        deadline_ms: int = 400,
        workers: int = 2,
        strict_timeout: bool = False,
    ) -> None:
        self.archive = archive if archive is not None else Archive()
        self.default_agent = default_agent
        #: ``auto`` 的偏好顺序：注册过谁就用谁，都没有再落到 ``default_agent``。
        #: Phase 4 只要 ``advisor.register("douzero:WP", agent)``，auto 就会自动切过去。
        self.auto_preference = tuple(auto_preference)
        self.default_top_k = top_k
        self.default_deadline_ms = deadline_ms
        self.strict_timeout = strict_timeout
        self.started_at = time.time()
        self.records: int | None = None
        self._sessions: dict[str, Session] = {}
        self._agents: dict[str, Agent] = {}
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ddz-advise")
        self._analysis_cache: dict[tuple[int, ...], dict] = {}
        self._fallback: Agent | None = None

    def _fallback_agent(self) -> Agent:
        """超时/出错时的兜底：永远是一个干净、独立的启发式实例。"""
        if self._fallback is None:
            self._fallback = make("heuristic")
        return self._fallback

    # ----------------------------------------------------------------- 会话

    def session(self, key: str) -> Session:
        found = self._sessions.get(key)
        if found is None:
            if len(self._sessions) >= _SESSION_LIMIT:
                oldest = min(self._sessions.values(), key=lambda s: s.seen_at)
                self._sessions.pop(oldest.key, None)
            found = Session(key=key)
            self._sessions[key] = found
        found.seen_at = time.time()
        return found

    def forget(self, key: str) -> bool:
        return self._sessions.pop(key, None) is not None

    def resolve_auto(self) -> str:
        """``auto`` 用哪个引擎：偏好列表里第一个已注册的，否则回到 ``default_agent``。"""
        for spec in self.auto_preference:
            if spec in self._agents:
                return spec
        if self.default_agent != "auto":
            return self.default_agent
        return "heuristic"

    def available_engines(self) -> list[str]:
        """当前真正能用的引擎（给浮窗/运维看的）。"""
        ready = [spec for spec in self.auto_preference if spec in self._agents]
        return ["heuristic", *ready]

    def register(self, name: str, agent: Agent) -> None:
        """预注册一个 agent 实例（测试替身、以及 Phase 4 的 DouZero 都走这里）。"""
        self._agents[name] = agent

    def agent_for(self, spec: str) -> tuple[Agent | None, str]:
        """``spec`` → (agent, 实际用的名字)。``auto`` 用配置里的默认引擎。"""
        wanted = (spec or "auto").strip() or "auto"
        if wanted == "auto":
            wanted = self.resolve_auto()
        if wanted.startswith("douzero") and wanted not in self._agents:
            # Phase 4：权重/上游代码就位并 register 之后，这里就会返回真 agent
            return None, wanted
        if wanted not in self._agents:
            self._agents[wanted] = make(wanted)
        return self._agents[wanted], self._agents[wanted].name

    # ----------------------------------------------------------------- 主入口

    def advise(
        self,
        session_key: str,
        payload: Mapping[str, Any],
        options: AdviseOptions | None = None,
        *,
        game_id: str | None = None,
    ) -> dict:
        """核心入口：解析 → 组装基础信息 → （该给建议时）问 agent。"""
        options = options or AdviseOptions()
        session = self.session(session_key)
        started = time.perf_counter()

        try:
            state = from_platform_state(payload, prev=session.state)
        except StateParseError as exc:
            # 解析不了要留痕：否则日志里只有一行 200，根本看不出哪里不对
            log.warning("session=%s 解析失败：%s", session_key, exc)
            return {
                "ok": False,
                "reason": "parse_error",
                "message": str(exc),
                "recommended": None,
                "alternatives": [],
            }
        if session.is_new_game(game_id, state.version):
            session.reset(game_id)

        # 记住这份状态：``unchanged`` 刷新后的计时也要落地（客户端 ``accept()`` 同口径），
        # 但迟到的旧 version 不能把会话倒退回去。
        if session.state is None or state.version >= session.state.version:
            session.state = state
        parse_ms = (time.perf_counter() - started) * 1000.0

        seat = options.for_seat if options.for_seat is not None else state.advise_seat
        view = state if seat is None or seat == state.seat else state.for_seat(seat)
        base = self._base_payload(view, seat, parse_ms)

        if seat is None:
            return {**base, "ok": False, "reason": "spectator", "message": "旁观且拿不到手牌，无法建议"}
        if state.phase == PHASE_FINISHED:
            return {**base, "ok": False, "reason": "finished", "message": "本局已结束"}
        if state.turn != seat:
            return {**base, "ok": False, "reason": "not_my_turn", "message": "还没轮到这一家"}

        if state.phase == PHASE_BIDDING:
            return self._bid_advice(view, seat, base, options, started)

        agent, name = self.agent_for(options.agent)
        cache_key = (state.version, seat, name)
        cached = session.cache.get(cache_key)
        if cached is not None:
            return cached

        if agent is None:
            return {
                **base,
                "ok": False,
                "reason": "no_engine",
                "message": f"{name} 未就绪：缺 torch 或 third_party/DouZero 权重，切回启发式即可用",
                "engine": {"name": name, "device": None, "fallback": False},
            }

        deadline = max(0.05, (options.deadline_ms or self.default_deadline_ms) / 1000.0)
        agent_ms = 0.0
        fallback = False
        future: Future = self._executor.submit(agent.act, view)
        try:
            candidate = future.result(timeout=deadline)
        except FutureTimeout:
            future.cancel()
            fallback = True
            candidate = self._fallback_agent().act(view)
        except Exception as exc:  # 引擎炸了也不能让浮窗空着
            fallback = True
            candidate = self._fallback_agent().act(view)
            candidate.reasons = [f"引擎出错，已回退启发式（{exc}）", *candidate.reasons]
        agent_ms = (time.perf_counter() - started) * 1000.0 - parse_ms

        result = {
            **base,
            "ok": True,
            "recommended": self._describe(candidate, options.top_k),
            "alternatives": [self._describe(alt, 0) for alt in candidate.alternatives[: max(0, options.top_k - 1)]],
            "engine": {
                "name": name,
                "device": None,
                "fallback": fallback,
                "tags": list(candidate.tags),
            },
            "timing_ms": {
                **base["timing_ms"],
                "agent": round(agent_ms, 3),
                **{k: v for k, v in candidate.timing.items() if k != "total"},
                "total": round((time.perf_counter() - started) * 1000.0, 3),
            },
        }
        session.remember(cache_key, result)
        return result

    # ----------------------------------------------------------------- 叫分与归档

    def _bid_advice(self, view: GameState, seat: int, base: dict, options: AdviseOptions, started: float) -> dict:
        # 叫分永远走启发式：DouZero 没有叫分模型，也不认识流局（PLAN §0.2）
        agent, name = self.agent_for("heuristic")
        bid = agent.bid(view)
        return {
            **base,
            "ok": True,
            "bidding": {
                "value": bid.value,
                "label": "不叫" if bid.value == 0 else f"{bid.value} 分",
                "strength": round(bid.strength, 2),
                "legal": view.legal_bids,
                "rationale": list(bid.reasons),
            },
            "engine": {"name": name, "device": None, "fallback": False},
            "timing_ms": {
                **base["timing_ms"],
                "agent": round((time.perf_counter() - started) * 1000.0 - base["timing_ms"]["parse"], 3),
                "total": round((time.perf_counter() - started) * 1000.0, 3),
            },
        }

    def ingest(
        self,
        state: Mapping[str, Any],
        *,
        game_id: str | None = None,
        captured_at: float | None = None,
        source: str = "platform",
        seat_of_interest: int | None = None,
        keep_raw: bool = False,
    ) -> dict:
        """归档一份终局 state（``/ingest`` 的实现）。"""
        from ..record import GameRecord

        try:
            record: GameRecord = from_finished_state(
                state,
                game_id=game_id,
                captured_at=captured_at,
                source=source,
                seat_of_interest=seat_of_interest,
            )
        except StateParseError as exc:
            log.warning("归档被拒：%s", exc)
            return {"ok": False, "reason": "parse_error", "message": str(exc)}

        try:
            added = self.archive.add(record, raw=state, keep_raw=keep_raw)
        except Exception as exc:  # 归档失败不该把脚本打挂
            return {
                "ok": False, "reason": "archive_error", "message": str(exc), "game_id": record.game_id
            }

        total = self.records
        if total is None:
            total = self.archive.count()
        elif added:
            total += 1
        self.records = total
        return {
            "ok": True,
            "added": added,
            "duplicate": not added,
            "game_id": record.game_id,
            "dedupe_key": record.dedupe_key(),
            "records": total,
            "message": "已归档" if added else "这局已经在库里了",
        }

    # ----------------------------------------------------------------- 组装

    def _base_payload(self, view: GameState, seat: int | None, parse_ms: float) -> dict:
        hand = view.hand_for(seat) if seat is not None else None
        return {
            "ok": False,  # 由调用方按情况改成 True
            "version": view.version,
            "seat": seat,
            "phase": view.phase,
            "is_my_turn": seat is not None and view.turn == seat and view.phase != PHASE_FINISHED,
            "leading": view.leading,
            "to_beat": view.to_beat.key() if view.to_beat is not None else None,
            "hand": list(hand) if hand else None,
            "counts": view.counts,
            "landlord": view.landlord,
            "multiplier": view.multiplier,
            "card_counter": counter_snapshot(view),
            "threats": [{"code": t.code, "level": t.level, "text": t.text} for t in threats(view)],
            "hand_analysis": self._analyze(hand),
            "bidding": None,
            "recommended": None,
            "alternatives": [],
            "engine": {"name": None, "device": None, "fallback": False},
            "timing_ms": {"parse": round(parse_ms, 3), "total": round(parse_ms, 3)},
        }

    def _analyze(self, hand) -> dict | None:
        """手牌分析带缓存（同一手牌在不同局面下反复问只算一次）。"""
        if not hand:
            return None
        key = tuple(sorted(hand))
        cached = self._analysis_cache.get(key)
        if cached is None:
            cached = analyze_hand(key)
            if len(self._analysis_cache) > 512:
                self._analysis_cache.clear()
            self._analysis_cache[key] = cached
        return cached

    @staticmethod
    def _describe(candidate: Candidate, top_k: int) -> dict:
        return {
            "cards": list(candidate.cards),
            "pattern_key": candidate.pattern_key,
            # 平台 DoudizhuRules.patternKey 的格式——代打时要用它拨「按哪种牌型打出」的下拉框，
            # 提交给服务器的 choice 就是它（我们自己的 key 类型名和点数约定都不同）
            "platform_key": candidate.move.pattern.platform_key() if candidate.move else None,
            "description": describe_move(candidate.move),
            "score": None if candidate.score == float("inf") else round(candidate.score, 3),
            "win_prob": candidate.win_prob,
            "source": candidate.source,
            "rationale": list(candidate.reasons[:3]),
            "tags": list(candidate.tags),
        }

    # ----------------------------------------------------------------- 运维

    def configure(self, **kwargs: Any) -> dict:
        """运行时改默认引擎 / top_k / 超时（``/config``）。"""
        applied: dict[str, Any] = {}
        if "default_agent" in kwargs and kwargs["default_agent"] is not None:
            self.default_agent = str(kwargs["default_agent"])
            applied["default_agent"] = self.default_agent
        if kwargs.get("top_k") is not None:
            self.default_top_k = int(kwargs["top_k"])
            applied["top_k"] = self.default_top_k
        if kwargs.get("deadline_ms") is not None:
            self.default_deadline_ms = int(kwargs["deadline_ms"])
            applied["deadline_ms"] = self.default_deadline_ms
        if kwargs.get("forget_session"):
            applied["forgot"] = self.forget(str(kwargs["forget_session"]))
        return applied

    def status(self) -> dict:
        records = self.records
        if records is None:
            records = self.archive.count()
        return {
            "version": __version__,
            "default_agent": self.default_agent,
            "auto_engine": self.resolve_auto(),
            "available_engines": self.available_engines(),
            "top_k": self.default_top_k,
            "deadline_ms": self.default_deadline_ms,
            "sessions": len(self._sessions),
            "records": records,
            "uptime_s": round(time.time() - self.started_at, 3),
        }
