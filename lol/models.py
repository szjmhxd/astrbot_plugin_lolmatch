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
    flag_a: str = ""  # 队标地址，卡片用；拿不到就画文字块
    flag_b: str = ""

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
class PerkSet:
    """一名选手的符文页。源站按「主系 + 4 枚 + 副系 + 2 枚」给。"""

    primary: str = ""  # 精密
    keystone: str = ""  # 征服者
    primary_slots: list[str] = field(default_factory=list)  # 凯旋 / 传说：欢欣 / 砍倒
    sub: str = ""  # 启迪
    sub_slots: list[str] = field(default_factory=list)  # 星界洞悉 / 神奇之鞋

    @property
    def ready(self) -> bool:
        return bool(self.keystone or self.primary)

    @property
    def short(self) -> str:
        """``精密·征服者 / 启迪·星界洞悉`` 这种紧凑写法，卡片和文字都用它。"""
        head = "·".join(part for part in (self.primary, self.keystone) if part)
        tail = self.sub
        if self.sub_slots:
            tail = f"{tail}·{self.sub_slots[0]}" if tail else self.sub_slots[0]
        return " / ".join(part for part in (head, tail) if part)

    @property
    def full(self) -> str:
        """带齐副系小符文的完整写法，给「详情」这种有版面的时候用。"""
        head = "·".join(part for part in (self.primary, self.keystone) if part)
        if self.primary_slots:
            head += f"（{'/'.join(self.primary_slots)}）"
        tail = self.sub
        if self.sub_slots:
            tail += f"（{'/'.join(self.sub_slots)}）"
        return " ｜ ".join(part for part in (head, tail) if part)


@dataclass(slots=True)
class PlayerStat:
    nickname: str = ""
    champion: str = ""
    hero_id: str = ""  # 数据源自己的英雄编号，用来补全英文名缺失的情况
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    mvp: bool = False
    # 下面这些只有取到逐局明细时才有值，渲染层按「有就显示」处理
    gold: int = 0
    cs: int = 0  # 补刀（hits）
    damage: int = 0  # 对英雄伤害
    damage_pct: float = 0.0  # 伤害占全队比例（0~100）
    part: float = 0.0  # 参团率（0~100）
    score: float = 0.0  # 数据源给的评分
    beiguo: bool = False  # 全场最差，源站叫「背锅」
    # 逐局明细里的更多维度
    hero_level: int = 0  # 英雄等级
    damage_taken: int = 0  # 承受伤害
    damage_taken_pct: float = 0.0
    gold_per_min: float = 0.0
    damage_per_min: float = 0.0
    items: list[str] = field(default_factory=list)  # 出装图标地址（源站只给图，没有道具名）
    spells: list[str] = field(default_factory=list)  # 召唤师技能中文名，闪现排在最前
    spell_icons: list[str] = field(default_factory=list)  # 与 spells 同序的图标地址
    hero_icon: str = ""  # 英雄头像地址，卡片用
    perks: PerkSet | None = None
    honor: str = ""  # 荣誉标记，源站给的是「3,5」这种编号
    player_id: str = ""

    @property
    def kda(self) -> str:
        return f"{self.kills}/{self.deaths}/{self.assists}"

    @property
    def kda_ratio(self) -> float:
        """KDA 比值，死亡 0 时按 1 算，避免除零。"""
        return (self.kills + self.assists) / max(1, self.deaths)


@dataclass(slots=True)
class DragonEvent:
    """一条龙（元素龙 / 大龙）被击杀的时刻。

    只有取到 LPL 官方直播数据（``lol/livedata``）的场次才有这串事件，
    所以整条时间线是**可选**的，没有就整块不画。

    ``kind`` 是源站自己的编号，只用来挑图标（``images/lol/dragon/icon1-{kind}.png``）。
    **不做中文翻译**：这个编号与英雄联盟官方的龙种编号对不上（源站前端那套
    iconfont 类名跟实际数据矛盾，比如 15 分钟就出现「远古龙」），
    猜错了比不写更糟，所以卡片上就是源站那张图。
    """

    second: int = 0
    clock: str = ""  # 06:07
    side: str = ""  # "a" / "b"，拿龙的一方
    kind: int = -1
    icon: str = ""


@dataclass(slots=True)
class TeamFight:
    """一次团战的起止时间，时间线中间那条轨用。"""

    begin: int = 0
    end: int = 0
    begin_clock: str = ""
    end_clock: str = ""

    @property
    def mid(self) -> int:
        return (self.begin + self.end) // 2


@dataclass(slots=True)
class H2HGame:
    """一场历史交手（整体场次，不是其中某一局）。"""

    date: str = ""  # 2026-07-12
    match_id: str = ""
    title: str = ""  # 2026 MSI季中赛-淘汰赛 07-12 14:00
    team_a: str = ""
    team_b: str = ""
    score_a: int = 0
    score_b: int = 0

    @property
    def winner(self) -> str:
        if self.score_a > self.score_b:
            return self.team_a
        if self.score_b > self.score_a:
            return self.team_b
        return ""

    @property
    def score_line(self) -> str:
        return f"{self.team_a} {self.score_a}:{self.score_b} {self.team_b}"

    @property
    def event(self) -> str:
        """赛事名。标题形如「2026 MSI季中赛-淘汰赛 07-12 14:00」，砍掉尾部日期。"""
        parts = self.title.rsplit(" ", 2)
        if len(parts) == 3 and ":" in parts[2]:
            return parts[0] or "—"
        return self.title or "—"


@dataclass(slots=True)
class HeadToHead:
    """两队的交手记录。

    ``wins_a`` / ``wins_b`` 已经统一到**当前这场比赛**的 a / b 侧——
    源站每条记录的 a/b 是按那场比赛自己的主客排的，不归一化就会出现
    「同一支队伍一会儿 a 一会儿 b」的混乱。
    """

    team_a: str = ""
    team_b: str = ""
    wins_a: int = 0
    wins_b: int = 0
    games: list[H2HGame] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.games) or self.wins_a or self.wins_b

    @property
    def score_line(self) -> str:
        return f"{self.wins_a} : {self.wins_b}"

    @property
    def count(self) -> int:
        return len(self.games) or (self.wins_a + self.wins_b)

    def recent(self, limit: int = 3) -> list[H2HGame]:
        return self.games[:limit] if limit > 0 else self.games


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
    # 逐局明细（没有时留空，渲染层跳过这些行）
    duration_sec: int = 0
    sides: str = ""  # 如「蓝 BLG ｜ 红 HLE」，缺失为空
    tower_a: int = 0
    tower_b: int = 0
    herald_a: int = 0
    herald_b: int = 0
    grub_a: int = 0
    grub_b: int = 0
    elder_a: int = 0
    elder_b: int = 0
    damage_a: int = 0
    damage_b: int = 0
    bans_a: list[str] = field(default_factory=list)
    bans_b: list[str] = field(default_factory=list)
    # 一血 / 一塔 / 一龙 / 首条大龙 / 首个先锋，值为 "a" / "b" / ""
    first_blood: str = ""
    first_tower: str = ""
    first_dragon: str = ""
    first_baron: str = ""
    first_herald: str = ""
    flag_a: str = ""
    flag_b: str = ""
    detailed: bool = False  # 是否取到了逐局明细
    # 时间线（只有 LPL 官方直播数据有的场次才有，没有就是空列表）
    dragons: list[DragonEvent] = field(default_factory=list)
    fights: list[TeamFight] = field(default_factory=list)

    @property
    def timeline_ready(self) -> bool:
        return bool(self.dragons or self.fights)

    @property
    def winner_text(self) -> str:
        return "A 方" if self.win_side == "a" else ("B 方" if self.win_side == "b" else "")

    @property
    def clock(self) -> str:
        """时长，``41:51`` 这种。没有秒数就退化成 ``41 分钟``。"""
        if self.duration_sec:
            return f"{self.duration_sec // 60}:{self.duration_sec % 60:02d}"
        return f"{self.duration_min} 分钟" if self.duration_min else ""

    @property
    def enriched(self) -> bool:
        """是否取到了逐局明细（BP、推塔、选手经济等）。"""
        return self.detailed


@dataclass(slots=True)
class MatchDetail:
    """单场比赛的完整信息：基本信息 + 逐局数据。"""

    match: Match = field(default_factory=Match)
    games: list[Game] = field(default_factory=list)
    extra: dict[str, str] = field(default_factory=dict)
    h2h: HeadToHead | None = None


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
