"""纯文本渲染。

刻意不做图片：赛程这类信息的价值在「什么时候、谁打谁、几比几」，
画成图只是把一个请求变成一次渲染 + 一次上传，还多一个外链图片失效的风险。
需要图片卡片的话，把这里的函数换成 HTML 模板即可，数据层不用动。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable

from .models import Match, MatchDetail, Tournament, status_icon, status_text
from .windows import window_text

_Champ = Callable[[str, str], str]


def _pad(text: str, width: int) -> str:
    """按**显示宽度**补空格。

    ``str.ljust`` 数的是字符数，一个汉字和一个字母都算 1，中文一多就歪。
    这里把全角/宽字符算 2，等宽字体下才能真正对齐。
    """
    used = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)
    return text + " " * max(0, width - used)


def _champ_default(hero_id: str, token: str) -> str:
    return token or "—"


def _gold(value: int) -> str:
    """78999 → 79.0k。"""
    if value <= 0:
        return "0"
    return f"{value / 1000:.1f}k"


def _line(match: Match, *, show_tournament: bool = False) -> str:
    icon = status_icon(match.status)
    clock = match.start_time or "--:--"
    if match.started:
        score = f"{match.team_a} {match.score_line} {match.team_b}"
    else:
        score = f"{match.team_a} vs {match.team_b}"
    extra = f"BO{match.best_of}" if match.best_of else ""
    if match.finished and match.winner:
        extra = f"{extra} · {match.winner} 胜".strip(" ·")
    tail = f"  {extra}" if extra else ""
    head = f"{icon} {clock}  {score}"
    if show_tournament and match.tournament_name:
        head = f"{icon} {clock}  [{match.tournament_name}] {score}"
    return f"{head}{tail}"


# ---------------------------------------------------------------------- 赛程


def day_report(
    day: str,
    matches: list[Match],
    *,
    title: str = "",
    limit: int = 20,
    source: str = "",
) -> str:
    """一天的赛程。``day`` 是 ``YYYY-MM-DD``。"""
    head = title or f"{day[5:]} 赛程"
    if not matches:
        return f"📭 {head}\n\n这一天没有比赛。"

    shown = matches[:limit] if limit > 0 else matches
    grouped: dict[str, list[Match]] = {}
    for match in shown:
        grouped.setdefault(match.tournament_name or "未知联赛", []).append(match)

    lines = [f"🔥 LOL {head}", ""]
    for tournament, items in grouped.items():
        lines.append(f"【{tournament}】")
        lines.extend(f"  {_line(match)}" for match in items)
        lines.append("")

    if len(matches) > len(shown):
        lines.append(f"… 还有 {len(matches) - len(shown)} 场未显示")
    footer = f"共 {len(matches)} 场"
    if source:
        footer += f" · 数据源 {source}"
    lines.append(footer)
    return "\n".join(lines).strip()


def week_report(matches: list[Match], *, days: int = 7, limit: int = 20, source: str = "") -> str:
    """未来 N 天。按日期分段。"""
    if not matches:
        return "📭 近期没有比赛。"

    days_seen: list[str] = []
    for match in matches:
        if match.start_date and match.start_date not in days_seen:
            days_seen.append(match.start_date)
    days_seen.sort()
    keep = set(days_seen[:days])

    lines = ["🗓 LOL 近期赛程", ""]
    count = 0
    for day in days_seen[:days]:
        items = [m for m in matches if m.start_date == day][:limit]
        count += len(items)
        lines.append(f"—— {day[5:]} ——")
        lines.extend(f"  {_line(m)}" for m in items)
        lines.append("")

    omitted = [m for m in matches if m.start_date not in keep]
    if omitted:
        lines.append(f"… 另有 {len(omitted)} 场在 {days} 天之后")
    footer = f"共 {count} 场"
    if source:
        footer += f" · 数据源 {source}"
    lines.append(footer)
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------- 详情


def detail_report(detail: MatchDetail, *, champ: _Champ = _champ_default, source: str = "") -> str:
    match = detail.match
    lines: list[str] = []

    title = f"📋 {match.id}"
    if match.tournament_name:
        title += f" · {match.tournament_name}"
    if match.round_name:
        title += f" · {match.round_name}"
    lines.append(title)

    state = f"{status_icon(match.status)} {status_text(match.status)}"
    if match.best_of:
        state += f" · BO{match.best_of}"
    when = match.clock or ""
    lines.append(f"{match.team_a} {match.score_line} {match.team_b}　{state}")
    if when:
        lines.append(when)
    lines.append("")

    if not detail.games:
        note = detail.extra.get("note") or "这场比赛还没有逐局数据。"
        lines.append(note)
        if source:
            lines.append(f"\n数据源 {source}")
        return "\n".join(lines).strip()

    for game in detail.games:
        winner = {"a": match.team_a, "b": match.team_b}.get(game.win_side, "")
        head = f"第 {game.index} 局"
        if winner:
            head += f" · {winner} 胜"
        if game.duration_min:
            head += f" · {game.duration_min} 分钟"
        lines.append(head)
        lines.append(
            f"  击杀 {game.kills_a} : {game.kills_b}　"
            f"经济 {_gold(game.gold_a)} : {_gold(game.gold_b)}"
        )
        lines.append(
            f"  大龙 {game.baron_a} : {game.baron_b}　小龙 {game.dragon_a} : {game.dragon_b}"
        )
        for squad, label in (
            (game.players_a, match.team_a),
            (game.players_b, match.team_b),
        ):
            if not squad:
                continue
            lines.append(f"  {label}")
            for player in squad:
                name = _pad(player.nickname, 12)
                hero = _pad(champ(player.hero_id, player.champion), 12)
                flag = " ★MVP" if player.mvp else ""
                lines.append(f"    {name}{hero}{player.kda}{flag}")
        lines.append("")

    if source:
        lines.append(f"数据源 {source}")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------- 联赛


def tournament_list(tournaments: list[Tournament], *, source: str = "") -> str:
    if not tournaments:
        return "📭 暂时没有进行中或即将开始的联赛。"
    lines = ["🏆 联赛列表（用 ID 订阅或查详情）", ""]
    for index, item in enumerate(tournaments, start=1):
        lines.append(f"{index:>2}. [{item.id}] {item.title}　{status_text(item.status)}")
        if item.period:
            lines.append(f"    {item.period}")
    lines.append("")
    lines.append("示例：/lol 订阅 1023　/lol 联赛详情 1023")
    if source:
        lines.append(f"数据源 {source}")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------- 帮助


def help_text(prefix: str = "/lol") -> str:
    return "\n".join(
        [
            "🎮 LOL 赛事助手",
            "",
            "【查询】",
            f"{prefix} 今日        今天的比赛",
            f"{prefix} 本周        未来 7 天",
            f"{prefix} 联赛        进行中/即将开始的联赛与 ID",
            f"{prefix} 联赛详情 <ID>   该联赛近期场次",
            f"{prefix} 详情 <比赛ID>   单场逐局数据（KDA/经济/大小龙/选手）",
            "",
            "【订阅推送】",
            f"{prefix} 订阅 <联赛ID>     每晚推送当日战报 + 次日赛程",
            f"{prefix} 取消订阅 <联赛ID>",
            f"{prefix} 订阅列表",
            f"{prefix} 测试推送           立刻发一条，验证 @ 和排版",
            "",
            "【提醒设置】",
            f"{prefix} 提醒                 看当前设置",
            f"{prefix} 提醒 全体            推送时 @全体（需机器人有群管理权限）",
            f"{prefix} 提醒 关              不 @ 任何人",
            f"{prefix} 提醒 加 @某人         把他加入 @ 名单",
            f"{prefix} 提醒 删 @某人",
            f"{prefix} 提醒 列表            看 @ 名单",
            f"{prefix} 提醒 时段 加 工作日 09:00-23:30",
            f"{prefix} 提醒 时段 列表 / 删 <序号> / 清",
            f"{prefix} 提醒 时间 23:00       每晚推送时刻",
            "",
            "【数据源】",
            f"{prefix} 设置 源 scoregg|pandascore",
            f"{prefix} 设置 key <PandaScore Key>    只对当前会话生效",
            f"{prefix} 设置 清key",
            f"{prefix} 状态                 当前会话设置与数据源状态",
            "",
            "时段支持「工作日 09:00-18:00」「休息日 10:00-22:00」「22:00-06:00」跨零点。",
            "工作时间判断含法定节假日与调休。",
        ]
    )


def settings_report(
    *,
    provider: str,
    provider_default: str,
    mention: str,
    mention_default: str,
    targets: list[str],
    windows: list[dict],
    windows_default: list[dict],
    push_time: str,
    push_time_default: str,
    subs: list[str],
    has_token: bool,
    workday_status: str,
    prefix: str = "/lol",
) -> str:
    def inherited(mine: bool, default: str) -> str:
        """会话没设置时标一句「默认是什么」，方便一眼看出从哪继承。"""
        return "" if mine else f"（默认 {default}）"

    lines = [
        "⚙️ 当前会话设置",
        "",
        f"数据源：{provider}{inherited(provider != provider_default, provider_default)}",
        f"PandaScore Key：{'已设置' if has_token else '未设置'}",
        f"推送时间：{push_time}{inherited(not push_time, push_time_default)}",
        f"可提醒时段：{window_text(windows)}{inherited(not windows, window_text(windows_default))}",
        f"提醒方式：{mention}{inherited(not mention, mention_default)}",
    ]
    if targets:
        lines.append(f"@ 名单：{'、'.join(targets)}")
    lines.append(f"已订阅：{('、'.join(subs)) if subs else '无'}")
    lines.append("")
    lines.append(f"工作日日历：{workday_status}")
    lines.append(f"改设置看 {prefix} 帮助")
    return "\n".join(lines)


# ---------------------------------------------------------------------- 推送


def push_report(
    today: str,
    tomorrow: str,
    results: list[Match],
    upcoming: list[Match],
    *,
    source: str = "",
) -> str:
    """每晚那条推送：今日战果 + 明日赛程。只包含已订阅联赛。"""
    lines = [f"📢 LOL 每日战报 · {today[5:]}", ""]

    if results:
        lines.append("【今日战果】")
        lines.extend(f"  {_line(match)}" for match in results)
    else:
        lines.append("【今日战果】")
        lines.append("  今天订阅的联赛没有比赛。")
    lines.append("")

    lines.append(f"【明日赛程 · {tomorrow[5:]}】")
    if upcoming:
        lines.extend(f"  {_line(match)}" for match in upcoming)
    else:
        lines.append("  明天订阅的联赛没有排期。")

    if source:
        lines.append("")
        lines.append(f"数据源 {source}")
    return "\n".join(lines).strip()
