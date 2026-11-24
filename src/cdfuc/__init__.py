# 让所有类型注解延迟为“字符串形式”解析，而不是在定义时立刻求值
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

# 导入子模块，统一暴露给外部使用
from .archfuc import ArchFuc
from .basefuc import (
    ClassProxy,
    DefaultInstance,
    DelegatedBase,
    check_path,
    ensure_param,
    lazy_import,
    module,
)
from .charfuc import CharFuc
from .logfuc import LogFuc
from .netfuc import NetFuc
from .pathfuc import PathFuc
from .sysfuc import SysFuc, log_runtime, subprocess_run
from .timefuc import TimeFuc

try:
    __version__ = version("cdfuc-toolkit")
except PackageNotFoundError:
    __version__ = "26.00.00"

__all__ = [
    "__version__",
    "ArchFuc",
    "CharFuc",
    "ClassProxy",
    "DefaultInstance",
    "DelegatedBase",
    "LogFuc",
    "NetFuc",
    "PathFuc",
    "SysFuc",
    "TimeFuc",
    "check_path",
    "ensure_param",
    "lazy_import",
    "log_runtime",
    "module",
    "subprocess_run",
]
