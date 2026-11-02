"""工作日日历：判断「今天是不是工作日」，含节假日与调休。

数据来源
--------
`NateScarlet/holiday-cn <https://github.com/NateScarlet/holiday-cn>`_ 每年一份 JSON，
结构是 ``days: [{name, date, isOffDay}]``，由国务院公告整理而来。判断规则：

1. 在 ``makeup``（调休上班）里 → 工作日，哪怕这天是周六；
2. 在 ``holidays``（放假）里 → 休息日，哪怕这天是周三；
3. 其余按星期几算：周一到周五工作日，周六周日休息。

抓取策略
--------
次年安排一般要到 10 月底到 11 月才公布，公布前那条 JSON 是 200 + 空数组。
所以判断「有没有」只能看 ``days`` 是否非空，不能看 HTTP 状态码。
策略是：**过了开放日（默认 11-01）起每天试一次，拿到就停手**。
数据缺失的年份退化成纯周末规则，并在状态里标成「未就绪」。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp

from ._log import logger

DEFAULT_SOURCE = "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json"
FALLBACK_SOURCES = (
    "https://fastly.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json",
    "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json",
)

_ISO_DATE = "%Y-%m-%d"


def parse_month_day(text: object, default: tuple[int, int]) -> tuple[int, int]:
    """``"11-01"`` / ``"11/1"`` / ``"11月1日"`` → ``(11, 1)``。写坏了回落默认值。"""
    raw = str(text or "").strip()
    if not raw:
        return default
    for sep in ("-", "/", "月", "."):
        if sep in raw:
            head, _, tail = raw.partition(sep)
            tail = tail.strip("日 ")
            try:
                month, day = int(head), int(tail)
            except ValueError:
                return default
            try:
                date(2000, month, day)
            except ValueError:
                return default
            return month, day
    return default


class WorkdayCalendar:
    """一年的工作日 / 休息日判断，外加数据自身的维护周期。"""

    def __init__(
        self,
        path: Path,
        *,
        source: str = DEFAULT_SOURCE,
        fetch_date: tuple[int, int] = (11, 1),
        timeout: float = 10.0,
    ) -> None:
        self._path = path
        self._source = source.strip() or DEFAULT_SOURCE
        self._fetch_date = fetch_date
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self._years: dict[int, dict[str, Any]] = {}
        self._attempts: dict[int, str] = {}
        self._loaded = False

    # ------------------------------------------------------------------ 读写

    async def load(self) -> None:
        async with self._lock:
            self._loaded = True
            if not self._path.exists():
                return
            try:
                raw = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
                parsed = json.loads(raw)
            except (OSError, ValueError) as exc:
                logger.warning(f"[LOL赛事] 工作日日历读取失败，按空数据处理：{exc}")
                return
            if not isinstance(parsed, dict):
                return
            for key, value in (parsed.get("years") or {}).items():
                try:
                    year = int(key)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    self._years[year] = self._repair(year, value)
            for key, value in (parsed.get("attempts") or {}).items():
                try:
                    self._attempts[int(key)] = str(value)
                except (TypeError, ValueError):
                    continue

    @staticmethod
    def _blank(year: int) -> dict[str, Any]:
        return {
            "year": year,
            "status": "",
            "source": "",
            "fetched_at": "",
            "holidays": {},
            "makeup": {},
        }

    @classmethod
    def _repair(cls, year: int, item: dict[str, Any]) -> dict[str, Any]:
        blank = cls._blank(year)
        blank["status"] = "ready" if item.get("status") == "ready" else ""
        for key in ("source", "fetched_at"):
            blank[key] = str(item.get(key) or "")
        for key in ("holidays", "makeup"):
            value = item.get(key)
            if isinstance(value, dict):
                blank[key] = {str(k): str(v or "") for k, v in value.items()}
        if blank["holidays"]:
            blank["status"] = "ready"
        return blank

    async def _flush(self) -> None:
        payload = json.dumps(
            {
                "version": 1,
                "years": {str(k): v for k, v in sorted(self._years.items())},
                "attempts": {str(k): v for k, v in sorted(self._attempts.items())},
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        )
        tmp = self._path.with_suffix(".tmp")

        def _write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._path)

        await asyncio.to_thread(_write)

    # ------------------------------------------------------------------ 判断

    def has_data(self, year: int) -> bool:
        return (self._years.get(year) or {}).get("status") == "ready"

    def is_workday(self, day: date) -> bool:
        item = self._years.get(day.year) or {}
        key = day.strftime(_ISO_DATE)
        if key in (item.get("makeup") or {}):
            return True
        if key in (item.get("holidays") or {}):
            return False
        return day.weekday() < 5

    def is_workday_opt(self, day: date) -> bool | None:
        """带「数据是否就绪」的版本：没数据时返回 ``None``，交给调用方决定怎么兜底。"""
        return self.is_workday(day) if self.has_data(day.year) else None

    def day_note(self, day: date) -> str:
        item = self._years.get(day.year) or {}
        key = day.strftime(_ISO_DATE)
        if key in (item.get("holidays") or {}):
            return f"{item['holidays'][key]}放假"
        if key in (item.get("makeup") or {}):
            return f"{item['makeup'][key]}调休上班"
        return "周末" if day.weekday() >= 5 else "普通工作日"

    def status_line(self) -> str:
        today = date.today()
        parts: list[str] = []
        for year in (today.year, today.year + 1):
            if self.has_data(year):
                got = (self._years.get(year) or {}).get("fetched_at", "")[:10]
                parts.append(f"{year} 已就绪（{got}）")
            else:
                parts.append(f"{year} 未就绪，按周末规则估算")
        return "；".join(parts)

    # ------------------------------------------------------------------ 抓取

    async def _get_json(self, url: str) -> Any:
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(url, headers={"User-Agent": "astrbot-plugin-lolmatch"}) as resp,
            ):
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError, OSError) as exc:
            logger.debug(f"[LOL赛事] 节假日源不可用 {url}：{type(exc).__name__}: {exc}")
            return None

    async def fetch_year(self, year: int) -> bool:
        for template in (self._source, *FALLBACK_SOURCES):
            url = template.format(year=year)
            payload = await self._get_json(url)
            if not isinstance(payload, dict):
                continue
            days = payload.get("days")
            if not isinstance(days, list):
                continue
            if not days:
                # 源可达但这一年还没公布。备源读的是同一份数据，不用再试。
                return False

            holidays: dict[str, str] = {}
            makeup: dict[str, str] = {}
            prefix = f"{year}-"
            for entry in days:
                if not isinstance(entry, dict):
                    continue
                raw_date = str(entry.get("date") or "").strip()
                if not raw_date.startswith(prefix):
                    continue
                name = str(entry.get("name") or "").strip()[:12]
                if entry.get("isOffDay"):
                    holidays[raw_date] = name
                else:
                    makeup[raw_date] = name
            if not holidays:
                continue

            item = self._years.setdefault(year, self._blank(year))
            item.update(
                {
                    "status": "ready",
                    "source": url,
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                    "holidays": holidays,
                    "makeup": makeup,
                }
            )
            self._attempts[year] = datetime.now().isoformat(timespec="seconds")
            await self._flush()
            logger.info(
                f"[LOL赛事] 已获取 {year} 年节假日："
                f"{len(holidays)} 天放假、{len(makeup)} 天调休上班"
            )
            return True
        return False

    async def maintain(self, now: datetime | None = None) -> list[str]:
        """每次心跳调用。绝大多数时候只做内存判断，跨到新的一天才会发请求。"""
        now = now or datetime.now()
        today = now.date()
        year = today.year
        events: list[str] = []

        async with self._lock:
            if not self._loaded:
                self._loaded = True

            if not self.has_data(year) and await self._try_fetch(year, today):
                events.append(f"获取 {year} 年节假日")

            target = year + 1
            if (
                today >= self._date_in(year, self._fetch_date)
                and not self.has_data(target)
                and await self._try_fetch(target, today)
            ):
                events.append(f"获取 {target} 年节假日")

            removed = await self._cleanup(today)
            if removed:
                events.append("清理 " + "、".join(str(y) for y in removed) + " 年数据")

        if events:
            logger.debug("[LOL赛事] 工作日日历：" + "；".join(events))
        return events

    async def _try_fetch(self, year: int, today: date) -> bool:
        """一天内最多试一次，失败后 30 分钟再试。

        为什么不试一次就等到明天：启动时网络抖一下很常见，直接锁到明天
        会让「工作日判断」整天退化成纯周末规则。30 分钟的间隔既不会打爆
        数据源，也能自愈。
        """
        last = self._attempts.get(year) or ""
        if last:
            try:
                gap = datetime.now() - datetime.fromisoformat(last)
            except ValueError:
                gap = None
            if gap is not None and gap < timedelta(minutes=30):
                return False
        self._attempts[year] = datetime.now().isoformat(timespec="seconds")
        ok = await self.fetch_year(year)
        if not ok:
            await self._flush()  # 记下这次尝试的时间
        return ok

    @staticmethod
    def _date_in(year: int, month_day: tuple[int, int]) -> date:
        try:
            return date(year, month_day[0], month_day[1])
        except ValueError:
            return date(year, 11, 1)

    async def _cleanup(self, today: date) -> list[int]:
        """当前年和次年必须留着，其余比今年早的年份清掉。"""
        keep = {today.year, today.year + 1}
        removed = [y for y in self._years if y not in keep and y < today.year]
        if not removed:
            return []
        for year in removed:
            self._years.pop(year, None)
            self._attempts.pop(year, None)
        await self._flush()
        return removed

    def days_until(self, day: date) -> int:
        return (day - date.today()).days

    def next_workday(self, day: date, *, horizon: int = 40) -> date | None:
        cursor = day + timedelta(days=1)
        for _ in range(horizon):
            if self.is_workday(cursor):
                return cursor
            cursor += timedelta(days=1)
        return None
