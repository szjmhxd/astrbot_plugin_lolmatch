"""图片卡片渲染（Pillow）。

为什么是 Pillow 而不是 HTML + 浏览器截图
----------------------------------------
赛程和详情本质上是**表格**：固定的列、要对齐的数字。浏览器截图在这里拿到的好处是
CSS 排版，代价是几百 MB 的 Chromium 和一堆系统库——AstrBot 宿主机上十有八九没有，
插件市场的用户更不会为了看个赛程去装浏览器。Pillow 是 AstrBot 本来就有的依赖，
画表格还更准，所以选它。

代价是字体要自己找。找不到中文字体（常见于精简容器）就整体降级成纯文字，
绝不能让「画不出图」变成「收不到内容」。

坐标约定
--------
下面所有常量都是**逻辑像素**（按 880 宽设计），:class:`Canvas` 内部统一乘 2 输出，
这样字体和线条都是原生分辨率，不会像先画小再放大那样糊。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from .models import Game, Match, MatchDetail, PlayerStat, day_label, stage_groups

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont

    _PIL_OK = True
except ImportError:  # pragma: no cover - AstrBot 一般都带 Pillow
    Image = ImageChops = ImageDraw = ImageFont = None  # type: ignore[assignment]
    _PIL_OK = False

from ._log import logger
from .icons import IconStore

# ---------------------------------------------------------------- 配色
BG = "#0d1117"
PANEL = "#161b24"
PANEL_2 = "#1c2331"
LINE = "#29313f"
INK = "#e6edf3"
MUTED = "#8b949e"
DIM = "#5b6572"
BLUE = "#58a6ff"
RED = "#f85149"
RED_BG = "#3a1a1c"
WIN = "#e3b341"
GREEN = "#3fb950"
FIGHT = "#a16207"  # 团战标记，暗金——比灰显眼、又不抢红蓝的注意力

WIDTH = 880
PAD = 22

# 中文字体候选。顺序即优先级：先用系统自带的，再退到常见开源字体。
_REGULAR = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhl.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
)
_BOLD = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyhbd.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansSC-Bold.otf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
)


def _pick_font(paths: tuple[str, ...]) -> str:
    for path in paths:
        if Path(path).exists():
            return path
    return ""


class Fonts:
    """按逻辑字号缓存字体对象。``available`` 为假时整张卡片都别画。"""

    def __init__(self, scale: int = 2) -> None:
        self.scale = scale
        self._regular = _pick_font(_REGULAR)
        self._bold = _pick_font(_BOLD) or self._regular
        self._cache: dict[tuple[int, bool], Any] = {}

    @property
    def available(self) -> bool:
        return bool(_PIL_OK and self._regular)

    def get(self, size: int, *, bold: bool = False) -> Any:
        key = (size, bold)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        path = self._bold if bold else self._regular
        font = None
        for index in range(4):
            try:
                font = ImageFont.truetype(path, size * self.scale, index=index)
                break
            except (OSError, ValueError):
                continue
        if font is None:  # 字体文件打不开就退回内置位图字体，至少不崩
            font = ImageFont.load_default()
        self._cache[key] = font
        return font


# ---------------------------------------------------------------- 画布


class Canvas:
    """逻辑坐标 → 设备坐标的薄封装。"""

    def __init__(self, fonts: Fonts, width: int, height: int) -> None:
        self.fonts = fonts
        self.scale = fonts.scale
        self.width = width
        self.height = height
        self.img = Image.new("RGB", (width * self.scale, height * self.scale), BG)
        self.draw = ImageDraw.Draw(self.img)

    def _box(self, x: float, y: float, w: float, h: float) -> list[int]:
        s = self.scale
        return [int(x * s), int(y * s), int((x + w) * s), int((y + h) * s)]

    def rrect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        radius: float = 6,
        fill: str | None = None,
        outline: str | None = None,
        width: float = 1,
    ) -> None:
        self.draw.rounded_rectangle(
            self._box(x, y, w, h),
            radius=int(radius * self.scale),
            fill=fill,
            outline=outline,
            width=max(1, int(width * self.scale)),
        )

    def line(self, x1: float, y1: float, x2: float, y2: float, *, fill: str = LINE) -> None:
        s = self.scale
        self.draw.line([int(x1 * s), int(y1 * s), int(x2 * s), int(y2 * s)], fill=fill, width=s)

    def text(
        self,
        x: float,
        y: float,
        text: str,
        size: int,
        *,
        fill: str = INK,
        bold: bool = False,
        anchor: str = "lt",
    ) -> None:
        if not text:
            return
        s = self.scale
        self.draw.text(
            (int(x * s), int(y * s)),
            text,
            font=self.fonts.get(size, bold=bold),
            fill=fill,
            anchor=anchor,
        )

    def text_right(self, right: float, y: float, text: str, size: int, **kwargs: Any) -> None:
        self.text(right, y, text, size, anchor="rt", **kwargs)

    def measure(self, text: str, size: int, *, bold: bool = False) -> float:
        return self.draw.textlength(text, font=self.fonts.get(size, bold=bold)) / self.scale

    def fit(self, text: str, size: int, limit: float, *, bold: bool = False) -> str:
        """超宽就截断加省略号。中文按显示宽度算，不按字符数。"""
        if not text or self.measure(text, size, bold=bold) <= limit:
            return text
        ellipsis = "…"
        room = limit - self.measure(ellipsis, size, bold=bold)
        out = ""
        for char in text:
            if self.measure(out + char, size, bold=bold) > room:
                break
            out += char
        return out + ellipsis

    def icon(
        self,
        x: float,
        y: float,
        size: float,
        data: bytes | None,
        *,
        radius: float = 0,
        placeholder: str = "",
        size_hint: int = 10,
        bg: str | None = None,
    ) -> None:
        """贴一张图。拿不到图就画个占位块，位置不错乱。

        ``bg`` 给一张底板：深色队标（黑底的 G2、GEN 那种）直接贴在深色面板上
        看不出边界，垫一层浅色底才有轮廓。
        """
        if bg:
            self.rrect(x, y, size, size, radius=radius or 4, fill=bg)
        box = self._box(x, y, size, size)
        if not data:
            self.rrect(x, y, size, size, radius=radius or 4, fill=PANEL_2)
            if placeholder:
                self.text(
                    x + size / 2,
                    y + size / 2,
                    self.fit(placeholder, size_hint, size - 4, bold=True),
                    size_hint,
                    fill=DIM,
                    bold=True,
                    anchor="mm",
                )
            return
        try:
            src = Image.open(io.BytesIO(data)).convert("RGBA")
        except Exception:  # noqa: BLE001 坏图不该让整张卡失败
            self.rrect(x, y, size, size, radius=radius or 4, fill=PANEL_2)
            return
        target = (max(1, box[2] - box[0]), max(1, box[3] - box[1]))
        src = src.resize(target, Image.LANCZOS)
        mask = src.getchannel("A")
        if radius:
            rounded = Image.new("L", target, 0)
            ImageDraw.Draw(rounded).rounded_rectangle(
                [0, 0, target[0] - 1, target[1] - 1],
                radius=int(radius * self.scale),
                fill=255,
            )
            mask = ImageChops.multiply(mask, rounded)
        self.img.paste(src, (box[0], box[1]), mask)

    def to_bytes(self) -> bytes:
        """输出 JPEG。

        同一张图 PNG 要 680KB、JPEG 只要 150KB 上下。卡片是大色块 + 小图标，
        用 ``subsampling=0`` 保住色度细节，文字边缘看不出压缩痕迹；群里发图
        的流量和上传时间却差四倍，这笔账很划算。
        """
        buffer = io.BytesIO()
        self.img.convert("RGB").save(
            buffer,
            format="JPEG",
            quality=88,
            optimize=True,
            subsampling=0,
        )
        return buffer.getvalue()


# ---------------------------------------------------------------- 小工具


def _gold(value: int) -> str:
    if value <= 0:
        return "0"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def _win_mark(game: Game, side: str) -> str:
    return "胜" if game.win_side == side else ""


def _bans_ready(bans: list[str]) -> bool:
    return sum(1 for name in bans if name) >= 3


def _collect_urls(matches: list[Match], games: list[Game]) -> list[str]:
    urls: list[str] = []
    for match in matches:
        urls.extend([match.flag_a, match.flag_b])
    for game in games:
        urls.extend([game.flag_a, game.flag_b])
        urls.extend(event.icon for event in game.dragons)
        for player in (*game.players_a, *game.players_b):
            urls.append(player.hero_icon)
            urls.extend(player.spell_icons)
            urls.extend(player.items)
    return [url for url in urls if url]


# ---------------------------------------------------------------- 赛程卡

HEAD_MAJOR = 26  # 阶段 / 联赛标题
HEAD_MINOR = 22  # 日期小标题
ROW_H = 46
ROW_ID_H = 54  # 多一行比赛 ID


def _schedule_blocks(matches: list[Match], limit: int, *, group: str) -> list[tuple[int, Any]]:
    """摊平成「标题 + 数据行」的序列。``level``：0 大标题 / 1 日期 / -1 一场比赛。

    两种分组都从同一份数据来，只是切法不同：
    ``tournament`` 按「日期 + 联赛」，``stage`` 按「阶段 → 日期」。
    """
    shown = matches[:limit] if limit > 0 else matches
    blocks: list[tuple[int, Any]] = []

    if group == "stage":
        for label, items in stage_groups(shown):
            blocks.append((0, label))
            day = ""
            for match in items:
                if match.start_date != day:
                    day = match.start_date
                    blocks.append((1, day_label(day)))
                blocks.append((-1, match))
        return blocks

    # 跨天的列表把日期提到标题上，单天的就只留联赛名——否则每块都在重复
    # 标题里已经写过的那个日期。
    days = {m.start_date for m in shown}
    grouped: dict[tuple[str, str], list[Match]] = {}
    for match in shown:
        key = (match.start_date, match.tournament_name or "其他联赛")
        grouped.setdefault(key, []).append(match)
    for (day, tournament), items in grouped.items():
        head = f"{day_label(day)}　{tournament}" if len(days) > 1 else f"【{tournament}】"
        blocks.append((0, head))
        blocks.extend((-1, match) for match in items)
    return blocks


def _schedule_row(
    canvas: Canvas,
    icons: dict[str, bytes],
    match: Match,
    y: float,
    *,
    show_id: bool = False,
) -> None:
    """一场比赛的条。左边时刻（可选再带一行比赛 ID），中间比分，两侧队标。"""
    height = ROW_ID_H if show_id else ROW_H
    canvas.rrect(PAD, y, WIDTH - PAD * 2, height - 4, radius=8, fill=PANEL)
    clock_y = y + 9 if show_id else y + 12
    canvas.text(PAD + 14, clock_y, match.start_time or "--:--", 15, fill=INK, bold=True)
    if show_id:
        # 11px + MUTED：聊天软件会把长图整体缩小，DIM 灰的 10px 缩完就看不清了，
        # 而这行编号正是「联赛详情」这张卡存在的理由之一。
        canvas.text(PAD + 14, y + 31, f"#{match.id}", 11, fill=MUTED)
    tag = f"BO{match.best_of}" if match.best_of else ""
    if tag:
        canvas.text(PAD + 66, y + 15, tag, 10, fill=DIM)
    canvas.icon(
        PAD + 96,
        y + 10 if show_id else y + 8,
        26,
        icons.get(match.flag_a),
        placeholder=match.team_a[:2],
        size_hint=9,
        bg=PANEL_2,
    )
    canvas.text(
        PAD + 130,
        y + 14 if show_id else y + 12,
        canvas.fit(match.team_a or "?", 15, 200),
        15,
    )
    middle = match.score_line if match.started else "vs"
    canvas.text(
        WIDTH / 2,
        y + 14 if show_id else y + 12,
        middle,
        15,
        fill=WIN if match.finished else MUTED,
        bold=match.finished,
        anchor="mt",
    )
    canvas.text_right(
        WIDTH - PAD - 48,
        y + 14 if show_id else y + 12,
        canvas.fit(match.team_b or "?", 15, 200),
        15,
    )
    canvas.icon(
        WIDTH - PAD - 40,
        y + 10 if show_id else y + 8,
        26,
        icons.get(match.flag_b),
        placeholder=match.team_b[:2],
        size_hint=9,
        bg=PANEL_2,
    )


def schedule_card(
    fonts: Fonts,
    icons: dict[str, bytes],
    *,
    title: str,
    matches: list[Match],
    limit: int = 20,
    source: str = "",
    subtitle: str = "",
    show_id: bool = False,
    group: str = "tournament",
    total: int = 0,
) -> bytes | None:
    """今日 / 本周 / 联赛详情共用一张卡，只是分组方式和副标题不同。

    ``total`` 只影响右上角那行字：联赛详情会分页，每页只拿到自己那一段，
    但读者关心的是「这个联赛一共多少场」，不是「这一页几张」。
    """
    if not fonts.available:
        return None
    blocks = _schedule_blocks(matches, limit, group=group)
    row_h = ROW_ID_H if show_id else ROW_H
    body = sum(
        HEAD_MAJOR if level == 0 else HEAD_MINOR if level == 1 else row_h for level, _ in blocks
    )
    head_h = 44 + (22 if subtitle else 0)
    height = PAD + head_h + max(body, 40) + 26 + PAD
    canvas = Canvas(fonts, WIDTH, height)

    canvas.text(PAD, PAD, title, 24, bold=True)
    canvas.text_right(WIDTH - PAD, PAD + 8, f"共 {total or len(matches)} 场", 13, fill=MUTED)

    y = PAD + 44
    if subtitle:
        canvas.text(PAD, y, subtitle, 12, fill=MUTED)
        y += 22
    if not blocks:
        canvas.text(PAD, y + 10, "这一天没有比赛。", 16, fill=MUTED)
        return canvas.to_bytes()

    for level, item in blocks:
        if level == 0:
            # 联赛详情里阶段是主角，用金色；赛程卡里联赛名只是分组，蓝色就够
            canvas.text(PAD, y + 4, item, 14, fill=WIN if group == "stage" else BLUE, bold=True)
            y += HEAD_MAJOR
        elif level == 1:
            canvas.text(PAD + 4, y + 2, item, 12, fill=MUTED)
            y += HEAD_MINOR
        else:
            _schedule_row(canvas, icons, item, y, show_id=show_id)
            y += row_h

    if source:
        canvas.text(PAD, height - PAD - 14, f"数据源 {source}", 11, fill=DIM)
    return canvas.to_bytes()


# ---------------------------------------------------------------- 比赛摘要卡


H2H_ROW = 22


def _h2h_panel(canvas: Canvas, h2h: Any, top: float) -> float:
    """历史交手的紧凑面板，返回结束时的 y。

    队名按胜负分色：赢的一方用金色、输的一方压暗。比分用中性色——
    两段都染色反而看不出谁赢了。

    只列最近 3 场：面板是给「这场有没有心理优势」这个问题用的，
    不是战绩档案馆，六场会把后面逐局表挤得没地方。
    """
    rows = h2h.recent(3)
    height = 26 + len(rows) * H2H_ROW + 10
    canvas.rrect(PAD, top, WIDTH - PAD * 2, height, radius=10, fill=PANEL)

    canvas.text(PAD + 14, top + 8, f"近 {h2h.count} 次交手", 11, fill=DIM)

    # 总比分右对齐排一行。先各自测宽再从右往左退，避免拼接时左右间距不等。
    name_size, score_size, gap = 13, 14, 10
    right = WIDTH - PAD - 14
    name_b = canvas.measure(h2h.team_b, name_size, bold=True)
    score = f"{h2h.wins_a} : {h2h.wins_b}"
    score_w = canvas.measure(score, score_size, bold=True)
    name_a = canvas.measure(h2h.team_a, name_size, bold=True)

    x_b = right - name_b
    x_score = x_b - gap - score_w
    x_a = x_score - gap - name_a
    canvas.text(
        x_a,
        top + 8,
        h2h.team_a,
        name_size,
        bold=True,
        fill=WIN if h2h.wins_a > h2h.wins_b else MUTED,
    )
    canvas.text(x_score, top + 7, score, score_size, bold=True, fill=INK)
    canvas.text(
        x_b,
        top + 8,
        h2h.team_b,
        name_size,
        bold=True,
        fill=WIN if h2h.wins_b > h2h.wins_a else MUTED,
    )

    y = top + 26
    for game in rows:
        canvas.text(PAD + 14, y + 3, game.date[5:], 10, fill=MUTED)
        x = PAD + 62
        a_win = game.score_a > game.score_b
        canvas.text(x, y, game.team_a, 12, bold=True, fill=WIN if a_win else DIM)
        x += canvas.measure(game.team_a, 12, bold=True) + 7
        score_text = f"{game.score_a}:{game.score_b}"
        canvas.text(x, y + 1, score_text, 11, fill=INK)
        x += canvas.measure(score_text, 11) + 7
        canvas.text(x, y, game.team_b, 12, bold=True, fill=DIM if a_win else WIN)
        canvas.text(
            PAD + 224,
            y + 3,
            canvas.fit(game.event, 10, WIDTH - PAD - 224 - 14),
            10,
            fill=DIM,
        )
        y += H2H_ROW
    return top + height + 10


def _h2h_line(canvas: Canvas, h2h: Any, top: float) -> float:
    """单局卡上的交手摘要，一行纯文字，返回结束时的 y。

    不套底色：上面那行「蓝 BLG ｜ 红 HLE」也是纯文字，中间夹一条独立色块
    会显得像没做完的组件。整块的高度也就 22，画底反而更挤。

    左边给总比分（谁占上风），右边压进最近三场的场次比分（连胜还是互有胜负）。
    """
    canvas.text(PAD + 2, top + 4, "交手", 9, fill=DIM)

    x = PAD + 36
    size = 11
    a_lead = h2h.wins_a > h2h.wins_b
    canvas.text(x, top + 2, h2h.team_a, size, bold=True, fill=WIN if a_lead else MUTED)
    x += canvas.measure(h2h.team_a, size, bold=True) + 6
    score = f"{h2h.wins_a}:{h2h.wins_b}"
    canvas.text(x, top + 2, score, size, bold=True, fill=INK)
    x += canvas.measure(score, size, bold=True) + 6
    canvas.text(x, top + 2, h2h.team_b, size, bold=True, fill=MUTED if a_lead else WIN)

    recent = "　".join(f"{game.date[5:]} {game.score_a}:{game.score_b}" for game in h2h.recent(3))
    if recent:
        canvas.text_right(WIDTH - PAD, top + 4, f"近 {h2h.count} 场　{recent}", 9, fill=DIM)
    return top + 22


def match_card(
    fonts: Fonts, icons: dict[str, bytes], detail: MatchDetail, *, source: str = ""
) -> bytes | None:
    """单场比赛：大比分 + 逐局简表。逐局细节看单局卡。"""
    if not fonts.available:
        return None
    match = detail.match
    games = detail.games
    h2h = detail.h2h if detail.h2h is not None and detail.h2h.ready else None
    h2h_block = 26 + len(h2h.recent(3)) * H2H_ROW + 10 + 10 if h2h else 0
    height = PAD + 40 + 118 + h2h_block + 36 + len(games) * 32 + 30 + PAD
    canvas = Canvas(fonts, WIDTH, height)

    subtitle = " · ".join(
        part for part in (match.tournament_name, match.round_name, f"BO{match.best_of}") if part
    )
    canvas.text(PAD, PAD, f"LOL 赛事 #{match.id}", 20, bold=True)
    if subtitle:
        canvas.text(PAD, PAD + 26, subtitle, 12, fill=MUTED)

    # 大比分区
    top = PAD + 40
    canvas.rrect(PAD, top, WIDTH - PAD * 2, 110, radius=12, fill=PANEL)
    canvas.icon(
        PAD + 22,
        top + 22,
        66,
        icons.get(match.flag_a),
        radius=10,
        placeholder=match.team_a[:3],
        size_hint=14,
        bg=PANEL_2,
    )
    canvas.text(
        PAD + 100,
        top + 30,
        match.team_a or "?",
        22,
        bold=True,
        fill=WIN if match.winner == match.team_a else INK,
    )
    canvas.text(PAD + 100, top + 64, "胜" if match.winner == match.team_a else " ", 12, fill=WIN)
    canvas.icon(
        WIDTH - PAD - 88,
        top + 22,
        66,
        icons.get(match.flag_b),
        radius=10,
        placeholder=match.team_b[:3],
        size_hint=14,
        bg=PANEL_2,
    )
    canvas.text_right(
        WIDTH - PAD - 100,
        top + 30,
        match.team_b or "?",
        22,
        bold=True,
        fill=WIN if match.winner == match.team_b else INK,
    )
    score = f"{match.score_a} : {match.score_b}" if match.started else "VS"
    canvas.text(WIDTH / 2, top + 34, score, 40, bold=True, anchor="mt")
    when = " ".join(part for part in (match.start_date, match.start_time) if part)
    canvas.text(WIDTH / 2, top + 82, when, 12, fill=MUTED, anchor="mt")

    # 逐局简表（有交手记录就先垫一块）
    y = top + 122
    if h2h:
        y = _h2h_panel(canvas, h2h, y)
    columns = (
        ("局", PAD + 14, "lt"),
        ("胜方", PAD + 72, "lt"),
        ("时长", PAD + 130, "lt"),
        ("击杀", PAD + 210, "lt"),
        ("经济", PAD + 300, "lt"),
        ("推塔", PAD + 410, "lt"),
        ("小龙", PAD + 490, "lt"),
        ("大龙", PAD + 570, "lt"),
        ("潮虫", PAD + 650, "lt"),
        ("伤害", PAD + 730, "lt"),
    )
    for label, x, anchor in columns:
        canvas.text(x, y, label, 11, fill=DIM, anchor=anchor)
    y += 20
    for game in games:
        canvas.line(PAD, y - 4, WIDTH - PAD, y - 4, fill=LINE)
        winner = {"a": match.team_a, "b": match.team_b}.get(game.win_side, "—")
        values = (
            f"第 {game.index} 局",
            winner,
            game.clock or "—",
            f"{game.kills_a}:{game.kills_b}",
            f"{_gold(game.gold_a)}:{_gold(game.gold_b)}",
            f"{game.tower_a}:{game.tower_b}" if game.enriched else "—",
            f"{game.dragon_a}:{game.dragon_b}",
            f"{game.baron_a}:{game.baron_b}",
            f"{game.grub_a}:{game.grub_b}" if game.enriched else "—",
            f"{_gold(game.damage_a)}:{_gold(game.damage_b)}",
        )
        for (label, x, anchor), value in zip(columns, values, strict=False):
            color = INK if label != "局" else MUTED
            canvas.text(x, y, canvas.fit(value, 13, 100), 13, fill=color, anchor=anchor)
        y += 32

    if source:
        canvas.text(PAD, height - PAD - 12, f"数据源 {source}", 11, fill=DIM)
    return canvas.to_bytes()


# ---------------------------------------------------------------- 单局详卡

PLAYER_ROW = 30
PLAYER_SUB = 22
TEAM_HEAD = 30

TIMELINE_H = 100  # 单层轨道的高度，多一层加 18
TIMELINE_LAYER = 18
DRAGON = 16


def _timeline_layers(game: Game) -> int:
    """这一局的时间线需要几层（1 或 2）。

    只有同一侧两条龙挨得比图标还近时才需要错层——大多数比赛是一层。
    先算出来再画，而不是一律按两层留白：那样没有密集龙的局会顶出一大块空白，
    看着像是图裂了。
    """
    total = max(1, game.duration_sec)
    span = WIDTH - PAD - 16 - (PAD + 46)
    peak = 0
    for side in ("a", "b"):
        previous: int | None = None
        layer = 0
        for event in (e for e in game.dragons if e.side == side):
            if previous is not None and (event.second - previous) / total * span < DRAGON + 2:
                layer = 1 - layer
            else:
                layer = 0
            peak = max(peak, layer)
            previous = event.second
    return 2 if peak else 1


def timeline_height(game: Game) -> float:
    return TIMELINE_H + (_timeline_layers(game) - 1) * TIMELINE_LAYER


def _timeline_panel(
    canvas: Canvas,
    icons: dict[str, bytes],
    game: Game,
    match: Match,
    top: float,
) -> float:
    """蓝 / 红双轨的龙击杀时间线，外面套一块底板，返回结束时的 y。

    比源站那张好在两处：

    1. 队伍竖着标在两条轨的左端，谁拿的不靠颜色猜；
    2. 同一侧两枚图标时间挨太近时自动换一行，不会叠成一坨——源站那张是直接盖上去的。

    团战画成轴下方一条暗轨 + 点亮段，不画成图标：十几根竖条挤在一起会跟龙图标
    抢注意力，而且看不出是「团战」还是「另一条龙」。

    只取两队 ``dragons`` 列表里的元素龙，不取顶层 ``dragon_event``：
    后者混着大龙和其他事件，照单全画会出现「时间线上 5 条龙、资源格写着 3 条」
    这种自相矛盾的画面。图标只按源站编号取图，不做中文名——编号与官方龙种对不上。
    """
    layers = _timeline_layers(game)
    height = TIMELINE_H + (layers - 1) * TIMELINE_LAYER
    canvas.rrect(PAD, top, WIDTH - PAD * 2, height, radius=10, fill=PANEL)

    total = max(1, game.duration_sec)
    left = PAD + 46
    right = WIDTH - PAD - 16
    span = right - left
    mid = top + 34 + (layers - 1) * TIMELINE_LAYER

    def x_of(second: int) -> float:
        return left + min(1.0, max(0.0, second / total)) * span

    # 主轴 + 每 10 分钟一根刻度
    canvas.line(left, mid, right, mid, fill=LINE)
    minutes = 0
    while minutes * 60 <= total:
        x = x_of(minutes * 60)
        canvas.line(x, mid - 3, x, mid + 3, fill=DIM)
        canvas.text(x, mid + 46, f"{minutes:02d}", 9, fill=DIM, anchor="mt")
        minutes += 10

    # 团战：先铺一条贯通的暗轨，再把有团战的时段点亮。
    # 直接画十几根独立小条会变成一串「沙粒」，读者得逐个去猜，读不出节奏。
    canvas.rrect(left, mid + 2, span, 3, radius=1.5, fill=PANEL_2)
    for fight in game.fights:
        start = x_of(fight.begin)
        end = max(start + 2, x_of(fight.end))
        canvas.rrect(start, mid + 2, end - start, 3, radius=1.5, fill=FIGHT)

    canvas.text_right(
        PAD + 40, mid - 21, canvas.fit(match.team_a or "?", 10, 38), 10, bold=True, fill=BLUE
    )
    canvas.text_right(
        PAD + 40, mid + 11, canvas.fit(match.team_b or "?", 10, 38), 10, bold=True, fill=RED
    )

    for side in ("a", "b"):
        events = [event for event in game.dragons if event.side == side]
        previous: float | None = None
        layer = 0
        for event in events:
            x = x_of(event.second)
            crowded = previous is not None and x - previous < DRAGON + 2
            layer = 1 - layer if crowded else 0
            previous = x
            if side == "a":
                y = mid - 24 - layer * TIMELINE_LAYER
            else:
                y = mid + 8 + layer * TIMELINE_LAYER
            canvas.icon(x - DRAGON / 2, y, DRAGON, icons.get(event.icon), radius=3)
    return top + height


def _player_rows(fonts: Fonts) -> float:
    return PLAYER_ROW + PLAYER_SUB


def game_card(
    fonts: Fonts,
    icons: dict[str, bytes],
    detail: MatchDetail,
    game: Game,
    *,
    source: str = "",
) -> bytes | None:
    """一局的完整体：资源对比 + 里程碑 + 禁选 + 双方五人（含技能/符文/出装）。"""
    if not fonts.available:
        return None
    match = detail.match
    squads = ((match.team_a, game.players_a, "a"), (match.team_b, game.players_b, "b"))
    squads = tuple(item for item in squads if item[1]) or squads[:1]
    has_first = _has_first(game)
    has_bans = _bans_ready(game.bans_a) or _bans_ready(game.bans_b)

    height = (
        PAD
        + 34  # 顶部标题
        + 66  # 比分区
        + 18  # 蓝红方那一行
        + (28 if detail.h2h is not None and detail.h2h.ready else 0)  # 交手一条
        + 68  # 资源网格（2 行）
        + (timeline_height(game) + 18 if game.timeline_ready else 0)
        + (30 if has_first else 0)
        + (56 if has_bans else 0)
        + sum(TEAM_HEAD + 26 + len(squad) * _player_rows(fonts) + 12 for _, squad, _ in squads)
        + 28
        + PAD
    )
    canvas = Canvas(fonts, WIDTH, height)

    # 顶部
    canvas.text(PAD, PAD, f"LOL 赛事 #{match.id} · 第 {game.index} 局", 18, bold=True)
    tail = " · ".join(
        part for part in (match.tournament_name, match.round_name, game.clock) if part
    )
    canvas.text_right(WIDTH - PAD, PAD + 4, tail, 12, fill=MUTED)

    # 比分区
    top = PAD + 34
    canvas.rrect(PAD, top, WIDTH - PAD * 2, 66, radius=10, fill=PANEL)
    canvas.icon(
        PAD + 12,
        top + 11,
        44,
        icons.get(game.flag_a or match.flag_a),
        radius=8,
        placeholder=match.team_a[:3],
        size_hint=10,
        bg=PANEL_2,
    )
    canvas.text(
        PAD + 66, top + 22, canvas.fit(match.team_a or "?", 18, 190), 18, bold=True, fill=BLUE
    )
    canvas.icon(
        WIDTH - PAD - 66,
        top + 11,
        44,
        icons.get(game.flag_b or match.flag_b),
        radius=8,
        placeholder=match.team_b[:3],
        size_hint=10,
        bg=PANEL_2,
    )
    canvas.text_right(
        WIDTH - PAD - 76,
        top + 22,
        canvas.fit(match.team_b or "?", 18, 190),
        18,
        bold=True,
        fill=RED,
    )
    canvas.text(
        WIDTH / 2, top + 7, f"{match.score_a} : {match.score_b}", 26, bold=True, anchor="mt"
    )
    canvas.text(WIDTH / 2, top + 42, "总比分", 9, fill=DIM, anchor="mt")

    y = top + 70
    if game.sides:
        canvas.text(PAD, y, game.sides, 10, fill=MUTED)
    win_side = {"a": "蓝方胜", "b": "红方胜"}.get(game.win_side, "")
    if win_side:
        canvas.text_right(WIDTH - PAD, y, win_side, 10, fill=WIN)
    y += 18

    h2h = detail.h2h if detail.h2h is not None and detail.h2h.ready else None
    if h2h:
        y = _h2h_line(canvas, h2h, y)
        y += 6

    pairs = [
        ("击杀", game.kills_a, game.kills_b, False),
        ("经济", game.gold_a, game.gold_b, True),
        ("推塔", game.tower_a, game.tower_b, False),
        ("小龙", game.dragon_a, game.dragon_b, False),
        ("大龙", game.baron_a, game.baron_b, False),
        ("先锋", game.herald_a, game.herald_b, False),
        ("潮虫", game.grub_a, game.grub_b, False),
        ("远古龙", game.elder_a, game.elder_b, False),
    ]
    for index, (label, left, right, is_gold) in enumerate(pairs):
        column = index % 4
        row = index // 4
        cell_w = (WIDTH - PAD * 2) / 4
        x = PAD + column * cell_w
        cy = y + row * 34
        canvas.rrect(x + 2, cy, cell_w - 6, 30, radius=6, fill=PANEL)
        canvas.text(x + cell_w / 2, cy + 3, label, 10, fill=DIM, anchor="mt")
        left_text = _gold(left) if is_gold else str(left)
        right_text = _gold(right) if is_gold else str(right)
        canvas.text(
            x + cell_w / 2 - 8,
            cy + 14,
            left_text,
            13,
            fill=BLUE if left >= right else INK,
            bold=True,
            anchor="rt",
        )
        canvas.text(x + cell_w / 2, cy + 14, ":", 11, fill=DIM, anchor="mt")
        canvas.text(
            x + cell_w / 2 + 8,
            cy + 14,
            right_text,
            13,
            fill=RED if right >= left else INK,
            bold=True,
            anchor="lt",
        )
    y += 34 * 2 + 6

    # 龙线（只有拿到官方直播数据的局才有）
    if game.timeline_ready:
        y += 10
        y = _timeline_panel(canvas, icons, game, match, y)
        y += 8

    # 里程碑
    if has_first:
        canvas.text(PAD, y, "里程碑", 11, fill=DIM)
        marks = []
        for attribute, label in (
            ("first_blood", "一血"),
            ("first_tower", "一塔"),
            ("first_dragon", "一龙"),
            ("first_herald", "首先锋"),
            ("first_baron", "首大龙"),
        ):
            side = getattr(game, attribute)
            if side:
                team = match.team_a if side == "a" else match.team_b
                marks.append(f"{label} {team}")
        canvas.text(PAD + 56, y, "　".join(marks), 11, fill=MUTED)
        y += 30

    # 禁选。只列认得出的名字——卡片不像等宽文字，没有「五个位置要对齐」的约束，
    # 摆一排「？」只是噪音，末尾标一句还有几个没认出来就够。
    if has_bans:
        for label, bans, color in (
            (match.team_a, game.bans_a, BLUE),
            (match.team_b, game.bans_b, RED),
        ):
            known = [name for name in bans if name]
            if len(known) < 3:
                continue
            canvas.text(PAD, y + 2, "BAN", 10, fill=DIM)
            canvas.text(PAD + 34, y, label, 11, fill=color, bold=True)
            names = "  ".join(known)
            if len(known) < len(bans):
                names += f"　（{len(bans) - len(known)} 个未识别）"
            canvas.text(
                PAD + 34 + 62, y, canvas.fit(names, 12, WIDTH - PAD * 2 - 116), 12, fill=MUTED
            )
            y += 26
        y += 4

    # 双方五人
    for label, squad, side in squads:
        if not squad:
            continue
        canvas.rrect(
            PAD, y, WIDTH - PAD * 2, TEAM_HEAD - 4, radius=6, fill=PANEL_2 if side == "a" else PANEL
        )
        flag = game.flag_a if side == "a" else game.flag_b
        canvas.icon(PAD + 8, y + 3, 20, icons.get(flag or ""), radius=4)
        canvas.text(PAD + 36, y + 4, label or "?", 14, bold=True, fill=BLUE if side == "a" else RED)
        canvas.text_right(
            WIDTH - PAD - 12,
            y + 6,
            f"击杀 {sum(p.kills for p in squad)}　经济 {_gold(sum(p.gold for p in squad))}"
            f"　伤害 {_gold(sum(p.damage for p in squad))}",
            10,
            fill=MUTED,
        )
        y += TEAM_HEAD
        y = _player_table(canvas, fonts, icons, squad, y)
        y += 12

    if source:
        canvas.text(PAD, height - PAD - 12, f"数据源 {source}", 11, fill=DIM)
    return canvas.to_bytes()


def _has_first(game: Game) -> bool:
    return any(
        getattr(game, attribute)
        for attribute in (
            "first_blood",
            "first_tower",
            "first_dragon",
            "first_herald",
            "first_baron",
        )
    )


def _player_table(
    canvas: Canvas, fonts: Fonts, icons: dict[str, bytes], squad: list[PlayerStat], y: float
) -> float:
    """一名选手两行：数据行 + 技能/符文/出装行。返回结束时的 y。"""
    # 列位置（逻辑像素，全部相对 PAD）
    col_nick = PAD + 8
    col_hero = PAD + 130
    col_lv = PAD + 268
    col_kda = PAD + 300
    col_gold = PAD + 400
    col_cs = PAD + 470
    col_dmg = PAD + 528
    col_part = PAD + 596
    col_taken = PAD + 656
    col_score = PAD + 730

    canvas.text(col_nick, y + 8, "选手", 10, fill=DIM)
    canvas.text(col_hero, y + 8, "英雄", 10, fill=DIM)
    canvas.text(col_lv, y + 8, "Lv", 10, fill=DIM)
    canvas.text(col_kda, y + 8, "KDA", 10, fill=DIM)
    canvas.text(col_gold, y + 8, "经济", 10, fill=DIM)
    canvas.text(col_cs, y + 8, "补刀", 10, fill=DIM)
    canvas.text(col_dmg, y + 8, "伤害%", 10, fill=DIM)
    canvas.text(col_part, y + 8, "参团", 10, fill=DIM)
    canvas.text(col_taken, y + 8, "承伤", 10, fill=DIM)
    canvas.text(col_score, y + 8, "评分", 10, fill=DIM)
    y += 24

    for player in squad:
        if player.mvp:
            canvas.rrect(PAD + 2, y, WIDTH - PAD * 2 - 4, _player_rows(fonts), radius=6, fill=PANEL)
        canvas.text(
            col_nick,
            y + 3,
            canvas.fit(player.nickname or "?", 13, 116),
            13,
            bold=True,
            fill=WIN if player.mvp else INK,
        )
        canvas.icon(col_hero - 22, y + 2, 22, icons.get(player.hero_icon), radius=4)
        canvas.text(col_hero, y + 4, canvas.fit(player.champion or "?", 12, 92), 12)
        canvas.text(col_lv, y + 5, str(player.hero_level or "—"), 11, fill=MUTED)
        canvas.text(
            col_kda, y + 3, player.kda, 13, bold=True, fill=GREEN if player.deaths == 0 else INK
        )
        canvas.text(col_gold, y + 4, _gold(player.gold), 12, fill=MUTED)
        canvas.text(col_cs, y + 4, str(player.cs or "—"), 12, fill=MUTED)
        canvas.text(
            col_dmg,
            y + 4,
            f"{player.damage_pct:.0f}%" if player.damage_pct else "—",
            12,
            fill=MUTED,
        )
        canvas.text(col_part, y + 4, f"{player.part:.0f}%" if player.part else "—", 12, fill=MUTED)
        canvas.text(col_taken, y + 4, _gold(player.damage_taken), 12, fill=MUTED)
        canvas.text(
            col_score, y + 4, f"{player.score:.1f}" if player.score else "—", 12, fill=MUTED
        )

        # 第二行：召唤师技能、符文、出装
        sub_y = y + PLAYER_ROW
        x = col_nick
        for index, icon_url in enumerate(player.spell_icons[:2]):
            canvas.icon(x + index * 20, sub_y, 18, icons.get(icon_url), radius=3)
        perk_text = player.perks.short if player.perks else ""
        if perk_text:
            canvas.text(col_nick + 44, sub_y + 3, canvas.fit(perk_text, 10, 300), 10, fill=DIM)
        item_x = col_nick + 356
        for index, item_url in enumerate(player.items):
            canvas.icon(item_x + index * 22, sub_y, 20, icons.get(item_url), radius=3)
        if player.beiguo:
            _honor_chip(canvas, y)
        y += _player_rows(fonts)

    canvas.line(PAD, y, WIDTH - PAD, y, fill=LINE)
    return y


def _honor_chip(canvas: Canvas, y: float) -> None:
    """「背锅」做成贴在该选手名字行右侧的小标签。

    之前贴在第二行右端，离选手名隔着一整行，看着像漂在卡片边缘的孤立文字；
    挪到名字这一行才能明确它属于谁。
    """
    label = "背锅"
    font_size = 10
    width = canvas.measure(label, font_size) + 14
    x = WIDTH - PAD - width
    canvas.rrect(x, y + 1, width, 17, radius=8, fill=RED_BG)
    canvas.text(x + width / 2, y + 3, label, font_size, fill=RED, anchor="mt")


# ---------------------------------------------------------------- 门面


class CardRenderer:
    """把「数据 + 图标」变成 PNG。任何一步失败都返回 ``None``，调用方退回文字。"""

    def __init__(self, icon_dir: Path, *, enabled: bool = True, scale: int = 2) -> None:
        self.fonts = Fonts(scale=scale)
        self.store = IconStore(icon_dir, enabled=enabled)
        self._enabled = enabled
        self._warned = False

    @property
    def available(self) -> bool:
        return self._enabled and self.fonts.available

    async def close(self) -> None:
        await self.store.close()

    def _fail(self, exc: Exception | None = None) -> None:
        if not self._warned:
            self._warned = True
            detail = f"：{type(exc).__name__}: {exc}" if exc else "（找不到中文字体）"
            logger.warning(f"[LOL赛事] 卡片渲染不可用，改用纯文字{detail}")

    async def schedule(
        self,
        *,
        title: str,
        matches: list[Match],
        limit: int = 20,
        source: str = "",
        subtitle: str = "",
    ) -> bytes | None:
        if not self.available:
            self._fail()
            return None
        try:
            icons = await self.store.warm(_collect_urls(matches, []))
            return schedule_card(
                self.fonts,
                icons,
                title=title,
                matches=matches,
                limit=limit,
                source=source,
                subtitle=subtitle,
            )
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
            return None

    async def tournament(
        self,
        *,
        title: str,
        matches: list[Match],
        subtitle: str = "",
        limit: int = 40,
        source: str = "",
        total: int = 0,
    ) -> bytes | None:
        """联赛详情卡：阶段 → 日期，每场带比赛 ID。"""
        if not self.available:
            self._fail()
            return None
        try:
            icons = await self.store.warm(_collect_urls(matches, []))
            return schedule_card(
                self.fonts,
                icons,
                title=title,
                matches=matches,
                limit=limit,
                source=source,
                subtitle=subtitle,
                show_id=True,
                group="stage",
                total=total,
            )
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
            return None

    async def match(self, detail: MatchDetail, *, source: str = "") -> bytes | None:
        if not self.available:
            self._fail()
            return None
        try:
            icons = await self.store.warm(_collect_urls([detail.match], detail.games))
            return match_card(self.fonts, icons, detail, source=source)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
            return None

    async def game(self, detail: MatchDetail, game: Game, *, source: str = "") -> bytes | None:
        if not self.available:
            self._fail()
            return None
        try:
            icons = await self.store.warm(_collect_urls([detail.match], [game]))
            return game_card(self.fonts, icons, detail, game, source=source)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
            return None

    async def game_set(
        self, detail: MatchDetail, *, limit: int = 1, source: str = ""
    ) -> list[bytes]:
        """按需出若干张单局卡。``limit`` 限制张数，避免一次刷一屏图。"""
        out: list[bytes] = []
        for game in detail.games[:limit] if limit > 0 else detail.games:
            png = await self.game(detail, game, source=source)
            if png:
                out.append(png)
        return out
