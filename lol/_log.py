"""日志适配。

AstrBot 里有它自己的 ``logger``（带插件前缀、写进 WebUI 日志页）。
脱离 AstrBot 单独跑解析逻辑时（单元测试、离线排查）没有这个模块，
这里回退到标准库 logging，两套接口保持兼容。
"""

from __future__ import annotations

try:  # pragma: no cover - 取决于运行环境
    from astrbot.api import logger  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_lolmatch")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

__all__ = ["logger"]
