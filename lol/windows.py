"""可提醒时段：解析、判定、展示。

写法（沿用和风天气插件那套，用户不用重新学）
------------------------------------------
- ``08:00-23:30``            全天可提醒，但只在 08:00 到 23:30 之间
- ``工作日 09:00-18:00``     只在工作日的这个区间提醒
- ``休息日 10:00-22:00``     只在休息日提醒
- ``22:00-06:00``            跨零点，夜里也算
- ``08:00-12:00, 20:00-23:00``  多个时段，逗号分隔
- 空                        不限制，全天可提醒

判断规则
--------
1. 时段带「工作日 / 休息日」前缀时，先用工作日日历判断当天属性；
   日历数据没就绪（``is_workday is None``）时按「不满足」处理——
   宁可少推一次，也不要在周末早上把战报推给不想看的人。
2. 其余时段不看日期，只看时钟。
3. 列表里**任意一个**时段命中即可提醒。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

DAY_ALIAS: dict[str, str] = {
    "工作日": "workday",
    "上班日": "workday",
    "workday": "workday",
    "work": "workday",
    "休息日": "restday",
    "周末": "restday",
    "节假日": "restday",
    "restday": "restday",
    "rest": "restday",
    "weekend": "restday",
    "不限": "all",
    "每天": "all",
    "全天": "all",
    "all": "all",
    "any": "all",
}

DAY_LABEL = {"workday": "工作日", "restday": "休息日", "all": ""}

_CLOCK_RE = re.compile(r"^(\d{1,2})\s*[:：]\s*(\d{1,2})$")
_SPLIT_RE = re.compile(r"[,，、;；\s]+")


class WindowError(ValueError):
    """时段写法不认识。消息直接回给用户看。"""


def parse_clock(text: str) -> tuple[int, int]:
    """``07:30`` / ``7：30`` / ``0730`` / ``7`` → ``(7, 30)``。"""
    raw = str(text or "").strip()
    if not raw:
        raise WindowError("时间不能为空")
    match = _CLOCK_RE.match(raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
    elif re.fullmatch(r"\d{3,4}", raw):
        hour, minute = int(raw[:-2]), int(raw[-2:])
    elif re.fullmatch(r"\d{1,2}", raw):
        hour, minute = int(raw), 0
    else:
        raise WindowError(f"时间格式不对：{text}（要 08:00 这种）")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise WindowError(f"时间越界：{text}")
    return hour, minute


def fmt_clock(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def normalize_days(value: Any) -> str:
    """认不出的写法一律当 ``all``，避免脏配置把整条提醒挡掉。"""
    return DAY_ALIAS.get(str(value or "").strip().lower(), "all")


def parse_window(tokens: list[str]) -> dict[str, str]:
    """``["工作日", "09:00-18:00"]`` → ``{"start", "end", "days"}`` 三个字段。"""
    days = "all"
    span = ""
    # 长词优先：「工作日」要排在「日」这类短别名之前
    words = sorted(DAY_ALIAS, key=len, reverse=True)

    for token in tokens:
        key = str(token or "").strip()
        if not key:
            continue
        if key in DAY_ALIAS:
            days = DAY_ALIAS[key]
            continue
        # 兼容「工作日09:00-18:00」这种没打空格的写法
        glued = next((w for w in words if key.startswith(w) and len(key) > len(w)), "")
        if glued:
            days = DAY_ALIAS[glued]
            key = key[len(glued) :].strip(" :：,，")
        if any(ch in key for ch in ("-", "—", "~", "到")):
            span = key
            continue
        raise WindowError(
            f"时段写法不认识：{key}（用 08:00-12:00，或加前缀「工作日 09:00-18:00」）"
        )

    if not span:
        raise WindowError("缺少时间范围，例如：08:00-12:00")

    raw = span.replace("~", "-").replace("到", "-").replace("—", "-")
    parts = [part for part in raw.split("-") if part.strip()]
    if len(parts) != 2:
        raise WindowError("时段格式：08:00-12:00")
    return {
        "start": fmt_clock(*parse_clock(parts[0])),
        "end": fmt_clock(*parse_clock(parts[1])),
        "days": days,
    }


def parse_windows_text(text: str) -> list[dict[str, str]]:
    """配置里那段 ``default_windows`` 文本 → 时段列表。乱写的部分跳过。"""
    raw = str(text or "").strip()
    if not raw:
        return []
    out: list[dict[str, str]] = []
    for chunk in re.split(r"[,，、;；]+", raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(parse_window(chunk.split()))
        except WindowError:
            continue
    return out


def day_allowed(mode: Any, is_workday: bool | None) -> bool:
    """这个时段今天该不该生效。"""
    normalized = normalize_days(mode)
    if normalized == "all":
        return True
    if is_workday is None:
        return False
    return is_workday if normalized == "workday" else not is_workday


def in_windows(
    now: datetime, windows: list[dict[str, Any]], is_workday: bool | None = None
) -> bool:
    """当前时刻是否落在任一可提醒时段内。空列表 = 全天可提醒。"""
    if not windows:
        return True
    current = now.hour * 60 + now.minute
    for window in windows:
        if not isinstance(window, dict):
            continue
        if not day_allowed(window.get("days"), is_workday):
            continue
        try:
            start_h, start_m = parse_clock(window.get("start") or "00:00")
            end_h, end_m = parse_clock(window.get("end") or "23:59")
        except WindowError:
            continue
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start <= end:
            if start <= current <= end:
                return True
        elif current >= start or current <= end:  # 跨零点
            return True
    return False


def window_text(windows: list[dict[str, Any]]) -> str:
    """给用户看的一行摘要。"""
    if not windows:
        return "全天"
    parts: list[str] = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        mode = normalize_days(window.get("days"))
        prefix = f"{DAY_LABEL[mode]} " if mode != "all" else ""
        parts.append(f"{prefix}{window.get('start', '?')}-{window.get('end', '?')}")
    return "、".join(parts) or "全天"


def next_open_time(
    now: datetime, windows: list[dict[str, Any]], is_workday: bool | None = None
) -> str:
    """下一次可提醒的时间点，用于「现在不在可提醒时段」时的提示。

    只在今天剩余时间与明天之间找，找不到就说「明天再说」，不做无限搜索。
    """
    if not windows:
        return "现在"
    for offset in range(2):
        target_day = date.fromordinal(now.date().toordinal() + offset)
        flag = is_workday if offset == 0 else None
        for window in windows:
            if not isinstance(window, dict):
                continue
            if offset == 0 and not day_allowed(window.get("days"), flag):
                continue
            try:
                hour, minute = parse_clock(window.get("start") or "00:00")
            except WindowError:
                continue
            candidate = datetime.combine(target_day, datetime.min.time()).replace(
                hour=hour, minute=minute
            )
            if candidate > now:
                label = "今天" if offset == 0 else "明天"
                return f"{label} {fmt_clock(hour, minute)}"
    return "稍后"
