"""纯文本渲染。

刻意不做图片：赛程这类信息的价值在「什么时候、谁打谁、几比几」，
画成图只是把一个请求变成一次渲染 + 一次上传，还多一个外链图片失效的风险。
需要图片卡片的话，把这里的函数换成 HTML 模板即可，数据层不用动。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable

from .models import (
    FORMAT_DOUBLE,
    FORMAT_SINGLE,
    Game,
    Match,
    MatchDetail,
    PlayerStat,
    Tournament,
    bracket_columns,
    bracket_rounds,
    day_label,
    detect_format,
    double_elim_split,
    stage_groups,
    status_icon,
    status_text,
    swiss_ready,
    swiss_rows,
    usage_hint,
)
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


def _line(
    match: Match,
    *,
    show_tournament: bool = False,
    show_date: bool = False,
    show_id: bool = False,
) -> str:
    icon = status_icon(match.status)
    clock = match.start_time or "--:--"
    if show_date and match.start_date:
        # 只有跨天的列表才需要日期，否则每一行都在重复标题里的那天
        clock = f"{match.start_date[5:]} {clock}"
    if match.started:
        score = f"{match.name_a} {match.score_line} {match.name_b}"
    else:
        score = f"{match.name_a} vs {match.name_b}"
    extra = f"BO{match.best_of}" if match.best_of else ""
    if match.finished and match.winner:
        extra = f"{extra} · {match.winner} 胜".strip(" ·")
    if show_id and match.id:
        extra = f"{extra}  #{match.id}".strip()
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
        lines.extend(f"  {_line(match, show_id=True)}" for match in items)
        lines.append("")

    if len(matches) > len(shown):
        lines.append(f"… 还有 {len(matches) - len(shown)} 场未显示")
    footer = f"共 {len(matches)} 场"
    if source:
        footer += f" · 数据源 {source}"
    lines.append(footer)
    hint = usage_hint(shown[0].tournament_id, shown[0].id) if shown else ""
    if hint:
        lines.append(f"💡 {hint}")
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
        lines.append(f"—— {day_label(day)} ——")
        lines.extend(f"  {_line(m, show_id=True)}" for m in items)
        lines.append("")

    omitted = [m for m in matches if m.start_date not in keep]
    if omitted:
        lines.append(f"… 另有 {len(omitted)} 场在 {days} 天之后")
    footer = f"共 {count} 场"
    if source:
        footer += f" · 数据源 {source}"
    lines.append(footer)
    hint = usage_hint(matches[0].tournament_id, matches[0].id) if matches else ""
    if hint:
        lines.append(f"💡 {hint}")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------- 联赛详情


def _match_text(match: Match) -> str:
    head = f"{match.start_date[5:]} " if match.start_date else ""
    if match.started:
        return f"{head}{match.name_a} {match.score_line} {match.name_b}".strip()
    return f"{head}{match.name_a} vs {match.name_b}".strip()


def bracket_lines(matches: list[Match]) -> list[str]:
    """晋级图的纯文字版：能成树的赛段一列一行，瑞士轮改成出线/淘汰名单。

    图形版是主菜，但这几行要能独立看懂——没装字体、或者客户端吞图的时候，
    它是唯一的替代。所以双败这里也不套「第 N 轮」：胜者组、败者组、总决赛
    三条轨道分开写，和图上看到的结构一一对应。
    """
    lines: list[str] = []
    for stage, items in stage_groups(matches):
        fmt = detect_format(items)

        if fmt == FORMAT_DOUBLE:
            layout = double_elim_split(items)
            if not layout.ready:
                continue
            lines.append(f"【双败 · {stage}】")
            if layout.winner:
                lines.append("  胜者组　" + "｜".join(_match_text(m) for m in layout.winner))
            if layout.loser:
                lines.append("  败者组　" + "｜".join(_match_text(m) for m in layout.loser))
            if layout.final:
                final = layout.final
                tail = ""
                if final.finished:
                    tail = f"　→ {final.winner} 夺冠"
                lines.append("  总决赛　" + _match_text(final) + tail)
            continue

        columns = bracket_columns(items)
        if columns:
            lines.append(f"【{'单败' if fmt == FORMAT_SINGLE else '晋级'} · {stage}】")
            for label, column in zip(bracket_rounds(columns), columns, strict=True):
                body = "｜".join(_match_text(m) for m in column)
                lines.append(f"  {label}　{body}")
            continue

        # 瑞士轮画不成树，但「谁出线」照样是能算出来的事实
        rows = swiss_rows(items)
        if not swiss_ready(rows):
            continue
        lines.append(f"【晋级形势 · {stage}】")
        for key, mark in (
            ("qualified", "✅ 出线"),
            ("playing", "⏳ 在争"),
            ("eliminated", "❌ 淘汰"),
        ):
            picked = [row for row in rows if row.state == key]
            if not picked:
                continue
            body = "、".join(f"{row.team} {row.record}" for row in picked)
            lines.append(f"  {mark}　{body}")
    return lines


def tournament_report(
    name: str,
    matches: list[Match],
    *,
    source: str = "",
    limit: int = 40,
    prefix: str = "/lol",
) -> str:
    """联赛详情：阶段 → 日期铺开，每行带比赛 ID，方便直接接「详情」。

    一个联赛动辄上百场，所以 ``limit`` 封顶；超出的部分只在末尾提一句，
    而不是把整屏刷满。``limit <= 0`` 表示不封顶——调用方要全量时用它，
    否则会被误当成「一场都不给」。
    """
    head = f"🏆 LOL 联赛详情 · {name}" if name else "🏆 LOL 联赛详情"
    if not matches:
        return f"{head}\n\n这个联赛还没有排期数据。"

    limit = limit if limit > 0 else len(matches)
    days = sorted({m.start_date for m in matches if m.start_date})
    lines = [head]
    meta = []
    if len(days) > 1:
        meta.append(f"{days[0][5:]} ~ {days[-1][5:]}")
    elif days:
        meta.append(days[0][5:])
    meta.append(f"共 {len(matches)} 场")
    stages = stage_groups(matches)
    if len(stages) > 1:
        meta.append("／".join(label for label, _ in stages))
    lines.append("　".join(meta))
    lines.append("")

    shown = 0
    for label, items in stages:
        block: list[str] = [f"【{label}】"]
        day = ""
        for match in items:
            if shown >= limit:
                break
            if match.start_date != day:
                day = match.start_date
                block.append(f"  {day_label(day)}")
            block.append(f"    {_line(match, show_id=True)}")
            shown += 1
        lines.extend(block)
        if shown >= limit:
            break

    lines.append("")
    if shown < len(matches):
        lines.append(f"… 还有 {len(matches) - shown} 场未显示（共 {len(matches)} 场）")
    bracket = bracket_lines(matches)
    if bracket:
        lines.extend(bracket)
        lines.append("")
    hint = usage_hint(matches[0].tournament_id, matches[0].id) if matches else ""
    if hint:
        lines.append(f"💡 {hint}")
    if source:
        lines.append(f"数据源 {source}")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------- 详情


def detail_report(
    detail: MatchDetail,
    *,
    champ: _Champ = _champ_default,
    source: str = "",
    only: int = 0,
    full: bool = False,
    prefix: str = "/lol",
) -> str:
    """单场详情。``only`` 大于 0 时只看那一局（BO5 时避免刷屏）。

    ``full`` 控制选手表的详细程度：关掉时只给 KDA / 经济 / 伤害占比，
    打开时才逐人附上召唤师技能、符文、补刀与参团率——一局十个人，
    全打开大概 60 行，够塞满一屏，所以默认关。
    """
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

    h2h = _h2h_block(detail)
    if h2h:
        lines.extend(h2h)

    if not detail.games:
        note = detail.extra.get("note") or "这场比赛还没有逐局数据。"
        lines.append(note)
        if source:
            lines.append(f"\n数据源 {source}")
        lines.append(f"💡 {usage_hint(match.tournament_id, match.id)}")
        return "\n".join(lines).strip()

    games = [g for g in detail.games if not only or g.index == only]
    if not games:
        lines.append(f"这场 BO{match.best_of or len(detail.games)} 没有第 {only} 局。")
        lines.append(f"💡 {usage_hint(match.tournament_id, match.id)}")
        return "\n".join(lines).strip()

    # 选手表只留给最后一局：一场 BO5 若每局都列十个人，光选手就 100 行，
    # 群里没人翻到底。前几局给资源对比，想细看某一局就带局号查。
    detail_index = games[-1].index
    for game in games:
        show_squad = game.index == detail_index
        lines.extend(
            _game_block(
                game,
                match,
                champ,
                full=full and show_squad,
                squads=show_squad,
            )
        )
        lines.append("")

    if not only and len(games) > 1:
        lines.append(f"… 选手数据只列了第 {detail_index} 局。")
        lines.append(f"看其他局：{prefix} 详情 {match.id} <局号>")

    if source:
        lines.append(f"数据源 {source}")
    lines.append(f"💡 {usage_hint(match.tournament_id, match.id)}")
    return "\n".join(lines).strip()


def _game_block(
    game: Game,
    match: Match,
    champ: _Champ,
    *,
    full: bool = False,
    squads: bool = True,
) -> list[str]:
    """一局的完整呈现：局面、双方资源、里程碑、BP、双方选手。"""
    winner = {"a": match.team_a, "b": match.team_b}.get(game.win_side, "")
    head = f"第 {game.index} 局"
    if winner:
        head += f" · {winner} 胜"
    if game.clock:
        head += f" · {game.clock}"
    lines = [head]
    if game.sides:
        lines.append(f"  {game.sides}")

    stats = [f"击杀 {game.kills_a} : {game.kills_b}"]
    if game.enriched:  # 没取到逐局明细时推塔是 0，不能当成真 0 显示
        stats.append(f"推塔 {game.tower_a} : {game.tower_b}")
    stats.append(f"经济 {_gold(game.gold_a)} : {_gold(game.gold_b)}")
    lines.append("  " + "　".join(stats))

    objectives = [
        f"小龙 {game.dragon_a} : {game.dragon_b}",
        f"大龙 {game.baron_a} : {game.baron_b}",
    ]
    # 这些只有逐局明细里才有，全是 0 就不占版面
    if game.herald_a or game.herald_b:
        objectives.append(f"先锋 {game.herald_a} : {game.herald_b}")
    if game.grub_a or game.grub_b:
        objectives.append(f"巢虫 {game.grub_a} : {game.grub_b}")
    if game.elder_a or game.elder_b:
        objectives.append(f"远古 {game.elder_a} : {game.elder_b}")
    lines.append("  " + "　".join(objectives))

    first = _first_line(game, match)
    if first:
        lines.append(f"  {first}")

    lines.extend(_timeline_lines(game, match))

    for bans, label in ((game.bans_a, match.team_a), (game.bans_b, match.team_b)):
        # 认出来的太少就整行不显示——「？ ？ ？」在群里只是噪音；
        # 认出一部分才用「？」占位，保证五个 Ban 的位置不错位
        if sum(1 for name in bans if name) < 3:
            continue
        lines.append(f"  禁 {label}：{' '.join(name or '？' for name in bans)}")

    for squad, label in (
        (game.players_a, match.team_a),
        (game.players_b, match.team_b),
    ):
        if not squads or not squad:
            continue
        lines.extend(_squad_block(squad, label, champ, full=full))
    return lines


def _h2h_block(detail: MatchDetail) -> list[str]:
    """历史交手段。只列最近 3 场——群里没人要翻完 6 场。"""
    h2h = detail.h2h
    if h2h is None or not h2h.ready:
        return []
    lines = [f"【交手】{h2h.team_a} {h2h.score_line} {h2h.team_b}（近 {h2h.count} 场）"]
    for game in h2h.recent(3):
        lines.append(f"  {_pad(game.date[5:], 6)}{_pad(game.score_line, 16)}{game.event}")
    lines.append("")
    return lines


def _timeline_lines(game: Game, match: Match) -> list[str]:
    """小龙 / 大龙击杀时刻，按队伍分行。

    源站只给「几分几秒、哪一方拿的」，种类编号与官方对不上，所以这里只写时刻、
    不猜龙种；卡片上才有图标。拿不到直播数据的局整块不显示，不占版面。
    """
    if not game.timeline_ready:
        return []
    out: list[str] = []
    for side in ("a", "b"):
        events = [event for event in game.dragons if event.side == side]
        if not events:
            continue
        team = (match.team_a if side == "a" else match.team_b) or "—"
        head = "龙线" if not out else "　　"
        out.append(f"  {head} {_pad(team, 8)}{' '.join(event.clock for event in events)}")
    if game.fights:
        tail = f"　团战 {len(game.fights)} 次"
        if out:
            out[-1] += tail
        else:
            out.append(f"  {tail.strip()}")
    return out


def _first_line(game: Game, match: Match) -> str:
    """「一血 X　一塔 Y」这种里程碑行，一条都没有就返回空串。"""
    parts: list[str] = []
    for attribute, label in (
        ("first_blood", "一血"),
        ("first_tower", "一塔"),
        ("first_dragon", "一龙"),
        ("first_herald", "首先锋"),
        ("first_baron", "首大龙"),
    ):
        side = getattr(game, attribute, "")
        if side:
            parts.append(f"{label} {match.team_a if side == 'a' else match.team_b}")
    return "　".join(parts)


def _squad_block(squad: list[PlayerStat], label: str, champ: _Champ, *, full: bool) -> list[str]:
    """一支队伍的五人表。``full`` 时每人多一行技能 / 符文 / 出装。"""
    rich = any(player.gold or player.damage_pct for player in squad)
    lines = [f"  {label}"]
    header = "    " + _pad("选手", 13) + _pad("英雄", 13) + _pad("KDA", 9)
    if rich:
        header += _pad("经济", 7) + _pad("伤害", 6)
    if full:
        header += _pad("补刀", 6) + "参团"
    lines.append(header.rstrip())

    for player in squad:
        row = (
            f"    {_pad(player.nickname, 13)}"
            f"{_pad(champ(player.hero_id, player.champion), 13)}"
            f"{_pad(player.kda, 9)}"
        )
        if rich:
            row += _pad(_gold(player.gold) if player.gold else "—", 7)
            row += _pad(f"{player.damage_pct:.0f}%" if player.damage_pct else "—", 6)
        if full:
            row += _pad(str(player.cs) if player.cs else "—", 6)
            row += f"{player.part:.0f}%" if player.part else "—"
        if player.mvp:
            row += " ★MVP"
        elif player.beiguo:
            row += " 背锅"
        lines.append(row.rstrip())

        if not full:
            continue
        extra = _player_extra(player)
        if extra:
            lines.append("      " + extra)
    return lines


def _player_extra(player: PlayerStat) -> str:
    """选手的第二行：等级、召唤师技能、符文、出装件数。"""
    parts: list[str] = []
    if player.hero_level:
        parts.append(f"Lv{player.hero_level}")
    if player.spells:
        parts.append("/".join(player.spells))
    if player.perks is not None and player.perks.ready:
        parts.append(player.perks.full)
    if player.items:
        parts.append(f"出装 {len(player.items)} 件")
    return "　".join(parts)


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
            f"{prefix} 联赛详情 <ID>   该联赛全部场次，按阶段分组、每场带比赛 ID",
            f"{prefix} 详情 <比赛ID>   单场逐局数据（BP/推塔/选手经济伤害）",
            f"{prefix} 详情 <比赛ID> <局号>  只看其中一局",
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
    first = (results or upcoming or [None])[0]
    hint = usage_hint(first.tournament_id, first.id) if first else ""
    if hint:
        lines.append(f"💡 {hint}")
    return "\n".join(lines).strip()
