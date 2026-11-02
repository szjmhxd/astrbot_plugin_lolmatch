"""会话级设置与订阅的持久化。

为什么放 JSON 而不是数据库
--------------------------
数据量极小（几十个会话 × 几个字段），而且机器人场景下没人会去做 SQL 迁移。
一个文件、原子写、读坏了能重建，比引入 ORM 更省事，也符合 AstrBot 官方
「持久化数据放在 data 目录」的要求（这里用 ``StarTools.get_data_dir()`` 的返回值）。

数据形状
--------
::

    {
      "version": 1,
      "sessions": {
        "aiocqhttp:GroupMessage:123456": {
          "provider": "scoregg",          # "" = 跟随插件默认
          "token": "",                    # PandaScore 会话级 Key
          "mention": "all",               # none / all / list / ""（跟随默认）
          "targets": ["10001"],           # @ 指定人
          "windows": [{"start": "09:00", "end": "23:30", "days": "all"}],
          "push_time": "23:00",           # "" = 跟随默认
          "subs": ["1023"],               # 订阅的联赛 ID
          "last_push": "2026-11-01"       # 当天已推过就不重复推
        }
      }
    }
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ._log import logger

VERSION = 1

# 允许出现在会话设置里的字段，避免脏数据无限膨胀
_FIELDS = (
    "provider",
    "token",
    "mention",
    "targets",
    "windows",
    "push_time",
    "last_push",
    "subs",
)


def _clean_session(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        if key in ("targets", "subs"):
            if isinstance(value, list):
                out[key] = [str(item) for item in value if str(item).strip()]
        elif key == "windows":
            if isinstance(value, list):
                out[key] = [item for item in value if isinstance(item, dict)]
        else:
            out[key] = str(value or "")
    return out


class SessionStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._loaded = False

    # ------------------------------------------------------------------ 读写

    async def load(self) -> None:
        async with self._lock:
            self._loaded = True
            if not self._path.exists():
                return
            try:
                raw = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
                parsed = json.loads(raw)
            except (OSError, ValueError) as exc:
                logger.warning(f"[LOL赛事] 会话数据读取失败，从空数据开始：{exc}")
                return
            sessions = parsed.get("sessions") if isinstance(parsed, dict) else None
            if isinstance(sessions, dict):
                self._sessions = {
                    str(k): _clean_session(v) for k, v in sessions.items() if isinstance(v, dict)
                }

    async def flush(self) -> None:
        payload = json.dumps(
            {"version": VERSION, "sessions": self._sessions},
            ensure_ascii=False,
            indent=2,
        )
        tmp = self._path.with_suffix(".tmp")

        def _write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._path)

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            logger.warning(f"[LOL赛事] 会话数据写入失败：{exc}")

    # ------------------------------------------------------------------ 会话

    def get(self, umo: str) -> dict[str, Any]:
        return dict(self._sessions.get(umo) or {})

    def all_sessions(self) -> dict[str, dict[str, Any]]:
        return {key: dict(value) for key, value in self._sessions.items()}

    async def update(self, umo: str, **fields: Any) -> dict[str, Any]:
        """改几个字段并落盘。传 ``None`` 表示删掉这个字段（回到默认）。"""
        async with self._lock:
            current = self._sessions.setdefault(umo, {})
            for key, value in fields.items():
                if key not in _FIELDS:
                    continue
                if value is None:
                    current.pop(key, None)
                else:
                    current[key] = value
            if not current:
                self._sessions.pop(umo, None)
            snapshot = dict(current)
        await self.flush()
        return snapshot

    async def drop(self, umo: str) -> None:
        async with self._lock:
            self._sessions.pop(umo, None)
        await self.flush()

    # ------------------------------------------------------------------ 订阅

    def subs(self, umo: str) -> list[str]:
        raw = self._sessions.get(umo, {}).get("subs")
        return [str(item) for item in raw] if isinstance(raw, list) else []

    async def subscribe(self, umo: str, tournament_id: str, *, limit: int = 3) -> tuple[bool, str]:
        current = self.subs(umo)
        if tournament_id in current:
            return False, f"已经订阅了联赛 {tournament_id}。"
        if limit > 0 and len(current) >= limit:
            return False, (f"最多订阅 {limit} 个联赛，先「/lol 取消订阅 <ID>」腾个位置。")
        current.append(tournament_id)
        await self.update(umo, subs=current)
        return True, f"已订阅联赛 {tournament_id}。"

    async def unsubscribe(self, umo: str, tournament_id: str) -> tuple[bool, str]:
        current = self.subs(umo)
        if tournament_id not in current:
            return False, f"没有订阅联赛 {tournament_id}。"
        current.remove(tournament_id)
        await self.update(umo, subs=current)
        return True, f"已取消订阅联赛 {tournament_id}。"

    def subscribed_sessions(self) -> dict[str, list[str]]:
        """所有有订阅的会话 → 订阅列表。推送循环用。"""
        out: dict[str, list[str]] = {}
        for umo, data in self._sessions.items():
            subs = data.get("subs")
            if isinstance(subs, list) and subs:
                out[umo] = [str(item) for item in subs]
        return out

    async def mark_pushed(self, umo: str, day: str) -> None:
        await self.update(umo, last_push=day)

    def pushed_today(self, umo: str, day: str) -> bool:
        return str(self._sessions.get(umo, {}).get("last_push") or "") == day
