"""图片资源下载与缓存。

卡片的队标、英雄头像、装备、召唤师技能、天赋图标全是 ``img.scoregg.com`` 上的外链。
群聊客户端不会去加载外链图，所以必须在服务端下下来贴进图画里。

三条原则
--------
1. **失败不影响主流程**：任何一张图拿不到就跳过，卡片照画，缺的位置留空。
   一次详情最多要下 100 多张小图，个别超时是常态。
2. **磁盘缓存**：同一张队标 / 英雄头像会被反复用到，落盘后第二次就只读本地。
   文件名用 URL 的 sha1，不解析源站的路径结构——它随时会变。
3. **有上限**：装备图标数量无上限（每个赛季都新增），所以给个文件数上限，
   超了就不再往磁盘写，只走内存。缓存目录可以随时整个删掉，删了会自动重建。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import aiohttp

from ._log import logger

_MAX_FILES = 3000
_MAX_BYTES = 2 * 1024 * 1024  # 单张图超过 2MB 就不要了，正常的图标都几十 KB


class IconStore:
    """按 URL 取图片字节，带内存 + 磁盘两层缓存。"""

    def __init__(
        self,
        root: Path,
        *,
        enabled: bool = True,
        timeout: float = 12.0,
        concurrency: int = 6,
    ) -> None:
        self._root = root
        self._enabled = enabled
        self._timeout = timeout
        self._sem = asyncio.Semaphore(concurrency)
        self._memory: dict[str, bytes | None] = {}
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._files = 0
        self._counted = False

    # ------------------------------------------------------------------ 生命周期

    async def _http(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                    headers={
                        "user-agent": "Mozilla/5.0 (compatible; astrbot-plugin-lolmatch)",
                        "referer": "https://www.scoregg.com/",
                        "accept": "image/*,*/*",
                    },
                )
            return self._session

    async def close(self) -> None:
        async with self._lock:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None
        self._memory.clear()

    # ------------------------------------------------------------------ 读取

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()

    def _path(self, url: str) -> Path:
        key = self._key(url)
        return self._root / key[:2] / f"{key}.img"

    async def get(self, url: str) -> bytes | None:
        """取一张图。命中缓存直接返回，取不到返回 ``None``。"""
        raw = str(url or "").strip()
        if not raw or not self._enabled:
            return None
        if raw in self._memory:
            return self._memory[raw]

        cached = await self._read_disk(raw)
        if cached:
            self._memory[raw] = cached
            return cached

        data = await self._download(raw)
        if data:
            # 失败**不进内存缓存**：CDN 偶发超时很常见，缓存住反而让这张图永远缺失
            self._memory[raw] = data
            await self._write_disk(raw, data)
        return data

    async def _read_disk(self, url: str) -> bytes | None:
        path = self._path(url)
        try:
            if not path.exists():
                return None
            return await asyncio.to_thread(path.read_bytes)
        except OSError:
            return None

    async def _write_disk(self, url: str, data: bytes) -> None:
        if not self._counted:
            self._counted = True
            self._files = await asyncio.to_thread(self._count_files)
        if self._files >= _MAX_FILES:
            return
        path = self._path(url)
        try:
            await asyncio.to_thread(self._put, path, data)
            self._files += 1
        except OSError as exc:
            logger.debug(f"[LOL赛事] 图标落盘失败：{exc}")

    @staticmethod
    def _put(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def _count_files(self) -> int:
        try:
            return sum(1 for _ in self._root.rglob("*.img"))
        except OSError:
            return 0

    async def _download(self, url: str) -> bytes | None:
        session = await self._http()
        async with self._sem:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.debug(f"[LOL赛事] 图标 {url} 返回 HTTP {resp.status}")
                        return None
                    data = await resp.read()
            except (TimeoutError, aiohttp.ClientError) as exc:
                logger.debug(f"[LOL赛事] 图标下载失败 {url}：{type(exc).__name__}")
                return None
        if not data or len(data) > _MAX_BYTES:
            return None
        return data

    # ------------------------------------------------------------------ 批量

    async def warm(self, urls: list[str]) -> dict[str, bytes]:
        """并发预热一批图，返回拿到的那些。插图前先调一次，省得边画边等。"""
        wanted = [url for url in dict.fromkeys(urls) if url]
        if not wanted or not self._enabled:
            return {}
        results = await asyncio.gather(*(self.get(url) for url in wanted))
        return {url: data for url, data in zip(wanted, results, strict=False) if data}
