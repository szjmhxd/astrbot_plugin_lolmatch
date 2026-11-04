"""内部数据模型。

所有数据源都必须先归一化成这里的结构，指令层与渲染层只认这些类。
换数据源时只改 provider，不动上层。

设计原则：**字段要么有值，要么是明确的空值**（``""`` / ``0`` / ``[]``），
不出现 ``None`` 到处漏的情况——否则渲染层要写一堆 ``or "未知"``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# 赛事状态：0 未开始 / 1 进行中 / 2 已结束
STATUS_TEXT = ("未开始", "进行中", "已结束")
STATUS_ICON = ("🕐", "🔴", "✅")

_WEEKDAY = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 晋级图：首列最多 8 场（16 队），整段最多 15 场（8+4+2+1）
_BRACKET_FIRST_MAX = 8
_BRACKET_TOTAL_MAX = 15

# 源站表示「对阵未定」的几种写法：空串、TBD，以及中文站偶尔给的占位符
_TBD_NAMES = {"", "tbd", "-", "—", "待定", "未知"}


def is_tbd(name: str) -> bool:
    return (name or "").strip().lower() in _TBD_NAMES


def day_label(day: str, *, with_year: bool = False) -> str:
    """``2026-10-26`` → ``10-26 周一``。

    带星期是因为赛程密集时「10-26 和 10-27 哪场在前」一眼看不出来，
    加上周几读起来快得多。日期解析不了就原样返回，别让渲染层炸掉。
    """
    text = (day or "").strip()
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError:
        return text or "—"
    head = parsed.isoformat() if with_year else parsed.strftime("%m-%d")
    return f"{head} {_WEEKDAY[parsed.weekday()]}"


def stage_groups(matches: list[Match]) -> list[tuple[str, list[Match]]]:
    """按「比赛阶段」分组，顺序即首次出现的先后（赛程本身已按日期排好）。

    源站的 ``round_name`` 只给到「入围赛 / 瑞士轮 / 淘汰赛」这一层，
    没有八强四强决赛的细分——所以这就是阶段能拿到的全部信息。
    """
    groups: dict[str, list[Match]] = {}
    for match in matches:
        groups.setdefault(match.round_name or "赛程", []).append(match)
    return list(groups.items())


def stage_has_result(matches: list[Match]) -> bool:
    """这个赛段有没有打完过一场。

    刚开赛的赛段（今天才第一场）整段都是占位，画成树或格子都只有「待定」，
    不如不出图——读者点开联赛详情要的是已有结果，不是预告。
    """
    return any(match.finished for match in matches)


def paginate_by_day(matches: list[Match], limit: int) -> list[list[Match]]:
    """按**日期边界**切页，每页不超过 ``limit`` 场。

    联赛详情卡之前直接 ``matches[:limit]``，等于从中间一刀切——S16 有 44 场，
    前 36 场刚好切到 10-31，于是 11-01 那三场和整段淘汰赛凭空消失。
    数据没丢，是卡片吞了，而图上又没有任何「后面还有」的提示，读者只会以为没比赛。

    现在按天打包：一天之内不劈开，宁可让单天超限自成一页也不拆。
    """
    if limit <= 0 or len(matches) <= limit:
        return [matches] if matches else []

    days: dict[str, list[Match]] = {}
    for match in matches:
        days.setdefault(match.start_date, []).append(match)

    pages: list[list[Match]] = []
    page: list[Match] = []
    for items in days.values():
        if page and len(page) + len(items) > limit:
            pages.append(page)
            page = []
        page.extend(items)
    if page:
        pages.append(page)
    return pages


def bracket_columns(matches: list[Match]) -> list[list[Match]] | None:
    """把一个赛段的场次摆成单败淘汰图的列（从左到右），摆不出来返回 ``None``。

    源站没给「哪场是八强、哪场通向哪场」，只有一个阶段名。但淘汰赛的形状是
    自证的：从决赛往前数，永远是 1、2、4、8 场递减，多出来的归最左一列。
    S16 淘汰赛 7 场 → ``[4, 2, 1]``；入围赛 6 场 → ``[3, 2, 1]``，和官网一致。

    不是这个形状就不画（瑞士轮 33 场会算成 ``[18, 8, 4, 2, 1]``，首列 18 场根本不是树）。
    宁可不出图，也不要把瑞士轮画成八强。
    """
    if len(matches) < 2 or len(matches) > _BRACKET_TOTAL_MAX:
        return None

    tail: list[int] = []  # 从决赛往前数的每列容量：1、2、4……
    rest = len(matches)
    size = 1
    while rest >= size:
        tail.append(size)
        rest -= size
        size *= 2
    if rest:  # 余数归最左一列（3→2→1 的 3 就是这么来的）
        tail.append(rest)
    sizes = tail[::-1]

    if sizes[0] > _BRACKET_FIRST_MAX:
        return None
    if any(sizes[i] > sizes[i + 1] * 2 for i in range(len(sizes) - 1)):
        return None

    cols: list[list[Match]] = []
    cursor = 0
    for size in sizes:
        cols.append(matches[cursor : cursor + size])
        cursor += size
    return cols


def bracket_parents(prev_count: int, index: int) -> list[int]:
    """子列第 ``index`` 个在父列里的「位置父母」。

    单败树就是 ``index * 2`` 和 ``index * 2 + 1``。用于**排版**（子块的纵向中心
    对到两个父块的中心之间），不用于判定对阵关系——那个交给 :func:`bracket_edges`。
    """
    return [j for j in range(prev_count) if j // 2 == index]


def bracket_strict(columns: list[list[Match]]) -> bool:
    """是不是干净的单败树（每列正好是下一列的两倍）。

    只有这种形状，「第 j 个 ↔ 第 j//2 个」才等价于真实的晋级关系。
    2026 MSI 的淘汰赛是 14 场双败（``[7,4,2,1]``），入围赛是 6 场 4 队双败
    （``[3,2,1]``），都不满足——照位置连线会连出根本不存在的对阵。
    """
    if len(columns) < 2:
        return False
    counts = [len(col) for col in columns]
    return all(counts[i] == counts[i + 1] * 2 for i in range(len(counts) - 1))


def bracket_edges(
    columns: list[list[Match]], *, strict: bool = False
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """连线的两端，``((父列, 行), (子列, 行))``。

    主依据是**数据**：一场比赛里某个队，上一次出现在同一赛段的哪一场——那就是
    它的来路（赢了上去还是输了掉下去都算，反正它是从那儿来的）。这条规则对单败、
    双败都成立，而且随时能拿赛程复算。

    ``strict`` 为真（干净单败树）时再补一波按位置的连线，用来兜住「对阵未定」
    的场次——它们没有队名，只能靠形状连。
    """
    pos: dict[str, tuple[int, int]] = {}
    for ci, col in enumerate(columns):
        for ri, match in enumerate(col):
            if match.id:
                pos[match.id] = (ci, ri)

    order = sorted(
        (match for col in columns for match in col),
        key=lambda m: (m.start_date, m.start_time, m.id),
    )

    edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for ci in range(1, len(columns)):
        for ri, match in enumerate(columns[ci]):
            for team in (match.team_a, match.team_b):
                if is_tbd(team):
                    continue
                prev: Match | None = None
                for other in order:
                    if other is match:
                        break
                    if team in (other.team_a, other.team_b):
                        prev = other
                if prev is None or prev.id not in pos:
                    continue
                parent = pos[prev.id]
                if parent[0] < ci:  # 只连「上一列 → 这一列」，不画回头线
                    edges.add((parent, (ci, ri)))

    if strict:
        for ci in range(1, len(columns)):
            for ri in range(len(columns[ci])):
                for pj in bracket_parents(len(columns[ci - 1]), ri):
                    edges.add(((ci - 1, pj), (ci, ri)))
    return sorted(edges)


_ROUND_BY_COUNT = {1: "决赛", 2: "半决赛", 4: "八强", 8: "十六强", 16: "三十二强"}


def bracket_rounds(columns: list[list[Match]]) -> list[str]:
    """给晋级图的每一列起个名字。

    只有形状严格是 4→2→1 这种整倍递减时才敢叫「八强 / 半决赛 / 决赛」；
    入围赛那种 3→2→1、MSI 那种 7→4→2→1 都是双败结构，硬套「半决赛」是编的，
    就老实编号。
    """
    if bracket_strict(columns):
        return [_ROUND_BY_COUNT.get(len(col), f"第{i + 1}轮") for i, col in enumerate(columns)]
    return [f"第{i + 1}轮" for i in range(len(columns))]


# ---------------------------------------------------------------- 瑞士轮

# 瑞士轮不是树，是一条「先到 N 胜出线、先到 N 负出局」的积分跑道。
# LOL 世界赛 / 季中赛都是 3 胜晋级、3 负淘汰：16 队打完正好 8 进 8 出
# （2 个 3-0 + 3 个 3-1 + 3 个 3-2，对应 2 个 0-3 + 3 个 1-3 + 3 个 2-3）。
SWISS_WIN_TARGET = 3
SWISS_LOSS_TARGET = 3
SWISS_TEAM_MIN = 8


@dataclass(slots=True)
class SwissRow:
    """一支队在瑞士轮里的战绩轨迹。

    ``path`` 是按时间顺序的每轮胜负，画成格子就是「连胜几场后栽在哪」，
    比只看 3-2 这个数字直观——3-0 出线的是横扫，3-2 出线的是被逼到墙角再翻盘。
    ``game_wins`` / ``game_losses`` 是小局分，积分榜上和大场分并列——同为 3-2，
    6-4 和 6-7 是两种状态。
    """

    team: str = ""
    wins: int = 0
    losses: int = 0
    path: list[bool] = field(default_factory=list)  # True 为胜
    flag: str = ""
    game_wins: int = 0
    game_losses: int = 0

    @property
    def record(self) -> str:
        return f"{self.wins}-{self.losses}"

    @property
    def games(self) -> str:
        return f"{self.game_wins}-{self.game_losses}"

    @property
    def diff(self) -> int:
        return self.game_wins - self.game_losses

    @property
    def state(self) -> str:
        """``qualified`` 出线 / ``eliminated`` 出局 / ``playing`` 还有机会。"""
        if self.wins >= SWISS_WIN_TARGET:
            return "qualified"
        if self.losses >= SWISS_LOSS_TARGET:
            return "eliminated"
        return "playing"

    @property
    def done(self) -> bool:
        return self.state != "playing"


@dataclass(slots=True)
class SwissLane:
    """瑞士轮图上的一个格子，也就是一场对局。

    ``wins`` / ``losses`` 是**赛前**战绩，决定它落在哪一档；``decide`` 是这场
    的性质。两者都必须按赛前状态算——算完再更新，否则 3-0 出线那一场会被
    记成 3-0 之后，标成「常规轮」。
    """

    match: Match
    round: int = 1
    wins: int = 0
    losses: int = 0
    decide: str = "none"  # none 常规 / adv 晋级战 / out 淘汰战 / both 生死战

    @property
    def band(self) -> tuple[int, int]:
        return (self.wins, self.losses)

    @property
    def decided(self) -> bool:
        return self.match.finished and self.match.score_a != self.match.score_b

    @property
    def winner(self) -> str:
        if not self.decided:
            return ""
        return self.match.team_a if self.match.score_a > self.match.score_b else self.match.team_b

    @property
    def loser(self) -> str:
        if not self.decided:
            return ""
        return self.match.team_b if self.match.score_a > self.match.score_b else self.match.team_a


def _swiss_decide(wins: int, losses: int) -> str:
    """这一场是晋级战、淘汰战、还是两样都占的生死战。

    必须双向判：只看「胜场摸到晋级线」会给 2-2 生死战的败者漏标淘汰。
    """
    win_edge = wins == SWISS_WIN_TARGET - 1
    loss_edge = losses == SWISS_LOSS_TARGET - 1
    if win_edge and loss_edge:
        return "both"
    if win_edge:
        return "adv"
    if loss_edge:
        return "out"
    return "none"


def swiss_rows(matches: list[Match]) -> list[SwissRow]:
    """把一段赛程算成每队的瑞士轮战绩。

    只认**已结束**的比赛：没打完的不该记胜负，否则「3-0 出线」会提前成立。
    出线/出局之后的场次也不再计入（源站偶尔会留下补赛或表演赛），
    一个队真正的战绩就是打到 3 胜或 3 负那一刻为止。
    """
    rows: dict[str, SwissRow] = {}
    order = sorted(matches, key=lambda m: (m.start_date, m.start_time, m.id))
    for match in order:
        if not match.finished or match.score_a == match.score_b:
            continue
        sides = (
            (match.team_a, match.flag_a, match.score_a, match.score_b),
            (match.team_b, match.flag_b, match.score_b, match.score_a),
        )
        for team, flag, own, foe in sides:
            if is_tbd(team):
                continue
            row = rows.get(team)
            if row is None:
                row = rows[team] = SwissRow(team=team)
            if not row.flag:
                row.flag = flag
            if row.done:
                continue
            row.path.append(own > foe)
            row.game_wins += own
            row.game_losses += foe
            if own > foe:
                row.wins += 1
            else:
                row.losses += 1
    return sorted(rows.values(), key=lambda r: (-r.wins, r.losses, r.team))


def swiss_standings(rows: list[SwissRow]) -> list[tuple[int, SwissRow]]:
    """积分榜：``(名次, 队)``。同大场战绩共享名次（1、2、3、4、4、6…）。"""
    ordered = sorted(
        rows,
        key=lambda r: (-r.wins, r.losses, -r.diff, -r.game_wins, r.team),
    )
    out: list[tuple[int, SwissRow]] = []
    rank = 0
    prev: tuple[int, int] | None = None
    for index, row in enumerate(ordered, 1):
        key = (row.wins, row.losses)
        if key != prev:
            rank = index
            prev = key
        out.append((rank, row))
    return out


def swiss_lanes(matches: list[Match]) -> list[list[SwissLane]] | None:
    """把瑞士轮摆成「一轮一列」的格子，摆不出来返回 ``None``。

    轮次靠**回溯**算，不信任何静态标签：一支队打第几场，就是第几轮。
    摆不成形的都不画——对阵未定（没队名，接不上线）、双方已赛场次不等
    （数据本身矛盾）、轮次编号断档（少了一轮的赛程），这三种情况画出来都是错的。
    """
    if not matches:
        return None

    order = sorted(matches, key=lambda m: (m.start_date, m.start_time, m.id))
    if any(is_tbd(m.team_a) or is_tbd(m.team_b) for m in order):
        return None

    wins: dict[str, int] = {}
    losses: dict[str, int] = {}
    played: dict[str, int] = {}
    columns: dict[int, list[SwissLane]] = {}

    for match in order:
        a, b = match.team_a, match.team_b
        if played.get(a, 0) != played.get(b, 0):
            return None
        number = played.get(a, 0) + 1
        lane = SwissLane(
            match=match,
            round=number,
            wins=wins.get(a, 0),
            losses=losses.get(a, 0),
            decide=_swiss_decide(wins.get(a, 0), losses.get(a, 0)),
        )
        columns.setdefault(number, []).append(lane)
        played[a] = played.get(a, 0) + 1
        played[b] = played.get(b, 0) + 1
        if lane.decided:
            if match.score_a > match.score_b:
                wins[a] = wins.get(a, 0) + 1
                losses[b] = losses.get(b, 0) + 1
            else:
                wins[b] = wins.get(b, 0) + 1
                losses[a] = losses.get(a, 0) + 1

    rounds = sorted(columns)
    if rounds != list(range(1, len(rounds) + 1)):
        return None
    return [columns[number] for number in rounds]


def swiss_round_dates(columns: list[list[SwissLane]]) -> list[str]:
    """每一列的日期标签：同一天就写一天，跨天写区间。"""
    out: list[str] = []
    for column in columns:
        days = [lane.match.start_date for lane in column if lane.match.start_date]
        if not days:
            out.append("")
            continue
        head, tail = min(days), max(days)
        out.append(day_label(head) if head == tail else f"{head[5:]} ~ {tail[5:]}")
    return out


def swiss_shaped(rows: list[SwissRow]) -> bool:
    """形状像不像瑞士轮。

    光看「队多」不够——S 赛小组赛也是 16 队，但那是分组循环。两条硬指标：
    队数是 2 的幂（瑞士轮为对半出线而生，16 进 8、8 进 4）；
    没有队打过 5 场以上（先到 3 胜/3 负，最多 5 场，双循环小组赛会到 6）。
    """
    if len(rows) < SWISS_TEAM_MIN or len(rows) & (len(rows) - 1):
        return False
    return not any(len(row.path) > SWISS_WIN_TARGET + SWISS_LOSS_TARGET - 1 for row in rows)


def swiss_ready(rows: list[SwissRow], *, rounds: int = 0) -> bool:
    """够不够格画成「轮次跑道」。

    除了形状，还得有东西可看：要么已经有人出线/出局，要么赛程已经走过三轮
    （``rounds``）——瑞士轮打了两轮时，画出来只是一张对阵表，不如直接列赛程。
    """
    if not swiss_shaped(rows):
        return False
    return any(row.done for row in rows) or rounds >= 3


# ---------------------------------------------------------------- 赛制识别

# 三种赛制的版式完全不同：单败是一棵干净的树，双败是「上胜者组 / 下败者组 /
# 右决赛」两条轨道，瑞士轮是一条按轮次铺开的跑道。**判错比画丑严重得多**：
# 双败画成单败，败者组三场比赛没地方放，队伍旅程直接断；
# 瑞士轮画成淘汰树，33 场挤进树里，战绩分档这个核心信息全丢。
FORMAT_SINGLE = "single"
FORMAT_DOUBLE = "double"
FORMAT_SWISS = "swiss"

FORMAT_LABEL = {
    FORMAT_SINGLE: "单败淘汰",
    FORMAT_DOUBLE: "双败淘汰",
    FORMAT_SWISS: "瑞士轮",
}

# 行级结局标记。用语义键而不是颜色，配色交给渲染层。
MARK_ADVANCE = "advance"  # 晋级（还有下一场）
MARK_OUT = "out"  # 淘汰
MARK_DROP = "drop"  # 第一败，掉进败者组
MARK_CHAMPION = "champion"  # 夺冠
MARK_RUNNER = "runner"  # 亚军

MARK_TEXT = {
    MARK_ADVANCE: "晋级",
    MARK_OUT: "淘汰",
    MARK_DROP: "跌入败者",
    MARK_CHAMPION: "夺冠",
    MARK_RUNNER: "亚军",
}


def _is_pow2(n: int) -> bool:
    return n > 0 and not n & (n - 1)


def stage_teams(matches: list[Match]) -> set[str]:
    """这个赛段真实出现过的队名。占位的「待定」不算队。"""
    out: set[str] = set()
    for match in matches:
        for team in (match.team_a, match.team_b):
            if team and not is_tbd(team):
                out.add(team)
    return out


def team_records(matches: list[Match]) -> dict[str, tuple[int, int, int]]:
    """每队 ``(大场胜, 大场负, 出场次数)``，只算已结束的真实对局。"""
    stats: dict[str, tuple[int, int, int]] = {}

    def bump(team: str, win: int, loss: int, played: int) -> None:
        if not team or is_tbd(team):
            return
        won, lost, seen = stats.get(team, (0, 0, 0))
        stats[team] = (won + win, lost + loss, seen + played)

    for match in sorted(matches, key=lambda m: (m.start_date, m.start_time, m.id)):
        if match.pending:
            continue
        bump(match.team_a, 0, 0, 1)
        bump(match.team_b, 0, 0, 1)
        if not match.finished or not match.team_a or not match.team_b:
            continue
        if match.score_a == match.score_b:
            continue
        won_a = match.score_a > match.score_b
        bump(match.team_a, 1 if won_a else 0, 0 if won_a else 1, 0)
        bump(match.team_b, 0 if won_a else 1, 1 if won_a else 0, 0)
    return stats


def detect_format(matches: list[Match]) -> str | None:
    """这个赛段是单败、双败还是瑞士轮。**只看数据，不看赛事名。**

    三条指纹按「误判代价」排序，前一条成立就不再往下试：

    1. 有队输过 3 场、或有人打满 5 场 —— 单败和双败每队最多 2 负，不可能出现；
    2. 场次 = 队数 - 1，且能切成严格整倍递减的树（4→2→1）—— 单败；
    3. 场次 = 2N-2（双败的固定式），或形状不是整倍递减 —— 双败。

    形状优先于场次式：赛段打到一半时 N 会因为「还没出场的队」偏小，
    场次式对不上，但形状（1、2、4 反推的列）与打没打完无关。
    """
    if len(matches) < 2:
        return None

    stats = team_records(matches)
    if stats:
        max_loss = max(loss for _, loss, _ in stats.values())
        max_played = max(played for _, _, played in stats.values())
        if max_loss >= SWISS_LOSS_TARGET or max_played > SWISS_WIN_TARGET + SWISS_LOSS_TARGET - 1:
            return FORMAT_SWISS

    teams = len(stage_teams(matches))
    columns = bracket_columns(matches)
    if columns is None:
        return FORMAT_SWISS if swiss_shaped(swiss_rows(matches)) else None

    strict = bracket_strict(columns)
    if strict and teams and len(matches) == teams - 1 and _is_pow2(teams):
        return FORMAT_SINGLE
    if strict:
        return FORMAT_SINGLE
    if teams and _is_pow2(teams) and len(matches) in (2 * teams - 2, 2 * teams - 1):
        return FORMAT_DOUBLE
    return FORMAT_DOUBLE


@dataclass(slots=True)
class DoubleElim:
    """双败赛段拆成三条轨道：胜者组、败者组、总决赛。

    判定只看一件事：**这场比赛开打前，双方各自输过几次**。
    两个都没输过 = 胜者组；有一方输过 = 败者组。最后一场是总决赛
    （双败的总决赛永远排在最后，而且它必然是一队 0 负对一队 1 负，
    按「都 0 负才算胜者组」会把它误分到败者组，所以先摘出来）。
    """

    winner: list[Match]
    loser: list[Match]
    final: Match | None

    @property
    def ready(self) -> bool:
        return bool(self.winner or self.loser)


def double_elim_split(matches: list[Match]) -> DoubleElim:
    ordered = sorted(matches, key=lambda m: (m.start_date, m.start_time, m.id))
    if len(ordered) < 2:
        return DoubleElim(ordered, [], None)

    final = ordered[-1]
    winner: list[Match] = []
    loser: list[Match] = []
    losses: dict[str, int] = {}
    for match in ordered[:-1]:
        a, b = match.team_a, match.team_b
        if losses.get(a, 0) == 0 and losses.get(b, 0) == 0:
            winner.append(match)
        else:
            loser.append(match)
        if match.finished and a and b and not match.pending:
            beaten = b if match.score_a > match.score_b else a
            if beaten:
                losses[beaten] = losses.get(beaten, 0) + 1
    return DoubleElim(winner, loser, final)


def match_marks(matches: list[Match], fmt: str) -> dict[str, tuple[str, str]]:
    """每场两行的结局标记，``match.id -> (上行, 下行)``，没标记写空串。

    单败按「一败出局」标四态：晋级 / 淘汰 / 夺冠 / 亚军。

    双败**只标负方**（跌入败者 / 淘汰），胜方留空。理由：双败图的核心信息是
    「谁掉下去了」，胜方每场都标「晋级」等于整列都在喊，反而看不出重点；
    而且胜者组赢一场本来就不是出线，标「晋级」是错话。

    标记按**队伍状态**算，不按场次类型猜——瑞士轮那次的 bug 教训：
    只看「胜场到线」会把生死战败者的淘汰标记漏掉。
    """
    ordered = sorted(matches, key=lambda m: (m.start_date, m.start_time, m.id))
    if not ordered:
        return {}

    final = ordered[-1]
    marks: dict[str, tuple[str, str]] = {}

    def put(match: Match, a: str, b: str) -> None:
        if match.id:
            marks[match.id] = (a, b)

    if fmt == FORMAT_SINGLE:
        for match in ordered:
            if not match.finished or match.pending:
                put(match, "", "")
                continue
            won_a = match.score_a > match.score_b
            if match is final:
                put(match, MARK_CHAMPION, MARK_RUNNER)
                if not won_a:
                    put(match, MARK_RUNNER, MARK_CHAMPION)
            else:
                put(match, MARK_ADVANCE, MARK_OUT)
                if not won_a:
                    put(match, MARK_OUT, MARK_ADVANCE)
        return marks

    # 双败：只标负方
    losses: dict[str, int] = {}
    for match in ordered:
        a, b = match.team_a, match.team_b
        if not match.finished or not a or not b or match.pending:
            put(match, "", "")
            continue
        won_a = match.score_a > match.score_b
        beaten = b if won_a else a
        count = losses.get(beaten, 0) + 1
        losses[beaten] = count

        if match is final:
            put(match, MARK_CHAMPION, MARK_RUNNER)
            if not won_a:
                put(match, MARK_RUNNER, MARK_CHAMPION)
            continue

        fate = MARK_OUT if count >= 2 else MARK_DROP
        if won_a:
            put(match, "", fate)
        else:
            put(match, fate, "")
    return marks


# ---------------------------------------------------------------- 淘汰赛积分


@dataclass(slots=True)
class KnockoutRow:
    """一支队在淘汰赛（单败 / 双败）里的战绩、旅程与名次。

    ``path`` 是按时间顺序的每场「胜负 + 对手」，面板上画成
    「胜 C9 → 胜 MVK → 负 KC」的旅程线——单看 3-1 这个大场数，
    分不出这支队是一路横扫，还是输一场掉进败者组再爬回来的。

    ``rank`` 为 0 表示名次未定（还在打）；同轮出局的队共享名次，
    和正式赛事排名惯例一致（八强赛四支败者同列 5-8 名）。
    """

    team: str = ""
    flag: str = ""
    wins: int = 0
    losses: int = 0
    game_wins: int = 0
    game_losses: int = 0
    path: list[tuple[bool, str]] = field(default_factory=list)  # (胜?, 对手)
    rank: int = 0
    state: str = "playing"  # champion / runner / out / playing

    @property
    def record(self) -> str:
        return f"{self.wins}-{self.losses}"

    @property
    def games(self) -> str:
        return f"{self.game_wins}-{self.game_losses}"

    @property
    def diff(self) -> int:
        return self.game_wins - self.game_losses

    @property
    def path_text(self) -> str:
        """旅程线文字：「胜 C9 → 胜 MVK → 负 KC」。"""
        return " → ".join(f"{'胜' if won else '负'} {foe}" for won, foe in self.path)

    @property
    def badge(self) -> str:
        """名次段徽章。按名次段而不是按赛制细节叫——
        1 冠军、2 亚军、3-4 四强、5-8 八强，单败双败通用。"""
        if not self.rank:
            return "在战"
        if self.rank == 1:
            return "冠军"
        if self.rank == 2:
            return "亚军"
        if self.rank <= 4:
            return "四强"
        if self.rank <= 8:
            return "八强"
        if self.rank <= 16:
            return "十六强"
        return f"{self.rank} 名"


def knockout_rows(matches: list[Match], fmt: str | None = None) -> list[KnockoutRow]:
    """淘汰赛的每队积分 / 旅程 / 名次，双败、单败的右侧面板用。

    名次按**淘汰轮次**分段共享：同一轮出局的队同名次，越晚出局名次越好。
    打完没打完都出榜——进行中的赛段冠军栏写「待定」，名次只发给已经出局的队
    （还在打的队不预发 1、2 名，那是在替没打完的比赛宣布结果）。
    """
    ordered = sorted(matches, key=lambda m: (m.start_date, m.start_time, m.id))
    if not ordered:
        return []
    fmt = fmt or detect_format(ordered) or FORMAT_SINGLE

    # ---- 淘汰轮次键：单败用晋级图的列号，双败用败者组的深度轮次。
    # 名次段的满员数按**结构**算而不是实际出局人数：半决赛只打完一场时，
    # 那支败者占的是「3-4 名」段，另一场还没打，不预支下一段的名次。
    round_of: dict[str, int] = {}
    band_size: dict[int, int] = {}
    if fmt == FORMAT_SINGLE:
        cols = bracket_columns(ordered) or []
        for ci, col in enumerate(cols):
            band_size[ci] = len(col)
            for match in col:
                if match.id:
                    round_of[match.id] = ci
    else:
        # 败者组每场恰好淘汰一队（败者带着第二败离场），轮次用深度回推：
        # 该场轮次 = max(双方上一轮轮次) + 1，和双败图的败者组分列同一套算法
        layout = double_elim_split(ordered)
        depth: dict[str, int] = {}
        for match in layout.loser:
            key = max(depth.get(match.team_a, 0), depth.get(match.team_b, 0)) + 1
            if match.id:
                round_of[match.id] = key
            band_size[key] = band_size.get(key, 0) + 1
            for team in (match.team_a, match.team_b):
                if team and not is_tbd(team):
                    depth[team] = key

    # ---- 大场 / 小局 / 旅程，只认有赛果的场
    rows: dict[str, KnockoutRow] = {}
    elim: dict[str, str] = {}  # 队名 -> 把它淘汰掉的那场的 id

    def row_for(team: str, flag: str = "") -> KnockoutRow | None:
        if not team or is_tbd(team):
            return None
        row = rows.get(team)
        if row is None:
            row = rows[team] = KnockoutRow(team=team)
        if flag and not row.flag:
            row.flag = flag
        return row

    for match in ordered:
        row_a = row_for(match.team_a, match.flag_a)
        row_b = row_for(match.team_b, match.flag_b)
        if row_a is None or row_b is None:
            continue
        if not match.finished or match.pending or match.score_a == match.score_b:
            continue
        won_a = match.score_a > match.score_b
        row_a.game_wins += match.score_a
        row_a.game_losses += match.score_b
        row_b.game_wins += match.score_b
        row_b.game_losses += match.score_a
        row_a.path.append((won_a, match.team_b))
        row_b.path.append((not won_a, match.team_a))
        if won_a:
            row_a.wins += 1
            row_b.losses += 1
        else:
            row_b.wins += 1
            row_a.losses += 1
        loser_row = row_b if won_a else row_a
        out_now = (
            loser_row.losses >= 2
            if fmt != FORMAT_SINGLE
            else loser_row.losses >= 1 and match is not ordered[-1]
        )
        if out_now and loser_row.team not in elim:
            elim[loser_row.team] = match.id

    # ---- 冠军 / 亚军：双败、单败都认「最后一场」为决赛（与 match_marks 同一口径）
    final = ordered[-1]
    if final.finished and not final.pending and final.score_a != final.score_b:
        won_a = final.score_a > final.score_b
        champ = row_for(final.team_a if won_a else final.team_b)
        runner = row_for(final.team_b if won_a else final.team_a)
        if champ is not None:
            champ.rank = 1
            champ.state = "champion"
        if runner is not None:
            runner.rank = 2
            runner.state = "runner"

    # ---- 出局队按淘汰轮次分段共享名次。段起点按**结构满员**算，
    # 空段也占位：半决赛还没打完时，首轮败者已经是「5-8 名」段的人，
    # 不能预支「3-4 名」的名次——那是在替没打完的比赛发奖
    if fmt == FORMAT_SINGLE and band_size:
        final_key = max(band_size)  # 决赛列 = 1、2 名，不进出局段
        rank_keys = [key for key in sorted(band_size, reverse=True) if key != final_key]
    else:
        rank_keys = sorted(band_size, reverse=True)
    start_rank: dict[int, int] = {}
    tail = 3
    for key in rank_keys:
        start_rank[key] = tail
        tail += band_size[key]

    groups: dict[int, list[KnockoutRow]] = {}
    for team, match_id in elim.items():
        row = rows.get(team)
        if row is None or row.state in ("champion", "runner"):
            continue
        groups.setdefault(round_of.get(match_id, 0), []).append(row)
    for key, members in groups.items():
        base = start_rank.get(key, 3)
        for row in members:
            row.rank = base
            row.state = "out"

    # ---- 展示顺序：冠亚 → 还在打的（胜场多的在前）→ 出局的按名次
    order_state = {"champion": 0, "runner": 1, "playing": 2, "out": 3}
    return sorted(
        rows.values(),
        key=lambda r: (
            order_state.get(r.state, 4),
            r.rank or 99,
            -r.wins,
            r.losses,
            -r.diff,
            r.team,
        ),
    )


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

    def __post_init__(self) -> None:
        """按比分补 ``winner``。

        源站的 ``win_team_short_name`` 时有时无——MSI 淘汰赛 14 场全空。
        空值不会报错，只会在图上悄悄出事：晋级图的「胜者续战」实线全靠它，
        一旦为空，所有线都会掉进「败者续战」分支，整张图变成一片橙色虚线。
        比分永远在，所以让比分兜底。
        """
        if self.winner or not self.finished or self.pending:
            return
        if self.score_a > self.score_b:
            self.winner = self.team_a
        elif self.score_b > self.score_a:
            self.winner = self.team_b

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
    def pending(self) -> bool:
        """对阵未定。淘汰赛的后几轮源站会先占日期，两个队名都空或写 TBD。

        以前这种条目在解析层就被丢了，于是「11-08 半决赛」这条日期整个不存在，
        而官网的晋级图上有它——占位本身就是信息（哪天打），不该扔。
        """
        return is_tbd(self.team_a) and is_tbd(self.team_b)

    @property
    def name_a(self) -> str:
        """展示用队名：空的写成「待定」，别在卡片上留一块空白。"""
        return "待定" if is_tbd(self.team_a) else self.team_a

    @property
    def name_b(self) -> str:
        return "待定" if is_tbd(self.team_b) else self.team_b

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
