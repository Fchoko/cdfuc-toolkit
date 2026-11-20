import random
import re
import time
from collections.abc import Sequence
from datetime import datetime, timedelta

from .basefuc import ClassProxy, DefaultInstance


class TimeFuc(DefaultInstance):
    """
    时间工具模块。

    提供无状态、高频调用的时间格式化、耗时度量、暂停与时刻调度功能。
    """

    # ============================== 常量 ==============================

    _HHMM_PATTERN = re.compile(r"^(?:\d|[01]\d|2[0-3]):[0-5]\d$")  # 24 小时制 'H:MM' 或 'HH:MM'

    # ============================== 内部工具 ==============================

    @staticmethod
    def _now() -> datetime:
        """
        获取当前本地 datetime。

        :return: 当前本地时间的 datetime 对象
        """
        return datetime.now()

    @classmethod
    def _check_hhmm(cls, time_str: str) -> bool:
        """
        校验时间字符串是否符合 24 小时制 'HH:MM'。

        :param time_str: 时间字符串
        :return: 校验成功返回 True
        """
        if not cls._HHMM_PATTERN.match(time_str):
            raise ValueError(f"The time ({time_str!r}) must be in 'HH:MM' format")
        return True

    @staticmethod
    def _format_hhmm(dt: datetime) -> str:
        """
        将 datetime 对象格式化为 'HH:MM'。

        :param dt: datetime 对象
        :return: 时间字符串
        """
        return dt.strftime("%H:%M")

    @classmethod
    def _parse_hhmm(cls, time_str: str) -> datetime:
        """
        将 'HH:MM' 时间字符串转换为今日对应的 datetime（秒、微秒为 0）。

        :param time_str: 时间字符串，如 '05:00'
        :return: 当日对应的 datetime 对象
        """
        cls._check_hhmm(time_str)

        hour, minute = map(int, time_str.split(":"))
        return cls._now().replace(hour=hour, minute=minute, second=0, microsecond=0)

    # ============================== 具体功能 ==============================

    @ClassProxy
    def monotonic(self) -> float:
        """
        获取单调时钟时间戳（秒），不受系统时钟调整影响，专供性能度量与速率计算。

        :return: 当前单调时间戳
        """
        return time.monotonic()

    @ClassProxy
    def current_time(self, fmt: str = "%Y-%m-%d %H:%M:%S", *, with_ms: bool = False) -> str:
        """
        获取当前时间的格式化字符串。

        当格式串中包含秒（%S）且启用 with_ms 时，自动在秒之后追加 3 位毫秒。

        :param fmt: 时间格式字符串，默认为 '%Y-%m-%d %H-%M-%S'
        :param with_ms: 是否在输出秒时附带 3 位毫秒，默认 False
        :return: 当前时间格式化字符串
        """
        now = self._now()

        if with_ms and "%S" in fmt:
            ms_str = f"{now.microsecond // 1000:03d}"
            sep = "-" if "-%S" in fmt or "%S-" in fmt else "."  # 若 %S 紧跟破折号或下划线，追加 '-{ms}'；若紧跟冒号或点，追加 '.{ms}'
            fmt = fmt.replace("%S", f"%S{sep}{ms_str}", 1)

        return now.strftime(fmt)

    @ClassProxy
    def measure_runtime(self, start: int | float = 0) -> tuple[float, float]:
        """
        计算程序运行时间。

        :param start:
            - 0：记录当前时间戳
            - 非 0：计算运行耗时
        :return: 元组 (开始时间戳, 已运行秒数)
        """
        now_ts = time.monotonic()
        if start == 0:
            return now_ts, 0.0
        return float(start), max(0.0, now_ts - float(start))

    @ClassProxy
    def sleep_seconds(self, *seconds: int | float | Sequence[int | float] | None) -> float:
        """
        暂停程序执行（支持固定或随机时间）。

        :param seconds:
            - 单值：固定秒数
            - 两值或元组/列表：(min, max) 随机范围
        :return: 实际暂停秒数
        :raises ValueError: 当参数超过 2 个或为空区间时抛出
        """
        if not seconds:
            return 0.0

        flat_args: list[float] = []  # 平铺可能传入的嵌套序列（如传入元组或列表 (1, 3)）
        for item in seconds:
            if item is None:
                continue
            if isinstance(item, (tuple, list)):
                flat_args.extend(float(x) for x in item)
            else:
                flat_args.append(float(item))

        if not flat_args:
            return 0.0

        if len(flat_args) == 1:  # 单个参数：固定暂停时间
            duration = flat_args[0]
        elif len(flat_args) == 2:  # 两个参数：随机暂停时间
            duration = random.uniform(*sorted(flat_args))
        else:  # 参数不符合要求
            raise ValueError(f"sleep_seconds expects 1 value or 2 values (range), but got {len(flat_args)}")

        if duration > 0:
            time.sleep(duration)
        return duration

    @ClassProxy
    def nearest_time(self, target: Sequence[str]) -> str:
        """
        计算距离当前时间最近的目标时间（跨天自动顺延）。

        :param target: 时间字符串序列，如 ('05:00', '12:30')
        :return: 最近时间点（'HH:MM'）
        :raises ValueError: 目标时间序列为空时抛出
        """
        if not target:
            raise ValueError("Target time list cannot be empty")

        now = self._now()
        candidates: list[datetime] = []  # 初始化经换算过的实时字符串

        for t in target:
            dt = self._parse_hhmm(t)  # 过滤并验证有效时间，将时间字符串转换为今天的 datetime 对象
            if dt < now:  # 如果目标时间已过，顺延至次日
                dt += timedelta(days=1)  # 计算预定时间相对于当前的值
            candidates.append(dt)  # 存储时间戳

        return self._format_hhmm(min(candidates))  # 返回最接近当前时间的时间

    @ClassProxy
    def wait_until(self, target: str) -> tuple[bool, tuple[int, int, int]]:
        """
        等待直到指定时间点（单步至多步进 1 秒，跨天自动顺延）。

        :param target: 目标时间 'HH:MM'
        :return: 元组 (是否到达目标时间, 剩余时间 (小时, 分钟, 秒))
        """
        now = self._now()
        target_time = self._parse_hhmm(target)
        if target_time <= now:
            target_time += timedelta(days=1)  # 目标时间已过时顺延至次日
        remaining = (target_time - now).total_seconds()  # 获取时间差秒数

        if remaining <= 0:  # 如果目标时间已到或已过，返回
            return True, (0, 0, 0)

        total_rem = int(remaining)
        hours, rem = divmod(total_rem, 3600)  # 计算剩余时间的小时、分钟、秒
        minutes, seconds = divmod(rem, 60)

        self.sleep_seconds(min(1.0, remaining))  # 每次至多暂停 1 秒，避免 0 秒空转
        is_reached = (remaining <= 1.0)
        return is_reached, (hours, minutes, seconds)
