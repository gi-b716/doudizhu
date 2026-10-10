"""复盘页的 HTTP 面：一张页面 + 三个数据接口。

    GET /review                              页面（自带 CSS/JS，不引任何外部资源）
    GET /review/games?limit=100              对局列表（新的在前）
    GET /review/game/{game_id}               牌谱骨架（逐帧 + 可分析的手）
    GET /review/game/{game_id}/decision/{i}  某一手的四方对比 + 判定

为什么单开一个模块而不是塞进 ``app.py``：那边只管 HTTP 与参数校验，而复盘这一套
（读档案、找局、序列化牌谱）有自己的形状。**逻辑一律留在 :mod:`ddz.tools.review`**
——那边不 import FastAPI，测试不用起服务；这里只做取数、校验、拼 JSON。

两条纪律，都是压力测试里量出来的：

- **路由全部是同步 ``def``**。点开一手要跑最多 5 秒 CPU 的求解器：同步路由会被
  FastAPI 丢进线程池，事件循环照常服务浮窗的 /advise 与 /health；写成 ``async def``
  会把进程卡住——浮窗探活超时，面板当场显示"服务未启动"。
- **绝不用 ``advisor._executor``**（那是 /advise 的池子，只有 2 个 worker、400ms 期限）。
  复盘任务挤进去，实盘的建议会集体超时回落成启发式——为了看复盘把代打搞坏，不值。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from ..record import GameRecord
from ..tools.review import decision_indexes, detail, skeleton

__all__ = ["build_router", "PAGE"]

#: 页面文件跟本模块放一起（一键包整体拷 ``ddz/``，不需要额外的打包步骤）
PAGE = Path(__file__).with_name("review.html")


def _records(advisor) -> list[GameRecord]:
    """档案里的全部对局。

    ``strict=False`` 只跳过**行级**坏数据（坏 JSON / schema 不对）；``from_dict``
    自己还可能因为牌堆不是 54 张的排列而抛错——所以逐条兜住：一行脏数据不该让
    整个复盘页打不开。
    """
    out: list[GameRecord] = []
    for record in advisor.archive.records(strict=False):
        try:
            out.append(record)
        except Exception:  # pragma: no cover - 脏档案
            continue
    return out


def _find(advisor, game_id: str) -> GameRecord | None:
    """按局号找一局。档案**不按 game_id 索引**（去重键是发牌），只能扫一遍。"""
    want = str(game_id)
    return next((r for r in _records(advisor) if str(r.game_id) == want), None)


def build_router(advisor) -> APIRouter:
    """把这几个路由挂到服务上。走闭包拿 advisor，不依赖 ``app.state``。"""
    router = APIRouter()

    @router.get("/review", response_class=HTMLResponse)
    def review_page() -> HTMLResponse:
        # 读不到页面文件是**装坏了**，不是业务失败——让它报错，
        # 别装成 200 的 JSON 丢给浏览器（那样只会显示一坨文字）。
        html = PAGE.read_text(encoding="utf-8")
        # no-store：不然改完页面刷新没反应，看着像 JS 写错了
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @router.get("/review/games")
    def review_games(limit: int = Query(100, ge=1, le=500)) -> dict:
        from ..tools.report import summarize

        summary = summarize(_records(advisor), recent=limit)
        rows = list(reversed(summary["recent"]))  # summarize 给的是"最近的 N 局"，列表要新的在前
        return {
            "ok": True,
            "games": rows,
            "total": summary["games"],
            "skipped": summary["skipped"],
            "message": "" if rows else "档案里还没有对局——打一局就会自动归档。",
        }

    @router.get("/review/game/{game_id}")
    def review_game(game_id: str) -> dict:
        record = _find(advisor, game_id)
        if record is None:
            # 这一局多半是**还没归档**（脚本只在终局归档），不是出错
            return {"ok": False, "reason": "no_such_game",
                    "message": f"档案里没有这一局（{game_id}）。还没打完的对局要等终局归档。"}
        return {"ok": True, "game": skeleton(record)}

    @router.get("/review/game/{game_id}/decision/{index}")
    def review_decision(game_id: str, index: int) -> dict:
        record = _find(advisor, game_id)
        if record is None:
            return {"ok": False, "reason": "no_such_game",
                    "message": f"档案里没有这一局（{game_id}）。"}
        # 护栏要卡在"是不是出牌/不出"上，不能只卡范围：对着 bid 事件求解会返回一份
        # **看着挺像的假分析**（引擎照样给建议、牌型字段全空），比报错更糟。
        if index not in set(decision_indexes(record)):
            return {"ok": False, "reason": "not_a_decision",
                    "message": f"第 {index} 手不是可分析的出牌/不出（叫分、发牌、结算不算）。"}
        # agent_for 必须用 advisor 的：它带进程级缓存，不然每点一手就重装一次 DouZero
        info = detail(record, index, agent_for=advisor.agent_for)
        return {"ok": True, "decision": info}

    return router
