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

from .models import Game, Match, MatchDetail, PlayerStat

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
        for player in (*game.players_a, *game.players_b):
            urls.append(player.hero_icon)
            urls.extend(player.spell_icons)
            urls.extend(player.items)
    return [url for url in urls if url]


# ---------------------------------------------------------------- 赛程卡


def _schedule_rows(matches: list[Match], limit: int) -> list[tuple[str, list[Match]]]:
    grouped: dict[str, list[Match]] = {}
    for match in matches[:limit] if limit > 0 else matches:
        grouped.setdefault(match.tournament_name or "其他联赛", []).append(match)
    return list(grouped.items())


def schedule_card(
    fonts: Fonts,
    icons: dict[str, bytes],
    *,
    title: str,
    matches: list[Match],
    limit: int = 20,
    source: str = "",
) -> bytes | None:
    """今日 / 本周赛程。一行一场，队标 26px。"""
    if not fonts.available:
        return None
    groups = _schedule_rows(matches, limit)
    rows = sum(len(items) for _, items in groups)
    height = PAD + 44 + len(groups) * 26 + rows * 46 + 26 + PAD
    canvas = Canvas(fonts, WIDTH, height)

    canvas.text(PAD, PAD, title, 24, bold=True)
    canvas.text_right(WIDTH - PAD, PAD + 8, f"共 {len(matches)} 场", 13, fill=MUTED)

    y = PAD + 44
    if not groups:
        canvas.text(PAD, y + 10, "这一天没有比赛。", 16, fill=MUTED)
        return canvas.to_bytes()

    for tournament, items in groups:
        canvas.text(PAD, y, f"【{tournament}】", 14, fill=BLUE, bold=True)
        y += 26
        for match in items:
            canvas.rrect(PAD, y, WIDTH - PAD * 2, 42, radius=8, fill=PANEL)
            canvas.text(PAD + 14, y + 12, match.start_time or "--:--", 15, fill=INK, bold=True)
            tag = f"BO{match.best_of}" if match.best_of else ""
            if tag:
                canvas.text(PAD + 66, y + 15, tag, 10, fill=DIM)
            canvas.icon(
                PAD + 96,
                y + 8,
                26,
                icons.get(match.flag_a),
                placeholder=match.team_a[:2],
                size_hint=9,
                bg=PANEL_2,
            )
            canvas.text(PAD + 130, y + 12, canvas.fit(match.team_a or "?", 15, 200), 15)
            middle = match.score_line if match.started else "vs"
            canvas.text(
                WIDTH / 2,
                y + 12,
                middle,
                15,
                fill=WIN if match.finished else MUTED,
                bold=match.finished,
                anchor="mt",
            )
            canvas.text_right(
                WIDTH - PAD - 48, y + 12, canvas.fit(match.team_b or "?", 15, 200), 15
            )
            canvas.icon(
                WIDTH - PAD - 40,
                y + 8,
                26,
                icons.get(match.flag_b),
                placeholder=match.team_b[:2],
                size_hint=9,
                bg=PANEL_2,
            )
            y += 46

    if source:
        canvas.text(PAD, height - PAD - 14, f"数据源 {source}", 11, fill=DIM)
    return canvas.to_bytes()


# ---------------------------------------------------------------- 比赛摘要卡


def match_card(
    fonts: Fonts, icons: dict[str, bytes], detail: MatchDetail, *, source: str = ""
) -> bytes | None:
    """单场比赛：大比分 + 逐局简表。逐局细节看单局卡。"""
    if not fonts.available:
        return None
    match = detail.match
    games = detail.games
    height = PAD + 40 + 118 + 36 + len(games) * 32 + 30 + PAD
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

    # 逐局简表
    y = top + 122
    columns = (
        ("局", PAD + 8, "lt"),
        ("胜方", PAD + 68, "lt"),
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
        + 68  # 资源网格（2 行）
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
        self, *, title: str, matches: list[Match], limit: int = 20, source: str = ""
    ) -> bytes | None:
        if not self.available:
            self._fail()
            return None
        try:
            icons = await self.store.warm(_collect_urls(matches, []))
            return schedule_card(
                self.fonts, icons, title=title, matches=matches, limit=limit, source=source
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
