"""召唤师技能编号 → 中文名。

源站逐局明细里，召唤师技能只给图标地址：``/images/lol/skill/4.png``。
那个数字是拳头官方的 summoner spell ID，跨版本稳定，所以直接按表翻译，
不需要额外请求，也不会因为新英雄上线而失效。

为什么自带一张表而不是去拉 Data Dragon
--------------------------------------
``summoner.json`` 在 Data Dragon 上有，但只有 12 条、几年不变，为它多打一次网络
请求（还要处理失败）不划算。表就写在这里，缺了某个 ID 显示编号本身，不影响其他字段。
"""

from __future__ import annotations

import re

# 拳头官方 summoner spell ID。名字取国服译名。
SPELL_NAMES: dict[int, str] = {
    1: "净化",
    3: "虚弱",
    4: "闪现",
    6: "疾跑",
    7: "治疗",
    11: "惩戒",
    12: "传送",
    13: "清晰术",
    14: "点燃",
    21: "屏障",
}

# 展示顺序：闪现永远排第一，其余按这个顺序，最后才是未知编号
_ORDER = (4, 12, 11, 14, 7, 21, 3, 6, 1, 13)

_SPELL_ID = re.compile(r"/skill/(\d+)\.png")


def spell_id(url: str) -> int:
    """从图标地址里取编号；取不到返回 0。"""
    found = _SPELL_ID.search(str(url or ""))
    return int(found.group(1)) if found else 0


def spell_name(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    number = spell_id(raw)
    if not number:
        # 地址格式变了也别丢信息，至少让人看出是个技能图标
        return ""
    return SPELL_NAMES.get(number, f"技能{number}")


def spell_names(urls: list[str]) -> list[str]:
    """把一组的技能图标翻译成中文名，按「闪现优先」排序并去空。"""
    return [name for name, _ in spell_pairs(urls)]


def spell_pairs(urls: list[str]) -> list[tuple[str, str]]:
    """``[(中文名, 图标地址)]``，顺序与 :func:`spell_names` 完全一致。

    卡片要画图标、文字要写名字，两边必须同序，所以排序只在这里做一次。
    """
    pairs = [(spell_id(url), spell_name(url), str(url)) for url in urls if url]
    pairs = [item for item in pairs if item[1]]

    def rank(item: tuple[int, str, str]) -> int:
        try:
            return _ORDER.index(item[0])
        except ValueError:
            return len(_ORDER)

    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for _, name, url in sorted(pairs, key=rank):
        if name in seen:
            continue
        seen.add(name)
        out.append((name, url))
    return out
