"""英雄中文名映射。

scoregg 只在数据里给英雄图标地址，从文件名拿到的是英文 ID（``Rumble``）。
直接展示对中文群不太友好，所以额外拉一份 Data Dragon 的 ``zh_CN`` 英雄表做映射。

这是一个**可选增强**：拉不到就退回英文 ID，绝不影响主流程。
Data Dragon 是拳头官方的静态资源 CDN，无需密钥、无调用配额，适合做这种映射表。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp

from ._log import logger

VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
CHAMPION_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/zh_CN/champion.json"


class ChampionBook:
    """英文 ID / 数字 ID → 中文英雄名。磁盘缓存，随插件数据目录走。"""

    def __init__(self, path: Path, *, timeout: float = 10.0) -> None:
        self._path = path
        self._timeout = timeout
        self._by_id: dict[str, str] = {}
        self._by_key: dict[str, str] = {}
        # 数据源自己的英雄编号 → Data Dragon 的英文 ID。
        # 新英雄的图标路径是纯数字（champion_new/543.png），拿不到名字，
        # 但同一个编号总是同一个英雄，所以从「有名字的那几场」学一份映射出来。
        self._hero_ids: dict[str, str] = {}
        self._dirty = False
        self._loaded = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 读取

    async def load(self) -> None:
        """从磁盘缓存加载。文件不存在就静默跳过（下次 refresh 会补）。"""
        async with self._lock:
            self._loaded = True
            if not self._path.exists():
                return
            try:
                raw = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
                parsed = json.loads(raw)
            except (OSError, ValueError) as exc:
                logger.debug(f"[LOL赛事] 英雄表读取失败：{exc}")
                return
            self._apply(parsed)

    def _apply(self, parsed: Any) -> None:
        if not isinstance(parsed, dict):
            return
        by_id = parsed.get("by_id")
        by_key = parsed.get("by_key")
        hero_ids = parsed.get("hero_ids")
        if isinstance(by_id, dict):
            self._by_id = {str(k): str(v) for k, v in by_id.items() if v}
        if isinstance(by_key, dict):
            self._by_key = {str(k): str(v) for k, v in by_key.items() if v}
        if isinstance(hero_ids, dict):
            self._hero_ids = {str(k): str(v) for k, v in hero_ids.items() if v}

    @property
    def ready(self) -> bool:
        return bool(self._by_id)

    @property
    def learned_count(self) -> int:
        """已经学到多少个英雄编号。预热逻辑用它判断要不要垫数据。"""
        return len(self._hero_ids)

    # ------------------------------------------------------------------ 学习

    def learn(self, hero_id: str, english_id: str) -> None:
        """记下「数据源英雄编号 → 英文 ID」。同一编号已有映射就不覆盖。"""
        if not hero_id or not english_id or english_id.isdigit():
            return
        if self._hero_ids.get(hero_id) == english_id:
            return
        self._hero_ids[hero_id] = english_id
        self._dirty = True

    async def save(self) -> None:
        """有学习到新映射时落盘。没有变化就什么都不做。"""
        if not self._dirty:
            return
        self._dirty = False
        await self._flush(self._version)

    # ------------------------------------------------------------------ 查询

    def name(self, token: str) -> str:
        """``Rumble`` / ``88`` → 中文名；查不到原样返回。"""
        raw = str(token or "").strip()
        if not raw:
            return ""
        return self._by_id.get(raw) or self._by_key.get(raw) or raw

    def by_ddragon(self, key: str) -> str:
        """Data Dragon 的数字 key：``84`` → 离群之刺。"""
        return self._by_key.get(str(key or "").strip(), "")

    def by_source(self, source_id: str) -> str:
        """数据源自己的英雄编号：``101`` → 暗黑元首（靠打过多场比赛学到）。"""
        raw = str(source_id or "").strip()
        if not raw:
            return ""
        return self.name(self._hero_ids.get(raw, ""))

    def resolve(self, hero_id: str, token: str) -> str:
        """优先用英文 ID；拿不到就用编号反查。都失败给一个占位符。"""
        raw = str(token or "").strip()
        if raw and not raw.isdigit():
            return self.name(raw)
        learned = self._hero_ids.get(str(hero_id or ""))
        if learned:
            return self.name(learned)
        # 编号本身就是 Data Dragon 的 key 时也能直接命中（有些源是这么给的）
        if raw.isdigit() and raw in self._by_key:
            return self._by_key[raw]
        return "—"

    @property
    def _version(self) -> str:
        return getattr(self, "_data_version", "") or ""

    # ------------------------------------------------------------------ 拉取

    async def refresh(self, *, version: str = "") -> bool:
        """拉一次英雄表并落盘。返回是否成功。"""
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if not version:
                    version = await self._latest_version(session)
                if not version:
                    return False
                url = CHAMPION_URL.format(version=version)
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.debug(f"[LOL赛事] 英雄表 HTTP {resp.status}")
                        return False
                    payload = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError, OSError) as exc:
            logger.debug(f"[LOL赛事] 英雄表拉取失败：{type(exc).__name__}: {exc}")
            return False

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data:
            return False

        by_id: dict[str, str] = {}
        by_key: dict[str, str] = {}
        for entry in data.values():
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            if entry.get("id"):
                by_id[str(entry["id"])] = name
            if entry.get("key"):
                by_key[str(entry["key"])] = name

        self._by_id, self._by_key = by_id, by_key
        self._data_version = version
        self._dirty = False
        await self._flush(version)
        logger.info(f"[LOL赛事] 已更新英雄中文名 {len(by_id)} 个（{version}）")
        return True

    @staticmethod
    async def _latest_version(session: aiohttp.ClientSession) -> str:
        try:
            async with session.get(VERSIONS_URL) as resp:
                if resp.status != 200:
                    return ""
                versions = await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError, ValueError):
            return ""
        return str(versions[0]) if isinstance(versions, list) and versions else ""

    async def _flush(self, version: str) -> None:
        self._data_version = version
        payload = json.dumps(
            {
                "version": version,
                "by_id": self._by_id,
                "by_key": self._by_key,
                "hero_ids": self._hero_ids,
            },
            ensure_ascii=False,
            indent=1,
        )
        tmp = self._path.with_suffix(".tmp")

        def _write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._path)

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            logger.debug(f"[LOL赛事] 英雄表落盘失败：{exc}")
