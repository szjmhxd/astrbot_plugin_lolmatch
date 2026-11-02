"""让插件目录可以被当作 Python 包导入。

AstrBot 自己是按路径加载 ``main.py`` 的，不依赖这个文件；它的作用是让
``from astrbot_plugin_lolmatch.main import LolMatchPlugin`` 这类写法在
单元测试和离线排查时也能用。
"""
