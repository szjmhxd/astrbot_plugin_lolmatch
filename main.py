"""LOL 赛事助手 —— 查赛程赛果，订阅后按时段推送。

设计取舍
--------
**图 + 文两种形态**：赛程、比赛摘要、单局详情都有卡片（Pillow 画），
文字版一直保留。取不到字体或画图失败就自动退回纯文字，功能不缩水——
群里看不到图和收到一片报错，显然是前者更能接受。

**数据源可换**：scoregg（玩加赛事）默认，PandaScore 兜底。上层只认
``lol.models`` 里的结构，换源不改指令。

**提醒自己负责**：@ 谁、什么时段可以打扰、每晚几点推，全部沉到会话级设置里，
因为同一个机器人可能同时服务「半夜也要看比赛的宿舍群」和「只准上班时间说话的
工作群」，这两类需求没法用一份全局配置满足。

开发约定
--------
所有 ``@filter.*`` 必须写在插件类里（AstrBot 按 ``handler_module_path`` 精确匹配
归属 handler，写在其它模块会被静默丢弃）。
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .lol import render
from .lol.card import CardRenderer
from .lol.champions import ChampionBook
from .lol.models import (
    FORMAT_DOUBLE,
    FORMAT_SINGLE,
    ProviderError,
    bracket_columns,
    day_label,
    detect_format,
    double_elim_split,
    match_marks,
    paginate_by_day,
    stage_groups,
    stage_has_result,
    swiss_lanes,
    swiss_ready,
    swiss_rows,
    swiss_standings,
)
from .lol.notify import Notifier, mention_text, normalize_mention
from .lol.provider import DataHub, normalize_provider
from .lol.store import SessionStore
from .lol.windows import (
    WindowError,
    in_windows,
    next_open_time,
    parse_clock,
    parse_window,
    parse_windows_text,
    window_text,
)
from .lol.workday import WorkdayCalendar, parse_month_day

PLUGIN_NAME = "astrbot_plugin_lolmatch"


# 指令的返回值：文字 / (配图文案, 图) / (配图文案, 图, 图发不出去时改发的全文)
_Result = str | tuple[str, list[bytes]] | tuple[str, list[bytes], str]

_CMD = "/lol"
_AT_PREFIX = ("@", "＠")
_TICK_SECONDS = 30

# 改整个会话行为的子指令（需要管理员）。只读的「提醒 / 状态」不在此列。
_ADMIN_SUBS = {"订阅", "取消订阅", "sub", "unsubscribe", "刷新英雄"}


class LolMatchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self._config = config
        self._store: SessionStore | None = None
        self._workday: WorkdayCalendar | None = None
        self._champions: ChampionBook | None = None
        self._hub: DataHub | None = None
        self._notifier = Notifier(context)
        self._cards: CardRenderer | None = None
        self._task: asyncio.Task | None = None
        self._warmup: asyncio.Task | None = None

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        data_dir = self._data_dir()
        self._store = SessionStore(data_dir / "sessions.json")
        await self._store.load()

        self._workday = WorkdayCalendar(
            data_dir / "holidays.json",
            fetch_date=parse_month_day(self._cfg_str("holiday_fetch_date", "11-01"), (11, 1)),
            timeout=float(self._cfg_int("request_timeout", 12)),
        )
        await self._workday.load()

        self._champions = ChampionBook(
            data_dir / "champions.json",
            timeout=float(self._cfg_int("request_timeout", 12)),
        )
        await self._champions.load()

        self._hub = DataHub(
            default_provider=self._cfg_str("default_provider", "scoregg"),
            default_token=self._cfg_str("pandascore_token", ""),
            timeout=float(self._cfg_int("request_timeout", 12)),
            book=self._champions,
        )

        self._cards = CardRenderer(
            data_dir / "icons",
            enabled=self._cfg_bool("image_cards", True),
        )
        if not self._cards.available:
            logger.warning("[LOL赛事] 图片卡片不可用（缺 Pillow 或中文字体），全部退回纯文字")

        self._task = asyncio.create_task(self._push_loop())
        self._warmup = asyncio.create_task(self._warm_up())
        logger.info("[LOL赛事] 插件已启动")

    async def terminate(self) -> None:
        for task in (self._task, self._warmup):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[LOL赛事] 停止后台任务时忽略异常：{exc}")
        if self._hub is not None:
            await self._hub.close()
        if self._cards is not None:
            await self._cards.close()
        logger.info("[LOL赛事] 插件已停止")

    async def _warm_up(self) -> None:
        """启动后补一次日历与英雄表，不阻塞插件加载。"""
        try:
            if self._workday is not None:
                await self._workday.maintain()
            if self._champions is not None and not self._champions.ready:
                await self._champions.refresh()
            await self._seed_champions()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[LOL赛事] 启动预热失败：{type(exc).__name__}: {exc}")

    async def _seed_champions(self) -> None:
        """首次安装时抓两场已结束的比赛，把英雄编号映射垫一批出来。

        新英雄的图标路径是纯数字，源站没给英文名。不垫的话第一屏详情里会有
        一半英雄显示「—」。这里只花 3 个请求，之后就靠平时的查询慢慢补全。
        """
        if self._champions is None or self._hub is None:
            return
        if self._champions.learned_count >= 60:
            return

        yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            _, matches = await self._hub.call("schedule", provider="scoregg", day=yesterday)
        except ProviderError as exc:
            logger.debug(f"[LOL赛事] 英雄表预热跳过：{exc}")
            return

        finished = [m for m in matches if m.finished][:2]
        for match in finished:
            try:
                await self._hub.call("detail", provider="scoregg", match_id=match.id)
            except ProviderError:
                continue
        await self._champions.save()
        learned = self._champions.learned_count
        if learned:
            logger.info(f"[LOL赛事] 英雄编号映射已垫 {learned} 条")

    # ------------------------------------------------------------------ 配置

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self._config.get(key) if hasattr(self._config, "get") else None
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    def _cfg_str(self, key: str, default: str = "") -> str:
        return str(self._cfg(key, default) or default)

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "on")

    def _cfg_list(self, key: str) -> list[str]:
        value = self._cfg(key, [])
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    def _data_dir(self) -> Path:
        try:
            return StarTools.get_data_dir(PLUGIN_NAME)
        except Exception as exc:  # noqa: BLE001
            fallback = Path("data") / "plugin_data" / PLUGIN_NAME
            fallback.mkdir(parents=True, exist_ok=True)
            logger.warning(f"[LOL赛事] 数据目录回退到 {fallback}（{exc}）")
            return fallback

    # ------------------------------------------------------------------ 会话工具

    @staticmethod
    def _umo(event: AstrMessageEvent) -> str:
        for attr in ("unified_msg_origin", "session_id"):
            value = getattr(event, attr, None)
            if isinstance(value, str) and value:
                return value
        return ""

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        for attr in ("get_sender_id", "get_user_id"):
            getter = getattr(event, attr, None)
            if callable(getter):
                try:
                    value = getter()
                except Exception as exc:  # noqa: BLE001 适配器实现不一，取不到就换下一个
                    logger.debug(f"[LOL赛事] 取发送者 ID 失败：{type(exc).__name__}: {exc}")
                    continue
                if value:
                    return str(value)
        sender = getattr(getattr(event, "message_obj", None), "sender", None)
        return str(getattr(sender, "user_id", "") or "")

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        admins = self._cfg_list("admin_ids")
        if not admins:
            return True
        return self._sender_id(event) in admins

    @staticmethod
    def _extract_ats(event: AstrMessageEvent) -> list[str]:
        """从消息链里抠出被 @ 的用户 ID。"""
        found: list[str] = []
        message = getattr(getattr(event, "message_obj", None), "message", None) or []
        for comp in message:
            if type(comp).__name__ != "At" and getattr(comp, "type", "") != "at":
                continue
            qq = str(getattr(comp, "qq", "") or "")
            if qq and qq != "all" and qq not in found:
                found.append(qq)
        return found

    @staticmethod
    def _strip_command(message_str: str, command: str) -> list[str]:
        text = (message_str or "").replace("\u3000", " ").strip()
        text = re.sub(r"^(?:@\S+\s+)+", "", text)
        text = re.sub(
            rf"^[~/!#！]{{0,2}}\s*{re.escape(command)}(?=\s|$)\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        return text.split() if text else []

    # ------------------------------------------------------------------ 会话配置合并

    def _session(self, umo: str) -> dict[str, Any]:
        return self._store.get(umo) if self._store else {}

    def _effective(self, umo: str) -> dict[str, Any]:
        """把会话设置和全局默认合起来，得到实际生效的一份配置。"""
        conf = self._session(umo)
        provider = normalize_provider(
            conf.get("provider"), self._cfg_str("default_provider", "scoregg")
        )
        token = str(conf.get("token") or "") or self._cfg_str("pandascore_token", "")
        mention = normalize_mention(conf.get("mention"), "none")
        if not conf.get("mention"):
            mention = normalize_mention(self._cfg_str("default_mention", "none"), "none")
        targets = conf.get("targets") if isinstance(conf.get("targets"), list) else None
        if not targets:
            targets = self._cfg_list("default_mention_targets")
        if "windows" in conf and isinstance(conf["windows"], list):
            windows = conf["windows"]
        else:
            windows = parse_windows_text(self._cfg_str("default_windows", ""))
        push_time = str(conf.get("push_time") or "") or self._cfg_str("push_time", "23:00")
        return {
            "provider": provider,
            "token": token,
            "mention": mention,
            "targets": [str(item) for item in targets],
            "windows": windows,
            "push_time": push_time,
            "subs": [str(item) for item in (conf.get("subs") or [])],
        }

    # ------------------------------------------------------------------ 指令入口

    @filter.command("lol")
    async def cmd_lol(self, event: AstrMessageEvent):
        """LOL 赛事助手"""
        if not self._cfg_bool("enabled", True):
            yield event.plain_result("LOL 赛事助手已关闭。")
            return
        if self._store is None or self._hub is None:
            yield event.plain_result("插件还在初始化，请稍后再试。")
            return

        umo = self._umo(event)
        args = self._strip_command(event.message_str, "lol")
        sub = args[0].lower() if args else ""
        rest = args[1:]

        if sub in _ADMIN_SUBS and not self._is_admin(event):
            yield event.plain_result("这个操作需要管理员权限。")
            return

        try:
            result = await self._dispatch(event, umo, sub, rest)
        except ProviderError as exc:
            logger.warning(f"[LOL赛事] 数据源失败：{exc}")
            result = (
                f"⚠️ 数据源暂时不可用：\n{exc}\n\n稍后再试，或用「{_CMD} 设置 源 pandascore」换源。"
            )
        except Exception as exc:  # noqa: BLE001 指令级兜底，别让一条指令把插件带崩
            logger.error(f"[LOL赛事] 指令 {sub} 处理失败：{type(exc).__name__}: {exc}")
            result = f"⚠️ 处理出错了：{type(exc).__name__}: {exc}"

        # 第三条是「图发不出去时改发的全文」：配图文案只有一行概括，
        # 平台一旦吞图，读者就只剩这一行了。带图发成功就别再补，否则群里两份内容。
        if isinstance(result, tuple):
            text, images = result[0], result[1]
            fallback = result[2] if len(result) > 2 else text
        else:
            text, images, fallback = result, [], result
        if images and await self._notifier.send(umo, text, images=images):
            return
        if fallback:
            yield event.plain_result(fallback)

    async def _dispatch(
        self, event: AstrMessageEvent, umo: str, sub: str, rest: list[str]
    ) -> _Result:
        if sub in ("", "帮助", "help", "?", "？", "菜单"):
            return render.help_text(_CMD)
        if sub in ("今日", "今天", "today"):
            return await self._cmd_day(umo, date.today())
        if sub in ("本周", "周", "week"):
            return await self._cmd_week(umo)
        if sub in ("联赛", "赛事", "leagues"):
            return await self._cmd_tournaments(umo)
        if sub in ("联赛详情", "赛事详情", "tournament"):
            return await self._cmd_tournament(umo, rest)
        if sub in ("详情", "比赛", "detail"):
            return await self._cmd_detail(umo, rest)
        if sub in ("订阅列表", "订阅们", "sublist"):
            return self._cmd_sub_list(umo)
        if sub in ("订阅", "sub"):
            return await self._cmd_subscribe(umo, rest)
        if sub in ("取消订阅", "unsub", "unsubscribe"):
            return await self._cmd_unsubscribe(umo, rest)
        if sub in ("提醒", "通知", "mention"):
            return await self._cmd_remind(umo, rest, event)
        if sub in ("设置", "config"):
            return await self._cmd_config(umo, rest, event)
        if sub in ("状态", "status", "info"):
            return self._cmd_status(umo)
        if sub in ("测试推送", "测试", "test"):
            return await self._cmd_test_push(umo, event)
        if sub in ("刷新英雄", "refresh"):
            assert self._champions is not None
            ok = await self._champions.refresh()
            return "英雄中文名已更新。" if ok else "英雄表拉取失败，稍后再试（不影响查询）。"
        return f"不认识的子指令「{sub}」。发 {_CMD} 帮助 看用法。"

    # ------------------------------------------------------------------ 查询

    async def _cmd_day(self, umo: str, day: date) -> str | tuple[str, list[bytes]]:
        conf = self._effective(umo)
        day_str = day.strftime("%Y-%m-%d")
        assert self._hub is not None
        used, matches = await self._hub.call(
            "today", provider=conf["provider"], token=conf["token"], today=day_str
        )
        limit = self._cfg_int("max_matches_per_day", 20)
        text = render.day_report(
            day_str,
            matches,
            title=f"{day_str[5:]} 赛程",
            limit=limit,
            source=self._label(used),
        )
        label = self._label(used)
        images = await self._schedule_cards(f"{day_str[5:]} 赛程", matches, limit, label)
        if images:
            # 有图就不用再把整张列表念一遍，一句话说明场次就够
            if matches:
                brief = f"📅 {day_str[5:]}　共 {len(matches)} 场"
            else:
                brief = f"📭 {day_str[5:]} 没有比赛"
            return brief + f"　数据源 {label}", images
        return text

    async def _cmd_week(self, umo: str) -> str | tuple[str, list[bytes]]:
        conf = self._effective(umo)
        assert self._hub is not None
        used, matches = await self._hub.call("week", provider=conf["provider"], token=conf["token"])
        limit = self._cfg_int("max_matches_per_day", 20)
        text = render.week_report(
            matches,
            days=max(1, self._cfg_int("week_match_days", 7)),
            limit=limit,
            source=self._label(used),
        )
        label = self._label(used)
        images = await self._schedule_cards("LOL 近期赛程", matches, limit, label)
        if images:
            return f"🗓 LOL 近期赛程　共 {len(matches)} 场　数据源 {label}", images
        return text

    async def _schedule_cards(self, title: str, matches, limit: int, source: str) -> list[bytes]:
        if self._cards is None or not self._cards.available or not matches:
            return []
        subtitle = f"只列前 {limit} 场" if limit > 0 and len(matches) > limit else ""
        png = await self._cards.schedule(
            title=title, matches=matches, limit=limit, source=source, subtitle=subtitle
        )
        return [png] if png else []

    async def _cmd_detail(self, umo: str, rest: list[str]) -> str | tuple[str, list[bytes]]:
        token = rest[0] if rest else ""
        if not token.isdigit():
            return f"用法：{_CMD} 详情 <比赛ID> [局号]（比赛ID 可以先查「{_CMD} 今日」拿到）"
        conf = self._effective(umo)
        assert self._hub is not None
        # 第一次查详情时英雄表可能还没拉下来，这里补一次（之后都走缓存）
        if self._champions is not None and not self._champions.ready:
            await self._champions.refresh()
        used, detail = await self._hub.call(
            "detail", provider=conf["provider"], token=conf["token"], match_id=token
        )
        champ = self._champions.resolve if self._champions else (lambda _hid, tok: tok or "—")
        # 第二个参数看单局：BO5 全列出来太长，可以只查其中一局
        only = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 0
        label = self._label(used)
        text = render.detail_report(detail, champ=champ, source=label, only=only, full=True)

        images = await self._detail_cards(detail, only=only, source=label)
        return (text, images) if images else text

    async def _detail_cards(self, detail, *, only: int, source: str) -> list[bytes]:
        """给详情配图：指定局就画那一局，否则画最后一局（决胜局信息最全）。"""
        if self._cards is None or not self._cards.available:
            return []
        if not detail.games:
            png = await self._cards.match(detail, source=source)
            return [png] if png else []
        games = [g for g in detail.games if g.index == only] if only else detail.games[-1:]
        if not games:
            return []
        png = await self._cards.game(detail, games[0], source=source)
        return [png] if png else []

    async def _cmd_tournaments(self, umo: str) -> str:
        conf = self._effective(umo)
        assert self._hub is not None
        used, items = await self._hub.call(
            "upcoming_tournaments",
            provider=conf["provider"],
            token=conf["token"],
            limit=18,
        )
        return render.tournament_list(items, source=self._label(used))

    async def _cmd_tournament(self, umo: str, rest: list[str]) -> _Result:
        token = rest[0] if rest else ""
        if not token.isdigit():
            return f"用法：{_CMD} 联赛详情 <联赛ID>（先用「{_CMD} 联赛」查 ID）"
        conf = self._effective(umo)
        assert self._hub is not None
        used, matches = await self._hub.call(
            "schedule",
            provider=conf["provider"],
            token=conf["token"],
            tournament_id=token,
        )
        name = (matches[0].tournament_name if matches else "") or f"联赛 {token}"
        label = self._label(used)
        if not matches:
            return f"🏆 {name}\n\n这个联赛还没有排期数据。　数据源 {label}"

        # 纯文字兜底也要全给：40 场封顶会同样吞掉末尾几天，和图一个毛病
        text = render.tournament_report(name, matches, source=label, limit=0)
        images = await self._tournament_cards(name, matches, label)
        if images:
            brief = self._tournament_brief(name, matches, label)
            if len(images) > 1:
                # 多张图在多数客户端展开成一排缩略图，先说一句省得读者以为只有一张
                brief += f"　分 {len(images)} 张"
            # 第三项是图发不出去时的全文：只有一行概括等于什么都没给
            return brief, images, text
        return text

    def _tournament_brief(self, name: str, matches, source: str) -> str:
        """配图时只留一句概括——编号和日期都在图上，不再重复念一遍列表。"""
        days = sorted({m.start_date for m in matches if m.start_date})
        if len(days) > 1:
            span = f"{days[0][5:]} ~ {days[-1][5:]}"
        else:
            span = day_label(days[0]) if days else ""
        stages = stage_groups(matches)
        brief = f"🏆 {name}　{span}　共 {len(matches)} 场"
        if len(stages) > 1:
            brief += f" · {'／'.join(label for label, _ in stages)}"
        pending = self._pending_stages(matches)
        if pending:
            brief += f"（{'、'.join(pending)} 尚无赛果，未出图）"
        return f"{brief}　数据源 {source}"

    async def _tournament_cards(self, name: str, matches, source: str) -> list[bytes]:
        """联赛详情图：**一个赛段一张**，赛程和晋级图在同一张里。

        每个赛段只出一张图，画法由**赛制**决定（`detect_format` 从数据判，不看赛事名）：
        单败画树、双败画「上胜者组 / 下败者组 / 右总决赛」、瑞士轮画轮次跑道；
        三种都不成立才退回清单。判错的代价比画丑严重——双败当单败画，
        败者组那几场会被塞进某一列，看不出谁是从胜者组掉下来的。

        刚开赛的赛段整段跳过：今天才第一场的话，画出来全是「待定」，
        没有结果可看，不如不出。
        """
        if self._cards is None or not self._cards.available:
            return []
        out: list[bytes] = []
        limit = self._cfg_int("tournament_card_limit", 36)
        for stage, items in stage_groups(matches):
            if not stage_has_result(items):
                continue
            out.extend(await self._stage_cards(name, stage, items, source, limit))
        return out

    def _pending_stages(self, matches) -> list[str]:
        """一个结果都没有的赛段，配文里点一句，免得读者以为漏画了。"""
        notes: list[str] = []
        for stage, items in stage_groups(matches):
            if stage_has_result(items) or not items:
                continue
            days = sorted({m.start_date for m in items if m.start_date})
            when = f" {days[0][5:]} 起" if days else ""
            notes.append(f"{stage}{when}")
        return notes

    async def _stage_cards(
        self, name: str, stage: str, items, source: str, limit: int
    ) -> list[bytes]:
        """单个赛段的一张图：单败树 / 双败双轨 / 瑞士轮跑道 → 清单，逐级降级。"""
        assert self._cards is not None
        days = sorted({m.start_date for m in items if m.start_date})
        span = day_label(days[0]) if len(days) == 1 else f"{days[0][5:]} ~ {days[-1][5:]}"
        title = f"{name} · {stage}"
        fmt = detect_format(items)
        marks = match_marks(items, fmt) if fmt else {}

        # 双败：两条轨道 + 总决赛。**必须先于单败判断**——双败的场次形状
        # 也是「1、2、4 递减」，当成单败画会变成一棵歪树，败者组那几场
        # 被塞在某一列里，看不出谁是从胜者组掉下来的
        if fmt == FORMAT_DOUBLE:
            layout = double_elim_split(items)
            if layout.ready:
                png = await self._cards.double_elim(
                    title=title,
                    layout=layout,
                    marks=marks,
                    subtitle=(
                        f"{len(items)} 场 · 双败淘汰｜赢着往上走，输一场掉进败者组"
                        f"（橙色虚线），两败出局　{span}"
                    ),
                    source=source,
                )
                if png:
                    return [png]

        if fmt == FORMAT_SINGLE and bracket_columns(items):
            columns = bracket_columns(items) or []
            png = await self._cards.bracket(
                title=title,
                columns=columns,
                marks=marks,
                subtitle=f"{len(items)} 场 · 单败淘汰｜一败出局　{span}",
                source=source,
            )
            if png:
                return [png]

        # 树摆不出来不等于没得画：瑞士轮本来就是「先到 3 胜出线」的积分跑道
        lanes = swiss_lanes(items)
        rows = swiss_rows(items)
        if lanes and swiss_ready(rows, rounds=len(lanes)):
            png = await self._cards.swiss(
                title=title,
                columns=lanes,
                standings=swiss_standings(rows),
                subtitle=(
                    f"{len(rows)} 队 · {len(lanes)} 轮 · {len(items)} 场"
                    f"｜三胜晋级、三负淘汰；每轮按赛前战绩分档（胜多在上）　{span}"
                ),
                source=source,
            )
            if png:
                return [png]

        # 兜底：画不成图的赛段老老实实列清单，一场都不能少
        pages = paginate_by_day(items, limit)
        out: list[bytes] = []
        for index, page in enumerate(pages, 1):
            page_title = title if len(pages) == 1 else f"{title}（{index}/{len(pages)}）"
            png = await self._cards.tournament(
                title=page_title,
                matches=page,
                subtitle=span,
                limit=0,
                source=source,
                total=len(items),
            )
            if png:
                out.append(png)
        return out

    # ------------------------------------------------------------------ 订阅

    def _cmd_sub_list(self, umo: str) -> str:
        assert self._store is not None
        subs = self._store.subs(umo)
        if not subs:
            return f"本会话还没有订阅任何联赛。用「{_CMD} 联赛」看可用 ID，再「{_CMD} 订阅 <ID>」。"
        lines = ["📌 本会话订阅的联赛", ""]
        lines.extend(f"  {tid}" for tid in subs)
        lines.append("")
        lines.append(f"取消：{_CMD} 取消订阅 <ID>")
        return "\n".join(lines)

    async def _cmd_subscribe(self, umo: str, rest: list[str]) -> str:
        token = rest[0] if rest else ""
        if not token.isdigit():
            return f"用法：{_CMD} 订阅 <联赛ID>"
        assert self._store is not None
        ok, message = await self._store.subscribe(
            umo, token, limit=self._cfg_int("subscribe_limit", 3)
        )
        if not ok:
            return message
        conf = self._effective(umo)
        return (
            f"{message}\n"
            f"每晚 {conf['push_time']} 推送当日战果与次日赛程，"
            f"可提醒时段 {window_text(conf['windows'])}，"
            f"{mention_text(conf['mention'], conf['targets'])}。"
        )

    async def _cmd_unsubscribe(self, umo: str, rest: list[str]) -> str:
        token = rest[0] if rest else ""
        if not token.isdigit():
            return f"用法：{_CMD} 取消订阅 <联赛ID>"
        assert self._store is not None
        _ok, message = await self._store.unsubscribe(umo, token)
        return message

    # ------------------------------------------------------------------ 提醒设置

    async def _cmd_remind(self, umo: str, rest: list[str], event: AstrMessageEvent) -> str:
        assert self._store is not None
        conf = self._effective(umo)
        # 非管理员只能看，不能改
        if not self._is_admin(event):
            return self._remind_overview(umo, conf) + "\n\n（改设置需要管理员权限）"
        if not rest:
            return self._remind_overview(umo, conf)

        head = rest[0].lower()
        tail = rest[1:]

        if head in ("全体", "all", "atall"):
            return await self._set_mention(umo, "all")
        if head in ("关", "关闭", "off", "none", "无"):
            return await self._set_mention(umo, "none")
        if head in ("列表", "list"):
            if not conf["targets"]:
                return f"@ 名单是空的。用「{_CMD} 提醒 加 @某人」添加。"
            lines = ["📣 @ 名单", ""]
            lines.extend(f"  {tid}" for tid in conf["targets"])
            lines.append("")
            lines.append(f"移除：{_CMD} 提醒 删 @某人")
            return "\n".join(lines)
        if head in ("加", "add"):
            ats = self._extract_ats(event)
            if not ats:
                ats = [item for item in tail if item.isdigit()]
            if not ats:
                return f"用法：{_CMD} 提醒 加 @某人（@ 不出来就直接写用户 ID）"
            targets = list(dict.fromkeys(conf["targets"] + ats))
            await self._store.update(umo, mention="list", targets=targets)
            return f"已加入 @ 名单：{'、'.join(ats)}\n当前共 {len(targets)} 人。"
        if head in ("删", "remove", "del"):
            ats = self._extract_ats(event) or [item for item in tail if item.isdigit()]
            if not ats:
                return f"用法：{_CMD} 提醒 删 @某人"
            targets = [item for item in conf["targets"] if item not in ats]
            await self._store.update(umo, targets=targets, mention="list" if targets else "none")
            return f"已移除：{'、'.join(ats)}\n当前 @ 名单 {len(targets)} 人。"
        if head in ("时段", "window", "windows"):
            return await self._cmd_window(umo, tail)
        if head in ("时间", "时刻", "time"):
            raw = tail[0] if tail else ""
            try:
                hour, minute = parse_clock(raw)
            except WindowError:
                return f"用法：{_CMD} 提醒 时间 23:00"
            hhmm = f"{hour:02d}:{minute:02d}"
            await self._store.update(umo, push_time=hhmm)
            return f"推送时间已改为 {hhmm}。"
        return f"不认识的提醒设置「{head}」。发 {_CMD} 提醒 看当前设置。"

    def _remind_overview(self, umo: str, conf: dict[str, Any]) -> str:
        workday_status = self._workday.status_line() if self._workday else "不可用"
        return "\n".join(
            [
                "🔔 提醒设置",
                "",
                f"推送时间：{conf['push_time']}",
                f"可提醒时段：{window_text(conf['windows'])}",
                f"提醒方式：{mention_text(conf['mention'], conf['targets'])}",
                f"@ 名单人数：{len(conf['targets'])}",
                "",
                f"改 @ 方式：{_CMD} 提醒 全体 / {_CMD} 提醒 关 / {_CMD} 提醒 加 @某人",
                f"改时段：{_CMD} 提醒 时段 加 工作日 09:00-23:30",
                f"改时间：{_CMD} 提醒 时间 23:00",
                "",
                f"工作日日历：{workday_status}",
            ]
        )

    async def _set_mention(self, umo: str, mode: str) -> str:
        assert self._store is not None
        await self._store.update(umo, mention=mode)
        if mode == "all":
            return (
                "已设置为推送时 @全体成员。\n"
                "如果没 @ 出来，基本是机器人没有群管理权限——消息会照常发出，只是不带 @。"
            )
        return "已关闭 @。推送只发内容。"

    async def _cmd_window(self, umo: str, tail: list[str]) -> str:
        assert self._store is not None
        conf = self._effective(umo)
        if not tail:
            return f"用法：{_CMD} 提醒 时段 加 工作日 09:00-23:30"

        action = tail[0].lower()
        rest = tail[1:]

        if action in ("列表", "list", ""):
            if not conf["windows"]:
                return "当前没有时段限制（全天可提醒）。"
            lines = ["⏰ 可提醒时段", ""]
            for index, window in enumerate(conf["windows"], start=1):
                lines.append(f"  {index}. {window_text([window])}")
            lines.append("")
            lines.append(f"删除：{_CMD} 提醒 时段 删 <序号>")
            return "\n".join(lines)

        if action in ("清", "清除", "clear"):
            await self._store.update(umo, windows=[])
            return "已清空时段限制，改为全天可提醒。"

        if action in ("删", "删除", "remove", "del"):
            raw = rest[0] if rest else ""
            if not raw.isdigit():
                return f"用法：{_CMD} 提醒 时段 删 <序号>"
            index = int(raw)
            windows = list(conf["windows"])
            if not 1 <= index <= len(windows):
                return f"序号超了，当前只有 {len(windows)} 个时段。"
            windows.pop(index - 1)
            await self._store.update(umo, windows=windows)
            return f"已删除第 {index} 个时段。当前：{window_text(windows)}"

        if action in ("加", "添加", "add"):
            try:
                window = parse_window(rest)
            except WindowError as exc:
                return str(exc)
            windows = list(conf["windows"])
            if window in windows:
                return "这个时段已经有了。"
            windows.append(window)
            await self._store.update(umo, windows=windows)
            return f"已添加时段：{window_text([window])}\n当前：{window_text(windows)}"

        return f"用法：{_CMD} 提醒 时段 加|删|清|列表"

    # ------------------------------------------------------------------ 设置

    async def _cmd_config(self, umo: str, rest: list[str], event: AstrMessageEvent) -> str:
        assert self._store is not None
        if not rest or not self._is_admin(event):
            return "\n".join(
                [
                    "⚙️ 数据源设置",
                    "",
                    f"  当前源：{self._effective(umo)['provider']}",
                    f"  {_CMD} 设置 源 scoregg|pandascore",
                    f"  {_CMD} 设置 key <PandaScore Key>   只对当前会话生效",
                    f"  {_CMD} 设置 清key",
                ]
            )

        head = rest[0].lower()
        tail = rest[1:]

        if head in ("源", "provider", "source"):
            raw = tail[0] if tail else ""
            if not raw:
                return f"用法：{_CMD} 设置 源 scoregg|pandascore"
            name = raw.lower()
            if name not in ("scoregg", "pandascore", "默认", "default"):
                return "只支持 scoregg 或 pandascore。"
            if name in ("默认", "default"):
                await self._store.update(umo, provider=None)
                return f"已恢复默认源（{self._cfg_str('default_provider', 'scoregg')}）。"
            await self._store.update(umo, provider=name)
            if name == "pandascore" and not self._effective(umo)["token"]:
                return (
                    "已切到 PandaScore，但还没有 Key。\n"
                    f"申请方法见仓库 docs/pandascore.md，然后 {_CMD} 设置 key <你的Key>。"
                )
            return f"本会话数据源已切到 {name}。"

        if head in ("key", "密钥", "token"):
            if not self._cfg_bool("allow_session_token", True):
                return "插件配置里关闭了「允许会话自定义 Key」，只能改全局配置。"
            raw = tail[0] if tail else ""
            if not raw:
                return f"用法：{_CMD} 设置 key <PandaScore Key>"
            if len(raw) < 10:
                return "这个 Key 看着不像 PandaScore 的，检查一下再发。"
            await self._store.update(umo, token=raw)
            return "已保存本会话的 PandaScore Key（不回显内容）。"

        if head in ("清key", "清密钥", "clearkey"):
            await self._store.update(umo, token=None)
            return "已清除本会话的 Key，改用插件配置里的默认 Key。"

        return f"不认识的设置项「{head}」。发 {_CMD} 设置 看用法。"

    def _cmd_status(self, umo: str) -> str:
        conf = self._effective(umo)
        assert self._store is not None
        return render.settings_report(
            provider=conf["provider"],
            provider_default=self._cfg_str("default_provider", "scoregg"),
            mention=mention_text(conf["mention"], conf["targets"]),
            mention_default=mention_text(
                normalize_mention(self._cfg_str("default_mention", "none"), "none"), []
            ),
            targets=conf["targets"],
            windows=conf["windows"],
            windows_default=parse_windows_text(self._cfg_str("default_windows", "")),
            push_time=conf["push_time"],
            push_time_default=self._cfg_str("push_time", "23:00"),
            subs=conf["subs"],
            has_token=bool(conf["token"]),
            workday_status=self._workday.status_line() if self._workday else "不可用",
            prefix=_CMD,
        )

    async def _cmd_test_push(self, umo: str, event: AstrMessageEvent) -> str:
        """立刻按当前设置发一条推送，用来验证 @ 和排版。

        不标记「今天已推过」，也不管可提醒时段——它就是一次手动触发。
        """
        if not self._is_admin(event):
            return "测试推送需要管理员权限。"
        conf = self._effective(umo)
        assert self._hub is not None
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        used, today_matches = await self._hub.call(
            "today", provider=conf["provider"], token=conf["token"], today=today
        )
        _, tomorrow_matches = await self._hub.call(
            "schedule", provider=conf["provider"], token=conf["token"], day=tomorrow
        )
        sub_set = set(conf["subs"]) or {m.tournament_id for m in today_matches + tomorrow_matches}
        results = [m for m in today_matches if m.tournament_id in sub_set]
        upcoming = [m for m in tomorrow_matches if m.tournament_id in sub_set]

        text = render.push_report(today, tomorrow, results, upcoming, source=self._label(used))
        images = await self._push_cards(results, conf, self._label(used))
        ok = await self._notifier.send(
            umo, text, mode=conf["mention"], targets=conf["targets"], images=images
        )
        if not ok:
            return "发送失败，看日志里有没有平台报错。"
        return (
            f"已发一条测试推送（{mention_text(conf['mention'], conf['targets'])}）。\n"
            "没收到 @ 的话，检查机器人是否有群管理权限。"
        )

    # ------------------------------------------------------------------ 推送

    async def _push_loop(self) -> None:
        """每 30 秒看一次有没有到点的会话。"""
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 推送循环不能死
                logger.warning(f"[LOL赛事] 推送循环异常：{type(exc).__name__}: {exc}")
            await asyncio.sleep(_TICK_SECONDS)

    async def _tick(self) -> None:
        if self._store is None or self._hub is None:
            return
        now = datetime.now()
        if self._workday is not None:
            await self._workday.maintain(now)

        targets = self._store.subscribed_sessions()
        if not targets:
            return

        today = now.strftime("%Y-%m-%d")
        for umo, subs in targets.items():
            try:
                await self._maybe_push(umo, subs, now, today)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[LOL赛事] 推送 {umo} 失败：{type(exc).__name__}: {exc}")

    async def _maybe_push(self, umo: str, subs: list[str], now: datetime, today: str) -> None:
        assert self._store is not None
        if self._store.pushed_today(umo, today):
            return

        conf = self._effective(umo)
        try:
            hour, minute = (int(part) for part in conf["push_time"].split(":"))
        except (ValueError, AttributeError):
            hour, minute = 23, 0
        if (now.hour, now.minute) < (hour, minute):
            return

        is_workday = self._workday.is_workday_opt(now.date()) if self._workday else None
        if not in_windows(now, conf["windows"], is_workday):
            logger.debug(
                f"[LOL赛事] {umo} 到点但不在可提醒时段，"
                f"下次可提醒：{next_open_time(now, conf['windows'], is_workday)}"
            )
            return

        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        assert self._hub is not None
        used, today_matches = await self._hub.call(
            "today", provider=conf["provider"], token=conf["token"], today=today
        )
        _, tomorrow_matches = await self._hub.call(
            "schedule",
            provider=conf["provider"],
            token=conf["token"],
            day=tomorrow,
        )

        sub_set = set(subs)
        results = [m for m in today_matches if m.tournament_id in sub_set]
        upcoming = [m for m in tomorrow_matches if m.tournament_id in sub_set]
        if not results and not upcoming:
            await self._store.mark_pushed(umo, today)
            logger.debug(f"[LOL赛事] {umo} 订阅的联赛今天明天都没比赛，跳过推送")
            return

        text = render.push_report(today, tomorrow, results, upcoming, source=self._label(used))
        images = await self._push_cards(results, conf, self._label(used))
        ok = await self._notifier.send(
            umo, text, mode=conf["mention"], targets=conf["targets"], images=images
        )
        if ok:
            await self._store.mark_pushed(umo, today)
            logger.info(
                f"[LOL赛事] 已推送 {umo}：{len(results)} 场战果、{len(upcoming)} 场赛程、"
                f"{len(images)} 张图"
            )

    async def _push_cards(self, results, conf: dict[str, Any], label: str) -> list[bytes]:
        """给已结束的比赛配单局详卡。

        每场要多打一次详情 + 每局一次 CDN，所以有个张数上限：
        订阅了三个联赛又赶上比赛日的话，不限制会一次发十几张图。
        """
        if self._cards is None or not self._cards.available or self._hub is None:
            return []
        limit = self._cfg_int("push_detail_cards", 2)
        if limit <= 0:
            return []
        images: list[bytes] = []
        for match in results[:limit]:
            if not match.finished:
                continue
            try:
                _, detail = await self._hub.call(
                    "detail",
                    provider=conf["provider"],
                    token=conf["token"],
                    match_id=match.id,
                )
            except ProviderError as exc:
                logger.debug(f"[LOL赛事] 推送补详情失败 #{match.id}：{exc}")
                continue
            except Exception as exc:  # noqa: BLE001 补图失败不该拖垮推送
                logger.debug(f"[LOL赛事] 推送补详情异常 #{match.id}：{type(exc).__name__}: {exc}")
                continue
            if not detail.games:
                continue
            png = await self._cards.game(detail, detail.games[-1], source=label)
            if png:
                images.append(png)
        return images

    @staticmethod
    def _label(provider: str) -> str:
        return {"scoregg": "玩加赛事", "pandascore": "PandaScore"}.get(provider, provider)
