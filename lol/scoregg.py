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

# 逐局明细单独放在 CDN 上，跟 api_url.php 不是一个域名，也不需要任何参数：
# BP 禁选、推塔/先锋/巢虫/远古龙、每名选手的经济/补刀/伤害占比/参团率都在里面。
GAME_STATS_API = "https://img.scoregg.com/match/result/{}.json"

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


def _num(value: Any) -> int:
    """``"14.2k"`` / ``"1.2M"`` / ``"280"`` → 整数。逐局明细里的数字都带单位。"""
    raw = str(value or "").strip()
    if not raw:
        return 0
    scale = 1
    if raw[-1] in "kK":
        scale, raw = 1_000, raw[:-1]
    elif raw[-1] in "mM":
        scale, raw = 1_000_000, raw[:-1]
    try:
        return round(float(raw) * scale)
    except ValueError:
        return 0


def _float(value: Any) -> float:
    try:
        return float(str(value or "").strip())
    except ValueError:
        return 0.0


def _champion_token(url: Any) -> str:
    """从图标地址取文件名（去扩展名）。数字编号保留，禁选里就是纯数字。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    tail = raw.rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[0] if "." in tail else tail


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
        result_ids: list[str] = []
        results = data.get("result_list")
        if isinstance(results, list):
            match_team_a = str(data.get("teamID_a") or "")
            match_team_b = str(data.get("teamID_b") or "")
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
                result_ids.append(str(raw.get("resultID") or ""))

            # 逐局明细（BP、推塔、选手经济伤害）要再打一次 CDN，一局一个请求。
            # BO5 也就五个，直接并发取；取不到的那局保留上面的精简数据。
            if any(result_ids):
                blobs = await asyncio.gather(
                    *(self._game_stats(rid) for rid in result_ids),
                    return_exceptions=True,
                )
                pairs = [
                    (game, blob)
                    for game, blob in zip(games, blobs, strict=False)
                    if isinstance(blob, dict) and blob
                ]
                for game, blob in pairs:
                    self._enrich(game, blob, match_team_a, match_team_b)
                # 禁选要用「源站编号 → 英雄名」，而编号是靠这一场的选手数据学的，
                # 所以必须等所有局都学完再解析，否则先解析的那局全是「？」。
                for game, blob in pairs:
                    self._fill_bans(game, blob, match_team_a, match_team_b)

        logger.debug(f"[LOL赛事] scoregg 详情 {match_id}：{len(games)} 局")
        if self._book is not None:
            await self._book.save()
        return MatchDetail(match=match, games=games)

    async def _game_stats(self, result_id: str) -> dict[str, Any]:
        """取单局明细。失败一律返回空 dict，让上层退回精简统计，不丢整场。

        注意这个端点不在 ``api_url.php`` 后面，是静态 CDN，所以也不需要
        ``api_path`` 那套参数，直接 GET 就行。
        """
        if not result_id or result_id == "0":
            return {}
        session = await self._http()
        url = GAME_STATS_API.format(result_id)
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.debug(f"[LOL赛事] 逐局明细 {result_id} 返回 HTTP {resp.status}")
                    return {}
                body = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            logger.debug(f"[LOL赛事] 逐局明细 {result_id} 取不到：{type(exc).__name__}: {exc}")
            return {}
        if not isinstance(body, dict) or str(body.get("code")) != "200":
            return {}
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def _enrich(
        self,
        game: Game,
        blob: dict[str, Any],
        match_team_a: str,
        match_team_b: str,
    ) -> None:
        """把 CDN 上的逐局明细合并进 :class:`Game`。

        明细按**红蓝方**组织（``red_*`` / ``blue_*``），而模型统一用 a/b 侧，
        所以先确认 a 侧是红还是蓝，之后所有字段都按这个映射取。
        """
        raw = blob.get("result_list")
        if not isinstance(raw, dict):
            return

        red_name = str(raw.get("red_name") or "")
        blue_name = str(raw.get("blue_name") or "")
        a_side, b_side = self._sides(raw, match_team_a, match_team_b)

        def side_of(side: str) -> str:
            return "a" if side == a_side else "b"

        def pick(side: str, key: str) -> Any:
            return raw.get(f"{side}_{key}")

        # --- 局面信息
        minutes = _int(raw.get("game_time_m"))
        seconds = _int(raw.get("game_time_s"))
        if minutes or seconds:
            game.duration_sec = minutes * 60 + seconds
            game.duration_min = minutes
        if blue_name or red_name:
            game.sides = f"蓝 {blue_name or '—'} ｜ 红 {red_name or '—'}"

        # --- 双方统计
        for side in (a_side, b_side):
            slot = side_of(side)
            won = str(pick(side, "result") or "") == "1"
            if won:
                game.win_side = slot
            setattr(game, f"kills_{slot}", _int(pick(side, "kill")))
            setattr(game, f"gold_{slot}", _int(pick(side, "money")))
            setattr(game, f"dragon_{slot}", _int(pick(side, "small_dargon")))
            setattr(game, f"baron_{slot}", _int(pick(side, "big_dargon")))
            setattr(game, f"tower_{slot}", _int(pick(side, "tower")))
            setattr(game, f"herald_{slot}", _int(pick(side, "riftHeraldKills")))
            setattr(game, f"grub_{slot}", _int(pick(side, "void_grub")))
            setattr(game, f"elder_{slot}", _int(pick(side, "elder")))
            setattr(game, f"damage_{slot}", _int(pick(side, "attack")))

            players = self._parse_star_players(raw, side)
            if players:
                setattr(game, f"players_{slot}", players)

        game.detailed = True

    @staticmethod
    def _sides(raw: dict[str, Any], match_team_a: str, match_team_b: str) -> tuple[str, str]:
        """确认「a 侧」是红方还是蓝方。明细里所有字段都按这个映射取。"""
        red_id = str(raw.get("red_teamID") or "")
        if match_team_a:
            a_side = "red" if red_id and red_id == match_team_a else "blue"
        elif match_team_b and red_id == match_team_b:
            a_side = "blue"
        else:
            a_side = "red"
        return a_side, ("blue" if a_side == "red" else "red")

    def _fill_bans(
        self,
        game: Game,
        blob: dict[str, Any],
        match_team_a: str,
        match_team_b: str,
    ) -> None:
        """等所有局的选手都学完，再解析禁选（编号靠选手数据反查）。"""
        raw = blob.get("result_list")
        if not isinstance(raw, dict):
            return
        a_side, b_side = self._sides(raw, match_team_a, match_team_b)
        game.bans_a = self._parse_bans(raw, a_side)
        game.bans_b = self._parse_bans(raw, b_side)

    def _parse_bans(self, raw: dict[str, Any], side: str) -> list[str]:
        """禁选只给了图标地址，而且编号分两套，必须按目录区分：

        - ``.../champion/Rumble.png``：英文 ID，最稳；
        - ``.../champion/84.png``：Data Dragon 的数字 key；
        - ``.../champion_new/101.png``：**源站自己的**英雄编号。

        后两者混着查会认错英雄（同一个 101，一边是辛德拉一边是卡尔玛）。
        源站编号只能靠平时打比赛时从选手数据里学，学不到就先不显示。
        """
        if self._book is None:
            return []
        out: list[str] = []
        for i in range(1, 6):
            url = str(raw.get(f"{side}_ban_{i}") or "")
            token = _champion_token(url)
            if not token:
                continue
            if not token.isdigit():
                name = self._book.name(token)
            elif "champion_new" in url:
                name = self._book.by_source(token)
            else:
                name = self._book.by_ddragon(token)
            # 认不出来留空占位，渲染成「？」，这样 5 个 Ban 的位置不会错位
            out.append(name or "")
        return out

    def _parse_star_players(self, raw: dict[str, Any], side: str) -> list[PlayerStat]:
        """解析 ``{side}_star_{a..e}_*`` 五名选手。

        这里同时把「源站英雄编号 → 中文名」记下来，禁选那边要靠它认英雄。
        """
        out: list[PlayerStat] = []
        for i, slot in enumerate("abcde", start=1):
            prefix = f"{side}_star_{slot}_"
            nickname = str(raw.get(f"{prefix}name") or "").strip()
            hero_id = str(raw.get(f"{side}_hero_{slot}_heroID") or "").strip()
            champion = str(raw.get(f"{side}_hero_{slot}_name") or "").strip()
            if not champion:
                # 中文名缺失时用选人图标兜底（英文名，渲染层会翻成中文）
                champion = _champion_name(raw.get(f"{side}_pick_{i}"))
            if not (nickname or champion):
                continue
            if self._book is not None and hero_id and champion:
                self._book.learn(hero_id, champion)
            out.append(
                PlayerStat(
                    nickname=nickname,
                    champion=champion,
                    hero_id=hero_id,
                    kills=_int(raw.get(f"{prefix}kills")),
                    deaths=_int(raw.get(f"{prefix}deaths")),
                    assists=_int(raw.get(f"{prefix}assists")),
                    mvp=str(raw.get(f"{prefix}mvp") or "") == "1",
                    gold=_num(raw.get(f"{prefix}money")),
                    cs=_int(raw.get(f"{prefix}hits")),
                    damage=_int(raw.get(f"{prefix}atk_o")),
                    damage_pct=_float(raw.get(f"{prefix}atk_p")),
                    part=_float(raw.get(f"{prefix}part")),
                    score=_float(raw.get(f"{prefix}score")),
                    beiguo=str(raw.get(f"{prefix}beiguo") or "") == "1",
                )
            )
        return out

    def _parse_players(self, raw: Any) -> list[PlayerStat]:
        if not isinstance(raw, list):
            return []
        out: list[PlayerStat] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            hero_id = str(item.get("heroID") or "")
            name = _champion_name(item.get("hero_image"))
            if not name and self._book is not None and hero_id:
                # 图标是纯数字编号时，用平时学到的「编号 → 英雄名」补上
                name = self._book.by_source(hero_id)
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
