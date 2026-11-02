"""内部数据模型。

所有数据源都必须先归一化成这里的结构，指令层与渲染层只认这些类。
换数据源时只改 provider，不动上层。

设计原则：**字段要么有值，要么是明确的空值**（``""`` / ``0`` / ``[]``），
不出现 ``None`` 到处漏的情况——否则渲染层要写一堆 ``or "未知"``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 赛事状态：0 未开始 / 1 进行中 / 2 已结束
STATUS_TEXT = ("未开始", "进行中", "已结束")
STATUS_ICON = ("🕐", "🔴", "✅")


def status_text(status: int) -> str:
    if 0 <= status < len(STATUS_TEXT):
        return STATUS_TEXT[status]
    return "未知"


def status_icon(status: int) -> str:
    if 0 <= status < len(STATUS_ICON):
        return STATUS_ICON[status]
    return "❔"


@dataclass(slots=True)
class Tournament:
    """一个联赛 / 杯赛。"""

    id: str = ""
    name: str = ""
    short_name: str = ""
    start_date: str = ""
    end_date: str = ""
    status: int = 0
    image: str = ""

    @property
    def title(self) -> str:
        return self.short_name or self.name or f"联赛 {self.id}"

    @property
    def period(self) -> str:
        if self.start_date and self.end_date:
            return f"{self.start_date} ~ {self.end_date}"
        return self.start_date or self.end_date or ""


@dataclass(slots=True)
class Match:
    """一场比赛（BO3/BO5 这个层级，不是其中某一局）。"""

    id: str = ""
    tournament_id: str = ""
    tournament_name: str = ""
    round_name: str = ""
    team_a: str = ""
    team_b: str = ""
    score_a: int = 0
    score_b: int = 0
    best_of: int = 0
    start_date: str = ""  # YYYY-MM-DD
    start_time: str = ""  # HH:MM
    status: int = 0
    winner: str = ""  # 胜方队名，未结束为空

    @property
    def day(self) -> str:
        return self.start_date

    @property
    def started(self) -> bool:
        return self.status >= 1

    @property
    def finished(self) -> bool:
        return self.status >= 2

    @property
    def score_line(self) -> str:
        if not self.started:
            return "vs"
        return f"{self.score_a} : {self.score_b}"

    @property
    def clock(self) -> str:
        return f"{self.start_date[5:]} {self.start_time}".strip()


@dataclass(slots=True)
class PlayerStat:
    nickname: str = ""
    champion: str = ""
    hero_id: str = ""  # 数据源自己的英雄编号，用来补全英文名缺失的情况
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    mvp: bool = False

    @property
    def kda(self) -> str:
        return f"{self.kills}/{self.deaths}/{self.assists}"


@dataclass(slots=True)
class Game:
    """BO 里的单局。"""

    index: int = 1
    win_side: str = ""  # "a" / "b"
    kills_a: int = 0
    kills_b: int = 0
    gold_a: int = 0
    gold_b: int = 0
    baron_a: int = 0
    baron_b: int = 0
    dragon_a: int = 0
    dragon_b: int = 0
    duration_min: int = 0
    players_a: list[PlayerStat] = field(default_factory=list)
    players_b: list[PlayerStat] = field(default_factory=list)

    @property
    def winner_text(self) -> str:
        return "A 方" if self.win_side == "a" else ("B 方" if self.win_side == "b" else "")


@dataclass(slots=True)
class MatchDetail:
    """单场比赛的完整信息：基本信息 + 逐局数据。"""

    match: Match = field(default_factory=Match)
    games: list[Game] = field(default_factory=list)
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class DaySchedule:
    """某一天的赛程：``day`` 是 ``YYYY-MM-DD``，按联赛分组的比赛列表。"""

    day: str = ""
    matches: list[Match] = field(default_factory=list)

    def group(self) -> dict[str, list[Match]]:
        grouped: dict[str, list[Match]] = {}
        for match in self.matches:
            grouped.setdefault(match.tournament_name or match.tournament_id, []).append(match)
        return grouped


class ProviderError(RuntimeError):
    """数据源不可用。``retryable`` 告诉上层要不要换备用源。"""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable
