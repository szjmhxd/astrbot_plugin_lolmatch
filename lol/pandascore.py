"""PandaScore 数据源（备选）。

用途
----
scoregg 是站点内部接口，随时可能死。PandaScore 是商业数据商，字段规范、
有服务承诺、有明确的限流口径，适合作为兜底。

免费档能拿到什么
----------------
Fixtures 档（0 元）：赛程、赛果、BO 数、战队与联赛元信息。
**逐局数据与选手统计在 Historical 档（400€/月起）才有**，所以本 provider 的
``detail`` 通常只能给出比分，拿不到 KDA——这是套餐差异，不是代码问题。

免费档限流：1000 请求/小时。所以这里对同一个 URL 做了 60 秒内存缓存，
避免用户连续敲几条指令就把配额打空。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import aiohttp

from ._log import logger
from .models import Game, Match, MatchDetail, ProviderError, Tournament

API = "https://api.pandascore.co"
_CACHE_TTL = 60.0

# PandaScore 的 status → 内部 status
_STATUS_MAP = {
    "not_started": 0,
    "running": 1,
    "finished": 2,
    "canceled": 2,
    "postponed": 0,
}

_LOCAL_TZ = datetime.now().astimezone().tzinfo or UTC


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _local(iso: Any) -> datetime | None:
    """``2026-11-04T06:00:00Z`` → 本地时间。解析不了就返回 None。"""
    raw = str(iso or "").strip()
    if not raw:
        return None
    text = raw.replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(_LOCAL_TZ)


class PandaScoreProvider:
    name = "pandascore"
    label = "PandaScore"

    def __init__(self, token: str, *, timeout: float = 12.0) -> None:
        self._token = (token or "").strip()
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}

    # ------------------------------------------------------------------ 底层

    async def _http(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Accept": "application/json",
                        "User-Agent": "astrbot-plugin-lolmatch/1.0",
                    },
                )
            return self._session

    async def close(self) -> None:
        async with self._lock:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self._token:
            raise ProviderError(
                "没有配置 PandaScore Key。请用「/lol 设置 key xxx」或在插件配置里填。",
                retryable=False,
            )

        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]

        session = await self._http()
        try:
            async with session.get(f"{API}{path}", params=params or {}) as resp:
                if resp.status == 401:
                    raise ProviderError("PandaScore Key 无效或已过期", retryable=False)
                if resp.status == 429:
                    raise ProviderError("PandaScore 触发限流（免费档 1000 次/小时）")
                if resp.status != 200:
                    raise ProviderError(f"PandaScore 返回 HTTP {resp.status}")
                payload = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise ProviderError(f"PandaScore 请求失败：{type(exc).__name__}: {exc}") from exc
        except ValueError as exc:
            raise ProviderError("PandaScore 返回了非 JSON 内容") from exc

        self._cache[key] = (now, payload)
        return payload

    # ------------------------------------------------------------------ 解析

    @staticmethod
    def _parse_tournament(raw: dict[str, Any]) -> Tournament:
        league = raw.get("league") or {}
        start = _local(raw.get("begin_at") or raw.get("scheduled_at"))
        end = _local(raw.get("end_at"))
        status = 2 if raw.get("end_at") and end and end < datetime.now(_LOCAL_TZ) else 1
        return Tournament(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            short_name=str(raw.get("name") or league.get("name") or ""),
            start_date=start.strftime("%Y-%m-%d") if start else "",
            end_date=end.strftime("%Y-%m-%d") if end else "",
            status=status,
            image=str(league.get("image_url") or ""),
        )

    @staticmethod
    def _parse_match(raw: dict[str, Any]) -> Match:
        tournament = raw.get("tournament") or {}
        league = raw.get("league") or {}
        opponents = raw.get("opponents") or []
        results = {
            str(r.get("team_id")): _int(r.get("score"))
            for r in raw.get("results") or []
            if isinstance(r, dict)
        }

        def _team(index: int) -> tuple[str, str]:
            if index < len(opponents) and isinstance(opponents[index], dict):
                team = opponents[index].get("opponent") or {}
                return (
                    str(team.get("acronym") or team.get("name") or ""),
                    str(team.get("id") or ""),
                )
            return "", ""

        name_a, id_a = _team(0)
        name_b, id_b = _team(1)
        when = _local(raw.get("scheduled_at") or raw.get("begin_at"))
        winner_id = str(raw.get("winner_id") or "")
        winner = ""
        if winner_id and winner_id == id_a:
            winner = name_a
        elif winner_id and winner_id == id_b:
            winner = name_b

        return Match(
            id=str(raw.get("id") or ""),
            tournament_id=str(tournament.get("id") or ""),
            tournament_name=str(tournament.get("name") or league.get("name") or ""),
            round_name=str(raw.get("name") or ""),
            team_a=name_a,
            team_b=name_b,
            score_a=results.get(id_a, 0) if id_a else 0,
            score_b=results.get(id_b, 0) if id_b else 0,
            best_of=_int(raw.get("number_of_games")),
            start_date=when.strftime("%Y-%m-%d") if when else "",
            start_time=when.strftime("%H:%M") if when else "",
            status=_STATUS_MAP.get(str(raw.get("status") or ""), 0),
            winner=winner,
        )

    # ------------------------------------------------------------------ 对外

    async def tournaments(self, *, page: int = 1, limit: int = 18) -> list[Tournament]:
        payload = await self._get(
            "/lol/tournaments",
            {"page": page, "per_page": max(1, min(limit, 50)), "sort": "begin_at"},
        )
        if not isinstance(payload, list):
            return []
        return [self._parse_tournament(item) for item in payload if isinstance(item, dict)]

    async def upcoming_tournaments(self, *, limit: int = 18) -> list[Tournament]:
        upcoming = await self._get("/lol/tournaments/upcoming", {"per_page": limit})
        running = await self._get("/lol/tournaments/running", {"per_page": limit})
        merged: list[Tournament] = []
        for payload in (running, upcoming):
            if isinstance(payload, list):
                merged.extend(
                    self._parse_tournament(item) for item in payload if isinstance(item, dict)
                )
        seen: set[str] = set()
        return [t for t in merged if not (t.id in seen or seen.add(t.id))]

    async def schedule(self, *, day: str = "", tournament_id: str = "") -> list[Match]:
        """某一天的比赛；``day`` 为空时返回最近的赛程。

        PandaScore 没有「按日期拉」的简单参数（要走 ``range[scheduled_at]`` 这种
        带方括号的查询串，不同版本行为不一致），所以这里改成拉「未开始 + 已结束」
        两头，再按本地日期过滤。免费档 1000 次/小时完全够，且有 60 秒缓存。
        """
        if tournament_id:
            payload = await self._get(
                f"/lol/tournaments/{tournament_id}/matches",
                {"per_page": 50, "sort": "scheduled_at"},
            )
            matches = self._collect(payload)
        else:
            upcoming = await self._get(
                "/lol/matches/upcoming", {"per_page": 50, "sort": "scheduled_at"}
            )
            past = await self._get("/lol/matches/past", {"per_page": 50, "sort": "-scheduled_at"})
            matches = self._collect(upcoming) + self._collect(past)

        if day:
            matches = [m for m in matches if m.start_date == day]
        matches.sort(key=lambda m: (m.start_date, m.start_time))
        return matches

    def _collect(self, payload: Any) -> list[Match]:
        if not isinstance(payload, list):
            return []
        return [self._parse_match(item) for item in payload if isinstance(item, dict)]

    async def week(self) -> list[Match]:
        return await self.schedule()

    async def today(self, today: str) -> list[Match]:
        return await self.schedule(day=today)

    async def detail(self, match_id: str) -> MatchDetail:
        payload = await self._get(f"/lol/matches/{match_id}")
        if not isinstance(payload, dict):
            raise ProviderError(f"PandaScore 没有返回比赛 {match_id}")

        match = self._parse_match(payload)
        games: list[Game] = []
        for index, raw in enumerate(payload.get("games") or [], start=1):
            if not isinstance(raw, dict):
                continue
            winner = raw.get("winner") or {}
            winner_id = str(winner.get("id") or "")
            opponents = payload.get("opponents") or []
            team_a_id = ""
            if opponents and isinstance(opponents[0], dict):
                team_a_id = str((opponents[0].get("opponent") or {}).get("id") or "")
            games.append(
                Game(
                    index=index,
                    win_side="a"
                    if winner_id and winner_id == team_a_id
                    else ("b" if winner_id else ""),
                )
            )
        if not games:
            logger.debug(
                f"[LOL赛事] PandaScore 未返回 {match_id} 的逐局数据（免费档不含 Historical）"
            )
        return MatchDetail(
            match=match,
            games=games,
            extra={"note": "PandaScore 免费档不含逐局技术统计，仅显示比分。"},
        )
