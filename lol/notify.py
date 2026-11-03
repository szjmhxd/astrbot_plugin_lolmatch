"""消息发送与 @ 提醒。

@ 的三种档位
------------
- ``none``：不 @ 任何人，纯文本；
- ``all``：@全体成员；
- ``list``：@ 指定的人（用户 ID 列表）。

关于 @全体 的权限
-----------------
AstrBot 没有统一的「机器人是不是群管理」查询接口，各适配器行为也不同：
QQ（aiocqhttp）里非管理员发 @全体 会被服务端拒掉，KOOK 里则可能直接报错。
所以这里的做法是**先试再降级**：带 @ 发送，失败就立刻退成不带 @ 的纯文本，
并在日志里留一条 warning。用户侧永远能收到内容，最坏情况只是没 @ 到人。

这也意味着「@全体没有生效」最常见的原因不是你配错了，而是机器人没有群管理权限。
"""

from __future__ import annotations

import contextlib
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ._log import logger

try:  # AstrBot 不同版本位置略有差异
    from astrbot.core.message.message_event_result import MessageChain
except ImportError:  # pragma: no cover
    MessageChain = None  # type: ignore[assignment]

try:
    from astrbot.api.message_components import At
except ImportError:  # pragma: no cover
    At = None  # type: ignore[assignment]

try:
    from astrbot.api.message_components import AtAll
except ImportError:  # pragma: no cover
    AtAll = None  # type: ignore[assignment]

try:
    from astrbot.api.message_components import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]

try:
    from astrbot.api.message_components import Plain
except ImportError:  # pragma: no cover
    Plain = None  # type: ignore[assignment]

MENTION_MODES = ("none", "all", "list")


def normalize_mention(value: Any, fallback: str = "none") -> str:
    key = str(value or "").strip().lower()
    return key if key in MENTION_MODES else fallback


def mention_text(mode: str, targets: list[str]) -> str:
    """给「当前设置」这类回显用的中文描述。"""
    normalized = normalize_mention(mode)
    if normalized == "all":
        return "@全体成员"
    if normalized == "list":
        return "@ " + "、".join(targets) if targets else "@ 名单为空（等同于不 @）"
    return "不 @ 任何人"


class Notifier:
    """把「文本 + @ 策略」变成一次发送，并处理平台不支持的情况。"""

    def __init__(self, context: Any) -> None:
        self._context = context

    # ------------------------------------------------------------------ 构造

    def _chain(
        self,
        text: str,
        mode: str,
        targets: list[str],
        images: list[Any] | None = None,
    ) -> Any:
        if MessageChain is None or Plain is None:
            return None
        parts: list[Any] = []
        normalized = normalize_mention(mode)

        if normalized == "all" and AtAll is not None:
            try:
                parts.append(AtAll())
                parts.append(Plain("\n"))
            except Exception as exc:  # noqa: BLE001  # pragma: no cover
                logger.debug(f"[LOL赛事] 构造 @全体 失败：{exc}")
                parts = []
        elif normalized == "list" and At is not None:
            for target in targets:
                try:
                    parts.append(At(qq=str(target)))
                except Exception as exc:  # noqa: BLE001  # pragma: no cover
                    logger.debug(f"[LOL赛事] 构造 @{target} 失败：{exc}")
            if len(parts) > 0:
                parts.append(Plain("\n"))

        # 图放前面：群里先看到比分板，再读文字，比反过来顺手
        for image in images or []:
            parts.append(image)
        if text:
            parts.append(Plain(text))

        try:
            return MessageChain(parts)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            logger.debug(f"[LOL赛事] 构造消息链失败：{exc}")
            return None

    def _image_components(self, pngs: list[bytes]) -> tuple[list[Any], list[Path]]:
        """把 PNG 字节变成消息组件，返回组件和需要事后清理的临时文件。"""
        if Image is None:
            return [], []
        components: list[Any] = []
        files: list[Path] = []
        for png in pngs:
            target = Path(tempfile.gettempdir()) / f"lolmatch_{uuid.uuid4().hex}.png"
            try:
                target.write_bytes(png)
            except OSError as exc:
                logger.debug(f"[LOL赛事] 卡片临时文件写入失败：{exc}")
                continue
            files.append(target)
            try:
                # 优先走文件：各适配器对文件路径的支持最一致
                components.append(Image.fromFileSystem(str(target)))
            except Exception:  # noqa: BLE001 老版本没有这个类方法，退回 base64
                try:
                    components.append(Image.fromBytes(png))
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[LOL赛事] 构造图片消息失败：{exc}")
        return components, files

    # ------------------------------------------------------------------ 发送

    async def send(
        self,
        umo: str,
        text: str,
        *,
        mode: str = "none",
        targets: list[str] | None = None,
        images: list[bytes] | None = None,
    ) -> bool:
        """发一条消息，可选带图。失败时逐级降级：带图带 @ → 只带 @ → 纯文本。"""
        sender = getattr(self._context, "send_message", None)
        if not callable(sender):
            logger.warning("[LOL赛事] 当前 AstrBot 版本不支持主动发送，跳过本次推送")
            return False

        targets = targets or []
        components, files = self._image_components(images or [])

        attempts: list[Any] = []
        if components:
            attempts.append(self._chain(text, mode, targets, components))
        attempts.append(self._chain(text, mode, targets))
        attempts.append(text)

        try:
            for index, payload in enumerate(attempts):
                if payload is None:
                    continue
                try:
                    await sender(umo, payload)
                    return True
                except Exception as exc:  # noqa: BLE001 各平台异常类型不统一，只能兜住
                    if index == 0 and components:
                        logger.warning(
                            f"[LOL赛事] 带卡片发送失败，改为纯文字：{type(exc).__name__}: {exc}"
                        )
                    elif payload is not attempts[-1]:
                        logger.warning(
                            f"[LOL赛事] 带 @ 发送失败（多半是机器人没有群管理权限），"
                            f"降级为普通消息：{type(exc).__name__}: {exc}"
                        )
                    else:
                        logger.warning(f"[LOL赛事] 推送到 {umo} 失败：{type(exc).__name__}: {exc}")
            return False
        finally:
            # 图发完就没用了；删不掉也不影响功能，只是临时目录多几个文件
            for path in files:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
