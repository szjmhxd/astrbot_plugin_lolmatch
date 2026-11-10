"""日志入口。

日志一律走 AstrBot 的 ``logger``（带插件前缀、写进 WebUI 日志页，且受宿主配置的
日志级别与落盘策略管理）。**不做标准库 logging 回退**：一个插件两套日志出口会让
日志字段、级别、去向都分叉，宿主也收不到这些记录。

离线跑解析逻辑（单元测试、排查脚本）时由调用方先注入 ``astrbot.api``（mock 一个
带 info/warning/error/debug 方法的对象即可），不要在本模块里兜底。
"""

from __future__ import annotations

from astrbot.api import logger  # type: ignore[attr-defined]

__all__ = ["logger"]
