"""数据源门面：按会话选源，失败自动切下一个。

上层（指令、推送）只跟这里打交道，不关心下面是 scoregg 还是 PandaScore。

选源顺序
--------
1. 会话自己指定的源（``/lol 设置 源 pandascore``）；
2. 会话没指定就用插件配置的默认源；
3. 当前源抛 ``retryable`` 的 ``ProviderError`` 时，换另一个源重试一次；
   PandaScore 没配 Key 就直接跳过。

为什么只换一次
--------------
两个源同时挂的时候，反复重试只会把等待时间拉长、把日志刷满。
一次就够——用户看到的是「两个源都挂了」这句有用的信息。
"""

from __future__ import annotations

from typing import Any

from ._log import logger
from .models import ProviderError
from .pandascore import PandaScoreProvider
from .scoregg import ScoreggProvider

PROVIDERS = ("scoregg", "pandascore")
PROVIDER_LABEL = {"scoregg": "玩加赛事", "pandascore": "PandaScore"}


def normalize_provider(value: Any, fallback: str = "scoregg") -> str:
    key = str(value or "").strip().lower()
    return key if key in PROVIDERS else fallback


class DataHub:
    """管理两个 provider 实例的生命周期，并做切源。"""

    def __init__(
        self,
        *,
        default_provider: str = "scoregg",
        default_token: str = "",
        timeout: float = 12.0,
        book: Any = None,
    ) -> None:
        self._default_provider = normalize_provider(default_provider)
        self._default_token = (default_token or "").strip()
        self._timeout = timeout
        self._book = book
        self._scoregg: ScoreggProvider | None = None
        # PandaScore 的 Key 是按会话可变的，所以按 token 缓存实例
        self._panda: dict[str, PandaScoreProvider] = {}

    # ------------------------------------------------------------------ 实例

    def _scoregg_provider(self) -> ScoreggProvider:
        if self._scoregg is None:
            self._scoregg = ScoreggProvider(timeout=self._timeout, book=self._book)
        return self._scoregg

    def _panda_provider(self, token: str) -> PandaScoreProvider:
        key = token or self._default_token
        cached = self._panda.get(key)
        if cached is None:
            cached = PandaScoreProvider(key, timeout=self._timeout)
            self._panda[key] = cached
        return cached

    def _pick(self, name: str, token: str) -> tuple[str, Any]:
        if name == "pandascore":
            effective = token or self._default_token
            if not effective:
                raise ProviderError(
                    "PandaScore 还没有配置 Key。用「/lol 设置 key xxx」"
                    "或在插件配置里填「PandaScore API Key」。",
                    retryable=False,
                )
            return "pandascore", self._panda_provider(effective)
        return "scoregg", self._scoregg_provider()

    async def close(self) -> None:
        if self._scoregg is not None:
            await self._scoregg.close()
            self._scoregg = None
        for provider in self._panda.values():
            await provider.close()
        self._panda.clear()

    # ------------------------------------------------------------------ 调用

    async def call(
        self,
        method: str,
        *,
        provider: str = "",
        token: str = "",
        **kwargs: Any,
    ) -> tuple[str, Any]:
        """依次尝试选定的源与备源，返回 ``(实际使用的源名, 结果)``。

        两个源都失败时抛最后一次的 ``ProviderError``，消息里带上两边的失败原因。
        """
        primary = normalize_provider(provider or self._default_provider)
        order = [primary] + [name for name in PROVIDERS if name != primary]
        errors: list[str] = []

        for name in order:
            try:
                used, instance = self._pick(name, token)
            except ProviderError as exc:
                errors.append(f"{PROVIDER_LABEL.get(name, name)}：{exc}")
                continue
            try:
                result = await getattr(instance, method)(**kwargs)
                if name != primary:
                    logger.info(
                        f"[LOL赛事] {PROVIDER_LABEL.get(primary, primary)} 不可用，"
                        f"本次改用 {PROVIDER_LABEL.get(name, name)}"
                    )
                return used, result
            except ProviderError as exc:
                errors.append(f"{PROVIDER_LABEL.get(name, name)}：{exc}")
                if not exc.retryable:
                    # 例如 Key 无效——换源也没用，直接把原因抛给用户
                    raise
                logger.warning(f"[LOL赛事] {PROVIDER_LABEL.get(name, name)} 调用失败：{exc}")
            except Exception as exc:  # noqa: BLE001 解析出错也要能切源
                errors.append(f"{PROVIDER_LABEL.get(name, name)}：{type(exc).__name__}: {exc}")
                logger.warning(
                    f"[LOL赛事] {PROVIDER_LABEL.get(name, name)} 异常：{type(exc).__name__}: {exc}"
                )

        raise ProviderError("；".join(errors) or "没有可用的数据源")
