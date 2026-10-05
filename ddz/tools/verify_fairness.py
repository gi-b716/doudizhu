"""校验归档对局库的发牌承诺。

算法与客户端 ``renderFairness()`` 逐字一致：

    sha256(deck.join(',') + '|' + salt) == commitment

平台在发牌前公布承诺、终局后公开完整牌序与盐，所以事后可以复核发牌没有被改动。
这里对 :file:`data/games.jsonl` 里的每一副牌重算一遍。

    python -m ddz.tools.verify_fairness --records data/games.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

from .. import cards as C
from ..data.archive import Archive
from ..record import Deal, GameRecord

__all__ = ["DealCheck", "check_deal", "check_record", "check_archive", "main", "commitment_of"]


def _use_utf8_stdout() -> None:
    """Windows 控制台默认是 GBK，中文/符号输出会炸；这里退化成可打印。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - 只在异常环境触发
        pass


def commitment_of(deck: Iterable[int], salt: str) -> str:
    """复算承诺（客户端的 JS 版本是 ``sha256(deal.deck.join(',') + '|' + deal.salt)``）。"""
    body = ",".join(str(c) for c in deck) + "|" + salt
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DealCheck:
    game_id: str
    deal_index: int
    ok: bool
    reason: str = ""

    def __str__(self) -> str:
        mark = "✓" if self.ok else "✗"
        extra = f"（{self.reason}）" if self.reason else ""
        return f"{mark} {self.game_id} 第 {self.deal_index + 1} 副牌{extra}"


def check_deal(game_id: str, deal: Deal) -> DealCheck:
    if sorted(deal.deck) != list(range(C.DECK_SIZE)):
        return DealCheck(game_id, deal.index, False, "牌序不是 0..53 的排列")
    if not deal.commitment:
        return DealCheck(game_id, deal.index, False, "没有承诺可校验")
    if not deal.salt:
        return DealCheck(game_id, deal.index, False, "没有公开的盐")
    actual = commitment_of(deal.deck, deal.salt)
    if actual != deal.commitment:
        return DealCheck(game_id, deal.index, False, f"承诺不符：{actual[:16]}… != {deal.commitment[:16]}…")
    return DealCheck(game_id, deal.index, True)


def check_record(record: GameRecord) -> list[DealCheck]:
    return [check_deal(record.game_id, deal) for deal in record.deals]


def check_archive(records: Iterable[GameRecord]) -> list[DealCheck]:
    out: list[DealCheck] = []
    for record in records:
        out.extend(check_record(record))
    return out


def main(argv: Sequence[str] | None = None) -> int:
    _use_utf8_stdout()
    parser = argparse.ArgumentParser(prog="verify_fairness", description="复核归档对局的发牌承诺")
    parser.add_argument("--records", default=None, help="对局库 JSONL（默认 data/games.jsonl）")
    parser.add_argument("--json", action="store_true", help="输出机器可读的结果")
    parser.add_argument("--quiet", action="store_true", help="只输出汇总")
    args = parser.parse_args(argv)

    archive = Archive(Path(args.records) if args.records else None)
    records = list(archive.records())
    checks = check_archive(records)
    failed = [c for c in checks if not c.ok]

    if args.json:
        print(
            json.dumps(
                {
                    "records": len(records),
                    "deals": len(checks),
                    "ok": len(checks) - len(failed),
                    "failed": len(failed),
                    "failures": [
                        {"game_id": c.game_id, "deal": c.deal_index, "reason": c.reason} for c in failed
                    ],
                },
                ensure_ascii=False,
            )
        )
    else:
        if not args.quiet:
            for check in checks:
                print(check)
        print(f"共 {len(records)} 局 / {len(checks)} 副牌：{len(checks) - len(failed)} 副通过，{len(failed)} 副失败")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
