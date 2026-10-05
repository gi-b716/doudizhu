"""对局库归档：``data/games.jsonl`` 一行一局，``--keep-raw`` 时另存原始终局 state。

归档只收**终局** state（完美信息样本）。去重键是 ``sha256(第一副牌的牌序|盐|玩家名)``
（见 :func:`ddz.record.dedupe_key`）——同一局被反复抓取也只会留一行。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..record import SCHEMA, GameRecord, dedupe_key_of
from ..state import StateParseError

__all__ = ["ArchiveError", "Archive", "data_dir", "games_path", "raw_dir"]

GAMES_FILENAME = "games.jsonl"
RAW_DIRNAME = "raw"


class ArchiveError(Exception):
    """归档文件本身有问题（行号会写在消息里）。"""


def data_dir() -> Path:
    """数据根目录；``DDZ_DATA_DIR`` 可覆盖（测试与离线工具用）。"""
    override = os.environ.get("DDZ_DATA_DIR")
    return Path(override) if override else Path(__file__).resolve().parents[2] / "data"


def games_path() -> Path:
    return data_dir() / GAMES_FILENAME


def raw_dir() -> Path:
    return data_dir() / RAW_DIRNAME


class Archive:
    """JSONL 归档的读写门面。

    ``keys()`` 惰性缓存已存在的去重键，所以连续 :meth:`add` 不会反复扫文件。
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        raw_path: Path | str | None = None,
        keep_raw: bool = False,
    ) -> None:
        self.path = Path(path) if path is not None else games_path()
        self.raw_path = Path(raw_path) if raw_path is not None else raw_dir()
        self.keep_raw = keep_raw
        self._keys: set[str] | None = None

    # ----------------------------------------------------------------- 读

    def exists(self) -> bool:
        return self.path.exists()

    def keys(self) -> set[str]:
        if self._keys is None:
            self._keys = {dedupe_key_of(data) for _, data in self._iter_dicts(strict=False)}
        return self._keys

    def records(self, *, strict: bool = True) -> Iterator[GameRecord]:
        for _, data in self._iter_dicts(strict=strict):
            yield GameRecord.from_dict(data)

    def count(self) -> int:
        return sum(1 for _ in self._iter_dicts(strict=False))

    def _iter_dicts(self, *, strict: bool) -> Iterator[tuple[int, Mapping[str, Any]]]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                text = line.strip()
                if not text:
                    continue
                try:
                    data = json.loads(text)
                    if data.get("schema") != SCHEMA:
                        raise StateParseError(f"schema 不是 {SCHEMA}：{data.get('schema')!r}")
                except (json.JSONDecodeError, StateParseError) as exc:
                    if strict:
                        raise ArchiveError(f"{self.path}:{lineno} 解析失败：{exc}") from exc
                    continue
                yield lineno, data

    # ----------------------------------------------------------------- 写

    def add(
        self,
        record: GameRecord,
        *,
        raw: Mapping[str, Any] | None = None,
        keep_raw: bool | None = None,
    ) -> bool:
        """写入一局；重复的一局（同一副发牌）返回 ``False`` 且不落盘。

        ``keep_raw`` 为 ``None`` 时跟随实例默认值；``raw`` 给的是原始终局 state。
        """
        key = record.dedupe_key()
        if key in self.keys():
            return False
        problems = record.validate()
        if problems:
            raise ArchiveError("记录不自洽：" + "；".join(problems))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(record.to_line() + "\n")
        self._keys.add(key)
        if (self.keep_raw if keep_raw is None else keep_raw) and raw is not None:
            self.raw_path.mkdir(parents=True, exist_ok=True)
            target = self.raw_path / f"{record.game_id}.json"
            target.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
