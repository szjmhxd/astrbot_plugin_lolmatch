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

from .models import (
    MARK_ADVANCE,
    MARK_CHAMPION,
    MARK_DROP,
    MARK_OUT,
    MARK_RUNNER,
    MARK_TEXT,
    DoubleElim,
    Game,
    KnockoutRow,
    Match,
    MatchDetail,
    PlayerStat,
    SwissLane,
    SwissRow,
    bracket_columns,
    bracket_edges,
    bracket_rounds,
    bracket_strict,
    date_span,
    day_label,
    is_tbd,
    stage_groups,
    swiss_round_dates,
    usage_hint,
)

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

    def seg(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        fill: str = LINE,
        width: float = 1,
        dash: float = 0,
    ) -> None:
        """画一条横平竖直的线，``dash`` 非零就是虚线。

        Pillow 没有虚线样式，只能自己按段画。只支持水平/垂直是有意为之：
        瑞士轮和晋级图的连线全是直角折线，斜线一条都不该有。
        """
        s = self.scale
        pen = max(1, int(width * s))
        length = abs(x2 - x1) + abs(y2 - y1)
        if dash <= 0 or length <= 0:
            self.draw.line(
                [int(x1 * s), int(y1 * s), int(x2 * s), int(y2 * s)], fill=fill, width=pen
            )
            return
        step = max(1.5, dash)
        cursor = 0.0
        while cursor < length:
            end = min(cursor + step, length)
            ratio_a, ratio_b = cursor / length, end / length
            self.draw.line(
                [
                    int((x1 + (x2 - x1) * ratio_a) * s),
                    int((y1 + (y2 - y1) * ratio_a) * s),
                    int((x1 + (x2 - x1) * ratio_b) * s),
                    int((y1 + (y2 - y1) * ratio_b) * s),
                ],
                fill=fill,
                width=pen,
            )
            cursor = end + step * 0.75

    def vtext(
        self,
        x: float,
        y: float,
        text: str,
        size: int,
        *,
        fill: str = INK,
    ) -> None:
        """竖排（逆时针 90 度）的短标签，给分档条用。

        中文竖排一个字一个位置很难看，旋转整行才是排版做法；Pillow 没有文字旋转，
        只能先画到一张小图上再转。
        """
        if not text:
            return
        s = self.scale
        font = self.fonts.get(size, bold=True)
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        box = probe.textbbox((0, 0), text, font=font)
        tw, th = box[2] - box[0] + 4, box[3] - box[1] + 4
        tile = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        ImageDraw.Draw(tile).text((-box[0] + 2, -box[1] + 2), text, font=font, fill=fill)
        tile = tile.rotate(90, expand=True)
        self.img.paste(
            tile,
            (int(x * s) - tile.width // 2, int(y * s) - tile.height // 2),
            tile,
        )

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

HEAD_MAJOR = 22  # 阶段 / 联赛标题
HEAD_MINOR = 18  # 日期小标题
ROW_H = 42
ROW_ID_H = 48  # 多一行比赛 ID


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
    # 标题里已经写过的那个日期。联赛 ID 跟在名字后，照着就能发「联赛详情」。
    days = {m.start_date for m in shown}
    grouped: dict[tuple[str, str, str], list[Match]] = {}
    for match in shown:
        key = (match.start_date, match.tournament_name or "其他联赛", match.tournament_id)
        grouped.setdefault(key, []).append(match)
    for (day, tournament, tid), items in grouped.items():
        tag = f" #{tid}" if tid else ""
        head = f"{day_label(day)}　{tournament}{tag}" if len(days) > 1 else f"【{tournament}{tag}】"
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
    clock_y = y + 8 if show_id else y + 10
    canvas.text(PAD + 14, clock_y, match.start_time or "--:--", 15, fill=INK, bold=True)
    if show_id:
        # 10px + MUTED：聊天软件会把长图整体缩小，DIM 灰的 10px 缩完就看不清了，
        # 而这行编号正是「联赛详情」这张卡存在的理由之一。
        canvas.text(PAD + 14, y + 26, f"#{match.id}", 10, fill=MUTED)
    tag = f"BO{match.best_of}" if match.best_of else ""
    if tag:
        canvas.text(PAD + 66, y + 13, tag, 10, fill=DIM)
    # 对阵未定的场次（淘汰赛后几轮）压暗：日期是真的，队伍还没定
    name_fill = MUTED if match.pending else INK
    canvas.icon(
        PAD + 96,
        y + 9 if show_id else y + 6,
        26,
        None if match.pending else icons.get(match.flag_a),
        placeholder="" if match.pending else match.name_a[:2],
        size_hint=9,
        bg=PANEL_2,
    )
    canvas.text(
        PAD + 130,
        y + 11 if show_id else y + 10,
        canvas.fit(match.name_a, 15, 200),
        15,
        fill=name_fill,
    )
    middle = match.score_line if match.started else "vs"
    canvas.text(
        WIDTH / 2,
        y + 11 if show_id else y + 10,
        middle,
        15,
        fill=WIN if match.finished else MUTED,
        bold=match.finished,
        anchor="mt",
    )
    canvas.text_right(
        WIDTH - PAD - 48,
        y + 11 if show_id else y + 10,
        canvas.fit(match.name_b, 15, 200),
        15,
        fill=name_fill,
    )
    canvas.icon(
        WIDTH - PAD - 40,
        y + 9 if show_id else y + 6,
        26,
        None if match.pending else icons.get(match.flag_b),
        placeholder="" if match.pending else match.name_b[:2],
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
    head_h = 40 + (20 if subtitle else 0)
    height = PAD + head_h + max(body, 40) + 26 + PAD
    canvas = Canvas(fonts, WIDTH, height)

    canvas.text(PAD, PAD, title, 24, bold=True)
    canvas.text_right(WIDTH - PAD, PAD + 8, f"共 {total or len(matches)} 场", 13, fill=MUTED)

    y = PAD + 40
    if subtitle:
        canvas.text(PAD, y, subtitle, 12, fill=MUTED)
        y += 20
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
    first = matches[0] if matches else None
    _foot_hint(
        canvas,
        usage_hint(first.tournament_id if first else "", first.id if first else ""),
        height - PAD - 14,
    )
    return canvas.to_bytes()


def _foot_hint(canvas: Canvas, hint: str, y: float, *, right: float | None = None) -> None:
    """图右下角的查询提示。跟「数据源」同一行，左源右提示。"""
    if not hint:
        return
    edge = canvas.width - PAD if right is None else right
    canvas.text_right(edge, y, canvas.fit(hint, 10, max(120.0, edge - PAD - 200)), 10, fill=DIM)


# ---------------------------------------------------------------- 晋级图

BRACKET_BOX_H = 62
BRACKET_GAP = 46  # 列间水平空隙，连线画在这里
BRACKET_VGAP = 22  # 同列上下两场的距离（要放得下下一场的日期行）
BRACKET_HEAD = 15  # 每场上面那行日期
BRACKET_LINE = "#2f6b86"  # 晋级线：比灰线更有方向感，又不抢胜方的蓝
BRACKET_LEGEND = (
    (MARK_TEXT[MARK_ADVANCE], "#3fb950", ""),
    (MARK_TEXT[MARK_OUT], "#ff7b85", ""),
    (MARK_TEXT[MARK_CHAMPION], "#f5c542", ""),
    (MARK_TEXT[MARK_RUNNER], "#a8b6c8", ""),
)
BRACKET_GOLD = "#f5c542"  # 总决赛：金框 + 金色行头
BRACKET_GOLD_BG = "#6b5314"

# 结局标记的配色。金/红两系里各有两个含义（夺冠 vs 晋级、淘汰 vs 跌入败者），
# 光靠颜色分不开，所以标记一律配文字，颜色只是加速识别。
MARK_TONE = {
    MARK_ADVANCE: "#3fb950",
    MARK_OUT: "#ff7b85",
    MARK_DROP: "#ffb020",
    MARK_CHAMPION: BRACKET_GOLD,
    MARK_RUNNER: "#a8b6c8",
}

# 淘汰赛右侧面板（双败 / 单败共用）：最终名次 / 当前积分。
# 宽度与瑞士轮积分榜一致，整套图里两张榜长得一样，读者不用重新学。
KO_PANEL_W = 292
KO_PANEL_GAP = 44
KO_ROW_H = 36  # 每队两行：名次 / 队名 / 战绩，下面跟一行旅程线

# 状态徽章：(文字色, 底色)。徽章是实心药丸，缩图后仍认得出冠军在哪。
KO_BADGE = {
    "冠军": ("#221a03", BRACKET_GOLD),
    "亚军": ("#1b2430", "#c9d4e4"),
    "四强": ("#0b1c33", "#58a6ff"),
    "八强": ("#d5dde8", "#2b3543"),
    "十六强": ("#d5dde8", "#2b3543"),
    "在战": ("#0d2818", "#3fb950"),
}


def _knockout_panel_height(rows: int) -> float:
    return 118 + rows * KO_ROW_H + 26


def _knockout_panel(
    canvas: Canvas,
    icons: dict[str, bytes],
    rows: list[KnockoutRow],
    x: float,
    y: float,
    *,
    total: int,
    double: bool,
    bo: int = 0,
    source: str = "",
) -> float:
    """右侧「最终名次 / 当前积分」面板：三格统计 + 名次表 + 每队旅程线。

    画法对标赛事官网的名次板：先看冠军和赛制，再看每队走到哪。
    旅程线是这个面板存在的理由——双败图里两支队的负场数一样，
    看不出谁是从败者组爬回来的；「胜 C9 → 胜 MVK → 负 KC」一行讲完一支队的路。
    """
    height = _knockout_panel_height(len(rows))
    canvas.rrect(x, y, KO_PANEL_W, height, radius=12, fill="#0e131c")

    finished = any(row.state == "champion" for row in rows)
    champion = next((row.team for row in rows if row.state == "champion"), "")
    canvas.text(x + 12, y + 12, "最终名次" if finished else "当前积分", 13, bold=True, fill=INK)

    rule = "两败出局" if double else "一败出局"
    bo_text = f"全程 BO{bo}" if bo else ("双败" if double else "单败")
    canvas.text(
        x + 12,
        y + 33,
        f"{len(rows)} 队 · {total} 场 · {bo_text} · {rule}",
        9,
        fill=DIM,
    )

    cells = (
        ("冠军", champion or "待定", WIN if champion else DIM),
        ("总场次", str(total), MUTED),
        ("赛制", f"BO{bo}" if bo else ("双败" if double else "单败"), MUTED),
    )
    cell_w = (KO_PANEL_W - 24) / 3
    for index, (label, value, tone) in enumerate(cells):
        cx = x + 12 + index * cell_w
        canvas.text(
            cx, y + 52, canvas.fit(value, 15, cell_w - 8, bold=True), 15, fill=tone, bold=True
        )
        canvas.text(cx + 1, y + 76, label, 9, fill=DIM)

    canvas.line(x + 12, y + 92, x + KO_PANEL_W - 12, y + 92)

    for label, cx in (
        ("名次", x + 12),
        ("战队", x + 44),
        ("大场", x + 196),
        ("小局", x + 244),
    ):
        canvas.text(cx, y + 100, label, 9, fill=DIM)
    canvas.text_right(x + KO_PANEL_W - 14, y + 100, "状态", 9, fill=DIM)

    top = y + 118
    for index, row in enumerate(rows):
        row_y = top + index * KO_ROW_H
        if index:
            canvas.line(x + 12, row_y, x + KO_PANEL_W - 12, row_y, fill="#1a2231")

        gone = row.state == "out"
        tone = MUTED if gone else INK
        rank_text = str(row.rank) if row.rank else "—"
        canvas.text(x + 14, row_y + 5, rank_text, 10, fill=DIM, bold=True)
        canvas.icon(
            x + 36,
            row_y + 5,
            16,
            icons.get(row.flag) if row.flag else None,
            radius=3,
            placeholder=row.team[:2],
            size_hint=7,
            bg=PANEL_2,
        )
        canvas.text(
            x + 57,
            row_y + 5,
            canvas.fit(row.team, 10.5, 86, bold=True),
            10.5,
            fill=tone,
            bold=True,
        )
        canvas.text_right(x + 196, row_y + 5, row.record, 10.5, fill=tone, bold=True)
        canvas.text_right(x + 244, row_y + 5, row.games, 9.5, fill=MUTED)

        badge = row.badge
        ink_badge, bg_badge = KO_BADGE.get(badge, ("#d5dde8", "#2b3543"))
        bw = canvas.measure(badge, 8.5, bold=True) + 12
        canvas.rrect(x + KO_PANEL_W - 14 - bw, row_y + 4, bw, 15, radius=7.5, fill=bg_badge)
        canvas.text(
            x + KO_PANEL_W - 14 - bw + 6, row_y + 5.5, badge, 8.5, fill=ink_badge, bold=True
        )

        if row.path_text:
            # 旅程线占整行的第二行，右边到徽章列都是空的——放开到接近全宽，
            # 「胜 TSW → 胜 G2 → 负 BLG → 胜 Lyon → 胜 BLG」这种五段路一行放下
            canvas.text(
                x + 34,
                row_y + 22,
                canvas.fit(row.path_text, 7.5, KO_PANEL_W - 62),
                7.5,
                fill=DIM,
            )

    if source:
        canvas.text(x + 14, y + height - 18, f"数据源 {source}", 8.5, fill=DIM)
    return height


def _bracket_box(
    canvas: Canvas,
    icons: dict[str, bytes],
    match: Match,
    x: float,
    y: float,
    w: float,
    *,
    marks: tuple[str, str] = ("", ""),
    gold: bool = False,
) -> None:
    """一场比赛画成一个双行小块：上队一行、下队一行，胜方整行填色。

    填色而不是只给文字上色，是因为这张图经常被平台压到手机宽度的一半，
    13px 的字到那时候已经糊了，色块还认得出。

    ``marks`` 是两行的结局标记（晋级 / 淘汰 / 夺冠 / 亚军 / 跌入败者），
    写在比分左边的小字。只靠颜色分不出「晋级」和「夺冠」——两者都是金色系，
    而「跌入败者组」和「淘汰」都是红色系，文字是唯一无歧义的信号。

    ``gold`` 给总决赛用：金框 + 金头，视觉上从两条轨道里脱出来。
    """
    head = BRACKET_HEAD
    canvas.text(
        x,
        y - head + 1,
        match.clock,
        9.5,
        fill=DIM if not gold else BRACKET_GOLD,
    )
    parts = [f"#{match.id}" if match.id else "", f"BO{match.best_of}" if match.best_of else ""]
    tail = " · ".join(part for part in parts if part)
    if tail:
        canvas.text_right(x + w, y - head + 1, tail, 9.5, fill=DIM if not gold else BRACKET_GOLD)

    top = y
    canvas.rrect(
        x,
        top,
        w,
        BRACKET_BOX_H,
        radius=6,
        fill=PANEL,
        outline=BRACKET_GOLD if gold else None,
        width=1.4,
    )

    rows = (
        (match.name_a, match.flag_a, match.score_a, match.score_a > match.score_b, marks[0]),
        (match.name_b, match.flag_b, match.score_b, match.score_b > match.score_a, marks[1]),
    )
    for index, (name, flag, score, won, mark) in enumerate(rows):
        row = index * (BRACKET_BOX_H // 2) + 4
        # 只有「已开打并且赢了」才填色；未开打和对阵未定都用中性灰
        decided = match.started
        win_now = decided and won
        fill = (BRACKET_GOLD_BG if gold else SD_BLUE) if win_now else SD_DARK
        ink = "#ffffff" if win_now else MUTED
        canvas.rrect(x + 3, top + row - 1, w - 6, BRACKET_BOX_H // 2, radius=4, fill=fill)
        if not match.pending:  # 待定行不画队标位：空灰块比留白更碍眼
            canvas.icon(
                x + 6,
                top + row,
                20,
                icons.get(flag),
                radius=3,
                placeholder=name[:2],
                size_hint=8,
                bg=PANEL_2,
            )
        mark_w = len(mark) * 8.5 + 6 if mark else 0
        canvas.text(
            x + 32,
            top + row + 3,
            canvas.fit(name, 13, w - 74 - mark_w, bold=win_now),
            13,
            fill=ink,
            bold=win_now,
        )
        if mark:
            canvas.text_right(
                x + w - 38,
                top + row + 5,
                MARK_TEXT.get(mark, mark),
                8.5,
                fill=MARK_TONE.get(mark, MUTED),
                bold=True,
            )
        canvas.text_right(
            x + w - 8,
            top + row + 3,
            str(score) if decided else "–",
            13,
            fill=ink,
            bold=win_now,
        )


def _stack_column(wanted: list[float], top: float, pitch: float) -> list[float]:
    """把一列按下标摆开：先按期望位置排序，挤住就往下推，最后整列拉回重心。"""
    if not wanted:
        return []
    order = sorted(range(len(wanted)), key=lambda k: wanted[k])
    placed = [0.0] * len(wanted)
    cursor = top
    for k in order:
        y = max(wanted[k], cursor)
        placed[k] = y
        cursor = y + pitch
    if len(wanted) > 1:
        shift = (sum(wanted) - sum(placed)) / len(wanted)
        placed = [y + shift for y in placed]
    lowest = min(placed)
    if lowest < top:
        placed = [y + (top - lowest) for y in placed]
    return placed


def _edge_from_winner(parent: Match, child: Match) -> bool | None:
    """连线是不是「上一场的胜者往上走」。两队都没名的占位场次返回 ``None``。"""
    for team in (child.team_a, child.team_b):
        if team and team in (parent.team_a, parent.team_b):
            if not parent.finished:
                return None
            return parent.winner == team
    return None


def bracket_card(
    fonts: Fonts,
    icons: dict[str, bytes],
    *,
    title: str,
    columns: list[list[Match]],
    marks: dict[str, tuple[str, str]] | None = None,
    subtitle: str = "",
    source: str = "",
    standings: list[KnockoutRow] | None = None,
) -> bytes | None:
    """单败晋级图：横向多列，列间画直角连线，胜方高亮，未定的写「待定」。

    列宽随列数自适应，但封顶 320——两列的赛段别拉成一整条长砖。
    ``standings`` 给出时右侧加「最终名次」面板：树管「谁赢了谁」，
    榜管「每支队走到哪」，一张图回答两个问题。
    """
    if not fonts.available or not columns:
        return None

    cols = [col for col in columns if col]
    if not cols:
        return None

    inner = WIDTH - PAD * 2
    box_w = min(320.0, (inner - BRACKET_GAP * (len(cols) - 1)) / len(cols))
    used = box_w * len(cols) + BRACKET_GAP * (len(cols) - 1)
    x0 = (WIDTH - used) / 2

    top = PAD + 104
    pitch = BRACKET_BOX_H + BRACKET_VGAP
    rounds = bracket_rounds(cols)
    edges = bracket_edges(cols, strict=bracket_strict(cols))
    # 决赛 = 最后一列唯一那场。金框让它在整列卡片里一眼可辨——
    # 单败图读者最先找的就是「谁夺冠」
    champion = cols[-1][0] if len(cols[-1]) == 1 else None

    # 纵向位置：首列等距铺开，其余每场对准**真实来路**的中心。
    # 不能按 j//2 算——双败赛段（MSI 淘汰赛 7→4）那个位置关系根本不存在，
    # 按位置摆会出现「比赛悬在两条无关的线之间」。
    feeders: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for (pc, pr), (cc, cr) in edges:
        if cc == pc + 1:
            feeders.setdefault((cc, cr), []).append((pc, pr))

    ys: list[list[float]] = [[top + i * pitch for i in range(len(cols[0]))]]
    for index in range(1, len(cols)):
        prev = ys[index - 1]
        wanted: list[float] = []
        for j in range(len(cols[index])):
            parents = feeders.get((index, j))
            if parents:
                center = sum(prev[p] + BRACKET_BOX_H / 2 for _, p in parents) / len(parents)
                wanted.append(center - BRACKET_BOX_H / 2)
            else:  # 对阵未定的场次没有来路，按位置兜底
                wanted.append(top + j * pitch)
        ys.append(_stack_column(wanted, top, pitch))

    body_h = max(max(c) for c in ys) + BRACKET_BOX_H + 40 + PAD
    panel_x = WIDTH - PAD + KO_PANEL_GAP
    height = int(max(body_h, PAD + _knockout_panel_height(len(standings or [])) + PAD))
    canvas = Canvas(
        fonts,
        int(panel_x + KO_PANEL_W + PAD) if standings else WIDTH,
        height,
    )

    canvas.text(PAD, PAD, title, 22, bold=True)
    if subtitle:
        canvas.text_right(WIDTH - PAD, PAD + 6, subtitle, 12, fill=MUTED)

    # 图例：单败的四种结局全靠行内小字，字号只有 8.5，缩图后容易糊。
    # 这里给一次对照，读者知道「绿=晋级、红=淘汰、金=夺冠」就够了
    lx = WIDTH - PAD
    for label, tone, _ in reversed(BRACKET_LEGEND):
        lx -= canvas.measure(label, 9)
        canvas.text(lx, PAD + 34, label, 9, fill=DIM)
        lx -= 14
        canvas.rrect(lx - 4, PAD + 35, 8, 8, radius=2, fill=tone)
        lx -= 12

    # 先画连线再画块，线头被块盖住，不会露出来
    for (pc, pr), (cc, cr) in edges:
        if cc != pc + 1:
            continue
        left = x0 + pc * (box_w + BRACKET_GAP) + box_w
        right = x0 + cc * (box_w + BRACKET_GAP)
        mid = (left + right) / 2
        py = ys[pc][pr] + BRACKET_BOX_H / 2
        cy = ys[cc][cr] + BRACKET_BOX_H / 2
        # 双败赛段里「输着掉下来」和「赢着升上去」是两种去向，线得分开：
        # 入围赛 4 队就是双败，全画灰线的话，谁从胜者组掉下来完全看不出来
        won = _edge_from_winner(cols[pc][pr], cols[cc][cr])
        tone = BRACKET_LINE if won is not False else SD_AMBER
        dash = 0 if won is not False else 4.0
        canvas.seg(left + 3, py, mid, py, fill=tone, width=1.3, dash=dash)
        canvas.seg(mid, py, mid, cy, fill=tone, width=1.3, dash=dash)
        canvas.seg(mid, cy, right - 3, cy, fill=tone, width=1.3, dash=dash)

    for index, col in enumerate(cols):
        x = x0 + index * (box_w + BRACKET_GAP)
        canvas.text(x + 2, top - 46, rounds[index], 11, fill=WIN, bold=True)
        days = [match.start_date for match in col if match.start_date]
        if days:
            canvas.text(x + 2, top - 31, date_span(min(days), max(days)), 9, fill=DIM)
        for j, match in enumerate(col):
            _bracket_box(
                canvas,
                icons,
                match,
                x,
                ys[index][j],
                box_w,
                marks=(marks or {}).get(match.id, ("", "")),
                gold=match is champion,
            )

    if standings:
        bo = max((match.best_of for match in (m for c in cols for m in c)), default=0)
        _knockout_panel(
            canvas,
            icons,
            standings,
            panel_x,
            PAD,
            total=sum(len(col) for col in cols),
            double=False,
            bo=bo,
            source=source,
        )

    if source and not standings:
        canvas.text(PAD, height - PAD - 12, f"数据源 {source}", 11, fill=DIM)
    flat = [match for col in cols for match in col]
    _foot_hint(
        canvas,
        usage_hint(flat[0].tournament_id if flat else "", flat[0].id if flat else ""),
        height - PAD - 12,
        right=WIDTH - PAD,
    )
    return canvas.to_bytes()


# ---------------------------------------------------------------- 双败淘汰

# 双败不是一棵树，是**两条轨道 + 一场总决赛**：上面胜者组（赢着往上走），
# 下面败者组（输一场掉下来），右边金框总决赛。硬画成单败树的话，败者组
# 那几场会被塞进某一列里，看起来像「凭空多出来的比赛」，队伍旅程直接断。
#
# 列 = 日期而不是轮次：双败的两条轨道轮次对不齐（8 队时胜者组 3 轮、
# 败者组 4 轮），按日期排才是同一根时间轴，读者能顺着日子往下看。
DK_CARD_W = 246
DK_GAP = 78  # 列间净空，连线走廊
DK_BAND_PAD = 14
DK_BAND_TITLE = 24
DK_BAND_GAP = 30
DK_MAX_W = 2400
DK_LEGEND = (
    (MARK_TEXT[MARK_DROP], "#ffb020", ""),
    (MARK_TEXT[MARK_OUT], "#ff7b85", ""),
    (MARK_TEXT[MARK_CHAMPION], "#f5c542", ""),
    ("胜者续战", "#3bc1ff", "solid"),
    ("跌入败者组", "#ffb020", "dash"),
)
DK_WIN_TINT = "#131b2c"  # 胜者组带底：极淡的蓝
DK_LOSE_TINT = "#1d1710"  # 败者组带底：极淡的橙
DK_WIN_EDGE = "#2d51c8"
DK_LOSE_EDGE = "#c98a1e"


def _deck_metrics(cols: int, *, reserve: float = 0.0) -> tuple[float, float]:
    """双败图的卡宽与列间距：列多就收窄，别把图拉成一条长墙。

    ``reserve`` 是右侧积分面板要占的宽度——面板是固定宽度，
    得从列的预算里先扣掉，否则 6 列双败 + 面板会把整张图顶出屏幕。
    """
    gap = float(DK_GAP)
    card_w = float(DK_CARD_W)
    limit = DK_MAX_W - reserve
    total = PAD * 2 + cols * card_w + (cols - 1) * gap
    if total > limit and cols > 1:
        gap = max(56.0, gap - (total - limit) / (cols - 1))
        total = PAD * 2 + cols * card_w + (cols - 1) * gap
    if total > limit:
        card_w = (limit - PAD * 2 - (cols - 1) * gap) / cols
    return max(196.0, card_w), gap


def _box_row_y(top: float, side: int) -> float:
    """卡片内第 ``side`` 行（0 上 1 下）的中心 y。连线端点要落在这里。"""
    return top + BRACKET_BOX_H * 0.25 + side * (BRACKET_BOX_H / 2)


def _band_height(buckets: list[list[Match]], pitch: float) -> float:
    rows = max((len(bucket) for bucket in buckets), default=0)
    if not rows:
        return 0.0
    return rows * pitch - BRACKET_VGAP + DK_BAND_PAD * 2 + DK_BAND_TITLE


def _depth_rounds(matches: list[Match], *, winners: bool) -> list[list[Match]]:
    """把一组比赛按「轮次」分列。

    ``winners=True``（胜者组）直接用形状反推：1、2、4 递减，一场不漏。
    ``winners=False``（败者组）不能这么推——败者组的轮次不是等比递减
    （8 队是 ``[2,2,1,1]``，不是 ``[3,2,1]``）。

    败者组按**轮次**分层：``该场轮次 = max(双方上一场所在轮次) + 1``，
    刚从胜者组掉下来的队算第 0 轮。这里不能用「已打场次」代替轮次——
    8 队赛制里 L1 分两天打完（各 1 场），两队都打完 L1 时场次都是 1，
    但它们的下一场是 L3 不是 L2，用场次会算出 ``[2,3,1]`` 这种错列。
    """
    ordered = sorted(matches, key=lambda m: (m.start_date, m.start_time, m.id))
    if winners:
        columns = bracket_columns(ordered)
        if columns:
            return columns

    depth: dict[str, int] = {}
    buckets: dict[int, list[Match]] = {}
    for match in ordered:
        round_no = max(depth.get(match.team_a, 0), depth.get(match.team_b, 0)) + 1
        buckets.setdefault(round_no, []).append(match)
        for team in (match.team_a, match.team_b):
            if team:
                depth[team] = round_no
    return [buckets[key] for key in sorted(buckets)]


def double_elim_card(
    fonts: Fonts,
    icons: dict[str, bytes],
    *,
    title: str,
    layout: DoubleElim,
    marks: dict[str, tuple[str, str]],
    subtitle: str = "",
    source: str = "",
    standings: list[KnockoutRow] | None = None,
) -> bytes | None:
    """双败赛段：上胜者组、下败者组、右总决赛，连线是每支队的去向。

    列 = **轮次**，不是日期。日期分列在 4 队 6 场上好看（参考资料就是这么画的），
    到 8 队 14 场、跨 7 个比赛日就崩：同一棵树被切成 7 列，一条「胜决 → 总决赛」
    的线要横穿四列，图上会糊成一团。按轮次排，最长的一条线也只跨两列。

    ``standings`` 给出时右侧加「当前积分 / 最终名次」面板——双败图的核心疑问
    是「谁掉下来过、怎么爬回来的」，旅程线比负场数回答得直接。
    """
    if not fonts.available:
        return None

    every = [*layout.winner, *layout.loser]
    if layout.final:
        every.append(layout.final)
    if not every:
        return None

    win_cols = _depth_rounds(layout.winner, winners=True) if layout.winner else []
    lose_cols = _depth_rounds(layout.loser, winners=False) if layout.loser else []
    if not win_cols and not lose_cols:
        return None

    # 列索引：胜者组第 i 轮在第 i 列，败者组第 k 轮在第 k 列（**右移一格**）。
    # 这个错位不是排版偏好——败者组第一轮要等胜者组第一轮打完才能开始，
    # 两者同列的话，「从胜者组掉下来」的连线就得往回走，线会在目标卡里绕圈。
    win_offset = 0
    lose_offset = 1
    span = max(len(win_cols) + win_offset, len(lose_cols) + lose_offset)
    cols = span + (1 if layout.final else 0)
    panel_extra = KO_PANEL_GAP + KO_PANEL_W if standings else 0.0
    card_w, gap = _deck_metrics(cols, reserve=panel_extra)
    pitch = BRACKET_BOX_H + BRACKET_VGAP

    head = PAD + 76  # 内容顶：给标题、图例和列头留位置
    win_h = _band_height(win_cols, pitch)
    lose_top = head + (win_h + DK_BAND_GAP if win_h else 0)
    lose_h = _band_height(lose_cols, pitch)
    content_bottom = lose_top + lose_h
    height = int(
        max(content_bottom + 52 + PAD, PAD + _knockout_panel_height(len(standings or [])) + PAD)
    )

    body_right = PAD + cols * card_w + (cols - 1) * gap
    canvas = Canvas(fonts, int(body_right + panel_extra + PAD), height)
    right = body_right  # 标题、图例的右锚点是图区右缘，不是画布右缘——面板还在右边

    canvas.text(PAD, PAD, title, 22, bold=True)
    if subtitle:
        canvas.text_right(right, PAD + 6, subtitle, 12, fill=MUTED)

    # 图例：四个结局标记 + 两种线。双败图的线有明确语义，
    # 不写清楚「青色=赢着往上走 / 橙色虚线=输一场掉下去」，读者只能猜
    lx = right
    for label, tone, style in reversed(DK_LEGEND):
        lx -= canvas.measure(label, 9)
        canvas.text(lx, PAD + 34, label, 9, fill=DIM)
        lx -= 14
        if style == "solid":
            canvas.seg(lx - 6, PAD + 39, lx + 6, PAD + 39, fill=tone, width=1.5)
        elif style == "dash":
            canvas.seg(lx - 6, PAD + 39, lx + 6, PAD + 39, fill=tone, width=1.2, dash=3.5)
        else:
            canvas.rrect(lx - 4, PAD + 35, 8, 8, radius=2, fill=tone)
        lx -= 12

    def col_x(index: int) -> float:
        return PAD + index * (card_w + gap)

    def band_span(columns: list[list[Match]], offset: int) -> tuple[int, int]:
        """这一带有比赛的第一列和最后一列（含偏移），空带返回 ``(-1, -1)``。"""
        filled = [i + offset for i, column in enumerate(columns) if column]
        return (filled[0], filled[-1]) if filled else (-1, -1)

    # 两条轨道的底衬与标题：没有这条浅色带，读者看不出「上下一分为二」，
    # 会把败者组的比赛当成胜者组打歪了的一列
    for top, high, tint, edge, label, count, columns, offset in (
        (
            head,
            win_h,
            DK_WIN_TINT,
            DK_WIN_EDGE,
            "胜者组",
            len(layout.winner),
            win_cols,
            win_offset,
        ),
        (
            lose_top,
            lose_h,
            DK_LOSE_TINT,
            DK_LOSE_EDGE,
            "败者组",
            len(layout.loser),
            lose_cols,
            lose_offset,
        ),
    ):
        first_col, tail_col = band_span(columns, offset)
        if high <= 0 or tail_col < 0:
            continue
        x0 = col_x(first_col) - 10
        band_w = col_x(tail_col) + card_w - x0 + 20
        canvas.rrect(x0, top, band_w, high, radius=12, fill=tint)
        canvas.rrect(x0, top, 3, high, radius=1.5, fill=edge)
        canvas.text(x0 + 12, top + 7, f"{label} · {count} 场", 11, fill=edge, bold=True)

    place: dict[str, tuple[float, float]] = {}
    for top, columns, offset, tinted in (
        (head + DK_BAND_TITLE + DK_BAND_PAD, win_cols, win_offset, bool(win_h)),
        (lose_top + DK_BAND_TITLE + DK_BAND_PAD, lose_cols, lose_offset, bool(lose_h)),
    ):
        if not tinted:
            continue
        for index, column in enumerate(columns):
            for order, match in enumerate(column):
                place[match.id] = (col_x(index + offset), top + order * pitch)

    final = layout.final
    if final:
        # 总决赛单独一列、垂直居中于两条轨道之间：它不属于任何一条轨道，
        # 混进带里读者会以为它是胜者组（或败者组）的最后一场
        last = span
        place[final.id] = (
            col_x(last),
            head + (content_bottom - head - BRACKET_BOX_H) / 2,
        )

    # 连线：按「队」路由——某队在本赛段的上一次出场就是它的来路。
    # 按胜负位路由会把「两队同时掉败者组」的线接反。
    ordered = sorted(every, key=lambda m: (m.start_date, m.start_time, m.id))
    drawing: list[tuple[float, float, float, float, bool | None]] = []
    drawn: set[tuple[str, str, int]] = set()
    for index, match in enumerate(ordered):
        for side, team in enumerate((match.team_a, match.team_b)):
            if not team or is_tbd(team) or match.id not in place:
                continue
            for previous in reversed(ordered[:index]):
                sides = (previous.team_a, previous.team_b)
                if team not in sides or previous.id not in place:
                    continue
                key = (previous.id, match.id, sides.index(team))
                if key in drawn:
                    break
                drawn.add(key)
                won = previous.winner == team if previous.finished else None
                drawing.append(
                    (
                        place[previous.id][0] + card_w,
                        _box_row_y(place[previous.id][1], sides.index(team)),
                        place[match.id][0],
                        _box_row_y(place[match.id][1], side),
                        won,
                    )
                )
                break

    # 同一个走廊里的线**必须错轨**：xs 一样的边如果都走中点，会叠成一根
    # 长竖线，读者会以为是同一条线从顶穿到底
    rails: dict[tuple[int, int], list[tuple[float, float, float, float, bool | None]]] = {}
    for edge in drawing:
        rails.setdefault((round(edge[0]), round(edge[2])), []).append(edge)
    for group in rails.values():
        group.sort(key=lambda e: min(e[1], e[3]))
        for slot, (x1, y1, x2, y2, won) in enumerate(group):
            middle = (x1 + x2) / 2 + (slot - (len(group) - 1) / 2) * 6.5
            xm = min(max(middle, x1 + 5), x2 - 5)
            if won:
                tone, dash = SD_CYAN, 0
            elif won is False:
                tone, dash = SD_AMBER, 4.0
            else:  # 上一场还没打完，去向未定，别用败者色——那是谎报
                tone, dash = BRACKET_LINE, 3.0
            canvas.seg(x1, y1, xm, y1, fill=tone, width=1.3, dash=dash)
            canvas.seg(xm, y1, xm, y2, fill=tone, width=1.3, dash=dash)
            canvas.seg(xm, y2, x2, y2, fill=tone, width=1.3, dash=dash)

    for match in every:
        spot = place.get(match.id)
        if not spot:
            continue
        _bracket_box(
            canvas,
            icons,
            match,
            spot[0],
            spot[1],
            card_w,
            marks=marks.get(match.id, ("", "")),
            gold=match is final,
        )

    if standings:
        bo = max((match.best_of for match in every), default=0)
        _knockout_panel(
            canvas,
            icons,
            standings,
            body_right + KO_PANEL_GAP,
            PAD,
            total=len(every),
            double=True,
            bo=bo,
            source=source,
        )

    if source and not standings:
        canvas.text(PAD, height - PAD - 10, f"数据源 {source}", 11, fill=DIM)
    _foot_hint(
        canvas,
        usage_hint(every[0].tournament_id if every else "", every[0].id if every else ""),
        height - PAD - 10,
    )
    return canvas.to_bytes()


# ---------------------------------------------------------------- 瑞士轮

# 一张「轮次跑道」：一列一轮，列内按赛前战绩分档（胜多在上），
# 列间的连线就是每支队的去向——赢了走实线，输了走虚线。
SD_CARD_W = 172
SD_CARD_H = 66
SD_HEAD_H = 18
SD_ROW_H = 24
SD_GAP = 112  # 列间净空，连线走廊
SD_PITCH = 78  # 同档内相邻卡片的间距
SD_BAND_GAP = 34  # 档与档之间
SD_BAND_LABEL = 18  # 档位竖排标签占的宽度
SD_PANEL_W = 292
SD_PANEL_GAP = 44

SD_BLUE = "#24409c"  # 常规胜方
SD_GREEN = "#0e6b44"  # 晋级胜方
SD_DARK = "#12161e"  # 败方
SD_RED_BG = "#3a121a"  # 淘汰败方
SD_RED_TXT = "#ff6e80"
SD_CYAN = "#3bc1ff"  # 胜者续战
SD_AMBER = "#ffb020"  # 败者续战
SD_BAND = "#182030"

SD_DECIDE_TONE = {"adv": GREEN, "out": RED, "both": WIN}

# 图例：(文字, 颜色, 线型)。线型为空表示画个色块。
SD_LEGEND = (
    ("晋级战", GREEN, ""),
    ("淘汰战", RED, ""),
    ("生死战", WIN, ""),
    ("常规轮", DIM, ""),
    ("胜者续战", SD_CYAN, "solid"),
    ("败者续战", SD_AMBER, "dash"),
)


def _column_height(bands: list[list[SwissLane]]) -> float:
    total = sum(len(band) * SD_CARD_H + (len(band) - 1) * (SD_PITCH - SD_CARD_H) for band in bands)
    return total + (len(bands) - 1) * SD_BAND_GAP


def _band_top(bands: list[list[SwissLane]], top: float, body: float) -> float:
    """这一列纵向居中后的起点。列高不同，居中才对得齐中线。"""
    return top + (body - _column_height(bands)) / 2


def _swiss_bands(lanes: list[SwissLane]) -> list[list[SwissLane]]:
    """按赛前战绩分档，胜多在上；同一档内按开赛时间排序。

    档内时间序是读图的硬约定：同一轮同一个战绩档，谁先开打谁在上。
    进来的 lanes 本身就是时间序，这里再显式排一次——分档逻辑不该
    依赖上游恰好有序。
    """
    buckets: dict[tuple[int, int], list[SwissLane]] = {}
    for lane in lanes:
        buckets.setdefault(lane.band, []).append(lane)
    for bucket in buckets.values():
        bucket.sort(key=lambda lane: (lane.match.start_date, lane.match.start_time, lane.match.id))
    return [buckets[key] for key in sorted(buckets, key=lambda b: (-b[0], b[1]))]


def _swiss_layout(
    columns: list[list[SwissLane]], top: float, body: float
) -> tuple[list[list[list[SwissLane]]], list[list[float]]]:
    """排位置：返回「每列的档」和「每张卡的中心 y」。

    列内顺序 = 分档（赛前战绩，胜多在上）→ 档内开赛时间。曾按邻列同队
    的位置做重心排序压连线交叉，但那样档内顺序就跟比赛先后脱节——
    读图的人按「这场几点开打」找场次，时间序比少几条交叉线重要。
    ``centers`` 的下标按档展开序（与 ``_swiss_edges`` 一致）。
    """
    bands = [_swiss_bands(column) for column in columns]
    centers: list[list[float]] = [[0.0] * len(column) for column in columns]
    for ci, column_bands in enumerate(bands):
        y = top + (body - _column_height(column_bands)) / 2
        index = 0
        for band in column_bands:
            for _ in band:
                centers[ci][index] = y + SD_CARD_H / 2
                index += 1
                y += SD_PITCH
            y += SD_BAND_GAP - (SD_PITCH - SD_CARD_H)
    return bands, centers


def _swiss_edges(
    bands: list[list[list[SwissLane]]], centers: list[list[float]]
) -> list[list[tuple[float, float, bool]]]:
    """每条连线的两端 + 这条线是「赢着来的」还是「输着来的」。

    行序跟 ``centers`` 一样按**档展开序**走，不是上游的时间序——档内
    时间序画图后卡片按档铺开，线端点必须对准卡片实际所在的高度，
    否则线会接到别的卡上。线的两端落在**队自己那一行**的中心，不是
    卡片中心：两张卡各有两个队，全连到卡片中点的话，两条线会在入口处
    并成一条，读者分不清哪条是哪个队。
    """
    rows = [[lane for band in column for lane in band] for column in bands]
    corridors: list[list[tuple[float, float, bool]]] = []
    for ci in range(1, len(rows)):
        edges: list[tuple[float, float, bool]] = []
        for ri, lane in enumerate(rows[ci]):
            for side, team in enumerate((lane.match.team_a, lane.match.team_b)):
                for pi, other in enumerate(rows[ci - 1]):
                    sides = (other.match.team_a, other.match.team_b)
                    if team in sides:
                        theirs = sides.index(team)
                        edges.append(
                            (
                                centers[ci - 1][pi] + _row_offset(theirs),
                                centers[ci][ri] + _row_offset(side),
                                other.winner == team,
                            )
                        )
                        break
        corridors.append(edges)
    return corridors


def _row_offset(side: int) -> float:
    """卡片里第 ``side`` 行中心相对卡片中心的偏移。"""
    return SD_HEAD_H + side * SD_ROW_H + SD_ROW_H / 2 - SD_CARD_H / 2


def _rail_slots(edges: list[tuple[float, float, bool]]) -> list[int]:
    """给同一条走廊里的线分配轨道，纵向区间不重叠才共轨。

    长线优先挑轨道：短线贴着卡片走，视觉上不会跟长线缠成一束。
    """
    order = sorted(range(len(edges)), key=lambda k: -abs(edges[k][1] - edges[k][0]))
    rails: list[list[float]] = []
    slots = [0] * len(edges)
    for k in order:
        low, high = sorted((edges[k][0], edges[k][1]))
        for index, (top, bottom) in enumerate(rails):
            if high < top - 5 or low > bottom + 5:
                rails[index] = [min(top, low), max(bottom, high)]
                slots[k] = index
                break
        else:
            rails.append([low, high])
            slots[k] = len(rails) - 1
    return slots


def _swiss_lane_card(
    canvas: Canvas,
    icons: dict[str, bytes],
    lane: SwissLane,
    x: float,
    y: float,
) -> None:
    """一场对局：上面一行赢家、下面一行输家，胜方整行填色。

    行填色比只给小分上色好读——缩到手机上一半大小时色块还在，数字已经糊了。
    左边那条 3px 竖条标记这场的性质（晋级战绿 / 淘汰战红 / 生死战琥珀）。
    """
    match = lane.match
    canvas.rrect(x, y, SD_CARD_W, SD_CARD_H, radius=6, fill=PANEL)

    tone = SD_DECIDE_TONE.get(lane.decide)
    if tone:
        canvas.rrect(x, y + 7, 3, SD_CARD_H - 14, radius=1, fill=tone)

    canvas.text(x + 8, y + 4, match.clock, 8.5, fill=DIM)
    if match.id:
        canvas.text_right(x + SD_CARD_W - 7, y + 4, f"#{match.id}", 8, fill=DIM)

    win_a = match.score_a > match.score_b
    rows = (
        (match.team_a, match.flag_a, match.score_a, win_a),
        (match.team_b, match.flag_b, match.score_b, not win_a),
    )
    for index, (name, flag, score, won) in enumerate(rows):
        top = y + SD_HEAD_H + index * SD_ROW_H
        fill = SD_DARK
        ink = MUTED
        if lane.decided and won:
            fill = SD_GREEN if lane.decide in ("adv", "both") else SD_BLUE
            ink = "#ffffff"
        elif lane.decided and lane.decide in ("out", "both"):
            fill = SD_RED_BG
            ink = SD_RED_TXT
        canvas.rrect(x + 3, top, SD_CARD_W - 6, SD_ROW_H, radius=4, fill=fill)
        pending = is_tbd(name)
        canvas.icon(
            x + 7,
            top + 4,
            15,
            None if pending else icons.get(flag),
            radius=3,
            placeholder="" if pending else name[:2],
            size_hint=7,
            bg=PANEL_2,
        )
        canvas.text(
            x + 26,
            top + 4,
            canvas.fit("待定" if pending else name, 11, SD_CARD_W - 60, bold=won),
            11,
            fill=ink,
            bold=bool(won and lane.decided),
        )
        canvas.text_right(
            x + SD_CARD_W - 8,
            top + 3,
            str(score) if lane.decided else "–",
            11.5,
            fill=ink,
            bold=bool(won and lane.decided),
        )


def _standings_panel(
    canvas: Canvas,
    icons: dict[str, bytes],
    standings: list[tuple[int, SwissRow]],
    x: float,
    y: float,
    source: str,
) -> float:
    """右侧积分榜：名次、大场、小局、状态，晋级线画在出线与出局之间。"""
    rows = len(standings)
    height = 84 + rows * 26 + 24
    canvas.rrect(x, y, SD_PANEL_W, height, radius=12, fill="#0e131c")

    qualified = sum(1 for _, row in standings if row.state == "qualified")
    eliminated = sum(1 for _, row in standings if row.state == "eliminated")
    played = sum(row.wins + row.losses for _, row in standings) // 2
    cells = (
        ("晋级", str(qualified), GREEN),
        ("淘汰", str(eliminated), RED),
        ("场次", str(played), MUTED),
    )
    cell_w = (SD_PANEL_W - 24) / 3
    for index, (label, value, tone) in enumerate(cells):
        cx = x + 12 + index * cell_w
        canvas.text(cx, y + 12, value, 18, fill=tone, bold=True)
        canvas.text(cx + 1, y + 36, label, 9.5, fill=DIM)

    canvas.line(x + 12, y + 52, x + SD_PANEL_W - 12, y + 52)

    for label, cx in (
        ("排名", x + 12),
        ("战队", x + 44),
        ("大场", x + 196),
        ("小局", x + 244),
        ("状态", x + SD_PANEL_W - 14),
    ):
        anchor = "lt" if cx < x + 100 else "rt"
        canvas.text(cx, y + 62, label, 9, fill=DIM, anchor=anchor)

    top = y + 84
    for index, (rank, row) in enumerate(standings):
        row_y = top + index * 26
        if index:
            canvas.line(x + 12, row_y, x + SD_PANEL_W - 12, row_y, fill="#1a2231")

        # 晋级线：出线区与出局区之间划一刀，比让读者自己数「第 8 名」直观
        if index == qualified and 0 < qualified < rows:
            canvas.line(x + 12, row_y, x + SD_PANEL_W - 14, row_y, fill=RED)
            canvas.text(x + 12, row_y - 10, "晋级线", 8, fill=RED)

        tone = WIN if row.state == "qualified" else (RED if row.state == "eliminated" else MUTED)
        canvas.rrect(x + 8, row_y + 6, 2.5, 14, radius=1, fill=tone)
        canvas.text(x + 14, row_y + 5, str(rank), 10, fill=DIM, bold=True)
        canvas.icon(
            x + 36,
            row_y + 5,
            16,
            icons.get(row.flag) if row.flag else None,
            radius=3,
            placeholder=row.team[:2],
            size_hint=7,
            bg=PANEL_2,
        )
        canvas.text(
            x + 57,
            row_y + 5,
            canvas.fit(row.team, 10.5, 86, bold=True),
            10.5,
            fill=MUTED if row.state == "eliminated" else INK,
            bold=True,
        )
        canvas.text_right(
            x + 196,
            row_y + 5,
            row.record,
            10.5,
            fill=INK if row.state != "eliminated" else MUTED,
            bold=True,
        )
        canvas.text_right(x + 244, row_y + 5, row.games, 9.5, fill=MUTED)
        state_text = {"qualified": "晋级", "eliminated": "淘汰"}.get(row.state, "在争")
        canvas.text_right(x + SD_PANEL_W - 14, row_y + 5, state_text, 9, fill=tone)

    if source:
        canvas.text(x + 14, y + height - 18, f"数据源 {source}", 8.5, fill=DIM)
    return height


def swiss_diagram_card(
    fonts: Fonts,
    icons: dict[str, bytes],
    *,
    title: str,
    columns: list[list[SwissLane]],
    standings: list[tuple[int, SwissRow]],
    subtitle: str = "",
    source: str = "",
) -> bytes | None:
    """瑞士轮晋级图：一轮一列，列内按赛前战绩分档，右边榜单给最终去向。

    为什么不是树：33 场按 1/2/4 反推是 ``[18,8,4,2,1]``，首列 18 场之间没有
    「谁淘汰谁」的关系，硬画等于把 18 场编成八强。瑞士轮的真实结构是
    「先到 3 胜出线、先到 3 负出局」，所以它是一条跑道——每支队顺着连线走到底，
    走成绿的晋级、走成红的出局。
    """
    if not fonts.available or not columns or not standings:
        return None

    rounds = len(columns)
    body_w = rounds * SD_CARD_W + (rounds - 1) * SD_GAP
    width = int(PAD + SD_BAND_LABEL + body_w + SD_PANEL_GAP + SD_PANEL_W + PAD)
    body = max(_column_height(_swiss_bands(column)) for column in columns)

    top = 126.0
    height = int(top + body + 56)
    canvas = Canvas(fonts, width, height)

    canvas.text(PAD, PAD, title, 20, bold=True)
    if subtitle:
        canvas.text(PAD, PAD + 30, subtitle, 11, fill=MUTED)

    # 图例靠右：色块标性质，线型标「赢着来还是输着来」
    lx = width - PAD
    for label, tone, style in reversed(SD_LEGEND):
        lx -= canvas.measure(label, 9)
        canvas.text(lx, PAD + 6, label, 9, fill=DIM)
        lx -= 14
        if style == "solid":
            canvas.seg(lx - 6, PAD + 11, lx + 6, PAD + 11, fill=tone, width=1.5)
        elif style == "dash":
            canvas.seg(lx - 6, PAD + 11, lx + 6, PAD + 11, fill=tone, width=1.2, dash=3.5)
        else:
            canvas.rrect(lx - 4, PAD + 7, 8, 8, radius=2, fill=tone)
        lx -= 12

    canvas.line(PAD, 84, width - PAD, 84)

    bands, centers = _swiss_layout(columns, top, body)
    dates = swiss_round_dates(columns)
    xs = [PAD + SD_BAND_LABEL + ci * (SD_CARD_W + SD_GAP) for ci in range(rounds)]

    # 三遍画，顺序即层级：档底 → 连线 → 卡片。
    # 线画在档底之上，才会一路顶到卡片左缘；画在档底之下就会在档边断掉，
    # 读者看不出这条线到底是进这场，还是停在半空。
    for ci, band_list in enumerate(bands):
        x = xs[ci]
        canvas.text(x, top - 34, f"第 {ci + 1} 轮", 11, fill=WIN, bold=True)
        canvas.text(x, top - 19, dates[ci], 9, fill=DIM)
        y = _band_top(bands[ci], top, body)
        for band in band_list:
            band_h = _column_height([band])
            canvas.rrect(
                x - SD_BAND_LABEL,
                y - 6,
                SD_CARD_W + SD_BAND_LABEL + 5,
                band_h + 12,
                radius=8,
                fill=SD_BAND,
            )
            wins, losses = band[0].wins, band[0].losses
            tone = GREEN if wins > losses else (RED if wins < losses else BLUE)
            canvas.rrect(x - SD_BAND_LABEL, y - 6, 2.5, band_h + 12, radius=1, fill=tone)
            canvas.vtext(
                x - SD_BAND_LABEL / 2 - 1,
                y + band_h / 2,
                f"{wins} 胜 {losses} 负",
                8,
                fill=tone,
            )
            y += band_h + SD_BAND_GAP

    for ci, edges in enumerate(_swiss_edges(bands, centers), 1):
        slots = _rail_slots(edges)
        left = xs[ci - 1] + SD_CARD_W
        # 线停在档条边上而不是卡片边上：档条背后是同一档的比赛，
        # 停在档口读起来是「进这一组」，穿过档条则会压住竖排标签
        right = xs[ci] - SD_BAND_LABEL + 3
        span = max(24.0, right - left - 12)
        for k, (y0, y1, won) in enumerate(edges):
            rail = min(slots[k], 9)
            xr = left + 9 + rail * span / 9
            tone = SD_CYAN if won else SD_AMBER
            dash = 0 if won else 4.0
            canvas.seg(left, y0, xr, y0, fill=tone, width=1.4, dash=dash)
            canvas.seg(xr, y0, xr, y1, fill=tone, width=1.4, dash=dash)
            canvas.seg(xr, y1, right, y1, fill=tone, width=1.4, dash=dash)

    for ci in range(rounds):
        x = xs[ci]
        y = _band_top(bands[ci], top, body)
        for band in bands[ci]:
            for index, lane in enumerate(band):
                _swiss_lane_card(canvas, icons, lane, x, y + index * SD_PITCH)
            y += _column_height([band]) + SD_BAND_GAP

    _standings_panel(canvas, icons, standings, width - PAD - SD_PANEL_W, top - 34, source)
    first = columns[0][0].match if columns and columns[0] else None
    _foot_hint(
        canvas,
        usage_hint(first.tournament_id if first else "", first.id if first else ""),
        height - PAD - 12,
    )
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
    _foot_hint(canvas, usage_hint(match.tournament_id, match.id), height - PAD - 12)
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
    everyone = [p for _, squad, _ in squads for p in squad]
    peak_damage = max((p.damage_pct for p in everyone), default=0.0)
    peak_taken = max((p.damage_taken for p in everyone), default=0)
    for label, squad, side in squads:
        if not squad:
            continue
        canvas.rrect(
            PAD, y, WIDTH - PAD * 2, TEAM_HEAD - 4, radius=6, fill=PANEL_2 if side == "a" else PANEL
        )
        flag = game.flag_a if side == "a" else game.flag_b
        canvas.icon(PAD + 8, y + 3, 20, icons.get(flag or ""), radius=4)
        canvas.text(PAD + 36, y + 4, label or "?", 14, bold=True, fill=BLUE if side == "a" else RED)
        if game.win_side:
            # 胜负贴着队名，别丢到右边数字堆里——一眼要先知道这行是哪边的
            won = side == game.win_side
            chip_x = PAD + 44 + canvas.measure(label or "?", 14, bold=True)
            canvas.rrect(chip_x, y + 6, 22, 15, radius=7, fill=WIN if won else PANEL)
            canvas.text(
                chip_x + 11,
                y + 7,
                "胜" if won else "负",
                10,
                fill=BG if won else DIM,
                anchor="mt",
            )
        canvas.text_right(
            WIDTH - PAD - 12,
            y + 6,
            f"击杀 {sum(p.kills for p in squad)}　经济 {_gold(sum(p.gold for p in squad))}"
            f"　伤害 {_gold(sum(p.damage for p in squad))}",
            10,
            fill=MUTED,
        )
        y += TEAM_HEAD
        y = _player_table(
            canvas,
            fonts,
            icons,
            squad,
            y,
            peak_damage=peak_damage,
            peak_taken=peak_taken,
        )
        y += 12

    if source:
        canvas.text(PAD, height - PAD - 12, f"数据源 {source}", 11, fill=DIM)
    _foot_hint(canvas, usage_hint(match.tournament_id, match.id), height - PAD - 12)
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


def _bar(canvas: Canvas, x: float, y: float, w: float, ratio: float, color: str) -> None:
    """一根细比例条：底轨常驻，填充按 ``ratio``（0~1）。

    只给「伤害%」「承伤%」两列配条，别的列不配——一条 3px 的条在 880 宽的卡上
    只够表达一个量级，列多了就变成一排噪声，反而看不出谁是核心。
    """
    canvas.rrect(x, y, w, 3, radius=1.5, fill=PANEL_2)
    if ratio > 0:
        canvas.rrect(x, y, max(1.5, w * min(1.0, ratio)), 3, radius=1.5, fill=color)


def _player_table(
    canvas: Canvas,
    fonts: Fonts,
    icons: dict[str, bytes],
    squad: list[PlayerStat],
    y: float,
    *,
    peak_damage: float = 0.0,
    peak_taken: float = 0.0,
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
        if peak_damage > 0:
            _bar(canvas, col_dmg, y + 20, 52, player.damage_pct / peak_damage, WIN)
        canvas.text(col_part, y + 4, f"{player.part:.0f}%" if player.part else "—", 12, fill=MUTED)
        canvas.text(col_taken, y + 4, _gold(player.damage_taken), 12, fill=MUTED)
        # 承伤条按**绝对值**归一，不按 damage_taken_pct：那一列显示的是 23.8k 这种
        # 绝对值，底下却按占比画条，两套口径会让读者对不上号。
        if peak_taken > 0:
            _bar(canvas, col_taken, y + 20, 48, player.damage_taken / peak_taken, GREEN)
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
        show_id: bool = False,
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
                show_id=show_id,
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

    async def bracket(
        self,
        *,
        title: str,
        columns: list[list[Match]],
        marks: dict[str, tuple[str, str]] | None = None,
        subtitle: str = "",
        source: str = "",
        standings: list[KnockoutRow] | None = None,
    ) -> bytes | None:
        """赛段晋级图。"""
        if not self.available:
            self._fail()
            return None
        try:
            flat = [match for col in columns for match in col]
            icons = await self.store.warm(_collect_urls(flat, []))
            return bracket_card(
                self.fonts,
                icons,
                title=title,
                columns=columns,
                marks=marks or {},
                subtitle=subtitle,
                source=source,
                standings=standings,
            )
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
            return None

    async def double_elim(
        self,
        *,
        title: str,
        layout: DoubleElim,
        marks: dict[str, tuple[str, str]],
        subtitle: str = "",
        source: str = "",
        standings: list[KnockoutRow] | None = None,
    ) -> bytes | None:
        """双败淘汰图：上胜者组、下败者组、右总决赛。"""
        if not self.available:
            self._fail()
            return None
        try:
            flat = [*layout.winner, *layout.loser]
            if layout.final:
                flat.append(layout.final)
            icons = await self.store.warm(_collect_urls(flat, []))
            return double_elim_card(
                self.fonts,
                icons,
                title=title,
                layout=layout,
                marks=marks,
                subtitle=subtitle,
                source=source,
                standings=standings,
            )
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
            return None

    async def swiss(
        self,
        *,
        title: str,
        columns: list[list[SwissLane]],
        standings: list[tuple[int, SwissRow]],
        subtitle: str = "",
        source: str = "",
    ) -> bytes | None:
        """瑞士轮晋级图。画不成树的赛段里有相当一部分其实是瑞士轮。"""
        if not self.available:
            self._fail()
            return None
        try:
            flags = [lane.match.flag_a for column in columns for lane in column]
            flags += [lane.match.flag_b for column in columns for lane in column]
            flags += [row.flag for _, row in standings if row.flag]
            icons = await self.store.warm([flag for flag in flags if flag])
            return swiss_diagram_card(
                self.fonts,
                icons,
                title=title,
                columns=columns,
                standings=standings,
                subtitle=subtitle,
                source=source,
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
