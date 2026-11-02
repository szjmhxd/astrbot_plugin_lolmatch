"""LOL 赛事助手的数据与逻辑层。

这个包刻意不依赖 AstrBot 的运行时对象（Context / Star），
只用标准库 + aiohttp，方便脱离框架跑单元测试。
"""

from __future__ import annotations

__all__ = ["models", "pandascore", "provider", "scoregg", "store", "windows", "workday"]
