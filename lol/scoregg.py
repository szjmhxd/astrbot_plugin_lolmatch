"""scoregg（玩加赛事）数据源。

为什么选它
----------
1. 无需鉴权、无需付费，赛程赛果一次请求全带；
2. 中文队名、中文联赛名，机器人直接可用；
3. 单场详情里**逐局 KDA、经济、大小龙、推塔、每分钟补刀与伤害、双方各 5 人的
   英雄与 KDA 都在同一个响应里**，不需要二次请求。

风险与对策
----------
这是站点前端在用的内部接口（真实后端 ``api-v2.scoregg.com``），没有公开文档，
字段随时可能变。所以：

- 所有解析都走 ``_get`` 里的统一容错，单场字段缺失只降级显示，不抛整条异常；
- 对外只暴露 :class:`~lol.models.Match` 等内部模型，上层感知不到字段名；
- 接口不可用时抛 :class:`ProviderError`，由上层切到备用源。

接口行为备忘（实测 2026-11）
---------------------------
- 不传 ``date``：返回连续 7 天，键形如 ``2026.11.02``，当天无比赛时值是空列表；
- 传 ``date`` + ``tournament_id``：只返回那一天，且只含该联赛。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any

import aiohttp

from ._log import logger
from .models import (
    Game,
    Match,
    MatchDetail,
    PlayerStat,
    ProviderError,
    Tournament,
)

API = "https://www.scoregg.com/services/api_url.php"

# 站点前端固定带的参数，缺了会 404 或返回空
BASE_PARAMS: dict[str, Any] = {
    "platform": "web",
    "api_version": "9.9.9",
    "gameID": 1,
}

# 联赛状态：2 = 已结束
_TOURNAMENT_FINISHED = 2


def _int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _day_key(text: str) -> str:
    """``2026.11.02`` → ``2026-11-02``。已经是横线格式就原样返回。"""
    raw = str(text or "").strip()
    return raw.replace(".", "-") if raw else ""


def _champion_name(url: Any) -> str:
    """从英雄图标地址里取英雄名：``.../champion/Rumble.png`` → ``Rumble``。

    新英雄的图标路径是 ``.../champion_new/543.png``（纯数字），取不到名字就
    返回空串——由 :class:`~lol.champions.ChampionBook` 用 heroID 兜底。
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    tail = raw.rsplit("/", 1)[-1]
    name = tail.rsplit(".", 1)[0] if "." in tail else tail
    return "" if name.isdigit() else name


def _when(data: dict[str, Any]) -> tuple[str, str]:
    """详情接口的时间字段有两种形态，统一成 ``(YYYY-MM-DD, HH:MM)``。

    - ``start_time`` 是秒级时间戳（如 ``1793487600``）；
    - ``start_time_string`` 是 ``11-01 07:00``，没有年份。
    优先用时间戳，因为它自带年份，跨年不会错。
    """
    try:
        stamp = int(str(data.get("start_time") or ""))
    except (TypeError, ValueError):
        stamp = 0
    if stamp > 1_000_000_000:
        moment = datetime.fromtimestamp(stamp)
        return moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M")

    raw = str(data.get("start_time_string") or "").strip()
    match = re.match(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}:\d{2})", raw)
    if match:
        month, day, clock = int(match.group(1)), int(match.group(2)), match.group(3)
        year = datetime.now().year
        # 「12-31」在今天是一月时说明是去年，反之亦然，避免跨年显示错年份
        guess = datetime(year, month, day)
        today = datetime.now()
        if (today - guess).days > 180:
            year += 1
        elif (guess - today).days > 180:
            year -= 1
        return f"{year}-{month:02d}-{day:02d}", clock
    return str(data.get("start_date") or ""), raw[-5:] if raw else ""


class ScoreggProvider:
    """scoregg 数据源。实例可复用，内部持有连接池。"""

    name = "scoregg"
    label = "玩加赛事"

    def __init__(self, *, timeout: float = 12.0, book: Any = None) -> None:
        self._timeout = timeout
        self._book = book  # ChampionBook | None，用来补全缺失的英雄名
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 底层

    async def _http(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                    headers={
                        "accept": "application/json",
                        "user-agent": "astrbot-plugin-lolmatch/1.0",
                        "referer": "https://www.scoregg.com/",
                    },
                )
            return self._session

    async def close(self) -> None:
        async with self._lock:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None

    async def _post(self, params: dict[str, Any]) -> dict[str, Any]:
        """打一次接口，返回 ``data`` 段。失败抛 :class:`ProviderError`。"""
        payload = {**BASE_PARAMS, **params}
        session = await self._http()
        try:
            async with session.post(API, data=payload) as resp:
                if resp.status != 200:
                    raise ProviderError(f"scoregg 返回 HTTP {resp.status}")
                # 这个接口偶尔会吐 HTML（风控页），不能直接用 resp.json()
                text = await resp.text()
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise ProviderError(f"scoregg 请求失败：{type(exc).__name__}: {exc}") from exc

        try:
            body = __import__("json").loads(text)
        except ValueError as exc:
            raise ProviderError("scoregg 返回了非 JSON 内容（可能被风控拦截）") from exc

        if not isinstance(body, dict) or str(body.get("code")) != "200":
            message = body.get("message") if isinstance(body, dict) else ""
            raise ProviderError(f"scoregg 业务错误：{message or body!r}"[:200])

        data = body.get("data")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ 解析

    @staticmethod
    def _parse_tournament(raw: dict[str, Any]) -> Tournament:
        return Tournament(
            id=str(raw.get("tournamentID") or raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            short_name=str(raw.get("short_name") or ""),
            start_date=str(raw.get("start_date") or ""),
            end_date=str(raw.get("end_date") or ""),
            status=_int(raw.get("status")),
            image=str(raw.get("image_thumb") or raw.get("image") or ""),
        )

    @staticmethod
    def _parse_match(raw: dict[str, Any], tournament: dict[str, Any] | None, day: str) -> Match:
        info = tournament or {}
        return Match(
            id=str(raw.get("match_id") or ""),
            tournament_id=str(raw.get("tournamentID") or info.get("tournamentID") or ""),
            tournament_name=str(
                raw.get("tournament_name") or info.get("short_name") or info.get("name") or ""
            ),
            round_name=str(raw.get("round_name") or ""),
            team_a=str(raw.get("team_a_short_name") or ""),
            team_b=str(raw.get("team_b_short_name") or ""),
            score_a=_int(raw.get("team_a_win")),
            score_b=_int(raw.get("team_b_win")),
            best_of=_int(raw.get("game_count")),
            start_date=day or str(raw.get("start_date") or ""),
            start_time=str(raw.get("start_time") or ""),
            status=_int(raw.get("status")),
            winner=str(raw.get("win_team_short_name") or ""),
        )

    # ------------------------------------------------------------------ 对外

    async def tournaments(self, *, page: int = 1, limit: int = 18) -> list[Tournament]:
        """联赛列表。``status == 2`` 的（已结束）由调用方决定要不要过滤。"""
        data = await self._post(
            {
                "api_path": "services/match/web_tournament_group_list.php",
                "method": "GET",
                "language_id": 1,
                "type": "all",
                "page": page,
                "limit": limit,
            }
        )
        items = data.get("list")
        if not isinstance(items, list):
            return []
        return [self._parse_tournament(item) for item in items if isinstance(item, dict)]

    async def upcoming_tournaments(self, *, limit: int = 18) -> list[Tournament]:
        """正在进行 + 尚未开始。刷订阅列表时用这个，避免把往届赛事列出来。"""
        return [t for t in await self.tournaments(limit=limit) if t.status != _TOURNAMENT_FINISHED]

    async def schedule(self, *, day: str = "", tournament_id: str = "") -> list[Match]:
        """某一天（或未来一周）的比赛。``day`` 为空时返回 7 天。"""
        params: dict[str, Any] = {"api_path": "services/match/web_math_list.php"}
        if day:
            params["date"] = day
        if tournament_id:
            params["tournament_id"] = tournament_id
        data = await self._post(params)

        listing = data.get("list")
        if not isinstance(listing, dict):
            return []

        out: list[Match] = []
        for raw_day, value in listing.items():
            norm_day = _day_key(raw_day)
            if day and norm_day != day:
                continue
            if not isinstance(value, dict):
                continue
            blocks = value.get("info")
            if not isinstance(blocks, dict):
                continue
            for block in blocks.values():
                if not isinstance(block, dict):
                    continue
                info = block.get("tournamentinfo") or {}
                matches = block.get("list")
                if not isinstance(matches, list):
                    continue
                for item in matches:
                    if not isinstance(item, dict):
                        continue
                    parsed = self._parse_match(item, info, norm_day)
                    # 赛程里偶尔会有「对阵未定」的空壳条目（两个队名都空），直接丢掉
                    if not (parsed.team_a or parsed.team_b):
                        continue
                    out.append(parsed)
        out.sort(key=lambda m: (m.start_date, m.start_time))
        return out

    async def week(self) -> list[Match]:
        return await self.schedule()

    async def today(self, today: str) -> list[Match]:
        return await self.schedule(day=today)

    async def detail(self, match_id: str) -> MatchDetail:
        data = await self._post(
            {
                "api_path": "services/match/match_info_new.php",
                "method": "post",
                "language_id": 1,
                "matchID": match_id,
            }
        )
        if not data:
            raise ProviderError(f"scoregg 没有返回比赛 {match_id} 的信息")

        status = _int(data.get("status"))
        winner = ""
        win_id = str(data.get("win_teamID") or "")
        if win_id:
            if win_id == str(data.get("teamID_a") or ""):
                winner = str(data.get("team_a_short_name") or "")
            elif win_id == str(data.get("teamID_b") or ""):
                winner = str(data.get("team_b_short_name") or "")

        when_date, when_time = _when(data)
        match = Match(
            id=str(data.get("matchID") or match_id),
            tournament_id=str(data.get("tournamentID") or ""),
            tournament_name=str(
                data.get("tournament_short_name") or data.get("tournament_name") or ""
            ),
            round_name=str(data.get("round_name") or ""),
            team_a=str(data.get("team_a_short_name") or ""),
            team_b=str(data.get("team_b_short_name") or ""),
            score_a=_int(data.get("team_a_win")),
            score_b=_int(data.get("team_b_win")),
            best_of=_int(data.get("game_count")),
            start_date=when_date,
            start_time=when_time,
            status=status,
            winner=winner,
        )

        games: list[Game] = []
        results = data.get("result_list")
        if isinstance(results, list):
            for index, raw in enumerate(results, start=1):
                if not isinstance(raw, dict):
                    continue
                team_a_id = str(raw.get("teamID_a") or "")
                seat = str(raw.get("win_teamID") or "")
                win_side = ""
                if seat and team_a_id:
                    win_side = "a" if seat == team_a_id else "b"
                games.append(
                    Game(
                        index=index,
                        win_side=win_side,
                        kills_a=_int(raw.get("kills_a")),
                        kills_b=_int(raw.get("kills_b")),
                        gold_a=_int(raw.get("money_a")),
                        gold_b=_int(raw.get("money_b")),
                        baron_a=_int(raw.get("baron_a")),
                        baron_b=_int(raw.get("baron_b")),
                        dragon_a=_int(raw.get("dragon_a")),
                        dragon_b=_int(raw.get("dragon_b")),
                        duration_min=_int(raw.get("game_time_minute")),
                        players_a=self._parse_players(raw.get("record_list_a")),
                        players_b=self._parse_players(raw.get("record_list_b")),
                    )
                )

        logger.debug(f"[LOL赛事] scoregg 详情 {match_id}：{len(games)} 局")
        if self._book is not None:
            await self._book.save()
        return MatchDetail(match=match, games=games)

    def _parse_players(self, raw: Any) -> list[PlayerStat]:
        if not isinstance(raw, list):
            return []
        out: list[PlayerStat] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            hero_id = str(item.get("heroID") or "")
            name = _champion_name(item.get("hero_image"))
            # 图标是纯数字说明源站没给英文名，但 heroID 是稳定的，
            # 记下来下次用同一编号的英雄就能反查出名字
            if self._book is not None and hero_id and name:
                self._book.learn(hero_id, name)
            out.append(
                PlayerStat(
                    nickname=str(item.get("player_nickname") or ""),
                    champion=name,
                    hero_id=hero_id,
                    kills=_int(item.get("kills")),
                    deaths=_int(item.get("deaths")),
                    assists=_int(item.get("assists")),
                    mvp=str(item.get("mvp")) == "1",
                )
            )
        return out
